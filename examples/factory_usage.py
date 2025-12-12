#!/usr/bin/env python3
"""
Example demonstrating the factory pattern for creating simulations.

This example shows how to use the factory functions to create
power system simulations without directly instantiating engines.
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.models import create_default_simulation, create_engine, create_simulation
from src.schemas.results import PowerFlowAlgorithm, PowerFlowConfig


def main():
    print("=" * 80)
    print("Factory Pattern - Simulation Creation Example")
    print("=" * 80)
    print()

    # Method 1: Using create_default_simulation (simplest)
    print("Method 1: Using create_default_simulation()")
    print("-" * 80)
    engine, model_info = create_default_simulation(
        feeders_range="short", linetype="cable"
    )

    print(f"Created simulation:")
    print(f"  Engine: {model_info['engine_type']}")
    print(f"  Model: {model_info['model_type']}")
    print(f"  Network: {model_info['name']}")
    print(f"  Buses: {model_info['num_buses']}")
    print(f"  Lines: {model_info['num_lines']}")
    print(f"  Generators: {model_info['num_generators']}")
    print(f"  Loads: {model_info['num_loads']}")
    print()

    # Method 2: Using create_simulation with explicit types
    print("Method 2: Using create_simulation() with explicit types")
    print("-" * 80)
    engine2, model_info2 = create_simulation(
        engine_type="pandapower",
        model_type="dickert_lv",
        feeders_range="middle",
        linetype="cable",
    )

    print(f"Created simulation:")
    print(f"  Engine: {model_info2['engine_type']}")
    print(f"  Model: {model_info2['model_type']}")
    print(f"  Buses: {model_info2['num_buses']}")
    print()

    # Method 3: Using create_engine directly
    print("Method 3: Using create_engine() directly")
    print("-" * 80)
    engine3 = create_engine(
        engine_type="pandapower", model_type="dickert_lv", feeders_range="short"
    )

    topology = engine3.get_topology()
    print(f"Created engine:")
    print(f"  Network: {topology.name}")
    print(f"  Buses: {len(topology.buses)}")
    print()

    # Run a simulation with the first engine
    print("Running simulation with Method 1 engine...")
    print("-" * 80)

    config = PowerFlowConfig(
        algorithm=PowerFlowAlgorithm.NEWTON_RAPHSON,
        max_iterations=20,
        tolerance=1e-6,
    )

    result = engine.run_simulation(config)
    print(f"  Converged: {result.converged}")
    print(f"  Iterations: {result.iterations}")
    print(f"  Execution time: {result.execution_time_ms:.2f} ms")
    print()

    # Get state
    state = engine.get_current_state()
    print(f"Grid state:")
    print(f"  Total generation: {state.total_generation_mw:.3f} MW")
    print(f"  Total load: {state.total_load_mw:.3f} MW")
    print(f"  Total losses: {state.total_losses_mw:.3f} MW")
    print()

    print("=" * 80)
    print("Factory pattern demonstration complete!")
    print("=" * 80)


if __name__ == "__main__":
    main()
