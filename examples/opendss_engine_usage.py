"""
Example: Using OpenDSS Engine via RPC

This example demonstrates how to use the OpenDSSEngine to run power system
simulations with OpenDSS through a remote RPC service.

Prerequisites:
- OpenDSS RPC service running (e.g., docker container on port 8000)
- DSS circuit file accessible to the service
"""

import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from engines import OpenDSSEngine
from schemas.results import PowerFlowConfig


def main():
    """Demonstrate OpenDSS engine usage."""

    # Configuration
    opendss_service_url = "http://localhost:8000"
    dss_file_path = "/home/ubuntu/IEEE37Bus_PV.dss"

    print("=" * 70)
    print("OpenDSS Engine RPC Example")
    print("=" * 70)

    # Create engine (this loads the circuit)
    print(f"\n1. Initializing OpenDSS engine...")
    print(f"   Service URL: {opendss_service_url}")
    print(f"   DSS File: {dss_file_path}")

    try:
        engine = OpenDSSEngine(
            service_url=opendss_service_url,
            dss_file_path=dss_file_path,
            timeout_seconds=30.0,
        )
        print("   ✓ Engine initialized successfully")

    except Exception as e:
        print(f"   ✗ Failed to initialize engine: {e}")
        print("\n   Make sure the OpenDSS RPC service is running:")
        print("   docker run -p 8000:8000 opendss-service")
        return

    # Get topology
    print("\n2. Getting network topology...")
    topology = engine.get_topology()
    print(f"   Circuit: {topology.name}")
    print(f"   Buses: {len(topology.buses)}")
    print(f"   Lines: {len(topology.lines)}")
    print(f"   Generators: {len(topology.generators)}")
    print(f"   Loads: {len(topology.loads)}")

    # Run power flow
    print("\n3. Running power flow simulation...")
    config = PowerFlowConfig()
    result = engine.run_simulation(config)

    print(f"   Converged: {result.converged}")
    print(f"   Iterations: {result.iterations}")
    if result.error_message:
        print(f"   Error: {result.error_message}")

    # Get system state
    print("\n4. Getting system state...")
    state = engine.get_current_state()

    print(f"   Total Generation: {state.total_generation_mw:.2f} MW")
    print(f"   Total Load: {state.total_load_mw:.2f} MW")
    print(f"   Total Losses: {state.total_losses_mw:.2f} MW")

    # Show sample bus voltages
    print("\n5. Sample bus voltages (first 5 buses):")
    for bus_id, bus_state in list(state.buses.items())[:5]:
        bus_info = topology.buses[bus_id]
        print(
            f"   {bus_info.name}: {bus_state.voltage_pu:.4f} pu, "
            f"{bus_state.angle_deg:.2f}°"
        )

    # Show sample line flows
    print("\n6. Sample line flows (first 5 lines):")
    for line_id, line_state in list(state.lines.items())[:5]:
        line_info = topology.lines[line_id]
        print(
            f"   {line_info.name}: P={line_state.p_from_mw:.3f} MW, "
            f"Q={line_state.q_from_mvar:.3f} MVAr"
        )

    # Control example: Modify a load
    print("\n7. Control example: Modifying load setpoint...")
    if topology.loads:
        load_id = list(topology.loads.keys())[0]
        load_info = topology.loads[load_id]

        print(f"   Modifying load: {load_info.name}")
        print(f"   Original: P={load_info.p_mw:.3f} MW, Q={load_info.q_mvar:.3f} MVAr")

        # Increase load by 10%
        new_p = load_info.p_mw * 1.1
        new_q = load_info.q_mvar * 1.1
        engine.set_load_demand(load_id, new_p, new_q)

        print(f"   New: P={new_p:.3f} MW, Q={new_q:.3f} MVAr")

        # Re-run power flow
        result = engine.run_simulation()
        print(f"   Re-run converged: {result.converged}")

    # Time-stepped simulation
    print("\n8. Time-stepped simulation (10 steps)...")
    for step in range(10):
        result = engine.run_simulation()
        state = engine.get_current_state()

        if (step + 1) % 5 == 0:
            print(
                f"   Step {step+1}: Gen={state.total_generation_mw:.2f} MW, "
                f"Load={state.total_load_mw:.2f} MW"
            )

    print("\n" + "=" * 70)
    print("Example completed successfully!")
    print("=" * 70)


if __name__ == "__main__":
    main()
