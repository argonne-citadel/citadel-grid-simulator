"""DNP3 outstation interface for grid simulator.

NOTE: DNP3 support is currently not implemented.

The available Python DNP3 libraries have significant limitations:
- pydnp3: Outdated, incompatible with modern CMake versions
- dnp3protocol: Low-level library requiring full server implementation from scratch

DNP3 support is planned for future implementation. For now, use Modbus TCP
which is fully functional and widely supported in SCADA systems.

This file is a placeholder for future DNP3 implementation.
"""

import logging
from typing import Any, Dict, Optional

from .base import DNP3Handler
from ..engines.base import PowerSystemEngine

logger = logging.getLogger(__name__)


class GridDNP3Outstation(DNP3Handler):
    """
    Placeholder for DNP3 outstation implementation.

    DNP3 support is not currently available. Use Modbus TCP instead.
    """

    def __init__(
        self,
        engine: PowerSystemEngine,
        config: Optional[Dict[str, Any]] = None,
        host: str = "0.0.0.0",
        port: int = 20000,
    ):
        """
        Initialize DNP3 outstation placeholder.

        Args:
            engine: Power system engine instance.
            config: Protocol configuration.
            host: IP address to bind to.
            port: TCP port to listen on.

        Raises:
            NotImplementedError: DNP3 is not currently supported.
        """
        super().__init__(engine, config)
        self.host = host
        self.port = port
        raise NotImplementedError(
            "DNP3 support is not currently implemented. "
            "Please use Modbus TCP which is fully functional."
        )

    def start(self) -> None:
        """Start the DNP3 outstation."""
        raise NotImplementedError("DNP3 support is not currently implemented")

    def stop(self) -> None:
        """Stop the DNP3 outstation."""
        pass

    def is_running(self) -> bool:
        """Check if DNP3 outstation is running."""
        return False

    def update_measurements(self) -> None:
        """Update DNP3 points with current grid state."""
        pass

    def get_statistics(self) -> Dict[str, Any]:
        """Get DNP3 statistics."""
        return {
            "num_connections": 0,
            "messages_sent": 0,
            "messages_received": 0,
            "errors": 0,
            "uptime_seconds": 0,
        }

    def get_outstation_config(self) -> Dict[str, Any]:
        """Get DNP3 outstation configuration."""
        return {
            "outstation_address": 0,
            "master_address": 0,
            "port": self.port,
            "database_sizes": {},
        }

    def add_binary_input(self, index: int, value: bool) -> None:
        """Add or update a binary input point."""
        pass

    def add_analog_input(self, index: int, value: float) -> None:
        """Add or update an analog input point."""
        pass
