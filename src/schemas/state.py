"""
Runtime state Pydantic models.

This module defines data models for the current state of the grid simulation.
"""

from datetime import datetime
from typing import Dict, Optional

from pydantic import BaseModel, Field

from .common import (
    AngleDeg,
    BusID,
    GeneratorID,
    LineID,
    LoadID,
    PowerMW,
    PowerMVAr,
    StorageID,
    VoltagePU,
)


# ============================================================================
# Bus State
# ============================================================================


class BusState(BaseModel):
    """Current state of a bus/node."""

    bus_id: BusID
    voltage_pu: VoltagePU = Field(description="Voltage magnitude in per-unit")
    angle_deg: AngleDeg = Field(description="Voltage angle in degrees")
    timestamp: datetime = Field(description="State timestamp")


# ============================================================================
# Line State
# ============================================================================


class LineState(BaseModel):
    """Current state of a line/branch."""

    line_id: LineID

    # Power flow from 'from_bus' perspective
    p_from_mw: PowerMW = Field(description="Active power from 'from_bus' in MW")
    q_from_mvar: PowerMVAr = Field(description="Reactive power from 'from_bus' in MVAr")

    # Power flow from 'to_bus' perspective
    p_to_mw: PowerMW = Field(description="Active power from 'to_bus' in MW")
    q_to_mvar: PowerMVAr = Field(description="Reactive power from 'to_bus' in MVAr")

    # Current and loading
    current_ka: float = Field(ge=0, description="Current magnitude in kA")
    loading_percent: float = Field(ge=0, description="Loading as % of max current")

    timestamp: datetime = Field(description="State timestamp")


# ============================================================================
# Generator State
# ============================================================================


class GeneratorState(BaseModel):
    """Current state of a generator/DER."""

    generator_id: GeneratorID

    # Power output
    p_mw: PowerMW = Field(description="Active power output in MW")
    q_mvar: PowerMVAr = Field(description="Reactive power output in MVAr")

    # Voltage (for PV buses)
    voltage_pu: Optional[VoltagePU] = Field(
        None, description="Terminal voltage in per-unit"
    )

    timestamp: datetime = Field(description="State timestamp")


# ============================================================================
# Load State
# ============================================================================


class LoadState(BaseModel):
    """Current state of a load."""

    load_id: LoadID

    # Power consumption
    p_mw: PowerMW = Field(description="Active power consumption in MW")
    q_mvar: PowerMVAr = Field(description="Reactive power consumption in MVAr")

    timestamp: datetime = Field(description="State timestamp")


# ============================================================================
# Storage State
# ============================================================================


class StorageState(BaseModel):
    """Current state of energy storage."""

    storage_id: StorageID

    # Power (positive = discharging, negative = charging)
    p_mw: PowerMW = Field(description="Power in MW (+ discharge, - charge)")

    # State of charge
    soc_mwh: float = Field(ge=0, description="State of charge in MWh")
    soc_percent: float = Field(ge=0, le=100, description="State of charge as %")

    timestamp: datetime = Field(description="State timestamp")


# ============================================================================
# Complete Grid State
# ============================================================================


class GridState(BaseModel):
    """Complete snapshot of grid state at a point in time."""

    timestamp: datetime = Field(description="State snapshot timestamp")

    # Component states
    buses: Dict[BusID, BusState] = Field(description="All bus states")
    lines: Dict[LineID, LineState] = Field(description="All line states")
    generators: Dict[GeneratorID, GeneratorState] = Field(
        description="All generator states"
    )
    loads: Dict[LoadID, LoadState] = Field(description="All load states")
    storage: Dict[StorageID, StorageState] = Field(
        default_factory=dict, description="All storage states"
    )

    # Power flow convergence
    converged: bool = Field(description="Power flow converged successfully")
    iterations: Optional[int] = Field(None, description="Number of iterations")

    # System-level metrics
    total_generation_mw: float = Field(description="Total active power generation")
    total_load_mw: float = Field(description="Total active power load")
    total_losses_mw: float = Field(
        description="Total system losses (can be negative in some edge cases)"
    )
