"""
Grid-STIX Exporter

Exports grid simulation data as STIX 2.1 bundles for cybersecurity analysis.
"""

from typing import List, Dict, Any, Optional
from datetime import datetime
import json
from pathlib import Path
import sys

# Add Grid-STIX library to path
GRID_STIX_PATH = Path(
    "/home/bblakely/code/ztcard/ztcard-stack/src/stack/dockerfiles/console/ztcard-grid-stix/python"
)
if str(GRID_STIX_PATH) not in sys.path:
    sys.path.insert(0, str(GRID_STIX_PATH))

from stix2 import Bundle

from .annotator import GridSTIXAnnotator
from .telemetry import TelemetryConverter
from ..schemas.state import GridState


class STIXExporter:
    """
    Exports grid simulation data as STIX 2.1 bundles.

    Supports both full exports (topology + telemetry) and incremental
    exports (telemetry only) for efficient cybersecurity analysis.
    """

    def __init__(
        self,
        annotator: GridSTIXAnnotator,
        telemetry_converter: TelemetryConverter,
    ):
        """
        Initialize the STIX exporter.

        Args:
            annotator: GridSTIXAnnotator with component annotations
            telemetry_converter: TelemetryConverter for state conversion
        """
        self.annotator = annotator
        self.telemetry_converter = telemetry_converter
        self._last_export_time: Optional[float] = None

    def export_full_bundle(
        self,
        states: Optional[List[GridState]] = None,
        max_telemetry: int = 1000,
    ) -> Bundle:
        """
        Export a complete STIX bundle with topology and telemetry.

        Args:
            states: Optional list of grid states to include as telemetry
            max_telemetry: Maximum number of telemetry objects to include

        Returns:
            STIX 2.1 Bundle containing all objects
        """
        objects = []

        # Add all topology objects (assets, components)
        objects.extend(self.annotator.get_all_stix_objects())

        # Add telemetry if states provided
        if states:
            telemetry = self.telemetry_converter.batch_convert_states(
                states, max_telemetry=max_telemetry
            )
            objects.extend(telemetry)

        # Create STIX bundle
        bundle = Bundle(objects=objects)

        self._last_export_time = datetime.now().timestamp()

        return bundle

    def export_incremental_bundle(
        self,
        states: List[GridState],
        max_telemetry: int = 1000,
        time_filter: Optional[datetime] = None,
    ) -> Bundle:
        """
        Export an incremental STIX bundle with only new telemetry.

        Args:
            states: List of grid states to convert
            max_telemetry: Maximum number of telemetry objects to include
            time_filter: Optional timestamp - only include states after this time

        Returns:
            STIX 2.1 Bundle containing telemetry objects
        """
        # Filter states by time if requested
        if time_filter is not None:
            states = [s for s in states if s.timestamp > time_filter]

        # Convert states to telemetry
        telemetry = self.telemetry_converter.batch_convert_states(
            states, max_telemetry=max_telemetry
        )

        # Create bundle with only telemetry
        bundle = Bundle(objects=telemetry)

        self._last_export_time = datetime.now().timestamp()

        return bundle

    def export_to_file(
        self,
        bundle: Bundle,
        filepath: Path,
        pretty: bool = True,
    ) -> None:
        """
        Export a STIX bundle to a JSON file.

        Args:
            bundle: STIX bundle to export
            filepath: Path to output file
            pretty: Whether to pretty-print the JSON
        """
        filepath.parent.mkdir(parents=True, exist_ok=True)

        with open(filepath, "w") as f:
            if pretty:
                f.write(bundle.serialize(pretty=True))
            else:
                f.write(bundle.serialize())

    def export_topology_only(self) -> Bundle:
        """
        Export only the topology (no telemetry).

        Returns:
            STIX 2.1 Bundle containing topology objects
        """
        objects = self.annotator.get_all_stix_objects()
        return Bundle(objects=objects)

    def get_bundle_stats(self, bundle: Bundle) -> Dict[str, Any]:
        """
        Get statistics about a STIX bundle.

        Args:
            bundle: STIX bundle to analyze

        Returns:
            Dictionary with bundle statistics
        """
        stats = {
            "total_objects": len(bundle.objects),
            "object_types": {},
            "bundle_id": bundle.id,
            "spec_version": bundle.spec_version,
        }

        # Count objects by type
        for obj in bundle.objects:
            obj_type = obj.type
            stats["object_types"][obj_type] = stats["object_types"].get(obj_type, 0) + 1

        return stats

    def export_with_metadata(
        self,
        bundle: Bundle,
        filepath: Path,
        metadata: Optional[Dict[str, Any]] = None,
    ) -> None:
        """
        Export a STIX bundle with additional metadata.

        Args:
            bundle: STIX bundle to export
            filepath: Path to output file
            metadata: Optional metadata to include
        """
        output = {
            "metadata": metadata or {},
            "bundle": json.loads(bundle.serialize()),
            "export_time": datetime.now().isoformat(),
            "stats": self.get_bundle_stats(bundle),
        }

        filepath.parent.mkdir(parents=True, exist_ok=True)

        with open(filepath, "w") as f:
            json.dump(output, f, indent=2)

    def get_last_export_time(self) -> Optional[float]:
        """Get the timestamp of the last export."""
        return self._last_export_time
