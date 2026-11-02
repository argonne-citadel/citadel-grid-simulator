"""
OpenDSS Regulator Interface.

This module provides OpenDSSRegulatorInterface, which encapsulates the
OpenDSS voltage-regulator control surface: regulator topology export, tap
control, and voltage-setpoint control.

Public interface:
- OpenDSSRegulatorInterface(engine): construct with a reference to the
  owning OpenDSSEngine.
- _get_regulators_topology() -> Dict[TransformerID, TransformerInfo]
- set_transformer_tap(transformer_id, tap_position) -> None
- get_regulator_tap_position(regulator_id) -> int
- set_regulator_voltage_setpoint(regulator_id, vreg) -> None
- get_regulator_voltage_setpoint(regulator_id) -> float

This module exists because opendss_engine.py exceeded the project's
1500-line file-size lint limit (scripts/lint_custom.py); the regulator
surface was the most cohesive seam to extract. Composition, not
inheritance: this class holds a reference to the owning OpenDSSEngine
rather than being mixed into it. That reference (rather than passing the
specific mapping/adjacency containers directly) is deliberate: both
_regulator_id_to_name and _transformer_adjacency are rebuilt — rebound to
brand-new dict/list objects, not mutated in place — by
OpenDSSEngine._build_id_mappings() on every reset(). Holding those
containers directly would capture a stale snapshot the first time reset()
runs; holding the engine and reading its attributes live avoids that.
"""

import logging
import math
from typing import TYPE_CHECKING, Dict

import opendssdirect as dss

from ..schemas.common import BusID, TransformerID
from ..schemas.topology import TransformerInfo

if TYPE_CHECKING:
    from .opendss_engine import OpenDSSEngine

logger = logging.getLogger(__name__)


def _vreg_bounds_from_pt(
    *,
    winding_kv: float,
    n_phases: int,
    is_delta: bool,
    pt_ratio: float,
    min_tap: float,
    max_tap: float,
) -> tuple[float, float]:
    """Compute a regulator's achievable ForwardVreg range on the PT secondary.

    Pure arithmetic, split out from `_get_regulator_vreg_bounds` so it can be
    tested with hand-computed cases for every winding type without loading a
    feeder — the bug this guards against (a 3-phase wye winding) does not
    exist on the only git-tracked feeder (IEEE-37, whose regulators are
    single-phase), so a feeder-loading test could not catch it.

    The regulator's PT is referenced line-to-NEUTRAL. The per-phase base
    voltage is therefore:
      - 3-phase WYE winding: `winding_kv / √3` — `dss.Transformers.kV()`
        reports the line-to-line rating for a wye winding, so dividing by √3
        recovers the phase voltage the PT actually measures.
      - single-phase winding, OR any delta winding: `winding_kv` unchanged —
        the rated kV already is the phase/terminal voltage, no √3 applies.

    Then `vreg_bound = base_volts × tap_ratio / pt_ratio`, with tap_ratio the
    transformer's own MinTap/MaxTap (the per-unit range the tap changer can
    physically reach).

    Worked cases (all MinTap/MaxTap 0.9/1.1, i.e. ±10%):
      - IEEE-37  creg (1-phase delta, kV 4.8, PT 40):
        4800 × 0.9 / 40 = 108.0 → [108.0, 132.0]
      - IEEE-123 creg1a (3-phase wye, kV 4.16, PT 20):
        (4160/√3) × 0.9 / 20 = 2402.2 × 0.9 / 20 = 108.1 → [108.1, 132.1]
        (the OLD formula gave 4160 × 0.9 / 20 = 187.2, which wrongly
        excluded this regulator's own shipped 120 V setpoint)
      - IEEE-8500 reg (1-phase wye, kV 7.2, PT 60):
        7200 × 0.9 / 60 = 108.0 → [108.0, 132.0]
    """
    if n_phases == 3 and not is_delta:
        base_volts = winding_kv * 1000.0 / math.sqrt(3.0)
    else:
        base_volts = winding_kv * 1000.0

    vreg_min = base_volts * min_tap / pt_ratio
    vreg_max = base_volts * max_tap / pt_ratio
    return vreg_min, vreg_max


class OpenDSSRegulatorInterface:
    """OpenDSS voltage-regulator control surface, extracted from OpenDSSEngine.

    Constructed with a reference to the owning OpenDSSEngine so it can read
    the live regulator-name mapping and transformer-adjacency state that
    OpenDSSEngine._build_id_mappings() rebuilds on every reset() — see the
    module docstring for why a reference is held rather than the individual
    containers.
    """

    def __init__(self, engine: "OpenDSSEngine") -> None:
        self._engine = engine

    def _get_regulators_topology(self) -> Dict[TransformerID, TransformerInfo]:
        """Build the regulator dict: same TransformerInfo shape as a plain
        transformer, but keyed by RegControl (see _build_id_mappings) and
        carrying live tap fields.

        tap_min/tap_max are derived from the winding's NumTaps() (ANSI
        convention: tap number ranges over [-NumTaps/2, +NumTaps/2]), not
        hardcoded, so a model with a non-default tap range is reported
        correctly rather than silently assumed to be +-16.
        """
        regulators: Dict[TransformerID, TransformerInfo] = {}
        for reg_id, reg_name in self._engine._regulator_id_to_name.items():
            try:
                dss.RegControls.Name(reg_name)
                xfm_name = dss.RegControls.Transformer()
                tap_position = int(dss.RegControls.TapNumber())

                match = next(
                    (
                        (a, b)
                        for a, b, name in self._engine._transformer_adjacency
                        if name.lower() == f"transformer.{xfm_name}".lower()
                    ),
                    None,
                )
                if match is None:
                    raise RuntimeError(
                        f"Regulator {reg_name} controls transformer "
                        f"{xfm_name}, but no matching entry was found in "
                        "_transformer_adjacency — RegControls and "
                        "PDElements iteration disagree."
                    )
                from_bus_id, to_bus_id = match

                kv_hv, kv_lv, kva_rated, xhl = (
                    self._engine._read_transformer_winding_electrical(xfm_name)
                )
                dss.Transformers.Name(xfm_name)
                dss.Transformers.Wdg(2)
                num_taps = dss.Transformers.NumTaps()
                tap_half_range = int(num_taps) // 2

                regulators[reg_id] = TransformerInfo(
                    transformer_id=reg_id,
                    name=xfm_name,
                    hv_bus=BusID(from_bus_id),
                    lv_bus=BusID(to_bus_id),
                    rated_power_mva=max(kva_rated / 1000.0, 0.001),
                    hv_voltage_kv=max(kv_hv, 0.001),
                    lv_voltage_kv=max(kv_lv, 0.001),
                    reactance_percent=max(xhl, 0.0),
                    resistance_percent=0.0,
                    tap_position=tap_position,
                    tap_min=-tap_half_range,
                    tap_max=tap_half_range,
                )
            except Exception as exc:
                logger.warning("Skipping regulator %s in topology: %s", reg_name, exc)
        return regulators

    def set_transformer_tap(
        self, transformer_id: TransformerID, tap_position: int
    ) -> None:
        """Force a voltage regulator's tap position via its RegControl.

        Only the tap-controlled subset of transformers identified as
        regulators in _build_id_mappings() can be commanded here — a plain
        transformer has no RegControl and therefore no controllable tap in
        this model.

        Does NOT survive a solve under automatic control. OpenDSS's
        RegControl runs closed-loop voltage regulation on every Solve() —
        it re-targets the tap to hold ForwardVreg±ForwardBand regardless of
        who last set it. A tap forced here will be visible immediately (the
        read-back and the resulting voltage both reflect it) but the next
        run_simulation() call re-solves the circuit, the RegControl sees a
        voltage away from its own setpoint, and drives the tap back to
        whatever satisfies ForwardVreg. Confirmed empirically: a commanded
        tap of -10 reverted to 16 after the following solve. This makes tap
        forcing the wrong primitive for a persistent voltage-control attack
        (see set_regulator_voltage_setpoint for that); it remains correct
        for a scenario that first inhibits automatic RegControl operation
        and then forces a tap — not implemented here since nothing in this
        engine currently disables RegControl action.

        Raises:
            KeyError: If transformer_id is not a known regulator.
            ValueError: If tap_position is outside the regulator's tap range.
            RuntimeError: If the OpenDSS API call to read bounds or apply
                the tap fails.
        """
        if transformer_id not in self._engine._regulator_id_to_name:
            raise KeyError(f"Regulator {transformer_id} not found")

        reg_name = self._engine._regulator_id_to_name[transformer_id]

        try:
            dss.RegControls.Name(reg_name)
            xfm_name = dss.RegControls.Transformer()
            winding = dss.RegControls.Winding()
            dss.Transformers.Name(xfm_name)
            dss.Transformers.Wdg(winding)
            num_taps = dss.Transformers.NumTaps()
        except Exception as e:
            raise RuntimeError(
                f"Failed to read tap bounds for regulator {reg_name}: {e}"
            )

        tap_half_range = int(num_taps) // 2
        tap_min, tap_max = -tap_half_range, tap_half_range
        if tap_position < tap_min or tap_position > tap_max:
            raise ValueError(
                f"Tap position {tap_position} out of range "
                f"[{tap_min}, {tap_max}] for regulator {reg_name}"
            )

        try:
            dss.RegControls.Name(reg_name)
            dss.RegControls.TapNumber(tap_position)
        except Exception as e:
            raise RuntimeError(f"Failed to set tap position: {e}")

        logger.info(
            "Set regulator %s (transformer %s) tap -> %d",
            reg_name,
            xfm_name,
            tap_position,
        )

    def get_regulator_tap_position(self, regulator_id: TransformerID) -> int:
        """Read the live tap position of a voltage regulator's RegControl.

        Not part of the PowerSystemEngine base interface — a per-engine
        capability, mirroring how set_bus_reactive_injection is exposed only
        on PandaPowerEngine. Modbus readback (HR 6000+) uses this via
        getattr, matching that precedent. This is the observable that shows
        a regulator responding to a voltage-setpoint command — see
        set_regulator_voltage_setpoint's docstring for why tap position
        itself is read-only from the supervisory side in this scenario.

        Raises:
            KeyError: If regulator_id is not a known regulator.
            RuntimeError: If the OpenDSS API call fails.
        """
        if regulator_id not in self._engine._regulator_id_to_name:
            raise KeyError(f"Regulator {regulator_id} not found")

        reg_name = self._engine._regulator_id_to_name[regulator_id]
        try:
            dss.RegControls.Name(reg_name)
            return int(dss.RegControls.TapNumber())
        except Exception as e:
            raise RuntimeError(
                f"Failed to read tap position for regulator {reg_name}: {e}"
            )

    def _get_regulator_vreg_bounds(self, reg_name: str) -> tuple[float, float]:
        """Derive the achievable ForwardVreg range from the device itself.

        ForwardVreg is expressed on the RegControl's PT secondary base (the
        same base the RegControl uses internally, not a per-unit fraction of
        nominal — confirmed empirically against IEEE37Bus_PV.dss's default
        regulators: PTRatio=40, winding kV()=4.8kV, MinTap/MaxTap=0.9/1.1
        gives a bound of [108, 132], which is exactly the textbook ANSI
        108-132V range for a 120V-base voltage regulator.

        The PT is referenced line-to-NEUTRAL. `dss.Transformers.kV()` returns
        the winding's rated kV, which for a 3-phase **wye** winding is the
        line-to-LINE value — so the per-phase base is that kV divided by √3.
        For a single-phase winding, or a delta winding, the rated kV already
        is the phase voltage and no √3 applies. `_vreg_bounds_from_pt`
        encodes that; see its docstring for the per-winding-type derivation.
        Reading phase count, connection, tap range and PT ratio from the live
        circuit rather than hardcoding a 108–132 V constant means a
        differently-configured regulator gets its own correct bound.
        """
        dss.RegControls.Name(reg_name)
        winding = dss.RegControls.Winding()
        pt_ratio = dss.RegControls.PTRatio()
        xfm_name = dss.RegControls.Transformer()

        dss.Circuit.SetActiveElement(f"Transformer.{xfm_name}")
        n_phases = dss.CktElement.NumPhases()

        dss.Transformers.Name(xfm_name)
        dss.Transformers.Wdg(winding)
        winding_kv = dss.Transformers.kV()
        is_delta = dss.Transformers.IsDelta()
        min_tap = dss.Transformers.MinTap()
        max_tap = dss.Transformers.MaxTap()

        return _vreg_bounds_from_pt(
            winding_kv=winding_kv,
            n_phases=n_phases,
            is_delta=is_delta,
            pt_ratio=pt_ratio,
            min_tap=min_tap,
            max_tap=max_tap,
        )

    def set_regulator_voltage_setpoint(
        self, regulator_id: TransformerID, vreg: float
    ) -> None:
        """Command a voltage regulator's ForwardVreg (target voltage).

        This is the supervisory-legitimate control path: the RegControl's
        own closed-loop tap-changing logic then moves the tap to hold this
        target on every subsequent Solve(), so — unlike set_transformer_tap
        — the effect persists across solves rather than being overwritten
        by automatic regulation. See set_transformer_tap's docstring for
        the measured tap-reversion behavior that motivated commanding the
        setpoint instead of the tap directly.

        Units: ForwardVreg is in volts on the RegControl's PT secondary
        base (typically ~120V nominal for a single-phase regulator; NOT a
        per-unit 0-1-ish fraction). See _get_regulator_vreg_bounds for the
        derivation and empirical confirmation of this convention.

        Does not call Solve() — the new target only takes physical effect
        (moves the tap, moves bus voltage) on the next run_simulation()
        call, same as every other setpoint in this engine.

        Raises:
            KeyError: If regulator_id is not a known regulator.
            ValueError: If vreg is outside the regulator's achievable range.
            RuntimeError: If the OpenDSS API call to read bounds or apply
                the setpoint fails.
        """
        if regulator_id not in self._engine._regulator_id_to_name:
            raise KeyError(f"Regulator {regulator_id} not found")

        reg_name = self._engine._regulator_id_to_name[regulator_id]

        try:
            vreg_min, vreg_max = self._get_regulator_vreg_bounds(reg_name)
        except Exception as e:
            raise RuntimeError(
                f"Failed to read voltage setpoint bounds for regulator {reg_name}: {e}"
            )

        if vreg < vreg_min or vreg > vreg_max:
            raise ValueError(
                f"Voltage setpoint {vreg} out of range "
                f"[{vreg_min:.2f}, {vreg_max:.2f}] for regulator {reg_name}"
            )

        try:
            dss.RegControls.Name(reg_name)
            dss.RegControls.ForwardVreg(vreg)
        except Exception as e:
            raise RuntimeError(f"Failed to set voltage setpoint: {e}")

        logger.info(
            "Set regulator %s voltage setpoint -> %.2f V (PT secondary base)",
            reg_name,
            vreg,
        )

    def get_regulator_voltage_setpoint(self, regulator_id: TransformerID) -> float:
        """Read a voltage regulator's live ForwardVreg target.

        Not part of the PowerSystemEngine base interface — see
        get_regulator_tap_position for the precedent this follows.

        Raises:
            KeyError: If regulator_id is not a known regulator.
            RuntimeError: If the OpenDSS API call fails.
        """
        if regulator_id not in self._engine._regulator_id_to_name:
            raise KeyError(f"Regulator {regulator_id} not found")

        reg_name = self._engine._regulator_id_to_name[regulator_id]
        try:
            dss.RegControls.Name(reg_name)
            return float(dss.RegControls.ForwardVreg())
        except Exception as e:
            raise RuntimeError(
                f"Failed to read voltage setpoint for regulator {reg_name}: {e}"
            )
