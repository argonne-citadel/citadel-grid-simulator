"""sim_time — read the substrate's shared simulated-time anchor.

Public interface:
    ClockAnchor                    — frozen (sim_epoch, wall_start, sim_rate)
    fetch_anchor(url, timeout)     — one GET {url}/clock
    get_anchor()                   — cached; reads SIM_CLOCK_URL from the env
    compute_sim_now(anchor, now)   — pure: sim_epoch + (now - wall_start) * sim_rate

WHY THIS EXISTS. The simulator was running its own clock, and it disagreed
with the one every emitted event is stamped with.

    substrate clock:  sim_now = sim_epoch + (wall_now - wall_start) * sim_rate
                      SIM_RATE=2880 in the substrate corpus -> 2880x
    profile clock:    += STEP_MINUTES (1) per power-flow solve, one solve per
                      TIMESTEP_SECONDS (1.0) of wall time             -> 60x

They differ by 48x. In a 30-sim-day baseline window (900 s of wall time), the
substrate clock advanced 30 days while the grid's operating point travelled
15 hours — from Monday 1 Jan 08:00 to Monday 1 Jan 23:00. An event stamped
"day 12, 03:00" was recorded against a grid that still thought it was the
first evening, so any temporal feature joining telemetry to sim_timestamp was
correlating two different time bases. It also meant a day-of-week term could
never fire: the profile never reached a Saturday, in any window length we run.

Deliberately stdlib-only, using urllib rather than httpx. This repository is
a standalone submodule with its own dependency set, and one GET at startup
does not justify either a new dependency or a hard import from the parent's
dockerfiles/common. The wire contract ({sim_epoch, wall_start, sim_rate} from
GET /clock) is what the two sides share, not code.

Fail-loud semantics mirror the parent's sim_clock_client on purpose, so the
two behave the same way in the same situations:
  - SIM_CLOCK_URL unset        -> None. The standalone/UI/demo path, where the
                                  simulator legitimately owns its own clock.
  - SIM_CLOCK_URL unreachable  -> raises. A substrate run whose grid silently
                                  fell back to a private 60x clock is exactly
                                  the failure this module exists to end.
"""

import json
import os
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from threading import Lock
from typing import Optional

_FETCH_TIMEOUT_SECONDS = 5.0

_anchor_cache: Optional["ClockAnchor"] = None
_anchor_lock = Lock()
_anchor_fetched = False


@dataclass(frozen=True)
class ClockAnchor:
    """The (sim_epoch, wall_start, sim_rate) triple every producer shares."""

    sim_epoch: datetime
    wall_start: datetime
    sim_rate: float


def _parse_iso(raw: str) -> datetime:
    normalized = raw.replace("Z", "+00:00") if raw.endswith("Z") else raw
    parsed = datetime.fromisoformat(normalized)
    if parsed.tzinfo is None:
        raise ValueError(f"timestamp from sim-clock missing tz offset: {raw!r}")
    return parsed.astimezone(timezone.utc)


def fetch_anchor(url: str, timeout: float = _FETCH_TIMEOUT_SECONDS) -> ClockAnchor:
    """GET {url}/clock and parse the anchor. Raises on any failure."""
    endpoint = f"{url.rstrip('/')}/clock"
    with urllib.request.urlopen(endpoint, timeout=timeout) as response:  # nosec B310
        payload = json.loads(response.read().decode("utf-8"))

    sim_rate = float(payload["sim_rate"])
    if sim_rate <= 0:
        raise ValueError(f"sim-clock returned non-positive sim_rate: {sim_rate}")

    return ClockAnchor(
        sim_epoch=_parse_iso(payload["sim_epoch"]),
        wall_start=_parse_iso(payload["wall_start"]),
        sim_rate=sim_rate,
    )


def get_anchor() -> Optional[ClockAnchor]:
    """Cached anchor for this process, or None when SIM_CLOCK_URL is unset.

    Cached because wall_start is fixed for the life of a run: re-fetching
    would add nothing but a failure mode.
    """
    global _anchor_cache, _anchor_fetched
    with _anchor_lock:
        if _anchor_fetched:
            return _anchor_cache
        url = os.environ.get("SIM_CLOCK_URL", "").strip()
        _anchor_cache = fetch_anchor(url) if url else None
        _anchor_fetched = True
        return _anchor_cache


def compute_sim_now(anchor: ClockAnchor, now: Optional[datetime] = None) -> datetime:
    """Simulated time corresponding to wall-clock `now` (default: utcnow)."""
    wall_now = now if now is not None else datetime.now(timezone.utc)
    elapsed_wall = (wall_now - anchor.wall_start).total_seconds()
    return anchor.sim_epoch + timedelta(seconds=elapsed_wall * anchor.sim_rate)


def _reset_cache_for_tests() -> None:
    """Test helper. NOT for production use."""
    global _anchor_cache, _anchor_fetched
    with _anchor_lock:
        _anchor_cache = None
        _anchor_fetched = False
