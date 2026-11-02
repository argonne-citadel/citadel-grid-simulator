"""Tests for the OpenDSS engine's diurnal solution hour.

Pins the defect fixed in docs/decisions/opendss_diurnal_hour_from_sim_clock.md
(parent repo): the solution hour used to be frozen for an entire run
(`random.randint(6, 18)` in reset(), unseeded), so PV output never varied
within a run and reruns of the same seed landed on different operating
points. `solution_hour()` now derives the hour from the substrate's shared
sim-clock anchor when one is available, and falls back to the historical
fixed-hour behaviour (logged once) only when there is no simulated time to
track (standalone/UI/demo).

These tests monkeypatch `get_anchor` at its import site in
`src.engines.opendss_engine` rather than the sim_time module-level cache,
since that is the name `solution_hour()` actually calls.
"""

import logging
from datetime import datetime, timezone
from pathlib import Path

import opendssdirect as dss
import pytest

from src.engines import opendss_engine as opendss_engine_module
from src.engines.opendss_engine import OpenDSSEngine
from src.sim_time import ClockAnchor
from src.sim_time import compute_sim_now as real_compute_sim_now

TEST_DSS_FILE = Path(__file__).parent.parent / "examples" / "IEEE37Bus_PV.dss"


@pytest.fixture
def dss_file_path() -> str:
    if not TEST_DSS_FILE.exists():
        pytest.skip(f"Test DSS file not found: {TEST_DSS_FILE}")
    return str(TEST_DSS_FILE)


def _frozen_anchor(sim_dt: datetime) -> ClockAnchor:
    """An anchor whose sim_rate is negligible, so compute_sim_now(anchor) is
    sim_dt regardless of how much real wall-clock time elapses while the
    test runs — the same technique test_der_profiles_clock.py uses.
    """
    return ClockAnchor(
        sim_epoch=sim_dt, wall_start=datetime.now(timezone.utc), sim_rate=1e-9
    )


class TestSolutionHourTracksSimClock:
    """Acceptance item 3: the solved hour matches compute_sim_now(anchor).hour
    at several points across a simulated day."""

    @pytest.mark.parametrize("hour", [2, 8, 14, 20, 23])
    def test_solution_hour_matches_compute_sim_now(
        self, dss_file_path, monkeypatch, hour
    ):
        anchor = _frozen_anchor(datetime(2024, 6, 15, hour, 0, 0, tzinfo=timezone.utc))
        monkeypatch.setattr(opendss_engine_module, "get_anchor", lambda: anchor)

        engine = OpenDSSEngine(dss_file_path)

        assert engine.solution_hour() == real_compute_sim_now(anchor).hour
        assert engine.solution_hour() == hour


class TestSolutionHourFallback:
    """Acceptance item 4: no anchor means today's fixed-hour behaviour,
    logged once."""

    def test_no_anchor_uses_fixed_pinned_hour(self, dss_file_path, monkeypatch, caplog):
        monkeypatch.setattr(opendss_engine_module, "get_anchor", lambda: None)

        with caplog.at_level(logging.INFO):
            engine = OpenDSSEngine(dss_file_path)
            pinned_hour = engine._pinned_hour
            for _ in range(5):
                assert engine.solution_hour() == pinned_hour

        fallback_messages = [
            record.getMessage()
            for record in caplog.records
            if "falling back to fixed pinned hour" in record.getMessage()
        ]
        assert len(fallback_messages) == 1, (
            "fallback must log exactly once per engine instance, not once "
            f"per solve — got {len(fallback_messages)}: {fallback_messages}"
        )


class _HourRecordingSolution:
    """Wraps the real `dss.Solution` object, recording every explicit
    `Hour(value)` setter call while forwarding everything else (including
    the `Hour()` getter and `Solve()`) to the real object.

    A plain `mock.patch`/`mocker.spy` cannot sit on `dss.Solution.Hour`
    directly: OpenDSSDirect.py's API objects override `__setattr__` to
    reject attribute assignment ("use function calls instead of
    assignments"), which blocks both patching and un-patching. Wrapping the
    object at the `opendss_engine.dss` import site sidesteps that guard
    without touching the real opendssdirect object at all.
    """

    def __init__(self, real_solution: object) -> None:
        self._real_solution = real_solution
        self.hour_setter_calls: list[int] = []

    def Hour(self, *args: int) -> object:
        if args:
            self.hour_setter_calls.append(args[0])
        return self._real_solution.Hour(*args)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._real_solution, name)


class _HourRecordingDSS:
    """Forwards every attribute except `Solution` to the real `dss` module."""

    def __init__(self, real_dss: object, solution: _HourRecordingSolution) -> None:
        self._real_dss = real_dss
        self.Solution = solution

    def __getattr__(self, name: str) -> object:
        return getattr(self._real_dss, name)


class TestAntiAutoAdvanceProperty:
    """Acceptance item 2: the hour is set explicitly before every Solve()
    and never left to free-run. Deleting the `dss.Solution.Hour(...)` call
    in run_simulation() makes this test fail."""

    def test_solution_hour_set_before_every_solve(self, dss_file_path, monkeypatch):
        anchor = _frozen_anchor(datetime(2024, 3, 1, 10, 0, 0, tzinfo=timezone.utc))
        monkeypatch.setattr(opendss_engine_module, "get_anchor", lambda: anchor)

        recording_solution = _HourRecordingSolution(dss.Solution)
        monkeypatch.setattr(
            opendss_engine_module, "dss", _HourRecordingDSS(dss, recording_solution)
        )

        engine = OpenDSSEngine(dss_file_path)
        recording_solution.hour_setter_calls.clear()

        n_solves = 4
        for _ in range(n_solves):
            result = engine.run_simulation()
            assert result.converged

        assert len(recording_solution.hour_setter_calls) == n_solves, (
            "Solution.Hour(...) must be invoked with an explicit hour before "
            "every Solve() — if it is not, Daily mode auto-advances the "
            "loadshape instead of tracking simulated time."
        )
        for solved_hour in recording_solution.hour_setter_calls:
            assert solved_hour == anchor.sim_epoch.hour


class TestReproducibility:
    """Acceptance item 7: the same anchor produces the same sequence of
    solved hours across two engine instances — the property the unseeded
    `random.randint(6, 18)` broke."""

    def test_same_anchor_same_hour_sequence_across_instances(
        self, dss_file_path, monkeypatch
    ):
        def run_and_collect_hours(sim_dt: datetime, n_solves: int) -> list[int]:
            anchor = _frozen_anchor(sim_dt)
            monkeypatch.setattr(opendss_engine_module, "get_anchor", lambda: anchor)
            recording_solution = _HourRecordingSolution(dss.Solution)
            monkeypatch.setattr(
                opendss_engine_module,
                "dss",
                _HourRecordingDSS(dss, recording_solution),
            )
            engine = OpenDSSEngine(dss_file_path)
            recording_solution.hour_setter_calls.clear()
            for _ in range(n_solves):
                engine.run_simulation()
            return recording_solution.hour_setter_calls

        sim_dt = datetime(2024, 9, 10, 16, 0, 0, tzinfo=timezone.utc)
        sequence_a = run_and_collect_hours(sim_dt, n_solves=3)
        sequence_b = run_and_collect_hours(sim_dt, n_solves=3)

        assert sequence_a == sequence_b
        assert sequence_a == [sim_dt.hour] * 3
