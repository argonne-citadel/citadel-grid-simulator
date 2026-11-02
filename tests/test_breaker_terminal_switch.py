"""Regression tests: a breaker is a terminal switch, and a wedged sim loop fails loudly.

Disabling the only element at a leaf bus used to delete the bus from OpenDSS;
re-enabling it did not bring the bus back, so every later bus read raised and
``GridSimulator.step`` skipped publishing for as long as the line stayed closed.
The tracked IEEE-37 feeder plus a temporary unloaded leaf line reproduces the
IEEE-123 ``l99``/``sw7`` case without Docker-provisioned data.
"""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from src.engines.opendss_engine import OpenDSSEngine, dss
from src.protocols.modbus_server import GridModbusServer, StrictDeviceSimCore
from src.simulator import MAX_CONSECUTIVE_FAILED_STEPS, GridSimulator

pytestmark = pytest.mark.unit

_FEEDER = Path(__file__).parent.parent / "examples" / "IEEE37Bus_PV.dss"
_LEAF_BUS = "breaker_test_leaf"
_LEAF_LINE = "breaker_test_line"


@pytest.fixture
def leaf_engine(tmp_path: Path) -> OpenDSSEngine:
    """Build one real engine; OpenDSS circuit state is process-global."""
    wrapper = tmp_path / "ieee37_with_unloaded_leaf.dss"
    wrapper.write_text(
        f"Compile [{_FEEDER}]\n"
        f"New Line.{_LEAF_LINE} phases=3 bus1=701.1.2.3 "
        f"bus2={_LEAF_BUS}.1.2.3 r1=0.1 x1=0.1 r0=0.2 x0=0.2 "
        "c1=0 c0=0 length=0.01 units=km\n"
        "CalcVoltageBases\n",
        encoding="utf-8",
    )
    engine = OpenDSSEngine(str(wrapper), pinned_hour=12, fuzz_seed=42)
    engine.reset()
    return engine


def _step_n(
    sim: GridSimulator, published: list[Any], n: int
) -> list[tuple[float, bool]]:
    """Step n times and return (leaf voltage, line in_service) per step."""
    engine = sim.engine
    assert isinstance(engine, OpenDSSEngine)
    leaf_id = engine._bus_name_to_id[_LEAF_BUS]
    line_id = engine._line_name_to_id[_LEAF_LINE]
    seen = []
    for _ in range(n):
        before = len(published)
        sim.step()
        assert len(published) == before + 1, "step did not publish"
        seen.append(
            (
                published[-1].buses[leaf_id].voltage_pu,
                engine.get_line_info(line_id).in_service,
            )
        )
    return seen


def test_open_then_close_leaf_breaker_keeps_publishing(
    leaf_engine: OpenDSSEngine,
) -> None:
    """Open and reclose an unloaded leaf line through GridSimulator.step()."""
    engine = leaf_engine
    line_id = engine._line_name_to_id[_LEAF_LINE]
    leaf_id = engine._bus_name_to_id[_LEAF_BUS]
    sim = GridSimulator(engine, timestep_seconds=1.0)
    published: list[Any] = []
    sim.add_state_callback(published.append)

    closed_before = _step_n(sim, published, 3)
    engine.set_breaker_status(line_id, False)
    opened = _step_n(sim, published, 3)
    assert _LEAF_BUS in dss.Circuit.AllBusNames()
    assert leaf_id in published[-1].islanded_buses
    engine.set_breaker_status(line_id, True)
    reclosed = _step_n(sim, published, 3)

    assert sim.stats["failed_steps"] == 0
    assert len(published) == 9
    assert all(v > 0.9 and ins for v, ins in closed_before)
    assert all(v == 0.0 and not ins for v, ins in opened)
    assert all(0.9 < v < 1.1 and ins for v, ins in reclosed)
    assert leaf_id not in published[-1].islanded_buses


def test_reset_restores_open_breaker(leaf_engine: OpenDSSEngine) -> None:
    """reset() leaves every line closed."""
    line_id = leaf_engine._line_name_to_id[_LEAF_LINE]
    leaf_engine.set_breaker_status(line_id, False)
    assert not leaf_engine.get_line_info(line_id).in_service

    leaf_engine.reset()

    assert leaf_engine.get_line_info(line_id).in_service
    leaf_id = leaf_engine._bus_name_to_id[_LEAF_BUS]
    assert leaf_engine.get_current_state().buses[leaf_id].voltage_pu > 0.9


def _status_coil_reads_closed(
    server: GridModbusServer, core: StrictDeviceSimCore, line_id: int
) -> bool:
    coil_index = server.line_to_coil_index[line_id]
    (value,) = asyncio.run(core.async_getValues(1, 1, coil_index, 1))
    return bool(value)


def test_status_coil_follows_live_breaker_state(leaf_engine: OpenDSSEngine) -> None:
    """The status coil reports the live breaker, not the startup topology."""
    line_id = leaf_engine._line_name_to_id[_LEAF_LINE]
    sim = GridSimulator(leaf_engine, timestep_seconds=1.0)
    server = GridModbusServer(leaf_engine, host="127.0.0.1", port=5027)
    server._build_point_mapping()
    core = StrictDeviceSimCore(server._create_sim_device())
    server.context = core

    sim.step()
    server.update_measurements()
    assert _status_coil_reads_closed(server, core, line_id)

    leaf_engine.set_breaker_status(line_id, False)
    sim.step()
    server.update_measurements()
    assert not _status_coil_reads_closed(server, core, line_id)
    assert leaf_engine.open_line_ids() == frozenset({line_id})

    leaf_engine.set_breaker_status(line_id, True)
    sim.step()
    server.update_measurements()
    assert _status_coil_reads_closed(server, core, line_id)
    assert leaf_engine.open_line_ids() == frozenset()

    leaf_engine.set_breaker_status(line_id, False)
    leaf_engine.reset()
    server.update_measurements()
    assert leaf_engine.open_line_ids() == frozenset()
    assert all(
        _status_coil_reads_closed(server, core, lid)
        for lid in server.line_to_coil_index
    )


def test_absent_bus_raises(
    leaf_engine: OpenDSSEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A bus missing from the live list is a contract violation, not zero volts."""
    live = dss.Circuit.AllBusNames()
    monkeypatch.setattr(
        type(dss.Circuit),
        "AllBusNames",
        lambda _circuit: [n for n in live if n != _LEAF_BUS],
    )
    with pytest.raises(RuntimeError, match="absent from the OpenDSS bus list"):
        leaf_engine.get_current_state()


def _fail_get_state_n_times(
    engine: OpenDSSEngine, monkeypatch: pytest.MonkeyPatch, failures: int
) -> None:
    real = engine.get_current_state
    remaining = [failures]

    def flaky() -> Any:
        if remaining[0] > 0:
            remaining[0] -= 1
            raise RuntimeError("synthetic step failure")
        return real()

    monkeypatch.setattr(engine, "get_current_state", flaky)


def test_consecutive_failures_halt_and_flip_health(
    leaf_engine: OpenDSSEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """N consecutive failed steps stop the simulator and /health reports 503."""
    from src.web_api import GridVisualizationAPI

    _fail_get_state_n_times(leaf_engine, monkeypatch, 10**6)
    sim = GridSimulator(leaf_engine, timestep_seconds=1.0)
    sim.running = True
    client = GridVisualizationAPI(sim).app.test_client()

    for _ in range(MAX_CONSECUTIVE_FAILED_STEPS - 1):
        sim.step()
    assert sim.running
    assert client.get("/health").status_code == 200

    sim.step()

    assert not sim.running
    assert sim.halted_reason is not None
    assert "synthetic step failure" in sim.halted_reason
    response = client.get("/health")
    assert response.status_code == 503
    assert response.get_json()["status"] == "unhealthy"


def test_single_failure_then_success_does_not_halt(
    leaf_engine: OpenDSSEngine, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A transient failure resets the consecutive counter."""
    _fail_get_state_n_times(leaf_engine, monkeypatch, 1)
    sim = GridSimulator(leaf_engine, timestep_seconds=1.0)
    sim.running = True

    sim.step()
    assert sim.consecutive_failed_steps == 1
    for _ in range(MAX_CONSECUTIVE_FAILED_STEPS + 1):
        sim.step()

    assert sim.running
    assert sim.halted_reason is None
    assert sim.consecutive_failed_steps == 0
    assert sim.stats["failed_steps"] == 1
