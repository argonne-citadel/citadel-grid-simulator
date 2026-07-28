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
import random
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

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
    BusState,
    GeneratorState,
    GridState,
    LineState,
    LoadState,
    StorageState,
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
from .base import PowerSystemEngine

logger = logging.getLogger(__name__)


class OpenDSSEngine(PowerSystemEngine):
    """
    OpenDSS implementation of PowerSystemEngine.

    This engine uses OpenDSSDirect.py to run OpenDSS simulations directly
    in the Python process, providing full PowerSystemEngine functionality.
    """

    def __init__(self, dss_file_path: str):
        """
        Initialize the OpenDSS engine.

        Args:
            dss_file_path: Path to DSS circuit file to load
        """
        self.dss_file_path = dss_file_path

        # ID mapping: OpenDSS uses string names, we use integer IDs
        self._bus_name_to_id: Dict[str, BusID] = {}
        self._bus_id_to_name: Dict[BusID, str] = {}
        self._line_name_to_id: Dict[str, LineID] = {}
        self._line_id_to_name: Dict[LineID, str] = {}
        self._load_name_to_id: Dict[str, LoadID] = {}
        self._load_id_to_name: Dict[LoadID, str] = {}
        self._pv_name_to_id: Dict[str, GeneratorID] = {}
        self._pv_id_to_name: Dict[GeneratorID, str] = {}

        # Last power flow result
        self._last_pf_result: Optional[PowerFlowResult] = None

        # Simulation hour pinned for this run.  Randomized on reset() so
        # different runs exercise different irradiance operating points.
        # Snapshot mode re-uses this hour on every Solve() — no time drift.
        self._pinned_hour: int = 8

        # Source bus name (slack) — populated by _build_id_mappings()
        self._source_bus_name: str = ""

        # Non-line PDElement connections (transformers, reactors) as (from_id, to_id)
        # pairs.  Populated by _build_id_mappings(); exposed in get_topology() as
        # synthetic line entries so the zone partitioner sees a fully connected graph
        # even when the slack bus is only reachable via a substation transformer.
        self._transformer_adjacency: list[tuple[int, int, str]] = []

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

            # Clear existing circuit
            dss.run_command("Clear")

            # Compile DSS file
            dss.run_command(f"Compile [{self.dss_file_path}]")

            # Daily mode with pinned hour: loadshapes (irradiance, temperature)
            # are evaluated at Solution.Hour(), giving hour-dependent PV output.
            # Snapshot mode (Mode=0) does NOT evaluate daily loadshapes — PVs
            # use their base irrad value regardless of hour, eliminating diurnal
            # diversity.  Instead, we keep Daily mode but pin the hour before
            # each Solve() in run_simulation() to prevent auto-advancement.
            # Hour is randomized on reset() so different runs exercise different
            # diurnal conditions (hour 6-18 = non-zero PV irradiance).
            dss.Solution.Mode(1)  # Daily — evaluates loadshapes at Hour()
            dss.Solution.Number(1)  # 1 step per Solve()
            dss.Solution.StepSize(3600)  # 1 hour per step
            dss.Solution.Hour(self._pinned_hour)

            # Get circuit info
            circuit_name = dss.Circuit.Name()
            num_buses = dss.Circuit.NumBuses()
            num_nodes = dss.Circuit.NumNodes()

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

    def _fuzz_loads(self, load_scale: float = 0.05, der_scale: float = 0.03) -> None:
        """Apply uniform random noise to all loads and PV setpoints.

        Uses an unseeded RNG so each reset produces a genuinely distinct
        operating point, enabling meaningful repeated-measures statistics.
        Scales are calibrated to match BW33 practice: ±5% loads, ±3% DERs.

        Args:
            load_scale: Fractional noise range for loads (default ±5%).
            der_scale:  Fractional noise range for PV Pmpp (default ±3%).
        """
        load_names = dss.Loads.AllNames()
        for name in load_names:
            dss.Loads.Name(name)
            dss.Loads.kW(dss.Loads.kW() * (1 + random.uniform(-load_scale, load_scale)))
            dss.Loads.kvar(
                dss.Loads.kvar() * (1 + random.uniform(-load_scale, load_scale))
            )
        logger.debug(
            "Load fuzz applied: ±%.0f%% on %d loads", load_scale * 100, len(load_names)
        )

        pv_names = dss.PVsystems.AllNames()
        for name in pv_names:
            dss.PVsystems.Name(name)
            dss.PVsystems.Pmpp(
                dss.PVsystems.Pmpp() * (1 + random.uniform(-der_scale, der_scale))
            )
        logger.debug(
            "DER fuzz applied: ±%.0f%% on %d PV systems", der_scale * 100, len(pv_names)
        )

    def reset(self) -> None:
        """Reset to clean initial state with stochastic load variation.

        Recompiles the DSS circuit from source (restoring all original values),
        rebuilds ID mappings, fuzzes loads, and runs a power flow.  Each reset
        picks a random hour from [6, 18] (all hours with non-zero PV irradiance)
        so different runs exercise different diurnal operating conditions.
        The hour is pinned for the duration of the run (Snapshot mode).

        Raises:
            RuntimeError: If the post-reset power flow does not converge.
        """
        self._pinned_hour = random.randint(6, 18)
        self._load_circuit()  # dss.run_command("Compile [...]") restores originals
        self._build_id_mappings()  # Rebuild after reload (bus/line indices may shift)
        self._fuzz_loads()
        result = self.run_simulation()
        if not result.converged:
            raise RuntimeError(
                f"OpenDSS reset power flow did not converge: {result.error_message}"
            )
        logger.info(
            "OpenDSS engine reset: hour=%d, loads fuzzed, power flow converged",
            self._pinned_hour,
        )

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
            # auto-advancing through the irradiance loadshape.  Each run
            # operates at a fixed diurnal operating point (set by reset()).
            dss.Solution.Hour(self._pinned_hour)
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

        # Build transformer dict from _transformer_adjacency metadata.
        # Only include elements whose name starts with "Transformer." —
        # reactors and other non-line PDElements are already represented
        # as synthetic lines for connectivity.
        transformers = {}
        for idx, (from_bus_id, to_bus_id, elem_name) in enumerate(
            self._transformer_adjacency
        ):
            if not elem_name.lower().startswith("transformer."):
                continue
            trafo_id = TransformerID(idx)
            # Read kVA and kV from the active element
            try:
                dss.Circuit.SetActiveElement(elem_name)
                kva = dss.CktElement.NormalAmps()  # kVA for transformers
                # Read winding voltages from Transformers interface
                xfm_name = elem_name.split(".", 1)[1]
                dss.Transformers.Name(xfm_name)
                dss.Transformers.Wdg(1)
                kv_hv = dss.Transformers.kV()
                kva_rated = dss.Transformers.kVA()
                dss.Transformers.Wdg(2)
                kv_lv = dss.Transformers.kV()
                xhl = dss.Transformers.Xhl()
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
                logger.warning(
                    "Skipping transformer %s in topology: %s", elem_name, exc
                )

        return NetworkTopology(
            buses=buses,
            lines=lines,
            transformers=transformers,
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

            return LineInfo(
                line_id=line_id,
                name=line_name,
                from_bus=from_bus,
                to_bus=to_bus,
                resistance_ohm=0.0,
                reactance_ohm=0.0,
                capacitance_nf=0.0,
                max_current_ka=(
                    dss.CktElement.NormalAmps() / 1000.0
                    if dss.CktElement.NormalAmps() > 0
                    else 1.0
                ),
                in_service=dss.CktElement.Enabled(),
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

        # Get bus states.
        # OpenDSS convention: disconnected (islanded) buses have voltage_pu == 0.0
        # exactly, which is the equivalent of pandapower's NaN vm_pu for isolated buses.
        buses = {}
        islanded_bus_ids: list[int] = []
        for bus_id, bus_name in self._bus_id_to_name.items():
            try:
                dss.Circuit.SetActiveBus(bus_name)
                voltages_angles = dss.Bus.puVmagAngle()

                # Extract magnitude and angle (alternating in array)
                voltages_pu = [
                    voltages_angles[i] for i in range(0, len(voltages_angles), 2)
                ]
                angles_deg = [
                    voltages_angles[i] for i in range(1, len(voltages_angles), 2)
                ]

                # Use first phase voltage (if multi-phase)
                voltage_pu = voltages_pu[0] if voltages_pu else 0.0
                angle_deg = angles_deg[0] if angles_deg else 0.0

                # 0.0 pu is the OpenDSS convention for a bus isolated from the reference
                # (breaker open upstream).  Record it for observability parity with the
                # pandapower engine's islanded_buses field (which uses NaN detection).
                if voltage_pu == 0.0:
                    islanded_bus_ids.append(int(bus_id))
                    logger.warning(
                        "Bus %s ('%s') has 0.0 pu voltage — likely islanded "
                        "(breaker open upstream).",
                        bus_id,
                        bus_name,
                    )

                buses[bus_id] = BusState(
                    bus_id=bus_id,
                    voltage_pu=voltage_pu,
                    angle_deg=angle_deg,
                    timestamp=timestamp,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to read state for bus %s ('%s'): %s", bus_id, bus_name, exc
                )

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

        # Get generator states (PV systems)
        generators = {}
        for gen_id, pv_name in self._pv_id_to_name.items():
            try:
                dss.PVsystems.Name(pv_name)
                dss.Circuit.SetActiveElement(f"PVSystem.{pv_name}")
                # CktElement.Powers() returns [P1, Q1, P2, Q2, ...] in kW/kVAR
                # per phase, concatenated across ALL terminals (bus terminal + neutral
                # return terminal).  Summing all even indices cancels to zero because
                # terminal-1 injection (negative, OpenDSS load convention) is exactly
                # offset by the neutral return at terminal-2.
                #
                # Fix: restrict to terminal-1 only (first NumPhases * 2 values).
                # Negate because OpenDSS load convention is injection = negative power;
                # GeneratorState.p_mw uses positive = generating (matches pandapower).
                powers = dss.CktElement.Powers()
                num_phases = dss.CktElement.NumPhases()
                t1_end = num_phases * 2
                p_mw = -sum(powers[i] for i in range(0, t1_end, 2)) / 1000.0
                q_mvar = -sum(powers[i] for i in range(1, t1_end, 2)) / 1000.0
                generators[gen_id] = GeneratorState(
                    generator_id=gen_id,
                    p_mw=p_mw,
                    q_mvar=q_mvar,
                    voltage_pu=None,
                    timestamp=timestamp,
                )
            except Exception as exc:
                logger.warning(
                    "Failed to read state for generator %s: %s", pv_name, exc
                )

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

        # Get circuit totals
        try:
            total_power = dss.Circuit.TotalPower()
            losses = dss.Circuit.Losses()

            total_gen = total_power[0] / 1000.0
            total_load = sum(load.p_mw for load in loads.values())
            total_losses = losses[0] / 1000.0 / 1000.0  # Convert from W to MW

        except Exception as exc:
            logger.warning("Failed to read circuit totals: %s", exc)
            total_gen = 0.0
            total_load = 0.0
            total_losses = 0.0

        return GridState(
            timestamp=timestamp,
            buses=buses,
            lines=lines,
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
        """Set breaker (line enabled) status."""
        if line_id not in self._line_id_to_name:
            raise KeyError(f"Line {line_id} not found")

        line_name = self._line_id_to_name[line_id]

        try:
            dss.Lines.Name(line_name)
            dss.CktElement.Enabled(closed)
            logger.info(f"Set breaker {line_name} to {'CLOSED' if closed else 'OPEN'}")
        except Exception as e:
            raise RuntimeError(f"Failed to set breaker status: {e}")

    def set_generator_setpoint(
        self,
        generator_id: GeneratorID,
        p_mw: Optional[PowerMW] = None,
        q_mvar: Optional[PowerMVAr] = None,
    ) -> None:
        """Set generator (PV system) setpoint."""
        if generator_id not in self._pv_id_to_name:
            raise KeyError(f"Generator {generator_id} not found")

        pv_name = self._pv_id_to_name[generator_id]

        try:
            dss.PVsystems.Name(pv_name)

            if p_mw is not None:
                dss.PVsystems.Pmpp(p_mw * 1000.0)

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
        """Set transformer tap position."""
        raise NotImplementedError("Transformer taps not implemented in OpenDSS engine")
