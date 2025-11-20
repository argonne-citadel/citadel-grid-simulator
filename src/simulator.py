"""Real-time grid simulation core with 1-second timestep."""

import time
import threading
from queue import Queue, Empty
from typing import Dict, Any, Optional, Callable
import logging

from .engines.base import PowerSystemEngine
from .schemas.commands import (
    BreakerCommand,
    CommandType,
    GeneratorCommand,
    LoadCommand,
    StorageCommand,
)
from .schemas.state import GridState

logger = logging.getLogger(__name__)


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

        # Statistics
        self.stats = {
            "total_steps": 0,
            "failed_steps": 0,
            "commands_processed": 0,
            "average_step_time": 0.0,
        }

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
            self.engine.execute_command(command)
        except Exception as e:
            logger.error(f"Error executing command {type(command).__name__}: {e}")

    def _control_bess(self):
        """
        Dynamic BESS control based on grid net load.

        Controls battery storage to balance generation and load:
        - If generation > load (excess): charge batteries (negative power)
        - If load > generation (deficit): discharge batteries (positive power)
        - Power proportional to imbalance, limited by battery capacity and SOC
        """
        # Get current state
        state = self.engine.get_current_state()

        if not state.storage:
            return  # No storage to control

        # Calculate total generation
        total_gen = sum(gen.p_mw for gen in state.generators.values())

        # Calculate total load
        total_load = sum(load.p_mw for load in state.loads.values())

        # Net imbalance (positive = excess generation, negative = deficit)
        net_imbalance = total_gen - total_load

        # Distribute imbalance across all BESS units proportionally
        num_bess = len(state.storage)

        for storage_id, storage_state in state.storage.items():
            # Get storage info
            try:
                storage_info = self.engine.get_storage_info(storage_id)
            except KeyError:
                continue

            # Calculate max power (assume C/4 rate)
            max_p_mw = storage_info.energy_capacity_mwh / 4.0
            soc = storage_state.soc_percent

            # Calculate target power for this BESS (share of total imbalance)
            target_p_mw = (
                -net_imbalance / num_bess
            )  # Negative because we want to absorb excess

            # Limit based on SOC constraints
            if target_p_mw < 0:  # Charging
                if soc >= 90.0:
                    target_p_mw = 0.0
                elif soc >= 80.0:
                    target_p_mw *= (90.0 - soc) / 10.0
            else:  # Discharging
                if soc <= 10.0:
                    target_p_mw = 0.0
                elif soc <= 20.0:
                    target_p_mw *= (soc - 10.0) / 10.0

            # Limit to capacity
            target_p_mw = max(-max_p_mw, min(max_p_mw, target_p_mw))

            # Set the storage power
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
        """Execute one simulation timestep."""
        step_start = time.time()

        try:
            # Process queued commands
            self._process_commands()

            # Dynamic BESS control based on net load
            self._control_bess()

            # Run power flow using engine
            from .schemas.results import PowerFlowConfig, PowerFlowAlgorithm

            config = PowerFlowConfig(
                algorithm=PowerFlowAlgorithm.NEWTON_RAPHSON,
                max_iterations=20,
                tolerance=1e-6,
            )
            result = self.engine.run_simulation(config)

            if not result.converged:
                logger.warning("Power flow did not converge")

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

        except Exception as e:
            logger.error(f"Error in simulation step: {e}")
            self.stats["failed_steps"] += 1

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
