"""Basic tests for grid simulator."""

import pytest
import sys
from pathlib import Path

# Import all critical modules to catch import errors early
# This ensures that pymodbus 3.x API compatibility issues are caught
from src.models import DickertLVModel
from src.simulator import GridSimulator
from src.engines.pandapower_engine import PandaPowerEngine
from src.schemas.commands import CommandType, BreakerCommand
from src.protocols.modbus_server import GridModbusServer

# Verify pymodbus 3.x imports work correctly
from pymodbus.server import StartAsyncTcpServer
from pymodbus.datastore import (
    ModbusSequentialDataBlock,
    ModbusServerContext,
    ModbusDeviceContext,
)


def test_pymodbus_api():
    """Test that pymodbus 3.x API is correctly imported."""
    # Verify correct imports (should not raise ImportError)
    assert StartAsyncTcpServer is not None
    assert ModbusDeviceContext is not None
    assert ModbusServerContext is not None

    # Verify old API is NOT available (would cause errors in pymodbus 3.x)
    try:
        from pymodbus.device import ModbusDeviceIdentification as OldAPI

        assert False, "Old pymodbus.device module should not be available in 3.x"
    except ModuleNotFoundError:
        pass  # Expected - old API not available


def test_dickert_lv_model_creation():
    """Test that Dickert LV model can be created."""
    model = DickertLVModel(feeders_range="short", linetype="cable")
    assert model is not None
    assert model.net is not None

    # Check topology
    topology = model.get_topology_info()
    assert topology["num_buses"] > 0
    assert topology["num_lines"] > 0
    assert topology["num_loads"] > 0


def test_dickert_lv_model_validation():
    """Test that model validation works."""
    model = DickertLVModel(feeders_range="short", linetype="cable")
    assert model.validate() is True


def test_grid_simulator_creation():
    """Test that grid simulator can be created."""
    model = DickertLVModel(feeders_range="short", linetype="cable")
    engine = PandaPowerEngine(model.get_network())
    simulator = GridSimulator(engine, timestep_seconds=1.0)

    assert simulator is not None
    assert simulator.timestep == 1.0
    assert simulator.running is False


def test_grid_simulator_single_step():
    """Test that simulator can execute a single step."""
    model = DickertLVModel(feeders_range="short", linetype="cable")
    engine = PandaPowerEngine(model.get_network())
    simulator = GridSimulator(engine, timestep_seconds=1.0)

    # Execute one step
    simulator.step()

    # Check that state was captured
    state = simulator.get_current_state()
    assert state is not None
    assert hasattr(state, "timestamp")
    assert hasattr(state, "converged")
    assert state.converged is True


def test_grid_simulator_command_queue():
    """Test that commands can be queued."""
    model = DickertLVModel(feeders_range="short", linetype="cable")
    engine = PandaPowerEngine(model.get_network())
    simulator = GridSimulator(engine, timestep_seconds=1.0)

    # Queue a command
    command = BreakerCommand(line_id=0, closed=False)
    simulator.queue_command(command)

    # Command should be in queue
    assert not simulator.command_queue.empty()


def test_grid_simulator_breaker_control():
    """Test breaker control commands."""
    model = DickertLVModel(feeders_range="short", linetype="cable")
    engine = PandaPowerEngine(model.get_network())
    simulator = GridSimulator(engine, timestep_seconds=1.0)

    # Get first line
    if len(engine.net.line) > 0:
        line_id = engine.net.line.index[0]

        # Initially should be in service
        initial_status = engine.net.line.at[line_id, "in_service"]
        assert initial_status == True

        # Open breaker
        command = BreakerCommand(line_id=line_id, closed=False)
        simulator.queue_command(command)
        simulator.step()

        # Should now be out of service
        final_status = engine.net.line.at[line_id, "in_service"]
        assert final_status == False


def test_control_points():
    """Test that control points are identified."""
    model = DickertLVModel(feeders_range="short", linetype="cable")
    control_points = model.get_control_points()

    assert "breakers" in control_points
    assert "loads" in control_points
    assert len(control_points["breakers"]) > 0
    assert len(control_points["loads"]) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
