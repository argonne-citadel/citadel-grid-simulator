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
from flask import Flask, Response, jsonify, send_from_directory

from .metrics import get_prometheus_metrics

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
        host: str = "0.0.0.0",  # nosec B104 — containerized web API; bind-to-all required for docker port mapping
        port: int = 8080,
        authorization_wrapper=None,
    ) -> None:
        """
        Initialize visualization API.

        Args:
            simulator: GridSimulator instance
            host: IP address to bind to (default: 0.0.0.0)
            port: TCP port to listen on (default: 8080)
            authorization_wrapper: Optional AuthorizationWrapper for Zero-Trust
        """
        self.simulator = simulator
        self.host = host
        self.port = port
        self.auth_wrapper = authorization_wrapper

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

        # Register control API blueprint
        self._register_control_api()

        logger.info(f"GridVisualizationAPI initialized on {host}:{port}")

    def _register_routes(self):  # noqa: C901
        """Register Flask routes."""

        @self.app.route("/")
        def index():
            """Serve the main visualization page."""
            return send_from_directory("static", "index.html")

        @self.app.route("/control")
        def control():
            """Serve the HMI/SCADA control panel."""
            return send_from_directory("static", "control.html")

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

        @self.app.route("/reset", methods=["POST"])
        def reset():
            """Reset grid engine to initial state with stochastic load variation.

            Delegates to the engine's reset() method, which is engine-agnostic.
            PandaPowerEngine.reset() requires reset_fn at construction (provided
            by main.py). OpenDSSEngine.reset() recompiles the circuit and fuzzes
            loads.  Both engines run a power flow before returning so the state
            is valid immediately after this call.

            The simulation loop is stopped before resetting and restarted after.
            OpenDSS uses global C-library state that is not thread-safe — calling
            reset() while the simulation thread is mid-Solve() causes a crash.
            Stopping the loop first eliminates the race.
            """
            was_running = self.simulator.running
            if was_running:
                self.simulator.stop()
            try:
                self.simulator.engine.reset()
                pinned_hour = getattr(self.simulator.engine, "_pinned_hour", None)
                logger.info("Grid engine reset via /reset (hour=%s)", pinned_hour)
                return jsonify(
                    {
                        "status": "ok",
                        "message": "Grid reset to initial state",
                        "simulation_hour": pinned_hour,
                    }
                )
            except Exception as e:
                logger.error(f"Error resetting grid: {e}")
                return jsonify({"error": str(e)}), 500
            finally:
                if was_running:
                    self.simulator.start(threaded=True)

        @self.app.route("/topology")
        def get_topology():
            """Get static network topology for zone partitioning and graph analysis.

            Returns bus/line/generator/storage connectivity without power-flow
            results.  Consumers (e.g. data-init) use this to derive zone
            assignments via BFS without importing any simulator library.

            Response schema:
              buses:      {bus_id: {bus_type: str}}
              lines:      {line_id: {from_bus: int, to_bus: int}}
              generators: {gen_id:  {bus: int}}
              storage:    {stor_id: {bus: int}}
            """
            try:
                topology = self.simulator.get_cached_topology()
                if topology is None:
                    return jsonify({"error": "Topology not cached yet"}), 503
                return jsonify(
                    {
                        "buses": {
                            str(bus_id): {"bus_type": info.bus_type}
                            for bus_id, info in topology.buses.items()
                        },
                        "lines": {
                            str(line_id): {
                                "from_bus": info.from_bus,
                                "to_bus": info.to_bus,
                            }
                            for line_id, info in topology.lines.items()
                        },
                        "generators": {
                            str(gen_id): {"bus": info.bus}
                            for gen_id, info in topology.generators.items()
                        },
                        "storage": {
                            str(stor_id): {"bus": info.bus}
                            for stor_id, info in topology.storage.items()
                        },
                    }
                )
            except Exception as e:
                logger.error(f"Error generating topology: {e}")
                return jsonify({"error": str(e)}), 500

        @self.app.route("/health")
        def health():
            """Health check endpoint."""
            return jsonify(
                {"status": "ok", "simulator_running": self.simulator.running}
            )

        @self.app.route("/metrics")
        def metrics():
            """Prometheus metrics endpoint."""
            metrics_data, content_type = get_prometheus_metrics()
            return Response(metrics_data, mimetype=content_type)

    def _register_control_api(self):
        """Register HMI control API blueprint."""
        try:
            from .hmi_control_api import HMIControlAPI

            control_api = HMIControlAPI(
                simulator=self.simulator,
                authorization_wrapper=self.auth_wrapper,
            )
            self.app.register_blueprint(control_api.blueprint)
            logger.info("HMI Control API registered")
        except ImportError as e:
            logger.warning(f"HMI Control API not available: {e}")

    def _generate_topology(self) -> Dict[str, Any]:  # noqa: C901, PLR0915
        """
        Generate topology coordinates using generic engine interface.

        Returns:
            Dictionary with x, y coordinates for buses, lines, generators, loads, and storage
        """
        # Use cached topology (thread-safe, no engine calls)
        topology = self.simulator.get_cached_topology()
        if topology is None:
            raise RuntimeError(
                "Topology not cached — call simulator.cache_topology() first"
            )

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

        # Generate layout — prefer geographic coordinates from the engine
        # (BusCoords.csv for OpenDSS models like IEEE 8500).  Falls back to
        # NetworkX computed layout for models without coordinates.
        bus_coords = getattr(self.simulator.engine, "_bus_coords", {})
        if len(bus_coords) > len(topology.buses) * 0.3:
            # Use geographic coordinates — normalize to plot scale
            xs = [c[0] for c in bus_coords.values()]
            ys = [c[1] for c in bus_coords.values()]
            cx, cy = sum(xs) / len(xs), sum(ys) / len(ys)
            span = max(max(xs) - min(xs), max(ys) - min(ys), 1.0)
            scale = 200.0 / span

            import random as _rng

            _rng.seed(42)

            # First pass: place buses with known geographic coords
            pos = {}
            unplaced_buses = []
            for bus_id in topology.buses.keys():
                key = f"bus_{bus_id}"
                if int(bus_id) in bus_coords:
                    bx, by = bus_coords[int(bus_id)]
                    pos[key] = ((bx - cx) * scale, (by - cy) * scale)
                else:
                    unplaced_buses.append(key)

            # BFS propagation: place uncoordinated buses near their nearest
            # coordinated neighbor.  Repeats until all buses are placed or
            # no progress is made (disconnected components default to 0,0).
            remaining = set(unplaced_buses)
            while remaining:
                placed_this_round = []
                for key in remaining:
                    for neighbor in G.neighbors(key):
                        if neighbor in pos:
                            nx_, ny_ = pos[neighbor]
                            pos[key] = (
                                nx_ + _rng.uniform(-0.3, 0.3),
                                ny_ + _rng.uniform(-0.3, 0.3),
                            )
                            placed_this_round.append(key)
                            break
                if not placed_this_round:
                    # No progress — place remaining at (0,0)
                    for key in remaining:
                        pos[key] = (0.0, 0.0)
                    break
                remaining -= set(placed_this_round)

            # Place generators, loads, storage near their bus with small offset
            for node in G.nodes():
                if node in pos:
                    continue
                for neighbor in G.neighbors(node):
                    if neighbor in pos:
                        bx, by = pos[neighbor]
                        pos[node] = (bx + _rng.uniform(-1, 1), by + _rng.uniform(-1, 1))
                        break
                else:
                    pos[node] = (0.0, 0.0)

            logger.info(
                "Using geographic bus coordinates for layout (%d/%d buses)",
                len(bus_coords),
                len(topology.buses),
            )
        else:
            # Computed layout — Kamada-Kawai for small, spring for large
            try:
                if G.number_of_nodes() > 500:
                    pos = nx.spring_layout(G, k=2, iterations=20, seed=42, scale=100)
                else:
                    initial_pos = nx.spring_layout(G, seed=42)
                    pos = nx.kamada_kawai_layout(G, pos=initial_pos, scale=100)
            except Exception:
                # Deliberate visualization fallback: kamada_kawai fails on
                # disconnected graphs; spring_layout always works. Logged at
                # warning so the fallback isn't silent.
                logger.warning(
                    "kamada_kawai_layout failed; falling back to spring_layout",
                    exc_info=True,
                )
                pos = nx.spring_layout(G, k=2, iterations=20, seed=42, scale=100)

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
                    "tap_position": trafo_info.tap_position,
                    "tap_min": trafo_info.tap_min,
                    "tap_max": trafo_info.tap_max,
                }
            )

        # Shunt data (capacitors/reactors placed at bus position with small offset)
        shunts = []
        for shunt_id in sorted(topology.shunts.keys()):
            shunt_info = topology.shunts[shunt_id]
            bus_id = shunt_info.bus

            bus_idx = sorted(topology.buses.keys()).index(bus_id)
            shunt_x = bus_coords_x[bus_idx] + 3
            shunt_y = bus_coords_y[bus_idx] + 3

            shunts.append(
                {
                    "id": int(shunt_id),
                    "name": shunt_info.name,
                    "bus_id": int(bus_id),
                    "x": shunt_x,
                    "y": shunt_y,
                    "q_mvar": shunt_info.q_mvar,
                    "p_mw": shunt_info.p_mw,
                    "in_service": shunt_info.in_service,
                }
            )

        # Switch data (placed as short segments between bus and element)
        switch_coords = []
        sorted_bus_keys = sorted(topology.buses.keys())
        for switch_id in sorted(topology.switches.keys()):
            sw_info = topology.switches[switch_id]

            # Only render bus-to-bus switches (et="b") — line/trafo switches
            # are implicitly shown by the line/trafo being in/out of service.
            if sw_info.element_type != "b":
                continue

            bus_a = sw_info.bus
            bus_b_id = sw_info.element

            bus_b_key = int(bus_b_id)
            if bus_a in topology.buses and bus_b_key in topology.buses:
                a_idx = sorted_bus_keys.index(bus_a)
                b_idx = sorted_bus_keys.index(bus_b_key)
                switch_coords.append(
                    {
                        "id": int(switch_id),
                        "name": sw_info.name,
                        "bus": int(bus_a),
                        "element": bus_b_id,
                        "x": [bus_coords_x[a_idx], bus_coords_x[b_idx]],
                        "y": [bus_coords_y[a_idx], bus_coords_y[b_idx]],
                        "closed": sw_info.closed,
                        "switch_type": sw_info.switch_type,
                    }
                )

        return {
            "buses": {"x": bus_coords_x, "y": bus_coords_y, "metadata": bus_metadata},
            "lines": line_coords,
            "transformers": transformer_coords,
            "generators": generators,
            "loads": loads,
            "storage": storage_units,
            "shunts": shunts,
            "switches": switch_coords,
        }

    def _get_state_data(self) -> Dict[str, Any]:  # noqa: C901, PLR0915
        """
        Get current grid state with topology.

        Returns:
            Dictionary with version, topology, and current state
        """
        with self._lock:
            # Use cached topology (populated by simulator.cache_topology() in
            # the main thread before the simulation loop starts).  Never call
            # engine.get_topology() from the Flask thread — OpenDSS global
            # C-library state is not thread-safe.
            if self._topology_cache is None:
                self._topology_cache = self._generate_topology()

            # NetworkTopology object for line/bus iteration in state assembly
            topology = self.simulator.get_cached_topology()

            # Update dynamic data in cached topology (generator output, storage SOC/power)
            state = self.simulator.get_current_state()
            if state and self._topology_cache:
                if "generators" in self._topology_cache:
                    for gen_unit in self._topology_cache["generators"]:
                        gen_id = gen_unit["id"]
                        if gen_id in state.generators:
                            gen_unit["p_kw"] = state.generators[gen_id].p_mw * 1000
                if "storage" in self._topology_cache:
                    for storage_unit in self._topology_cache["storage"]:
                        storage_id = storage_unit["id"]
                        if storage_id in state.storage:
                            storage_state = state.storage[storage_id]
                            storage_unit["soc_percent"] = storage_state.soc_percent
                            storage_unit["p_kw"] = storage_state.p_mw * 1000

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

            # Extract bus voltages and phase angles from GridState
            bus_voltages = []
            bus_angles = []
            for bus_id in sorted(state.buses.keys()):
                voltage = state.buses[bus_id].voltage_pu
                angle = state.buses[bus_id].angle_deg
                # Handle NaN values
                if np.isnan(voltage):
                    voltage = 0.0
                if np.isnan(angle):
                    angle = 0.0
                bus_voltages.append(float(voltage))
                bus_angles.append(float(angle))

            # Extract line status, loading, and power flows from GridState
            line_status = []
            line_loading = []
            line_p_mw = []
            line_q_mvar = []

            for line_id in sorted(topology.lines.keys()):
                # Get status from topology
                line_info = topology.lines[line_id]
                line_status.append(line_info.in_service)

                # Get loading and power from state
                if line_id in state.lines:
                    line_state = state.lines[line_id]
                    loading = line_state.loading_percent
                    p_from = line_state.p_from_mw
                    q_from = line_state.q_from_mvar
                    if pd.isna(loading):
                        loading = 0.0
                    if pd.isna(p_from):
                        p_from = 0.0
                    if pd.isna(q_from):
                        q_from = 0.0
                    line_loading.append(float(loading))
                    line_p_mw.append(float(p_from))
                    line_q_mvar.append(float(q_from))
                else:
                    line_loading.append(0.0)
                    line_p_mw.append(0.0)
                    line_q_mvar.append(0.0)

            # Extract generator/DER output power
            generator_power = []
            for gen_id in sorted(state.generators.keys()):
                gen_state = state.generators[gen_id]
                p_mw = gen_state.p_mw if not np.isnan(gen_state.p_mw) else 0.0
                q_mvar = gen_state.q_mvar if not np.isnan(gen_state.q_mvar) else 0.0
                generator_power.append(
                    {
                        "id": int(gen_id),
                        "p_mw": float(p_mw),
                        "q_mvar": float(q_mvar),
                    }
                )

            # Extract load power
            load_power = []
            for load_id in sorted(state.loads.keys()):
                load_state = state.loads[load_id]
                p_mw = load_state.p_mw if not np.isnan(load_state.p_mw) else 0.0
                q_mvar = load_state.q_mvar if not np.isnan(load_state.q_mvar) else 0.0
                load_power.append(
                    {
                        "id": int(load_id),
                        "p_mw": float(p_mw),
                        "q_mvar": float(q_mvar),
                    }
                )

            # System totals (from GridState which computes them properly)
            total_generation_mw = (
                float(state.total_generation_mw)
                if state.total_generation_mw is not None
                and not np.isnan(state.total_generation_mw)
                else 0.0
            )
            total_load_mw = (
                float(state.total_load_mw)
                if state.total_load_mw is not None and not np.isnan(state.total_load_mw)
                else 0.0
            )
            total_losses_mw = (
                float(state.total_losses_mw)
                if state.total_losses_mw is not None
                and not np.isnan(state.total_losses_mw)
                else 0.0
            )

            # Frequency from network (pandapower nominal)
            frequency_hz = float(
                topology.frequency_hz if topology.frequency_hz else 60.0
            )

            return {
                "ver": self._topology_version,
                "topology": self._topology_cache,
                "state": {
                    "bus_voltage": bus_voltages,
                    "bus_angle": bus_angles,
                    "line_status": line_status,
                    "line_loading": line_loading,
                    "line_p_mw": line_p_mw,
                    "line_q_mvar": line_q_mvar,
                    "generator_power": generator_power,
                    "load_power": load_power,
                    "total_generation_mw": total_generation_mw,
                    "total_load_mw": total_load_mw,
                    "total_losses_mw": total_losses_mw,
                    "ext_grid_import_mw": float(state.ext_grid_import_mw),
                    "frequency_hz": frequency_hz,
                    "converged": self.simulator.last_step_converged,
                    "sim_time": self.simulator.current_time,
                    "simulation_hour": getattr(
                        self.simulator.engine, "_pinned_hour", None
                    ),
                    # Non-zero means some bus results were NaN/inf-substituted.
                    # Distinct from converged=False (global failure); this is
                    # bus-level partial non-convergence granularity.
                    "nan_bus_count": state.nan_bus_count,
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
