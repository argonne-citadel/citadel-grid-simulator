"""Tests for sim-clock anchoring and the weekday/weekend load term.

Two defects are pinned here, and the first is why the second could not have
worked on its own.

CLOCK. The profile advanced STEP_MINUTES (1) per power-flow solve, one solve
per TIMESTEP_SECONDS (1.0) of wall time — a 60x compression — while every
substrate event is stamped from a clock running at SIM_RATE, 2880x in the
corpus. In a 30-sim-day baseline (900 s wall) the substrate clock advanced 30
days and the grid travelled 15 hours. The two disagreed by 48x, so telemetry
and sim_timestamp described different instants.

WEEKEND. With the grid never leaving Monday 1 Jan, a day-of-week term was
unreachable regardless of correctness. These tests therefore assert the clock
first, then the weekend behaviour it makes observable.
"""

from datetime import datetime, timedelta, timezone

import pytest

from src.engines.der_profiles import (
    DEFAULT_START_DT,
    StochasticDERProfile,
    is_weekend,
)
from src.sim_time import ClockAnchor, compute_sim_now

# The substrate's real compression: 1 sim-day per 30 s of wall time.
_SUBSTRATE_SIM_RATE = 2880.0
_WALL_START = datetime(2026, 9, 3, 12, 0, 0, tzinfo=timezone.utc)
# A Monday, matching the profile's historical default start.
_SIM_EPOCH = datetime(2024, 1, 1, 8, 0, 0, tzinfo=timezone.utc)


def _anchor(sim_rate: float = _SUBSTRATE_SIM_RATE) -> ClockAnchor:
    return ClockAnchor(
        sim_epoch=_SIM_EPOCH, wall_start=_WALL_START, sim_rate=sim_rate
    )


class TestSimClockAnchoring:
    def test_unanchored_profile_keeps_stepping_locally(self):
        """Standalone / UI mode is unchanged: advance() still steps the clock."""
        profile = StochasticDERProfile(seed=1)
        assert profile.anchored is False
        before = profile.current_dt
        profile.advance()
        assert profile.current_dt - before == timedelta(minutes=1)

    def test_anchored_profile_ignores_advance(self):
        """Anchored time is a function of wall-clock now, not of solve count.

        The engine calls advance() once per solve and must not need to know
        which mode it is in, so this is a no-op rather than an error.
        """
        profile = StochasticDERProfile(seed=1, anchor=_anchor())
        assert profile.anchored is True
        before = profile.current_dt
        for _ in range(100):
            profile.advance()
        # Only wall-clock drift between the two reads, far under a sim-minute
        # of the 2880x clock would require 0.02 s of wall time to accrue.
        assert abs((profile.current_dt - before).total_seconds()) < 3600

    def test_anchor_reproduces_the_substrate_clock_exactly(self):
        """compute_sim_now must match sim_epoch + elapsed_wall * sim_rate.

        Hand-computed: 900 s of wall time at 2880x is 2,592,000 sim-seconds,
        which is exactly 30 days.
        """
        anchor = _anchor()
        sim_now = compute_sim_now(anchor, _WALL_START + timedelta(seconds=900))
        assert sim_now - _SIM_EPOCH == timedelta(days=30)

    def test_thirty_day_window_now_traverses_thirty_days(self):
        """The regression: the grid used to travel 15 h over this window.

        900 s wall is the new 30-sim-day baseline. Under the old local clock
        (1 sim-minute per 1 s solve) that was 900 minutes = 15 hours.
        """
        anchor = _anchor()
        end = compute_sim_now(anchor, _WALL_START + timedelta(seconds=900))
        assert (end - _SIM_EPOCH).days == 30

        old_clock_span = timedelta(minutes=900)
        assert old_clock_span == timedelta(hours=15)
        assert end - _SIM_EPOCH > old_clock_span * 40

    def test_thirty_day_window_reaches_weekends(self):
        """A day-of-week term is only meaningful if a weekend is reached."""
        anchor = _anchor()
        seen = {
            compute_sim_now(anchor, _WALL_START + timedelta(seconds=s)).weekday()
            for s in range(0, 901, 60)
        }
        assert seen == set(range(7)), f"not all weekdays traversed: {sorted(seen)}"

        # And the old clock reached none of them.
        old_days = {
            (DEFAULT_START_DT + timedelta(minutes=m)).weekday() for m in range(0, 901)
        }
        assert old_days == {0}, "old local clock should never leave Monday"


class TestWeekendLoadTerm:
    """Weekday/weekend behaviour, asserted per load class."""

    @staticmethod
    def _profile_at(sim_dt: datetime) -> StochasticDERProfile:
        """A profile pinned to one simulated instant via a synthetic anchor."""
        return StochasticDERProfile(
            seed=7,
            anchor=ClockAnchor(
                sim_epoch=sim_dt,
                wall_start=datetime.now(timezone.utc),
                sim_rate=1e-9,  # effectively frozen for the duration of a test
            ),
        )

    def test_is_weekend_boundaries(self):
        # 2024-01-01 is a Monday.
        assert is_weekend(datetime(2024, 1, 5, 12, 0)) is False  # Friday
        assert is_weekend(datetime(2024, 1, 6, 12, 0)) is True  # Saturday
        assert is_weekend(datetime(2024, 1, 7, 12, 0)) is True  # Sunday
        assert is_weekend(datetime(2024, 1, 8, 12, 0)) is False  # Monday

    def test_industrial_drops_at_the_weekend(self):
        """Two-thirds of the weekday shape, from the 0.6/0.9 commercial ratio."""
        weekday = self._profile_at(datetime(2024, 1, 3, 10, 0, tzinfo=timezone.utc))
        weekend = self._profile_at(datetime(2024, 1, 6, 10, 0, tzinfo=timezone.utc))
        wd = weekday.industrial_load_factor()
        we = weekend.industrial_load_factor()
        assert we < wd
        # 2/3 ratio, loose enough for the +-0.02 Gaussian noise on each draw.
        assert we / wd == pytest.approx(2 / 3, abs=0.08)

    def test_residential_flattens_rather_than_uniformly_dropping(self):
        """At the evening peak it falls; in the overnight trough it barely moves."""
        peak_wd = self._profile_at(
            datetime(2024, 1, 3, 19, 0, tzinfo=timezone.utc)
        ).residential_load_factor()
        peak_we = self._profile_at(
            datetime(2024, 1, 6, 19, 0, tzinfo=timezone.utc)
        ).residential_load_factor()
        trough_wd = self._profile_at(
            datetime(2024, 1, 3, 3, 0, tzinfo=timezone.utc)
        ).residential_load_factor()
        trough_we = self._profile_at(
            datetime(2024, 1, 6, 3, 0, tzinfo=timezone.utc)
        ).residential_load_factor()

        assert peak_we < peak_wd, "weekend evening peak should be lower"
        assert abs(peak_wd - peak_we) > abs(trough_wd - trough_we), (
            "the weekend adjustment must compress the peak more than the "
            "trough — a flatter curve, not a uniformly lower one"
        )

    def test_flat_load_is_unchanged_at_the_weekend(self):
        """A data center / smelter runs 24/7; a weekend term would be wrong."""
        wd = self._profile_at(
            datetime(2024, 1, 3, 10, 0, tzinfo=timezone.utc)
        ).flat_load_factor()
        we = self._profile_at(
            datetime(2024, 1, 6, 10, 0, tzinfo=timezone.utc)
        ).flat_load_factor()
        assert we == pytest.approx(wd, abs=0.05)

    def test_solar_has_no_weekend_term(self):
        """Irradiance does not know what day it is."""
        wd = self._profile_at(
            datetime(2024, 1, 3, 12, 0, tzinfo=timezone.utc)
        ).solar_factor()
        we = self._profile_at(
            datetime(2024, 1, 6, 12, 0, tzinfo=timezone.utc)
        ).solar_factor()
        assert we == pytest.approx(wd, abs=0.15)
