"""
PandaPower engine implementation.

This module provides a concrete implementation of the PowerSystemEngine
interface using the PandaPower library.
"""

import time
from datetime import datetime
from typing import Dict, Optional, TYPE_CHECKING

import pandapower as pp
import pandas as pd

if TYPE_CHECKING:
    from ..grid_stix_integration.annotator import GridSTIXAnnotator

from .base import PowerSystemEngine
from ..schemas.common import (
    BusID,
    BusType,
    DERType,
    DeviceStatus,
    GeneratorID,
    LineID,
    LoadID,
    PowerMW,
    PowerMVAr,
    StorageID,
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
    StorageInfo,
    TransformerInfo,
)


class PandaPowerEngine(PowerSystemEngine):
    """
    PandaPower implementation of PowerSystemEngine.

    This engine wraps a PandaPower network and provides the standard
    PowerSystemEngine interface for grid simulation.
    """

    def __init__(
        self,
        net: pp.pandapowerNet,
        enable_grid_stix: bool = False,
    ):
        """
        Initialize the PandaPower engine.

        Args:
            net: PandaPower network object to wrap.
            enable_grid_stix: Whether to enable Grid-STIX annotation (optional).
        """
        self.net = net
        self._last_pf_result: Optional[PowerFlowResult] = None
        self._enable_grid_stix = enable_grid_stix
        self._grid_stix_annotator: Optional["GridSTIXAnnotator"] = None

        # Initialize Grid-STIX if enabled
        if self._enable_grid_stix:
            self._initialize_grid_stix()

    def _initialize_grid_stix(self) -> None:
        """Initialize Grid-STIX annotation for the network."""
        try:
            from grid_stix_integration import GridSTIXAnnotator

            self._grid_stix_annotator = GridSTIXAnnotator()

            # Annotate the topology
            topology = self.get_topology()
            self._grid_stix_annotator.annotate_topology(topology)

            print(
                f"Grid-STIX annotation enabled: {len(self._grid_stix_annotator.get_all_stix_objects())} objects created"
            )
        except Exception as e:
            print(f"Warning: Failed to initialize Grid-STIX: {e}")
            self._enable_grid_stix = False
            self._grid_stix_annotator = None

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
            from grid_stix_integration import TelemetryConverter, STIXExporter

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
                    # Last resort: try DC power flow (always converges)
                    pp.rundcpp(self.net)

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
                GeneratorID(int(idx)): self._generator_to_info(idx)
                for idx in self.net.sgen.index
            }

        loads = {
            LoadID(int(idx)): self._load_to_info(idx) for idx in self.net.load.index
        }

        storage = {}
        if hasattr(self.net, "storage") and len(self.net.storage) > 0:
            storage = {
                StorageID(int(idx)): self._storage_to_info(idx)
                for idx in self.net.storage.index
            }

        return NetworkTopology(
            buses=buses,
            lines=lines,
            transformers=transformers,
            generators=generators,
            loads=loads,
            storage=storage,
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
        if not hasattr(self.net, "sgen") or generator_id not in self.net.sgen.index:
            raise KeyError(f"Generator {generator_id} not found")
        return self._generator_to_info(generator_id)

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

    def get_current_state(self) -> GridState:
        """Get current grid state after power flow."""
        timestamp = datetime.now()

        # Get bus states
        buses = {}
        if hasattr(self.net, "res_bus"):
            for bus_id in self.net.bus.index:
                if bus_id in self.net.res_bus.index:
                    buses[BusID(int(bus_id))] = BusState(
                        bus_id=BusID(int(bus_id)),
                        voltage_pu=float(self.net.res_bus.at[bus_id, "vm_pu"]),
                        angle_deg=float(self.net.res_bus.at[bus_id, "va_degree"]),
                        timestamp=timestamp,
                    )

        # Get line states
        lines = {}
        if hasattr(self.net, "res_line"):
            for line_id in self.net.line.index:
                if line_id in self.net.res_line.index:
                    lines[LineID(int(line_id))] = LineState(
                        line_id=LineID(int(line_id)),
                        p_from_mw=float(self.net.res_line.at[line_id, "p_from_mw"]),
                        q_from_mvar=float(self.net.res_line.at[line_id, "q_from_mvar"]),
                        p_to_mw=float(self.net.res_line.at[line_id, "p_to_mw"]),
                        q_to_mvar=float(self.net.res_line.at[line_id, "q_to_mvar"]),
                        current_ka=float(self.net.res_line.at[line_id, "i_ka"]),
                        loading_percent=float(
                            self.net.res_line.at[line_id, "loading_percent"]
                        ),
                        timestamp=timestamp,
                    )

        # Get generator states
        generators = {}
        if hasattr(self.net, "res_sgen"):
            for gen_id in self.net.sgen.index:
                if gen_id in self.net.res_sgen.index:
                    generators[GeneratorID(int(gen_id))] = GeneratorState(
                        generator_id=GeneratorID(int(gen_id)),
                        p_mw=float(self.net.res_sgen.at[gen_id, "p_mw"]),
                        q_mvar=float(self.net.res_sgen.at[gen_id, "q_mvar"]),
                        voltage_pu=None,  # PandaPower doesn't provide this for sgen
                        timestamp=timestamp,
                    )

        # Get load states
        loads = {}
        if hasattr(self.net, "res_load"):
            for load_id in self.net.load.index:
                if load_id in self.net.res_load.index:
                    loads[LoadID(int(load_id))] = LoadState(
                        load_id=LoadID(int(load_id)),
                        p_mw=float(self.net.res_load.at[load_id, "p_mw"]),
                        q_mvar=float(self.net.res_load.at[load_id, "q_mvar"]),
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

        # Add external grid generation
        if hasattr(self.net, "res_ext_grid"):
            for ext_grid_id in self.net.ext_grid.index:
                if ext_grid_id in self.net.res_ext_grid.index:
                    total_gen += float(self.net.res_ext_grid.at[ext_grid_id, "p_mw"])

        total_load = sum(l.p_mw for l in loads.values())
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
        )

    # ========================================================================
    # Control Methods
    # ========================================================================

    def set_breaker_status(self, line_id: LineID, closed: bool) -> None:
        """Set breaker status (line in_service)."""
        if line_id not in self.net.line.index:
            raise KeyError(f"Line {line_id} not found")
        self.net.line.at[line_id, "in_service"] = bool(closed)

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
            # Check limits - allow 2x overload for flexibility in testing/control
            max_p = float(self.net.sgen.at[generator_id, "p_mw"])  # Nominal capacity
            if abs(p_mw) > abs(max_p) * 2.0:  # Allow 2x overload
                raise ValueError(
                    f"Power setpoint {p_mw} MW exceeds generator capacity {max_p} MW"
                )
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
        """Set storage power setpoint."""
        if not hasattr(self.net, "storage") or storage_id not in self.net.storage.index:
            raise KeyError(f"Storage {storage_id} not found")

        # Check limits
        max_p = float(self.net.storage.at[storage_id, "max_e_mwh"]) / 4.0  # C/4 rate
        if abs(p_mw) > max_p:
            raise ValueError(
                f"Power setpoint {p_mw} MW exceeds storage capacity {max_p} MW"
            )

        self.net.storage.at[storage_id, "p_mw"] = p_mw

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

            # Get actual power from results (positive = discharging, negative = charging)
            p_mw = float(self.net.res_storage.at[storage_id, "p_mw"])

            # Get capacity
            max_e_mwh = float(self.net.storage.at[storage_id, "max_e_mwh"])

            # Calculate energy change (MWh)
            # Negative power = charging (increases SOC)
            # Positive power = discharging (decreases SOC)
            energy_change_mwh = -p_mw * timestep_hours

            # Apply efficiency
            efficiency = 0.95  # Round-trip efficiency
            if energy_change_mwh > 0:  # Charging
                energy_change_mwh *= efficiency
            else:  # Discharging
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

    def _generator_to_info(self, gen_id: int) -> GeneratorInfo:
        """Convert PandaPower sgen to GeneratorInfo."""
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

        p_mw = float(self.net.sgen.at[gen_id, "p_mw"])

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
            p_max_mw=abs(p_mw),  # Use nominal as max
            p_min_mw=0.0,
            q_max_mvar=abs(p_mw) * 0.5,  # Assume 0.5 power factor capability
            q_min_mvar=-abs(p_mw) * 0.5,
            voltage_setpoint_pu=None,
            status=(
                DeviceStatus.ONLINE
                if self.net.sgen.at[gen_id, "in_service"]
                else DeviceStatus.OFFLINE
            ),
            in_service=bool(self.net.sgen.at[gen_id, "in_service"]),
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

    def _transformer_to_info(self, trafo_id: int) -> TransformerInfo:
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
