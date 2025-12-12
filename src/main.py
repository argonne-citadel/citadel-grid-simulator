"""Main entry point for the Grid Simulator."""

import logging
import signal
import sys
import asyncio
import argparse
import json
from pathlib import Path
from typing import List, Dict, Any

import pandapower as pp

from .models import DickertLVModel
from .models.factory import create_simulation
from .simulator import GridSimulator
from . import settings

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

    def __call__(self, state):
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


async def run_with_protocols(
    simulator,
    enable_dnp3=True,
    enable_modbus=True,
    enable_web=True,
    dnp3_port=20000,
    modbus_port=502,
    web_port=8080,
):
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
            dnp3 = GridDNP3Outstation(simulator, host="0.0.0.0", port=dnp3_port)
            dnp3.start()
            protocol_servers.append(("DNP3", dnp3))
            logger.info(f"DNP3 outstation started on port {dnp3_port}")

        # Initialize Modbus server
        modbus = None
        if enable_modbus:
            logger.info(f"Initializing Modbus TCP server on port {modbus_port}...")
            modbus = GridModbusServer(
                simulator.engine, host="0.0.0.0", port=modbus_port
            )
            # Modbus server runs in async context
            modbus_task = asyncio.create_task(modbus.start())
            protocol_servers.append(("Modbus", modbus))
            logger.info(f"Modbus TCP server started on port {modbus_port}")

        # Initialize web visualization API
        if enable_web:
            logger.info(f"Initializing web visualization API on port {web_port}...")
            web_api = GridVisualizationAPI(simulator, host="0.0.0.0", port=web_port)
            web_thread = web_api.run_async()
            protocol_servers.append(("Web API", web_api))
            logger.info(f"Web visualization API started on port {web_port}")

        # Add callback to update Modbus server with each simulation step
        if modbus:

            def update_modbus_callback(state):
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
        if enable_modbus:
            await modbus_task
        else:
            # Just wait indefinitely
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


def export_topology(net):
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

        # Export lines
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

    except Exception as e:
        logger.error(f"Failed to export topology: {e}")


def export_topology_from_schema(topology):
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

        # Export lines
        for line in topology.lines.values():
            topology_data["lines"].append(
                {
                    "index": int(line.line_id),
                    "name": line.name,
                    "from_bus": int(line.from_bus),
                    "to_bus": int(line.to_bus),
                    "length_km": line.length_km if hasattr(line, "length_km") else 0.0,
                    "in_service": True,
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
                    "in_service": True,
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

    except Exception as e:
        logger.error(f"Failed to export topology from schema: {e}")
        import traceback

        traceback.print_exc()


def _add_der_portfolio(grid_model: DickertLVModel) -> None:
    """Add a diverse DER portfolio to the grid for zero-trust testing.

    Distribution-level DER focused on DOE CESER cybersecurity research:
    - Rooftop/Commercial Solar: 10-500 kW (typical distributed solar)
    - Small Wind: 10-100 kW (small distributed wind)
    - Residential/Commercial Loads: 5-500 kW (distribution-appropriate)
    - Battery Storage: 10-200 kW (distributed energy storage)
    """
    net = grid_model.get_network()

    # Get available buses (exclude slack bus 0)
    load_buses = [int(idx) for idx in net.load.bus.unique() if idx != 0]

    if len(load_buses) == 0:
        logger.warning("No load buses found for DER placement")
        return

    logger.info("=" * 80)
    logger.info("Adding Realistic DER Portfolio for Zero-Trust Research")
    logger.info("=" * 80)

    # Step 0: Keep original base loads and generators, add DER on top
    logger.info("\nStep 0: Keeping original base loads and generators...")
    num_original_loads = len(net.load)
    num_original_gens = len(net.sgen) if hasattr(net, "sgen") else 0
    original_load_total = net.load.p_mw.sum()
    original_gen_total = net.sgen.p_mw.sum() if num_original_gens > 0 else 0.0
    logger.info(
        f"  Keeping {num_original_loads} original base loads ({original_load_total*1000:.0f} kW)"
    )
    logger.info(
        f"  Keeping {num_original_gens} original base generators ({original_gen_total*1000:.0f} kW)"
    )
    # Do NOT drop loads or generators

    # Step 1: Add additional distribution-level loads on top of original loads
    logger.info("\nStep 1: Adding additional distribution-level loads...")

    # Get all non-slack buses for potential load placement
    all_buses = [int(idx) for idx in net.bus.index if idx != 0]  # Exclude slack bus

    # Commercial buildings with rooftop solar potential: 50-120 kW (moderate increase)
    commercial_buses = [all_buses[i] for i in [4, 7, 12, 19] if i < len(all_buses)]
    for i, bus in enumerate(commercial_buses[:2]):  # Only 2 commercial loads
        load_mw = 0.05 + (i * 0.07)  # 0.05-0.12 MW (50-120 kW)
        pp.create_load(
            net,
            bus=bus,
            p_mw=load_mw,
            q_mvar=load_mw * 0.25,  # Power factor ~0.97
            name=f"Commercial_{bus}",
        )
        logger.info(
            f"  Added commercial building at bus {bus}: {load_mw:.3f} MW ({load_mw*1000:.0f} kW)"
        )

    # Small industrial/warehouse: 30-50 kW (light industrial)
    industrial_buses = [all_buses[i] for i in [1, 10, 15] if i < len(all_buses)]
    for i, bus in enumerate(industrial_buses[:1]):  # Only 1 industrial load
        load_mw = 0.03 + (i * 0.02)  # 0.03-0.05 MW (30-50 kW)
        pp.create_load(
            net,
            bus=bus,
            p_mw=load_mw,
            q_mvar=load_mw * 0.2,  # Power factor ~0.98
            name=f"Industrial_{bus}",
        )
        logger.info(
            f"  Added small industrial at bus {bus}: {load_mw:.3f} MW ({load_mw*1000:.0f} kW)"
        )

    # Step 2: Add distribution-level DER (sized to complement original generation)
    logger.info("\nStep 2: Adding distribution-level DER...")

    # Rooftop/Commercial Solar PV: Size based on grid location
    # Larger PV near grid tie (low bus numbers), smaller at far ends to avoid voltage spikes
    logger.info("  Adding rooftop/commercial solar PV...")
    # Distribute PV across grid with sizes inversely proportional to distance from grid tie
    pv_config = [
        (2, 0.10),  # Bus 2: 100 kW (close to grid tie)
        (5, 0.08),  # Bus 5: 80 kW
        (8, 0.06),  # Bus 8: 60 kW
        (11, 0.05),  # Bus 11: 50 kW
        (14, 0.08),  # Bus 14: 80 kW
        (17, 0.03),  # Bus 17: 30 kW (far from grid tie)
        (20, 0.02),  # Bus 20: 20 kW (farthest from grid tie)
    ]

    for bus_idx, p_mw in pv_config:
        if bus_idx < len(all_buses):
            bus = all_buses[bus_idx]
            grid_model.add_der(bus=bus, p_mw=p_mw, q_mvar=0.0, der_type="PV")
            logger.info(
                f"    Solar PV at bus {bus}: {p_mw:.3f} MW ({p_mw*1000:.0f} kW)"
            )

    # Small wind turbines: 15-50 kW (smaller to complement original)
    logger.info("  Adding small wind turbines...")
    wind_buses = [all_buses[i] for i in [3, 7, 11, 18] if i < len(all_buses)]
    for i, bus in enumerate(wind_buses[:3]):  # Reduced from 5 to 3
        p_mw = 0.015 + (i * 0.015)  # 0.015-0.045 MW (15-45 kW)
        grid_model.add_der(bus=bus, p_mw=p_mw, q_mvar=0.0, der_type="Wind")
        logger.info(
            f"    Wind turbine at bus {bus}: {p_mw:.3f} MW ({p_mw*1000:.0f} kW)"
        )

    # Step 3: Add distributed battery storage: 10-50 kW (smaller to complement original)
    # BESS will dynamically charge/discharge based on grid net load
    logger.info("\nStep 3: Adding distributed battery storage...")
    bess_buses = [all_buses[i] for i in [2, 9, 14] if i < len(all_buses)]
    for i, bus in enumerate(bess_buses[:2]):  # Reduced from 3 to 2
        p_mw_capacity = 0.01 + (i * 0.02)  # 0.01-0.03 MW (10-30 kW) capacity
        max_e_mwh = p_mw_capacity * 4  # 4 hours of storage
        # Start with zero power - will be controlled dynamically
        grid_model.add_storage(
            bus=bus,
            p_mw=0.0,  # Start idle, will be controlled by dynamic BESS controller
            max_e_mwh=max_e_mwh,
            soc_percent=50.0,
        )
        logger.info(
            f"    BESS at bus {bus}: {p_mw_capacity:.3f} MW ({p_mw_capacity*1000:.0f} kW) capacity, {max_e_mwh:.2f} MWh"
        )

    # Calculate actual totals
    total_load = net.load.p_mw.sum()
    total_gen = (
        net.sgen.p_mw.sum() if hasattr(net, "sgen") and len(net.sgen) > 0 else 0.0
    )
    total_bess = (
        net.storage.p_mw.sum()
        if hasattr(net, "storage") and len(net.storage) > 0
        else 0.0
    )
    total_bess_energy = (
        net.storage.max_e_mwh.sum()
        if hasattr(net, "storage") and len(net.storage) > 0
        else 0.0
    )

    # Count buses with loads and generators
    buses_with_loads = len(net.load.bus.unique())
    buses_with_gens = (
        len(net.sgen.bus.unique()) if hasattr(net, "sgen") and len(net.sgen) > 0 else 0
    )
    total_buses = len([idx for idx in net.bus.index if idx != 0])  # Exclude slack bus

    logger.info("=" * 80)
    logger.info("Distribution-Level DER Portfolio Summary:")
    logger.info(f"  Total Buses: {total_buses} (excluding slack)")
    logger.info(f"  Buses with Loads: {buses_with_loads}")
    logger.info(f"  Buses with Generators: {buses_with_gens}")
    logger.info(
        f"  Original Load: {original_load_total:.3f} MW ({original_load_total*1000:.0f} kW)"
    )
    logger.info(
        f"  Original Generation: {original_gen_total:.3f} MW ({original_gen_total*1000:.0f} kW)"
    )
    logger.info(
        f"  Total Load (with additions): {total_load:.3f} MW ({total_load*1000:.0f} kW)"
    )
    logger.info(
        f"  Total Generation (with additions): {total_gen:.3f} MW ({total_gen*1000:.0f} kW)"
    )
    logger.info(
        f"  BESS Capacity: {total_bess:.3f} MW ({total_bess*1000:.0f} kW) / {total_bess_energy:.2f} MWh"
    )
    net_balance = total_gen - total_load
    if net_balance > 0:
        logger.info(f"  Net Export: {net_balance:.3f} MW ({net_balance*1000:.0f} kW)")
    else:
        logger.info(
            f"  Net Import: {abs(net_balance):.3f} MW ({abs(net_balance)*1000:.0f} kW)"
        )
    logger.info(
        f"  Scenario: Original Dickert loads + distributed solar, wind, and battery storage"
    )
    logger.info("=" * 80)


def create_pandapower_model(model_name: str):
    """Create a PandaPower grid model.

    Args:
        model_name: Name of the model to create

    Returns:
        Grid model instance with get_network() method

    Raises:
        ValueError: If model_name is not supported
    """
    if model_name == "dickert-lv":
        logger.info("Creating Dickert LV network model...")
        grid_model = DickertLVModel(feeders_range="long", linetype="cable")

        # Add DER portfolio for zero-trust research
        _add_der_portfolio(grid_model)

        return grid_model
    else:
        raise ValueError(
            f"Unsupported PandaPower model: {model_name}. "
            f"Supported models: dickert-lv"
        )


def create_engine(engine_type: str, enable_grid_stix: bool = False, **kwargs):
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
        from .engines.pandapower_engine import PandaPowerEngine

        # PandaPower requires a network object
        if "network" not in kwargs:
            raise ValueError("PandaPower engine requires 'network' argument")

        logger.info("Creating PandaPower engine")
        return PandaPowerEngine(
            kwargs["network"],
            enable_grid_stix=enable_grid_stix,
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


def main():
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
        default="pandapower",
        help="Power system engine to use (default: pandapower)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Model to use: for PandaPower, specify model name (default: dickert-lv); for OpenDSS, specify DSS file path (default: /usr/app/examples/IEEE37Bus_PV.dss)",
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

    logger.info("=" * 80)
    logger.info(f"ZTCard Grid Simulator - {args.engine.upper()} Engine")
    logger.info("=" * 80)

    # Enable Grid-STIX if requested
    enable_stix = args.enable_grid_stix or args.export_stix is not None
    if enable_stix:
        logger.info("Grid-STIX annotation enabled")

    # Determine model based on engine and --model argument
    if args.engine == "pandapower":
        model_name = args.model if args.model else "dickert-lv"
    elif args.engine == "opendss":
        model_path = args.model if args.model else "/usr/app/examples/IEEE37Bus_PV.dss"

    # Create engine based on selected type
    try:
        if args.engine == "pandapower":
            # Create grid model for PandaPower
            grid_model = create_pandapower_model(model_name)

            topology = grid_model.get_topology_info()

            logger.info(f"Network topology:")
            logger.info(f"  - Buses: {topology['num_buses']}")
            logger.info(f"  - Lines: {topology['num_lines']}")
            logger.info(f"  - Loads: {topology['num_loads']}")
            logger.info(f"  - DERs: {topology['num_ders']}")
            logger.info(f"  - Storage: {topology['num_storage']}")

            # Get control points for zero-trust policy enforcement
            control_points = grid_model.get_control_points()
            logger.info(f"Control points available:")
            logger.info(f"  - Breakers: {len(control_points['breakers'])}")
            logger.info(f"  - Loads: {len(control_points['loads'])}")
            logger.info(f"  - DERs: {len(control_points.get('ders', []))}")

            # Export topology for MTU consumption
            logger.info("Exporting network topology...")
            export_topology(grid_model.get_network())

            # Create PandaPower engine
            engine = create_engine(
                "pandapower",
                enable_grid_stix=enable_stix,
                network=grid_model.get_network(),
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
    logger.info(f"Creating simulator...")
    simulator = GridSimulator(
        engine=engine,
        timestep_seconds=settings.TIMESTEP_SECONDS,
    )
    logger.info(f"Simulator created with {settings.TIMESTEP_SECONDS}s timestep")

    # Export Grid-STIX if requested
    if args.export_stix:
        if not enable_stix:
            logger.error("--export-stix requires --enable-grid-stix")
            return 1

        logger.info(f"Exporting Grid-STIX bundle to {args.export_stix}...")
        try:
            # Run one power flow to get initial state
            engine.run_simulation()

            # Export with telemetry
            engine.export_grid_stix(args.export_stix, include_telemetry=True)
            logger.info(f"✓ Grid-STIX bundle exported successfully")

            # If only exporting, exit after export
            if args.mode == "standalone" and args.duration is None:
                logger.info("Export complete, exiting")
                return 0
        except Exception as e:
            logger.error(f"Failed to export Grid-STIX bundle: {e}")
            return 1

    # Add state update callback
    simulator.add_state_callback(state_update_callback)

    # Set up signal handler for graceful shutdown
    def signal_handler(sig, frame):
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
