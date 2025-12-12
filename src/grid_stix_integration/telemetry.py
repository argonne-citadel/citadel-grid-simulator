"""
Grid-STIX Telemetry Converter

Converts GridState objects to Grid-STIX GridTelemetry observables
and control commands to Grid-STIX ControlActionEvent objects.
"""

from typing import List, Dict, Any, Optional
from datetime import datetime
import sys
from pathlib import Path

# Add Grid-STIX library to path
GRID_STIX_PATH = Path(
    "/home/bblakely/code/ztcard/ztcard-stack/src/stack/dockerfiles/console/ztcard-grid-stix/python"
)
if str(GRID_STIX_PATH) not in sys.path:
    sys.path.insert(0, str(GRID_STIX_PATH))

from grid_stix.events_observables.GridTelemetry import GridTelemetry
from grid_stix.events_observables.ControlActionEvent import ControlActionEvent

from ..schemas.state import GridState, BusState, LineState
from ..schemas.commands import (
    Command,
    BreakerCommand,
    GeneratorCommand,
    LoadCommand,
    StorageCommand,
)
from .annotator import GridSTIXAnnotator


class TelemetryConverter:
    """
    Converts grid simulation state and commands to Grid-STIX observables.

    This enables cybersecurity analysis by representing grid telemetry
    and control actions in the standardized STIX 2.1 format.
    """

    def __init__(self, annotator: Optional[GridSTIXAnnotator] = None):
        """
        Initialize the telemetry converter.

        Args:
            annotator: Optional GridSTIXAnnotator for component ID mapping
        """
        self.annotator = annotator

    def convert_grid_state(
        self, state: GridState, batch_size: int = 100
    ) -> List[GridTelemetry]:
        """
        Convert a GridState to Grid-STIX GridTelemetry observables.

        Args:
            state: Grid state to convert
            batch_size: Maximum number of telemetry objects to create per call

        Returns:
            List of GridTelemetry objects
        """
        telemetry_objects: List[GridTelemetry] = []
        count = 0

        # Convert bus voltages
        for bus_id, bus_state in state.buses.items():
            if count >= batch_size:
                break

            telemetry = self._create_bus_telemetry(bus_id, bus_state, state.timestamp)
            if telemetry:
                telemetry_objects.append(telemetry)
                count += 1

        # Convert line flows
        for line_id, line_state in state.lines.items():
            if count >= batch_size:
                break

            # Create telemetry for active power flow
            telemetry_p = self._create_line_power_telemetry(
                line_id, line_state, state.timestamp, "active"
            )
            if telemetry_p:
                telemetry_objects.append(telemetry_p)
                count += 1

            if count >= batch_size:
                break

            # Create telemetry for reactive power flow
            telemetry_q = self._create_line_power_telemetry(
                line_id, line_state, state.timestamp, "reactive"
            )
            if telemetry_q:
                telemetry_objects.append(telemetry_q)
                count += 1

        return telemetry_objects

    def _create_bus_telemetry(
        self, bus_id: int, bus_state: BusState, timestamp: datetime
    ) -> Optional[GridTelemetry]:
        """Create Grid-STIX telemetry for bus voltage."""
        try:
            # Get STIX ID if annotator is available
            source_device = None
            if self.annotator:
                stix_id = self.annotator.get_stix_id(f"bus_{bus_id}")
                if stix_id:
                    source_device = [stix_id]

            if not source_device:
                source_device = [f"bus_{bus_id}"]

            telemetry = GridTelemetry(
                name=f"Bus {bus_id} Voltage",
                x_measurement_type=["voltage_magnitude"],
                x_measurement_timestamp=[timestamp.isoformat()],
                x_source_device=source_device,
                x_value=[float(bus_state.voltage_pu)],
                x_metric_unit=["per_unit"],
                x_measurement_accuracy=[0.001],  # Typical accuracy
                x_quality_indicator=["good"],
                x_threshold_exceeded=[
                    abs(bus_state.voltage_pu - 1.0) > 0.1
                ],  # ±10% threshold
            )

            return telemetry
        except Exception as e:
            print(f"Warning: Failed to create bus telemetry for bus {bus_id}: {e}")
            return None

    def _create_line_power_telemetry(
        self, line_id: int, line_state: LineState, timestamp: datetime, power_type: str
    ) -> Optional[GridTelemetry]:
        """Create Grid-STIX telemetry for line power flow."""
        try:
            # Get STIX ID if annotator is available
            source_device = None
            if self.annotator:
                stix_id = self.annotator.get_stix_id(f"line_{line_id}")
                if stix_id:
                    source_device = [stix_id]

            if not source_device:
                source_device = [f"line_{line_id}"]

            if power_type == "active":
                value = float(line_state.p_from_mw)
                measurement_type = "active_power"
                unit = "MW"
            else:  # reactive
                value = float(line_state.q_from_mvar)
                measurement_type = "reactive_power"
                unit = "MVAr"

            telemetry = GridTelemetry(
                name=f"Line {line_id} {power_type.capitalize()} Power",
                x_measurement_type=[measurement_type],
                x_measurement_timestamp=[timestamp.isoformat()],
                x_source_device=source_device,
                x_value=[value],
                x_metric_unit=[unit],
                x_measurement_accuracy=[0.01],  # 1% accuracy
                x_quality_indicator=["good"],
                x_threshold_exceeded=[False],  # Could add loading threshold check
            )

            return telemetry
        except Exception as e:
            print(f"Warning: Failed to create line telemetry for line {line_id}: {e}")
            return None

    def convert_command(
        self, command: Command, timestamp: float
    ) -> Optional[ControlActionEvent]:
        """
        Convert a control command to a Grid-STIX ControlActionEvent.

        Args:
            command: Control command to convert
            timestamp: Timestamp of the command

        Returns:
            ControlActionEvent object or None if conversion fails
        """
        try:
            # Determine command details based on type
            action_details: Dict[str, Any]
            if isinstance(command, BreakerCommand):
                action_type = "breaker_control"
                target = f"line_{command.line_id}"
                action_details = {
                    "action": "close" if command.closed else "open",
                    "line_id": command.line_id,
                }
            elif isinstance(command, GeneratorCommand):
                action_type = "generator_setpoint"
                target = f"gen_{command.generator_id}"
                action_details = {
                    "p_mw": command.p_mw,
                    "q_mvar": command.q_mvar,
                    "generator_id": command.generator_id,
                }
            elif isinstance(command, LoadCommand):
                action_type = "load_adjustment"
                target = f"load_{command.load_id}"
                action_details = {
                    "p_mw": command.p_mw,
                    "q_mvar": command.q_mvar,
                    "load_id": command.load_id,
                }
            elif isinstance(command, StorageCommand):
                action_type = "storage_control"
                target = f"storage_{command.storage_id}"
                action_details = {
                    "p_mw": command.p_mw,
                    "storage_id": command.storage_id,
                }
            else:
                return None

            # Get STIX ID if annotator is available
            target_ref = None
            if self.annotator:
                stix_id = self.annotator.get_stix_id(target)
                if stix_id:
                    target_ref = [stix_id]

            if not target_ref:
                target_ref = [target]

            event = ControlActionEvent(
                name=f"{action_type} on {target}",
                x_event_type=[action_type],
                x_timestamp=[datetime.fromtimestamp(timestamp).isoformat()],
                x_source_component=["grid_simulator"],
                x_target_component=target_ref,
                x_action_details=[str(action_details)],
                x_authorization_status=["authorized"],
                x_execution_status=["executed"],
            )

            return event
        except Exception as e:
            print(f"Warning: Failed to convert command to STIX: {e}")
            return None

    def batch_convert_states(
        self, states: List[GridState], max_telemetry: int = 1000
    ) -> List[GridTelemetry]:
        """
        Convert multiple grid states to telemetry with batching.

        Args:
            states: List of grid states to convert
            max_telemetry: Maximum total telemetry objects to create

        Returns:
            List of GridTelemetry objects
        """
        all_telemetry: List[GridTelemetry] = []

        for state in states:
            if len(all_telemetry) >= max_telemetry:
                break

            remaining = max_telemetry - len(all_telemetry)
            telemetry = self.convert_grid_state(state, batch_size=remaining)
            all_telemetry.extend(telemetry)

        return all_telemetry
