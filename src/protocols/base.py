"""
Abstract base class for SCADA protocol handlers.

This module defines the interface that all protocol implementations must follow.
"""

from abc import ABC, abstractmethod
from typing import Any, Dict, Optional

from ..engines.base import PowerSystemEngine


class ProtocolHandler(ABC):
    """
    Abstract base class for SCADA protocol handlers.

    A ProtocolHandler provides a communication interface between external
    SCADA systems and the power system engine, translating protocol-specific
    messages to engine commands and engine state to protocol responses.
    """

    def __init__(
        self, engine: PowerSystemEngine, config: Optional[Dict[str, Any]] = None
    ):
        """
        Initialize the protocol handler.

        Args:
            engine: Power system engine to interface with.
            config: Protocol-specific configuration.
        """
        self.engine = engine
        self.config = config or {}
        self._running = False

    @abstractmethod
    def start(self) -> None:
        """
        Start the protocol server/handler.

        This should initialize the protocol server and begin listening
        for incoming connections or messages.

        Raises:
            RuntimeError: If server fails to start.
        """
        pass

    @abstractmethod
    def stop(self) -> None:
        """
        Stop the protocol server/handler.

        This should gracefully shut down the protocol server and
        close all connections.
        """
        pass

    @abstractmethod
    def is_running(self) -> bool:
        """
        Check if the protocol handler is running.

        Returns:
            True if handler is active, False otherwise.
        """
        pass

    @abstractmethod
    def update_measurements(self) -> None:
        """
        Update measurements from the engine.

        This should query the current grid state from the engine
        and update the protocol's internal measurement database.
        Called periodically or after each simulation step.
        """
        pass

    @abstractmethod
    def get_statistics(self) -> Dict[str, Any]:
        """
        Get protocol statistics.

        Returns:
            Dictionary containing protocol statistics such as:
            - num_connections: Number of active connections
            - messages_sent: Total messages sent
            - messages_received: Total messages received
            - errors: Number of errors
            - uptime_seconds: Time since start
        """
        pass

    def get_config(self) -> Dict[str, Any]:
        """
        Get protocol configuration.

        Returns:
            Dictionary containing protocol configuration.
        """
        return self.config.copy()

    def update_config(self, config: Dict[str, Any]) -> None:
        """
        Update protocol configuration.

        Args:
            config: New configuration parameters.

        Note:
            Some configuration changes may require restart.
        """
        self.config.update(config)


class DNP3Handler(ProtocolHandler):
    """
    Abstract base class for DNP3 protocol handlers.

    Extends ProtocolHandler with DNP3-specific functionality.
    """

    @abstractmethod
    def get_outstation_config(self) -> Dict[str, Any]:
        """
        Get DNP3 outstation configuration.

        Returns:
            Dictionary containing DNP3-specific configuration such as:
            - outstation_address: DNP3 outstation address
            - master_address: DNP3 master address
            - port: TCP port
            - database_sizes: Point database sizes
        """
        pass

    @abstractmethod
    def add_binary_input(self, index: int, value: bool) -> None:
        """
        Add or update a binary input point.

        Args:
            index: Point index.
            value: Point value.
        """
        pass

    @abstractmethod
    def add_analog_input(self, index: int, value: float) -> None:
        """
        Add or update an analog input point.

        Args:
            index: Point index.
            value: Point value.
        """
        pass


class ModbusHandler(ProtocolHandler):
    """
    Abstract base class for Modbus protocol handlers.

    Extends ProtocolHandler with Modbus-specific functionality.
    """

    @abstractmethod
    def get_server_config(self) -> Dict[str, Any]:
        """
        Get Modbus server configuration.

        Returns:
            Dictionary containing Modbus-specific configuration such as:
            - unit_id: Modbus unit/slave ID
            - port: TCP port
            - register_blocks: Configured register blocks
        """
        pass

    @abstractmethod
    def set_holding_register(self, address: int, value: int) -> None:
        """
        Set a holding register value.

        Args:
            address: Register address.
            value: Register value (16-bit).
        """
        pass

    @abstractmethod
    def set_input_register(self, address: int, value: int) -> None:
        """
        Set an input register value.

        Args:
            address: Register address.
            value: Register value (16-bit).
        """
        pass

    @abstractmethod
    def set_coil(self, address: int, value: bool) -> None:
        """
        Set a coil value.

        Args:
            address: Coil address.
            value: Coil value.
        """
        pass

    @abstractmethod
    def set_discrete_input(self, address: int, value: bool) -> None:
        """
        Set a discrete input value.

        Args:
            address: Input address.
            value: Input value.
        """
        pass
