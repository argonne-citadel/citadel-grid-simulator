"""
Tests for GridSimulator lifecycle and edge cases.

Tests simulator start/stop, state callbacks, BESS control,
and other lifecycle-related functionality.
"""

import pytest
import time
import threading
from queue import Queue

from src.models.dickert_lv import DickertLVModel
from src.simulator import GridSimulator
from src.engines.pandapower_engine import PandaPowerEngine
from src.schemas.commands import BreakerCommand, StorageCommand


class TestSimulatorLifecycle:
    """Test simulator lifecycle operations."""

    @pytest.fixture
    def simulator(self):
        """Create a simulator instance for testing."""
        model = DickertLVModel(feeders_range="short", linetype="cable")
        engine = PandaPowerEngine(model.get_network())
        sim = GridSimulator(engine, timestep_seconds=0.1)
        yield sim
        if sim.running:
            sim.stop()

    def test_simulator_start(self, simulator):
        """Test starting the simulator."""
        assert simulator.running is False

        simulator.start()

        assert simulator.running is True
        assert simulator._sim_thread is not None
        assert simulator._sim_thread.is_alive()

        simulator.stop()

    def test_simulator_stop(self, simulator):
        """Test stopping the simulator."""
        simulator.start()
        assert simulator.running is True

        simulator.stop()

        assert simulator.running is False
        # Give thread time to finish
        time.sleep(0.2)
        if simulator._sim_thread:
            assert not simulator._sim_thread.is_alive()

    def test_simulator_restart(self, simulator):
        """Test restarting the simulator."""
        # Start
        simulator.start()
        assert simulator.running is True

        # Stop
        simulator.stop()
        time.sleep(0.2)
        assert simulator.running is False

        # Restart
        simulator.start()
        assert simulator.running is True

        simulator.stop()

    def test_simulator_double_start(self, simulator):
        """Test that starting an already running simulator is handled."""
        simulator.start()
        assert simulator.running is True

        # Try to start again - should be handled gracefully
        simulator.start()
        assert simulator.running is True

        simulator.stop()

    def test_simulator_double_stop(self, simulator):
        """Test that stopping an already stopped simulator is handled."""
        simulator.start()
        simulator.stop()
        time.sleep(0.2)

        # Try to stop again - should be handled gracefully
        simulator.stop()
        assert simulator.running is False


class TestStateCallbacks:
    """Test state update callbacks."""

    @pytest.fixture
    def simulator(self):
        """Create a simulator instance for testing."""
        model = DickertLVModel(feeders_range="short", linetype="cable")
        engine = PandaPowerEngine(model.get_network())
        sim = GridSimulator(engine, timestep_seconds=1.0)
        yield sim
        sim.stop()

    def test_add_state_callback(self, simulator):
        """Test adding a state callback."""
        callback_called = []

        def callback(state):
            callback_called.append(True)

        simulator.add_state_callback(callback)
        assert callback in simulator.state_update_callbacks

        # Execute a step to trigger callback
        simulator.step()

        assert len(callback_called) > 0

    def test_multiple_callbacks(self, simulator):
        """Test multiple state callbacks."""
        callback1_called = []
        callback2_called = []

        def callback1(state):
            callback1_called.append(True)

        def callback2(state):
            callback2_called.append(True)

        simulator.add_state_callback(callback1)
        simulator.add_state_callback(callback2)

        simulator.step()

        assert len(callback1_called) > 0
        assert len(callback2_called) > 0

    def test_callback_exception_handling(self, simulator):
        """Test that callback exceptions don't crash the simulator."""

        def bad_callback(state):
            raise ValueError("Callback error")

        def good_callback(state):
            pass

        simulator.add_state_callback(bad_callback)
        simulator.add_state_callback(good_callback)

        # Should not raise exception
        simulator.step()

        # Simulator should still be functional
        state = simulator.get_current_state()
        assert state is not None


class TestCommandQueue:
    """Test command queue functionality."""

    @pytest.fixture
    def simulator(self):
        """Create a simulator instance for testing."""
        model = DickertLVModel(feeders_range="short", linetype="cable")
        engine = PandaPowerEngine(model.get_network())
        sim = GridSimulator(engine, timestep_seconds=1.0)
        yield sim
        sim.stop()

    def test_queue_multiple_commands(self, simulator):
        """Test queuing multiple commands."""
        cmd1 = BreakerCommand(line_id=0, closed=False)
        cmd2 = BreakerCommand(line_id=1, closed=False)

        simulator.queue_command(cmd1)
        simulator.queue_command(cmd2)

        assert simulator.command_queue.qsize() == 2

    def test_commands_processed_in_order(self, simulator):
        """Test that commands are processed in FIFO order."""
        if len(simulator.engine.net.line) < 2:
            pytest.skip("Need at least 2 lines for this test")

        line_id_0 = simulator.engine.net.line.index[0]
        line_id_1 = simulator.engine.net.line.index[1]

        # Queue commands
        cmd1 = BreakerCommand(line_id=line_id_0, closed=False)
        cmd2 = BreakerCommand(line_id=line_id_1, closed=False)

        simulator.queue_command(cmd1)
        simulator.queue_command(cmd2)

        # Process commands
        simulator.step()

        # Both should be processed
        assert simulator.engine.net.line.at[line_id_0, "in_service"] == False
        assert simulator.engine.net.line.at[line_id_1, "in_service"] == False

    def test_invalid_command_handling(self, simulator):
        """Test handling of invalid commands."""
        # Command with invalid line ID
        cmd = BreakerCommand(line_id=9999, closed=False)
        simulator.queue_command(cmd)

        # Should not crash
        simulator.step()

        # Simulator should still be functional
        state = simulator.get_current_state()
        assert state is not None


class TestBESSControl:
    """Test BESS control logic."""

    @pytest.fixture
    def simulator_with_storage(self):
        """Create a simulator with storage."""
        model = DickertLVModel(feeders_range="short", linetype="cable")
        net = model.get_network()

        # Add storage if not present
        import pandapower as pp

        if len(net.storage) == 0:
            pp.create_storage(
                net, bus=1, p_mw=0.0, max_e_mwh=0.1, name="Test BESS", soc_percent=50.0
            )

        engine = PandaPowerEngine(net)
        sim = GridSimulator(engine, timestep_seconds=1.0)
        yield sim
        sim.stop()

    def test_bess_charge_command(self, simulator_with_storage):
        """Test BESS charging command."""
        storage_id = simulator_with_storage.engine.net.storage.index[0]

        # Command to charge (negative power)
        cmd = StorageCommand(storage_id=storage_id, p_mw=-0.02)
        simulator_with_storage.queue_command(cmd)

        simulator_with_storage.step()

        # Verify charging
        assert simulator_with_storage.engine.net.storage.at[storage_id, "p_mw"] == -0.02

    def test_bess_discharge_command(self, simulator_with_storage):
        """Test BESS discharging command."""
        storage_id = simulator_with_storage.engine.net.storage.index[0]

        # Command to discharge (positive power) - use smaller value within capacity
        cmd = StorageCommand(storage_id=storage_id, p_mw=0.02)
        simulator_with_storage.queue_command(cmd)

        simulator_with_storage.step()

        # Verify discharging
        assert simulator_with_storage.engine.net.storage.at[storage_id, "p_mw"] == 0.02


class TestSimulatorStatistics:
    """Test simulator statistics tracking."""

    @pytest.fixture
    def simulator(self):
        """Create a simulator instance for testing."""
        model = DickertLVModel(feeders_range="short", linetype="cable")
        engine = PandaPowerEngine(model.get_network())
        sim = GridSimulator(engine, timestep_seconds=1.0)
        yield sim
        sim.stop()

    def test_step_count(self, simulator):
        """Test that step count is tracked."""
        initial_steps = simulator.stats["total_steps"]

        simulator.step()
        simulator.step()
        simulator.step()

        assert simulator.stats["total_steps"] == initial_steps + 3

    def test_get_statistics(self, simulator):
        """Test getting simulator statistics."""
        simulator.step()

        stats = simulator.get_statistics()

        assert stats is not None
        assert "total_steps" in stats
        assert "failed_steps" in stats
        assert "commands_processed" in stats
        assert stats["total_steps"] > 0


class TestSimulatorIntegration:
    """Integration tests for simulator."""

    def test_full_simulation_cycle(self):
        """Test a complete simulation cycle."""
        # Create model and simulator
        model = DickertLVModel(feeders_range="short", linetype="cable")
        engine = PandaPowerEngine(model.get_network())
        simulator = GridSimulator(engine, timestep_seconds=0.1)

        # Add callback to track states
        states_captured = []

        def capture_state(state):
            states_captured.append(state)

        simulator.add_state_callback(capture_state)

        # Run simulation
        simulator.start()
        time.sleep(0.5)  # Run for 0.5 seconds
        simulator.stop()

        # Verify states were captured
        assert len(states_captured) > 0

        # Verify all states converged
        for state in states_captured:
            assert state.converged is True

    def test_simulation_with_commands(self):
        """Test simulation with dynamic commands."""
        model = DickertLVModel(feeders_range="short", linetype="cable")
        engine = PandaPowerEngine(model.get_network())
        simulator = GridSimulator(engine, timestep_seconds=1.0)

        # Add storage for testing commands
        import pandapower as pp

        if len(engine.net.storage) == 0:
            pp.create_storage(
                engine.net,
                bus=1,
                p_mw=0.0,
                max_e_mwh=0.1,
                name="Test BESS",
                soc_percent=50.0,
            )

        # Queue storage command (won't cause convergence issues)
        if len(engine.net.storage) > 0:
            storage_id = engine.net.storage.index[0]
            simulator.queue_command(StorageCommand(storage_id=storage_id, p_mw=-0.01))

        # Run steps
        for _ in range(5):
            simulator.step()

        # Verify simulation completed
        assert simulator.stats["total_steps"] >= 5

        simulator.stop()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
