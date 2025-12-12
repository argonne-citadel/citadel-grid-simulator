"""Tests for Modbus TCP server interface."""

import pytest
from src.models.dickert_lv import DickertLVModel
from src.simulator import GridSimulator
from src.engines.pandapower_engine import PandaPowerEngine
from src.schemas.commands import CommandType, BreakerCommand, GeneratorCommand
from src.protocols.modbus_server import GridModbusServer, GridModbusDataBlock


class TestModbusServer:
    """Test suite for Modbus TCP server."""

    @pytest.fixture
    def simulator(self):
        """Create a simulator instance for testing."""
        model = DickertLVModel()
        net = model.get_network()
        engine = PandaPowerEngine(net)
        sim = GridSimulator(engine, timestep_seconds=1.0)
        yield sim
        sim.stop()

    def test_server_initialization(self, simulator):
        """Test Modbus server can be initialized."""
        server = GridModbusServer(simulator.engine, host="127.0.0.1", port=5020)
        assert server.engine == simulator.engine
        assert server.host == "127.0.0.1"
        assert server.port == 5020
        assert server.context is None  # Not started yet

    def test_point_mapping_creation(self, simulator):
        """Test that point mapping is created correctly."""
        server = GridModbusServer(simulator.engine, host="127.0.0.1", port=5021)
        server._build_point_mapping()

        # Check that buses are mapped
        assert len(server.bus_to_hr_index) > 0

        # Check that lines are mapped
        assert len(server.line_to_hr_index) > 0
        assert len(server.line_to_coil_index) > 0

        # Check that loads are mapped
        assert len(server.load_to_hr_index) > 0

        # Verify mapping ranges
        for bus_id, hr_idx in server.bus_to_hr_index.items():
            assert 0 <= hr_idx < 1000, f"Bus HR index {hr_idx} out of range"

        for line_id, indices in server.line_to_hr_index.items():
            assert 1000 <= indices["p_from"] < 2000, "Line P HR out of range"
            assert 2000 <= indices["q_from"] < 3000, "Line Q HR out of range"
            assert 3000 <= indices["loading"] < 4000, "Line loading HR out of range"

        for line_id, coil_idx in server.line_to_coil_index.items():
            assert 0 <= coil_idx < 1000, f"Line coil index {coil_idx} out of range"

        for load_id, hr_idx in server.load_to_hr_index.items():
            assert 5000 <= hr_idx < 6000, f"Load HR index {hr_idx} out of range"

    def test_datastore_creation(self, simulator):
        """Test that Modbus datastore is created with correct sizes."""
        server = GridModbusServer(simulator.engine, host="127.0.0.1", port=5022)
        server._build_point_mapping()
        context = server._create_datastore()

        # Context should be created
        assert context is not None

        # Should have slave context for unit ID 1
        assert 1 in context

    def test_get_point_mapping(self, simulator):
        """Test getting point mapping information."""
        server = GridModbusServer(simulator.engine, host="127.0.0.1", port=5023)
        server._build_point_mapping()

        mapping = server.get_point_mapping()

        assert "buses" in mapping
        assert "lines" in mapping
        assert "loads" in mapping
        assert "holding_registers" in mapping["lines"]
        assert "coils" in mapping["lines"]

    def test_update_measurements_without_context(self, simulator):
        """Test that update_measurements handles not-started state gracefully."""
        server = GridModbusServer(simulator.engine, host="127.0.0.1", port=5024)

        # Should not crash when context not initialized
        # update_measurements() takes no arguments, it reads from engine
        server.update_measurements()

    def test_callback_registration(self, simulator):
        """Test that server can register as a callback."""
        server = GridModbusServer(simulator.engine, host="127.0.0.1", port=5025)

        initial_callbacks = len(simulator.state_update_callbacks)
        simulator.add_state_callback(server.update_measurements)

        assert len(simulator.state_update_callbacks) == initial_callbacks + 1
        assert server.update_measurements in simulator.state_update_callbacks


class TestModbusDataBlock:
    """Test suite for custom Modbus data block."""

    @pytest.fixture
    def simulator(self):
        """Create a simulator instance for testing."""
        model = DickertLVModel()
        net = model.get_network()
        engine = PandaPowerEngine(net)
        sim = GridSimulator(engine, timestep_seconds=1.0)
        yield sim
        sim.stop()

    def test_data_block_initialization(self, simulator):
        """Test data block can be initialized."""
        server = GridModbusServer(simulator.engine, host="127.0.0.1", port=5030)
        block = GridModbusDataBlock(0, [0] * 100, server)
        assert block.server == server


class TestModbusIntegration:
    """Integration tests for Modbus with simulator."""

    @pytest.fixture
    def simulator_with_modbus(self):
        """Create simulator with Modbus server."""
        model = DickertLVModel()
        net = model.get_network()
        engine = PandaPowerEngine(net)
        sim = GridSimulator(engine, timestep_seconds=1.0)

        # Note: We don't actually start the Modbus server in tests
        # to avoid binding to network ports during testing
        server = GridModbusServer(sim.engine, host="127.0.0.1", port=5026)
        server._build_point_mapping()

        yield sim, server

        sim.stop()

    def test_simulator_state_capture(self, simulator_with_modbus):
        """Test that simulator captures state correctly for Modbus."""
        sim, server = simulator_with_modbus

        # Run one simulation step
        sim.step()

        # Get current state
        state = sim.get_current_state()

        # Verify state has expected attributes
        assert hasattr(state, "buses")
        assert hasattr(state, "lines")
        assert hasattr(state, "converged")
        assert state.converged is True

        # Verify we can map buses to Modbus registers
        for bus_id in state.buses.keys():
            assert bus_id in server.bus_to_hr_index

        # Verify we can map lines to Modbus registers
        for line_id in state.lines.keys():
            assert line_id in server.line_to_coil_index

    def test_value_scaling(self, simulator_with_modbus):
        """Test that Modbus server can update datastore from engine state."""
        sim, server = simulator_with_modbus

        # Create datastore
        server.context = server._create_datastore()

        # Run simulation to generate state
        sim.step()

        # Update measurements from engine
        server.update_measurements()

        # Verify datastore was updated
        slave_context = server.context[1]

        # Check that at least some values were written
        # (exact values depend on simulation results)
        if 0 in server.bus_to_hr_index:
            hr_idx = server.bus_to_hr_index[0]
            values = slave_context.getValues(3, hr_idx, 1)
            # Should have a non-zero voltage value (scaled by 1000)
            assert values[0] > 0, "Bus voltage should be updated in datastore"


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
