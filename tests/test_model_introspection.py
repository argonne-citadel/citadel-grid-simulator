"""
Tests for model introspection module.

This module tests the automatic point mapping generation from Pandapower networks.
"""

import pytest

from src.model_introspection import (
    ModelIntrospector,
    PointMapping,
    ProtocolType,
    PointType,
    MeasurementType,
    generate_point_mapping,
)
from src.engines.pandapower_engine import PandaPowerEngine
from src.models.dickert_lv import DickertLVModel


class TestModelIntrospection:
    """Test model introspection functionality."""

    @pytest.fixture
    def dickert_engine(self):
        """Create Dickert LV engine for testing."""
        model = DickertLVModel(feeders_range="short", linetype="cable")
        net = model.get_network()
        return PandaPowerEngine(net)

    def test_create_introspector(self, dickert_engine):
        """Test creating a model introspector."""
        introspector = ModelIntrospector(dickert_engine, ProtocolType.MODBUS)
        assert introspector.engine is dickert_engine
        assert introspector.protocol == ProtocolType.MODBUS
        assert introspector.mapping is None

    def test_generate_modbus_mapping(self, dickert_engine):
        """Test generating Modbus point mapping."""
        introspector = ModelIntrospector(dickert_engine, ProtocolType.MODBUS)
        mapping = introspector.generate_mapping()

        assert isinstance(mapping, PointMapping)
        assert mapping.protocol == ProtocolType.MODBUS
        assert len(mapping.points) > 0
        assert mapping.base_mva > 0
        assert mapping.frequency_hz > 0

    def test_bus_voltage_points(self, dickert_engine):
        """Test that bus voltage points are created correctly."""
        mapping = generate_point_mapping(dickert_engine, ProtocolType.MODBUS)

        topology = dickert_engine.get_topology()

        # Get all bus voltage points (HR 0-999)
        voltage_points = [
            p for p in mapping.points if p.measurement_type == MeasurementType.VOLTAGE
        ]

        # Should have one voltage point per bus
        assert len(voltage_points) == len(topology.buses)

        # Check first voltage point
        first_point = voltage_points[0]
        assert first_point.point_type == PointType.HOLDING_REGISTER
        assert first_point.point_index >= 0
        assert first_point.point_index < 1000
        assert first_point.asset_type == "Bus"
        assert first_point.units == "pu"
        assert first_point.scaling_factor == 1000.0
        assert first_point.read_only is True
        assert first_point.requires_authorization is False

    def test_line_measurement_points(self, dickert_engine):
        """Test that line measurement points are created correctly."""
        mapping = generate_point_mapping(dickert_engine, ProtocolType.MODBUS)

        topology = dickert_engine.get_topology()

        # Get line active power points (HR 1000-1999)
        p_points = [
            p
            for p in mapping.points
            if p.measurement_type == MeasurementType.ACTIVE_POWER
            and p.asset_type == "Line"
            and p.read_only
        ]

        # Should have one P point per line
        assert len(p_points) == len(topology.lines)

        # Check first P point
        first_point = p_points[0]
        assert first_point.point_type == PointType.HOLDING_REGISTER
        assert first_point.point_index >= 1000
        assert first_point.point_index < 2000
        assert first_point.units == "MW"
        assert first_point.scaling_factor == 100.0

    def test_breaker_control_points(self, dickert_engine):
        """Test that breaker control points are created correctly."""
        mapping = generate_point_mapping(dickert_engine, ProtocolType.MODBUS)

        topology = dickert_engine.get_topology()

        # Get breaker control points (Coil 1000-1999)
        control_points = [
            p
            for p in mapping.points
            if p.measurement_type == MeasurementType.BREAKER_CONTROL
        ]

        # Should have one control point per line
        assert len(control_points) == len(topology.lines)

        # Check first control point
        first_point = control_points[0]
        assert first_point.point_type == PointType.COIL
        assert first_point.point_index >= 1000
        assert first_point.point_index < 2000
        assert first_point.asset_type == "Line"
        assert first_point.read_only is False
        assert first_point.requires_authorization is True  # Control requires auth

    def test_generator_points(self, dickert_engine):
        """Test that generator points are created correctly."""
        mapping = generate_point_mapping(dickert_engine, ProtocolType.MODBUS)

        topology = dickert_engine.get_topology()

        # Get generator measurement points (HR 4000+)
        gen_meas_points = [
            p
            for p in mapping.points
            if p.asset_type == "Generator"
            and p.measurement_type == MeasurementType.ACTIVE_POWER
        ]

        # Get generator setpoint points (HR 10000+)
        gen_setpoint_points = [
            p
            for p in mapping.points
            if p.measurement_type == MeasurementType.GENERATOR_SETPOINT
        ]

        # Should have measurement and setpoint for each generator
        if len(topology.generators) > 0:
            assert len(gen_meas_points) == len(topology.generators)
            assert len(gen_setpoint_points) == len(topology.generators)

            # Check setpoint requires authorization
            for point in gen_setpoint_points:
                assert point.requires_authorization is True
                assert point.read_only is False

    def test_load_points(self, dickert_engine):
        """Test that load points are created correctly."""
        mapping = generate_point_mapping(dickert_engine, ProtocolType.MODBUS)

        topology = dickert_engine.get_topology()

        # Get load measurement points (HR 5000+)
        load_meas_points = [
            p
            for p in mapping.points
            if p.asset_type == "Load"
            and p.measurement_type == MeasurementType.ACTIVE_POWER
        ]

        # Should have one measurement point per load
        assert len(load_meas_points) == len(topology.loads)

        # Check first load point
        first_point = load_meas_points[0]
        assert first_point.point_type == PointType.HOLDING_REGISTER
        assert first_point.point_index >= 5000
        assert first_point.point_index < 6000
        assert first_point.units == "MW"

    def test_point_lookup(self, dickert_engine):
        """Test point lookup functionality."""
        mapping = generate_point_mapping(dickert_engine, ProtocolType.MODBUS)

        # Test get_point by type and index
        point = mapping.get_point(PointType.HOLDING_REGISTER, 0)
        assert point is not None
        assert point.measurement_type == MeasurementType.VOLTAGE
        assert point.asset_type == "Bus"

        # Test get_point for non-existent point
        point = mapping.get_point(PointType.HOLDING_REGISTER, 99999)
        assert point is None

    def test_get_points_by_asset(self, dickert_engine):
        """Test getting all points for a specific asset."""
        mapping = generate_point_mapping(dickert_engine, ProtocolType.MODBUS)

        # Find a line that actually exists in the mapping
        line_points_all = [p for p in mapping.points if p.asset_type == "Line"]
        assert len(line_points_all) > 0, "No line points found in mapping"

        # Get the asset_id and type from the first line point
        first_line_id = line_points_all[0].asset_id
        first_line_type = line_points_all[0].asset_type

        # Get all points for this line (using asset_type to disambiguate)
        line_points = mapping.get_points_by_asset(
            first_line_id, asset_type=first_line_type
        )

        # Should have multiple points: P, Q, loading, status, control
        assert len(line_points) >= 5

        # All points should be for the same asset
        for point in line_points:
            assert point.asset_id == first_line_id
            assert point.asset_type == "Line"

    def test_get_points_by_type(self, dickert_engine):
        """Test getting all points of a specific type."""
        mapping = generate_point_mapping(dickert_engine, ProtocolType.MODBUS)

        topology = dickert_engine.get_topology()

        # Get all coil points
        coil_points = mapping.get_points_by_type(PointType.COIL)

        # Should have status + control coils for each line
        assert len(coil_points) == len(topology.lines) * 2

        # All should be coils
        for point in coil_points:
            assert point.point_type == PointType.COIL

    def test_mapping_to_dict(self, dickert_engine):
        """Test exporting mapping to dictionary."""
        mapping = generate_point_mapping(dickert_engine, ProtocolType.MODBUS)
        mapping_dict = mapping.to_dict()

        assert isinstance(mapping_dict, dict)
        assert mapping_dict["protocol"] == "modbus"
        assert "model_name" in mapping_dict
        assert "base_mva" in mapping_dict
        assert "frequency_hz" in mapping_dict
        assert "points" in mapping_dict
        assert isinstance(mapping_dict["points"], list)
        assert len(mapping_dict["points"]) == len(mapping.points)

        # Check first point structure
        first_point = mapping_dict["points"][0]
        assert "point_index" in first_point
        assert "point_type" in first_point
        assert "asset_id" in first_point
        assert "asset_type" in first_point
        assert "measurement_type" in first_point
        assert "requires_authorization" in first_point

    def test_export_to_yaml(self, dickert_engine, tmp_path):
        """Test exporting mapping to YAML file."""
        introspector = ModelIntrospector(dickert_engine, ProtocolType.MODBUS)
        mapping = introspector.generate_mapping()

        # Export to temporary file
        yaml_file = tmp_path / "mapping.yaml"
        introspector.export_to_yaml(str(yaml_file))

        # Verify file was created
        assert yaml_file.exists()

        # Verify file contains expected content
        import yaml

        with open(yaml_file, "r") as f:
            data = yaml.safe_load(f)

        assert data["protocol"] == "modbus"
        assert len(data["points"]) == len(mapping.points)

    def test_export_to_json(self, dickert_engine, tmp_path):
        """Test exporting mapping to JSON file."""
        introspector = ModelIntrospector(dickert_engine, ProtocolType.MODBUS)
        mapping = introspector.generate_mapping()

        # Export to temporary file
        json_file = tmp_path / "mapping.json"
        introspector.export_to_json(str(json_file))

        # Verify file was created
        assert json_file.exists()

        # Verify file contains expected content
        import json

        with open(json_file, "r") as f:
            data = json.load(f)

        assert data["protocol"] == "modbus"
        assert len(data["points"]) == len(mapping.points)

    def test_authorization_flags(self, dickert_engine):
        """Test that authorization flags are set correctly."""
        mapping = generate_point_mapping(dickert_engine, ProtocolType.MODBUS)

        # All read-only points should not require authorization
        read_only_points = [p for p in mapping.points if p.read_only]
        for point in read_only_points:
            assert point.requires_authorization is False

        # All control points should require authorization
        control_points = [p for p in mapping.points if not p.read_only]
        for point in control_points:
            assert point.requires_authorization is True

    def test_convenience_function(self, dickert_engine):
        """Test the convenience function for generating mappings."""
        mapping = generate_point_mapping(dickert_engine)

        assert isinstance(mapping, PointMapping)
        assert mapping.protocol == ProtocolType.MODBUS
        assert len(mapping.points) > 0

    def test_point_index_uniqueness(self, dickert_engine):
        """Test that point indices are unique within each point type."""
        mapping = generate_point_mapping(dickert_engine, ProtocolType.MODBUS)

        # Group points by type
        points_by_type = {}
        for point in mapping.points:
            if point.point_type not in points_by_type:
                points_by_type[point.point_type] = []
            points_by_type[point.point_type].append(point)

        # Check uniqueness within each type
        for point_type, points in points_by_type.items():
            indices = [p.point_index for p in points]
            assert len(indices) == len(
                set(indices)
            ), f"Duplicate indices in {point_type}"

    def test_scaling_factors(self, dickert_engine):
        """Test that scaling factors are appropriate for each measurement type."""
        mapping = generate_point_mapping(dickert_engine, ProtocolType.MODBUS)

        # Voltage should be scaled by 1000 (pu * 1000)
        voltage_points = [
            p for p in mapping.points if p.measurement_type == MeasurementType.VOLTAGE
        ]
        for point in voltage_points:
            assert point.scaling_factor == 1000.0

        # Power should be scaled by 100 (MW * 100)
        power_points = [
            p
            for p in mapping.points
            if p.measurement_type
            in [MeasurementType.ACTIVE_POWER, MeasurementType.REACTIVE_POWER]
        ]
        for point in power_points:
            assert point.scaling_factor == 100.0

        # Loading should be scaled by 10 (% * 10)
        loading_points = [
            p for p in mapping.points if p.measurement_type == MeasurementType.LOADING
        ]
        for point in loading_points:
            assert point.scaling_factor == 10.0
