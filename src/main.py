"""Main entry point for the Grid Simulator."""

import argparse
import asyncio
import json
import logging
import signal
import sys
from pathlib import Path
from typing import Any, Dict, List

from . import settings
from .engines.base import PowerSystemEngine
from .models import BaranWu33Model
from .models.factory import create_simulation
from .schemas.topology import NetworkTopology
from .simulator import GridSimulator

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)

logger = logging.getLogger(__name__)


class StateLogger:
    """Throttled state logger to avoid excessive log output."""

    def __init__(self, log_interval: int = 10):
        """Initialize with logging interval in steps.

        Args:
            log_interval: Log every N steps (default: 10 = every 10 seconds at 1s timestep)
        """
        self.counter = 0
        self.log_interval = log_interval

    def __call__(self, state: Any) -> None:
        """Callback for state updates.

        Args:
            state: GridState object from simulator
        """
        from .schemas.state import GridState

        self.counter += 1

        # Only log every N seconds
        if (
            isinstance(state, GridState)
            and state.converged
            and self.counter % self.log_interval == 0
        ):
            num_buses = len(state.buses)
            avg_voltage = (
                sum(bus.voltage_pu for bus in state.buses.values()) / num_buses
                if num_buses > 0
                else 0
            )
            logger.info(
                f"Sim time: {state.timestamp:.1f}s | Buses: {num_buses} | Avg voltage: {avg_voltage:.3f} pu"
            )


# Create state logger instance (log every 100 steps = 10 seconds at 0.1s timestep)
state_update_callback = StateLogger(log_interval=100)


async def run_with_protocols(  # noqa: C901, PLR0915
    simulator: GridSimulator,
    enable_dnp3: bool = True,
    enable_modbus: bool = True,
    enable_web: bool = True,
    dnp3_port: int = 20000,
    modbus_port: int = 502,
    web_port: int = 8080,
) -> None:
    """Run simulator with SCADA protocol servers."""
    # Import protocol modules only when needed
    from typing import Any

    from .protocols.dnp3_outstation import GridDNP3Outstation
    from .protocols.modbus_server import GridModbusServer
    from .web_api import GridVisualizationAPI

    protocol_servers: list[tuple[str, Any]] = []

    try:
        # Initialize DNP3 outstation
        if enable_dnp3:
            logger.info(f"Initializing DNP3 outstation on port {dnp3_port}...")
            dnp3 = GridDNP3Outstation(simulator.engine, host="0.0.0.0", port=dnp3_port)  # nosec B104 — containerized DNP3 outstation; bind-to-all required for docker port mapping
            dnp3.start()
            protocol_servers.append(("DNP3", dnp3))
            logger.info(f"DNP3 outstation started on port {dnp3_port}")

        # Initialize Modbus server
        modbus = None
        if enable_modbus:
            logger.info(f"Initializing Modbus TCP server on port {modbus_port}...")
            modbus = GridModbusServer(
                simulator.engine,
                host="0.0.0.0",  # nosec B104 — containerized Modbus server; bind-to-all required for docker port mapping
                port=modbus_port,
                engine_lock=simulator.engine_lock,
                engine_lock_timeout_s=settings.TIMESTEP_SECONDS,
            )
            # Modbus server runs in async context
            modbus_task = asyncio.create_task(modbus.start())
            protocol_servers.append(("Modbus", modbus))
            logger.info(f"Modbus TCP server started on port {modbus_port}")

        # Initialize web visualization API
        if enable_web:
            logger.info(f"Initializing web visualization API on port {web_port}...")
            web_api = GridVisualizationAPI(simulator, host="0.0.0.0", port=web_port)  # nosec B104 — containerized web API; bind-to-all required for docker port mapping
            web_thread = web_api.run_async()
            protocol_servers.append(("Web API", web_api))
            logger.info(f"Web visualization API started on port {web_port}")

        # Add callback to update Modbus server with each simulation step
        if modbus:

            def update_modbus_callback(state: Any) -> None:
                """Update Modbus registers with latest grid state."""
                try:
                    modbus.update_measurements()
                except Exception as e:
                    logger.error(f"Error updating Modbus server: {e}")

            simulator.add_state_callback(update_modbus_callback)
            logger.info("Modbus server will be updated with each simulation step")

        # Start simulator in threaded mode
        logger.info("Starting grid simulator in background...")
        simulator.start(duration_seconds=None, threaded=True)
        logger.info("Grid simulator running")

        logger.info("-" * 80)
        logger.info("SCADA Protocol Servers Active:")
        if enable_dnp3:
            logger.info(f"  - DNP3 Outstation: 0.0.0.0:{dnp3_port}")
        if enable_modbus:
            logger.info(f"  - Modbus TCP: 0.0.0.0:{modbus_port}")
        if enable_web:
            logger.info(f"  - Web Visualization: http://0.0.0.0:{web_port}")
        logger.info("Press Ctrl+C to stop")
        logger.info("-" * 80)

        # Keep running until interrupted
        # Don't await modbus_task - let it run in background
        # Just wait indefinitely to keep the event loop alive
        await asyncio.Event().wait()

    except asyncio.CancelledError:
        logger.info("Protocol servers cancelled")
    finally:
        # Stop protocol servers
        for name, server in protocol_servers:
            try:
                if hasattr(server, "stop"):
                    server.stop()
                logger.info(f"Stopped {name} server")
            except Exception as e:
                logger.error(f"Error stopping {name} server: {e}")


def export_topology(net: Any) -> None:
    """Export pandapower network topology to JSON for MTU consumption.

    Args:
        net: pandapower network object
    """
    try:
        # Create shared data directory
        shared_dir = Path("/shared")
        shared_dir.mkdir(exist_ok=True)

        topology_data: Dict[str, Any] = {
            "buses": [],
            "lines": [],
            "transformers": [],
            "metadata": {
                "network_name": net.name,
                "base_mva": float(net.sn_mva) if hasattr(net, "sn_mva") else 1.0,
                "exported_at": str(Path(__file__).stat().st_mtime),
            },
        }

        # Export buses
        for idx in net.bus.index:
            bus_data = net.bus.loc[idx]
            topology_data["buses"].append(
                {
                    "index": int(idx),
                    "name": str(bus_data.get("name", f"Bus {idx}")),
                    "vn_kv": float(bus_data["vn_kv"]),
                    "in_service": bool(bus_data.get("in_service", True)),
                    "type": str(bus_data.get("type", "b")),
                }
            )

        # Export lines (including impedance parameters for embedding features)
        for idx in net.line.index:
            line_data = net.line.loc[idx]
            topology_data["lines"].append(
                {
                    "index": int(idx),
                    "name": str(line_data.get("name", f"Line {idx}")),
                    "from_bus": int(line_data["from_bus"]),
                    "to_bus": int(line_data["to_bus"]),
                    "length_km": float(line_data["length_km"]),
                    "std_type": str(line_data.get("std_type", "Unknown")),
                    "max_i_ka": float(line_data.get("max_i_ka", 0.0)),
                    "in_service": bool(line_data.get("in_service", True)),
                    "r_ohm_per_km": float(line_data.get("r_ohm_per_km", 0.0)),
                    "x_ohm_per_km": float(line_data.get("x_ohm_per_km", 0.0)),
                    "c_nf_per_km": float(line_data.get("c_nf_per_km", 0.0)),
                }
            )

        # Export transformers if present
        if hasattr(net, "trafo") and len(net.trafo) > 0:
            for idx in net.trafo.index:
                trafo_data = net.trafo.loc[idx]
                topology_data["transformers"].append(
                    {
                        "index": int(idx),
                        "name": str(trafo_data.get("name", f"Transformer {idx}")),
                        "hv_bus": int(trafo_data["hv_bus"]),
                        "lv_bus": int(trafo_data["lv_bus"]),
                        "sn_mva": float(trafo_data["sn_mva"]),
                        "vn_hv_kv": float(trafo_data["vn_hv_kv"]),
                        "vn_lv_kv": float(trafo_data["vn_lv_kv"]),
                        "in_service": bool(trafo_data.get("in_service", True)),
                        "vk_percent": float(trafo_data.get("vk_percent", 0.0)),
                        "vkr_percent": float(trafo_data.get("vkr_percent", 0.0)),
                    }
                )

        # Export loads
        topology_data["loads"] = []
        for idx in net.load.index:
            load = net.load.loc[idx]
            topology_data["loads"].append(
                {
                    "index": int(idx),
                    "name": str(load.get("name", f"Load {idx}")),
                    "bus": int(load["bus"]),
                    "p_mw": float(load["p_mw"]),
                    "q_mvar": float(load["q_mvar"]),
                    "in_service": bool(load.get("in_service", True)),
                }
            )

        # Export generators (sgens / DERs)
        topology_data["generators"] = []
        for idx in net.sgen.index:
            sgen = net.sgen.loc[idx]
            topology_data["generators"].append(
                {
                    "index": int(idx),
                    "name": str(sgen.get("name", f"sgen {idx}")),
                    "bus": int(sgen["bus"]),
                    "p_mw": float(sgen["p_mw"]),
                    "max_p_mw": float(sgen.get("max_p_mw", sgen["p_mw"])),
                    "type": str(sgen.get("type", "PV")),
                    "in_service": bool(sgen.get("in_service", True)),
                }
            )

        # Export storage
        topology_data["storage"] = []
        if hasattr(net, "storage") and len(net.storage) > 0:
            for idx in net.storage.index:
                stor = net.storage.loc[idx]
                topology_data["storage"].append(
                    {
                        "index": int(idx),
                        "name": str(stor.get("name", f"Storage {idx}")),
                        "bus": int(stor["bus"]),
                        "max_e_mwh": float(stor.get("max_e_mwh", 0.0)),
                        "p_mw": float(stor.get("p_mw", 0.0)),
                        "in_service": bool(stor.get("in_service", True)),
                    }
                )

        # Write to shared file
        topology_file = shared_dir / "grid_topology.json"
        with open(topology_file, "w") as f:
            json.dump(topology_data, f, indent=2)

        logger.info(f"✓ Topology exported to {topology_file}")
        logger.info(f"  - {len(topology_data['buses'])} buses")
        logger.info(f"  - {len(topology_data['lines'])} lines")
        logger.info(f"  - {len(topology_data['transformers'])} transformers")
        logger.info(f"  - {len(topology_data['loads'])} loads")
        logger.info(f"  - {len(topology_data['generators'])} generators")
        logger.info(f"  - {len(topology_data['storage'])} storage units")

    except Exception as e:
        logger.error(f"Failed to export topology: {e}")


def export_topology_from_schema(topology: NetworkTopology) -> None:
    """Export topology from Topology schema object to JSON for MTU consumption.

    Args:
        topology: Topology object from engine.get_topology()
    """
    try:
        # Create shared data directory
        shared_dir = Path("/shared")
        shared_dir.mkdir(exist_ok=True)

        topology_data: Dict[str, Any] = {
            "buses": [],
            "lines": [],
            "transformers": [],
            "metadata": {
                "network_name": topology.name,
                "base_mva": topology.base_mva,
                "exported_at": str(Path(__file__).stat().st_mtime),
            },
        }

        # Export buses
        for bus in topology.buses.values():
            topology_data["buses"].append(
                {
                    "index": int(bus.bus_id),
                    "name": bus.name,
                    "vn_kv": bus.voltage_nominal_kv,
                    "in_service": True,
                    "type": "b",
                }
            )

        # Export lines (including impedance parameters for embedding features).
        # LineInfo stores total ohm/nf values (not per-km); exported as-is since
        # length_km is not available in the schema — the MTU treats them identically.
        for line in topology.lines.values():
            topology_data["lines"].append(
                {
                    "index": int(line.line_id),
                    "name": line.name,
                    "from_bus": int(line.from_bus),
                    "to_bus": int(line.to_bus),
                    "length_km": line.length_km if hasattr(line, "length_km") else 0.0,
                    "max_i_ka": line.max_current_ka,
                    "in_service": True,
                    "r_ohm_per_km": line.resistance_ohm,
                    "x_ohm_per_km": line.reactance_ohm,
                    "c_nf_per_km": line.capacitance_nf,
                }
            )

        # Export transformers
        for trafo in topology.transformers.values():
            topology_data["transformers"].append(
                {
                    "index": int(trafo.transformer_id),
                    "name": trafo.name,
                    "hv_bus": int(trafo.hv_bus),
                    "lv_bus": int(trafo.lv_bus),
                    "sn_mva": trafo.rated_power_mva,
                    "vn_hv_kv": trafo.hv_voltage_kv,
                    "vn_lv_kv": trafo.lv_voltage_kv,
                    "in_service": True,
                    "vk_percent": trafo.reactance_percent,
                    "vkr_percent": trafo.resistance_percent,
                }
            )

        # Export loads
        topology_data["loads"] = []
        for load in topology.loads.values():
            topology_data["loads"].append(
                {
                    "index": int(load.load_id),
                    "name": load.name,
                    "bus": int(load.bus),
                    "p_mw": load.p_mw,
                    "q_mvar": load.q_mvar,
                    "in_service": load.in_service,
                }
            )

        # Export generators (DERs)
        topology_data["generators"] = []
        for gen in topology.generators.values():
            topology_data["generators"].append(
                {
                    "index": int(gen.generator_id),
                    "name": gen.name,
                    "bus": int(gen.bus),
                    "p_mw": gen.p_max_mw,
                    "max_p_mw": gen.p_max_mw,
                    "type": gen.der_type.value if gen.der_type else "PV",
                    "in_service": gen.in_service,
                }
            )

        # Export storage
        topology_data["storage"] = []
        for stor in topology.storage.values():
            topology_data["storage"].append(
                {
                    "index": int(stor.storage_id),
                    "name": stor.name,
                    "bus": int(stor.bus),
                    "max_e_mwh": stor.energy_capacity_mwh,
                    "p_mw": stor.p_max_mw,
                    "in_service": stor.in_service,
                }
            )

        # Write to shared file
        topology_file = shared_dir / "grid_topology.json"
        with open(topology_file, "w") as f:
            json.dump(topology_data, f, indent=2)

        logger.info(f"✓ Topology exported to {topology_file}")
        logger.info(f"  - {len(topology_data['buses'])} buses")
        logger.info(f"  - {len(topology_data['lines'])} lines")
        logger.info(f"  - {len(topology_data['transformers'])} transformers")
        logger.info(f"  - {len(topology_data['loads'])} loads")
        logger.info(f"  - {len(topology_data['generators'])} generators")
        logger.info(f"  - {len(topology_data['storage'])} storage units")

    except Exception as e:
        logger.error(f"Failed to export topology from schema: {e}")
        import traceback

        traceback.print_exc()


def create_pandapower_model(model_name: str) -> Any:
    """Create a PandaPower grid model.

    DER placement is handled internally by each model class; no
    separate portfolio injection step is needed.

    Args:
        model_name: Name of the model to create
            ("baran-wu-33" or "cyderms-composite")

    Returns:
        Grid model instance with get_network() method

    Raises:
        ValueError: If model_name is not supported
    """
    if model_name == "baran-wu-33":
        logger.info("Creating Baran-Wu 33-bus network model...")
        return BaranWu33Model()
    elif model_name == "cyderms-composite":
        logger.info("Creating CyDERMS composite grid model...")
        from .models.cyderms_composite import CyDERMSCompositeModel

        return CyDERMSCompositeModel()
    else:
        raise ValueError(
            f"Unsupported PandaPower model: {model_name}. "
            f"Supported models: baran-wu-33, cyderms-composite"
        )


def create_engine(
    engine_type: str, enable_grid_stix: bool = False, **kwargs: Any
) -> PowerSystemEngine:
    """Factory function to create power system engines.

    Args:
        engine_type: Type of engine ('pandapower', 'opendss', etc.)
        enable_grid_stix: Whether to enable Grid-STIX annotation
        **kwargs: Engine-specific arguments

    Returns:
        PowerSystemEngine instance

    Raises:
        ValueError: If engine_type is not supported
    """
    if engine_type == "pandapower":
        from .engines.der_profiles import StochasticDERProfile
        from .engines.pandapower_engine import PandaPowerEngine

        # PandaPower requires a network object
        if "network" not in kwargs:
            raise ValueError("PandaPower engine requires 'network' argument")

        # Stochastic DER profile: quasi-static time-series PV and wind output.
        # Solar base = IEEE37Bus_PV.dss MyIrrad loadshape; wind = diurnal profile.
        # Default start 08:00 Jan 1 2024 (irrad=0.3, non-zero morning generation).
        der_profile = StochasticDERProfile()

        logger.info("Creating PandaPower engine with stochastic DER profile")
        return PandaPowerEngine(
            kwargs["network"],
            reset_fn=kwargs.get("reset_fn"),
            enable_grid_stix=enable_grid_stix,
            der_profile=der_profile,
        )

    elif engine_type == "opendss":
        from .engines.opendss_engine import OpenDSSEngine

        # OpenDSS requires a DSS file path
        if "dss_file" not in kwargs:
            raise ValueError("OpenDSS engine requires 'dss_file' argument")

        logger.info(f"Creating OpenDSS engine with file: {kwargs['dss_file']}")
        return OpenDSSEngine(kwargs["dss_file"])

    else:
        raise ValueError(
            f"Unsupported engine type: {engine_type}. "
            f"Supported types: pandapower, opendss"
        )


def main() -> int:  # noqa: C901, PLR0915
    """Main function to run the grid simulator."""
    # Parse command line arguments
    parser = argparse.ArgumentParser(description="ZTCard Grid Simulator")
    parser.add_argument(
        "--mode",
        choices=["standalone", "scada"],
        default="standalone",
        help="Run mode: standalone (no protocols) or scada (with DNP3/Modbus)",
    )
    parser.add_argument(
        "--engine",
        choices=["pandapower", "opendss"],
        default=None,
        help=(
            "Power system engine to use. If omitted, inferred from --model: "
            "'.dss' suffix → opendss, otherwise → pandapower."
        ),
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help=(
            "Model to load. PandaPower: model name (default: baran-wu-33). "
            "OpenDSS: path to .dss file (default: /usr/app/examples/IEEE37Bus_PV.dss). "
            "Engine is inferred from this value when --engine is not provided."
        ),
    )
    parser.add_argument(
        "--dnp3-port",
        type=int,
        default=settings.DEFAULT_DNP3_PORT,
        help=f"DNP3 outstation port (default: {settings.DEFAULT_DNP3_PORT})",
    )
    parser.add_argument(
        "--modbus-port",
        type=int,
        default=settings.DEFAULT_MODBUS_PORT,
        help=f"Modbus TCP port (default: {settings.DEFAULT_MODBUS_PORT})",
    )
    parser.add_argument(
        "--no-dnp3", action="store_true", help="Disable DNP3 outstation"
    )
    parser.add_argument(
        "--no-modbus", action="store_true", help="Disable Modbus server"
    )
    parser.add_argument(
        "--duration",
        type=int,
        default=None,
        help="Simulation duration in seconds (default: run indefinitely)",
    )
    parser.add_argument(
        "--enable-grid-stix",
        action="store_true",
        help="Enable Grid-STIX annotation for telemetry data",
    )
    parser.add_argument(
        "--export-stix",
        type=str,
        default=None,
        help="Export Grid-STIX bundle to specified file path",
    )
    args = parser.parse_args()

    if not args.model:
        raise RuntimeError(
            "GRID_MODEL is not set. Set it in .env (e.g. GRID_MODEL=baran-wu-33 "
            "or GRID_MODEL=/usr/app/examples/IEEE37Bus_PV.dss)."
        )

    # Infer engine from model value when --engine is not explicitly supplied.
    # *.dss suffix → opendss; anything else → pandapower.
    if args.engine is None:
        args.engine = "opendss" if args.model.lower().endswith(".dss") else "pandapower"
        logger.info("Engine inferred from model '%s': %s", args.model, args.engine)

    logger.info("=" * 80)
    logger.info(f"ZTCard Grid Simulator - {args.engine.upper()} Engine")
    logger.info("=" * 80)

    # Remove any stale topology file before initializing the engine.  The MTU
    # polls for this file's existence; if a file from a previous run (possibly
    # a different topology) is already on disk, the MTU reads it immediately
    # and publishes the wrong bus/line set.  Deleting it here forces the MTU
    # to wait until the fresh file is written after engine initialization.
    stale_topology = Path("/shared/grid_topology.json")
    if stale_topology.exists():
        stale_topology.unlink()
        logger.info(
            "Removed stale /shared/grid_topology.json (MTU will wait for fresh file)"
        )

    # Enable Grid-STIX if requested
    enable_stix = args.enable_grid_stix or args.export_stix is not None
    if enable_stix:
        logger.info("Grid-STIX annotation enabled")

    if args.engine == "pandapower":
        model_name = args.model
    elif args.engine == "opendss":
        model_path = args.model

    # Create engine based on selected type
    grid_model = None  # only set for pandapower; reactor callback check guards on this
    try:
        if args.engine == "pandapower":
            # Create grid model for PandaPower
            grid_model = create_pandapower_model(model_name)

            topology = grid_model.get_topology_info()

            logger.info("Network topology:")
            logger.info(f"  - Buses: {topology['num_buses']}")
            logger.info(f"  - Lines: {topology['num_lines']}")
            logger.info(f"  - Loads: {topology['num_loads']}")
            logger.info(f"  - DERs: {topology['num_ders']}")
            logger.info(f"  - Storage: {topology['num_storage']}")

            # Get control points for zero-trust policy enforcement
            control_points = grid_model.get_control_points()
            logger.info("Control points available:")
            logger.info(f"  - Breakers: {len(control_points['breakers'])}")
            logger.info(f"  - Loads: {len(control_points['loads'])}")
            logger.info(f"  - DERs: {len(control_points.get('ders', []))}")

            # Export topology for MTU consumption
            logger.info("Exporting network topology...")
            export_topology(grid_model.get_network())

            # Create PandaPower engine; reset_fn enables the /reset endpoint to
            # rebuild the network without knowing the model type.
            engine = create_engine(
                "pandapower",
                enable_grid_stix=enable_stix,
                network=grid_model.get_network(),
                reset_fn=grid_model.reset_network,
            )

        elif args.engine == "opendss":
            # Create OpenDSS engine
            logger.info(f"Loading OpenDSS circuit from: {model_path}")

            # Verify DSS file exists
            dss_path = Path(model_path)
            if not dss_path.exists():
                logger.error(f"DSS file not found: {model_path}")
                return 1

            engine = create_engine(
                "opendss",
                enable_grid_stix=enable_stix,
                dss_file=model_path,
            )

            # Log OpenDSS circuit info
            topology = engine.get_topology()
            logger.info(f"OpenDSS circuit loaded: {topology.name}")
            logger.info(f"  - Buses: {len(topology.buses)}")
            logger.info(f"  - Lines: {len(topology.lines)}")
            logger.info(f"  - Loads: {len(topology.loads)}")
            logger.info(f"  - Generators: {len(topology.generators)}")

            # Export topology for MTU consumption
            logger.info("Exporting OpenDSS network topology...")
            export_topology_from_schema(topology)

        else:
            logger.error(f"Unsupported engine: {args.engine}")
            return 1

    except Exception as e:
        logger.error(f"Failed to create engine: {e}")
        import traceback

        traceback.print_exc()
        return 1

    # Create simulator
    logger.info("Creating simulator...")
    simulator = GridSimulator(
        engine=engine,
        timestep_seconds=settings.TIMESTEP_SECONDS,
    )
    logger.info(f"Simulator created with {settings.TIMESTEP_SECONDS}s timestep")

    # Cache topology in the main thread before starting the simulation loop.
    # The web API reads from this cache instead of calling engine.get_topology()
    # directly, avoiding OpenDSS thread-safety issues on large models.
    simulator.cache_topology()

    # Register reactor state callback for CyDERMS composite models.
    # The callback steps ReactorTransientModel instances each timestep and
    # queues ReactorCommand instances to update gen table entries.
    # create_pandapower_model() returns different, unrelated model classes
    # depending on model_name (BaranWu33Model has no reactor support), so
    # this capability is looked up dynamically rather than through a shared
    # typed interface -- same getattr pattern as the engine-capability
    # checks in modbus_server.py / simulator.py.
    create_reactor_callback = getattr(grid_model, "create_reactor_callback", None)
    if create_reactor_callback is not None:
        reactor_cb = create_reactor_callback(simulator)
        simulator.add_state_callback(reactor_cb)
        logger.info(
            "Reactor state callback registered: %d reactors",
            len(getattr(grid_model, "reactors", [])),
        )

    # Export Grid-STIX if requested
    if args.export_stix:
        if not enable_stix:
            logger.error("--export-stix requires --enable-grid-stix")
            return 1

        logger.info(f"Exporting Grid-STIX bundle to {args.export_stix}...")
        try:
            # Run one power flow to get initial state
            engine.run_simulation()

            # export_grid_stix is a PandaPowerEngine-only capability, not
            # part of the PowerSystemEngine base interface (OpenDSSEngine
            # doesn't implement it) -- same getattr/fail-loud pattern as
            # the engine-capability checks above and in modbus_server.py.
            export_grid_stix = getattr(engine, "export_grid_stix", None)
            if export_grid_stix is None:
                raise TypeError(
                    f"Engine {type(engine).__name__} does not support "
                    "--export-stix (missing export_grid_stix)"
                )

            # Export with telemetry
            export_grid_stix(args.export_stix, include_telemetry=True)
            logger.info("✓ Grid-STIX bundle exported successfully")

            # If only exporting, exit after export
            if args.mode == "standalone" and args.duration is None:
                logger.info("Export complete, exiting")
                return 0
        except Exception as e:
            logger.error(f"Failed to export Grid-STIX bundle: {e}")
            return 1

    # Add state update callback
    simulator.add_state_callback(state_update_callback)

    # Set up signal handler for graceful shutdown.
    # signal.signal handlers must accept (signum, frame); the `_frame` argument
    # is required by the stdlib signature but not consumed here.
    def signal_handler(sig: int, _frame: Any) -> None:
        logger.info("Received shutdown signal")
        simulator.stop()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Run based on mode
    if args.mode == "scada":
        logger.info("Starting in SCADA mode with protocol servers...")
        try:
            asyncio.run(
                run_with_protocols(
                    simulator,
                    enable_dnp3=not args.no_dnp3,
                    enable_modbus=not args.no_modbus,
                    dnp3_port=args.dnp3_port,
                    modbus_port=args.modbus_port,
                )
            )
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        except Exception as e:
            logger.error(f"Error in SCADA mode: {e}")
            return 1
        finally:
            simulator.stop()
    else:
        # Standalone mode
        logger.info("Starting in standalone mode (no protocol servers)...")
        logger.info("Starting simulation (1-second timestep)...")
        logger.info("Press Ctrl+C to stop")
        logger.info("-" * 80)

        try:
            simulator.start(duration_seconds=args.duration, threaded=False)
        except KeyboardInterrupt:
            logger.info("Interrupted by user")
        except Exception as e:
            logger.error(f"Simulation error: {e}")
            return 1
        finally:
            simulator.stop()

    # Print statistics
    stats = simulator.get_statistics()
    logger.info("-" * 80)
    logger.info("Simulation Statistics:")
    logger.info(f"  - Total steps: {stats['total_steps']}")
    logger.info(f"  - Failed steps: {stats['failed_steps']}")
    logger.info(f"  - Commands processed: {stats['commands_processed']}")
    logger.info(f"  - Average step time: {stats['average_step_time']:.4f}s")

    return 0


if __name__ == "__main__":
    sys.exit(main())
