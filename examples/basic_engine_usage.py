#!/usr/bin/env python3
"""
Basic example demonstrating the new engine abstraction layer.

This example shows how to:
1. Create a PandaPower network
2. Wrap it with the PandaPowerEngine
3. Use the abstract interface to query topology and state
4. Execute control commands
5. Run simulations
"""

import sys
from pathlib import Path

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from src.engines import PandaPowerEngine
from src.models.dickert_lv import DickertLVModel
from src.schemas.commands import (
    BreakerCommand,
    GeneratorCommand,
    LoadCommand,
    StorageCommand,
)
from src.schemas.results import PowerFlowConfig, PowerFlowAlgorithm


def main():
    print("=" * 80)
    print("Engine Abstraction Layer - Basic Usage Example")
    print("=" * 80)
    print()

    # Step 1: Create a PandaPower network using existing model
    print("Step 1: Creating Dickert LV network...")
    model = DickertLVModel(feeders_range="short", linetype="cable")
    pp_net = model.get_network()
    print(f"  ✓ Created network with {len(pp_net.bus)} buses")
    print()

    # Step 2: Wrap with engine abstraction
    print("Step 2: Wrapping with PandaPowerEngine...")
    engine = PandaPowerEngine(pp_net)
    print("  ✓ Engine initialized")
    print()

    # Step 3: Query topology using abstract interface
    print("Step 3: Querying network topology...")
    topology = engine.get_topology()
    print(f"  Network: {topology.name}")
    print(f"  Base MVA: {topology.base_mva}")
    print(f"  Frequency: {topology.frequency_hz} Hz")
    print(f"  Buses: {len(topology.buses)}")
    print(f"  Lines: {len(topology.lines)}")
    print(f"  Generators: {len(topology.generators)}")
    print(f"  Loads: {len(topology.loads)}")
    print(f"  Storage: {len(topology.storage)}")
    print()

    # Step 4: Run initial power flow
    print("Step 4: Running initial power flow...")
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

    # Step 5: Get current state
    print("Step 5: Querying grid state...")
    state = engine.get_current_state()
    print(f"  Timestamp: {state.timestamp}")
    print(f"  Converged: {state.converged}")
    print(f"  Total generation: {state.total_generation_mw:.3f} MW")
    print(f"  Total load: {state.total_load_mw:.3f} MW")
    print(f"  Total losses: {state.total_losses_mw:.3f} MW")
    print()

    # Step 6: Display some bus voltages
    print("Step 6: Sample bus voltages:")
    for bus_id, bus_state in list(state.buses.items())[:5]:
        bus_info = topology.buses[bus_id]
        print(
            f"  Bus {bus_id} ({bus_info.name}): {bus_state.voltage_pu:.4f} pu, {bus_state.angle_deg:.2f}°"
        )
    print()

    # Step 7: Execute control commands
    print("Step 7: Executing control commands...")

    # Adjust a load if available
    if topology.loads:
        load_id = list(topology.loads.keys())[0]
        original_p = topology.loads[load_id].p_mw
        new_p = original_p * 1.2  # Increase by 20%

        cmd = LoadCommand(load_id=load_id, p_mw=new_p)
        engine.execute_command(cmd)
        print(f"  ✓ Adjusted Load {load_id}: {original_p:.3f} → {new_p:.3f} MW")

    # Adjust a generator if available
    if topology.generators:
        gen_id = list(topology.generators.keys())[0]
        original_p = topology.generators[gen_id].p_max_mw
        new_p = original_p * 0.8  # Reduce to 80%

        cmd = GeneratorCommand(generator_id=gen_id, p_mw=new_p)
        engine.execute_command(cmd)
        print(f"  ✓ Adjusted Generator {gen_id}: {original_p:.3f} → {new_p:.3f} MW")

    print()

    # Step 8: Run power flow again after changes
    print("Step 8: Running power flow after control actions...")
    result = engine.run_simulation(config)
    print(f"  Converged: {result.converged}")
    print(f"  Iterations: {result.iterations}")
    print()

    # Step 9: Get updated state
    print("Step 9: Updated grid state:")
    state = engine.get_current_state()
    print(f"  Total generation: {state.total_generation_mw:.3f} MW")
    print(f"  Total load: {state.total_load_mw:.3f} MW")
    print(f"  Total losses: {state.total_losses_mw:.3f} MW")
    print()

    # Step 10: Demonstrate breaker control
    print("Step 10: Breaker control example...")
    if topology.lines:
        line_id = list(topology.lines.keys())[0]
        line_info = topology.lines[line_id]

        print(
            f"  Line {line_id} ({line_info.name}): {line_info.from_bus} → {line_info.to_bus}"
        )
        print(f"  Initial status: {'CLOSED' if line_info.in_service else 'OPEN'}")

        # Open the breaker
        cmd = BreakerCommand(line_id=line_id, closed=False)
        engine.execute_command(cmd)
        print(f"  ✓ Opened breaker on line {line_id}")

        # Run power flow
        result = engine.run_simulation(config)
        print(f"  Power flow converged: {result.converged}")

        # Close the breaker again
        cmd = BreakerCommand(line_id=line_id, closed=True)
        engine.execute_command(cmd)
        print(f"  ✓ Closed breaker on line {line_id}")

    print()
    print("=" * 80)
    print("Example completed successfully!")
    print("=" * 80)


if __name__ == "__main__":
    main()
