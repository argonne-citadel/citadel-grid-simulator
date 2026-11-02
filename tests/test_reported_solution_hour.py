"""`/state` and `/reset` report the hour the physics actually ran at.

Both endpoints used to report `engine._pinned_hour`, the fallback used only
when NO sim-clock anchor exists. Under the substrate every engine is
anchored, so that field froze at whatever `reset()` last drew while the
loadshapes tracked sim time. Smoke cell local-20261020T140509Z recorded
`sim_hour: 15` with `sgen_generation_mw: 0.0` -- 15:00 is peak irradiance on
IEEE-123, so the artifact read as a broken DER coupling when the measurement
had simply been taken after dark. Nine of 38 historical smoke runs carry a
zero-DER result under an hour that should have had sun.
"""

import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parents[1]))

from src.engines.opendss_engine import OpenDSSEngine  # noqa: E402
from src.web_api import _engine_solution_hour  # noqa: E402

_IEEE37 = Path(__file__).parents[1] / "examples" / "IEEE37Bus_PV.dss"


class _FakeAnchoredEngine:
    """Anchored engine: solve hour moves, `_pinned_hour` does not."""

    def __init__(self, hour: int):
        self._hour = hour
        self._pinned_hour = 15  # the stale fallback the bug reported

    def solution_hour(self) -> int:
        return self._hour


class _EngineWithoutTheMethod:
    """PandaPowerEngine's shape: no solution_hour at all."""

    _pinned_hour = 9


@pytest.mark.unit
class TestReportedHourFollowsTheSolve:
    def test_it_reports_the_solve_hour_not_the_stale_fallback(self) -> None:
        assert _engine_solution_hour(_FakeAnchoredEngine(23)) == 23

    def test_it_tracks_a_moving_clock(self) -> None:
        engine = _FakeAnchoredEngine(10)
        assert _engine_solution_hour(engine) == 10
        engine._hour = 22
        assert _engine_solution_hour(engine) == 22

    def test_an_engine_without_the_method_reports_unknown(self) -> None:
        """None, not the stale 9: a wrong hour is worse than no hour."""
        assert _engine_solution_hour(_EngineWithoutTheMethod()) is None


@pytest.mark.unit
@pytest.mark.skipif(not _IEEE37.exists(), reason="IEEE-37 feeder not present")
class TestAgainstARealEngine:
    def test_the_reported_hour_equals_the_anchored_sim_hour(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The whole bug in one assertion, on a real engine.

        `reset()` draws a fallback hour in [6, 18]; the anchor is pinned to
        22:00, outside that range, so a reported 22 cannot be the fallback
        having coincidentally agreed.
        """
        import src.sim_time as sim_time

        anchor = sim_time.ClockAnchor(
            sim_epoch=datetime(2024, 1, 1, tzinfo=timezone.utc),
            wall_start=datetime.now(timezone.utc) - timedelta(seconds=11),
            sim_rate=7200.0,  # 11 wall-s -> 22 sim-h past midnight
        )
        monkeypatch.setattr(sim_time, "get_anchor", lambda: anchor)
        monkeypatch.setattr(
            "src.engines.opendss_engine.get_anchor", lambda: anchor, raising=False
        )

        engine = OpenDSSEngine(str(_IEEE37))
        engine.reset()
        reported = _engine_solution_hour(engine)
        assert reported == engine.solution_hour()
        assert reported == 22
        assert reported != engine._pinned_hour
