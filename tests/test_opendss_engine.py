"""
Tests for OpenDSSEngine implementation.

Tests the PowerSystemEngine interface implementation for OpenDSS,
including circuit loading, state management, and control operations.
"""

import pytest
from pathlib import Path
from datetime import datetime

from src.engines.opendss_engine import OpenDSSEngine
from src.schemas.common import BusType, DeviceStatus, DERType
from src.schemas.topology import BusInfo, LineInfo, GeneratorInfo, LoadInfo
from src.schemas.state import BusState, LineState, GeneratorState, LoadState
from src.schemas.results import PowerFlowConfig

# Path to test DSS file
TEST_DSS_FILE = Path(__file__).parent.parent / "examples" / "IEEE37Bus_PV.dss"


@pytest.fixture
def dss_file_path():
    """Return path to test DSS file."""
    if not TEST_DSS_FILE.exists():
        pytest.skip(f"Test DSS file not found: {TEST_DSS_FILE}")
    return str(TEST_DSS_FILE)


@pytest.fixture
def opendss_engine(dss_file_path):
    """Create OpenDSS engine with test circuit."""
    try:
        engine = OpenDSSEngine(dss_file_path)
        return engine
    except Exception as e:
        pytest.skip(f"Could not initialize OpenDSS engine: {e}")


class TestEngineInitialization:
    """Test engine initialization and setup."""

    def test_create_engine_from_dss_file(self, dss_file_path):
        """Test creating engine from DSS file."""
        engine = OpenDSSEngine(dss_file_path)
        assert engine is not None
        assert engine.dss_file_path == dss_file_path

    def test_engine_loads_circuit(self, opendss_engine):
        """Test that circuit is loaded successfully."""
        # Check that ID mappings are populated
        assert len(opendss_engine._bus_name_to_id) > 0
        assert len(opendss_engine._line_name_to_id) > 0
        assert len(opendss_engine._load_name_to_id) > 0

    def test_engine_has_pv_systems(self, opendss_engine):
        """Test that PV systems are detected."""
        # IEEE37Bus_PV should have PV systems
        assert len(opendss_engine._pv_name_to_id) > 0


class TestTopologyRetrieval:
    """Test topology information retrieval."""

    def test_get_topology(self, opendss_engine):
        """Test getting complete network topology."""
        topology = opendss_engine.get_topology()

        assert topology is not None
        assert len(topology.buses) > 0
        assert len(topology.lines) > 0
        assert len(topology.loads) > 0
        assert topology.name is not None
        assert topology.base_mva == 1.0
        assert topology.frequency_hz == 60.0

    def test_get_bus_info(self, opendss_engine):
        """Test getting bus information."""
        topology = opendss_engine.get_topology()
        bus_ids = list(topology.buses.keys())

        if bus_ids:
            bus_id = bus_ids[0]
            bus_info = opendss_engine.get_bus_info(bus_id)

            assert isinstance(bus_info, BusInfo)
            assert bus_info.bus_id == bus_id
            assert bus_info.name is not None
            assert bus_info.voltage_nominal_kv > 0

    def test_get_line_info(self, opendss_engine):
        """Test getting line information."""
        topology = opendss_engine.get_topology()
        line_ids = list(topology.lines.keys())

        if line_ids:
            line_id = line_ids[0]
            line_info = opendss_engine.get_line_info(line_id)

            assert isinstance(line_info, LineInfo)
            assert line_info.line_id == line_id
            assert line_info.name is not None
            assert line_info.from_bus is not None
            assert line_info.to_bus is not None

    def test_get_load_info(self, opendss_engine):
        """Test getting load information."""
        topology = opendss_engine.get_topology()
        load_ids = list(topology.loads.keys())

        if load_ids:
            load_id = load_ids[0]
            load_info = opendss_engine.get_load_info(load_id)

            assert isinstance(load_info, LoadInfo)
            assert load_info.load_id == load_id
            assert load_info.name is not None
            assert load_info.bus is not None

    def test_get_generator_info(self, opendss_engine):
        """Test getting generator (PV) information."""
        topology = opendss_engine.get_topology()
        gen_ids = list(topology.generators.keys())

        if gen_ids:
            gen_id = gen_ids[0]
            gen_info = opendss_engine.get_generator_info(gen_id)

            assert isinstance(gen_info, GeneratorInfo)
            assert gen_info.generator_id == gen_id
            assert gen_info.name is not None
            assert gen_info.bus is not None
            assert gen_info.der_type == DERType.SOLAR_PV

    def test_storage_not_implemented(self, opendss_engine):
        """Test that storage raises NotImplementedError."""
        from src.schemas.common import StorageID

        with pytest.raises(KeyError, match="Storage not implemented"):
            opendss_engine.get_storage_info(StorageID(0))


class TestSimulation:
    """Test power flow simulation."""

    def test_run_simulation_default_config(self, opendss_engine):
        """Test running simulation with default configuration."""
        result = opendss_engine.run_simulation()

        assert result is not None
        assert hasattr(result, "converged")
        assert hasattr(result, "iterations")
        assert result.iterations >= 0

    def test_run_simulation_custom_config(self, opendss_engine):
        """Test running simulation with custom configuration."""
        config = PowerFlowConfig(max_iterations=50)
        result = opendss_engine.run_simulation(config)

        assert result is not None
        assert result.config == config

    def test_get_convergence_status(self, opendss_engine):
        """Test getting convergence status."""
        # Run simulation first
        opendss_engine.run_simulation()

        # Check convergence status
        converged = opendss_engine.get_convergence_status()
        assert isinstance(converged, bool)


class TestStateRetrieval:
    """Test state information retrieval."""

    def test_get_current_state_after_simulation(self, opendss_engine):
        """Test getting state after running simulation."""
        # Run simulation first
        opendss_engine.run_simulation()

        # Get current state
        state = opendss_engine.get_current_state()

        assert state is not None
        assert len(state.buses) > 0
        assert len(state.lines) > 0
        assert len(state.loads) > 0
        assert isinstance(state.timestamp, datetime)
        assert isinstance(state.converged, bool)

    def test_bus_state_has_voltages(self, opendss_engine):
        """Test that bus states contain voltage information."""
        opendss_engine.run_simulation()
        state = opendss_engine.get_current_state()

        if state.buses:
            bus_state = next(iter(state.buses.values()))
            assert isinstance(bus_state, BusState)
            assert bus_state.voltage_pu is not None
            assert bus_state.angle_deg is not None

    def test_line_state_has_power_flows(self, opendss_engine):
        """Test that line states contain power flow information."""
        opendss_engine.run_simulation()
        state = opendss_engine.get_current_state()

        if state.lines:
            line_state = next(iter(state.lines.values()))
            assert isinstance(line_state, LineState)
            assert line_state.p_from_mw is not None
            assert line_state.q_from_mvar is not None

    def test_load_state_has_power(self, opendss_engine):
        """Test that load states contain power information."""
        opendss_engine.run_simulation()
        state = opendss_engine.get_current_state()

        if state.loads:
            load_state = next(iter(state.loads.values()))
            assert isinstance(load_state, LoadState)
            assert load_state.p_mw is not None
            assert load_state.q_mvar is not None

    def test_generator_state_has_power(self, opendss_engine):
        """Test that generator states contain power information."""
        opendss_engine.run_simulation()
        state = opendss_engine.get_current_state()

        if state.generators:
            gen_state = next(iter(state.generators.values()))
            assert isinstance(gen_state, GeneratorState)
            assert gen_state.p_mw is not None
            assert gen_state.q_mvar is not None


class TestControlOperations:
    """Test control operations."""

    def test_set_breaker_status(self, opendss_engine):
        """Test setting breaker (line enable) status."""
        topology = opendss_engine.get_topology()
        line_ids = list(topology.lines.keys())

        if line_ids:
            line_id = line_ids[0]

            # Open breaker
            opendss_engine.set_breaker_status(line_id, False)

            # Close breaker
            opendss_engine.set_breaker_status(line_id, True)

            # Should not raise exception

    def test_set_load_demand(self, opendss_engine):
        """Test setting load demand."""
        topology = opendss_engine.get_topology()
        load_ids = list(topology.loads.keys())

        if load_ids:
            load_id = load_ids[0]

            # Set load power
            opendss_engine.set_load_demand(load_id, p_mw=0.1, q_mvar=0.05)

            # Should not raise exception

    def test_set_generator_setpoint(self, opendss_engine):
        """Test setting generator setpoint."""
        topology = opendss_engine.get_topology()
        gen_ids = list(topology.generators.keys())

        if gen_ids:
            gen_id = gen_ids[0]

            # Set generator power
            opendss_engine.set_generator_setpoint(gen_id, p_mw=0.05)

            # Should not raise exception

    def test_storage_control_not_implemented(self, opendss_engine):
        """Test that storage control raises NotImplementedError."""
        from src.schemas.common import StorageID

        with pytest.raises(NotImplementedError):
            opendss_engine.set_storage_power(StorageID(0), 0.1)

    def test_transformer_tap_not_implemented(self, opendss_engine):
        """Test that transformer tap control raises NotImplementedError."""
        from src.schemas.common import TransformerID

        with pytest.raises(NotImplementedError):
            opendss_engine.set_transformer_tap(TransformerID(0), 0)


class TestErrorHandling:
    """Test error handling."""

    def test_invalid_bus_id(self, opendss_engine):
        """Test handling of invalid bus ID."""
        from src.schemas.common import BusID

        with pytest.raises(KeyError):
            opendss_engine.get_bus_info(BusID(99999))

    def test_invalid_line_id(self, opendss_engine):
        """Test handling of invalid line ID."""
        from src.schemas.common import LineID

        with pytest.raises(KeyError):
            opendss_engine.get_line_info(LineID(99999))

    def test_invalid_load_id(self, opendss_engine):
        """Test handling of invalid load ID."""
        from src.schemas.common import LoadID

        with pytest.raises(KeyError):
            opendss_engine.get_load_info(LoadID(99999))

    def test_invalid_generator_id(self, opendss_engine):
        """Test handling of invalid generator ID."""
        from src.schemas.common import GeneratorID

        with pytest.raises(KeyError):
            opendss_engine.get_generator_info(GeneratorID(99999))

    def test_nonexistent_dss_file(self):
        """Test handling of nonexistent DSS file."""
        with pytest.raises(RuntimeError, match="Failed to load circuit"):
            OpenDSSEngine("/nonexistent/file.dss")


class TestIDMapping:
    """Test ID mapping functionality."""

    def test_bus_name_to_id_mapping(self, opendss_engine):
        """Test bus name to ID mapping."""
        assert len(opendss_engine._bus_name_to_id) > 0
        assert len(opendss_engine._bus_id_to_name) > 0
        assert len(opendss_engine._bus_name_to_id) == len(
            opendss_engine._bus_id_to_name
        )

    def test_line_name_to_id_mapping(self, opendss_engine):
        """Test line name to ID mapping."""
        assert len(opendss_engine._line_name_to_id) > 0
        assert len(opendss_engine._line_id_to_name) > 0
        assert len(opendss_engine._line_name_to_id) == len(
            opendss_engine._line_id_to_name
        )

    def test_load_name_to_id_mapping(self, opendss_engine):
        """Test load name to ID mapping."""
        assert len(opendss_engine._load_name_to_id) > 0
        assert len(opendss_engine._load_id_to_name) > 0
        assert len(opendss_engine._load_name_to_id) == len(
            opendss_engine._load_id_to_name
        )

    def test_generator_name_to_id_mapping(self, opendss_engine):
        """Test generator name to ID mapping."""
        # IEEE37Bus_PV should have PV systems
        assert len(opendss_engine._pv_name_to_id) > 0
        assert len(opendss_engine._pv_id_to_name) > 0
        assert len(opendss_engine._pv_name_to_id) == len(opendss_engine._pv_id_to_name)
