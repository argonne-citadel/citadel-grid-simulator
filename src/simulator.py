"""Real-time grid simulation core with 1-second timestep."""

import logging
import threading
import time
from queue import Empty, Queue
from typing import Any, Callable, Dict, Optional

from .engines.base import PowerSystemEngine
from .schemas.commands import (
    BreakerCommand,
    CommandType,
    GeneratorCommand,
    LoadCommand,
    ReactorCommand,
    StorageCommand,
)
from .schemas.state import GridState
from .schemas.topology import NetworkTopology

logger = logging.getLogger(__name__)

# A step loop runs at 1 s wall time, so 5 consecutive failures is about five
# seconds of continuous failure. A one- or two-step glitch (a rejected command,
# one bad solve) recovers on its own; five in a row means the state is wedged,
# and the alternative is publishing nothing for weeks, as job 23084 did.
MAX_CONSECUTIVE_FAILED_STEPS = 5


class SimulatorHalted(RuntimeError):
    """The simulator stopped itself; the process must exit so the run fails."""


class GridSimulator:
    """
    Real-time grid simulation wrapper with 1-second timestep.

    This simulator runs a continuous loop executing power flow calculations
    and processing SCADA commands. It's designed to interface with DNP3/Modbus
    protocol handlers for realistic SCADA simulation.

    Features:
    - 1-second timestep real-time simulation
    - Command queue for SCADA control operations
    - State capture and history tracking
    - Callback system for state updates
    - Thread-safe operation
    - Engine-agnostic (works with any PowerSystemEngine implementation)
    """

    def __init__(self, engine: PowerSystemEngine, timestep_seconds: float = 1.0):
        """
        Initialize the grid simulator.

        Args:
            engine: PowerSystemEngine instance to simulate
            timestep_seconds: Simulation timestep in seconds (default: 1.0)
        """
        self.engine = engine
        self.timestep = timestep_seconds
        self.current_time = 0.0
        self.running = False
        self.command_queue: Queue = Queue()
        self.state_history: list[GridState] = []
        self.max_history_length = 3600  # Keep 1 hour of history at 1s timestep

        # Callbacks for state updates (used by protocol handlers)
        self.state_update_callbacks: list[Callable[[GridState], None]] = []

        # Thread for simulation loop
        self._sim_thread: Optional[threading.Thread] = None
        self._lock = threading.Lock()

        # Cached topology — populated once from the main thread (before
        # the simulation loop starts) so the web API never calls
        # engine.get_topology() from the Flask thread.  OpenDSS uses
        # global C-library state; concurrent access from multiple threads
        # causes segfaults on large models (IEEE 8500).
        self._cached_topology: Optional[NetworkTopology] = None
        self._stock_regulator_vreg: Dict[int, float] = {}

        # Engine lock — serializes ALL engine access across threads.
        # The simulation thread holds this during step() (run_simulation +
        # get_current_state + callbacks).  Modbus write handlers acquire it
        # before calling engine.set_breaker_status / set_generator_setpoint.
        # Without this lock, concurrent dss.* calls from the Modbus async
        # thread and the simulation thread segfault the OpenDSS C library.
        self.engine_lock = threading.RLock()

        # Statistics
        self.stats = {
            "total_steps": 0,
            "failed_steps": 0,
            "non_converged_steps": 0,
            "commands_processed": 0,
            "average_step_time": 0.0,
        }

        # Tracks whether the most recent step() power flow converged.
        # Written by the simulator thread; read by the web API thread.
        # Python bool assignment is atomic under the GIL, so no lock is needed
        # for this single-flag read. web_api uses this to set the "converged"
        # field in /state, overriding the stale GridState.converged value that
        # would otherwise remain True after a NR failure (since no new GridState
        # is appended to state_history when step() throws).
        self.last_step_converged: bool = True

        # Consecutive step() exceptions, reset by any success. At
        # MAX_CONSECUTIVE_FAILED_STEPS the loop halts and `halted_reason` is set,
        # which the web API /health endpoint reports as unhealthy.
        self.consecutive_failed_steps = 0
        self.halted_reason: Optional[str] = None

        logger.info(f"GridSimulator initialized with {timestep_seconds}s timestep")

    def add_state_callback(self, callback: Callable[[GridState], None]):
        """
        Add a callback function to be called on each state update.

        Args:
            callback: Function that takes GridState as argument
        """
        self.state_update_callbacks.append(callback)
        callback_name = getattr(callback, "__name__", callback.__class__.__name__)
        logger.debug(f"Added state callback: {callback_name}")

    def queue_command(self, command):
        """
        Queue a command for execution in the next timestep.

        Args:
            command: Command to execute (BreakerCommand, GeneratorCommand, etc.)
        """
        self.command_queue.put(command)
        logger.debug(f"Queued command: {type(command).__name__}")

    def _process_commands(self):
        """Process all queued commands."""
        commands_processed = 0

        while not self.command_queue.empty():
            try:
                command = self.command_queue.get_nowait()
                self._execute_command(command)
                commands_processed += 1
            except Empty:
                break
            except Exception as e:
                logger.error(f"Error processing command: {e}")

        if commands_processed > 0:
            self.stats["commands_processed"] += commands_processed
            logger.debug(f"Processed {commands_processed} commands")

    def _execute_command(self, command):
        """
        Execute a single command using the engine interface.

        Args:
            command: Command to execute
        """
        try:
            if isinstance(command, ReactorCommand):
                # ReactorCommand targets gen table entries (not sgens).
                # Dispatched directly to set_reactor_gen_output() which is
                # specific to PandaPowerEngine. The base execute_command()
                # only knows about sgen-based GeneratorCommand.
                set_reactor = getattr(self.engine, "set_reactor_gen_output", None)
                if set_reactor is None:
                    raise TypeError(
                        f"Engine {type(self.engine).__name__} does not support "
                        "ReactorCommand (missing set_reactor_gen_output)"
                    )
                set_reactor(command.gen_id, command.p_mw, command.q_mvar)
            else:
                self.engine.execute_command(command)
        except Exception as e:
            logger.error(f"Error executing command {type(command).__name__}: {e}")

    # Target ext_grid import (MW). BESS discharges when import exceeds this,
    # charges when import falls below it (e.g. DER surplus). Based on no-BESS
    # operating point (~2.965 MW) minus one BESS unit capacity (0.25 MW).
    _BESS_IMPORT_TARGET_MW: float = 2.8

    # Deadband around target (MW). Import within ±50 kW of target → BESS idles.
    # Prevents micro-cycling when close to target; 50 kW ≈ 20% of one unit.
    _BESS_DEADBAND_MW: float = 0.05

    def _control_bess(self) -> None:  # noqa: C901
        """Peak-shaving BESS control targeting a fixed ext_grid import limit.

        Uses the previous timestep's power flow result as feedback (one-step delay,
        realistic for a real controller reading its SCADA telemetry). Dispatches each
        storage unit to reduce or increase ext_grid import toward _BESS_IMPORT_TARGET_MW.

        Sign convention (matching set_storage_power): positive = discharge (inject
        to grid), negative = charge (absorb from grid).
        """
        state = self.engine.get_current_state()
        if not state.storage:
            return

        if state.total_generation_mw is None:
            return  # No power flow result yet

        # ext_grid import from previous step.
        # state.total_generation_mw = ext_grid + DER sgens (BESS excluded per engine).
        total_der_mw = sum(gen.p_mw for gen in state.generators.values())
        ext_grid_import_mw = state.total_generation_mw - total_der_mw

        # Error: positive = import above target → discharge to reduce import.
        error_mw = ext_grid_import_mw - self._BESS_IMPORT_TARGET_MW

        if abs(error_mw) < self._BESS_DEADBAND_MW:
            for storage_id in state.storage:
                try:
                    self.engine.set_storage_power(storage_id, 0.0)
                except Exception as e:
                    logger.error(f"Error idling storage {storage_id}: {e}")
            return

        num_bess = len(state.storage)

        for storage_id, storage_state in state.storage.items():
            try:
                storage_info = self.engine.get_storage_info(storage_id)
            except KeyError:
                continue

            max_p_mw = storage_info.energy_capacity_mwh / 4.0  # C/4 rate
            soc = storage_state.soc_percent

            # Each unit handles an equal share of the error.
            # Positive target = discharge (import too high); negative = charge.
            target_p_mw = error_mw / num_bess

            # SOC limits: taper output near empty (discharge) or full (charge)
            if target_p_mw > 0:  # Discharging
                if soc <= 10.0:
                    target_p_mw = 0.0
                elif soc <= 20.0:
                    target_p_mw *= (soc - 10.0) / 10.0
            else:  # Charging
                if soc >= 90.0:
                    target_p_mw = 0.0
                elif soc >= 80.0:
                    target_p_mw *= (90.0 - soc) / 10.0

            target_p_mw = max(-max_p_mw, min(max_p_mw, target_p_mw))

            try:
                self.engine.set_storage_power(storage_id, target_p_mw)
            except Exception as e:
                logger.error(f"Error setting storage {storage_id} power: {e}")

    def _publish_state(self, state: GridState):
        """
        Publish state to all registered callbacks.

        Args:
            state: GridState to publish
        """
        for callback in self.state_update_callbacks:
            try:
                callback(state)
            except Exception as e:
                callback_name = getattr(
                    callback, "__name__", callback.__class__.__name__
                )
                logger.error(f"Error in state callback {callback_name}: {e}")

    def step(self):
        """Execute one simulation timestep.

        Acquires engine_lock for the entire step to prevent concurrent
        OpenDSS access from Modbus write handlers (which run in the
        asyncio thread).
        """
        step_start = time.time()

        with self.engine_lock:
            try:
                # Process queued commands
                self._process_commands()

                # Dynamic BESS control based on net load
                self._control_bess()

                # Run power flow using engine
                from .schemas.results import PowerFlowAlgorithm, PowerFlowConfig

                config = PowerFlowConfig(
                    algorithm=PowerFlowAlgorithm.NEWTON_RAPHSON,
                    max_iterations=20,
                    tolerance=1e-6,
                )
                result = self.engine.run_simulation(config)

                if not result.converged:
                    # Pre-fix: just a warning, then `last_step_converged = True`
                    # below; downstream /state consumers saw a non-converged solve
                    # labeled converged. Now counted in stats and reflected in the
                    # flag set after the try-block.
                    logger.warning(
                        "Power flow did not converge (algorithm=%s, max_iter=%d, "
                        "tol=%g); state will be published with converged=False "
                        "and last_step_converged=False so downstream consumers "
                        "can filter the step out.",
                        config.algorithm.value,
                        config.max_iterations,
                        config.tolerance,
                    )
                    self.stats["non_converged_steps"] += 1

                # Update battery SOC based on power flow results
                if hasattr(self.engine, "update_storage_soc"):
                    self.engine.update_storage_soc(timestep_seconds=self.timestep)

                # Get current state from engine
                state = self.engine.get_current_state()

                # Store in history
                with self._lock:
                    self.state_history.append(state)
                    if len(self.state_history) > self.max_history_length:
                        self.state_history.pop(0)

                # Publish to callbacks
                self._publish_state(state)

                # Update simulation time
                self.current_time += self.timestep

                # Update statistics
                self.stats["total_steps"] += 1
                step_time = time.time() - step_start
                self.stats["average_step_time"] = (
                    self.stats["average_step_time"] * (self.stats["total_steps"] - 1)
                    + step_time
                ) / self.stats["total_steps"]

                # Reflect the actual convergence outcome (atomic write under GIL).
                # Pre-fix this was hardcoded True on any non-throwing step, which
                # masked non-converged solves from /state and downstream attack-
                # prevention metrics. The except below still sets False on raise.
                self.last_step_converged = bool(result.converged)
                self.consecutive_failed_steps = 0

            except Exception as e:
                logger.error(f"Error in simulation step: {e}")
                self.stats["failed_steps"] += 1
                self.consecutive_failed_steps += 1
                if self.consecutive_failed_steps >= MAX_CONSECUTIVE_FAILED_STEPS:
                    self.halt(
                        f"{self.consecutive_failed_steps} consecutive failed "
                        f"simulation steps; last error: {e}"
                    )
                # Mark non-convergence. Written here so web_api can expose it in /state
                # rather than returning the stale pre-islanding state's converged=True.
                self.last_step_converged = False

    def halt(self, reason: str) -> None:
        """Stop the simulation loop and record why, so /health reports unhealthy.

        Args:
            reason: Human-readable cause, surfaced by the health endpoint.
        """
        logger.critical("Halting simulation: %s", reason)
        self.halted_reason = reason
        self.running = False

    def _run_loop(self, duration_seconds: Optional[float] = None):
        """
        Internal simulation loop (runs in separate thread).

        Args:
            duration_seconds: Optional duration to run simulation
        """
        logger.info("Starting simulation loop")
        start_time = time.time()

        while self.running:
            loop_start = time.time()

            # Execute one timestep
            self.step()

            # Calculate sleep time to maintain timestep
            elapsed = time.time() - loop_start
            sleep_time = max(0, self.timestep - elapsed)

            if sleep_time > 0:
                time.sleep(sleep_time)
            else:
                logger.warning(
                    f"Simulation step took {elapsed:.3f}s, exceeding timestep of {self.timestep}s"
                )

            # Check duration limit
            if duration_seconds and (time.time() - start_time) >= duration_seconds:
                logger.info(f"Simulation duration limit reached: {duration_seconds}s")
                break

        logger.info("Simulation loop stopped")

    def start(self, duration_seconds: Optional[float] = None, threaded: bool = True):
        """
        Start the simulation.

        Args:
            duration_seconds: Optional duration to run simulation
            threaded: If True, run in separate thread; if False, block in current thread
        """
        if self.running:
            logger.warning("Simulation already running")
            return

        self.running = True

        if threaded:
            self._sim_thread = threading.Thread(
                target=self._run_loop, args=(duration_seconds,), daemon=True
            )
            self._sim_thread.start()
            logger.info("Simulation started in background thread")
        else:
            self._run_loop(duration_seconds)

    def stop(self):
        """Stop the simulation."""
        if not self.running:
            logger.warning("Simulation not running")
            return

        logger.info("Stopping simulation...")
        self.running = False

        if self._sim_thread and self._sim_thread.is_alive():
            self._sim_thread.join(timeout=5.0)

        logger.info("Simulation stopped")

    def cache_topology(self) -> None:
        """Snapshot the engine topology for thread-safe access.

        Must be called from the main thread BEFORE starting the simulation
        loop.  After this, get_cached_topology() returns the snapshot
        without touching the engine (safe from any thread).
        """
        self._cached_topology = self.engine.get_topology()
        self._stock_regulator_vreg = self._read_stock_regulator_vreg(
            self._cached_topology
        )
        logger.info(
            "Topology cached: %d buses, %d lines, %d generators, %d loads",
            len(self._cached_topology.buses),
            len(self._cached_topology.lines),
            len(self._cached_topology.generators),
            len(self._cached_topology.loads),
        )

    def get_cached_topology(self):
        """Return the cached topology (thread-safe, no engine calls)."""
        return self._cached_topology

    def get_stock_regulator_vreg(self) -> Dict[int, float]:
        """{regulator id: ForwardVreg volts as compiled from the DSS model}.

        Captured with the topology, before the simulation loop starts and so
        before any setpoint write. `reset()` recompiles the model, which
        restores exactly these values, so they stay the feeder's planned
        setpoints for the life of the process. behavior-sim's VVO persona
        anchors its nudges here rather than on a band midpoint that can sit
        5% away from the published feeder's own regulation.
        """
        return dict(self._stock_regulator_vreg)

    def _read_stock_regulator_vreg(self, topology: NetworkTopology) -> Dict[int, float]:
        if not topology.regulators:
            return {}
        read = getattr(self.engine, "get_regulator_voltage_setpoint", None)
        if read is None:
            raise TypeError(
                f"Engine {type(self.engine).__name__} reports "
                f"{len(topology.regulators)} regulator(s) but cannot read "
                "their setpoints (no get_regulator_voltage_setpoint)."
            )
        return {int(reg_id): float(read(reg_id)) for reg_id in topology.regulators}

    def get_current_state(self) -> Optional[GridState]:
        """
        Get the most recent state.

        Returns:
            Most recent GridState, or None if no history
        """
        with self._lock:
            return self.state_history[-1] if self.state_history else None

    def get_statistics(self) -> Dict[str, Any]:
        """
        Get simulation statistics.

        Returns:
            Dictionary of simulation statistics
        """
        return self.stats.copy()
