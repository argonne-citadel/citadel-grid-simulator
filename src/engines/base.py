"""
Abstract base class for power system engines.

This module defines the interface that all power system modeling engines must implement.
"""

from abc import ABC, abstractmethod
from typing import Optional

from ..schemas.commands import (
    BreakerCommand,
    GeneratorCommand,
    LoadCommand,
    StorageCommand,
    TransformerTapCommand,
)
from ..schemas.common import (
    BusID,
    GeneratorID,
    LineID,
    LoadID,
    PowerMW,
    PowerMVAr,
    StorageID,
    TransformerID,
)
from ..schemas.results import PowerFlowConfig, PowerFlowResult
from ..schemas.state import GridState
from ..schemas.topology import (
    BusInfo,
    GeneratorInfo,
    LineInfo,
    LoadInfo,
    NetworkTopology,
    StorageInfo,
)


class PowerSystemEngine(ABC):
    """
    Abstract base class for power system modeling engines.

    This interface defines the contract that all power system engines
    (PandaPower, OpenDSS, PyPSA, etc.) must implement to be compatible
    with the grid simulator.
    """

    # ========================================================================
    # Core Simulation Methods
    # ========================================================================

    @abstractmethod
    def run_simulation(
        self, config: Optional[PowerFlowConfig] = None
    ) -> PowerFlowResult:
        """
        Run simulation (power flow or time-domain step).

        For steady-state solvers (PandaPower): Runs a single power flow calculation.
        For time-domain solvers (OpenDSS, FMPy): Advances simulation by one timestep.

        Args:
            config: Simulation configuration. If None, uses default settings.

        Returns:
            PowerFlowResult with convergence/solution status and metrics.

        Note:
            The name "PowerFlowResult" is used for compatibility, but for time-domain
            solvers this represents the result of a single simulation step.
        """
        pass

    @abstractmethod
    def get_convergence_status(self) -> bool:
        """
        Get the convergence/solution status of the last simulation run.

        For steady-state solvers: Returns True if power flow converged.
        For time-domain solvers: Returns True if last timestep solved successfully.

        Returns:
            True if the last simulation step was successful, False otherwise.
        """
        pass

    # ========================================================================
    # Topology Query Methods
    # ========================================================================

    @abstractmethod
    def get_topology(self) -> NetworkTopology:
        """
        Get the complete network topology.

        Returns:
            NetworkTopology containing all buses, lines, generators, loads, and storage.
        """
        pass

    @abstractmethod
    def get_bus_info(self, bus_id: BusID) -> BusInfo:
        """
        Get information about a specific bus.

        Args:
            bus_id: Bus identifier.

        Returns:
            BusInfo for the specified bus.

        Raises:
            KeyError: If bus_id does not exist.
        """
        pass

    @abstractmethod
    def get_line_info(self, line_id: LineID) -> LineInfo:
        """
        Get information about a specific line.

        Args:
            line_id: Line identifier.

        Returns:
            LineInfo for the specified line.

        Raises:
            KeyError: If line_id does not exist.
        """
        pass

    @abstractmethod
    def get_generator_info(self, generator_id: GeneratorID) -> GeneratorInfo:
        """
        Get information about a specific generator.

        Args:
            generator_id: Generator identifier.

        Returns:
            GeneratorInfo for the specified generator.

        Raises:
            KeyError: If generator_id does not exist.
        """
        pass

    @abstractmethod
    def get_load_info(self, load_id: LoadID) -> LoadInfo:
        """
        Get information about a specific load.

        Args:
            load_id: Load identifier.

        Returns:
            LoadInfo for the specified load.

        Raises:
            KeyError: If load_id does not exist.
        """
        pass

    @abstractmethod
    def get_storage_info(self, storage_id: StorageID) -> StorageInfo:
        """
        Get information about a specific storage unit.

        Args:
            storage_id: Storage identifier.

        Returns:
            StorageInfo for the specified storage unit.

        Raises:
            KeyError: If storage_id does not exist.
        """
        pass

    # ========================================================================
    # State Query Methods
    # ========================================================================

    @abstractmethod
    def get_current_state(self) -> GridState:
        """
        Get the current state of the grid.

        This should be called after running power flow to get the
        calculated voltages, power flows, etc.

        Returns:
            GridState containing current state of all components.
        """
        pass

    # ========================================================================
    # Control Methods
    # ========================================================================

    @abstractmethod
    def set_breaker_status(self, line_id: LineID, closed: bool) -> None:
        """
        Set the status of a circuit breaker (line switch).

        Args:
            line_id: Line/breaker identifier.
            closed: True to close the breaker, False to open it.

        Raises:
            KeyError: If line_id does not exist.
        """
        pass

    @abstractmethod
    def set_generator_setpoint(
        self,
        generator_id: GeneratorID,
        p_mw: Optional[PowerMW] = None,
        q_mvar: Optional[PowerMVAr] = None,
    ) -> None:
        """
        Set generator power output setpoint.

        Args:
            generator_id: Generator identifier.
            p_mw: Active power setpoint in MW. If None, unchanged.
            q_mvar: Reactive power setpoint in MVAr. If None, unchanged.

        Raises:
            KeyError: If generator_id does not exist.
            ValueError: If setpoint exceeds generator limits.
        """
        pass

    @abstractmethod
    def set_load_demand(
        self,
        load_id: LoadID,
        p_mw: Optional[PowerMW] = None,
        q_mvar: Optional[PowerMVAr] = None,
    ) -> None:
        """
        Set load power demand.

        Args:
            load_id: Load identifier.
            p_mw: Active power demand in MW. If None, unchanged.
            q_mvar: Reactive power demand in MVAr. If None, unchanged.

        Raises:
            KeyError: If load_id does not exist.
        """
        pass

    @abstractmethod
    def set_storage_power(self, storage_id: StorageID, p_mw: PowerMW) -> None:
        """
        Set storage power setpoint.

        Args:
            storage_id: Storage identifier.
            p_mw: Power setpoint in MW (positive = discharge, negative = charge).

        Raises:
            KeyError: If storage_id does not exist.
            ValueError: If power exceeds storage limits.
        """
        pass

    @abstractmethod
    def set_transformer_tap(
        self, transformer_id: TransformerID, tap_position: int
    ) -> None:
        """
        Set transformer tap position.

        Args:
            transformer_id: Transformer identifier.
            tap_position: Tap position (integer, range depends on transformer).

        Raises:
            KeyError: If transformer_id does not exist.
            ValueError: If tap_position is out of valid range.
        """
        pass

    # ========================================================================
    # Command Execution (Convenience Methods)
    # ========================================================================

    def execute_command(
        self,
        command: (
            BreakerCommand
            | GeneratorCommand
            | LoadCommand
            | StorageCommand
            | TransformerTapCommand
        ),
    ) -> None:
        """
        Execute a control command.

        This is a convenience method that dispatches to the appropriate
        control method based on command type.

        Args:
            command: Control command to execute.
        """
        if isinstance(command, BreakerCommand):
            self.set_breaker_status(command.line_id, command.closed)
        elif isinstance(command, GeneratorCommand):
            self.set_generator_setpoint(
                command.generator_id, command.p_mw, command.q_mvar
            )
        elif isinstance(command, LoadCommand):
            self.set_load_demand(command.load_id, command.p_mw, command.q_mvar)
        elif isinstance(command, StorageCommand):
            self.set_storage_power(command.storage_id, command.p_mw)
        elif isinstance(command, TransformerTapCommand):
            self.set_transformer_tap(command.transformer_id, command.tap_position)
        else:
            raise TypeError(f"Unknown command type: {type(command)}")
