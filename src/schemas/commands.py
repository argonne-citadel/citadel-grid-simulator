"""
Control command Pydantic models.

This module defines data models for control commands sent to the grid.
"""

from datetime import datetime
from enum import Enum
from typing import Optional, Union

from pydantic import BaseModel, Field

from .common import (
    GeneratorID,
    LineID,
    LoadID,
    PowerMW,
    PowerMVAr,
    StorageID,
    TransformerID,
    VoltagePU,
)


# ============================================================================
# Command Type Enum
# ============================================================================


class CommandType(str, Enum):
    """Type of control command."""

    BREAKER = "breaker"
    GENERATOR_SETPOINT = "generator_setpoint"
    LOAD_ADJUSTMENT = "load_adjustment"
    STORAGE_CONTROL = "storage_control"
    TRANSFORMER_TAP = "transformer_tap"


# ============================================================================
# Breaker Command
# ============================================================================


class BreakerCommand(BaseModel):
    """Command to control a circuit breaker (line switch)."""

    command_type: CommandType = Field(
        default=CommandType.BREAKER, description="Command type"
    )
    line_id: LineID = Field(description="Line/breaker to control")
    closed: bool = Field(description="True to close breaker, False to open")
    timestamp: datetime = Field(
        default_factory=datetime.now, description="Command timestamp"
    )


# ============================================================================
# Generator Setpoint Command
# ============================================================================


class GeneratorCommand(BaseModel):
    """Command to set generator output setpoint."""

    command_type: CommandType = Field(
        default=CommandType.GENERATOR_SETPOINT, description="Command type"
    )
    generator_id: GeneratorID = Field(description="Generator to control")

    # Power setpoints
    p_mw: Optional[PowerMW] = Field(None, description="Active power setpoint in MW")
    q_mvar: Optional[PowerMVAr] = Field(
        None, description="Reactive power setpoint in MVAr"
    )

    # Voltage setpoint (for PV buses)
    voltage_setpoint_pu: Optional[VoltagePU] = Field(
        None, description="Voltage setpoint in per-unit"
    )

    timestamp: datetime = Field(
        default_factory=datetime.now, description="Command timestamp"
    )


# ============================================================================
# Load Adjustment Command
# ============================================================================


class LoadCommand(BaseModel):
    """Command to adjust load demand."""

    command_type: CommandType = Field(
        default=CommandType.LOAD_ADJUSTMENT, description="Command type"
    )
    load_id: LoadID = Field(description="Load to adjust")

    # Power adjustments
    p_mw: Optional[PowerMW] = Field(None, description="New active power demand in MW")
    q_mvar: Optional[PowerMVAr] = Field(
        None, description="New reactive power demand in MVAr"
    )

    timestamp: datetime = Field(
        default_factory=datetime.now, description="Command timestamp"
    )


# ============================================================================
# Storage Control Command
# ============================================================================


class StorageCommand(BaseModel):
    """Command to control energy storage."""

    command_type: CommandType = Field(
        default=CommandType.STORAGE_CONTROL, description="Command type"
    )
    storage_id: StorageID = Field(description="Storage unit to control")

    # Power setpoint (positive = discharge, negative = charge)
    p_mw: PowerMW = Field(description="Power setpoint in MW (+ discharge, - charge)")

    timestamp: datetime = Field(
        default_factory=datetime.now, description="Command timestamp"
    )


# ============================================================================
# Transformer Tap Command
# ============================================================================


class TransformerTapCommand(BaseModel):
    """Command to adjust transformer tap position."""

    command_type: CommandType = Field(
        default=CommandType.TRANSFORMER_TAP, description="Command type"
    )
    transformer_id: TransformerID = Field(description="Transformer to control")

    # Tap position (integer, typically -16 to +16 or similar range)
    tap_position: int = Field(description="Tap position")

    timestamp: datetime = Field(
        default_factory=datetime.now, description="Command timestamp"
    )


# ============================================================================
# Union Type for All Commands
# ============================================================================

# Union type for any command
Command = Union[
    BreakerCommand,
    GeneratorCommand,
    LoadCommand,
    StorageCommand,
    TransformerTapCommand,
]
