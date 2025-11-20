"""Modbus TCP server interface for grid simulator.

This module provides a Modbus TCP server that maps grid measurements to Modbus registers
and handles control commands from a Modbus master (SCADA MTU).

Register Mapping:
- Holding Registers 0-999: Bus voltages (scaled to int, pu * 1000)
- Holding Registers 1000-1999: Line active power flows (scaled to int, MW * 100)
- Holding Registers 2000-2999: Line reactive power flows (scaled to int, MVAR * 100)
- Holding Registers 3000-3999: Line loading (scaled to int, % * 10)
- Holding Registers 4000-4999: DER active power (scaled to int, MW * 100)
- Holding Registers 5000-5999: Load active power (scaled to int, MW * 100)
- Coils 0-999: Breaker status (closed=1, open=0)
- Coils 1000-1999: Breaker control (write to open/close)
- Holding Registers 10000-10999: DER setpoints (write, MW * 100)
- Holding Registers 11000-11999: Load adjustments (write, MW * 100)
"""

import asyncio
import logging
import math
import time
from typing import Any, Dict, Optional

from pymodbus.datastore import (
    ModbusDeviceContext,
    ModbusSequentialDataBlock,
    ModbusServerContext,
)
from pymodbus.server import StartAsyncTcpServer

from .base import ModbusHandler
from ..engines.base import PowerSystemEngine
from ..schemas.common import LineID

logger = logging.getLogger(__name__)


class GridModbusDataBlock(ModbusSequentialDataBlock):
    """
    Custom Modbus data block that handles grid simulator state updates.

    This extends the standard sequential data block to provide callbacks
    when registers are written by a Modbus master.
    """

    def __init__(self, address, values, server=None, is_coil_block=False):
        """
        Initialize the data block.

        Args:
            address: Starting address for the block
            values: Initial values for the registers
            server: GridModbusServer instance for command handling
            is_coil_block: True if this is a coil block, False for holding registers
        """
        self.server = server
        self.is_coil_block = is_coil_block
        self._initializing = True  # Prevent command processing during init
        super().__init__(address, values)
        self._initializing = False  # Enable command processing after init

    def setValues(self, address, values):
        """
        Override setValues to handle write commands from Modbus master.

        Args:
            address: Starting address being written
            values: List of values being written
        """
        super().setValues(address, values)

        # Only process commands for coil block (not holding registers)
        if self.server and self.is_coil_block:
            self._handle_write(address, values)

    def _handle_write(self, address, values):
        """
        Handle write operations by executing commands on engine.

        Args:
            address: Starting address that was written
            values: Values that were written
        """
        # Skip command processing during initialization
        if getattr(self, "_initializing", False):
            return

        # Log all write operations
        logger.info(
            f"Modbus write: address={address}, values={values[:10] if len(values) > 10 else values}"
        )

        # Breaker control coils (1000-1999)
        if 1000 <= address < 2000:
            for idx, value in enumerate(values):
                line_id = address + idx - 1000
                # Validate that line exists before executing command
                try:
                    self.server.engine.get_line_info(line_id)
                    # Execute breaker command
                    self.server.engine.set_breaker_status(line_id, bool(value))
                    logger.debug(
                        f"Modbus: {'Close' if value else 'Open'} breaker on line {line_id}"
                    )
                except KeyError:
                    logger.debug(
                        f"Modbus: Ignoring command for non-existent line {line_id}"
                    )


class GridModbusServer(ModbusHandler):
    """
    Modbus TCP server that exposes grid measurements and accepts control commands.

    This class bridges the grid simulator with Modbus protocol, allowing SCADA
    systems to monitor and control the grid using standard Modbus TCP communications.
    """

    def __init__(
        self,
        engine: PowerSystemEngine,
        config: Optional[Dict[str, Any]] = None,
        host: str = "0.0.0.0",
        port: int = 502,
    ):
        """
        Initialize Modbus server.

        Args:
            engine: PowerSystemEngine instance to interface with.
            config: Protocol configuration.
            host: IP address to bind to (default: 0.0.0.0 for all interfaces).
            port: TCP port to listen on (default: 502, standard Modbus).
        """
        super().__init__(engine, config)
        self.host = host
        self.port = port

        # Point mapping caches
        self.bus_to_hr_index: Dict[int, int] = {}
        self.line_to_hr_index: Dict[int, Dict[str, int]] = {}
        self.line_to_coil_index: Dict[int, int] = {}
        self.der_to_hr_index: Dict[int, int] = {}
        self.load_to_hr_index: Dict[int, int] = {}

        # Modbus server context
        self.context: Optional[ModbusServerContext] = None
        self.server_task: Optional[asyncio.Task] = None
        self.start_time: Optional[float] = None

        # Statistics
        self.messages_sent = 0
        self.messages_received = 0
        self.errors = 0

        logger.info(f"GridModbusServer initialized for {host}:{port}")

    def _build_point_mapping(self):
        """Build mapping between grid elements and Modbus register addresses."""
        topology = self.engine.get_topology()

        # Map buses to holding registers (0-999)
        for idx, bus_id in enumerate(topology.buses.keys()):
            self.bus_to_hr_index[bus_id] = idx
            logger.debug(f"Bus {bus_id} -> HR {idx}")

        # Map lines to holding registers and coils
        for idx, line_id in enumerate(topology.lines.keys()):
            # Active power: HR 1000+
            # Reactive power: HR 2000+
            # Loading: HR 3000+
            self.line_to_hr_index[line_id] = {
                "p_from": 1000 + idx,
                "q_from": 2000 + idx,
                "loading": 3000 + idx,
            }
            # Breaker status: Coil 0+
            self.line_to_coil_index[line_id] = idx
            logger.debug(
                f"Line {line_id} -> HR P:{1000+idx} Q:{2000+idx} L:{3000+idx}, Coil {idx}"
            )

        # Map DER to holding registers (4000+)
        for idx, der_id in enumerate(topology.generators.keys()):
            self.der_to_hr_index[der_id] = 4000 + idx
            logger.debug(f"DER {der_id} -> HR {4000+idx}")

        # Map loads to holding registers (5000+)
        for idx, load_id in enumerate(topology.loads.keys()):
            self.load_to_hr_index[load_id] = 5000 + idx
            logger.debug(f"Load {load_id} -> HR {5000+idx}")

        logger.info(
            f"Point mapping complete: {len(self.bus_to_hr_index)} buses, "
            f"{len(self.line_to_hr_index)} lines, {len(self.der_to_hr_index)} DERs, "
            f"{len(self.load_to_hr_index)} loads"
        )

    def _create_datastore(self) -> ModbusServerContext:
        """
        Create Modbus datastore with appropriate sizes.

        Returns:
            ModbusServerContext configured for grid data
        """
        # Calculate required sizes
        max_hr = (
            max(
                [
                    max(self.bus_to_hr_index.values()) if self.bus_to_hr_index else 0,
                    (
                        max([v["loading"] for v in self.line_to_hr_index.values()])
                        if self.line_to_hr_index
                        else 0
                    ),
                    max(self.der_to_hr_index.values()) if self.der_to_hr_index else 0,
                    max(self.load_to_hr_index.values()) if self.load_to_hr_index else 0,
                    11999,  # Include write registers
                ]
            )
            + 1
        )

        max_coil = (
            max(
                [
                    (
                        max(self.line_to_coil_index.values())
                        if self.line_to_coil_index
                        else 0
                    ),
                    1999,  # Include control coils
                ]
            )
            + 1
        )

        # Create data blocks
        # Coils (discrete outputs) - for breaker status and control
        initial_coil_values = [0] * max_coil
        topology = self.engine.get_topology()
        for line_id, coil_index in self.line_to_coil_index.items():
            # Set status coils (0-999) to match actual breaker state
            line_info = topology.lines[LineID(line_id)]
            initial_coil_values[coil_index] = 1 if line_info.in_service else 0

        coils = GridModbusDataBlock(0, initial_coil_values, self, is_coil_block=True)

        # Discrete inputs - not used, but required by Modbus
        discrete_inputs = ModbusSequentialDataBlock(0, [0] * 100)

        # Holding registers - for measurements and setpoints
        holding_registers = GridModbusDataBlock(
            0, [0] * max_hr, self, is_coil_block=False
        )

        # Input registers - not used, but required by Modbus
        input_registers = ModbusSequentialDataBlock(0, [0] * 100)

        # Create device context
        device_context = ModbusDeviceContext(
            di=discrete_inputs, co=coils, hr=holding_registers, ir=input_registers
        )

        # Create server context (single device, unit ID 1)
        context = ModbusServerContext(devices={1: device_context}, single=False)

        logger.info(f"Datastore created: HR={max_hr}, Coils={max_coil}")
        return context

    def _safe_float_to_int(
        self, value: float, scale: float = 1.0, default: int = 0
    ) -> int:
        """
        Safely convert a float to int, handling NaN and infinity.

        Args:
            value: Float value to convert
            scale: Scaling factor to apply before conversion
            default: Default value to use if value is invalid

        Returns:
            Integer value, or default if value is invalid
        """
        if math.isnan(value) or math.isinf(value):
            return default
        try:
            return int(value * scale)
        except (ValueError, OverflowError):
            return default

    def update_measurements(self) -> None:
        """
        Update Modbus registers with current grid state.

        This is called periodically to sync grid state to Modbus registers.
        Can accept either GridState object or legacy dict format for compatibility.
        """
        if not self.context:
            logger.warning("Server context not initialized, cannot update measurements")
            return

        try:
            # Get current grid state
            state = self.engine.get_current_state()

            # Handle None state
            if state is None:
                logger.warning("No grid state available")
                return

            # Get device context
            device_context = self.context[1]

            # Update bus voltages (HR 0-999)
            for bus_id, bus_state in state.buses.items():
                if bus_id in self.bus_to_hr_index:
                    hr_index = self.bus_to_hr_index[bus_id]
                    value = self._safe_float_to_int(bus_state.voltage_pu, 1000, 0)
                    device_context.setValues(3, hr_index, [value])

            # Update line flows and loading (HR 1000-3999)
            for line_id, line_state in state.lines.items():
                if line_id in self.line_to_hr_index:
                    indices = self.line_to_hr_index[line_id]

                    # Active power (MW * 100)
                    value_p = self._safe_float_to_int(line_state.p_from_mw, 100, 0)
                    device_context.setValues(3, indices["p_from"], [value_p])

                    # Reactive power (MVAR * 100)
                    value_q = self._safe_float_to_int(line_state.q_from_mvar, 100, 0)
                    device_context.setValues(3, indices["q_from"], [value_q])

                    # Loading (% * 10)
                    value_l = self._safe_float_to_int(line_state.loading_percent, 10, 0)
                    device_context.setValues(3, indices["loading"], [value_l])

            # Update breaker status (Coils 0-999)
            topology = self.engine.get_topology()
            for line_id_int, coil_index in self.line_to_coil_index.items():
                line_id = LineID(line_id_int)
                if line_id in topology.lines:
                    line_info = topology.lines[line_id]
                    device_context.setValues(
                        1, coil_index, [1 if line_info.in_service else 0]
                    )

            # Update DER power (HR 4000+)
            for der_id, der_state in state.generators.items():
                if der_id in self.der_to_hr_index:
                    hr_index = self.der_to_hr_index[der_id]
                    value = self._safe_float_to_int(der_state.p_mw, 100, 0)
                    device_context.setValues(3, hr_index, [value])

            # Update load power (HR 5000+)
            for load_id, load_state in state.loads.items():
                if load_id in self.load_to_hr_index:
                    hr_index = self.load_to_hr_index[load_id]
                    value = self._safe_float_to_int(load_state.p_mw, 100, 0)
                    device_context.setValues(3, hr_index, [value])

            self.messages_sent += 1

        except Exception as e:
            logger.error(f"Error updating Modbus registers: {e}")
            self.errors += 1

    async def start(self) -> None:  # type: ignore[override]
        """Start the Modbus TCP server."""
        try:
            # Build point mapping
            self._build_point_mapping()

            # Create datastore
            self.context = self._create_datastore()

            # Mark as running
            self._running = True
            self.start_time = time.time()

            # Start server
            logger.info(f"Starting Modbus TCP server on {self.host}:{self.port}")
            await StartAsyncTcpServer(
                context=self.context, address=(self.host, self.port)
            )

        except Exception as e:
            logger.error(f"Error starting Modbus server: {e}")
            self._running = False
            raise

    def stop(self) -> None:
        """Stop the Modbus TCP server."""
        try:
            if self.server_task:
                self.server_task.cancel()
                self.server_task = None

            self.context = None
            self._running = False
            logger.info("Modbus TCP server stopped")

        except Exception as e:
            logger.error(f"Error stopping Modbus server: {e}")

    def is_running(self) -> bool:
        """Check if Modbus server is running."""
        return self._running

    def get_statistics(self) -> Dict[str, Any]:
        """Get Modbus server statistics."""
        uptime = time.time() - self.start_time if self.start_time else 0
        return {
            "num_connections": 1 if self._running else 0,  # Simplified
            "messages_sent": self.messages_sent,
            "messages_received": self.messages_received,
            "errors": self.errors,
            "uptime_seconds": uptime,
        }

    def get_server_config(self) -> Dict[str, Any]:
        """Get Modbus server configuration."""
        return {
            "unit_id": 1,
            "port": self.port,
            "host": self.host,
            "register_blocks": {
                "bus_voltages": "HR 0-999",
                "line_p_from": "HR 1000-1999",
                "line_q_from": "HR 2000-2999",
                "line_loading": "HR 3000-3999",
                "der_power": "HR 4000-4999",
                "load_power": "HR 5000-5999",
                "breaker_status": "Coils 0-999",
                "breaker_control": "Coils 1000-1999",
            },
        }

    def set_holding_register(self, address: int, value: int) -> None:
        """Set a holding register value."""
        if self.context:
            device_context = self.context[1]
            device_context.setValues(3, address, [value])

    def set_input_register(self, address: int, value: int) -> None:
        """Set an input register value."""
        if self.context:
            device_context = self.context[1]
            device_context.setValues(4, address, [value])

    def set_coil(self, address: int, value: bool) -> None:
        """Set a coil value."""
        if self.context:
            device_context = self.context[1]
            device_context.setValues(1, address, [1 if value else 0])

    def set_discrete_input(self, address: int, value: bool) -> None:
        """Set a discrete input value."""
        if self.context:
            device_context = self.context[1]
            device_context.setValues(2, address, [1 if value else 0])

    def get_point_mapping(self) -> Dict[str, Any]:
        """
        Get the current point mapping configuration.

        Returns:
            Dictionary describing the point mapping
        """
        return {
            "buses": dict(self.bus_to_hr_index),
            "lines": {
                "holding_registers": dict(self.line_to_hr_index),
                "coils": dict(self.line_to_coil_index),
            },
            "ders": dict(self.der_to_hr_index),
            "loads": dict(self.load_to_hr_index),
        }
