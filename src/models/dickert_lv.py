"""Dickert LV network model with DERs for zero-trust research.

This model uses pandapower's synthetic LV network generation which is specifically
designed for distribution-level DER research. It provides:
- Low voltage distribution feeders
- Distributed Energy Resources (solar PV, storage, etc.)
- Realistic topology for DER integration studies
- Manageable scale for cyber/control experimentation
"""

import pandapower as pp
import pandapower.networks as pn
from typing import Dict, Any, Optional, List
import logging

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

        try:
            self.net = pn.create_dickert_lv_network(
                feeders_range=feeders_range, linetype=linetype
            )
        except Exception as e:
            logger.error(f"Failed to create Dickert LV network: {e}")
            logger.info("Falling back to synthetic voltage control LV network")
            # Fallback to alternative synthetic LV network
            self.net = pn.create_synthetic_voltage_control_lv_network()

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
        assert len(self.net.bus) > 0, "Network has no buses"
        assert len(self.net.line) > 0, "Network has no lines"
        assert len(self.net.load) > 0, "Network has no loads"

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

    def reset(self):
        """Reset the network to initial state."""
        logger.info("Resetting Dickert LV model to initial state")
        try:
            self.net = pn.create_dickert_lv_network(
                feeders_range=self.feeders_range, linetype=self.linetype
            )
        except Exception:
            self.net = pn.create_synthetic_voltage_control_lv_network()
        self._setup_network()
        self.validate()
