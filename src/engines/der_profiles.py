"""
der_profiles — Stochastic DER and load profiles for pandapower quasi-static
time-series simulation.

Public interface:
    StochasticDERProfile  — tracks simulated time, produces PV/wind capacity
                            factors and load multipliers per step with Gaussian
                            noise on base hourly profiles.

Design decisions:
    • Base solar profile exactly matches the IEEE37Bus_PV.dss MyIrrad loadshape
      (24 hourly values) so the pandapower and OpenDSS engines produce comparable
      generation shapes without any parameter tuning.
    • Wind uses a diurnal profile calibrated to ~0.4–0.7 capacity factor with an
      afternoon thermal-convection peak — a typical mid-latitude onshore pattern.
    • Load profiles: industrial (3-shift), residential (evening peak), and flat
      (data center / smelter). All are [0, 1] multipliers on base load values.
    • Gaussian noise is added each step. Historical irradiance/SCADA data can
      replace the base profiles later by subclassing or passing custom lists.
    • Load profiles carry a WEEKDAY/WEEKEND term as well as the diurnal one.
      Industrial drops to two thirds and residential flattens toward its daily
      minimum; flat (data center / smelter) is 24/7 by definition and does not
      move. Values reused from timeseries/load_profiles.py rather than invented
      — see _WEEKEND_* below.
    • Simulated time comes from the SUBSTRATE'S clock when one is available
      (sim_time.get_anchor()), and only falls back to stepping STEP_MINUTES per
      solve when running standalone. Those two rates differ by 48x under the
      substrate's SIM_RATE=2880, which is why the fallback is not the default —
      see the `anchor` argument and sim_time's module docstring.
"""

import math
from datetime import datetime, timedelta
from typing import Optional

import numpy as np

from ..sim_time import ClockAnchor, compute_sim_now

# Default start: 08:00 Jan 1 2024. At this hour MyIrrad=0.3, giving realistic
# morning generation at the start of each attack run. Configurable at construction.
DEFAULT_START_DT: datetime = datetime(2024, 1, 1, 8, 0, 0)

# Simulated minutes advanced per step, used ONLY when no substrate sim-clock
# anchor is available (standalone / UI / demo). 1 min/step matches the UI clock
# compression (SIM_TIME_SCALE=60 in app.js).
#
# Not used under the substrate: SIM_RATE there is 2880, so stepping at 60x
# would leave the grid 48x behind the clock every event is stamped with.
STEP_MINUTES: int = 1

# Weekend terms. Both reused from timeseries/load_profiles.py so this repo's
# two profile generators cannot disagree about what a weekend does; see
# _weekend_industrial / _weekend_residential for the derivations.
_WEEKEND_INDUSTRIAL_SCALE: float = 0.6 / 0.9
_WEEKEND_RESIDENTIAL_BLEND: float = 0.9

# Solar base profile: IEEE37Bus_PV.dss MyIrrad loadshape, index = hour (0–23).
# Peak at solar noon (hours 12–13 = 1.0). Zero outside daylight hours.
_SOLAR_BASE: list[float] = [
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,  # 00–05: night
    0.1,
    0.2,
    0.3,
    0.5,
    0.8,
    0.9,  # 06–11: morning ramp
    1.0,
    1.0,
    0.99,
    0.9,
    0.7,
    0.4,  # 12–17: peak and afternoon descent
    0.1,
    0.0,
    0.0,
    0.0,
    0.0,
    0.0,  # 18–23: evening / night
]

# Wind base profile: mild diurnal cycle with afternoon thermal-convection peak.
# Stable nocturnal flow, morning boundary-layer suppression, afternoon mixing peak.
# Calibrated to ~0.55 mean capacity factor — typical mid-latitude onshore wind.
_WIND_BASE: list[float] = [
    0.55,
    0.55,
    0.55,
    0.55,
    0.55,
    0.50,  # 00–05: stable nocturnal
    0.45,
    0.40,
    0.40,
    0.42,
    0.45,
    0.50,  # 06–11: morning boundary-layer lull
    0.55,
    0.60,
    0.65,
    0.68,
    0.70,
    0.68,  # 12–17: afternoon thermal mixing peak
    0.65,
    0.60,
    0.60,
    0.58,
    0.56,
    0.55,  # 18–23: evening taper
]


# Industrial load profile: 3-shift factory pattern.
# Full output during day shift, slight reduction on evening shift,
# lower overnight.  Dips at shift changes (06, 14, 22).
_INDUSTRIAL_LOAD_BASE: list[float] = [
    0.60,
    0.60,
    0.60,
    0.60,
    0.60,
    0.55,  # 00–05: night shift
    0.50,
    0.85,
    0.90,
    0.95,
    0.95,
    0.95,  # 06–11: day shift ramp
    0.95,
    0.90,
    0.80,
    0.85,
    0.85,
    0.85,  # 12–17: lunch dip, evening shift
    0.85,
    0.85,
    0.80,
    0.75,
    0.65,
    0.60,  # 18–23: evening shift taper
]

# Residential load profile: morning and evening peaks, midday valley.
_RESIDENTIAL_LOAD_BASE: list[float] = [
    0.40,
    0.35,
    0.35,
    0.35,
    0.35,
    0.40,  # 00–05: sleeping
    0.55,
    0.75,
    0.80,
    0.70,
    0.55,
    0.50,  # 06–11: morning peak
    0.50,
    0.50,
    0.50,
    0.55,
    0.65,
    0.85,  # 12–17: midday low, evening ramp
    0.95,
    1.00,
    0.95,
    0.85,
    0.65,
    0.50,  # 18–23: evening peak, wind-down
]

# Flat load profile: data center / smelter — nearly constant.
_FLAT_LOAD_BASE: list[float] = [
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
    0.95,
]


class StochasticDERProfile:
    """
    Tracks simulated time and provides stochastic PV/wind capacity factors per step.

    Each call to solar_factor() / wind_factor() returns a value in [0, 1] drawn
    from the base hourly profile plus Gaussian noise. advance() steps the
    simulated clock forward by STEP_MINUTES minutes. reset() restores the
    initial datetime so each attack run starts at a reproducible operating point.

    Args:
        start_dt:     Simulated start datetime (default: 08:00 Jan 1 2024).
        solar_sigma:  Gaussian noise std-dev on PV factor (default 0.05 = ±5 %).
                      Represents cloud-cover transients.
        wind_sigma:   Gaussian noise std-dev on wind factor (default 0.12 = ±12 %).
                      Wind is inherently more variable than irradiance hour-to-hour.
        seed:         Optional RNG seed. Fix for reproducible attack-run baselines;
                      leave None for independent random draws across runs.
    """

    def __init__(
        self,
        start_dt: datetime = DEFAULT_START_DT,
        solar_sigma: float = 0.05,
        wind_sigma: float = 0.12,
        seed: Optional[int] = None,
        anchor: Optional[ClockAnchor] = None,
    ) -> None:
        self._start_dt = start_dt
        self._current_dt = start_dt
        self._solar_sigma = solar_sigma
        self._wind_sigma = wind_sigma
        self._rng = np.random.default_rng(seed)
        # Injected rather than fetched here so tests can pin an anchor without
        # HTTP, and so the caller decides whether this process participates in
        # the substrate's clock at all.
        self._anchor = anchor

    @property
    def anchored(self) -> bool:
        """True when simulated time comes from the substrate's shared clock."""
        return self._anchor is not None

    @property
    def current_dt(self) -> datetime:
        """Current simulated datetime.

        Derived from the substrate anchor when there is one, so the grid's
        operating point and the sim_timestamp on every emitted event agree.
        Falls back to the locally stepped clock only when running standalone.
        """
        if self._anchor is not None:
            return compute_sim_now(self._anchor)
        return self._current_dt

    def advance(self) -> None:
        """Advance the local clock one step; a no-op when anchored.

        Anchored time is a function of wall-clock now, so there is nothing to
        increment. Kept callable either way so the engine's solve loop does
        not need to branch — and deliberately NOT raising, because the caller
        advancing per solve is correct behaviour in standalone mode.
        """
        if self._anchor is not None:
            return
        self._current_dt += timedelta(minutes=STEP_MINUTES)

    def reset(self, start_dt: Optional[datetime] = None) -> None:
        """Reset simulated time to start_dt (or the initial start_dt if None).

        Called at the beginning of each attack run so every run starts at the
        same operating point regardless of when the previous run ended.
        """
        self._current_dt = start_dt if start_dt is not None else self._start_dt

    def solar_factor(self) -> float:
        """PV capacity factor [0, 1] for the current simulated time.

        Linearly interpolates the hourly base profile for sub-hour
        resolution, then adds Gaussian noise clamped to [0, 1].
        """
        base = _interpolate_hourly(self.current_dt, _SOLAR_BASE)
        return float(
            max(0.0, min(1.0, base + self._rng.normal(0.0, self._solar_sigma)))
        )

    def wind_factor(self) -> float:
        """Wind capacity factor [0, 1] for the current simulated time.

        Linearly interpolates the hourly base profile for sub-hour
        resolution, then adds Gaussian noise clamped to [0, 1].
        """
        base = _interpolate_hourly(self.current_dt, _WIND_BASE)
        return float(max(0.0, min(1.0, base + self._rng.normal(0.0, self._wind_sigma))))

    def industrial_load_factor(self) -> float:
        """Industrial load multiplier [0, 1] — 3-shift diurnal, reduced at weekends."""
        now = self.current_dt
        base = _interpolate_hourly(now, _INDUSTRIAL_LOAD_BASE)
        if is_weekend(now):
            base = _weekend_industrial(base)
        return float(max(0.0, min(1.0, base + self._rng.normal(0.0, 0.02))))

    def residential_load_factor(self) -> float:
        """Residential load multiplier [0, 1] — peaks on weekdays, flatter at weekends."""
        now = self.current_dt
        base = _interpolate_hourly(now, _RESIDENTIAL_LOAD_BASE)
        if is_weekend(now):
            base = _weekend_residential(base)
        return float(max(0.0, min(1.0, base + self._rng.normal(0.0, 0.03))))

    def flat_load_factor(self) -> float:
        """Flat load multiplier [0, 1] — nearly constant (data center/smelter)."""
        # No weekend term: a data center / smelter runs 24/7 by definition.
        base = _interpolate_hourly(self.current_dt, _FLAT_LOAD_BASE)
        return float(max(0.0, min(1.0, base + self._rng.normal(0.0, 0.01))))


def _interpolate_hourly(dt: datetime, hourly_profile: list[float]) -> float:
    """Linearly interpolate a 24-element hourly profile at a fractional hour."""
    h = dt.hour
    frac = dt.minute / 60.0
    v0 = hourly_profile[h % 24]
    v1 = hourly_profile[(h + 1) % 24]
    return v0 + frac * (v1 - v0)


def is_weekend(dt: datetime) -> bool:
    """Saturday or Sunday. Public so tests can state the phase explicitly."""
    return dt.weekday() >= 5


def _weekend_industrial(factor: float) -> float:
    """Industrial weekend output as a fraction of the weekday shape.

    Derivation: timeseries/load_profiles.py's commercial generator runs
    industrial sites at 0.9 x peak on weekdays and 0.6 x peak at weekends, so
    the weekend/weekday ratio is 0.6/0.9 = 2/3. Reused rather than re-chosen
    so the two profile generators in this repo cannot disagree about what a
    weekend does.
    """
    return factor * _WEEKEND_INDUSTRIAL_SCALE


def _weekend_residential(factor: float) -> float:
    """Residential weekends are flatter, not simply lower.

    Derivation: the same generator's residential weekend adjustment,
    `base * 0.9 + base_load * 0.1` — a 10% pull toward the overnight minimum,
    which compresses the morning and evening peaks while leaving the trough
    almost unchanged.

    KNOWN LIMITATION: this flattens the curve but does not SHIFT it. Real
    residential weekends also push the morning ramp roughly two hours later.
    Modelling that means new shapes rather than reused ones, so it is left
    out deliberately rather than guessed at; the substrate is easier than
    reality on this axis and timing-axis results are an upper bound.
    """
    trough = min(_RESIDENTIAL_LOAD_BASE)
    return factor * _WEEKEND_RESIDENTIAL_BLEND + trough * (
        1.0 - _WEEKEND_RESIDENTIAL_BLEND
    )
