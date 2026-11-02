"""Read complete OpenDSS bus states, reporting isolated buses as de-energized.

``read_bus_states`` returns states and de-energized IDs under the caller's engine
lock. Breakers are modeled as terminal switches (see
``OpenDSSEngine.set_breaker_status``), so every bus in the engine's ID map stays
in OpenDSS's live bus list for the life of a compiled circuit; an isolated bus
reads zero per-unit voltage. A bus missing from the live list therefore means
the ID map is stale or an element was disabled out-of-band, and raises rather
than inventing a voltage or dropping a published bus.
"""

import logging
from datetime import datetime

import opendssdirect as dss

from ..schemas.common import BusID
from ..schemas.state import BusState

logger = logging.getLogger(__name__)

# Match VoltagePU's schema bound. Above this, puVmagAngle may be raw volts.
_MAX_PLAUSIBLE_PU = 2.0

# An isolated bus reads exactly 0.0 today; the tolerance absorbs solver noise.
_DEENERGIZED_PU = 1e-3


def _read_voltage_angle(bus_name: str) -> tuple[float, float]:
    """Read first-phase per-unit voltage; unexplained missing data must raise."""
    try:
        if dss.Circuit.SetActiveBus(bus_name) < 0:
            raise RuntimeError("bus disappeared from the live bus list")
        values = dss.Bus.puVmagAngle()
        if len(values) < 2:
            raise RuntimeError("bus has no first-phase voltage and angle")
        voltage, angle = values[:2]
        if voltage > _MAX_PLAUSIBLE_PU:
            raise RuntimeError(
                f"voltage {voltage:g} exceeds {_MAX_PLAUSIBLE_PU} pu; "
                "OpenDSS may have returned raw volts"
            )
        return float(voltage), float(angle)
    except Exception as exc:
        raise RuntimeError(f"Failed to read state for bus {bus_name!r}: {exc}") from exc


def read_bus_states(
    bus_id_to_name: dict[BusID, str],
    timestamp: datetime,
) -> tuple[dict[BusID, BusState], list[int]]:
    """Return every mapped bus and the IDs of those reading as de-energized."""
    live_names = {name.lower() for name in dss.Circuit.AllBusNames()}
    buses: dict[BusID, BusState] = {}
    islanded: list[int] = []
    for bus_id, bus_name in bus_id_to_name.items():
        if bus_name not in live_names:
            raise RuntimeError(
                f"Bus {bus_name!r} (id {bus_id}) is absent from the OpenDSS bus "
                "list; breakers switch terminals and never remove buses, so the "
                "ID map is stale or an element was disabled out-of-band"
            )
        voltage, angle = _read_voltage_angle(bus_name)
        buses[bus_id] = BusState(
            bus_id=bus_id, voltage_pu=voltage, angle_deg=angle, timestamp=timestamp
        )
        if voltage < _DEENERGIZED_PU:
            logger.debug("Bus %s (%r) reads de-energized", bus_id, bus_name)
            islanded.append(int(bus_id))
    return buses, islanded
