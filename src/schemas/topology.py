"""
Network topology Pydantic models.

This module defines immutable data models for network topology information.
"""

from typing import Dict, List, Optional

from pydantic import BaseModel, Field

from .common import (
    AngleDeg,
    BusID,
    BusType,
    DERType,
    DeviceStatus,
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
# Bus/Node Information
# ============================================================================


class BusInfo(BaseModel, frozen=True):
    """Information about a bus/node in the network."""

    bus_id: BusID
    name: str
    bus_type: BusType
    voltage_nominal_kv: float = Field(gt=0, description="Nominal voltage in kV")

    # Optional Grid-STIX integration
    grid_stix_id: Optional[str] = Field(None, description="Grid-STIX object ID")
    grid_stix_metadata: Optional[Dict[str, str]] = Field(
        None, description="Additional Grid-STIX metadata"
    )


# ============================================================================
# Line/Branch Information
# ============================================================================


class LineInfo(BaseModel, frozen=True):
    """Information about a line/branch in the network."""

    line_id: LineID
    name: str
    from_bus: BusID
    to_bus: BusID

    # Electrical parameters
    resistance_ohm: float = Field(ge=0, description="Resistance in Ohms")
    reactance_ohm: float = Field(description="Reactance in Ohms")
    capacitance_nf: float = Field(ge=0, description="Capacitance in nF")
    max_current_ka: float = Field(gt=0, description="Maximum current in kA")

    # Status
    in_service: bool = Field(True, description="Line is in service")

    # Optional Grid-STIX integration
    grid_stix_id: Optional[str] = None
    grid_stix_metadata: Optional[Dict[str, str]] = None


# ============================================================================
# Generator/DER Information
# ============================================================================


class GeneratorInfo(BaseModel, frozen=True):
    """Information about a generator or DER in the network."""

    generator_id: GeneratorID
    name: str
    bus: BusID

    # Type and capacity
    der_type: Optional[DERType] = None
    p_max_mw: float = Field(gt=0, description="Maximum active power in MW")
    p_min_mw: float = Field(ge=0, description="Minimum active power in MW")
    q_max_mvar: float = Field(description="Maximum reactive power in MVAr")
    q_min_mvar: float = Field(description="Minimum reactive power in MVAr")

    # Control settings
    voltage_setpoint_pu: Optional[VoltagePU] = Field(
        None, description="Voltage setpoint for PV buses"
    )

    # Status
    status: DeviceStatus = DeviceStatus.ONLINE
    in_service: bool = True

    # Optional Grid-STIX integration
    grid_stix_id: Optional[str] = None
    grid_stix_metadata: Optional[Dict[str, str]] = None


# ============================================================================
# Load Information
# ============================================================================


class LoadInfo(BaseModel, frozen=True):
    """Information about a load in the network."""

    load_id: LoadID
    name: str
    bus: BusID

    # Load characteristics
    p_mw: PowerMW = Field(description="Active power demand in MW")
    q_mvar: PowerMVAr = Field(description="Reactive power demand in MVAr")

    # Status
    in_service: bool = True

    # Optional Grid-STIX integration
    grid_stix_id: Optional[str] = None
    grid_stix_metadata: Optional[Dict[str, str]] = None


# ============================================================================
# Transformer Information
# ============================================================================


class TransformerInfo(BaseModel, frozen=True):
    """Information about a transformer in the network."""

    transformer_id: TransformerID
    name: str
    hv_bus: BusID  # High voltage bus
    lv_bus: BusID  # Low voltage bus

    # Ratings
    rated_power_mva: float = Field(gt=0, description="Rated power in MVA")
    hv_voltage_kv: float = Field(
        gt=0, description="High voltage side nominal voltage in kV"
    )
    lv_voltage_kv: float = Field(
        gt=0, description="Low voltage side nominal voltage in kV"
    )

    # Electrical parameters
    resistance_percent: float = Field(ge=0, description="Resistance in percent")
    reactance_percent: float = Field(ge=0, description="Reactance in percent")

    # Tap changer (optional)
    tap_position: Optional[int] = Field(None, description="Current tap position")
    tap_min: Optional[int] = Field(None, description="Minimum tap position")
    tap_max: Optional[int] = Field(None, description="Maximum tap position")
    tap_step_percent: Optional[float] = Field(
        None, description="Tap step size in percent"
    )

    # Status
    in_service: bool = Field(True, description="Transformer is in service")

    # Optional Grid-STIX integration
    grid_stix_id: Optional[str] = None
    grid_stix_metadata: Optional[Dict[str, str]] = None


# ============================================================================
# Energy Storage Information
# ============================================================================


class StorageInfo(BaseModel, frozen=True):
    """Information about energy storage in the network."""

    storage_id: StorageID
    name: str
    bus: BusID

    # Capacity and power ratings
    energy_capacity_mwh: float = Field(gt=0, description="Energy capacity in MWh")
    p_max_mw: float = Field(gt=0, description="Maximum charge/discharge power in MW")

    # Efficiency
    efficiency_charge: float = Field(
        ge=0, le=1, description="Charging efficiency (0-1)"
    )
    efficiency_discharge: float = Field(
        ge=0, le=1, description="Discharging efficiency (0-1)"
    )

    # State of charge limits
    soc_min_percent: float = Field(ge=0, le=100, description="Minimum SOC %")
    soc_max_percent: float = Field(ge=0, le=100, description="Maximum SOC %")

    # Status
    status: DeviceStatus = DeviceStatus.ONLINE
    in_service: bool = True

    # Optional Grid-STIX integration
    grid_stix_id: Optional[str] = None
    grid_stix_metadata: Optional[Dict[str, str]] = None


# ============================================================================
# Complete Network Topology
# ============================================================================


class NetworkTopology(BaseModel, frozen=True):
    """Complete network topology structure."""

    buses: Dict[BusID, BusInfo] = Field(description="All buses in the network")
    lines: Dict[LineID, LineInfo] = Field(description="All lines in the network")
    transformers: Dict[TransformerID, TransformerInfo] = Field(
        default_factory=dict, description="All transformers in the network"
    )
    generators: Dict[GeneratorID, GeneratorInfo] = Field(
        description="All generators in the network"
    )
    loads: Dict[LoadID, LoadInfo] = Field(description="All loads in the network")
    storage: Dict[StorageID, StorageInfo] = Field(
        default_factory=dict, description="All storage units in the network"
    )

    # Metadata
    name: str = Field(description="Network name")
    base_mva: float = Field(gt=0, description="Base MVA for per-unit calculations")
    frequency_hz: float = Field(gt=0, description="System frequency in Hz")
