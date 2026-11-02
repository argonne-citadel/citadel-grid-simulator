"""Regression test: POST /reset must hold the engine lock across reset().

OpenDSS is a global C library with no thread safety. `GridSimulator.engine_lock`
is the documented serialization point for ALL engine access; the simulation
thread holds it during `step()` and Modbus write handlers acquire it before
calling `set_breaker_status` / `set_generator_setpoint` (`main.py` passes the
same lock object into the Modbus server, so they are not two locks).

`/reset` did not take it. It stopped the simulation loop instead, which is not
equivalent for two reasons: `stop()` joins with `timeout=5.0` and returns
regardless, so a thread mid-`Solve()` on IEEE-8500 can outlive it; and Modbus
handlers are never stopped at all, running for the whole episode.

The observed consequence was a partially-built circuit, not a segfault. Corpus
job 22702 reset IEEE-123 into `num_buses=0, num_elements=64` with OpenDSS
reporting no error, caught only by the post-compile non-empty assertion added
for Redmine #1294; the same defect on IEEE-8500 stopped job 22693. Before that
assertion existed, episodes continued against the wreckage and produced
power-flow numbers indistinguishable from real ones.

This pins the lock rather than the symptom. Asserting "reset produces a
non-empty circuit" would need a real engine and would still pass whenever the
race happened not to fire — the defect was intermittent across 45 episodes.
The property that has to hold is that the lock is HELD while reset runs.
"""

import threading
from unittest.mock import MagicMock

import pytest
from flask import Flask

from src.web_api import GridVisualizationAPI


@pytest.fixture
def held_flags() -> dict:
    return {"held_during_reset": None, "reset_called": False}


@pytest.fixture
def api(held_flags: dict) -> Flask:
    """A GridVisualizationAPI whose engine records lock state during reset()."""
    simulator = MagicMock()
    lock = threading.RLock()
    simulator.engine_lock = lock
    simulator.running = False

    def _reset() -> None:
        held_flags["reset_called"] = True
        # acquire(blocking=False) from this same thread succeeds for an RLock
        # whether or not it is held, so re-entrancy cannot be used to probe
        # ownership. Read the owner directly instead.
        held_flags["held_during_reset"] = lock._is_owned()  # type: ignore[attr-defined]

    simulator.engine.reset.side_effect = _reset
    simulator.engine._pinned_hour = 12

    return GridVisualizationAPI(simulator)


def test_reset_holds_the_engine_lock_while_resetting(
    api: GridVisualizationAPI, held_flags: dict
) -> None:
    """The lock must be owned at the moment engine.reset() runs."""
    client = api.app.test_client()
    response = client.post("/reset")

    assert response.status_code == 200, response.get_data(as_text=True)
    assert held_flags["reset_called"], "engine.reset() was never called"
    assert held_flags["held_during_reset"] is True, (
        "engine.reset() ran WITHOUT the engine lock held. OpenDSS Clear+Compile "
        "can then interleave with Modbus write handlers, which produced a "
        "partially-built circuit (num_buses=0, num_elements=64) in corpus job "
        "22702."
    )


def test_reset_releases_the_engine_lock_afterwards(api: GridVisualizationAPI) -> None:
    """A held-forever lock would deadlock the next Modbus write, not crash.

    Worth pinning separately: the failure mode of over-holding is a hang at the
    Modbus handler's acquire timeout, which looks like an unresponsive
    simulator rather than a lock bug.
    """
    client = api.app.test_client()
    client.post("/reset")

    lock = api.simulator.engine_lock
    assert lock.acquire(blocking=False), "engine lock still held after /reset"
    lock.release()


def test_reset_releases_the_engine_lock_even_when_reset_raises(
    api: GridVisualizationAPI,
) -> None:
    """An engine failure must not strand the lock.

    This is the path that matters in practice: #1294 made a bad compile RAISE
    where it previously returned silently, so the raising path is now reachable
    on every episode reset rather than being hypothetical.
    """
    api.simulator.engine.reset.side_effect = RuntimeError("compile failed")
    client = api.app.test_client()
    response = client.post("/reset")

    assert response.status_code == 500
    lock = api.simulator.engine_lock
    assert lock.acquire(blocking=False), (
        "engine lock stranded after a failing reset; every subsequent Modbus "
        "write would block until its acquire timeout"
    )
    lock.release()
