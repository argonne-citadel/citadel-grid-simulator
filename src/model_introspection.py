"""
Model Introspection Module for Grid Simulator.

This module provides protocol-agnostic point mapping generation from any power system engine.
It extracts grid topology and creates mappings between protocol points (DNP3/Modbus) and
grid assets, enabling model-agnostic authorization integration.

Key Features:
- Automatic point mapping generation from any PowerSystemEngine (Pandapower, OpenDSS, etc.)
- Support for multiple protocols (Modbus, DNP3)
- Comprehensive metadata for each point (asset_id, type, measurement, units)
- Reverse lookup: point index -> asset information
- Export mappings to YAML/JSON for documentation and configuration
"""

import logging
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Dict, List, Optional, Union

import yaml  # type: ignore[import-untyped]

from .engines.base import PowerSystemEngine
from .schemas.common import BusID, GeneratorID, LineID, LoadID, StorageID

logger = logging.getLogger(__name__)


class ProtocolType(str, Enum):
    """Supported SCADA protocol types."""

    MODBUS = "modbus"
    DNP3 = "dnp3"


class PointType(str, Enum):
    """Protocol point types."""

    # Modbus
    HOLDING_REGISTER = "holding_register"
    INPUT_REGISTER = "input_register"
    COIL = "coil"
    DISCRETE_INPUT = "discrete_input"

    # DNP3
    ANALOG_INPUT = "analog_input"
    ANALOG_OUTPUT = "analog_output"
    BINARY_INPUT = "binary_input"
    BINARY_OUTPUT = "binary_output"
    COUNTER = "counter"


class MeasurementType(str, Enum):
    """Types of measurements/controls."""

    VOLTAGE = "voltage"
    CURRENT = "current"
    ACTIVE_POWER = "active_power"
    REACTIVE_POWER = "reactive_power"
    LOADING = "loading"
    BREAKER_STATUS = "breaker_status"
    BREAKER_CONTROL = "breaker_control"
    GENERATOR_SETPOINT = "generator_setpoint"
    LOAD_SETPOINT = "load_setpoint"
    FREQUENCY = "frequency"
    ANGLE = "angle"


@dataclass
class PointMetadata:
    """Metadata for a single protocol point."""

    # Point identification
    point_index: int
    point_type: PointType
    protocol: ProtocolType

    # Asset identification
    asset_id: Union[BusID, LineID, GeneratorID, LoadID, StorageID]
    asset_type: str  # "Bus", "Line", "Generator", "Load", "Storage"
    asset_name: str

    # Measurement information
    measurement_type: MeasurementType
    description: str
    units: str
    scaling_factor: float = 1.0

    # Access control
    read_only: bool = True
    requires_authorization: bool = False

    # Additional metadata
    metadata: Dict[str, Any] = field(default_factory=dict)


@dataclass
class PointMapping:
    """Complete point mapping for a grid model."""

    protocol: ProtocolType
    model_name: str
    base_mva: float
    frequency_hz: float

    # Point mappings by type
    points: List[PointMetadata] = field(default_factory=list)

    # Reverse lookup indices
    _index_map: Dict[tuple, PointMetadata] = field(default_factory=dict, repr=False)

    def __post_init__(self):
        """Build reverse lookup index."""
        self._build_index()

    def _build_index(self):
        """Build reverse lookup index for fast point->asset queries."""
        self._index_map = {}
        for point in self.points:
            key = (point.point_type, point.point_index)
            self._index_map[key] = point

    def get_point(
        self, point_type: PointType, point_index: int
    ) -> Optional[PointMetadata]:
        """
        Get point metadata by type and index.

        Args:
            point_type: Type of point (e.g., HOLDING_REGISTER, COIL)
            point_index: Point index/address

        Returns:
            PointMetadata if found, None otherwise
        """
        return self._index_map.get((point_type, point_index))

    def get_points_by_asset(
        self,
        asset_id: Union[BusID, LineID, GeneratorID, LoadID, StorageID],
        asset_type: Optional[str] = None,
    ) -> List[PointMetadata]:
        """
        Get all points associated with an asset.

        Args:
            asset_id: Asset identifier
            asset_type: Optional asset type to filter by (e.g., "Bus", "Line")
                       If not provided, returns all points with matching asset_id
                       regardless of type.

        Returns:
            List of PointMetadata for the asset
        """
        if asset_type is None:
            # If no asset_type specified, match by both ID and type from first match
            # This handles the case where IDs might overlap between different asset types
            matching_points = [p for p in self.points if p.asset_id == asset_id]
            if matching_points:
                # Use the asset_type from the first matching point
                first_type = matching_points[0].asset_type
                return [p for p in matching_points if p.asset_type == first_type]
            return []
        else:
            return [
                p
                for p in self.points
                if p.asset_id == asset_id and p.asset_type == asset_type
            ]

    def get_points_by_type(self, point_type: PointType) -> List[PointMetadata]:
        """
        Get all points of a specific type.

        Args:
            point_type: Type of point

        Returns:
            List of PointMetadata of the specified type
        """
        return [p for p in self.points if p.point_type == point_type]

    def add_point(self, point: PointMetadata):
        """
        Add a point to the mapping.

        Args:
            point: PointMetadata to add
        """
        self.points.append(point)
        key = (point.point_type, point.point_index)
        self._index_map[key] = point

    def to_dict(self) -> Dict[str, Any]:
        """
        Export mapping to dictionary.

        Returns:
            Dictionary representation of the mapping
        """
        return {
            "protocol": self.protocol.value,
            "model_name": self.model_name,
            "base_mva": self.base_mva,
            "frequency_hz": self.frequency_hz,
            "points": [
                {
                    "point_index": p.point_index,
                    "point_type": p.point_type.value,
                    "asset_id": int(p.asset_id),
                    "asset_type": p.asset_type,
                    "asset_name": p.asset_name,
                    "measurement_type": p.measurement_type.value,
                    "description": p.description,
                    "units": p.units,
                    "scaling_factor": p.scaling_factor,
                    "read_only": p.read_only,
                    "requires_authorization": p.requires_authorization,
                    "metadata": p.metadata,
                }
                for p in self.points
            ],
        }


class ModelIntrospector:
    """
    Introspects power system engines and generates protocol point mappings.

    This class analyzes any PowerSystemEngine (Pandapower, OpenDSS, etc.) and
    automatically generates mappings between protocol points (Modbus registers,
    DNP3 points) and grid assets (buses, lines, generators, loads).
    """

    def __init__(
        self, engine: PowerSystemEngine, protocol: ProtocolType = ProtocolType.MODBUS
    ):
        """
        Initialize the introspector.

        Args:
            engine: PowerSystemEngine to introspect
            protocol: Protocol type to generate mappings for
        """
        self.engine = engine
        self.protocol = protocol
        self.mapping: Optional[PointMapping] = None

    def generate_mapping(self) -> PointMapping:
        """
        Generate complete point mapping for the network.

        Returns:
            PointMapping object with all points defined
        """
        # Get topology from engine
        topology = self.engine.get_topology()

        model_name = topology.name if topology.name else "Network"
        base_mva = topology.base_mva
        frequency_hz = topology.frequency_hz

        self.mapping = PointMapping(
            protocol=self.protocol,
            model_name=model_name,
            base_mva=base_mva,
            frequency_hz=frequency_hz,
        )

        if self.protocol == ProtocolType.MODBUS:
            self._generate_modbus_mapping()
        elif self.protocol == ProtocolType.DNP3:
            self._generate_dnp3_mapping()
        else:
            raise ValueError(f"Unsupported protocol: {self.protocol}")

        logger.info(
            f"Generated {len(self.mapping.points)} point mappings for {model_name} ({self.protocol.value})"
        )
        return self.mapping

    def _generate_modbus_mapping(self):
        """Generate Modbus register mapping."""
        if self.mapping is None:
            raise RuntimeError(
                "Mapping must be initialized before _generate_modbus_mapping() — "
                "construct the introspector via initialize_mapping() first."
            )

        # Get topology from engine
        topology = self.engine.get_topology()

        # Bus voltages: Holding Registers 0-999
        for idx, bus_id in enumerate(topology.buses):
            bus_info = self.engine.get_bus_info(bus_id)
            bus_name = bus_info.name if bus_info.name else f"Bus_{bus_id}"

            self.mapping.add_point(
                PointMetadata(
                    point_index=idx,
                    point_type=PointType.HOLDING_REGISTER,
                    protocol=ProtocolType.MODBUS,
                    asset_id=bus_id,
                    asset_type="Bus",
                    asset_name=bus_name,
                    measurement_type=MeasurementType.VOLTAGE,
                    description=f"{bus_name} voltage",
                    units="pu",
                    scaling_factor=1000.0,  # pu * 1000 -> int
                    read_only=True,
                    requires_authorization=False,
                )
            )

        # Line measurements: Holding Registers 1000-3999
        for idx, line_id in enumerate(topology.lines):
            line_info = self.engine.get_line_info(line_id)
            line_name = line_info.name if line_info.name else f"Line_{line_id}"

            # Active power from (HR 1000+)
            self.mapping.add_point(
                PointMetadata(
                    point_index=1000 + idx,
                    point_type=PointType.HOLDING_REGISTER,
                    protocol=ProtocolType.MODBUS,
                    asset_id=line_id,
                    asset_type="Line",
                    asset_name=line_name,
                    measurement_type=MeasurementType.ACTIVE_POWER,
                    description=f"{line_name} active power (from)",
                    units="MW",
                    scaling_factor=100.0,  # MW * 100 -> int
                    read_only=True,
                    requires_authorization=False,
                )
            )

            # Reactive power from (HR 2000+)
            self.mapping.add_point(
                PointMetadata(
                    point_index=2000 + idx,
                    point_type=PointType.HOLDING_REGISTER,
                    protocol=ProtocolType.MODBUS,
                    asset_id=line_id,
                    asset_type="Line",
                    asset_name=line_name,
                    measurement_type=MeasurementType.REACTIVE_POWER,
                    description=f"{line_name} reactive power (from)",
                    units="MVAR",
                    scaling_factor=100.0,  # MVAR * 100 -> int
                    read_only=True,
                    requires_authorization=False,
                )
            )

            # Loading (HR 3000+)
            self.mapping.add_point(
                PointMetadata(
                    point_index=3000 + idx,
                    point_type=PointType.HOLDING_REGISTER,
                    protocol=ProtocolType.MODBUS,
                    asset_id=line_id,
                    asset_type="Line",
                    asset_name=line_name,
                    measurement_type=MeasurementType.LOADING,
                    description=f"{line_name} loading",
                    units="%",
                    scaling_factor=10.0,  # % * 10 -> int
                    read_only=True,
                    requires_authorization=False,
                )
            )

            # Breaker status (Coil 0+)
            self.mapping.add_point(
                PointMetadata(
                    point_index=idx,
                    point_type=PointType.COIL,
                    protocol=ProtocolType.MODBUS,
                    asset_id=line_id,
                    asset_type="Line",
                    asset_name=line_name,
                    measurement_type=MeasurementType.BREAKER_STATUS,
                    description=f"{line_name} breaker status",
                    units="bool",
                    scaling_factor=1.0,
                    read_only=True,
                    requires_authorization=False,
                )
            )

            # Breaker control (Coil 1000+)
            self.mapping.add_point(
                PointMetadata(
                    point_index=1000 + idx,
                    point_type=PointType.COIL,
                    protocol=ProtocolType.MODBUS,
                    asset_id=line_id,
                    asset_type="Line",
                    asset_name=line_name,
                    measurement_type=MeasurementType.BREAKER_CONTROL,
                    description=f"{line_name} breaker control",
                    units="bool",
                    scaling_factor=1.0,
                    read_only=False,
                    requires_authorization=True,  # Control operations require authorization
                )
            )

        # Generator/DER power: Holding Registers 4000+
        for idx, gen_id in enumerate(topology.generators):
            gen_info = self.engine.get_generator_info(gen_id)
            gen_name = gen_info.name if gen_info.name else f"Gen_{gen_id}"

            self.mapping.add_point(
                PointMetadata(
                    point_index=4000 + idx,
                    point_type=PointType.HOLDING_REGISTER,
                    protocol=ProtocolType.MODBUS,
                    asset_id=gen_id,
                    asset_type="Generator",
                    asset_name=gen_name,
                    measurement_type=MeasurementType.ACTIVE_POWER,
                    description=f"{gen_name} active power",
                    units="MW",
                    scaling_factor=100.0,  # MW * 100 -> int
                    read_only=True,
                    requires_authorization=False,
                )
            )

            # Generator setpoint (HR 10000+)
            self.mapping.add_point(
                PointMetadata(
                    point_index=10000 + idx,
                    point_type=PointType.HOLDING_REGISTER,
                    protocol=ProtocolType.MODBUS,
                    asset_id=gen_id,
                    asset_type="Generator",
                    asset_name=gen_name,
                    measurement_type=MeasurementType.GENERATOR_SETPOINT,
                    description=f"{gen_name} power setpoint",
                    units="MW",
                    scaling_factor=100.0,  # MW * 100 -> int
                    read_only=False,
                    requires_authorization=True,  # Control operations require authorization
                )
            )

        # Load power: Holding Registers 5000+
        for idx, load_id in enumerate(topology.loads):
            load_info = self.engine.get_load_info(load_id)
            load_name = load_info.name if load_info.name else f"Load_{load_id}"

            self.mapping.add_point(
                PointMetadata(
                    point_index=5000 + idx,
                    point_type=PointType.HOLDING_REGISTER,
                    protocol=ProtocolType.MODBUS,
                    asset_id=load_id,
                    asset_type="Load",
                    asset_name=load_name,
                    measurement_type=MeasurementType.ACTIVE_POWER,
                    description=f"{load_name} active power",
                    units="MW",
                    scaling_factor=100.0,  # MW * 100 -> int
                    read_only=True,
                    requires_authorization=False,
                )
            )

            # Load setpoint (HR 11000+)
            self.mapping.add_point(
                PointMetadata(
                    point_index=11000 + idx,
                    point_type=PointType.HOLDING_REGISTER,
                    protocol=ProtocolType.MODBUS,
                    asset_id=load_id,
                    asset_type="Load",
                    asset_name=load_name,
                    measurement_type=MeasurementType.LOAD_SETPOINT,
                    description=f"{load_name} power setpoint",
                    units="MW",
                    scaling_factor=100.0,  # MW * 100 -> int
                    read_only=False,
                    requires_authorization=True,  # Control operations require authorization
                )
            )

        # Storage power: Holding Registers 6000+ (if storage exists)
        for idx, storage_id in enumerate(topology.storage):
            storage_info = self.engine.get_storage_info(storage_id)
            storage_name = (
                storage_info.name if storage_info.name else f"Storage_{storage_id}"
            )

            self.mapping.add_point(
                PointMetadata(
                    point_index=6000 + idx,
                    point_type=PointType.HOLDING_REGISTER,
                    protocol=ProtocolType.MODBUS,
                    asset_id=storage_id,
                    asset_type="Storage",
                    asset_name=storage_name,
                    measurement_type=MeasurementType.ACTIVE_POWER,
                    description=f"{storage_name} active power",
                    units="MW",
                    scaling_factor=100.0,  # MW * 100 -> int
                    read_only=True,
                    requires_authorization=False,
                )
            )

    def _generate_dnp3_mapping(self):
        """Generate DNP3 point mapping (placeholder for future implementation)."""
        # DNP3 mapping follows similar pattern but uses different point types
        # This will be implemented when DNP3 support is added
        logger.warning("DNP3 mapping generation not yet implemented")

    def export_to_yaml(self, filepath: str):
        """
        Export point mapping to YAML file.

        Args:
            filepath: Path to output YAML file
        """
        if not self.mapping:
            raise ValueError("No mapping generated. Call generate_mapping() first.")

        with open(filepath, "w") as f:
            yaml.dump(
                self.mapping.to_dict(), f, default_flow_style=False, sort_keys=False
            )

        logger.info(f"Exported point mapping to {filepath}")

    def export_to_json(self, filepath: str):
        """
        Export point mapping to JSON file.

        Args:
            filepath: Path to output JSON file
        """
        import json

        if not self.mapping:
            raise ValueError("No mapping generated. Call generate_mapping() first.")

        with open(filepath, "w") as f:
            json.dump(self.mapping.to_dict(), f, indent=2)

        logger.info(f"Exported point mapping to {filepath}")


def generate_point_mapping(
    engine: PowerSystemEngine, protocol: ProtocolType = ProtocolType.MODBUS
) -> PointMapping:
    """
    Convenience function to generate point mapping from a PowerSystemEngine.

    Args:
        engine: PowerSystemEngine instance (Pandapower, OpenDSS, etc.)
        protocol: Protocol type (default: Modbus)

    Returns:
        PointMapping object
    """
    introspector = ModelIntrospector(engine, protocol)
    return introspector.generate_mapping()
