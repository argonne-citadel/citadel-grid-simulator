"""Baran-Wu IEEE 33-bus distribution feeder model for zero-trust research.

Implements the widely-cited Baran & Wu (1989) 33-bus radial MV distribution
feeder at 12.66 kV, validated against ANSI C84.1 Range A voltage limits
(0.95–1.05 pu). This replaces the Dickert LV model, which produced
pre-existing voltage violations under North American limits and non-physical
DC-fallback angles after breaker trips on the LV radial topology.

Public interface:
    BaranWu33Model()         — create and validate network with DERs
    get_network()            — return pandapower net for engine use
    reset()                  — rebuild fresh from case33bw, apply fuzz
    _fuzz_loads(scale)       — apply ±scale noise (called by web_api reset)
    get_topology_info()      — dict with bus/line/DER counts
    get_der_info()           — dict mapping sgen index → DER metadata
    get_control_points()     — dict of controllable element indices

Design decisions:
    - No fallback constructor: if case33bw() fails, raise immediately.
    - Slack bus (bus 0) has no voltage limits; ext_grid fixes that reference.
    - All 6 sgens have explicit max_p_mw to survive zero-setpoint attacks
      (without this, set_generator_setpoint validation fails post-zeroing).
    - Fuzz ±5% loads, ±3% DERs — calibrated so NR converges reliably and
      voltages stay within ANSI limits across all fuzz realizations.
    - validate() asserts zero ANSI violations at nominal; any violation is a
      hard error because it means attack-phase voltage metrics are meaningless
      (can't distinguish attack impact from pre-existing violations).
    - Substation voltage set to 1.05 pu (see _setup_network docstring for
      full rationale). This is the primary mechanism that achieves ANSI
      compliance at nominal load — DER placement alone is insufficient.

Zone split (must match helpers.rego RBAC zones and Neo4j CONTROL edges):
    Zone A — RTU-001: lines 0–17, buses 0–18
    Zone B — RTU-002: lines 18–31, buses 19–32

DER sgen indices (assigned sequentially; case33bw has no base sgens):
    0: PV   bus 4,  0.200 MW  der-pv-0   Zone A
    1: PV   bus 8,  0.150 MW  der-pv-1   Zone A
    2: Wind bus 12, 0.100 MW  der-wind-0 Zone A
    3: PV   bus 20, 0.200 MW  der-pv-3   Zone B
    4: PV   bus 24, 0.150 MW  der-pv-4   Zone B
    5: Wind bus 28, 0.100 MW  der-wind-1 Zone B

Storage indices:
    0: BESS bus 15, 0.250 MW / 1.0 MWh  storage-0  Zone A
    1: BESS bus 31, 0.250 MW / 1.0 MWh  storage-1  Zone B

Modbus HR mapping: 10000 + sgen_index → DER setpoint register.
"""

import logging
from typing import Any, Dict, List

import numpy as np
import pandapower as pp
import pandapower.networks as pn

logger = logging.getLogger(__name__)


# ANSI C84.1 Range A primary distribution voltage limits
_V_MAX_PU = 1.05
_V_MIN_PU = 0.95

# DER and storage configuration — bus indices verified against case33bw topology.
# Zone A buses (4, 8, 12, 15) lie on lines 0–17 (RTU-001 zone).
# Zone B buses (20, 24, 28, 31) lie on lines 18–31 (RTU-002 zone).
_DER_CONFIG: List[Dict[str, Any]] = [
    {"bus": 4, "p_mw": 0.200, "type": "PV", "name": "der-pv-0"},
    {"bus": 8, "p_mw": 0.150, "type": "PV", "name": "der-pv-1"},
    {"bus": 12, "p_mw": 0.100, "type": "Wind", "name": "der-wind-0"},
    {"bus": 20, "p_mw": 0.200, "type": "PV", "name": "der-pv-3"},
    {"bus": 24, "p_mw": 0.150, "type": "PV", "name": "der-pv-4"},
    {"bus": 28, "p_mw": 0.100, "type": "Wind", "name": "der-wind-1"},
]

_STORAGE_CONFIG: List[Dict[str, Any]] = [
    {"bus": 15, "p_mw": 0.250, "max_e_mwh": 1.0, "name": "storage-0"},
    {"bus": 31, "p_mw": 0.250, "max_e_mwh": 1.0, "name": "storage-1"},
]


class BaranWu33Model:
    """
    Baran-Wu IEEE 33-bus MV distribution feeder with DERs.

    The base case33bw() network (12.66 kV, ~3.7 MW load, 32 lines) converges
    cleanly under NR at nominal loading with no voltage violations. Six DER
    units (~30% penetration) and two BESS units are added symmetrically
    across both RTU operational zones.

    After construction, the network is guaranteed to converge (NR) with
    zero ANSI C84.1 voltage violations at nominal operating point.
    """

    def __init__(self) -> None:
        """Initialize Baran-Wu 33-bus model with DERs and validate."""
        logger.info("Initializing Baran-Wu 33-bus model...")
        self.net = pn.case33bw()
        self._setup_network()
        self._add_ders()
        self.validate()
        logger.info(
            "Baran-Wu 33 model initialized: "
            f"{len(self.net.bus)} buses, "
            f"{len(self.net.line)} lines, "
            f"{len(self.net.load)} loads, "
            f"{len(self.net.sgen)} DERs, "
            f"{len(self.net.storage)} storage"
        )

    def _setup_network(self) -> None:
        """Configure substation voltage, ANSI limits, thermal limits, and service status.

        Substation voltage (ext_grid vm_pu = 1.05 pu):
            The default case33bw() sets the slack at 1.0 pu, which produces
            16 ANSI C84.1 violations at nominal load (worst bus: 0.926 pu).
            This is not a DER placement problem — it is a modeling artifact.
            In real North American distribution systems, the substation LV bus
            is held at the upper end of ANSI Range A (1.03–1.05 pu) so that
            voltage drop along the feeder keeps end customers within range.
            This practice is codified in ANSI C84.1 Table 1 (Range A service
            voltage 0.95–1.05 pu) and implemented via LTC transformer tap
            settings or AVR set-points at the substation.

            Setting vm_pu = 1.05 pu was verified by power flow:
              - Baseline violations: 16 (worst bus 0.926 pu, slack = 1.00)
              - After fix:            0 (worst bus 0.980 pu, slack = 1.05)
            This eliminates pre-existing violations so that attack-phase voltage
            metrics (Δvm_pu) reflect only the cyber event, not a standing deficit.

            Alternative considered: shunt capacitor banks (the Baran-Wu paper's
            own application). Rejected because capacitor placement introduces
            additional model parameters that would need validation and complicates
            the attack scenarios (capacitors can mask voltage changes from breaker
            trips). The substation tap approach is cleaner and more defensible.
        """
        self.net.name = "Baran-Wu IEEE 33-Bus Distribution Feeder"

        # Substation LV bus held at 1.05 pu — see docstring for full rationale.
        # Verified: clears all 16 baseline ANSI violations, worst bus = 0.980 pu.
        self.net.ext_grid.at[0, "vm_pu"] = 1.05

        # Apply ANSI C84.1 Range A limits on all non-slack buses.
        # Slack bus (0) voltage is fixed by ext_grid; setting limits there
        # is meaningless and would cause spurious assertion failures.
        non_slack = self.net.bus.index[self.net.bus.index != 0]
        self.net.bus.loc[non_slack, "max_vm_pu"] = _V_MAX_PU
        self.net.bus.loc[non_slack, "min_vm_pu"] = _V_MIN_PU

        # Set realistic thermal current limits for 12.66 kV MV distribution lines.
        # case33bw() defaults max_i_ka=99999 (unconstrained), which produces
        # loading_percent ≈ 0 for all lines despite multi-MW power flows.
        # 0.25 kA corresponds to a medium MV conductor and gives line 0
        # (3.53 MW, ~0.161 kA) approximately 64% loading at base case — typical
        # for a loaded MV feeder with N-1 security margin.
        # All BW33 lines use the same conductor type (resistance varies only due
        # to differing segment lengths), so a uniform limit is physically correct.
        self.net.line["max_i_ka"] = 0.25

        if "in_service" not in self.net.line.columns:
            self.net.line["in_service"] = True

    def _add_ders(self) -> None:
        """Add 6 DER sgens and 2 BESS storage units.

        Sgen indices are assigned sequentially starting at 0 because
        case33bw() has no base sgens. Each sgen gets an explicit max_p_mw
        equal to its nominal capacity so that set_generator_setpoint()
        can validate setpoints correctly after a zero-setpoint attack
        (without max_p_mw, the capacity check reads 0.0 and cannot be
        verified against nominal).
        """
        for cfg in _DER_CONFIG:
            idx = pp.create_sgen(
                self.net,
                bus=cfg["bus"],
                p_mw=cfg["p_mw"],
                q_mvar=0.0,
                type=cfg["type"],
                controllable=True,
                name=cfg["name"],
                max_p_mw=cfg["p_mw"],
            )
            logger.info(
                f"  sgen[{idx}] {cfg['name']} @ bus {cfg['bus']}: "
                f"{cfg['p_mw'] * 1000:.0f} kW {cfg['type']}"
            )

        for cfg in _STORAGE_CONFIG:
            idx = pp.create_storage(
                self.net,
                bus=cfg["bus"],
                p_mw=0.0,  # Start idle; controlled dynamically
                max_e_mwh=cfg["max_e_mwh"],
                soc_percent=50.0,
                name=cfg["name"],
            )
            logger.info(
                f"  storage[{idx}] {cfg['name']} @ bus {cfg['bus']}: "
                f"{cfg['p_mw'] * 1000:.0f} kW capacity / {cfg['max_e_mwh']} MWh"
            )

    def _fuzz_loads(self, scale: float = 0.05) -> None:
        """Apply ±scale uniform noise to loads and DERs for run-to-run variation.

        Uses an unseeded RNG so each reset produces a genuinely distinct
        operating point, enabling meaningful repeated-measures statistics.
        Scale is smaller than Dickert's ±10% because case33bw starts at a
        valid NR-convergent operating point — large fuzz risks pushing
        end-of-feeder buses outside ANSI limits.
        """
        rng = np.random.default_rng()  # unseeded — different every call

        if len(self.net.load) > 0:
            noise = 1.0 + rng.uniform(-scale, scale, size=len(self.net.load))
            self.net.load["p_mw"] = (self.net.load["p_mw"] * noise).clip(lower=0.0)
            self.net.load["q_mvar"] = (self.net.load["q_mvar"] * noise).clip(lower=0.0)
            logger.debug(
                f"Load fuzz applied: ±{scale * 100:.0f}% on {len(self.net.load)} loads"
            )

        # DERs fuzzed at ±3% (60% of load scale) to reflect irradiance/wind variability
        der_scale = scale * 0.6
        if hasattr(self.net, "sgen") and len(self.net.sgen) > 0:
            gen_noise = 1.0 + rng.uniform(
                -der_scale, der_scale, size=len(self.net.sgen)
            )
            self.net.sgen["p_mw"] = (self.net.sgen["p_mw"] * gen_noise).clip(lower=0.0)
            logger.debug(
                f"DER fuzz applied: ±{der_scale * 100:.1f}% on {len(self.net.sgen)} DERs"
            )

    def validate(self) -> bool:
        """Run NR power flow and assert ANSI compliance at the nominal operating point.

        Zero violations are required — not just logged. Pre-existing violations
        make attack-phase voltage metrics (Δvm_pu, violation count delta)
        uninterpretable: you cannot distinguish cyber-event impact from a
        standing network deficit. The substation tap setting (vm_pu = 1.05)
        in _setup_network() is what achieves compliance; if this raises, the
        tap setting or DER configuration has regressed.

        Raises:
            RuntimeError: Power flow did not converge (islanded section, bad data).
            RuntimeError: One or more buses outside ANSI C84.1 Range A at nominal
                load — indicates _setup_network() regression.
        """
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

        pp.runpp(self.net, algorithm="nr", max_iteration=50)

        if not self.net.converged:
            raise RuntimeError(
                "Baran-Wu 33 nominal power flow did not converge — "
                "check topology (islanded section?) or DER sizing."
            )

        non_slack_v = self.net.res_bus.loc[
            self.net.bus.index[self.net.bus.index != 0], "vm_pu"
        ]
        violations = int(((non_slack_v > _V_MAX_PU) | (non_slack_v < _V_MIN_PU)).sum())
        if violations > 0:
            raise RuntimeError(
                f"Nominal operating point has {violations} ANSI Range A violations "
                f"[{_V_MIN_PU}, {_V_MAX_PU}] pu — worst bus: {non_slack_v.min():.4f} pu. "
                f"Check ext_grid vm_pu setting in _setup_network() (expected 1.05 pu). "
                f"Pre-existing violations make attack-phase voltage metrics meaningless."
            )

        logger.info(
            f"Validation passed: NR converged, 0 ANSI violations, "
            f"V=[{non_slack_v.min():.4f}, {non_slack_v.max():.4f}] pu"
        )
        return True

    def reset(self) -> None:
        """Rebuild network from scratch with stochastic load variation.

        Reconstructs the full case33bw() network, re-adds DERs, fuzzes
        loads, and validates so each experimental run starts from a
        genuinely distinct operating point. Without fuzzing, repeated-run
        statistics are degenerate (σ=0 for grid metrics under GraphML
        which blocks all commands).
        """
        logger.info("Resetting Baran-Wu 33 model to initial state")
        self.net = pn.case33bw()
        self._setup_network()
        self._add_ders()
        self._fuzz_loads()
        self.validate()

    def reset_network(self) -> pp.pandapowerNet:
        """Create a fresh fuzzed network and return it.

        Callable by PandaPowerEngine as reset_fn. Delegates to reset() which
        rebuilds case33bw(), re-adds DERs, fuzzes loads, and validates via NR
        power flow. Returns self.net so the engine can install it without
        holding a reference to the model object.
        """
        self.reset()
        return self.net

    def get_network(self) -> pp.pandapowerNet:
        """Return the pandapower network object."""
        return self.net

    def get_topology_info(self) -> Dict[str, Any]:
        """Return network metadata dict."""
        num_ders = len(self.net.sgen) if hasattr(self.net, "sgen") else 0
        num_storage = len(self.net.storage) if hasattr(self.net, "storage") else 0

        info: Dict[str, Any] = {
            "num_buses": len(self.net.bus),
            "num_lines": len(self.net.line),
            "num_loads": len(self.net.load),
            "num_ders": num_ders,
            "num_storage": num_storage,
            "base_mva": float(self.net.sn_mva),
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
        """Return dict mapping sgen index → DER metadata."""
        if not hasattr(self.net, "sgen") or len(self.net.sgen) == 0:
            return {}

        der_info: Dict[int, Dict[str, Any]] = {}
        for idx in self.net.sgen.index:
            der_info[int(idx)] = {
                "bus": int(self.net.sgen.at[idx, "bus"]),
                "p_mw": float(self.net.sgen.at[idx, "p_mw"]),
                "q_mvar": float(
                    self.net.sgen.at[idx, "q_mvar"]
                    if "q_mvar" in self.net.sgen.columns
                    else 0.0
                ),
                "type": (
                    str(self.net.sgen.at[idx, "type"])
                    if "type" in self.net.sgen.columns
                    else "PV"
                ),
                "controllable": bool(
                    self.net.sgen.at[idx, "controllable"]
                    if "controllable" in self.net.sgen.columns
                    else True
                ),
                "in_service": bool(self.net.sgen.at[idx, "in_service"]),
            }
        return der_info

    def get_control_points(self) -> Dict[str, List[int]]:
        """Return controllable element indices for zero-trust enforcement."""
        control_points: Dict[str, List[int]] = {
            "breakers": self.net.line.index.tolist(),
            "loads": self.net.load.index.tolist(),
        }

        if hasattr(self.net, "sgen") and len(self.net.sgen) > 0:
            controllable_ders: List[int] = self.net.sgen[
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
        """Add a DER to the network (for ad-hoc experimental additions)."""
        der_idx = pp.create_sgen(
            self.net,
            bus=bus,
            p_mw=p_mw,
            q_mvar=q_mvar,
            type=der_type,
            controllable=controllable,
            name=f"{der_type}_{bus}",
            max_p_mw=p_mw,
        )
        logger.info(f"Added {der_type} DER at bus {bus}: {p_mw * 1000:.0f} kW")
        return int(der_idx)

    def add_storage(
        self, bus: int, p_mw: float, max_e_mwh: float, soc_percent: float = 50.0
    ) -> int:
        """Add energy storage to the network."""
        storage_idx = pp.create_storage(
            self.net,
            bus=bus,
            p_mw=p_mw,
            max_e_mwh=max_e_mwh,
            soc_percent=soc_percent,
            name=f"Storage_{bus}",
        )
        logger.info(
            f"Added storage at bus {bus}: {p_mw * 1000:.0f} kW, {max_e_mwh} MWh"
        )
        return int(storage_idx)
