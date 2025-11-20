# CITADEL Grid Simulator

A pandapower-based electrical grid simulation system with DERs (Distributed Energy Resources) for cybersecurity and zero-trust policy research. The simulator acts as SCADA substations/RTUs communicating via DNP3 and Modbus protocols.

## Overview

The CITADEL Grid Simulator provides realistic power grid simulation capabilities for research and testing:

- **Modern Grid Model**: Dickert LV distribution network with DER support
- **Real-time Simulation**: 1-second timestep power flow calculations
- **SCADA Protocols**: DNP3 outstation and Modbus server interfaces
- **Time-Series Data**: Realistic load, solar PV, and wind generation profiles
- **Zero-Trust Ready**: Control points for policy enforcement on breakers, loads, and DERs
- **Thread-Safe**: Concurrent operation with command queue and state callbacks

## Installation

```bash
# Using micromamba (recommended)
micromamba env create -f environment.yml
micromamba activate grid-simulator

# Or using conda
conda env create -f environment.yml
conda activate grid-simulator
```

## Configuration

```bash
# Copy example config
cp src/settingslocal.py.example src/settingslocal.py

# Edit configuration as needed
nano src/settingslocal.py
```

## Running the Simulator

```bash
# Using Makefile
make run

# Or directly
cd src && python -m main

# Or using the installed command
citadel-simulator
```

## Grid Models

The simulator provides an extensible framework for power grid modeling. The default implementation uses the Dickert LV distribution network model, but the architecture supports custom grid models.

### Default Model: Dickert LV Network

The included Dickert LV model is specifically designed for DER research and serves as a reference implementation:

- **DER-focused**: Designed for distribution-level DER integration studies
- **Modern architecture**: Reflects current grids with high DER penetration
- **Zero-trust suitable**: Smaller scale ideal for cyber/control experimentation
- **Realistic topology**: Includes residential/commercial loads, solar PV, storage
- **Manageable scale**: Rapid iteration for policy enforcement scenarios

**Network Classes**:
- **Class 1**: Smallest network (default)
- **Class 2-5**: Increasing size and complexity

### Custom Grid Models

The simulator architecture is designed to be model-agnostic. You can implement custom grid models by:

1. Creating a new model class in `src/models/`
2. Implementing the required pandapower network structure
3. Registering the model in the configuration

See `src/models/dickert_lv.py` for a reference implementation.

### Reference

```
@inproceedings{dickert2013,
  author = {Dickert, Jörg and Domagk, Max and Schegner, Peter},
  year = {2013},
  month = {06},
  title = {Benchmark Low Voltage Distribution Networks Based on Cluster Analysis of Actual Grid Properties},
  doi = {10.1109/PTC.2013.6652250}
}
```

## SCADA Protocols

### DNP3 Outstation

**Port**: 20000 (configurable)

**Point Mapping**:
- Analog Inputs 0-N: Bus voltages (per unit)
- Analog Inputs 100+: Line power flows (MW)
- Analog Inputs 200+: DER generation (MW)
- Binary Inputs 0-N: Breaker status (open/closed)
- Binary Outputs 0-N: Breaker controls

**Features**:
- Integrity polls
- Event reporting
- Control operations
- Quality flags
- Timestamps

### Modbus Server

**Port**: 502 (configurable)

**Register Mapping**:
- Holding Registers 0-999: Bus voltages (scaled)
- Holding Registers 1000-1999: Line flows (scaled)
- Holding Registers 2000-2999: DER generation (scaled)
- Coils 0-999: Breaker status
- Coils 1000-1999: Breaker controls

**Features**:
- Function codes: 1, 2, 3, 4, 5, 6, 15, 16
- Exception handling
- Multiple client support

## SCADA Commands

The simulator supports the following control commands:

- `OPEN_BREAKER` - Open a line breaker
- `CLOSE_BREAKER` - Close a line breaker
- `SET_DER_SETPOINT` - Adjust DER power output
- `ADJUST_LOAD` - Modify load demand
- `SET_STORAGE_POWER` - Control energy storage (charge/discharge)

## Configuration

### Environment Variables

```bash
# Simulation
SIMULATION_TIMESTEP=1.0          # seconds
NETWORK_CLASS=1                  # Dickert LV network class (1-5)
NUM_TRANSFORMERS=1               # Number of transformers (1-2)

# DNP3
DNP3_ENABLED=true
DNP3_PORT=20000
DNP3_LOCAL_ADDRESS=1
DNP3_REMOTE_ADDRESS=10

# Modbus
MODBUS_ENABLED=true
MODBUS_PORT=502
MODBUS_UNIT_ID=1

# Logging
LOG_LEVEL=INFO
LOG_FILE=grid_simulator.log
```

### Settings File

See `src/settingslocal.py.example` for all configuration options.

## Time-Series Data Generation

The simulator includes comprehensive time-series generators for realistic load and generation profiles.

### Load Profiles

**Residential**:
- Morning peak: 6-9 AM
- Evening peak: 5-10 PM
- Overnight minimum: 11 PM - 5 AM
- Seasonal variations (summer AC, winter heating)
- Weekday/weekend differences
- Stochastic noise (15%)

**Commercial**:
- Office: 8 AM - 6 PM weekdays
- Retail: 9 AM - 9 PM daily
- Industrial: 24/7 operation
- Stochastic noise (10%)

### Solar PV Profiles

- Cosine irradiance curve (sunrise to sunset)
- Seasonal sun angle variations
- Cloud effects: clear, partly cloudy, cloudy
- Inverter efficiency (96% default)
- Day-to-day variability

### Wind Profiles

- Power curve: cut-in 3 m/s, rated 12 m/s, cut-out 25 m/s
- AR(1) process for temporal correlation
- Cubic power relationship
- Stochastic variations

### Example Usage

```python
from timeseries import LoadProfileGenerator, SolarProfileGenerator

# Generate load profiles
load_gen = LoadProfileGenerator(random_seed=42)
loads = load_gen.generate_multiple_loads(
    num_residential=5,
    num_commercial=2,
    num_days=7,
    season='summer'
)

# Generate solar profiles
solar_gen = SolarProfileGenerator(random_seed=42)
solar = solar_gen.generate_with_variability(
    num_days=7,
    rated_capacity_kw=10.0,
    season='summer'
)

# Convert to pandapower format
load_pp = load_gen.to_pandapower_format(loads, load_indices)
solar_pp = solar_gen.to_pandapower_format(solar, sgen_indices)
```

## Development

### Makefile Commands

```bash
make help      # Show available commands
make install   # Create conda environment
make test      # Run tests
make run       # Run simulator
make clean     # Remove generated files
make lint      # Run linting
make format    # Format code
```

### Testing

```bash
# Run all tests
make test

# Run specific test file
python -m pytest src/tests/test_basic.py -v

# Run with coverage
python -m pytest --cov=src src/tests/
```

### SCADA Client Testing

```bash
# Test with Modbus client
mbpoll -a 1 -r 0 -c 10 localhost

# Test with DNP3 client
# (Use dnp3demo or similar DNP3 master tool)
```

## Performance

- **Timestep**: 1 second (configurable)
- **State history**: 3600 steps (1 hour at 1s timestep)
- **Protocol updates**: On every timestep
- **Typical CPU**: <5% for Class 1 network
- **Memory**: ~100MB for Class 1 network

## Troubleshooting

### Common Issues

**Power flow doesn't converge**:
- Check load/generation balance
- Verify network connectivity
- Review voltage limits

**DNP3 connection fails**:
- Check port 20000 is not in use
- Verify firewall settings
- Check DNP3 addresses match

**Modbus connection fails**:
- Check port 502 is not in use (may need sudo)
- Verify unit ID matches
- Check register addresses

### Debug Mode

```bash
# Enable debug logging
export LOG_LEVEL=DEBUG
python -m src.main
```

## License

See LICENSE file for details.

## References

- [Pandapower Documentation](https://pandapower.readthedocs.io/)
- [DNP3 Protocol](https://www.dnp.org/)
- [Modbus Protocol](https://modbus.org/)
- [Grid-STIX Ontology](https://github.com/argonne-citadel/grid-stix)