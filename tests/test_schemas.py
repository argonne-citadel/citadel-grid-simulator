"""
Tests for Pydantic schema models.

Tests validation rules, type constraints, serialization/deserialization,
and edge cases for all schema models.
"""

import pytest
from datetime import datetime
from pydantic import ValidationError

from src.schemas.common import (
    BusID,
    LineID,
    GeneratorID,
    LoadID,
    StorageID,
    TransformerID,
    BusType,
    DeviceStatus,
    DERType,
    VoltagePU,
    AngleDeg,
    PowerMW,
    PowerMVAr,
    Frequency,
)
from src.schemas.topology import (
    BusInfo,
    LineInfo,
    GeneratorInfo,
    LoadInfo,
    StorageInfo,
    NetworkTopology,
)
from src.schemas.state import (
    BusState,
    LineState,
    GeneratorState,
    LoadState,
    StorageState,
    GridState,
)
from src.schemas.commands import (
    CommandType,
    BreakerCommand,
    GeneratorCommand,
    LoadCommand,
    StorageCommand,
    TransformerTapCommand,
)
from src.schemas.results import PowerFlowAlgorithm, PowerFlowConfig, PowerFlowResult


class TestCommonTypes:
    """Test common type aliases and enums."""

    def test_bus_type_enum(self):
        """Test BusType enum values."""
        assert BusType.PQ == "pq"
        assert BusType.PV == "pv"
        assert BusType.SLACK == "slack"

    def test_device_status_enum(self):
        """Test DeviceStatus enum values."""
        assert DeviceStatus.ONLINE == "online"
        assert DeviceStatus.OFFLINE == "offline"
        assert DeviceStatus.MAINTENANCE == "maintenance"

    def test_der_type_enum(self):
        """Test DERType enum values."""
        assert DERType.SOLAR_PV == "solar_pv"
        assert DERType.WIND == "wind"
        assert DERType.BATTERY_STORAGE == "battery_storage"
        assert DERType.OTHER == "other"


class TestTopologyModels:
    """Test topology schema models."""

    def test_bus_info_creation(self):
        """Test BusInfo model creation."""
        bus = BusInfo(
            bus_id=1,
            name="Bus 1",
            bus_type=BusType.PQ,
            voltage_nominal_kv=0.4,
        )
        assert bus.bus_id == 1
        assert bus.name == "Bus 1"
        assert bus.bus_type == BusType.PQ
        assert bus.voltage_nominal_kv == 0.4

    def test_bus_info_optional_fields(self):
        """Test BusInfo with optional Grid-STIX fields."""
        bus = BusInfo(
            bus_id=1,
            name="Bus 1",
            bus_type=BusType.PQ,
            voltage_nominal_kv=0.4,
            grid_stix_id="substation--123",
            grid_stix_metadata={"location": "Chicago"},
        )
        assert bus.grid_stix_id == "substation--123"
        assert bus.grid_stix_metadata == {"location": "Chicago"}

    def test_line_info_creation(self):
        """Test LineInfo model creation."""
        line = LineInfo(
            line_id=1,
            name="Line 1",
            from_bus=1,
            to_bus=2,
            resistance_ohm=0.1,
            reactance_ohm=0.05,
            capacitance_nf=10.0,
            max_current_ka=0.2,
            in_service=True,
        )
        assert line.line_id == 1
        assert line.from_bus == 1
        assert line.to_bus == 2
        assert line.resistance_ohm == 0.1

    def test_generator_info_creation(self):
        """Test GeneratorInfo model creation."""
        gen = GeneratorInfo(
            generator_id=1,
            name="PV 1",
            bus=2,
            der_type=DERType.SOLAR_PV,
            p_max_mw=0.1,
            p_min_mw=0.0,
            q_max_mvar=0.05,
            q_min_mvar=-0.05,
            status=DeviceStatus.ONLINE,
            in_service=True,
        )
        assert gen.generator_id == 1
        assert gen.der_type == DERType.SOLAR_PV
        assert gen.p_max_mw == 0.1

    def test_load_info_creation(self):
        """Test LoadInfo model creation."""
        load = LoadInfo(
            load_id=1,
            name="Load 1",
            bus=3,
            p_mw=0.05,
            q_mvar=0.01,
            in_service=True,
        )
        assert load.load_id == 1
        assert load.p_mw == 0.05
        assert load.q_mvar == 0.01

    def test_storage_info_creation(self):
        """Test StorageInfo model creation."""
        storage = StorageInfo(
            storage_id=1,
            name="BESS 1",
            bus=4,
            energy_capacity_mwh=0.1,
            p_max_mw=0.025,
            efficiency_charge=0.95,
            efficiency_discharge=0.95,
            soc_min_percent=10.0,
            soc_max_percent=90.0,
            status=DeviceStatus.ONLINE,
            in_service=True,
        )
        assert storage.storage_id == 1
        assert storage.energy_capacity_mwh == 0.1
        assert storage.efficiency_charge == 0.95

    def test_network_topology_creation(self):
        """Test NetworkTopology model creation."""
        bus1 = BusInfo(
            bus_id=1, name="Bus 1", bus_type=BusType.SLACK, voltage_nominal_kv=0.4
        )
        bus2 = BusInfo(
            bus_id=2, name="Bus 2", bus_type=BusType.PQ, voltage_nominal_kv=0.4
        )
        line1 = LineInfo(
            line_id=1,
            name="Line 1",
            from_bus=1,
            to_bus=2,
            resistance_ohm=0.1,
            reactance_ohm=0.05,
            capacitance_nf=10.0,
            max_current_ka=0.2,
            in_service=True,
        )

        topology = NetworkTopology(
            buses={1: bus1, 2: bus2},
            lines={1: line1},
            generators={},
            loads={},
            storage={},
            name="Test Network",
            base_mva=1.0,
            frequency_hz=60.0,
        )

        assert len(topology.buses) == 2
        assert len(topology.lines) == 1
        assert topology.name == "Test Network"
        assert topology.base_mva == 1.0


class TestStateModels:
    """Test state schema models."""

    def test_bus_state_creation(self):
        """Test BusState model creation."""
        timestamp = datetime.now()
        state = BusState(
            bus_id=1,
            voltage_pu=1.02,
            angle_deg=0.5,
            timestamp=timestamp,
        )
        assert state.bus_id == 1
        assert state.voltage_pu == 1.02
        assert state.angle_deg == 0.5
        assert state.timestamp == timestamp

    def test_line_state_creation(self):
        """Test LineState model creation."""
        timestamp = datetime.now()
        state = LineState(
            line_id=1,
            p_from_mw=0.05,
            q_from_mvar=0.01,
            p_to_mw=-0.048,
            q_to_mvar=-0.009,
            current_ka=0.15,
            loading_percent=75.0,
            timestamp=timestamp,
        )
        assert state.line_id == 1
        assert state.p_from_mw == 0.05
        assert state.loading_percent == 75.0

    def test_generator_state_creation(self):
        """Test GeneratorState model creation."""
        timestamp = datetime.now()
        state = GeneratorState(
            generator_id=1,
            p_mw=0.08,
            q_mvar=0.02,
            voltage_pu=1.0,
            timestamp=timestamp,
        )
        assert state.generator_id == 1
        assert state.p_mw == 0.08

    def test_load_state_creation(self):
        """Test LoadState model creation."""
        timestamp = datetime.now()
        state = LoadState(
            load_id=1,
            p_mw=0.05,
            q_mvar=0.01,
            timestamp=timestamp,
        )
        assert state.load_id == 1
        assert state.p_mw == 0.05

    def test_storage_state_creation(self):
        """Test StorageState model creation."""
        timestamp = datetime.now()
        state = StorageState(
            storage_id=1,
            p_mw=0.02,
            soc_mwh=0.05,
            soc_percent=50.0,
            timestamp=timestamp,
        )
        assert state.storage_id == 1
        assert state.soc_percent == 50.0

    def test_grid_state_creation(self):
        """Test GridState model creation."""
        timestamp = datetime.now()
        bus_state = BusState(
            bus_id=1, voltage_pu=1.0, angle_deg=0.0, timestamp=timestamp
        )

        grid_state = GridState(
            timestamp=timestamp,
            buses={1: bus_state},
            lines={},
            generators={},
            loads={},
            storage={},
            converged=True,
            iterations=5,
            total_generation_mw=0.1,
            total_load_mw=0.095,
            total_losses_mw=0.005,
        )

        assert grid_state.converged is True
        assert grid_state.iterations == 5
        assert len(grid_state.buses) == 1


class TestCommandModels:
    """Test command schema models."""

    def test_breaker_command_creation(self):
        """Test BreakerCommand model creation."""
        cmd = BreakerCommand(
            line_id=1,
            closed=False,
        )
        assert cmd.command_type == CommandType.BREAKER
        assert cmd.line_id == 1
        assert cmd.closed is False

    def test_generator_command_creation(self):
        """Test GeneratorCommand model creation."""
        cmd = GeneratorCommand(
            generator_id=1,
            p_mw=0.08,
            q_mvar=0.02,
        )
        assert cmd.command_type == CommandType.GENERATOR_SETPOINT
        assert cmd.generator_id == 1
        assert cmd.p_mw == 0.08

    def test_load_command_creation(self):
        """Test LoadCommand model creation."""
        cmd = LoadCommand(
            load_id=1,
            p_mw=0.06,
            q_mvar=0.015,
        )
        assert cmd.command_type == CommandType.LOAD_ADJUSTMENT
        assert cmd.load_id == 1
        assert cmd.p_mw == 0.06

    def test_storage_command_creation(self):
        """Test StorageCommand model creation."""
        cmd = StorageCommand(
            storage_id=1,
            p_mw=0.025,
        )
        assert cmd.command_type == CommandType.STORAGE_CONTROL
        assert cmd.storage_id == 1
        assert cmd.p_mw == 0.025

    def test_transformer_tap_command_creation(self):
        """Test TransformerTapCommand model creation."""
        cmd = TransformerTapCommand(
            transformer_id=1,
            tap_position=2,
        )
        assert cmd.command_type == CommandType.TRANSFORMER_TAP
        assert cmd.transformer_id == 1
        assert cmd.tap_position == 2


class TestResultModels:
    """Test result schema models."""

    def test_power_flow_config_creation(self):
        """Test PowerFlowConfig model creation."""
        config = PowerFlowConfig(
            algorithm=PowerFlowAlgorithm.NEWTON_RAPHSON,
            max_iterations=100,
            tolerance=1e-6,
            enforce_q_limits=True,
            distributed_slack=False,
        )
        assert config.algorithm == PowerFlowAlgorithm.NEWTON_RAPHSON
        assert config.max_iterations == 100
        assert config.tolerance == 1e-6

    def test_power_flow_config_defaults(self):
        """Test PowerFlowConfig default values."""
        config = PowerFlowConfig()
        assert config.algorithm == PowerFlowAlgorithm.NEWTON_RAPHSON
        assert config.max_iterations == 100
        assert config.tolerance == 1e-6

    def test_power_flow_result_success(self):
        """Test PowerFlowResult for successful convergence."""
        config = PowerFlowConfig()
        result = PowerFlowResult(
            converged=True,
            iterations=5,
            max_bus_p_mismatch=1e-9,
            max_bus_q_mismatch=5e-10,
            execution_time_ms=15.5,
            error_message=None,
            config=config,
        )
        assert result.converged is True
        assert result.iterations == 5
        assert result.error_message is None

    def test_power_flow_result_failure(self):
        """Test PowerFlowResult for failed convergence."""
        config = PowerFlowConfig()
        result = PowerFlowResult(
            converged=False,
            iterations=100,
            max_bus_p_mismatch=0.5,
            max_bus_q_mismatch=0.3,
            execution_time_ms=250.0,
            error_message="Power flow did not converge",
            config=config,
        )
        assert result.converged is False
        assert result.iterations == 100
        assert result.error_message == "Power flow did not converge"


class TestSerialization:
    """Test model serialization and deserialization."""

    def test_bus_info_serialization(self):
        """Test BusInfo JSON serialization."""
        bus = BusInfo(
            bus_id=1,
            name="Bus 1",
            bus_type=BusType.PQ,
            voltage_nominal_kv=0.4,
        )

        # Serialize to dict
        bus_dict = bus.model_dump()
        assert bus_dict["bus_id"] == 1
        assert bus_dict["bus_type"] == "pq"

        # Deserialize from dict
        bus2 = BusInfo(**bus_dict)
        assert bus2.bus_id == bus.bus_id
        assert bus2.name == bus.name

    def test_grid_state_serialization(self):
        """Test GridState JSON serialization."""
        timestamp = datetime.now()
        bus_state = BusState(
            bus_id=1, voltage_pu=1.0, angle_deg=0.0, timestamp=timestamp
        )

        grid_state = GridState(
            timestamp=timestamp,
            buses={1: bus_state},
            lines={},
            generators={},
            loads={},
            storage={},
            converged=True,
            total_generation_mw=0.1,
            total_load_mw=0.095,
            total_losses_mw=0.005,
        )

        # Serialize to dict
        state_dict = grid_state.model_dump()
        assert state_dict["converged"] is True
        assert 1 in state_dict["buses"]

        # Deserialize from dict
        grid_state2 = GridState(**state_dict)
        assert grid_state2.converged == grid_state.converged
        assert len(grid_state2.buses) == len(grid_state.buses)


class TestEdgeCases:
    """Test edge cases and validation."""

    def test_positive_voltage_required(self):
        """Test that voltage must be positive."""
        # Voltage must be > 0 per schema validation
        with pytest.raises(ValidationError) as exc_info:
            bus = BusInfo(
                bus_id=1,
                name="Bus 1",
                bus_type=BusType.PQ,
                voltage_nominal_kv=-0.4,  # Should fail
            )
        assert "greater_than" in str(exc_info.value)

    def test_zero_power(self):
        """Test zero power values."""
        load = LoadInfo(
            load_id=1,
            name="Load 1",
            bus=1,
            p_mw=0.0,
            q_mvar=0.0,
            in_service=True,
        )
        assert load.p_mw == 0.0
        assert load.q_mvar == 0.0

    def test_large_values(self):
        """Test handling of large values."""
        gen = GeneratorInfo(
            generator_id=1,
            name="Large Gen",
            bus=1,
            der_type=DERType.OTHER,
            p_max_mw=1000.0,
            p_min_mw=0.0,
            q_max_mvar=500.0,
            q_min_mvar=-500.0,
            status=DeviceStatus.ONLINE,
            in_service=True,
        )
        assert gen.p_max_mw == 1000.0

    def test_optional_none_values(self):
        """Test optional Grid-STIX fields with None values."""
        bus = BusInfo(
            bus_id=1,
            name="Bus 1",
            bus_type=BusType.PQ,
            voltage_nominal_kv=0.4,
            grid_stix_id=None,
            grid_stix_metadata=None,
        )
        assert bus.grid_stix_id is None
        assert bus.grid_stix_metadata is None
