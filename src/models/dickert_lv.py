"""Dickert LV network model with DERs for zero-trust research.

This model uses pandapower's synthetic LV network generation which is specifically
designed for distribution-level DER research. It provides:
- Low voltage distribution feeders
- Distributed Energy Resources (solar PV, storage, etc.)
- Realistic topology for DER integration studies
- Manageable scale for cyber/control experimentation
"""

import logging
from typing import Any, Dict, List, Optional

import numpy as np
import pandapower as pp
import pandapower.networks as pn

logger = logging.getLogger(__name__)


class DickertLVModel:
    """
    Dickert LV distribution network model with DERs.

    This model represents a low-voltage distribution network suitable for
    DER integration and zero-trust cybersecurity research. The network includes:
    - LV distribution feeders
    - Residential/commercial loads
    - Distributed solar PV generation
    - Potential for storage and EV charging
    - Communication/control points for SCADA integration

    The smaller scale (compared to transmission networks) makes it ideal for:
    - Rapid iteration of cyber/control scenarios
    - Zero-trust policy enforcement at DER/feeder level
    - Intrusion detection and response simulation
    - DER aggregation and control studies
    """

    def __init__(self, feeders_range: str = "short", linetype: str = "cable"):
        """
        Initialize the Dickert LV network.

        Args:
            feeders_range: Feeder length - 'short', 'middle', or 'long'
            linetype: Line type - 'cable' or 'C&OHL' (cable and overhead line)
        """
        logger.info(
            f"Initializing Dickert LV model (feeders_range={feeders_range}, linetype={linetype})"
        )

        # Removed silent fallback to create_synthetic_voltage_control_lv_network
        # on Dickert creation failure — different topology, different bus/line
        # counts, all silently substituted with only an INFO log. Topology
        # drift between supposedly-identical model invocations breaks both
        # training reproducibility and attack scenarios that assume specific
        # bus indices. If create_dickert_lv_network fails, the caller needs
        # to know.
        self.net = pn.create_dickert_lv_network(
            feeders_range=feeders_range, linetype=linetype
        )

        self.feeders_range = feeders_range
        self.linetype = linetype
        self._setup_network()
        self.validate()

        logger.info(
            f"Dickert LV model initialized: {len(self.net.bus)} buses, "
            f"{len(self.net.line)} lines, {len(self.net.load)} loads, "
            f"{len(self.net.sgen) if hasattr(self.net, 'sgen') else 0} DERs"
        )

    def _setup_network(self):
        """Configure network parameters for simulation."""
        # Set network name
        self.net.name = (
            f"Dickert LV Network ({self.feeders_range} feeders, {self.linetype})"
        )

        # Ensure all lines have in_service status for breaker simulation
        if "in_service" not in self.net.line.columns:
            self.net.line["in_service"] = True

        # Add DER metadata if static generators exist
        if hasattr(self.net, "sgen") and len(self.net.sgen) > 0:
            if "type" not in self.net.sgen.columns:
                # Assume solar PV for all static generators
                self.net.sgen["type"] = "PV"
            if "controllable" not in self.net.sgen.columns:
                # Mark all DERs as controllable for zero-trust policy enforcement
                self.net.sgen["controllable"] = True

    def get_network(self) -> pp.pandapowerNet:
        """
        Get the pandapower network object.

        Returns:
            pandapower network object
        """
        return self.net

    def validate(self) -> bool:
        """
        Validate the network structure and run initial power flow.

        Returns:
            True if validation successful

        Raises:
            RuntimeError: If power flow fails
        """
        # Validate network structure
        if len(self.net.bus) == 0:
            raise RuntimeError(
                "Network has no buses — _setup_network() did not populate the pandapower net"
            )
        if len(self.net.line) == 0:
            raise RuntimeError(
                "Network has no lines — _setup_network() did not populate the pandapower net"
            )
        if len(self.net.load) == 0:
            raise RuntimeError(
                "Network has no loads — _setup_network() did not populate the pandapower net"
            )

        # Run initial power flow
        try:
            pp.runpp(self.net)
            logger.info("Initial power flow converged successfully")
        except Exception as e:
            logger.error(f"Power flow failed: {e}")
            raise RuntimeError(f"Initial power flow validation failed: {e}")

        # Check for convergence
        if not self.net.converged:
            raise RuntimeError("Power flow did not converge")

        return True

    def get_topology_info(self) -> Dict[str, Any]:
        """
        Get network topology information.

        Returns:
            Dictionary containing topology details
        """
        num_ders = len(self.net.sgen) if hasattr(self.net, "sgen") else 0
        num_storage = len(self.net.storage) if hasattr(self.net, "storage") else 0

        info = {
            "feeders_range": self.feeders_range,
            "linetype": self.linetype,
            "num_buses": len(self.net.bus),
            "num_lines": len(self.net.line),
            "num_loads": len(self.net.load),
            "num_ders": num_ders,
            "num_storage": num_storage,
            "base_mva": self.net.sn_mva,
            "buses": self.net.bus.index.tolist(),
            "load_buses": self.net.load["bus"].tolist(),
        }

        if num_ders > 0:
            info["der_buses"] = self.net.sgen["bus"].tolist()
            info["der_types"] = (
                self.net.sgen["type"].tolist()
                if "type" in self.net.sgen.columns
                else ["PV"] * num_ders
            )

        if num_storage > 0:
            info["storage_buses"] = self.net.storage["bus"].tolist()

        return info

    def get_der_info(self) -> Dict[int, Dict[str, Any]]:
        """
        Get detailed information about DERs in the network.

        Returns:
            Dictionary mapping DER index to DER details
        """
        if not hasattr(self.net, "sgen") or len(self.net.sgen) == 0:
            return {}

        der_info = {}
        for idx in self.net.sgen.index:
            der_info[idx] = {
                "bus": self.net.sgen.at[idx, "bus"],
                "p_mw": self.net.sgen.at[idx, "p_mw"],
                "q_mvar": (
                    self.net.sgen.at[idx, "q_mvar"]
                    if "q_mvar" in self.net.sgen.columns
                    else 0.0
                ),
                "type": (
                    self.net.sgen.at[idx, "type"]
                    if "type" in self.net.sgen.columns
                    else "PV"
                ),
                "controllable": (
                    self.net.sgen.at[idx, "controllable"]
                    if "controllable" in self.net.sgen.columns
                    else True
                ),
                "in_service": self.net.sgen.at[idx, "in_service"],
            }
        return der_info

    def get_control_points(self) -> Dict[str, List]:
        """
        Get control points for zero-trust policy enforcement.

        Returns:
            Dictionary of controllable elements by type
        """
        control_points: Dict[str, List] = {
            "breakers": self.net.line.index.tolist(),  # All lines can be switched
            "loads": self.net.load.index.tolist(),  # Load shedding capability
        }

        if hasattr(self.net, "sgen") and len(self.net.sgen) > 0:
            # Controllable DERs
            controllable_ders: List = self.net.sgen[
                self.net.sgen.get("controllable", True)
            ].index.tolist()
            control_points["ders"] = controllable_ders

        if hasattr(self.net, "storage") and len(self.net.storage) > 0:
            control_points["storage"] = self.net.storage.index.tolist()

        return control_points

    def add_der(
        self,
        bus: int,
        p_mw: float,
        q_mvar: float = 0.0,
        der_type: str = "PV",
        controllable: bool = True,
    ) -> int:
        """
        Add a DER to the network.

        Args:
            bus: Bus index to connect DER
            p_mw: Active power generation (MW)
            q_mvar: Reactive power generation (MVAr)
            der_type: Type of DER ('PV', 'Wind', 'Storage', etc.)
            controllable: Whether DER is controllable

        Returns:
            Index of created DER
        """
        der_idx = pp.create_sgen(
            self.net,
            bus=bus,
            p_mw=p_mw,
            q_mvar=q_mvar,
            type=der_type,
            controllable=controllable,
            name=f"{der_type}_{bus}",
        )
        logger.info(f"Added {der_type} DER at bus {bus}: {p_mw} MW")
        return int(der_idx)

    def add_storage(
        self, bus: int, p_mw: float, max_e_mwh: float, soc_percent: float = 50.0
    ) -> int:
        """
        Add energy storage to the network.

        Args:
            bus: Bus index to connect storage
            p_mw: Power capacity (MW)
            max_e_mwh: Energy capacity (MWh)
            soc_percent: Initial state of charge (%)

        Returns:
            Index of created storage
        """
        storage_idx = pp.create_storage(
            self.net,
            bus=bus,
            p_mw=p_mw,
            max_e_mwh=max_e_mwh,
            soc_percent=soc_percent,
            name=f"Storage_{bus}",
        )
        logger.info(f"Added storage at bus {bus}: {p_mw} MW, {max_e_mwh} MWh")
        return int(storage_idx)

    def _fuzz_loads(self, scale: float = 0.10) -> None:
        """Apply ±scale uniform noise to loads and DERs for run-to-run variation.

        Uses an unseeded RNG so each reset produces a genuinely distinct operating
        point. ±10% is consistent with realistic demand uncertainty on an LV feeder
        (IEC 60038 ±10% voltage tolerance implies similar load variation tolerance).
        Generators (DERs) are scaled by ±5% to reflect irradiance/wind variability.
        """
        rng = np.random.default_rng()  # unseeded — different every call

        if len(self.net.load) > 0:
            noise = 1.0 + rng.uniform(-scale, scale, size=len(self.net.load))
            self.net.load["p_mw"] = (self.net.load["p_mw"] * noise).clip(lower=0.0)
            self.net.load["q_mvar"] = (self.net.load["q_mvar"] * noise).clip(lower=0.0)
            logger.debug(
                f"Load fuzz applied: ±{scale * 100:.0f}% on {len(self.net.load)} loads"
            )

        if hasattr(self.net, "sgen") and len(self.net.sgen) > 0:
            gen_noise = 1.0 + rng.uniform(
                -scale / 2, scale / 2, size=len(self.net.sgen)
            )
            self.net.sgen["p_mw"] = (self.net.sgen["p_mw"] * gen_noise).clip(lower=0.0)
            logger.debug(
                f"DER fuzz applied: ±{scale / 2 * 100:.0f}% on {len(self.net.sgen)} DERs"
            )

    def reset(self):
        """Reset the network to initial state with stochastic load variation.

        Fuzzes loads and DERs by ±10%/±5% respectively to ensure each experimental
        run starts from a genuinely distinct operating point. Without fuzzing, every
        reset produces an identical power flow solution, making repeated-run statistics
        degenerate (σ=0 for grid metrics under GraphML which blocks all commands).
        """
        logger.info("Resetting Dickert LV model to initial state")
        # The previous reset silently substituted
        # `create_synthetic_voltage_control_lv_network()` — a different
        # topology — when create_dickert_lv_network failed. Training-data
        # prep calls reset() iteratively; unflagged topology drift between
        # runs corrupts experiment reproducibility (each run trains on a
        # different graph without anyone noticing). Let the exception
        # propagate; if the LV network can't be created, that's the
        # answer the caller needs.
        self.net = pn.create_dickert_lv_network(
            feeders_range=self.feeders_range, linetype=self.linetype
        )
        self._setup_network()
        self._fuzz_loads()
        self.validate()
