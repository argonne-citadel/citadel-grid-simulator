"""Web API for real-time grid visualization.

This module provides a lightweight HTTP API that serves the latest grid state
to a browser-based Plotly visualization. The API implements a "latest-only polling"
pattern where the UI refreshes every 5 seconds but always gets the most recent data.

Features:
- Single /state endpoint returning JSON with topology and current state
- Version tracking to detect topology changes
- Optimized for fast response with minimal payload
- Thread-safe access to simulator state

Use Cases:
- Real-time monitoring of grid simulation
- Interactive visualization with zoom/pan preservation
- Debugging grid behavior during experiments
- Demonstration and education purposes
"""

import logging
import threading
from typing import Any, Dict, Optional

import networkx as nx
import numpy as np
import pandas as pd
from flask import Flask, jsonify, send_from_directory

logger = logging.getLogger(__name__)


class GridVisualizationAPI:
    """
    Web API server for grid visualization.

    This provides a REST API endpoint that returns the latest grid state
    along with topology information for rendering in Plotly.
    """

    def __init__(
        self,
        simulator,
        host: str = "0.0.0.0",
        port: int = 8080,
    ) -> None:
        """
        Initialize visualization API.

        Args:
            simulator: GridSimulator instance
            host: IP address to bind to (default: 0.0.0.0)
            port: TCP port to listen on (default: 8080)
        """
        self.simulator = simulator
        self.host = host
        self.port = port

        # Flask app setup
        self.app = Flask(__name__)
        self.app.config["JSON_SORT_KEYS"] = False

        # Topology version tracking
        self._topology_version = 0
        self._topology_cache: Optional[Dict[str, Any]] = None
        self._cached_topology_hash: Optional[tuple] = None
        self._lock = threading.Lock()

        # Register routes
        self._register_routes()

        logger.info(f"GridVisualizationAPI initialized on {host}:{port}")

    def _register_routes(self):
        """Register Flask routes."""

        @self.app.route("/")
        def index():
            """Serve the main visualization page."""
            return send_from_directory("static", "index.html")

        @self.app.route("/static/<path:path>")
        def send_static(path):
            """Serve static files (CSS, JS, etc)."""
            return send_from_directory("static", path)

        @self.app.route("/state")
        def get_state():
            """Get current grid state endpoint."""

            try:
                state_data = self._get_state_data()
                return jsonify(state_data)
            except Exception as e:
                logger.error(f"Error generating state data: {e}")
                return jsonify({"error": str(e)}), 500

        @self.app.route("/health")
        def health():
            """Health check endpoint."""
            return jsonify(
                {"status": "ok", "simulator_running": self.simulator.running}
            )

    def _generate_topology(self) -> Dict[str, Any]:
        """
        Generate topology coordinates using generic engine interface.

        Returns:
            Dictionary with x, y coordinates for buses, lines, generators, loads, and storage
        """
        # Get topology from engine (generic interface)
        topology = self.simulator.engine.get_topology()

        # Build graph from topology for layout generation
        G = nx.Graph()

        # Add bus nodes
        for bus_id in topology.buses.keys():
            G.add_node(f"bus_{bus_id}")

        # Add line connections
        for line_id, line_info in topology.lines.items():
            G.add_edge(f"bus_{line_info.from_bus}", f"bus_{line_info.to_bus}")

        # Add generator nodes and connections
        for gen_id, gen_info in topology.generators.items():
            G.add_node(f"gen_{gen_id}")
            G.add_edge(f"gen_{gen_id}", f"bus_{gen_info.bus}")

        # Add load nodes and connections
        for load_id, load_info in topology.loads.items():
            G.add_node(f"load_{load_id}")
            G.add_edge(f"load_{load_id}", f"bus_{load_info.bus}")

        # Add storage nodes and connections
        for storage_id, storage_info in topology.storage.items():
            G.add_node(f"storage_{storage_id}")
            G.add_edge(f"storage_{storage_id}", f"bus_{storage_info.bus}")

        # Add transformer connections (transformers connect two buses)
        for trafo_id, trafo_info in topology.transformers.items():
            G.add_edge(f"bus_{trafo_info.hv_bus}", f"bus_{trafo_info.lv_bus}")

        # Generate layout using networkx
        # Use Kamada-Kawai for better layout, with fixed initial positions for determinism
        try:
            # Create deterministic initial positions using a seeded spring layout
            initial_pos = nx.spring_layout(G, seed=42)
            pos = nx.kamada_kawai_layout(G, pos=initial_pos, scale=100)
        except:
            # Fallback to spring layout if Kamada-Kawai fails
            pos = nx.spring_layout(G, k=2, iterations=50, seed=42, scale=100)

        # Extract bus coordinates
        bus_coords_x = []
        bus_coords_y = []
        bus_metadata = []

        for bus_id in sorted(topology.buses.keys()):
            bus_info = topology.buses[bus_id]
            node_key = f"bus_{bus_id}"

            if node_key in pos:
                bus_coords_x.append(pos[node_key][0])
                bus_coords_y.append(pos[node_key][1])
            else:
                # Isolated bus, place at origin
                bus_coords_x.append(0.0)
                bus_coords_y.append(0.0)

            bus_metadata.append(
                {
                    "id": int(bus_id),
                    "name": bus_info.name,
                    "vn_kv": bus_info.voltage_nominal_kv,
                }
            )

        # Line connections
        line_coords = []
        for line_id in sorted(topology.lines.keys()):
            line_info = topology.lines[line_id]
            from_bus = line_info.from_bus
            to_bus = line_info.to_bus

            # Find bus indices
            from_idx = sorted(topology.buses.keys()).index(from_bus)
            to_idx = sorted(topology.buses.keys()).index(to_bus)

            line_coords.append(
                {
                    "x": [bus_coords_x[from_idx], bus_coords_x[to_idx]],
                    "y": [bus_coords_y[from_idx], bus_coords_y[to_idx]],
                    "line_id": int(line_id),
                    "from_bus": int(from_bus),
                    "to_bus": int(to_bus),
                    "in_service": line_info.in_service,
                }
            )

        # Generator data
        generators = []
        for gen_id in sorted(topology.generators.keys()):
            gen_info = topology.generators[gen_id]
            bus_id = gen_info.bus

            # Get coordinates from layout
            gen_key = f"gen_{gen_id}"
            if gen_key in pos:
                gen_x = pos[gen_key][0]
                gen_y = pos[gen_key][1]
            else:
                # Fallback to bus position with offset
                bus_idx = sorted(topology.buses.keys()).index(bus_id)
                gen_x = bus_coords_x[bus_idx] + 5
                gen_y = bus_coords_y[bus_idx] + 5

            generators.append(
                {
                    "id": int(gen_id),
                    "name": gen_info.name,
                    "bus_id": int(bus_id),
                    "x": gen_x,
                    "y": gen_y,
                    "p_kw": gen_info.p_max_mw * 1000,  # Convert MW to kW
                    "max_p_kw": gen_info.p_max_mw * 1000,
                    "type": gen_info.der_type.value,
                }
            )

        # Load data
        loads = []
        for load_id in sorted(topology.loads.keys()):
            load_info = topology.loads[load_id]
            bus_id = load_info.bus

            # Get coordinates from layout
            load_key = f"load_{load_id}"
            if load_key in pos:
                load_x = pos[load_key][0]
                load_y = pos[load_key][1]
            else:
                # Fallback to bus position with offset
                bus_idx = sorted(topology.buses.keys()).index(bus_id)
                load_x = bus_coords_x[bus_idx] - 5
                load_y = bus_coords_y[bus_idx] - 5

            loads.append(
                {
                    "id": int(load_id),
                    "name": load_info.name,
                    "bus_id": int(bus_id),
                    "x": load_x,
                    "y": load_y,
                    "p_kw": load_info.p_mw * 1000,  # Convert MW to kW
                }
            )

        # Storage data
        storage_units = []
        for storage_id in sorted(topology.storage.keys()):
            storage_info = topology.storage[storage_id]
            bus_id = storage_info.bus

            # Get coordinates from layout
            storage_key = f"storage_{storage_id}"
            if storage_key in pos:
                storage_x = pos[storage_key][0]
                storage_y = pos[storage_key][1]
            else:
                # Fallback to bus position with offset
                bus_idx = sorted(topology.buses.keys()).index(bus_id)
                storage_x = bus_coords_x[bus_idx] + 3
                storage_y = bus_coords_y[bus_idx] - 3

            storage_units.append(
                {
                    "id": int(storage_id),
                    "name": storage_info.name,
                    "bus_id": int(bus_id),
                    "x": storage_x,
                    "y": storage_y,
                    "p_kw": 0.0,  # Will be updated from state
                    "max_e_kwh": storage_info.energy_capacity_mwh
                    * 1000,  # Convert MWh to kWh
                    "soc_percent": 50.0,  # Will be updated from state
                }
            )

        # Transformer data
        transformer_coords = []
        for trafo_id in sorted(topology.transformers.keys()):
            trafo_info = topology.transformers[trafo_id]
            hv_bus = trafo_info.hv_bus
            lv_bus = trafo_info.lv_bus

            # Find bus indices
            hv_idx = sorted(topology.buses.keys()).index(hv_bus)
            lv_idx = sorted(topology.buses.keys()).index(lv_bus)

            transformer_coords.append(
                {
                    "x": [bus_coords_x[hv_idx], bus_coords_x[lv_idx]],
                    "y": [bus_coords_y[hv_idx], bus_coords_y[lv_idx]],
                    "trafo_id": int(trafo_id),
                    "name": trafo_info.name,
                    "hv_bus": int(hv_bus),
                    "lv_bus": int(lv_bus),
                    "rated_mva": trafo_info.rated_power_mva,
                    "hv_kv": trafo_info.hv_voltage_kv,
                    "lv_kv": trafo_info.lv_voltage_kv,
                    "in_service": trafo_info.in_service,
                }
            )

        return {
            "buses": {"x": bus_coords_x, "y": bus_coords_y, "metadata": bus_metadata},
            "lines": line_coords,
            "transformers": transformer_coords,
            "generators": generators,
            "loads": loads,
            "storage": storage_units,
        }

    def _get_state_data(self) -> Dict[str, Any]:
        """
        Get current grid state with topology.

        Returns:
            Dictionary with version, topology, and current state
        """
        with self._lock:
            # Get topology from engine
            topology = self.simulator.engine.get_topology()

            # Check if topology changed (number of buses/lines)
            current_topology_hash = (
                len(topology.buses),
                len(topology.lines),
            )

            if self._topology_cache is None:
                self._topology_cache = self._generate_topology()
                self._cached_topology_hash = current_topology_hash

            elif self._cached_topology_hash != current_topology_hash:
                # Topology changed, regenerate
                self._topology_version += 1
                self._topology_cache = self._generate_topology()
                self._cached_topology_hash = current_topology_hash
                logger.info(f"Topology changed, new version: {self._topology_version}")

            # Update dynamic data in cached topology (storage SOC and power)
            state = self.simulator.get_current_state()
            if state and self._topology_cache and "storage" in self._topology_cache:
                for storage_unit in self._topology_cache["storage"]:
                    storage_id = storage_unit["id"]
                    if storage_id in state.storage:
                        storage_state = state.storage[storage_id]
                        storage_unit["soc_percent"] = storage_state.soc_percent
                        storage_unit["p_kw"] = (
                            storage_state.p_mw * 1000
                        )  # Convert MW to kW

            if state is None:
                # No state available yet
                return {
                    "ver": self._topology_version,
                    "topology": self._topology_cache,
                    "state": {
                        "bus_voltage": [],
                        "line_status": [],
                        "line_loading": [],
                        "converged": False,
                        "sim_time": 0.0,
                    },
                }

            # Extract bus voltages from GridState
            bus_voltages = []
            for bus_id in sorted(state.buses.keys()):
                voltage = state.buses[bus_id].voltage_pu
                # Handle NaN values
                if np.isnan(voltage):
                    voltage = 0.0
                bus_voltages.append(float(voltage))

            # Extract line status and loading from GridState
            line_status = []
            line_loading = []

            for line_id in sorted(topology.lines.keys()):
                # Get status from topology
                line_info = topology.lines[line_id]
                line_status.append(line_info.in_service)

                # Get loading from state
                if line_id in state.lines:
                    loading = state.lines[line_id].loading_percent
                    # Handle NaN or missing values
                    if pd.isna(loading):
                        loading = 0.0
                    line_loading.append(float(loading))
                else:
                    line_loading.append(0.0)

            return {
                "ver": self._topology_version,
                "topology": self._topology_cache,
                "state": {
                    "bus_voltage": bus_voltages,
                    "line_status": line_status,
                    "line_loading": line_loading,
                    "converged": state.converged,
                    "sim_time": self.simulator.current_time,
                },
            }

    def run(self, debug: bool = False):
        """
        Run the Flask development server (blocking).

        Args:
            debug: Enable Flask debug mode
        """
        logger.info(f"Starting web API on http://{self.host}:{self.port}")
        self.app.run(host=self.host, port=self.port, debug=debug, threaded=True)

    def run_async(self):
        """Run the Flask server in a background thread (non-blocking)."""
        thread = threading.Thread(target=self.run, daemon=True)
        thread.start()
        logger.info("Web API started in background thread")
        return thread
