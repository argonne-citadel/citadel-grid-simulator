"""
PandaPower engine implementation.

This module provides a concrete implementation of the PowerSystemEngine
interface using the PandaPower library.
"""

import logging
import math
import time
from datetime import datetime
from typing import TYPE_CHECKING, Callable, Dict, Optional

import pandapower as pp
import pandas as pd

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
    ShuntID,
    StorageID,
    SwitchID,
    TransformerID,
)
from ..schemas.results import PowerFlowAlgorithm, PowerFlowConfig, PowerFlowResult
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
    ShuntInfo,
    StorageInfo,
    SwitchInfo,
    TransformerInfo,
)
from .base import PowerSystemEngine
from .der_profiles import StochasticDERProfile

if TYPE_CHECKING:
    from ..grid_stix_integration.annotator import GridSTIXAnnotator

logger = logging.getLogger(__name__)

# ID offset to separate pandapower gen-table generators from sgen-table
# generators in the unified GeneratorID space.  sgen IDs are used as-is
# (typically 0..N); gen IDs are offset by this constant.
_GEN_ID_OFFSET: int = 10000


class PandaPowerEngine(PowerSystemEngine):
    """
    PandaPower implementation of PowerSystemEngine.

    This engine wraps a PandaPower network and provides the standard
    PowerSystemEngine interface for grid simulation.
    """

    def __init__(
        self,
        net: pp.pandapowerNet,
        reset_fn: Optional[Callable[[], pp.pandapowerNet]] = None,
        enable_grid_stix: bool = False,
        der_profile: Optional[StochasticDERProfile] = None,
    ):
        """
        Initialize the PandaPower engine.

        Args:
            net: PandaPower network object to wrap.
            reset_fn: Optional callable that returns a fresh fuzzed pandapowerNet.
                Must be provided if reset() is called (e.g., by /reset endpoint).
                Typical value: lambda: BaranWu33Model().reset_network() or
                grid_model.reset_network (a bound method).
            enable_grid_stix: Whether to enable Grid-STIX annotation (optional).
            der_profile: Optional stochastic DER profile for quasi-static time-series
                PV and wind output. When provided, each run_simulation() call applies
                the profile factor before solving and advances simulated time after.
                reset() restores simulated time to the profile's start_dt.
        """
        self.net = net
        self._reset_fn = reset_fn
        self._last_pf_result: Optional[PowerFlowResult] = None
        self._enable_grid_stix = enable_grid_stix
        self._grid_stix_annotator: Optional["GridSTIXAnnotator"] = None
        self._der_profile = der_profile

        # Snapshot sgen nameplate capacities (Pmpp) and initialize setpoint tracking.
        # _sgen_pmpp:    baseline capacity (from max_p_mw column); never changes between resets.
        # _sgen_setpoint: most recently commanded setpoint per sgen; updated by
        #                 set_generator_setpoint() when a profile is active so that
        #                 attack commands are preserved across profile-apply cycles.
        self._sgen_pmpp: Dict[int, float] = {}
        self._sgen_setpoint: Dict[int, float] = {}
        self._snapshot_sgen_baseline()

        # Snapshot base load values for profile modulation.
        # Categorize by name prefix: TX_DataCenter/TX_Smelter → flat,
        # TX_IndustrialPark → industrial, everything else → residential.
        self._load_base_p: Dict[int, float] = {}
        self._load_base_q: Dict[int, float] = {}
        self._load_profile_type: Dict[
            int, str
        ] = {}  # "flat"|"industrial"|"residential"
        self._snapshot_load_baseline()

        # Populate bus coordinates from pandapower bus_geodata (if provided
        # by model).  web_api.py uses _bus_coords for geographic layout
        # instead of NetworkX when >30% of buses have coordinates.
        self._bus_coords: Dict[int, tuple[float, float]] = {}
        if hasattr(net, "bus_geodata") and len(net.bus_geodata) > 0:
            for idx, row in net.bus_geodata.iterrows():
                self._bus_coords[int(idx)] = (float(row["x"]), float(row["y"]))

        # Initialize Grid-STIX if enabled
        if self._enable_grid_stix:
            self._initialize_grid_stix()

    def _initialize_grid_stix(self) -> None:
        """Initialize Grid-STIX annotation for the network."""
        try:
            from ..grid_stix_integration import GridSTIXAnnotator

            self._grid_stix_annotator = GridSTIXAnnotator()

            # Annotate the topology
            topology = self.get_topology()
            self._grid_stix_annotator.annotate_topology(topology)

            print(
                f"Grid-STIX annotation enabled: {len(self._grid_stix_annotator.get_all_stix_objects())} objects created"
            )
        except Exception:
            logger.exception("Failed to initialize Grid-STIX")
            raise

    def export_grid_stix(self, filepath: str, include_telemetry: bool = False) -> None:
        """
        Export Grid-STIX bundle to file.

        Args:
            filepath: Path to output file
            include_telemetry: Whether to include current state as telemetry

        Raises:
            RuntimeError: If Grid-STIX is not enabled
        """
        if not self._enable_grid_stix or not self._grid_stix_annotator:
            raise RuntimeError("Grid-STIX is not enabled for this engine")

        try:
            from pathlib import Path

            from ..grid_stix_integration import STIXExporter, TelemetryConverter

            # Create telemetry converter and exporter
            converter = TelemetryConverter(self._grid_stix_annotator)
            exporter = STIXExporter(self._grid_stix_annotator, converter)

            # Export bundle
            if include_telemetry:
                state = self.get_current_state()
                bundle = exporter.export_full_bundle(states=[state])
            else:
                bundle = exporter.export_topology_only()

            # Write to file
            output_path = Path(filepath)
            exporter.export_to_file(bundle, output_path)

            stats = exporter.get_bundle_stats(bundle)
            print(f"Grid-STIX bundle exported to {filepath}")
            print(f"  Total objects: {stats['total_objects']}")
            print(f"  Object types: {stats['object_types']}")
        except Exception as e:
            print(f"Error exporting Grid-STIX bundle: {e}")
            raise

    def get_grid_stix_annotator(self) -> Optional["GridSTIXAnnotator"]:
        """Get the Grid-STIX annotator if enabled."""
        return self._grid_stix_annotator

    @property
    def grid_stix_enabled(self) -> bool:
        """Check if Grid-STIX is enabled."""
        return self._enable_grid_stix

    def reset(self) -> None:
        """Reset to a fresh fuzzed network and run initial power flow.

        Requires reset_fn to have been provided at construction. Installs the
        new network returned by reset_fn, which is expected to have already run
        a power flow and validated ANSI compliance (BaranWu33Model.reset_network
        satisfies this contract). Raises RuntimeError with a clear message if
        reset_fn was not provided so the failure is immediately actionable.
        """
        if self._reset_fn is None:
            raise RuntimeError(
                "PandaPowerEngine.reset() requires reset_fn to be provided at "
                "construction. Pass reset_fn=grid_model.reset_network when "
                "creating the engine."
            )
        self.net = self._reset_fn()
        # Re-snapshot after reset: _fuzz_loads() applies ±3% to sgen Pmpp so
        # _sgen_pmpp must reflect the freshly fuzzed values, not the old baseline.
        self._snapshot_sgen_baseline()
        self._snapshot_load_baseline()
        if self._der_profile is not None:
            self._der_profile.reset()
        logger.info("PandaPower engine reset: new network installed via reset_fn")

    # ========================================================================
    # Core Power Flow Methods
    # ========================================================================

    def run_simulation(
        self, config: Optional[PowerFlowConfig] = None
    ) -> PowerFlowResult:
        """Run power flow calculation using PandaPower."""
        if config is None:
            config = PowerFlowConfig()

        start_time = time.time()

        # Apply stochastic DER profile before solving so PV/wind output reflects
        # the current simulated hour. Must run before pp.runpp(), not after.
        if self._der_profile is not None:
            self._apply_der_profile()
            self._apply_load_profiles()

        try:
            # Map algorithm to PandaPower algorithm string
            algorithm_map = {
                PowerFlowAlgorithm.NEWTON_RAPHSON: "nr",
                PowerFlowAlgorithm.GAUSS_SEIDEL: "gs",
                PowerFlowAlgorithm.FAST_DECOUPLED: "fdbx",
                PowerFlowAlgorithm.DC: "dc",
            }
            pp_algorithm = algorithm_map.get(config.algorithm, "nr")

            # Try primary algorithm first
            try:
                pp.runpp(
                    self.net,
                    algorithm=pp_algorithm,
                    max_iteration=config.max_iterations,
                    tolerance_mva=config.tolerance,
                    enforce_q_lims=config.enforce_q_limits,
                    distributed_slack=config.distributed_slack,
                    init_vm_pu=config.init_vm_pu,
                    init_va_degree=config.init_va_degree,
                    numba=True,  # Use numba acceleration
                )
            except Exception as primary_error:
                # If primary algorithm fails, try with relaxed settings
                try:
                    pp.runpp(
                        self.net,
                        algorithm="nr",  # Newton-Raphson is most robust
                        max_iteration=100,  # More iterations
                        tolerance_mva=1e-4,  # Relaxed tolerance
                        enforce_q_lims=False,  # Don't enforce Q limits
                        distributed_slack=True,  # Distribute slack
                        init="dc",  # Initialize with DC power flow
                        numba=True,
                    )
                except Exception as secondary_error:
                    # Raise rather than fall back to DC power flow.
                    # DC power flow on an islanded radial network returns
                    # non-physical angles (e.g. 143°) that corrupt grid
                    # impact metrics. A topology infeasibility should be
                    # reported as such, not silently masked.
                    raise RuntimeError(
                        "Power flow failed to converge after breaker operation. "
                        f"Primary error: {primary_error}. "
                        f"Secondary error: {secondary_error}. "
                        "This likely indicates a topologically islanded section. "
                        "Check line in_service status."
                    ) from secondary_error

            execution_time_ms = (time.time() - start_time) * 1000

            # Get convergence info
            converged = bool(self.net.converged)
            iterations = (
                int(self.net._ppc["iterations"]) if hasattr(self.net, "_ppc") else 0
            )

            # Calculate mismatches if available
            max_p_mismatch = None
            max_q_mismatch = None
            if hasattr(self.net, "_ppc") and "bus" in self.net._ppc:
                # PandaPower stores mismatches in internal ppc structure
                # This is a simplified approach - actual mismatch calculation
                # would require more detailed access to internal state
                pass

            result = PowerFlowResult(
                converged=converged,
                iterations=iterations,
                max_bus_p_mismatch=max_p_mismatch,
                max_bus_q_mismatch=max_q_mismatch,
                execution_time_ms=execution_time_ms,
                error_message=None if converged else "Power flow did not converge",
                config=config,
            )

        except Exception as e:
            # Engine boundary: log full exception for diagnosis but return a
            # PowerFlowResult(converged=False, ...) so the caller's per-step
            # convergence-tracking loop (and downstream telemetry) sees a clean
            # failure signal rather than crashing the sim mid-step.
            logger.exception("Pandapower power flow raised exception")
            execution_time_ms = (time.time() - start_time) * 1000
            result = PowerFlowResult(
                converged=False,
                iterations=0,
                max_bus_p_mismatch=None,
                max_bus_q_mismatch=None,
                execution_time_ms=execution_time_ms,
                error_message=str(e),
                config=config,
            )

        self._last_pf_result = result

        # Advance simulated time after each solve, regardless of convergence,
        # so the clock tracks simulation steps even during islanding events.
        if self._der_profile is not None:
            self._der_profile.advance()

        return result

    def get_convergence_status(self) -> bool:
        """Get convergence status of last power flow."""
        return bool(self.net.converged) if hasattr(self.net, "converged") else False

    # ========================================================================
    # Topology Query Methods
    # ========================================================================

    def get_topology(self) -> NetworkTopology:
        """Get complete network topology."""
        # Build topology from PandaPower network
        buses = {BusID(int(idx)): self._bus_to_info(idx) for idx in self.net.bus.index}

        lines = {
            LineID(int(idx)): self._line_to_info(idx) for idx in self.net.line.index
        }

        # Transformers
        transformers = {}
        if hasattr(self.net, "trafo") and len(self.net.trafo) > 0:
            transformers = {
                TransformerID(int(idx)): self._transformer_to_info(idx)
                for idx in self.net.trafo.index
            }

        # PandaPower uses 'sgen' for static generators (DERs)
        generators = {}
        if hasattr(self.net, "sgen") and len(self.net.sgen) > 0:
            generators = {
                GeneratorID(int(idx)): self._sgen_to_info(idx)
                for idx in self.net.sgen.index
            }

        # PandaPower 'gen' table holds conventional generators (thermal, gas,
        # SMR, microreactors).  IDs are offset by _GEN_ID_OFFSET to avoid
        # collision with sgen IDs in the unified generator dict.
        if hasattr(self.net, "gen") and len(self.net.gen) > 0:
            for idx in self.net.gen.index:
                gid = GeneratorID(int(idx) + _GEN_ID_OFFSET)
                generators[gid] = self._gen_to_info(idx)

        loads = {
            LoadID(int(idx)): self._load_to_info(idx) for idx in self.net.load.index
        }

        storage = {}
        if hasattr(self.net, "storage") and len(self.net.storage) > 0:
            storage = {
                StorageID(int(idx)): self._storage_to_info(idx)
                for idx in self.net.storage.index
            }

        # Shunt capacitors / reactors
        shunts = {}
        if hasattr(self.net, "shunt") and len(self.net.shunt) > 0:
            shunts = {
                ShuntID(int(idx)): self._shunt_to_info(idx)
                for idx in self.net.shunt.index
            }

        # Switches (circuit breakers, disconnects)
        switches = {}
        if hasattr(self.net, "switch") and len(self.net.switch) > 0:
            switches = {
                SwitchID(int(idx)): self._switch_to_info(idx)
                for idx in self.net.switch.index
            }

        return NetworkTopology(
            buses=buses,
            lines=lines,
            transformers=transformers,
            generators=generators,
            loads=loads,
            storage=storage,
            shunts=shunts,
            switches=switches,
            name=(
                self.net.name
                if (hasattr(self.net, "name") and self.net.name)
                else "Network"
            ),
            base_mva=float(self.net.sn_mva),
            frequency_hz=float(self.net.f_hz) if hasattr(self.net, "f_hz") else 60.0,
        )

    def get_bus_info(self, bus_id: BusID) -> BusInfo:
        """Get information about a specific bus."""
        if bus_id not in self.net.bus.index:
            raise KeyError(f"Bus {bus_id} not found")
        return self._bus_to_info(bus_id)

    def get_line_info(self, line_id: LineID) -> LineInfo:
        """Get information about a specific line."""
        if line_id not in self.net.line.index:
            raise KeyError(f"Line {line_id} not found")
        return self._line_to_info(line_id)

    def get_generator_info(self, generator_id: GeneratorID) -> GeneratorInfo:
        """Get information about a specific generator."""
        raw_id = int(generator_id)
        if raw_id >= _GEN_ID_OFFSET:
            # Conventional generator (gen table)
            gen_idx = raw_id - _GEN_ID_OFFSET
            if not hasattr(self.net, "gen") or gen_idx not in self.net.gen.index:
                raise KeyError(f"Generator {generator_id} not found")
            return self._gen_to_info(gen_idx)
        # DER (sgen table)
        if not hasattr(self.net, "sgen") or raw_id not in self.net.sgen.index:
            raise KeyError(f"Generator {generator_id} not found")
        return self._sgen_to_info(raw_id)

    def get_load_info(self, load_id: LoadID) -> LoadInfo:
        """Get information about a specific load."""
        if load_id not in self.net.load.index:
            raise KeyError(f"Load {load_id} not found")
        return self._load_to_info(load_id)

    def get_storage_info(self, storage_id: StorageID) -> StorageInfo:
        """Get information about a specific storage unit."""
        if not hasattr(self.net, "storage") or storage_id not in self.net.storage.index:
            raise KeyError(f"Storage {storage_id} not found")
        return self._storage_to_info(storage_id)

    # ========================================================================
    # State Query Methods
    # ========================================================================

    def get_current_state(self) -> GridState:  # noqa: C901, PLR0915
        """Get current grid state after power flow."""
        timestamp = datetime.now()

        nan_count: list[int] = [0]  # mutable container for nonlocal mutation in closure
        islanded_bus_ids: list[
            int
        ] = []  # bus IDs with NaN vm_pu (disconnected from reference)

        def _safe_float(value: float, default: float = 0.0, context: str = "") -> float:
            """Convert to float, replacing NaN/inf with default.

            Each substitution increments nan_count[0] and logs a warning with
            context.  Callers must pass context (e.g. "bus-5 vm_pu") so that
            the log entry pinpoints which result is invalid.
            Non-zero nan_count after get_current_state() means partial
            non-convergence: some buses have invalid results even though
            last_step_converged may be True.
            """
            f = float(value)
            if pd.isna(f) or f == float("inf") or f == float("-inf"):
                nan_count[0] += 1
                import logging as _logging

                _logging.getLogger(__name__).warning(
                    "NaN/inf in power-flow result%s; substituting default=%s. "
                    "This indicates partial non-convergence for this bus/line.",
                    f" ({context})" if context else "",
                    default,
                )
                return default
            return f

        # Get bus states
        buses = {}
        if hasattr(self.net, "res_bus"):
            for bus_id in self.net.bus.index:
                if bus_id in self.net.res_bus.index:
                    raw_vm = self.net.res_bus.at[bus_id, "vm_pu"]
                    if (
                        pd.isna(raw_vm)
                        or raw_vm == float("inf")
                        or raw_vm == float("-inf")
                    ):
                        islanded_bus_ids.append(int(bus_id))
                    buses[BusID(int(bus_id))] = BusState(
                        bus_id=BusID(int(bus_id)),
                        voltage_pu=_safe_float(
                            raw_vm,
                            1.0,
                            f"bus-{bus_id} vm_pu",
                        ),
                        angle_deg=_safe_float(
                            self.net.res_bus.at[bus_id, "va_degree"],
                            0.0,
                            f"bus-{bus_id} va_degree",
                        ),
                        timestamp=timestamp,
                    )

        # Get line states
        lines = {}
        if hasattr(self.net, "res_line"):
            for line_id in self.net.line.index:
                if line_id in self.net.res_line.index:
                    lines[LineID(int(line_id))] = LineState(
                        line_id=LineID(int(line_id)),
                        p_from_mw=_safe_float(
                            self.net.res_line.at[line_id, "p_from_mw"]
                        ),
                        q_from_mvar=_safe_float(
                            self.net.res_line.at[line_id, "q_from_mvar"]
                        ),
                        p_to_mw=_safe_float(self.net.res_line.at[line_id, "p_to_mw"]),
                        q_to_mvar=_safe_float(
                            self.net.res_line.at[line_id, "q_to_mvar"]
                        ),
                        current_ka=_safe_float(self.net.res_line.at[line_id, "i_ka"]),
                        loading_percent=_safe_float(
                            self.net.res_line.at[line_id, "loading_percent"]
                        ),
                        timestamp=timestamp,
                    )

        # Get generator states — sgen (DERs)
        generators = {}
        if hasattr(self.net, "res_sgen"):
            for gen_id in self.net.sgen.index:
                if gen_id in self.net.res_sgen.index:
                    generators[GeneratorID(int(gen_id))] = GeneratorState(
                        generator_id=GeneratorID(int(gen_id)),
                        p_mw=_safe_float(self.net.res_sgen.at[gen_id, "p_mw"]),
                        q_mvar=_safe_float(self.net.res_sgen.at[gen_id, "q_mvar"]),
                        voltage_pu=None,
                        timestamp=timestamp,
                    )

        # Get generator states — gen (conventional: thermal, gas, SMR, reactors)
        if hasattr(self.net, "res_gen"):
            for gen_id in self.net.gen.index:
                if gen_id in self.net.res_gen.index:
                    gid = GeneratorID(int(gen_id) + _GEN_ID_OFFSET)
                    generators[gid] = GeneratorState(
                        generator_id=gid,
                        p_mw=_safe_float(self.net.res_gen.at[gen_id, "p_mw"]),
                        q_mvar=_safe_float(self.net.res_gen.at[gen_id, "q_mvar"]),
                        voltage_pu=None,
                        timestamp=timestamp,
                    )

        # Get load states
        loads = {}
        if hasattr(self.net, "res_load"):
            for load_id in self.net.load.index:
                if load_id in self.net.res_load.index:
                    loads[LoadID(int(load_id))] = LoadState(
                        load_id=LoadID(int(load_id)),
                        p_mw=_safe_float(self.net.res_load.at[load_id, "p_mw"]),
                        q_mvar=_safe_float(self.net.res_load.at[load_id, "q_mvar"]),
                        timestamp=timestamp,
                    )

        # Get storage states
        storage = {}
        if hasattr(self.net, "res_storage"):
            for stor_id in self.net.storage.index:
                if stor_id in self.net.res_storage.index:
                    # Get SOC - may be NaN if not initialized
                    soc_percent = (
                        float(self.net.storage.at[stor_id, "soc_percent"])
                        if "soc_percent" in self.net.storage.columns
                        else 50.0
                    )

                    # Handle NaN values - use default 50%
                    import math

                    if math.isnan(soc_percent):
                        soc_percent = 50.0

                    max_e_mwh = float(self.net.storage.at[stor_id, "max_e_mwh"])
                    storage[StorageID(int(stor_id))] = StorageState(
                        storage_id=StorageID(int(stor_id)),
                        p_mw=float(self.net.res_storage.at[stor_id, "p_mw"]),
                        soc_mwh=soc_percent * max_e_mwh / 100.0,
                        soc_percent=soc_percent,
                        timestamp=timestamp,
                    )

        # Calculate system totals
        total_gen = sum(g.p_mw for g in generators.values())

        # External grid: positive p_mw = supplying power to network (import),
        # negative = absorbing excess local generation (export).
        ext_grid_p = 0.0
        if hasattr(self.net, "res_ext_grid"):
            for ext_grid_id in self.net.ext_grid.index:
                if ext_grid_id in self.net.res_ext_grid.index:
                    ext_grid_p += float(self.net.res_ext_grid.at[ext_grid_id, "p_mw"])
                    total_gen += float(self.net.res_ext_grid.at[ext_grid_id, "p_mw"])

        total_load = sum(load.p_mw for load in loads.values())
        total_losses = total_gen - total_load  # Simplified

        return GridState(
            timestamp=timestamp,
            buses=buses,
            lines=lines,
            generators=generators,
            loads=loads,
            storage=storage,
            converged=self.get_convergence_status(),
            iterations=(
                self._last_pf_result.iterations if self._last_pf_result else None
            ),
            total_generation_mw=total_gen,
            total_load_mw=total_load,
            total_losses_mw=total_losses,
            ext_grid_import_mw=ext_grid_p,
            nan_bus_count=nan_count[0],
            islanded_buses=islanded_bus_ids,
        )
        # Item 21 (#1130) note: nan_count is tracked above and returned in
        # nan_bus_count. Callers that receive this state should check
        # state.nan_bus_count > 0 and treat it as a degraded result.
        # A threshold-raise here would be unreachable after the return above;
        # the MTU's poll_simulator() already raises via the poll_failed path
        # (item 25) when the grid state cannot be retrieved at all. Individual
        # NaN counts are logged with context by _safe_float() above.
        # If persistent NaN storms are observed, check pandapower convergence.

    # ========================================================================
    # Control Methods
    # ========================================================================

    def set_breaker_status(self, line_id: LineID, closed: bool) -> None:
        """Set breaker status (line in_service)."""
        if line_id not in self.net.line.index:
            raise KeyError(f"Line {line_id} not found")
        self.net.line.at[line_id, "in_service"] = bool(closed)

    def open_line_ids(self) -> frozenset[LineID]:
        """Lines currently out of service."""
        out_of_service = self.net.line.index[~self.net.line["in_service"].astype(bool)]
        return frozenset(LineID(int(idx)) for idx in out_of_service)

    def set_reactor_gen_output(
        self,
        gen_id: int,
        p_mw: PowerMW,
        q_mvar: Optional[PowerMVAr] = None,
    ) -> None:
        """Set gen table entry output (for reactors, not sgens).

        Reactors use pandapower gen entries (PV bus type). This method
        writes directly to net.gen without DER profile interaction.

        Args:
            gen_id: pandapower gen table index.
            p_mw: Active power output in MW.
            q_mvar: Reactive power output in MVAr (optional).

        Raises:
            KeyError: If gen_id is not in the gen table.
            ValueError: If p_mw exceeds max_p_mw by more than 10%.
        """
        if gen_id not in self.net.gen.index:
            raise KeyError(f"Gen {gen_id} not found in gen table")

        max_p = float(self.net.gen.at[gen_id, "max_p_mw"])
        if max_p > 0 and p_mw > max_p * 1.1:
            raise ValueError(
                f"Gen output {p_mw} MW exceeds max {max_p} MW for gen {gen_id}"
            )

        self.net.gen.at[gen_id, "p_mw"] = float(p_mw)
        if q_mvar is not None:
            self.net.gen.at[gen_id, "q_mvar"] = float(q_mvar)

        logger.debug("Gen[%d] output set to %.3f MW", gen_id, p_mw)

    def set_generator_setpoint(
        self,
        generator_id: GeneratorID,
        p_mw: Optional[PowerMW] = None,
        q_mvar: Optional[PowerMVAr] = None,
    ) -> None:
        """Set generator power setpoint."""
        if not hasattr(self.net, "sgen") or generator_id not in self.net.sgen.index:
            raise KeyError(f"Generator {generator_id} not found")

        if p_mw is not None:
            # Use max_p_mw column for capacity limit (set at DER creation time).
            # Fallback to current p_mw only when max_p_mw is not set or is zero,
            # which avoids a bug where zeroing a DER makes future writes impossible.
            raw_max = (
                self.net.sgen.at[generator_id, "max_p_mw"]
                if "max_p_mw" in self.net.sgen.columns
                else float("nan")
            )
            if math.isnan(raw_max) or raw_max <= 0:
                raw_max = float(self.net.sgen.at[generator_id, "p_mw"])
            max_p = max(abs(raw_max), 0.001)  # at least 1 W to avoid zero-limit trap
            if abs(p_mw) > max_p * 2.0:  # Allow 2x overload
                raise ValueError(
                    f"Power setpoint {p_mw} MW exceeds generator capacity {max_p} MW"
                )
            if self._der_profile is not None:
                # With an active profile, track the commanded setpoint separately.
                # _apply_der_profile() will write min(setpoint, Pmpp * factor) to
                # net.sgen before each solve, so the profile cap is always applied.
                # This preserves attack commands (e.g. setpoint=0) across steps.
                self._sgen_setpoint[int(generator_id)] = float(p_mw)
            else:
                self.net.sgen.at[generator_id, "p_mw"] = float(p_mw)

        if q_mvar is not None:
            if "q_mvar" in self.net.sgen.columns:
                self.net.sgen.at[generator_id, "q_mvar"] = q_mvar

    def set_load_demand(
        self,
        load_id: LoadID,
        p_mw: Optional[PowerMW] = None,
        q_mvar: Optional[PowerMVAr] = None,
    ) -> None:
        """Set load power demand."""
        if load_id not in self.net.load.index:
            raise KeyError(f"Load {load_id} not found")

        if p_mw is not None:
            self.net.load.at[load_id, "p_mw"] = p_mw

        if q_mvar is not None:
            self.net.load.at[load_id, "q_mvar"] = q_mvar

    def set_storage_power(self, storage_id: StorageID, p_mw: PowerMW) -> None:
        """Set storage power setpoint.

        Sign convention (caller-facing): positive = discharge (inject to grid),
        negative = charge (absorb from grid). Negated before writing to pandapower
        because pandapower uses load convention: positive = consuming = charging.
        """
        if not hasattr(self.net, "storage") or storage_id not in self.net.storage.index:
            raise KeyError(f"Storage {storage_id} not found")

        # Check limits
        max_p = float(self.net.storage.at[storage_id, "max_e_mwh"]) / 4.0  # C/4 rate
        if abs(p_mw) > max_p:
            raise ValueError(
                f"Power setpoint {p_mw} MW exceeds storage capacity {max_p} MW"
            )

        # Negate before writing: pandapower load convention (positive = consuming =
        # charging) is opposite to our caller-facing convention (positive = discharge).
        # External API positive → pandapower negative (injection = discharging). ✓
        self.net.storage.at[storage_id, "p_mw"] = -p_mw

    def set_bus_reactive_injection(self, bus_id: int, q_mvar: float) -> None:
        """Apply reactive power injection at a bus to simulate voltage manipulation.

        Used for Scenario B (insider voltage setpoint attacks). Inserts or updates
        a shunt element at the target bus, which the next power flow incorporates.
        Out-of-range injections cause voltage violations detectable by protection.

        Args:
            bus_id: Pandapower bus index
            q_mvar: Reactive power injection in Mvar (positive = capacitive/voltage rise)
        """
        import pandapower as pp

        if bus_id not in self.net.bus.index:
            raise KeyError(f"Bus {bus_id} not found")

        # Find or create a shunt element at this bus
        existing = (
            self.net.shunt[self.net.shunt.bus == bus_id]
            if hasattr(self.net, "shunt") and len(self.net.shunt) > 0
            else None
        )
        if existing is not None and len(existing) > 0:
            shunt_id = existing.index[0]
            self.net.shunt.at[shunt_id, "q_mvar"] = q_mvar
        else:
            pp.create_shunt(
                self.net, bus=bus_id, q_mvar=q_mvar, p_mw=0.0, name=f"vset-bus-{bus_id}"
            )

    def update_storage_soc(self, timestep_seconds: float = 1.0) -> None:
        """
        Update state of charge for all storage units based on power flow.

        This should be called after each simulation step to integrate power over time.

        Args:
            timestep_seconds: Simulation timestep in seconds (default: 1.0)
        """
        if not hasattr(self.net, "storage") or len(self.net.storage) == 0:
            return

        if not hasattr(self.net, "res_storage"):
            return  # No results available yet

        import math

        # Ensure soc_percent column exists
        if "soc_percent" not in self.net.storage.columns:
            self.net.storage["soc_percent"] = 50.0  # Initialize to 50%

        timestep_hours = timestep_seconds / 3600.0  # Convert to hours

        for storage_id in self.net.storage.index:
            if storage_id not in self.net.res_storage.index:
                continue

            # Get current SOC
            current_soc = float(self.net.storage.at[storage_id, "soc_percent"])
            if math.isnan(current_soc):
                current_soc = 50.0  # Default if NaN

            # Get actual power from pandapower result.
            # pandapower res_storage.p_mw sign: positive = consuming (charging),
            # negative = injecting (discharging).
            # set_storage_power() already negates (external discharge → pandapower
            # negative), so pandapower's sign directly encodes our energy flow:
            #   p_mw_pandapower > 0 → charging → SOC increases
            #   p_mw_pandapower < 0 → discharging → SOC decreases
            # Do NOT negate here — doing so would double-invert the sign.
            p_mw_pandapower = float(self.net.res_storage.at[storage_id, "p_mw"])

            # Get capacity
            max_e_mwh = float(self.net.storage.at[storage_id, "max_e_mwh"])

            # Energy change: positive = charging = SOC increases.
            energy_change_mwh = p_mw_pandapower * timestep_hours

            # Apply efficiency (one-way, applied on the energy-receiving side)
            efficiency = 0.95
            if energy_change_mwh > 0:  # Charging: less stored than consumed
                energy_change_mwh *= efficiency
            else:  # Discharging: more consumed from battery than delivered to grid
                energy_change_mwh /= efficiency

            # Calculate SOC change (%)
            soc_change = (energy_change_mwh / max_e_mwh) * 100.0

            # Update SOC with limits
            new_soc = current_soc + soc_change
            new_soc = max(0.0, min(100.0, new_soc))  # Clamp to [0, 100]

            # Store updated SOC
            self.net.storage.at[storage_id, "soc_percent"] = new_soc

    def set_transformer_tap(
        self, transformer_id: TransformerID, tap_position: int
    ) -> None:
        """Set transformer tap position."""
        # PandaPower has both 'trafo' and 'trafo3w' (3-winding transformers)
        if hasattr(self.net, "trafo") and transformer_id in self.net.trafo.index:
            # Check if tap changer exists
            if "tap_pos" not in self.net.trafo.columns:
                raise ValueError(
                    f"Transformer {transformer_id} does not have tap changer"
                )

            # Get tap limits
            tap_min = (
                int(self.net.trafo.at[transformer_id, "tap_min"])
                if "tap_min" in self.net.trafo.columns
                else -16
            )
            tap_max = (
                int(self.net.trafo.at[transformer_id, "tap_max"])
                if "tap_max" in self.net.trafo.columns
                else 16
            )

            # Validate tap position
            if tap_position < tap_min or tap_position > tap_max:
                raise ValueError(
                    f"Tap position {tap_position} out of range [{tap_min}, {tap_max}]"
                )

            self.net.trafo.at[transformer_id, "tap_pos"] = tap_position

        elif hasattr(self.net, "trafo3w") and transformer_id in self.net.trafo3w.index:
            # 3-winding transformer
            if "tap_pos" not in self.net.trafo3w.columns:
                raise ValueError(
                    f"3-winding transformer {transformer_id} does not have tap changer"
                )

            tap_min = (
                int(self.net.trafo3w.at[transformer_id, "tap_min"])
                if "tap_min" in self.net.trafo3w.columns
                else -16
            )
            tap_max = (
                int(self.net.trafo3w.at[transformer_id, "tap_max"])
                if "tap_max" in self.net.trafo3w.columns
                else 16
            )

            if tap_position < tap_min or tap_position > tap_max:
                raise ValueError(
                    f"Tap position {tap_position} out of range [{tap_min}, {tap_max}]"
                )

            self.net.trafo3w.at[transformer_id, "tap_pos"] = tap_position
        else:
            raise KeyError(f"Transformer {transformer_id} not found")

    # ========================================================================
    # DER Profile Methods
    # ========================================================================

    def _snapshot_sgen_baseline(self) -> None:
        """Snapshot sgen nameplate capacities (Pmpp) and initialise setpoints.

        Reads max_p_mw column (set explicitly at DER creation time in _add_ders())
        as the Pmpp baseline. Falls back to current p_mw only when max_p_mw is
        unavailable. Called at __init__ and after each reset() to pick up the
        freshly fuzzed Pmpp values that _fuzz_loads() applies at reset time.
        """
        self._sgen_pmpp = {}
        self._sgen_setpoint = {}
        if not (hasattr(self.net, "sgen") and len(self.net.sgen) > 0):
            return
        for idx in self.net.sgen.index:
            raw_max = (
                self.net.sgen.at[idx, "max_p_mw"]
                if "max_p_mw" in self.net.sgen.columns
                else float("nan")
            )
            if math.isnan(raw_max) or raw_max <= 0:
                raw_max = float(self.net.sgen.at[idx, "p_mw"])
            pmpp = max(abs(raw_max), 0.001)
            self._sgen_pmpp[int(idx)] = pmpp
            self._sgen_setpoint[int(idx)] = pmpp  # default: run at full capacity

    def _apply_der_profile(self) -> None:
        """Apply stochastic DER profile factors to net.sgen before each power flow.

        Computes actual output as min(commanded_setpoint, Pmpp × resource_factor).
        This correctly models:
          • Normal operation: output = Pmpp × irradiance/wind factor (< Pmpp)
          • Attack (setpoint = 0): min(0, available) = 0 — attack effect preserved
          • Attack (partial curtailment): capped by available resource if needed

        DER type is determined from net.sgen['type']: 'WP' or 'Wind' → wind factor,
        everything else → solar factor.
        """
        if self._der_profile is None:
            return
        if not (hasattr(self.net, "sgen") and len(self.net.sgen) > 0):
            return

        solar_f = self._der_profile.solar_factor()
        wind_f = self._der_profile.wind_factor()

        for idx in self.net.sgen.index:
            pp_type = str(
                self.net.sgen.at[idx, "type"]
                if "type" in self.net.sgen.columns
                else "PV"
            )
            is_wind = pp_type.upper() in ("WP", "WIND")
            factor = wind_f if is_wind else solar_f

            pmpp = self._sgen_pmpp.get(int(idx), 0.001)
            commanded = self._sgen_setpoint.get(int(idx), pmpp)
            available = pmpp * factor
            actual = min(commanded, available)
            self.net.sgen.at[idx, "p_mw"] = actual

            logger.debug(
                "DER profile sgen-%d (%s): factor=%.3f commanded=%.3fMW "
                "available=%.3fMW actual=%.3fMW [%s]",
                idx,
                "wind" if is_wind else "solar",
                factor,
                commanded,
                available,
                actual,
                self._der_profile.current_dt.strftime("%Y-%m-%d %H:%M"),
            )

    def _snapshot_load_baseline(self) -> None:
        """Snapshot base load P/Q and classify loads by profile type.

        TX-prefixed loads get flat or industrial profiles; all others
        get residential. Called at __init__ and after reset().
        """
        self._load_base_p = {}
        self._load_base_q = {}
        self._load_profile_type = {}
        if not (hasattr(self.net, "load") and len(self.net.load) > 0):
            return
        for idx in self.net.load.index:
            self._load_base_p[int(idx)] = float(self.net.load.at[idx, "p_mw"])
            self._load_base_q[int(idx)] = float(self.net.load.at[idx, "q_mvar"])
            name = (
                str(self.net.load.at[idx, "name"])
                if "name" in self.net.load.columns
                else ""
            )
            if name.startswith("TX_") and ("DataCenter" in name or "Smelter" in name):
                self._load_profile_type[int(idx)] = "flat"
            elif name.startswith("TX_") and "IndustrialPark" in name:
                self._load_profile_type[int(idx)] = "industrial"
            else:
                self._load_profile_type[int(idx)] = "residential"

    def _apply_load_profiles(self) -> None:
        """Apply time-of-day load profiles to all loads before power flow.

        Multiplies base P/Q by the appropriate profile factor for the
        current simulated hour. Only active when a DER profile (which
        tracks simulated time) is available.
        """
        if self._der_profile is None:
            return
        if not self._load_base_p:
            return

        industrial_f = self._der_profile.industrial_load_factor()
        residential_f = self._der_profile.residential_load_factor()
        flat_f = self._der_profile.flat_load_factor()

        factor_map = {
            "industrial": industrial_f,
            "residential": residential_f,
            "flat": flat_f,
        }

        for idx in self.net.load.index:
            idx_int = int(idx)
            profile_type = self._load_profile_type.get(idx_int, "residential")
            factor = factor_map[profile_type]
            self.net.load.at[idx, "p_mw"] = self._load_base_p[idx_int] * factor
            self.net.load.at[idx, "q_mvar"] = self._load_base_q[idx_int] * factor

    # ========================================================================
    # Helper Methods for Data Conversion
    # ========================================================================

    def _bus_to_info(self, bus_id: int) -> BusInfo:
        """Convert PandaPower bus to BusInfo."""
        import math

        bus_type_map = {
            "b": BusType.PQ,
            "n": BusType.PQ,
            "m": BusType.PQ,
        }
        # PandaPower doesn't explicitly store bus type in bus table
        # We infer it from connected elements
        bus_type = BusType.PQ  # Default

        # Check if this is the slack bus (ext_grid connection)
        if hasattr(self.net, "ext_grid"):
            if bus_id in self.net.ext_grid["bus"].values:
                bus_type = BusType.SLACK

        # Check if this has a generator (PV bus)
        if hasattr(self.net, "gen") and bus_id in self.net.gen["bus"].values:
            bus_type = BusType.PV

        # Get bus name, handling NaN values
        bus_name = f"Bus_{bus_id}"
        if "name" in self.net.bus.columns:
            name_value = self.net.bus.at[bus_id, "name"]
            if pd.notna(name_value) and isinstance(name_value, str):
                bus_name = name_value

        return BusInfo(
            bus_id=BusID(bus_id),
            name=bus_name,
            bus_type=bus_type,
            voltage_nominal_kv=float(self.net.bus.at[bus_id, "vn_kv"]),
            grid_stix_id=None,
            grid_stix_metadata=None,
        )

    def _line_to_info(self, line_id: int) -> LineInfo:
        """Convert PandaPower line to LineInfo."""
        # Get line name, handling NaN/None values
        line_name = f"Line_{line_id}"
        if "name" in self.net.line.columns:
            name_value = self.net.line.at[line_id, "name"]
            if pd.notna(name_value) and isinstance(name_value, str):
                line_name = name_value

        return LineInfo(
            line_id=LineID(line_id),
            name=line_name,
            from_bus=BusID(int(self.net.line.at[line_id, "from_bus"])),
            to_bus=BusID(int(self.net.line.at[line_id, "to_bus"])),
            resistance_ohm=float(
                self.net.line.at[line_id, "r_ohm_per_km"]
                * self.net.line.at[line_id, "length_km"]
            ),
            reactance_ohm=float(
                self.net.line.at[line_id, "x_ohm_per_km"]
                * self.net.line.at[line_id, "length_km"]
            ),
            capacitance_nf=float(
                self.net.line.at[line_id, "c_nf_per_km"]
                * self.net.line.at[line_id, "length_km"]
            ),
            max_current_ka=float(self.net.line.at[line_id, "max_i_ka"]),
            in_service=bool(self.net.line.at[line_id, "in_service"]),
        )

    def _sgen_to_info(self, gen_id: int) -> GeneratorInfo:
        """Convert PandaPower sgen (static generator / DER) to GeneratorInfo."""
        # Map PandaPower type to DERType
        pp_type = (
            self.net.sgen.at[gen_id, "type"]
            if "type" in self.net.sgen.columns
            else "PV"
        )
        der_type_map = {
            "PV": DERType.SOLAR_PV,
            "WP": DERType.WIND,
            "Wind": DERType.WIND,
            "Storage": DERType.BATTERY_STORAGE,
        }
        der_type = der_type_map.get(pp_type, DERType.OTHER)

        # Read nameplate capacity from max_p_mw column (set at DER creation time).
        # Do NOT use net.sgen['p_mw'] here — when a DER profile is active that
        # value reflects the current irradiance-scaled output, not the nameplate.
        raw_max_p = (
            self.net.sgen.at[gen_id, "max_p_mw"]
            if "max_p_mw" in self.net.sgen.columns
            else float("nan")
        )
        if math.isnan(raw_max_p) or raw_max_p <= 0:
            raw_max_p = float(self.net.sgen.at[gen_id, "p_mw"])
        p_max_mw = max(abs(raw_max_p), 0.001)

        # Get generator name, handling NaN/None values
        gen_name = f"Gen_{gen_id}"
        if "name" in self.net.sgen.columns:
            name_value = self.net.sgen.at[gen_id, "name"]
            if pd.notna(name_value) and isinstance(name_value, str):
                gen_name = name_value

        return GeneratorInfo(
            generator_id=GeneratorID(gen_id),
            name=gen_name,
            bus=BusID(int(self.net.sgen.at[gen_id, "bus"])),
            der_type=der_type,
            p_max_mw=p_max_mw,
            p_min_mw=0.0,
            q_max_mvar=p_max_mw * 0.5,
            q_min_mvar=-p_max_mw * 0.5,
            voltage_setpoint_pu=None,
            status=(
                DeviceStatus.ONLINE
                if self.net.sgen.at[gen_id, "in_service"]
                else DeviceStatus.OFFLINE
            ),
            in_service=bool(self.net.sgen.at[gen_id, "in_service"]),
        )

    def _gen_to_info(self, gen_id: int) -> GeneratorInfo:
        """Convert PandaPower gen (conventional generator) to GeneratorInfo."""
        gen_name = f"Gen_{gen_id}"
        if "name" in self.net.gen.columns:
            name_value = self.net.gen.at[gen_id, "name"]
            if pd.notna(name_value) and isinstance(name_value, str):
                gen_name = name_value

        p_max_mw = (
            float(self.net.gen.at[gen_id, "max_p_mw"])
            if "max_p_mw" in self.net.gen.columns
            else abs(float(self.net.gen.at[gen_id, "p_mw"]))
        )
        p_min_mw = (
            float(self.net.gen.at[gen_id, "min_p_mw"])
            if "min_p_mw" in self.net.gen.columns
            else 0.0
        )
        q_max = (
            float(self.net.gen.at[gen_id, "max_q_mvar"])
            if "max_q_mvar" in self.net.gen.columns
            else p_max_mw * 0.5
        )
        q_min = (
            float(self.net.gen.at[gen_id, "min_q_mvar"])
            if "min_q_mvar" in self.net.gen.columns
            else -p_max_mw * 0.5
        )
        p_max_mw = max(abs(p_max_mw), 0.001)

        vm_pu = None
        if "vm_pu" in self.net.gen.columns:
            vm_val = self.net.gen.at[gen_id, "vm_pu"]
            if pd.notna(vm_val):
                vm_pu = float(vm_val)

        return GeneratorInfo(
            generator_id=GeneratorID(int(gen_id) + _GEN_ID_OFFSET),
            name=gen_name,
            bus=BusID(int(self.net.gen.at[gen_id, "bus"])),
            der_type=DERType.OTHER,
            p_max_mw=p_max_mw,
            p_min_mw=max(p_min_mw, 0.0),
            q_max_mvar=q_max,
            q_min_mvar=q_min,
            voltage_setpoint_pu=vm_pu,
            status=(
                DeviceStatus.ONLINE
                if self.net.gen.at[gen_id, "in_service"]
                else DeviceStatus.OFFLINE
            ),
            in_service=bool(self.net.gen.at[gen_id, "in_service"]),
        )

    def _load_to_info(self, load_id: int) -> LoadInfo:
        """Convert PandaPower load to LoadInfo."""
        # Get load name, handling NaN/None values
        load_name = f"Load_{load_id}"
        if "name" in self.net.load.columns:
            name_value = self.net.load.at[load_id, "name"]
            if pd.notna(name_value) and isinstance(name_value, str):
                load_name = name_value

        return LoadInfo(
            load_id=LoadID(load_id),
            name=load_name,
            bus=BusID(int(self.net.load.at[load_id, "bus"])),
            p_mw=float(self.net.load.at[load_id, "p_mw"]),
            q_mvar=float(self.net.load.at[load_id, "q_mvar"]),
            in_service=bool(self.net.load.at[load_id, "in_service"]),
        )

    def _storage_to_info(self, storage_id: int) -> StorageInfo:
        """Convert PandaPower storage to StorageInfo."""
        max_e_mwh = float(self.net.storage.at[storage_id, "max_e_mwh"])

        # Get storage name, handling NaN/None values
        storage_name = f"Storage_{storage_id}"
        if "name" in self.net.storage.columns:
            name_value = self.net.storage.at[storage_id, "name"]
            if pd.notna(name_value) and isinstance(name_value, str):
                storage_name = name_value

        return StorageInfo(
            storage_id=StorageID(storage_id),
            name=storage_name,
            bus=BusID(int(self.net.storage.at[storage_id, "bus"])),
            energy_capacity_mwh=max_e_mwh,
            p_max_mw=max_e_mwh / 4.0,  # Assume C/4 rate
            efficiency_charge=0.95,  # Default efficiency
            efficiency_discharge=0.95,
            soc_min_percent=10.0,
            soc_max_percent=90.0,
            status=(
                DeviceStatus.ONLINE
                if self.net.storage.at[storage_id, "in_service"]
                else DeviceStatus.OFFLINE
            ),
            in_service=bool(self.net.storage.at[storage_id, "in_service"]),
        )

    def _transformer_to_info(self, trafo_id: int) -> TransformerInfo:  # noqa: C901
        """Convert PandaPower transformer to TransformerInfo."""
        # Get transformer name, handling NaN/None values
        trafo_name = f"Trafo_{trafo_id}"
        if "name" in self.net.trafo.columns:
            name_value = self.net.trafo.at[trafo_id, "name"]
            if pd.notna(name_value) and isinstance(name_value, str):
                trafo_name = name_value

        # Get tap changer info if available
        tap_position = None
        tap_min = None
        tap_max = None
        tap_step_percent = None

        if "tap_pos" in self.net.trafo.columns:
            tap_pos_value = self.net.trafo.at[trafo_id, "tap_pos"]
            if pd.notna(tap_pos_value):
                tap_position = int(tap_pos_value)

        if "tap_min" in self.net.trafo.columns:
            tap_min_value = self.net.trafo.at[trafo_id, "tap_min"]
            if pd.notna(tap_min_value):
                tap_min = int(tap_min_value)

        if "tap_max" in self.net.trafo.columns:
            tap_max_value = self.net.trafo.at[trafo_id, "tap_max"]
            if pd.notna(tap_max_value):
                tap_max = int(tap_max_value)

        if "tap_step_percent" in self.net.trafo.columns:
            tap_step_value = self.net.trafo.at[trafo_id, "tap_step_percent"]
            if pd.notna(tap_step_value):
                tap_step_percent = float(tap_step_value)

        return TransformerInfo(
            transformer_id=TransformerID(trafo_id),
            name=trafo_name,
            hv_bus=BusID(int(self.net.trafo.at[trafo_id, "hv_bus"])),
            lv_bus=BusID(int(self.net.trafo.at[trafo_id, "lv_bus"])),
            rated_power_mva=float(self.net.trafo.at[trafo_id, "sn_mva"]),
            hv_voltage_kv=float(self.net.trafo.at[trafo_id, "vn_hv_kv"]),
            lv_voltage_kv=float(self.net.trafo.at[trafo_id, "vn_lv_kv"]),
            resistance_percent=float(self.net.trafo.at[trafo_id, "vk_percent"])
            * 0.1,  # Approximate
            reactance_percent=float(self.net.trafo.at[trafo_id, "vk_percent"]),
            tap_position=tap_position,
            tap_min=tap_min,
            tap_max=tap_max,
            tap_step_percent=tap_step_percent,
            in_service=bool(self.net.trafo.at[trafo_id, "in_service"]),
        )

    def _shunt_to_info(self, shunt_id: int) -> ShuntInfo:
        """Convert PandaPower shunt to ShuntInfo."""
        shunt_name = f"Shunt_{shunt_id}"
        if "name" in self.net.shunt.columns:
            name_value = self.net.shunt.at[shunt_id, "name"]
            if pd.notna(name_value) and isinstance(name_value, str):
                shunt_name = name_value

        return ShuntInfo(
            shunt_id=ShuntID(shunt_id),
            name=shunt_name,
            bus=BusID(int(self.net.shunt.at[shunt_id, "bus"])),
            q_mvar=float(self.net.shunt.at[shunt_id, "q_mvar"]),
            p_mw=float(self.net.shunt.at[shunt_id, "p_mw"]),
            in_service=bool(self.net.shunt.at[shunt_id, "in_service"]),
        )

    def _switch_to_info(self, switch_id: int) -> SwitchInfo:
        """Convert PandaPower switch to SwitchInfo."""
        switch_name = f"Switch_{switch_id}"
        if "name" in self.net.switch.columns:
            name_value = self.net.switch.at[switch_id, "name"]
            if pd.notna(name_value) and isinstance(name_value, str):
                switch_name = name_value

        switch_type = "CB"
        if "type" in self.net.switch.columns:
            type_value = self.net.switch.at[switch_id, "type"]
            if pd.notna(type_value) and isinstance(type_value, str):
                switch_type = type_value

        return SwitchInfo(
            switch_id=SwitchID(switch_id),
            name=switch_name,
            bus=BusID(int(self.net.switch.at[switch_id, "bus"])),
            element=int(self.net.switch.at[switch_id, "element"]),
            element_type=str(self.net.switch.at[switch_id, "et"]),
            closed=bool(self.net.switch.at[switch_id, "closed"]),
            switch_type=switch_type,
        )
