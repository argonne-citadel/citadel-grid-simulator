"""
Common types, enums, and constrained types for grid simulator schemas.

This module defines the foundational types used across all Pydantic models.
"""

from enum import Enum
from typing import Annotated, NewType

from pydantic import Field


# ============================================================================
# Enums
# ============================================================================


class BusType(str, Enum):
    """Bus type classification."""

    SLACK = "slack"  # Reference bus (voltage and angle reference)
    PV = "pv"  # Generator bus (voltage controlled)
    PQ = "pq"  # Load bus (power specified)


class DeviceStatus(str, Enum):
    """Device operational status."""

    ONLINE = "online"
    OFFLINE = "offline"
    MAINTENANCE = "maintenance"
    FAULT = "fault"


class DERType(str, Enum):
    """Distributed Energy Resource type."""

    SOLAR_PV = "solar_pv"
    WIND = "wind"
    BATTERY_STORAGE = "battery_storage"
    DIESEL_GENERATOR = "diesel_generator"
    FUEL_CELL = "fuel_cell"
    MICROTURBINE = "microturbine"
    OTHER = "other"


# ============================================================================
# Type Aliases (for component IDs)
# ============================================================================

BusID = NewType("BusID", int)
LineID = NewType("LineID", int)
GeneratorID = NewType("GeneratorID", int)
LoadID = NewType("LoadID", int)
StorageID = NewType("StorageID", int)
TransformerID = NewType("TransformerID", int)


# ============================================================================
# Constrained Types (with validation)
# ============================================================================

# Voltage in per-unit (typically 0.9 - 1.1 for normal operation)
VoltagePU = Annotated[float, Field(ge=0.0, le=2.0, description="Voltage in per-unit")]

# Angle in degrees (-180 to 180)
AngleDeg = Annotated[float, Field(ge=-180.0, le=180.0, description="Angle in degrees")]

# Power in MW (can be negative for generation)
PowerMW = Annotated[float, Field(description="Active power in MW")]

# Reactive power in MVAr (can be negative)
PowerMVAr = Annotated[float, Field(description="Reactive power in MVAr")]

# Frequency in Hz (typically 59.5 - 60.5 for 60Hz systems)
Frequency = Annotated[float, Field(ge=55.0, le=65.0, description="Frequency in Hz")]
