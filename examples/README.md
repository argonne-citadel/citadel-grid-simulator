# Engine Abstraction Layer Examples

This directory contains examples demonstrating the new engine abstraction layer.

## Setup

Before running the examples, ensure you have the conda environment set up:

```bash
# Create/update the conda environment
conda env create -f environment.yml

# Or update if it already exists
conda env update -f environment.yml

# Activate the environment
conda activate grid-simulator
```

## Examples

### basic_engine_usage.py

Demonstrates the core functionality of the engine abstraction layer:

1. Creating a PandaPower network
2. Wrapping it with PandaPowerEngine
3. Querying network topology
4. Running power flow simulations
5. Getting grid state
6. Executing control commands (load, generator, breaker)

**Run:**
```bash
python examples/basic_engine_usage.py
```

**Expected Output:**
- Network topology information
- Power flow results
- Grid state (voltages, power flows, etc.)
- Control command execution results

## Key Concepts

### Engine Abstraction

The `PowerSystemEngine` abstract base class provides a unified interface for different power system modeling engines:

- **PandaPowerEngine**: Steady-state power flow solver
- **Future engines**: OpenDSS, PyPSA, FMPy (time-domain solvers)

### Type-Safe Schemas

All data is validated using Pydantic models:

- **Topology**: `NetworkTopology`, `BusInfo`, `LineInfo`, etc.
- **State**: `GridState`, `BusState`, `LineState`, etc.
- **Commands**: `BreakerCommand`, `GeneratorCommand`, etc.
- **Results**: `PowerFlowResult`, `PowerFlowConfig`

### Control Commands

Execute control actions using type-safe command objects:

```python
# Adjust load
cmd = LoadCommand(load_id=0, p_mw=1.5)
engine.execute_command(cmd)

# Control breaker
cmd = BreakerCommand(line_id=0, closed=False)
engine.execute_command(cmd)

# Set generator output
cmd = GeneratorCommand(generator_id=0, p_mw=2.0, q_mvar=0.5)
engine.execute_command(cmd)

# Adjust transformer tap
cmd = TransformerTapCommand(transformer_id=0, tap_position=5)
engine.execute_command(cmd)
```

## Architecture Benefits

1. **Engine Independence**: Switch between PandaPower, OpenDSS, PyPSA without changing application code
2. **Type Safety**: Pydantic validation catches errors at runtime
3. **Clean Interface**: Abstract methods define clear contracts
4. **Extensibility**: Easy to add new engine implementations
5. **Time-Domain Ready**: Interface supports both steady-state and time-domain solvers