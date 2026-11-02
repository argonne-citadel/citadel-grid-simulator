"""OpenDSS voltage boundaries and phase-correct PV terminal measurements.

Public helpers validate reset voltages and read PV electrical state. Bus
scalars are unsuitable for DER feedback: a multi-phase host reports one
phase, while the inverter may connect to another phase through a transformer.
PV voltage uses its actual terminal conductors and its declared kV rating,
independent of the host-bus phase ordering and OpenDSS bus-base assignment.
"""

import math
from datetime import datetime

import opendssdirect as dss

from ..schemas.common import GeneratorID
from ..schemas.state import GeneratorState

# Post-reset sanity band for _assert_post_reset_voltage_band(). This is NOT a
# C84.1 Range B compliance claim — it is wide enough to pass both stock
# feeders' own worst-case readings while still catching a base-mislabeling
# error by orders of magnitude. Derivation: a VoltageBases entry quoted
# line-to-neutral instead of line-to-line (or missing entirely) produced
# 0.853-0.866 pu on the IEEE-123 PV secondaries and ~280 pu on IEEE-37's
# (examples/Run_IEEE123_ZTCARD.dss, examples/IEEE37Bus_PV.dss) -- both are
# unambiguous failures the band below rejects. The stock feeders' own floors
# under correct bases are 0.926 pu (IEEE 8500 at peak load) and ~0.906 pu
# (IEEE-37's 3-wire delta primary node-to-ground reading, expected on a
# delta feeder and not a violation) -- both inside [0.9, 1.1]. IEEE-37's
# seeded reset jitter can lower that floor to ~0.9045 (seeds 0, 1, 42 over
# 24 hours); this is a sanity guard, not a voltage-compliance guarantee.
_RESET_VOLTAGE_BAND_PU = (0.9, 1.1)

# Below this pu, a node is islanded/de-energized (OpenDSS's 0.0-pu
# convention for a bus with no source path), not a voltage violation. Set
# at 1e-3, not the previously-used 0.1: a 10:1 base scaling error (the
# exact defect class this guard exists to catch) reads 0.095-0.106 pu
# measured, which a 0.1 floor would have silently ignored as "islanded" --
# hiding the offender the guard is supposed to report. Islanded buses read
# exactly 0.0, so 1e-3 still clears them with no false negatives.
_RESET_VOLTAGE_FLOOR_IGNORE_PU = 1e-3


def assert_post_reset_voltage_band() -> None:
    """Fail loudly if any energized node falls outside the sanity band.

    Runs once per reset(), after the post-reset power flow has already
    converged — convergence alone does not catch a base-mislabeling
    defect, since OpenDSS solves happily against a wrong kVBase and
    simply reports a nonsensical per-unit value (see
    _RESET_VOLTAGE_BAND_PU derivation above). This is the write
    boundary for post-reset state: check it once here rather than at
    every downstream reader.

    Raises:
        RuntimeError: If any non-islanded node lies outside
            _RESET_VOLTAGE_BAND_PU, naming up to 10 offenders (bus, pu,
            kVBase) and pointing at a misassigned voltage base as the
            first suspect.
    """
    offenders: list[tuple[str, float, float]] = []
    for bus_name in dss.Circuit.AllBusNames():
        dss.Circuit.SetActiveBus(bus_name)
        kv_base = dss.Bus.kVBase()
        voltages_angles = dss.Bus.puVmagAngle()
        voltages_pu = [voltages_angles[i] for i in range(0, len(voltages_angles), 2)]
        for voltage_pu in voltages_pu:
            # No raw-volts rescue here, unlike get_current_state(): a
            # nonzero kv_base means OpenDSS already reported this value
            # in per-unit, so a reading above _MAX_PLAUSIBLE_PU is a
            # real offender, not a volts/pu unit mismatch to repair.
            # Rescuing it (dividing by kv_base again) would silently
            # launder exactly the defect this guard exists to catch —
            # a base good enough to exist but wrong enough to produce
            # a nonsense per-unit value, which is precisely what a 10:1
            # base scaling error looks like.
            if voltage_pu < _RESET_VOLTAGE_FLOOR_IGNORE_PU:
                continue
            if not (
                _RESET_VOLTAGE_BAND_PU[0] <= voltage_pu <= _RESET_VOLTAGE_BAND_PU[1]
            ):
                offenders.append((bus_name, voltage_pu, kv_base))

    if offenders:
        sample = offenders[:10]
        raise RuntimeError(
            f"Post-reset power flow converged but {len(offenders)} "
            f"node(s) lie outside the sanity band {_RESET_VOLTAGE_BAND_PU} "
            "pu. First suspect: a misassigned VoltageBases entry (quoted "
            "line-to-neutral instead of line-to-line, or no base at all "
            "matching a secondary's winding kV/connection — see "
            "examples/Run_IEEE123_ZTCARD.dss and "
            "examples/IEEE37Bus_PV.dss for two confirmed cases). "
            f"Offenders (bus, pu, kVBase): {sample}"
        )


def read_pv_state(
    generator_id: GeneratorID, pv_name: str, timestamp: datetime
) -> GeneratorState:
    """Read terminal power, inverter-side phase voltage, and live nameplate.

    Single-phase wye PVs use phase-to-neutral voltage. Three-phase PVs use
    the mean of their phase-to-neutral magnitudes, normalized by kV/sqrt(3).
    Delta connections are rejected until an explicit line-voltage contract
    is implemented; silently applying a wye formula would corrupt feedback.
    """
    dss.PVsystems.Name(pv_name)
    dss.Circuit.SetActiveElement(f"PVSystem.{pv_name}")
    if dss.Properties.Value("conn").lower() not in ("wye", "ln"):
        raise RuntimeError(f"PV {pv_name}: inverter voltage requires a wye connection")
    phases = dss.CktElement.NumPhases()
    nominal_kv = float(dss.Properties.Value("kV"))
    nameplate_kw = dss.PVsystems.Pmpp()
    if nominal_kv <= 0 or nameplate_kw <= 0:
        raise RuntimeError(f"PV {pv_name}: non-positive kV or Pmpp rating")
    powers = dss.CktElement.Powers()
    voltages = dss.CktElement.VoltagesMagAng()[0 : 2 * phases : 2]
    nominal_ln_v = nominal_kv * 1000 / (math.sqrt(3) if phases > 1 else 1)
    voltage_pu = sum(voltages) / phases / nominal_ln_v
    if not math.isfinite(voltage_pu):
        raise RuntimeError(f"PV {pv_name}: non-finite terminal voltage")
    return GeneratorState(
        generator_id=generator_id,
        p_mw=-sum(powers[0 : 2 * phases : 2]) / 1000,
        q_mvar=-sum(powers[1 : 2 * phases : 2]) / 1000,
        voltage_pu=voltage_pu,
        nameplate_kw=nameplate_kw,
        timestamp=timestamp,
    )
