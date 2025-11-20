"""
Factory for creating power system engines and network models.

This module provides factory functions for creating engine and model instances,
making it easy to swap between different implementations.
"""

from typing import Any, Dict, Optional, Tuple

from ..engines.base import PowerSystemEngine
from ..engines.pandapower_engine import PandaPowerEngine
from .dickert_lv import DickertLVModel


def create_pandapower_dickert_lv(
    feeders_range: str = "short", linetype: str = "cable", **kwargs
) -> PandaPowerEngine:
    """
    Create a PandaPower engine with Dickert LV network.

    Args:
        feeders_range: Feeder length - 'short', 'middle', or 'long'
        linetype: Line type - 'cable' or 'C&OHL'
        **kwargs: Additional arguments (currently unused)

    Returns:
        PandaPowerEngine initialized with Dickert LV network.
    """
    # Create the network model
    model = DickertLVModel(feeders_range=feeders_range, linetype=linetype)

    # Get the PandaPower network
    pp_net = model.get_network()

    # Wrap with engine
    engine = PandaPowerEngine(pp_net)

    return engine


def create_engine(
    engine_type: str = "pandapower", model_type: str = "dickert_lv", **kwargs
) -> PowerSystemEngine:
    """
    Create a power system engine with specified network model.

    Args:
        engine_type: Type of engine ('pandapower', 'opendss', 'pypsa', etc.)
        model_type: Type of network model ('dickert_lv', 'ieee13', etc.)
        **kwargs: Model-specific parameters

    Returns:
        PowerSystemEngine instance.

    Raises:
        ValueError: If engine_type or model_type is not supported.
    """
    if engine_type == "pandapower":
        if model_type == "dickert_lv":
            return create_pandapower_dickert_lv(**kwargs)
        else:
            raise ValueError(f"Unsupported PandaPower model type: {model_type}")
    else:
        raise ValueError(f"Unsupported engine type: {engine_type}")


def create_simulation(
    engine_type: str = "pandapower", model_type: str = "dickert_lv", **kwargs
) -> Tuple[PowerSystemEngine, Dict[str, Any]]:
    """
    Create a complete simulation setup with engine and model info.

    Args:
        engine_type: Type of engine ('pandapower', 'opendss', 'pypsa', etc.)
        model_type: Type of network model ('dickert_lv', 'ieee13', etc.)
        **kwargs: Model-specific parameters

    Returns:
        Tuple of (engine, model_info) where model_info contains metadata
        about the created network.

    Raises:
        ValueError: If engine_type or model_type is not supported.
    """
    # Create the engine
    engine = create_engine(engine_type=engine_type, model_type=model_type, **kwargs)

    # Get topology info
    topology = engine.get_topology()

    # Build model info
    model_info = {
        "engine_type": engine_type,
        "model_type": model_type,
        "name": topology.name,
        "base_mva": topology.base_mva,
        "frequency_hz": topology.frequency_hz,
        "num_buses": len(topology.buses),
        "num_lines": len(topology.lines),
        "num_generators": len(topology.generators),
        "num_loads": len(topology.loads),
        "num_storage": len(topology.storage),
        "parameters": kwargs,
    }

    return engine, model_info


# Convenience aliases
def create_default_simulation(**kwargs) -> Tuple[PowerSystemEngine, Dict[str, Any]]:
    """
    Create a simulation with default settings (PandaPower + Dickert LV).

    Args:
        **kwargs: Model-specific parameters (feeders_range, linetype, etc.)

    Returns:
        Tuple of (engine, model_info).
    """
    return create_simulation(
        engine_type="pandapower", model_type="dickert_lv", **kwargs
    )
