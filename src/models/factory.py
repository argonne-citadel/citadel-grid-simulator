"""
Factory for creating power system engines and network models.

This module provides factory functions for creating engine and model instances,
making it easy to swap between different implementations.
"""

from typing import Any, Dict, Optional, Tuple

from ..engines.base import PowerSystemEngine
from ..engines.pandapower_engine import PandaPowerEngine
from .baran_wu_33 import BaranWu33Model


def create_pandapower_baran_wu_33(**kwargs: Any) -> PandaPowerEngine:
    """
    Create a PandaPower engine with Baran-Wu 33-bus network.

    Args:
        **kwargs: Additional arguments (currently unused)

    Returns:
        PandaPowerEngine initialized with Baran-Wu 33-bus network.
    """
    model = BaranWu33Model()
    pp_net = model.get_network()
    return PandaPowerEngine(pp_net)


def create_engine(
    engine_type: str = "pandapower", model_type: str = "baran_wu_33", **kwargs: Any
) -> PowerSystemEngine:
    """
    Create a power system engine with specified network model.

    Args:
        engine_type: Type of engine ('pandapower', 'opendss', 'pypsa', etc.)
        model_type: Type of network model ('baran_wu_33', etc.)
        **kwargs: Model-specific parameters

    Returns:
        PowerSystemEngine instance.

    Raises:
        ValueError: If engine_type or model_type is not supported.
    """
    if engine_type == "pandapower":
        if model_type == "baran_wu_33":
            return create_pandapower_baran_wu_33(**kwargs)
        else:
            raise ValueError(f"Unsupported PandaPower model type: {model_type}")
    else:
        raise ValueError(f"Unsupported engine type: {engine_type}")


def create_simulation(
    engine_type: str = "pandapower", model_type: str = "baran_wu_33", **kwargs: Any
) -> Tuple[PowerSystemEngine, Dict[str, Any]]:
    """
    Create a complete simulation setup with engine and model info.

    Args:
        engine_type: Type of engine ('pandapower', 'opendss', 'pypsa', etc.)
        model_type: Type of network model ('baran_wu_33', etc.)
        **kwargs: Model-specific parameters

    Returns:
        Tuple of (engine, model_info) where model_info contains metadata
        about the created network.

    Raises:
        ValueError: If engine_type or model_type is not supported.
    """
    engine = create_engine(engine_type=engine_type, model_type=model_type, **kwargs)
    topology = engine.get_topology()

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


def create_default_simulation(
    **kwargs: Any,
) -> Tuple[PowerSystemEngine, Dict[str, Any]]:
    """
    Create a simulation with default settings (PandaPower + Baran-Wu 33-bus).

    Args:
        **kwargs: Model-specific parameters

    Returns:
        Tuple of (engine, model_info).
    """
    return create_simulation(
        engine_type="pandapower", model_type="baran_wu_33", **kwargs
    )
