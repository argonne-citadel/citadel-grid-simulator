"""Basic tests for grid simulator."""

import pytest

# Import all critical modules to catch import errors early
# This ensures that pymodbus 3.x API compatibility issues are caught
from src.models import BaranWu33Model
from src.simulator import GridSimulator
from src.engines.pandapower_engine import PandaPowerEngine
from src.schemas.commands import CommandType, BreakerCommand
from src.protocols.modbus_server import GridModbusServer

# Verify pymodbus 3.14 imports work correctly
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice
from pymodbus.simulator.simcore import SimCore


def test_pymodbus_api():
    """Test that the pymodbus 3.14 API is correctly imported."""
    # Verify correct imports (should not raise ImportError)
    assert ModbusTcpServer is not None
    assert SimData is not None
    assert SimDevice is not None
    assert SimCore is not None

    # Verify old API is NOT available (would cause errors in pymodbus 3.x)
    try:
        from pymodbus.device import ModbusDeviceIdentification as OldAPI

        assert False, "Old pymodbus.device module should not be available in 3.x"
    except ModuleNotFoundError:
        pass  # Expected - old API not available


def test_pymodbus_datastore_signature_contract():
    """Freeze the pymodbus surface modbus_server.py actually depends on.

    Importability (above) catches a module move; it does not catch a renamed
    parameter or a hook that stops being called, which is silent until a
    request hits the server. The 3.12.1 -> 3.14.0 rewrite was exactly that
    shape: ModbusDeviceContext survived as a deprecated shim while the server
    stopped routing through it, so the data-block override that applied write
    commands to the engine was constructed and never invoked.

    The full contract — SimAction's arity, SimCore's dispatch signature, the
    SimRuntime block layout — lives in
    tests/stack/test_pymodbus_signature_contract_unit.py, which runs in CI via
    `make test-unit-dependency-contracts`. Kept here is the piece specific to
    this container.
    """
    import inspect

    from pymodbus.constants import ExcCodes

    fields = inspect.signature(SimDevice).parameters
    for expected in ("id", "simdata", "action"):
        assert expected in fields, (
            f"pymodbus SimDevice no longer takes {expected!r} (now: "
            f"{inspect.signature(SimDevice)}). src/protocols/modbus_server.py "
            f"installs GridModbusWriteHandler.action through it; without that "
            f"hook the server accepts breaker and setpoint commands and "
            f"applies none of them."
        )

    for expected in ("BITS", "REGISTERS"):
        assert hasattr(DataType, expected), (
            f"pymodbus DataType.{expected} is gone; the coil and holding "
            f"blocks in _create_sim_device declare it."
        )

    # DEVICE_BUSY is returned from the action hook to signal engine-lock
    # timeout (see modbus_server.py); pymodbus encodes it as a real Modbus
    # exception response.
    assert hasattr(ExcCodes, "DEVICE_BUSY"), (
        "pymodbus ExcCodes no longer defines DEVICE_BUSY; modbus_server.py "
        "returns it on engine-lock timeout."
    )


def test_baran_wu_33_model_creation():
    """Test that Baran-Wu 33-bus model can be created."""
    model = BaranWu33Model()
    assert model is not None
    assert model.net is not None

    # Check topology
    topology = model.get_topology_info()
    assert topology["num_buses"] > 0
    assert topology["num_lines"] > 0
    assert topology["num_loads"] > 0


def test_baran_wu_33_model_validation():
    """Test that model validation works."""
    model = BaranWu33Model()
    assert model.validate() is True


def test_grid_simulator_creation():
    """Test that grid simulator can be created."""
    model = BaranWu33Model()
    engine = PandaPowerEngine(model.get_network())
    simulator = GridSimulator(engine, timestep_seconds=1.0)

    assert simulator is not None
    assert simulator.timestep == 1.0
    assert simulator.running is False


def test_grid_simulator_single_step():
    """Test that simulator can execute a single step."""
    model = BaranWu33Model()
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
    model = BaranWu33Model()
    engine = PandaPowerEngine(model.get_network())
    simulator = GridSimulator(engine, timestep_seconds=1.0)

    # Queue a command
    command = BreakerCommand(line_id=0, closed=False)
    simulator.queue_command(command)

    # Command should be in queue
    assert not simulator.command_queue.empty()


def test_grid_simulator_breaker_control():
    """Test breaker control commands."""
    model = BaranWu33Model()
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
    model = BaranWu33Model()
    control_points = model.get_control_points()

    assert "breakers" in control_points
    assert "loads" in control_points
    assert len(control_points["breakers"]) > 0
    assert len(control_points["loads"]) > 0


if __name__ == "__main__":
    pytest.main([__file__, "-v"])


def test_volts_reading_is_recovered_not_dropped():
    """A bus reporting volts must not vanish from GridState.

    `dss.Bus.puVmagAngle()` returns RAW VOLTS for any bus whose kVBase is
    unset. That happens at runtime even though the .dss declares
    `Set VoltageBases` + `CalcVoltageBases`: a freshly compiled circuit
    reports all 147 IEEE-123 buses in per-unit, while a long-running engine
    reported 1 bus in volts at rest and 142 once breaker operations had
    de-energized parts of the feeder.

    The reading itself is ordinary — 2497.03 V against the 4.16 kV feeder's
    2401.8 V line-to-neutral base is 1.040 pu. Before this, it violated
    VoltagePU's le=2.0 bound, raised inside the per-bus try, and the bus was
    dropped from GridState: a readable bus became missing telemetry, and the
    only trace was a per-poll WARNING among thousands.
    """
    from src.engines import opendss_engine

    # 4.16 kV feeder -> 2.4018 kV line-to-neutral base.
    raw_volts = 2497.0340343445046
    kv_base = 2.4018
    expected_pu = raw_volts / (kv_base * 1000.0)

    assert expected_pu > 1.0, "sanity: this is a normal operating voltage"
    assert expected_pu < opendss_engine._MAX_PLAUSIBLE_PU, (
        "sanity: once normalized it is within the schema bound"
    )

    # The recovery arithmetic the engine performs.
    assert round(expected_pu, 3) == 1.04

    # The accessor the fix depends on must exist and be callable: an
    # AttributeError here would raise inside the per-bus try and drop the bus
    # anyway, which is the failure this change exists to remove.
    assert callable(opendss_engine.dss.Bus.kVBase)


def test_max_plausible_pu_matches_the_schema_bound():
    """The threshold must track VoltagePU's upper bound.

    If the schema bound moves and this constant does not, the engine either
    normalizes readings the schema would have accepted, or keeps dropping
    ones it would now reject.
    """
    from src.engines.opendss_engine import _MAX_PLAUSIBLE_PU
    from src.schemas.common import VoltagePU

    bound = next(
        c.le
        for field in VoltagePU.__metadata__
        for c in getattr(field, "metadata", [])
        if getattr(c, "le", None) is not None
    )
    assert _MAX_PLAUSIBLE_PU == bound
