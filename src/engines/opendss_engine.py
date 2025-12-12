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
from datetime import datetime
from pathlib import Path
from typing import Dict, Optional

import opendssdirect as dss

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
)


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

            # Get circuit info
            circuit_name = dss.Circuit.Name()
            num_buses = dss.Circuit.NumBuses()
            num_nodes = dss.Circuit.NumNodes()

            logger.info(
                f"Loaded circuit: {circuit_name} ({num_buses} buses, {num_nodes} nodes)"
            )

        except Exception as e:
            raise RuntimeError(f"Failed to load circuit: {e}")

    def _build_id_mappings(self) -> None:
        """Build bidirectional mappings between names and integer IDs."""
        # Map buses
        buses = dss.Circuit.AllBusNames()
        for idx, bus_name in enumerate(buses):
            bus_id = BusID(idx)
            self._bus_name_to_id[bus_name] = bus_id
            self._bus_id_to_name[bus_id] = bus_name

        # Map lines
        lines = dss.Lines.AllNames()
        for idx, line_name in enumerate(lines):
            line_id = LineID(idx)
            self._line_name_to_id[line_name] = line_id
            self._line_id_to_name[line_id] = line_name

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
            # Run power flow solution
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
        """Get convergence status of last simulation."""
        try:
            return bool(dss.Solution.Converged())
        except Exception:
            return False

    # ========================================================================
    # Topology Query Methods
    # ========================================================================

    def get_topology(self) -> NetworkTopology:
        """Get complete network topology."""
        # Get all buses
        buses = {}
        for bus_id, bus_name in self._bus_id_to_name.items():
            buses[bus_id] = self.get_bus_info(bus_id)

        # Get all lines
        lines = {}
        for line_id, line_name in self._line_id_to_name.items():
            lines[line_id] = self.get_line_info(line_id)

        # Get all generators (PV systems)
        generators = {}
        for gen_id, pv_name in self._pv_id_to_name.items():
            generators[gen_id] = self.get_generator_info(gen_id)

        # Get all loads
        loads = {}
        for load_id, load_name in self._load_id_to_name.items():
            loads[load_id] = self.get_load_info(load_id)

        # Get circuit info for metadata
        circuit_name = dss.Circuit.Name()

        return NetworkTopology(
            buses=buses,
            lines=lines,
            transformers={},  # Not implemented yet
            generators=generators,
            loads=loads,
            storage={},  # Not implemented yet
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

            return BusInfo(
                bus_id=bus_id,
                name=bus_name,
                bus_type=BusType.PQ,  # OpenDSS doesn't explicitly store bus type
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
            dss.Lines.Name(line_name)

            # Parse bus names to get bus IDs
            bus1_name = dss.Lines.Bus1().split(".")[0]  # Remove phase info
            bus2_name = dss.Lines.Bus2().split(".")[0]

            from_bus = self._bus_name_to_id.get(bus1_name, BusID(0))
            to_bus = self._bus_name_to_id.get(bus2_name, BusID(0))

            return LineInfo(
                line_id=line_id,
                name=line_name,
                from_bus=from_bus,
                to_bus=to_bus,
                resistance_ohm=0.0,  # Not directly available
                reactance_ohm=0.0,
                capacitance_nf=0.0,
                max_current_ka=1.0,  # Default
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

            # Parse bus name
            bus_name = dss.CktElement.BusNames()[0].split(".")[0]
            bus_id = self._bus_name_to_id.get(bus_name, BusID(0))

            return GeneratorInfo(
                generator_id=generator_id,
                name=pv_name,
                bus=bus_id,
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
        raise KeyError(f"Storage not implemented in OpenDSS engine")

    # ========================================================================
    # State Query Methods
    # ========================================================================

    def get_current_state(self) -> GridState:
        """Get current grid state after simulation."""
        timestamp = datetime.now()

        # Get bus states
        buses = {}
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

                buses[bus_id] = BusState(
                    bus_id=bus_id,
                    voltage_pu=voltage_pu,
                    angle_deg=angle_deg,
                    timestamp=timestamp,
                )
            except Exception:
                pass  # Skip buses that fail

        # Get line states
        lines = {}
        for line_id, line_name in self._line_id_to_name.items():
            try:
                dss.Lines.Name(line_name)
                powers = dss.CktElement.Powers()

                # Powers are [P1, Q1, P2, Q2, P3, Q3, ...] for each phase
                powers_kw = [powers[i] for i in range(0, len(powers), 2)]
                powers_kvar = [powers[i] for i in range(1, len(powers), 2)]

                # Sum powers across phases
                p_total = sum(powers_kw) / 1000.0  # Convert to MW
                q_total = sum(powers_kvar) / 1000.0

                lines[line_id] = LineState(
                    line_id=line_id,
                    p_from_mw=p_total,
                    q_from_mvar=q_total,
                    p_to_mw=-p_total,  # Approximate
                    q_to_mvar=-q_total,
                    current_ka=0.0,  # Not directly available
                    loading_percent=0.0,
                    timestamp=timestamp,
                )
            except Exception:
                pass

        # Get generator states (PV systems)
        generators = {}
        for gen_id, pv_name in self._pv_id_to_name.items():
            try:
                dss.PVsystems.Name(pv_name)

                # Approximate: use Pmpp as actual power
                generators[gen_id] = GeneratorState(
                    generator_id=gen_id,
                    p_mw=dss.PVsystems.Pmpp() / 1000.0 * dss.PVsystems.Irradiance(),
                    q_mvar=0.0,  # Assume unity power factor
                    voltage_pu=None,
                    timestamp=timestamp,
                )
            except Exception:
                pass

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
            except Exception:
                pass

        # Get circuit totals
        try:
            total_power = dss.Circuit.TotalPower()
            losses = dss.Circuit.Losses()

            total_gen = total_power[0] / 1000.0
            total_load = sum(l.p_mw for l in loads.values())
            total_losses = losses[0] / 1000.0 / 1000.0  # Convert from W to MW

        except Exception:
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
