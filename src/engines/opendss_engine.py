"""
OpenDSS Engine Implementation.

This module provides a PowerSystemEngine implementation that uses OpenDSS
directly via the OpenDSSDirect.py Python wrapper.

Features:
- Direct OpenDSS Integration: Uses OpenDSSDirect.py library
- Full PowerSystemEngine Interface: Implements all required methods
- Time-Stepped Simulation: Advances simulation timestep by timestep
- State Synchronization: Maintains mapping between OpenDSS and engine IDs

Architecture:
- In-Process Execution: OpenDSS runs directly in Python process
- Stateful Circuit: Maintains loaded DSS circuit between calls
- ID Mapping: Converts between string names (OpenDSS) and integer IDs (engine)
- Error Handling: OpenDSS exceptions translated to engine exceptions

Use Cases:
- Power flow analysis with OpenDSS
- Time-stepped distribution system simulation
- Integration with grid-simulator SCADA interfaces
"""

import logging
import math
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, Iterator, Optional

import opendssdirect as dss

from ..schemas.common import (
    BusID,
    BusType,
    DERType,
    DeviceStatus,
    GeneratorID,
    LineID,
    LoadID,
    PowerMVAr,
    PowerMW,
    StorageID,
    TransformerID,
)
from ..schemas.results import PowerFlowConfig, PowerFlowResult
from ..schemas.state import (
    GeneratorState,
    GridState,
    LineState,
    LoadState,
    StorageState,
    TransformerState,
)
from ..schemas.topology import (
    BusInfo,
    GeneratorInfo,
    LineInfo,
    LoadInfo,
    NetworkTopology,
    StorageInfo,
    TransformerInfo,
)
from ..sim_time import compute_sim_now, get_anchor
from .base import PowerSystemEngine
from .opendss_buses import _MAX_PLAUSIBLE_PU as _MAX_PLAUSIBLE_PU
from .opendss_buses import read_bus_states
from .opendss_regulators import OpenDSSRegulatorInterface
from .opendss_voltage import (
    _RESET_VOLTAGE_BAND_PU,
    _RESET_VOLTAGE_FLOOR_IGNORE_PU,
    assert_post_reset_voltage_band,
    read_pv_state,
)

logger = logging.getLogger(__name__)


class OpenDSSEngine(PowerSystemEngine):
    """
    OpenDSS implementation of PowerSystemEngine.

    This engine uses OpenDSSDirect.py to run OpenDSS simulations directly
    in the Python process, providing full PowerSystemEngine functionality.
    """

    def __init__(
        self,
        dss_file_path: str,
        fuzz_seed: Optional[int] = None,
        pinned_hour: Optional[int] = None,
    ):
        """
        Initialize the OpenDSS engine.

        Args:
            dss_file_path: Path to DSS circuit file to load
            fuzz_seed: Seed for the per-engine RNG that drives reset()'s load
                fuzz and no-anchor fallback hour. Default None draws from the
                OS entropy pool, so each engine instance produces a distinct
                sequence of operating points — the historical behaviour, and
                what repeated-measures statistics across episodes require.

                Supply an explicit seed to make two engine instances draw
                identically. That is what paired-baseline harm measurement
                needs (see docs/decisions/paired_baseline_harm_measurement.md):
                a with-attack and a without-attack run must differ only by the
                attack, and reset()'s +/-5% load fuzz is two orders of
                magnitude larger than the effect being resolved.
            pinned_hour: Explicit no-anchor fallback hour in [0, 23] (see
                solution_hour()). Default None preserves today's behaviour:
                reset() draws it from the RNG uniformly in [6, 18]. Supplying
                a value makes the no-anchor fallback hour an explicit,
                reproducible parameter instead of an RNG draw — load-bearing
                for paired-baseline measurement, since IEEE-123 fleet PV
                output is a discrete function of hour (0 kW for hours 6-8;
                see docs/decisions/paired_baseline_harm_measurement.md) and an
                undocumented draw previously decided whether a curtailment
                attack had anything to curtail.

        Raises:
            ValueError: If pinned_hour is supplied and outside [0, 23].
        """
        if pinned_hour is not None and pinned_hour not in range(24):
            raise ValueError(
                f"pinned_hour must be in [0, 23], got {pinned_hour}. Hours "
                "6-8 produce exactly 0 kW fleet PV output on IEEE-123 (see "
                "docs/decisions/paired_baseline_harm_measurement.md); hours "
                "outside [0, 23] are not valid clock values at all."
            )
        self.dss_file_path = dss_file_path
        self._pinned_hour_override = pinned_hour

        # Private RNG instance, never the `random` module's global state.
        # The global is shared process-wide, so anything else that seeds it
        # (a test, a library, another component in the same process) would
        # silently perturb the fuzz draw, and seeding it here would reach
        # back out and perturb them. A private Random keeps the determinism
        # contract local to this engine.
        self._fuzz_seed = fuzz_seed
        self._rng = random.Random(fuzz_seed)

        # ID mapping: OpenDSS uses string names, we use integer IDs
        self._bus_name_to_id: Dict[str, BusID] = {}
        self._bus_id_to_name: Dict[BusID, str] = {}
        self._line_name_to_id: Dict[str, LineID] = {}
        self._line_id_to_name: Dict[LineID, str] = {}
        # Maintained by set_breaker_status and cleared by reset() so the Modbus
        # status coils can read live breaker state without a per-line OpenDSS
        # query every step.
        self._open_line_ids: set[LineID] = set()
        self._load_name_to_id: Dict[str, LoadID] = {}
        self._load_id_to_name: Dict[LoadID, str] = {}
        self._pv_name_to_id: Dict[str, GeneratorID] = {}
        self._pv_id_to_name: Dict[GeneratorID, str] = {}

        # Voltage regulators: transformers with a RegControl attached.
        # Keyed by RegControl name (one RegControl per regulated winding),
        # in a separate ID space from _transformer_adjacency's plain
        # transformers — see _build_id_mappings() for why a regulator is
        # relabeled out of the generic transformer set rather than
        # appearing in both.
        self._regulator_name_to_id: Dict[str, TransformerID] = {}
        self._regulator_id_to_name: Dict[TransformerID, str] = {}
        self._regulator_transformer_names: set[str] = set()

        # Last power flow result
        self._last_pf_result: Optional[PowerFlowResult] = None

        # Fallback hour used only when no sim-clock anchor is available
        # (standalone/UI/demo — see solution_hour()). Randomized on reset()
        # so different runs exercise different irradiance operating points.
        # Irrelevant to anchored runs, which derive the hour from sim time.
        self._pinned_hour: int = 8

        # Set before _load_circuit() (which calls solution_hour()) so the
        # no-anchor fallback logs at most once per engine instance.
        self._logged_no_anchor_fallback: bool = False

        # Source bus name (slack) — populated by _build_id_mappings()
        self._source_bus_name: str = ""

        # Non-line PDElement connections (transformers, reactors) as (from_id, to_id)
        # pairs.  Populated by _build_id_mappings(); exposed in get_topology() as
        # synthetic line entries so the zone partitioner sees a fully connected graph
        # even when the slack bus is only reachable via a substation transformer.
        self._transformer_adjacency: list[tuple[int, int, str]] = []

        # Regulator control surface, extracted to its own module (see
        # opendss_regulators.py's module docstring for why). Holds a
        # reference back to this engine rather than a snapshot of its
        # mapping dicts, since _build_id_mappings() rebinds those dicts
        # (not mutates them in place) on every reset().
        self._regulator_interface = OpenDSSRegulatorInterface(self)

        # Initialize: load circuit and build ID mappings
        self._load_circuit()
        self._build_id_mappings()

        logger.info(
            f"OpenDSS engine initialized with {len(self._bus_name_to_id)} buses"
        )

    def _load_circuit(self) -> None:
        """Load the DSS circuit file."""
        try:
            dss_path = Path(self.dss_file_path)

            if not dss_path.exists():
                raise FileNotFoundError(f"DSS file not found: {self.dss_file_path}")

            logger.info(f"Loading DSS circuit from {self.dss_file_path}")

            # dss.Text.Command (not the deprecated dss.run_command) raises
            # DSSException on a failed Clear/Compile instead of returning the
            # error string for the caller to notice or ignore. run_command
            # returning silently on a bad Compile (e.g. a missing file) was
            # the root cause of Redmine #1294: reset() then proceeded against
            # whatever circuit state Clear had left behind.
            dss.Text.Command("Clear")
            dss.Text.Command(f"Compile [{self.dss_file_path}]")

            # Daily mode with pinned hour: loadshapes (irradiance, temperature)
            # are evaluated at Solution.Hour(), giving hour-dependent PV output.
            # Snapshot mode (Mode=0) does NOT evaluate daily loadshapes — PVs
            # use their base irrad value regardless of hour, eliminating diurnal
            # diversity.  Instead, we keep Daily mode but pin the hour before
            # each Solve() in run_simulation() to prevent auto-advancement:
            # without an explicit pin, Daily mode's Number(1)/StepSize(3600)
            # advances the loadshape by one hour per Solve(), which under the
            # substrate's SIM_RATE=2880 (one solve covers 0.80 sim-hours) runs
            # the loadshape at 1.25x simulated time — a 25% phase drift.  The
            # value pinned here now tracks the sim clock (see solution_hour())
            # instead of being frozen for the whole run, which makes the
            # loadshape run at exactly 1.0x simulated time at any sim rate and
            # matches der_profiles.py (pandapower), which already derives its
            # hour from the same clock anchor.
            dss.Solution.Mode(1)  # Daily — evaluates loadshapes at Hour()
            dss.Solution.Number(1)  # 1 step per Solve()
            dss.Solution.StepSize(3600)  # 1 hour per step
            dss.Solution.Hour(self.solution_hour())

            # Get circuit info
            circuit_name = dss.Circuit.Name()
            num_buses = dss.Circuit.NumBuses()
            num_nodes = dss.Circuit.NumNodes()
            num_elements = dss.Circuit.NumCktElements()

            # Text.Command raising on a bad Compile handles a hard failure,
            # but a Compile can also "succeed" (no exception) against a
            # syntactically valid file that declares a circuit with no
            # buses or elements in it — confirmed against a minimal
            # `New Circuit.*` fixture with no lines/loads/transformers,
            # which compiles cleanly and reports NumBuses()==0. That is the
            # same empty-circuit hazard Redmine #1294 describes, just
            # reached without an exception, so it needs its own guard.
            if num_buses <= 0 or num_elements <= 0:
                raise RuntimeError(
                    f"OpenDSS compiled '{self.dss_file_path}' without error "
                    f"but produced an empty circuit (num_buses={num_buses}, "
                    f"num_elements={num_elements}). Refusing to proceed with "
                    "a cleared/empty circuit."
                )

            logger.info(
                f"Loaded circuit: {circuit_name} ({num_buses} buses, {num_nodes} nodes)"
            )

        except Exception as e:
            raise RuntimeError(f"Failed to load circuit: {e}")

    def _build_id_mappings(self) -> None:  # noqa: C901, PLR0915
        """Build bidirectional mappings between names and integer IDs.

        Also identifies the slack (source) bus from dss.Vsources.BusNames() so
        get_bus_info() can return BusType.SLACK for the feeder head.  IEEE37's
        source is "sourcebus"; the name is matched case-insensitively by name
        rather than by position so that AllBusNames() ordering does not matter.
        """
        # Reset all mappings — safe to call multiple times (e.g. from reset())
        self._bus_name_to_id = {}
        self._bus_id_to_name = {}
        self._line_name_to_id = {}
        self._line_id_to_name = {}
        self._load_name_to_id = {}
        self._load_id_to_name = {}
        self._pv_name_to_id = {}
        self._pv_id_to_name = {}
        self._transformer_adjacency = []
        self._regulator_name_to_id = {}
        self._regulator_id_to_name = {}
        self._regulator_transformer_names = set()

        # Force a Solve() before enumerating buses. OpenDSS only fully
        # populates the bus list (including auto-generated internal "_int"
        # nodes from line splits / transformer taps used in models like
        # IEEE8500) after solver activation. Without this, AllBusNames()
        # below returns only the declared buses, and downstream line
        # iteration sees Bus1/Bus2 references that aren't in the mapping —
        # which until task #47 silently produced (0, 0) sentinels (every
        # IEEE8500 line collapsed to a phantom edge between bus 0 and bus 0).
        # The audit fix made one of two iteration paths raise; this Solve()
        # call addresses the root cause so both paths can find their buses.
        dss.Solution.Solve()

        # Identify slack bus via the first Vsource element.
        # dss.Vsources.AllNames() → element names (e.g. ['source']).
        # Activate the element, then read CktElement.BusNames() to get the
        # connected bus name (returned as "busname.phase1.phase2..." — strip phases).
        vsource_names = dss.Vsources.AllNames()
        if vsource_names:
            dss.Vsources.Name(vsource_names[0])
            bus_names = dss.CktElement.BusNames()
            self._source_bus_name = (
                bus_names[0].split(".")[0].lower() if bus_names else ""
            )
        else:
            self._source_bus_name = ""
        if self._source_bus_name:
            logger.info("OpenDSS slack (source) bus: '%s'", self._source_bus_name)
        else:
            logger.warning(
                "No Vsource bus found — BFS will fail if SLACK bus is required"
            )

        # Map buses and collect geographic coordinates (from BusCoords.csv).
        # Bus names are lowercased for consistent lookup — OpenDSS names are
        # case-insensitive but CktElement.BusNames() returns lowercase.
        buses = dss.Circuit.AllBusNames()
        if not buses:
            raise RuntimeError(
                "OpenDSS AllBusNames() returned empty after Compile + Solve. "
                "Circuit is not in a usable state — every downstream line "
                "iteration would resolve to silent (0, 0) phantom edges. "
                "Check the DSS file path and any Compile errors logged above."
            )
        self._bus_coords: dict[int, tuple[float, float]] = {}
        for idx, bus_name_raw in enumerate(buses):
            bus_id = BusID(idx)
            bus_name = bus_name_raw.lower()
            self._bus_name_to_id[bus_name] = bus_id
            self._bus_id_to_name[bus_id] = bus_name
            dss.Circuit.SetActiveBus(bus_name)
            bx, by = dss.Bus.X(), dss.Bus.Y()
            if bx != 0.0 or by != 0.0:
                self._bus_coords[int(bus_id)] = (bx, by)
        if self._bus_coords:
            logger.info(
                "Bus coordinates loaded: %d / %d buses have geographic coords",
                len(self._bus_coords),
                len(buses),
            )

        # Map lines and capture bus endpoints via the Lines iterator.
        # IMPORTANT: dss.Lines.AllNames() and the Lines.First/Next iterator
        # return DIFFERENT orders and counts on large models (IEEE 8500:
        # AllNames=3703, iterator=3698, order diverges after ~287). We use
        # the iterator as the authoritative source since it provides stable
        # Bus1/Bus2 access. Lines missed by the iterator (switches, DER
        # step-ups) are added via AllNames fallback with SetActiveElement.
        self._line_buses: dict[int, tuple[int, int]] = {}
        seen_names: set[str] = set()
        ret = dss.Lines.First()
        idx = 0
        while ret > 0:
            line_name = dss.Lines.Name()
            line_id = LineID(idx)
            self._line_name_to_id[line_name] = line_id
            self._line_id_to_name[line_id] = line_name
            seen_names.add(line_name.lower())
            b1 = dss.Lines.Bus1().split(".")[0].lower()
            b2 = dss.Lines.Bus2().split(".")[0].lower()
            # Apply the same fail-loud check as the AllNames fallback path
            # below (task #47). Previously this used .get(b, BusID(0)),
            # silently collapsing any unknown bus to bus 0 — that pattern
            # masked the IEEE8500 bus-enumeration bug for thousands of lines
            # while only the AllNames-fallback path (a handful of lines)
            # surfaced it. Both paths must enforce the same contract.
            if b1 not in self._bus_name_to_id or b2 not in self._bus_name_to_id:
                raise RuntimeError(
                    f"Line.{line_name} references unknown buses "
                    f"({b1!r}, {b2!r}) not in "
                    f"{sorted(self._bus_name_to_id)[:10]}... — bus-name "
                    f"table is out of sync with line iteration. (Iterator "
                    f"path; AllNames path applies the same check.)"
                )
            self._line_buses[int(line_id)] = (
                int(self._bus_name_to_id[b1]),
                int(self._bus_name_to_id[b2]),
            )
            idx += 1
            ret = dss.Lines.Next()
        # Contract check: dss.Lines.First/Next iterates only enabled lines,
        # so the set difference {AllNames} \ {iterator} must equal the set
        # of disabled lines (DSS source `enabled=False`). Anything else
        # would mean the iterator silently skipped an enabled line — a
        # dss-python contract violation that must surface loudly rather
        # than be absorbed by a defensive registration path (which is what
        # the previous "fallback" code did, masking exactly this class of
        # bug). IEEE8500 has 5 disabled switches (WD701_48332_sw and
        # friends); their buses are never activated by OpenDSS so the
        # AllBusNames table omits them, and including disabled switches
        # in the topology graph would create phantom edges.
        disabled_skipped: list[str] = []
        unexpected_iterator_misses: list[str] = []
        for line_name in dss.Lines.AllNames():
            if line_name.lower() in seen_names:
                continue
            dss.Circuit.SetActiveElement(f"Line.{line_name}")
            if not dss.CktElement.Enabled():
                disabled_skipped.append(line_name)
                continue
            unexpected_iterator_misses.append(line_name)

        if unexpected_iterator_misses:
            raise RuntimeError(
                f"dss.Lines.First/Next iterator missed "
                f"{len(unexpected_iterator_misses)} enabled line(s): "
                f"{unexpected_iterator_misses[:5]}"
                f"{'...' if len(unexpected_iterator_misses) > 5 else ''} "
                f"— violates the dss-python documented contract. Silently "
                f"adding these via a fallback would mask the underlying "
                f"issue (possible dss-python version drift); investigate "
                f"before proceeding."
            )

        if disabled_skipped:
            logger.info(
                "Skipped %d disabled line(s) (enabled=False in DSS source): %s",
                len(disabled_skipped),
                ", ".join(disabled_skipped[:5])
                + (" ..." if len(disabled_skipped) > 5 else ""),
            )
        logger.info(
            "Lines mapped from iterator: %d enabled (%d disabled lines "
            "excluded per OpenDSS contract)",
            idx,
            len(disabled_skipped),
        )

        # Map loads
        loads = dss.Loads.AllNames()
        for idx, load_name in enumerate(loads):
            load_id = LoadID(idx)
            self._load_name_to_id[load_name] = load_id
            self._load_id_to_name[load_id] = load_name

        # Map PV systems
        pvsystems = dss.PVsystems.AllNames()
        for idx, pv_name in enumerate(pvsystems):
            pv_id = GeneratorID(idx)
            self._pv_name_to_id[pv_name] = pv_id
            self._pv_id_to_name[pv_id] = pv_name

        # Collect non-line branch connections from PDElements (transformers, reactors, etc.)
        # These are missing from dss.Lines.AllNames() but are required for a fully
        # connected adjacency graph.  Typical examples: substation transformer
        # (sourcebus → feeder head), PV step-up transformers (trafo_pv* ↔ feeder bus),
        # voltage regulator transformers (799 ↔ 799r in IEEE37).
        # Each entry is (from_bus_id, to_bus_id, element_name) so get_topology()
        # can build proper TransformerInfo objects.
        self._transformer_adjacency = []
        ret = dss.PDElements.First()
        while ret > 0:
            elem_name = dss.PDElements.Name()
            if not elem_name.lower().startswith("line."):
                dss.Circuit.SetActiveElement(elem_name)
                raw_buses = dss.CktElement.BusNames()
                # Strip phase suffixes and deduplicate (preserving order)
                clean = list(
                    dict.fromkeys(
                        b.split(".")[0].lower() for b in raw_buses if b.strip()
                    )
                )
                if len(clean) >= 2:
                    id_a = self._bus_name_to_id.get(clean[0])
                    id_b = self._bus_name_to_id.get(clean[1])
                    if id_a is not None and id_b is not None and id_a != id_b:
                        self._transformer_adjacency.append(
                            (int(id_a), int(id_b), elem_name)
                        )
            ret = dss.PDElements.Next()
        logger.info(
            "Non-line branch connections collected: %d transformer adjacency pairs",
            len(self._transformer_adjacency),
        )

        # Identify voltage regulators: a RegControl names the transformer it
        # operates, so this is a relabeling of a subset of the transformers
        # just collected above, never an addition. get_topology() uses
        # _regulator_transformer_names to exclude these from the generic
        # `transformers` dict and build a separate `regulators` dict instead —
        # exporting a regulator-transformer under both would double-count it
        # in every topology-derived statistic downstream.
        reg_names = dss.RegControls.AllNames()
        if not reg_names:
            raise RuntimeError(
                "OpenDSS circuit has zero RegControls — no voltage regulators "
                "to export. Either this model was never expected to have any "
                "(OpenDSSEngine currently assumes regulated feeders), or the "
                "model changed and silently dropped its regulators. A silent "
                "zero here would leave nothing for the substation "
                "voltage-control scenario to command."
            )
        for idx, reg_name in enumerate(reg_names):
            dss.RegControls.Name(reg_name)
            xfm_name = dss.RegControls.Transformer()
            reg_id = TransformerID(idx)
            self._regulator_name_to_id[reg_name] = reg_id
            self._regulator_id_to_name[reg_id] = reg_name
            self._regulator_transformer_names.add(xfm_name.lower())
        logger.info(
            "Voltage regulators mapped: %d RegControl(s) -> transformers %s",
            len(reg_names),
            sorted(self._regulator_transformer_names),
        )

    def _read_transformer_winding_electrical(
        self, xfm_name: str
    ) -> tuple[float, float, float, float]:
        """Read (kv_hv, kv_lv, kva_rated, xhl) from the Transformers interface.

        Shared by the plain-transformer and regulator branches of
        get_topology(), which both need the same two-winding readout.
        """
        dss.Transformers.Name(xfm_name)
        dss.Transformers.Wdg(1)
        kv_hv = dss.Transformers.kV()
        kva_rated = dss.Transformers.kVA()
        dss.Transformers.Wdg(2)
        kv_lv = dss.Transformers.kV()
        xhl = dss.Transformers.Xhl()
        return kv_hv, kv_lv, kva_rated, xhl

    def _read_transformer_power_and_loading(
        self, xfm_name: str, kva_rated: float
    ) -> tuple[float, float, float]:
        """Read (p_mw, q_mvar, loading_percent) for one transformer.

        Reuses kva_rated from _read_transformer_winding_electrical rather
        than re-reading it. Activates the element on both the Transformers
        and CktElement interfaces before reading Powers() — the same
        double-activation get_current_state()'s PV-system loop uses, since
        dss.Transformers.Name() alone does not reliably leave the active
        CktElement pointed at this transformer.

        Loading percent is |S| / kVA_rated * 100, matching the line-loading
        convention this engine already reports (max phase current / rated
        current * 100), just expressed in apparent power instead of current
        since a transformer's three-phase nameplate rating is in kVA, not
        amps.
        """
        dss.Transformers.Name(xfm_name)
        dss.Circuit.SetActiveElement(f"Transformer.{xfm_name}")
        powers = dss.CktElement.Powers()
        num_phases = dss.CktElement.NumPhases()
        t1_end = num_phases * 2
        p_mw = sum(powers[i] for i in range(0, t1_end, 2)) / 1000.0
        q_mvar = sum(powers[i] for i in range(1, t1_end, 2)) / 1000.0
        apparent_kva = math.hypot(p_mw * 1000.0, q_mvar * 1000.0)
        loading_percent = (apparent_kva / kva_rated * 100.0) if kva_rated > 0 else 0.0
        return p_mw, q_mvar, loading_percent

    def _iter_plain_transformers(self) -> Iterator[tuple[TransformerID, str, int, int]]:
        """Yield (transformer_id, name, hv_bus_id, lv_bus_id) for every plain
        (non-regulator) transformer in _transformer_adjacency.

        Shared by _get_transformers_topology() and get_current_state()'s
        transformer-state loop so both assign the same TransformerID to the
        same element — the id is the element's index within
        _transformer_adjacency, not a separately maintained counter, so
        topology and state can never disagree about which transformer a
        given TransformerID refers to.
        """
        for idx, (from_bus_id, to_bus_id, elem_name) in enumerate(
            self._transformer_adjacency
        ):
            if not elem_name.lower().startswith("transformer."):
                continue
            xfm_name = elem_name.split(".", 1)[1]
            if xfm_name.lower() in self._regulator_transformer_names:
                continue
            yield TransformerID(idx), xfm_name, from_bus_id, to_bus_id

    def _resolve_feeder_bus(self, bus_id: int) -> int:
        """Return the feeder-connected bus ID for a transformer-secondary bus.

        PV systems in IEEE37 are defined on isolated transformer-secondary buses
        (trafo_pv*, bus IDs 39–44).  For zone assignment the relevant bus is the
        primary/feeder side of that transformer.  This method searches
        _transformer_adjacency for any edge that includes bus_id and returns the
        OTHER endpoint, i.e. the feeder-connected neighbour.

        Returns bus_id unchanged if no transformer adjacency entry is found
        (meaning the bus is already directly feeder-connected).
        """
        for a, b, _name in self._transformer_adjacency:
            if a == bus_id:
                return b
            if b == bus_id:
                return a
        return bus_id

    def solution_hour(self) -> int:
        """The hour the NEXT Solve() will evaluate loadshapes at (0-23).

        Public because it is the only honest answer to "when was this
        measured": `_pinned_hour` is merely the no-anchor fallback, which
        `/state` reported until 2026-10-20 (see web_api.py).

        Derived from the substrate's shared sim clock when one is available
        (SIM_CLOCK_URL set), so PV/load loadshapes evaluate at the same
        simulated instant der_profiles.py (pandapower) uses. With no anchor —
        standalone/UI/demo runs with no simulated time to track — falls back
        to today's fixed self._pinned_hour, logged once so the fallback is
        visible without a log line per solve.
        """
        anchor = get_anchor()
        if anchor is not None:
            return compute_sim_now(anchor).hour
        if not self._logged_no_anchor_fallback:
            logger.info(
                "No SIM_CLOCK_URL: OpenDSS engine has no simulated time to "
                "track, falling back to fixed pinned hour=%d for this run.",
                self._pinned_hour,
            )
            self._logged_no_anchor_fallback = True
        return self._pinned_hour

    def _fuzz_loads(self, load_scale: float = 0.05, der_scale: float = 0.03) -> None:
        """Apply uniform random noise to all loads and PV setpoints.

        Draws from this engine's own RNG (`self._rng`), seeded by the
        `fuzz_seed` constructor argument. Unseeded by default, so each reset
        produces a genuinely distinct operating point and repeated-measures
        statistics across episodes stay meaningful. Two engines given the
        same seed fuzz identically, which is what paired-baseline
        measurement requires.

        Scales are calibrated to match BW33 practice: ±5% loads, ±3% DERs.

        Args:
            load_scale: Fractional noise range for loads (default ±5%).
            der_scale:  Fractional noise range for PV Pmpp (default ±3%).
        """
        load_names = dss.Loads.AllNames()
        for name in load_names:
            dss.Loads.Name(name)
            dss.Loads.kW(
                dss.Loads.kW() * (1 + self._rng.uniform(-load_scale, load_scale))
            )
            dss.Loads.kvar(
                dss.Loads.kvar() * (1 + self._rng.uniform(-load_scale, load_scale))
            )
        logger.debug(
            "Load fuzz applied: ±%.0f%% on %d loads", load_scale * 100, len(load_names)
        )

        pv_names = dss.PVsystems.AllNames()
        for name in pv_names:
            dss.PVsystems.Name(name)
            dss.PVsystems.Pmpp(
                dss.PVsystems.Pmpp() * (1 + self._rng.uniform(-der_scale, der_scale))
            )
        logger.debug(
            "DER fuzz applied: ±%.0f%% on %d PV systems", der_scale * 100, len(pv_names)
        )

    def reset(self) -> None:
        """Reset to clean initial state with stochastic load variation.

        Recompiles the DSS circuit from source (restoring all original values),
        rebuilds ID mappings, fuzzes loads, and runs a power flow.  Each reset
        draws a random hour from [6, 18] (all hours with non-zero PV
        irradiance) from the RNG as the no-anchor fallback value used by
        solution_hour() — unless `pinned_hour` was supplied to __init__, in
        which case that explicit value is used instead. This only takes
        effect when no sim-clock anchor is available — an anchored run
        ignores it and tracks simulated time instead, varying diurnally within
        the run rather than sitting at one fixed hour.

        Both random draws come from this engine's own RNG, so two engines
        constructed with the same `fuzz_seed` reach identical post-reset
        state. Resets are not individually reproducible: the Nth reset of a
        seeded engine matches the Nth reset of another, because they consume
        the same stream in the same order.

        Raises:
            RuntimeError: If the post-reset power flow does not converge.
        """
        # The hour draw is consumed unconditionally, even when
        # `_pinned_hour_override` is set and its result discarded. Skipping
        # the draw would shift every subsequent RNG call (_fuzz_loads()
        # immediately below) by one position, so two engines sharing a
        # `fuzz_seed` but differing only in whether `pinned_hour` was
        # supplied would silently fuzz differently. Keeping the draw
        # unconditional keeps the RNG stream position identical either way.
        drawn_hour = self._rng.randint(6, 18)
        self._pinned_hour = (
            self._pinned_hour_override
            if self._pinned_hour_override is not None
            else drawn_hour
        )
        self._load_circuit()  # dss.Text.Command("Compile [...]") restores originals
        self._build_id_mappings()  # Rebuild after reload (bus/line indices may shift)
        self._close_all_lines()
        self._open_line_ids.clear()
        self._fuzz_loads()
        result = self.run_simulation()
        if not result.converged:
            raise RuntimeError(
                f"OpenDSS reset power flow did not converge: {result.error_message}"
            )
        self._assert_post_reset_voltage_band()
        logger.info(
            "OpenDSS engine reset: no-anchor fallback_hour=%d (only used "
            "without a sim-clock anchor), loads fuzzed, power flow converged",
            self._pinned_hour,
        )

    def _assert_post_reset_voltage_band(self) -> None:
        assert_post_reset_voltage_band()

    # ========================================================================
    # Core Simulation Methods
    # ========================================================================

    def run_simulation(
        self, config: Optional[PowerFlowConfig] = None
    ) -> PowerFlowResult:
        """Run one simulation timestep."""
        if config is None:
            config = PowerFlowConfig()

        try:
            # Pin hour before each Solve() to prevent Daily mode from
            # auto-advancing through the irradiance loadshape (see
            # _load_circuit() for the drift math). The pinned value now
            # tracks the sim clock via solution_hour() instead of being
            # fixed for the whole run, so PV/load loadshapes vary diurnally
            # within a run rather than sitting at one operating point.
            dss.Solution.Hour(self.solution_hour())

            # OpenDSS's own default MaxIterations (15) was silently in force
            # here — config.max_iterations was read by PowerFlowConfig but
            # never applied to the DSS solution, so this engine ignored the
            # same field pandapower_engine.py honours via runpp(max_iteration=...).
            # Confirmed by measurement at IEEE8500 hour=17 (the only
            # non-convergent point in a 24-hour sweep): MostIterationsDone()
            # hit exactly 15 (the unwired default) while ControlIterations()
            # reached only 2 of its own default cap of 10 — MaxIterations is
            # the cap that bound, not MaxControlIterations, so only
            # MaxIterations is wired to config here.
            dss.Solution.MaxIterations(config.max_iterations)
            dss.Solution.Solve()

            # Get results
            converged = dss.Solution.Converged()
            iterations = dss.Solution.Iterations()

            result = PowerFlowResult(
                converged=converged,
                iterations=iterations,
                max_bus_p_mismatch=None,
                max_bus_q_mismatch=None,
                execution_time_ms=0.0,  # Not tracked
                error_message=None if converged else "Solution did not converge",
                config=config,
            )

            self._last_pf_result = result
            return result

        except Exception as e:
            logger.error(f"Error during simulation step: {e}")
            result = PowerFlowResult(
                converged=False,
                iterations=0,
                max_bus_p_mismatch=None,
                max_bus_q_mismatch=None,
                execution_time_ms=0.0,
                error_message=str(e),
                config=config,
            )
            self._last_pf_result = result
            return result

    def get_convergence_status(self) -> bool:
        """Get convergence status of last simulation.

        Re-raises any DSS exception rather than collapsing it into False.
        A DSS API error returning False here would silently look identical to
        a real power-flow divergence — but they mean very different things to
        downstream attack-scenario analytics (real divergence is grid-state
        signal; API error is infra failure). Callers should treat any
        exception from this method as unrecoverable.
        """
        return bool(dss.Solution.Converged())

    # ========================================================================
    # Topology Query Methods
    # ========================================================================

    def _get_transformers_topology(self) -> Dict[TransformerID, TransformerInfo]:
        """Build the plain-transformer dict from _transformer_adjacency metadata.

        Only includes elements whose name starts with "Transformer." —
        reactors and other non-line PDElements are already represented as
        synthetic lines for connectivity in get_topology(). Regulator-
        controlled transformers are excluded here (relabeled into
        _get_regulators_topology() instead) so a regulator never appears in
        both dicts.
        """
        transformers: Dict[TransformerID, TransformerInfo] = {}
        for (
            trafo_id,
            xfm_name,
            from_bus_id,
            to_bus_id,
        ) in self._iter_plain_transformers():
            try:
                kv_hv, kv_lv, kva_rated, xhl = (
                    self._read_transformer_winding_electrical(xfm_name)
                )
                transformers[trafo_id] = TransformerInfo(
                    transformer_id=trafo_id,
                    name=xfm_name,
                    hv_bus=BusID(from_bus_id),
                    lv_bus=BusID(to_bus_id),
                    rated_power_mva=max(kva_rated / 1000.0, 0.001),
                    hv_voltage_kv=max(kv_hv, 0.001),
                    lv_voltage_kv=max(kv_lv, 0.001),
                    reactance_percent=max(xhl, 0.0),
                    resistance_percent=0.0,
                )
            except Exception as exc:
                # Skipping a transformer drops it from the topology — surface at
                # warning level so silent drops don't accumulate unnoticed.
                logger.warning("Skipping transformer %s in topology: %s", xfm_name, exc)
        return transformers

    def get_topology(self) -> NetworkTopology:
        """Get complete network topology."""
        # Get all buses
        buses = {}
        for bus_id, bus_name in self._bus_id_to_name.items():
            buses[bus_id] = self.get_bus_info(bus_id)

        # Get all lines — skip lines that can't be activated (some IEEE 8500
        # line names in AllNames() are stale after the Solve step).
        lines = {}
        for line_id, line_name in self._line_id_to_name.items():
            try:
                lines[line_id] = self.get_line_info(line_id)
            except KeyError:
                logger.debug(
                    "Skipping line %s ('%s') in topology — not found",
                    line_id,
                    line_name,
                )

        # Get all generators (PV systems)
        generators = {}
        for gen_id, pv_name in self._pv_id_to_name.items():
            generators[gen_id] = self.get_generator_info(gen_id)

        # Get all loads
        loads = {}
        for load_id, load_name in self._load_id_to_name.items():
            loads[load_id] = self.get_load_info(load_id)

        # Add transformer-based connections as synthetic line entries.
        # These are numbered from _SYNTHETIC_LINE_ID_BASE (1000) to avoid collision
        # with real line IDs.  The zone partitioner builds adjacency from all entries
        # in the lines dict, so this makes the graph fully connected even when the
        # slack bus is only reachable via a substation transformer (e.g. IEEE37 subxf).
        # Synthetic lines also appear in zone_map as "line-1000" etc. — harmless since
        # those keys are never referenced as Neo4j control targets.
        _SYNTHETIC_LINE_ID_BASE = max(lines.keys(), default=LineID(0)) + 1
        for idx, (from_bus_id, to_bus_id, _elem_name) in enumerate(
            self._transformer_adjacency
        ):
            synth_id = LineID(_SYNTHETIC_LINE_ID_BASE + idx)
            lines[synth_id] = LineInfo(
                line_id=synth_id,
                from_bus=BusID(from_bus_id),
                to_bus=BusID(to_bus_id),
                name=f"[xfm-{idx}]",
                resistance_ohm=0.0,
                reactance_ohm=0.0,
                capacitance_nf=0.0,
                max_current_ka=1.0,
                in_service=True,
            )

        # Get circuit info for metadata
        circuit_name = dss.Circuit.Name()

        transformers = self._get_transformers_topology()
        regulators = self._regulator_interface._get_regulators_topology()

        return NetworkTopology(
            buses=buses,
            lines=lines,
            transformers=transformers,
            regulators=regulators,
            generators=generators,
            loads=loads,
            storage={},  # IEEE 8500 has no storage elements
            name=circuit_name,
            base_mva=1.0,  # OpenDSS doesn't use base MVA concept
            frequency_hz=60.0,
        )

    def get_bus_info(self, bus_id: BusID) -> BusInfo:
        """Get information about a specific bus."""
        if bus_id not in self._bus_id_to_name:
            raise KeyError(f"Bus {bus_id} not found")

        bus_name = self._bus_id_to_name[bus_id]

        try:
            dss.Circuit.SetActiveBus(bus_name)

            # Get base voltage, use 1.0 kV as default if not set
            kv_base = dss.Bus.kVBase()
            if kv_base <= 0:
                # If kVBase is not set, try to infer from actual voltage
                voltages_angles = dss.Bus.puVmagAngle()
                if voltages_angles:
                    voltages_pu = [
                        voltages_angles[i] for i in range(0, len(voltages_angles), 2)
                    ]
                    # Use nominal voltage of 1.0 kV as fallback
                    kv_base = 1.0
                else:
                    kv_base = 1.0

            # Source bus is the slack/reference — required by topology_zone_partitioner BFS
            bus_type = (
                BusType.SLACK
                if bus_name.lower() == self._source_bus_name
                else BusType.PQ
            )

            return BusInfo(
                bus_id=bus_id,
                name=bus_name,
                bus_type=bus_type,
                voltage_nominal_kv=kv_base,
                grid_stix_id=None,
                grid_stix_metadata=None,
            )

        except Exception as e:
            raise KeyError(f"Failed to get bus info: {e}")

    def get_line_info(self, line_id: LineID) -> LineInfo:
        """Get information about a specific line."""
        if line_id not in self._line_id_to_name:
            raise KeyError(f"Line {line_id} not found")

        line_name = self._line_id_to_name[line_id]

        try:
            # Use pre-cached bus endpoints from _build_id_mappings() instead of
            # SetActiveElement, which returns wrong buses on large models (IEEE 8500)
            # due to OpenDSS global state corruption during concurrent iteration.
            cached = self._line_buses.get(int(line_id))
            if cached:
                from_bus = BusID(cached[0])
                to_bus = BusID(cached[1])
            else:
                dss.Circuit.SetActiveElement(f"Line.{line_name}")
                raw_buses = dss.CktElement.BusNames()
                b1 = raw_buses[0].split(".")[0].lower() if len(raw_buses) > 0 else ""
                b2 = raw_buses[1].split(".")[0].lower() if len(raw_buses) > 1 else ""
                from_bus = self._bus_name_to_id.get(b1, BusID(0))
                to_bus = self._bus_name_to_id.get(b2, BusID(0))

            # The cached-bus path above never activates this line, so the
            # active element is whatever was touched last. Activate it before
            # any CktElement read, rating included.
            dss.Lines.Name(line_name)
            max_current_ka = (
                dss.CktElement.NormalAmps() / 1000.0
                if dss.CktElement.NormalAmps() > 0
                else 1.0
            )

            return LineInfo(
                line_id=line_id,
                name=line_name,
                from_bus=from_bus,
                to_bus=to_bus,
                resistance_ohm=0.0,
                reactance_ohm=0.0,
                capacitance_nf=0.0,
                max_current_ka=max_current_ka,
                in_service=not dss.CktElement.IsOpen(1, 0),
            )

        except Exception as e:
            raise KeyError(f"Failed to get line info: {e}")

    def get_generator_info(self, generator_id: GeneratorID) -> GeneratorInfo:
        """Get information about a specific generator (PV system)."""
        if generator_id not in self._pv_id_to_name:
            raise KeyError(f"Generator {generator_id} not found")

        pv_name = self._pv_id_to_name[generator_id]

        try:
            dss.PVsystems.Name(pv_name)

            # Parse bus name.  PV systems in IEEE37 are defined on transformer-
            # secondary buses (trafo_pv*) which are isolated from the line graph.
            # Resolve to the feeder-connected primary bus so zone assignment is
            # based on where the DER actually injects into the distribution feeder.
            raw_bus_name = dss.CktElement.BusNames()[0].split(".")[0]
            raw_bus_id = int(self._bus_name_to_id.get(raw_bus_name, BusID(0)))
            feeder_bus_id = BusID(self._resolve_feeder_bus(raw_bus_id))

            return GeneratorInfo(
                generator_id=generator_id,
                name=pv_name,
                bus=feeder_bus_id,
                der_type=DERType.SOLAR_PV,
                p_max_mw=dss.PVsystems.Pmpp() / 1000.0,
                p_min_mw=0.0,
                q_max_mvar=dss.PVsystems.kVARated() / 1000.0 * 0.5,
                q_min_mvar=-dss.PVsystems.kVARated() / 1000.0 * 0.5,
                voltage_setpoint_pu=None,
                status=(
                    DeviceStatus.ONLINE
                    if dss.CktElement.Enabled()
                    else DeviceStatus.OFFLINE
                ),
                in_service=dss.CktElement.Enabled(),
            )

        except Exception as e:
            raise KeyError(f"Failed to get generator info: {e}")

    def get_load_info(self, load_id: LoadID) -> LoadInfo:
        """Get information about a specific load."""
        if load_id not in self._load_id_to_name:
            raise KeyError(f"Load {load_id} not found")

        load_name = self._load_id_to_name[load_id]

        try:
            dss.Loads.Name(load_name)

            # Parse bus name
            bus_name = dss.CktElement.BusNames()[0].split(".")[0]
            bus_id = self._bus_name_to_id.get(bus_name, BusID(0))

            return LoadInfo(
                load_id=load_id,
                name=load_name,
                bus=bus_id,
                p_mw=dss.Loads.kW() / 1000.0,
                q_mvar=dss.Loads.kvar() / 1000.0,
                in_service=dss.CktElement.Enabled(),
            )

        except Exception as e:
            raise KeyError(f"Failed to get load info: {e}")

    def get_storage_info(self, storage_id: StorageID) -> StorageInfo:
        """Get information about a specific storage unit."""
        raise KeyError("Storage not implemented in OpenDSS engine")

    # ========================================================================
    # State Query Methods
    # ========================================================================

    def get_current_state(self) -> GridState:  # noqa: C901, PLR0915
        """Get current grid state after simulation."""
        timestamp = datetime.now()

        buses, islanded_bus_ids = read_bus_states(self._bus_id_to_name, timestamp)

        # Get line states
        lines = {}
        for line_id, line_name in self._line_id_to_name.items():
            try:
                dss.Circuit.SetActiveElement(f"Line.{line_name}")
                norm_amps = dss.CktElement.NormalAmps()

                powers = dss.CktElement.Powers()
                # Powers are [P1, Q1, P2, Q2, ...] per phase at terminal-1 then terminal-2.
                # Use only terminal-1 (first NumPhases*2 values) for from-bus flows.
                num_phases = dss.CktElement.NumPhases()
                t1_end = num_phases * 2
                p_total = (
                    sum(powers[i] for i in range(0, t1_end, 2)) / 1000.0
                )  # kW → MW
                q_total = sum(powers[i] for i in range(1, t1_end, 2)) / 1000.0

                # Loading = max phase current / NormAmps * 100.
                # CurrentsMagAng() returns [|I|, θ, ...] per phase for terminal-1 then terminal-2.
                # Take the max magnitude across terminal-1 phases.
                currents = dss.CktElement.CurrentsMagAng()
                phase_magnitudes = [currents[i] for i in range(0, t1_end, 2)]
                max_current_a = max(phase_magnitudes) if phase_magnitudes else 0.0
                current_ka = max_current_a / 1000.0
                loading_pct = (
                    (max_current_a / norm_amps * 100.0) if norm_amps > 0 else 0.0
                )

                lines[line_id] = LineState(
                    line_id=line_id,
                    p_from_mw=p_total,
                    q_from_mvar=q_total,
                    p_to_mw=-p_total,  # Approximate
                    q_to_mvar=-q_total,
                    current_ka=current_ka,
                    loading_percent=loading_pct,
                    timestamp=timestamp,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to read state for line %s ('%s'): %s",
                    line_id,
                    line_name,
                    exc,
                )

        # Get transformer states (plain transformers only — regulators report
        # tap/setpoint straight from the engine, not through GridState; see
        # modbus_server.py's regulator_to_hr_index handling).
        transformers = {}
        for (
            trafo_id,
            xfm_name,
            _hv_bus_id,
            _lv_bus_id,
        ) in self._iter_plain_transformers():
            try:
                _, _, kva_rated, _ = self._read_transformer_winding_electrical(xfm_name)
                p_mw, q_mvar, loading_pct = self._read_transformer_power_and_loading(
                    xfm_name, kva_rated
                )
                transformers[trafo_id] = TransformerState(
                    transformer_id=trafo_id,
                    p_mw=p_mw,
                    q_mvar=q_mvar,
                    loading_percent=loading_pct,
                    timestamp=timestamp,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to read state for transformer %s: %s", xfm_name, exc
                )

        # Read the PV's own electrical terminal, never the host bus's first
        # phase. An unreadable inverter measurement invalidates this state.
        generators = {
            gen_id: read_pv_state(gen_id, pv_name, timestamp)
            for gen_id, pv_name in self._pv_id_to_name.items()
        }

        # Get load states
        loads = {}
        for load_id, load_name in self._load_id_to_name.items():
            try:
                dss.Loads.Name(load_name)

                loads[load_id] = LoadState(
                    load_id=load_id,
                    p_mw=dss.Loads.kW() / 1000.0,
                    q_mvar=dss.Loads.kvar() / 1000.0,
                    timestamp=timestamp,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to read state for load %s ('%s'): %s",
                    load_id,
                    load_name,
                    exc,
                )

        # Circuit totals. Deliberately NOT wrapped in a try/except that
        # substitutes zeros, which is what this did until 2026-09-29.
        #
        # An OpenDSS API failure here used to become
        # `total_gen = total_load = total_losses = 0.0` behind a warning --
        # "zero generation, zero load, zero losses". That is a physically
        # plausible reading, indistinguishable from a real all-zero state,
        # written straight into the artifacts every physical-harm figure is
        # computed from. A de-energized feeder and a failed API call produced
        # byte-identical output.
        #
        # This is the defect class that already cost this project two days and
        # three cancelled corpus runs: #1293 (authorized writes discarded with
        # no log) and #1294 (Clear/Compile failures unchecked, episodes running
        # against a silently emptied circuit). Both were invisible for exactly
        # this reason -- a failure that looks like data.
        #
        # Letting it raise changes GridState's error contract: a caller that
        # previously received zeros now sees the exception. That is the
        # intended change. A run that cannot read its own circuit totals has
        # not produced a measurement, and should stop rather than emit one.
        total_power = dss.Circuit.TotalPower()
        losses = dss.Circuit.Losses()

        total_gen = total_power[0] / 1000.0
        total_load = sum(load.p_mw for load in loads.values())
        total_losses = losses[0] / 1000.0 / 1000.0  # Convert from W to MW

        return GridState(
            timestamp=timestamp,
            buses=buses,
            lines=lines,
            transformers=transformers,
            generators=generators,
            loads=loads,
            storage={},
            converged=self.get_convergence_status(),
            iterations=(
                self._last_pf_result.iterations if self._last_pf_result else None
            ),
            total_generation_mw=total_gen,
            total_load_mw=total_load,
            total_losses_mw=total_losses,
            nan_bus_count=len(islanded_bus_ids),
            islanded_buses=islanded_bus_ids,
        )

    # ========================================================================
    # Control Methods
    # ========================================================================

    def set_breaker_status(self, line_id: LineID, closed: bool) -> None:
        """Open or close a line's breaker by switching terminal 1.

        All conductors of terminal 1 are opened or closed; the element itself
        stays enabled.

        Raises:
            KeyError: If `line_id` is unknown.
            RuntimeError: If OpenDSS rejects the command or the terminal state
                does not match the request afterwards.
        """
        if line_id not in self._line_id_to_name:
            raise KeyError(f"Line {line_id} not found")

        line_name = self._line_id_to_name[line_id]

        try:
            # Not Enabled(False): disabling the only element at a leaf bus
            # deletes that bus from OpenDSS, and re-enabling the element does
            # not recreate it, so every later bus read failed for as long as
            # the line stayed closed. A terminal switch keeps the bus in place.
            dss.Text.Command(f"{'Close' if closed else 'Open'} Line.{line_name} term=1")
            dss.Lines.Name(line_name)
            if dss.CktElement.IsOpen(1, 0) == closed:
                raise RuntimeError(f"terminal 1 of Line.{line_name} did not switch")
            logger.info(f"Set breaker {line_name} to {'CLOSED' if closed else 'OPEN'}")
        except Exception as e:
            raise RuntimeError(f"Failed to set breaker status: {e}")
        if closed:
            self._open_line_ids.discard(line_id)
        else:
            self._open_line_ids.add(line_id)

    def open_line_ids(self) -> frozenset[LineID]:
        """Lines whose terminal 1 this engine has opened since the last reset."""
        return frozenset(self._open_line_ids)

    def _close_all_lines(self) -> None:
        """Close terminal 1 of every line so a reset never starts with an open breaker."""
        for line_name in self._line_id_to_name.values():
            dss.Lines.Name(line_name)
            if dss.CktElement.IsOpen(1, 0):
                dss.Text.Command(f"Close Line.{line_name} term=1")

    def set_generator_setpoint(
        self,
        generator_id: GeneratorID,
        p_mw: Optional[PowerMW] = None,
        q_mvar: Optional[PowerMVAr] = None,
    ) -> None:
        """Set generator (PV system) setpoint.

        Only active power is supported. `q_mvar` is accepted for interface
        parity with PandaPowerEngine, which does honour it, and raises here
        rather than being silently discarded — this method ignored it
        outright until 2026-10-02, so the two engines disagreed on a shared
        interface with no error.

        Reactive power is deliberately unimplemented rather than merely
        missing. Three choices have to be made first, each of which bounds
        what a reactive-power attacker could do and therefore belongs with
        that attacker's design rather than in a one-line fix: WattPriority
        vs PFPriority when P and Q together exceed the inverter kVA rating,
        varFollowInverter (whether units supply Q at night), and whether
        inverter-side volt-VAR (OpenDSS InvControl) is active. See
        docs/decisions/paired_baseline_harm_measurement.md in the parent
        repo. Nothing can currently reach this path: the Modbus server
        exposes no reactive-power register and passes `p_mw=` only.

        A setpoint write is an inverter curtailment command, not a resize of
        the array: it sets `%Pmpp`, OpenDSS's upper limit on PVSystem output
        as a percentage of nameplate Pmpp. Writing `Pmpp` itself — the
        previous behaviour — changed the array's rated capacity on every
        dispatch, so repeated setpoints silently and permanently shrank or
        grew the nameplate instead of throttling output against a fixed one.
        `Pmpp` is treated as read-only here; it is set once, from the DSS
        source, and is only ever re-established on `reset()`'s recompile
        (after `_fuzz_loads()` perturbs it by up to ±3%) — nothing in this
        method may touch it again.

        A request above nameplate is clamped to 100% and logged at WARNING
        with both the requested and nameplate kW. This mirrors a real
        inverter, which cannot be commanded to exceed its own nameplate
        rating and simply saturates at it — it is not a software fallback
        masking an error, so it is not raised as one.

        Args:
            generator_id: Engine-local PV system ID.
            p_mw: Active power setpoint in MW, interpreted as an upper limit
                on output (curtailment), not a resize of nameplate capacity.
                No-op when None. Must be >= 0.
            q_mvar: Must be None. Present for interface parity only.

        Raises:
            KeyError: If generator_id is unknown.
            NotImplementedError: If q_mvar is not None.
            ValueError: If p_mw is negative.
            RuntimeError: If the underlying OpenDSS call fails.
        """
        if generator_id not in self._pv_id_to_name:
            raise KeyError(f"Generator {generator_id} not found")

        if q_mvar is not None:
            raise NotImplementedError(
                f"OpenDSSEngine.set_generator_setpoint does not support "
                f"reactive power (got q_mvar={q_mvar} for generator "
                f"{generator_id}). PandaPowerEngine honours q_mvar; this "
                f"engine does not. Implementing it requires choosing "
                f"inverter P/Q priority and night-time var behaviour first "
                f"— see docs/decisions/paired_baseline_harm_measurement.md. "
                f"Pass p_mw only."
            )

        if p_mw is not None and p_mw < 0:
            raise ValueError(
                f"Generator setpoint p_mw must be >= 0, got {p_mw} for "
                f"generator {generator_id}. A PV inverter cannot absorb "
                f"active power."
            )

        pv_name = self._pv_id_to_name[generator_id]

        try:
            dss.PVsystems.Name(pv_name)

            if p_mw is not None:
                nameplate_kw = dss.PVsystems.Pmpp()
                if nameplate_kw <= 0:
                    raise RuntimeError(
                        f"PV system {pv_name} has nameplate Pmpp={nameplate_kw} "
                        "kW; a %Pmpp curtailment percentage cannot be computed "
                        "against a non-positive nameplate. This indicates the "
                        "DSS model itself is misconfigured, not a bad setpoint "
                        "— fix the PVSystem's Pmpp, do not silently substitute "
                        "0%."
                    )
                requested_kw = p_mw * 1000.0
                pct_of_nameplate = requested_kw / nameplate_kw * 100.0
                if pct_of_nameplate > 100.0:
                    logger.warning(
                        "PV system %s setpoint %.3f kW exceeds nameplate "
                        "%.3f kW; clamping to 100%% (inverter saturation, "
                        "not a software fallback).",
                        pv_name,
                        requested_kw,
                        nameplate_kw,
                    )
                    pct_of_nameplate = 100.0
                dss.Text.Command(f"PVSystem.{pv_name}.%Pmpp={pct_of_nameplate}")

            logger.info(f"Set PV system {pv_name} setpoint")
        except Exception as e:
            raise RuntimeError(f"Failed to set generator setpoint: {e}")

    def set_load_demand(
        self,
        load_id: LoadID,
        p_mw: Optional[PowerMW] = None,
        q_mvar: Optional[PowerMVAr] = None,
    ) -> None:
        """Set load power demand."""
        if load_id not in self._load_id_to_name:
            raise KeyError(f"Load {load_id} not found")

        load_name = self._load_id_to_name[load_id]

        try:
            dss.Loads.Name(load_name)

            if p_mw is not None:
                dss.Loads.kW(p_mw * 1000.0)

            if q_mvar is not None:
                dss.Loads.kvar(q_mvar * 1000.0)

            logger.info(f"Set load {load_name} demand")
        except Exception as e:
            raise RuntimeError(f"Failed to set load demand: {e}")

    def set_storage_power(self, storage_id: StorageID, p_mw: PowerMW) -> None:
        """Set storage power setpoint."""
        raise NotImplementedError("Storage not implemented in OpenDSS engine")

    def set_transformer_tap(
        self, transformer_id: TransformerID, tap_position: int
    ) -> None:
        """Force a voltage regulator's tap position via its RegControl.

        See OpenDSSRegulatorInterface.set_transformer_tap for the full
        contract (tap-forcing vs. voltage-setpoint semantics, reversion
        behavior under automatic control).
        """
        self._regulator_interface.set_transformer_tap(transformer_id, tap_position)

    def get_regulator_tap_position(self, regulator_id: TransformerID) -> int:
        """Read the live tap position of a voltage regulator's RegControl.

        See OpenDSSRegulatorInterface.get_regulator_tap_position for the
        full contract.
        """
        return self._regulator_interface.get_regulator_tap_position(regulator_id)

    def set_regulator_voltage_setpoint(
        self, regulator_id: TransformerID, vreg: float
    ) -> None:
        """Command a voltage regulator's ForwardVreg (target voltage).

        See OpenDSSRegulatorInterface.set_regulator_voltage_setpoint for the
        full contract.
        """
        self._regulator_interface.set_regulator_voltage_setpoint(regulator_id, vreg)

    def get_regulator_voltage_setpoint(self, regulator_id: TransformerID) -> float:
        """Read a voltage regulator's live ForwardVreg target.

        See OpenDSSRegulatorInterface.get_regulator_voltage_setpoint for the
        full contract.
        """
        return self._regulator_interface.get_regulator_voltage_setpoint(regulator_id)
