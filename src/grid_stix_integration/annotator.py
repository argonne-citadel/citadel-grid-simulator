"""
Grid-STIX Annotator

Annotates grid components with Grid-STIX objects and maintains a registry
of STIX ID to component ID mappings for cybersecurity analysis.
"""

from typing import Dict, List, Optional, Any
from datetime import datetime
import sys
from pathlib import Path

# Add Grid-STIX library to path
GRID_STIX_PATH = Path(
    "/home/bblakely/code/ztcard/ztcard-stack/src/stack/dockerfiles/console/ztcard-grid-stix/python"
)
if str(GRID_STIX_PATH) not in sys.path:
    sys.path.insert(0, str(GRID_STIX_PATH))

from grid_stix.components.DistributedEnergyResource import DistributedEnergyResource
from grid_stix.components.BatteryEnergyStorageSystem import BatteryEnergyStorageSystem
from grid_stix.components.PhotovoltaicSystem import PhotovoltaicSystem
from grid_stix.components.WindTurbine import WindTurbine
from grid_stix.components.SmartMeter import SmartMeter
from grid_stix.assets.Generator import Generator
from grid_stix.assets.Transformer import Transformer
from grid_stix.assets.DistributionLine import DistributionLine
from grid_stix.assets.Substation import Substation

from ..schemas.topology import (
    NetworkTopology,
    BusInfo,
    LineInfo,
    GeneratorInfo,
    LoadInfo,
    StorageInfo,
)
from ..schemas.common import BusID, LineID, GeneratorID, LoadID, StorageID


class GridSTIXAnnotator:
    """
    Annotates grid components with Grid-STIX objects.

    Maintains a bidirectional mapping between grid component IDs and
    Grid-STIX object IDs for cybersecurity analysis and correlation.
    """

    def __init__(self):
        """Initialize the Grid-STIX annotator."""
        # Mapping: component_id -> STIX object
        self._component_to_stix: Dict[str, Any] = {}

        # Mapping: STIX ID -> component_id
        self._stix_to_component: Dict[str, str] = {}

        # Store all created STIX objects
        self._stix_objects: List[Any] = []

    def annotate_topology(self, topology: NetworkTopology) -> None:
        """
        Annotate all components in a network topology with Grid-STIX objects.

        Args:
            topology: Network topology to annotate
        """
        # Annotate buses (as substations or connection points)
        for bus_id, bus_info in topology.buses.items():
            self._annotate_bus(bus_id, bus_info)

        # Annotate lines
        for line_id, line_info in topology.lines.items():
            self._annotate_line(line_id, line_info)

        # Annotate generators
        for gen_id, gen_info in topology.generators.items():
            self._annotate_generator(gen_id, gen_info)

        # Annotate loads
        for load_id, load_info in topology.loads.items():
            self._annotate_load(load_id, load_info)

        # Annotate storage
        for storage_id, storage_info in topology.storage.items():
            self._annotate_storage(storage_id, storage_info)

    def _annotate_bus(self, bus_id: BusID, bus_info: BusInfo) -> None:
        """Annotate a bus as a Grid-STIX Substation."""
        try:
            stix_obj = Substation(
                name=bus_info.name or f"Bus_{bus_id}",
                x_high_voltage_level_kv=[float(bus_info.voltage_nominal_kv)],
                x_substation_type=["distribution"],
                x_grid_component_type="substation",
                x_operational_status="in_service",
            )

            self._register_stix_object(f"bus_{bus_id}", stix_obj)
        except Exception as e:
            # Log but don't fail - annotation is optional
            print(f"Warning: Failed to annotate bus {bus_id}: {e}")

    def _annotate_line(self, line_id: LineID, line_info: LineInfo) -> None:
        """Annotate a line as a Grid-STIX DistributionLine."""
        try:
            # Estimate line length from resistance (rough approximation: ~0.1 ohm/km for typical distribution lines)
            estimated_length_km = max(0.1, line_info.resistance_ohm / 0.1)

            stix_obj = DistributionLine(
                name=line_info.name or f"Line_{line_id}",
                x_grid_component_type="distribution_line",
                x_voltage_level_kv=[0.4],  # Default distribution voltage
                x_length_km=[float(estimated_length_km)],
                x_operational_status=(
                    "in_service" if line_info.in_service else "out_of_service"
                ),
            )

            self._register_stix_object(f"line_{line_id}", stix_obj)
        except Exception as e:
            print(f"Warning: Failed to annotate line {line_id}: {e}")

    def _annotate_generator(self, gen_id: GeneratorID, gen_info: GeneratorInfo) -> None:
        """Annotate a generator as a Grid-STIX Generator."""
        try:
            stix_obj = Generator(
                name=gen_info.name or f"Generator_{gen_id}",
                x_grid_component_type="generator",
                x_power_rating_mw=[float(gen_info.p_max_mw)],
                x_fuel_type=[gen_info.der_type or "unknown"],
                x_operational_status=(
                    "in_service" if gen_info.in_service else "out_of_service"
                ),
            )

            self._register_stix_object(f"gen_{gen_id}", stix_obj)
        except Exception as e:
            print(f"Warning: Failed to annotate generator {gen_id}: {e}")

    def _annotate_load(self, load_id: LoadID, load_info: LoadInfo) -> None:
        """Annotate a load as a Grid-STIX SmartMeter."""
        try:
            stix_obj = SmartMeter(
                name=load_info.name or f"Load_{load_id}",
                x_grid_component_type="smart_meter",
                x_operational_status=(
                    "in_service" if load_info.in_service else "out_of_service"
                ),
            )

            self._register_stix_object(f"load_{load_id}", stix_obj)
        except Exception as e:
            print(f"Warning: Failed to annotate load {load_id}: {e}")

    def _annotate_storage(
        self, storage_id: StorageID, storage_info: StorageInfo
    ) -> None:
        """Annotate storage as a Grid-STIX BatteryEnergyStorageSystem."""
        try:
            stix_obj = BatteryEnergyStorageSystem(
                name=storage_info.name or f"BESS_{storage_id}",
                x_bess_system_id=[f"storage_{storage_id}"],
                x_capacity_kwh=[float(storage_info.energy_capacity_mwh * 1000)],
                x_bess_power_rating_kw=[float(storage_info.p_max_mw * 1000)],
                x_operational_status=(
                    "in_service" if storage_info.in_service else "out_of_service"
                ),
            )

            self._register_stix_object(f"storage_{storage_id}", stix_obj)
        except Exception as e:
            print(f"Warning: Failed to annotate storage {storage_id}: {e}")

    def _register_stix_object(self, component_id: str, stix_obj: Any) -> None:
        """Register a STIX object and maintain bidirectional mapping."""
        self._component_to_stix[component_id] = stix_obj
        self._stix_to_component[stix_obj.id] = component_id
        self._stix_objects.append(stix_obj)

    def get_stix_object(self, component_id: str) -> Optional[Any]:
        """Get the STIX object for a component ID."""
        return self._component_to_stix.get(component_id)

    def get_component_id(self, stix_id: str) -> Optional[str]:
        """Get the component ID for a STIX object ID."""
        return self._stix_to_component.get(stix_id)

    def get_all_stix_objects(self) -> List[Any]:
        """Get all created STIX objects."""
        return self._stix_objects.copy()

    def get_stix_id(self, component_id: str) -> Optional[str]:
        """Get the STIX ID for a component ID."""
        stix_obj = self._component_to_stix.get(component_id)
        return stix_obj.id if stix_obj else None

    def clear(self) -> None:
        """Clear all annotations."""
        self._component_to_stix.clear()
        self._stix_to_component.clear()
        self._stix_objects.clear()
