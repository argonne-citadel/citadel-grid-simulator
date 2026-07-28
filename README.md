# CITADEL Grid Simulator

A power system simulator with pluggable power-flow engines (pandapower, OpenDSS)
and DER (Distributed Energy Resource) support, for cybersecurity and zero-trust
policy research. The simulator exposes a live grid model over Modbus TCP as a
SCADA RTU, plus a web dashboard for real-time visualization.

## Overview

- **Multiple grid models**: IEEE 33-bus radial feeder (default), a legacy Dickert
  LV distribution model, an IEEE 8500-node and IEEE 123-bus feeder (via OpenDSS,
  based on the Siemens PowerGym benchmarks), and an external CyDERMS composite
  model.
- **Two power-flow engines**: pandapower (steady-state, in-process) and OpenDSS
  (via `opendssdirect`, also in-process), unified behind a common engine
  interface.
- **Real-time simulation**: configurable timestep (default 1 s; safe for all
  bundled models, including the ~4900-bus IEEE 8500 feeder, which takes ~0.5 s
  per step).
- **SCADA protocol**: Modbus TCP server with a dynamically sized register map
  driven by the loaded model's topology. (DNP3 is present in name only — see
  [SCADA Protocols](#scada-protocols).)
- **Web dashboard**: live topology/state visualization plus an HMI-style control
  panel (some control-panel actions are stubbed — see
  [Web Dashboard and Control Panel](#web-dashboard-and-control-panel)).
- **Thread-safe**: a shared engine lock serializes the simulation loop against
  concurrent Modbus writes; writes that can't acquire the lock in time return a
  Modbus `DEVICE_BUSY` exception rather than blocking indefinitely.

## Installation

```bash
# Using micromamba (recommended)
micromamba env create -f environment.yml
micromamba activate grid-simulator

# Or using conda
conda env create -f environment.yml
conda activate grid-simulator
```

For development (linting, type-checking, tests), use `environment-dev.yml`
instead (`make init`).

## Configuration

Runtime behavior is controlled almost entirely by CLI flags (see
[Running the Simulator](#running-the-simulator)); the only setting read from an
environment variable is the simulation timestep:

```bash
TIMESTEP_SECONDS=1.0   # seconds per simulation step; see src/settings.py
```

For local overrides beyond that, copy the example settings file:

```bash
cp src/settingslocal.py.example src/settingslocal.py
```

`src/settings.py` imports `settingslocal.py` if present, so anything defined
there overrides the module-level defaults.

## Running the Simulator

`--model` is required — there is no default that applies automatically; the
process raises at startup if it isn't set.

```bash
# PandaPower model, SCADA mode (Modbus + web dashboard)
citadel-simulator --mode scada --model baran-wu-33

# OpenDSS model (engine is inferred from the .dss extension)
citadel-simulator --mode scada --model /usr/app/examples/IEEE37Bus_PV.dss

# Or via Makefile / directly from source
make run
cd src && python -m main --mode scada --model baran-wu-33
```

| Flag | Default | Notes |
|---|---|---|
| `--mode` | `standalone` | `standalone` runs the simulation loop only; `scada` also starts Modbus, the web dashboard, and (if not disabled) the DNP3 stub. |
| `--engine` | inferred from `--model` | `pandapower` or `opendss`; a `.dss` suffix on `--model` selects `opendss`, anything else selects `pandapower`. |
| `--model` | *(required)* | PandaPower: a model name (`baran-wu-33`, `cyderms-composite`). OpenDSS: a path to a `.dss` file. |
| `--modbus-port` | `502` | |
| `--dnp3-port` | `20000` | Accepted for compatibility; DNP3 doesn't currently start regardless (see below). |
| `--no-dnp3` / `--no-modbus` | off | Disable the respective protocol server. |
| `--duration` | run indefinitely | Simulation duration in seconds. |
| `--enable-grid-stix` / `--export-stix <path>` | off | Grid-STIX telemetry annotation/export. |

The shipped Docker image runs `--mode scada --no-dnp3 --enable-grid-stix`, with
`--model` set from the `GRID_MODEL` environment/compose variable.

## Grid Models

The simulator is model-agnostic at the engine level (see
[Engines](#engines)); each model supplies a network topology plus DER/storage
placement.

### Baran-Wu 33-Bus (default)

IEEE 33-bus radial MV feeder (12.66 kV, `pandapower.networks.case33bw()`) with
6 DERs (4 PV + 2 wind, ~30% penetration against ~3.7 MW load) and 2 battery
storage units. This replaced Dickert LV as the default because the Dickert
network produced voltage violations under North American limits and
non-physical fallback behavior after breaker trips (see
`src/models/baran_wu_33.py`).

> Baran, M. E., & Wu, F. F. (1989). Network reconfiguration in distribution
> systems for loss reduction and load balancing. *IEEE Transactions on Power
> Delivery*, 4(2), 1401-1407.

### Dickert LV Network (secondary)

Still available (`src/models/dickert_lv.py`) and used by some tests and
examples, but no longer the default:

```
@inproceedings{dickert2013,
  author = {Dickert, Jörg and Domagk, Max and Schegner, Peter},
  year = {2013},
  month = {06},
  title = {Benchmark Low Voltage Distribution Networks Based on Cluster Analysis of Actual Grid Properties},
  doi = {10.1109/PTC.2013.6652250}
}
```

### IEEE 8500-Node and IEEE 123-Bus (OpenDSS)

Full feeder models from the Siemens PowerGym benchmark suite, fetched at
Docker build time, with a rooftop-PV overlay layered on top:

- **IEEE 8500**: ~4900 buses, 12.47 kV, ~11 MW peak, 15 PV clusters (~30%
  penetration).
- **IEEE 123**: ~120 buses, 4.16 kV, ~3.5 MW peak, 15 PV sites (~32%
  penetration), zoned for authorization-policy experimentation.

Run via `--model examples/Run_IEEE8500_ZTCARD.dss` or
`--model examples/Run_IEEE123_ZTCARD.dss` (OpenDSS engine, inferred from the
`.dss` extension). Storage control and transformer-tap control are not
implemented on the OpenDSS engine.

### CyDERMS Composite

An adapter (`src/models/cyderms_composite.py`) around an external `cyderms`
package (not bundled in this repository) that adds reactor-generator support.
Requires the `cyderms` package to be available on the Python path.

## Engines

`PowerSystemEngine` (`src/engines/base.py`) is the common interface both
engines implement: topology/state queries, control operations (breaker,
generator, load, storage, transformer tap), `reset()`, and a command dispatcher.

- **`PandaPowerEngine`**: full read/control support, plus DER output modulation
  via a diurnal-profile-with-noise model (`src/engines/der_profiles.py`) that's
  applied before each power flow, Grid-STIX export, and reactor-generator
  support for CyDERMS models.
- **`OpenDSSEngine`**: runs OpenDSS in-process via `opendssdirect`. PV output
  comes from the loadshape baked into the `.dss` file rather than the
  pandapower-side DER profile model. Storage control and transformer-tap
  control raise `NotImplementedError`.

## SCADA Protocols

### Modbus TCP

Default port `502`. The register map is generated per-model from the loaded
topology (ranges below scale with the model's bus/line/generator/load counts,
they aren't fixed at exactly 1000 registers each):

| Range | Contents |
|---|---|
| Holding Registers 0-999 | Bus voltages (pu × 1000, read-only) |
| Holding Registers 1000-1999 | Line active power flow (MW × 100, read-only) |
| Holding Registers 2000-2999 | Line reactive power flow (MVAR × 100, read-only) |
| Holding Registers 3000-3999 | Line loading (% × 10, read-only) |
| Holding Registers 4000-4999 | DER active power (MW × 100, read-only) |
| Holding Registers 5000-5999 | Load active power (MW × 100, read-only) |
| Coils 0-999 | Breaker status (closed = 1, read-only) |
| Coils 1000-1999 | Breaker control (write) |
| Holding Registers 10000-10999 | DER setpoint (write, integer kW) |
| Holding Registers 11000-11999 | Load demand adjustment (write, integer kW) |
| Holding Registers 12000-12999 | Bus voltage setpoint via reactive injection (write, integer mvar × 10; requires an engine that supports it — OpenDSS doesn't) |

Writes acquire a shared lock against the simulation loop. If the lock isn't
available within the engine-lock timeout (defaults to the simulation
timestep), the server returns Modbus exception code `DEVICE_BUSY` (0x06)
instead of blocking — this keeps writes from stalling behind a running power
flow, and keeps two threads from touching the (thread-unsafe) OpenDSS engine
at once.

### DNP3 Outstation

**Not implemented.** `GridDNP3Outstation` raises `NotImplementedError` on
construction — the Python DNP3 libraries this depended on are no longer
compatible with the rest of the stack. `--dnp3-port` is still accepted as a
CLI flag for compatibility, but nothing listens on it. The shipped Docker
image always runs with `--no-dnp3`. Use Modbus TCP instead.

## Web Dashboard and Control Panel

Both are routes on the same Flask app (default port `8080`, started in
`--mode scada`):

| Route | Purpose |
|---|---|
| `GET /` | Real-time Plotly network visualization (voltages, line loading, generation/load/storage, polls `/state` every 5s). |
| `GET /control` | HMI-style control panel — see caveat below. |
| `GET /state` | Latest grid state as JSON. |
| `GET /topology` | Static connectivity only, no power-flow results. |
| `GET /health` | Liveness check. |
| `GET /metrics` | Prometheus exposition (see [Metrics](#metrics)). |
| `POST /reset` | Resets the loaded model and restarts the simulation loop. |

**Control panel caveat**: the breaker-control and generator-setpoint actions on
`/control` are currently stubbed — they log the requested action and return
success, but do not call through to the engine, so they don't actually change
grid state. Only the "read measurements" action reflects live simulator
state. The panel's role selector and authorization-decision readout are also
not backed by a real authorization check today: the code retains an
`authorization_wrapper` extension point (and docstrings describing an
Envoy/OPA/Neo4j design), but no wrapper is constructed or wired up anywhere in
this repository, so every operation reports `auth_decision="allow"` with zero
latency.

## Model Introspection API

`src/model_introspection.py` is a standalone utility for generating a
protocol-agnostic point mapping (bus voltages, line flows, breakers,
generators, loads, storage) from any `PowerSystemEngine`, independent of the
live Modbus server's own (separately implemented) register map:

```python
from model_introspection import generate_point_mapping

mapping = generate_point_mapping(engine, protocol="modbus")
mapping.export_to_yaml("points.yaml")
```

Useful for building authorization policy or tooling against a model's point
list without hand-maintaining a register table.

## Metrics

`GET /metrics` exposes Prometheus metric definitions for authorization
decision latency/counts (`src/metrics.py`) — but nothing in the current
codebase calls into these recorders, so today the endpoint exposes metric
*names* with no observations. This is retained instrumentation for a future
authorization integration, not live telemetry.

## Time-Series Data Generation

`src/timeseries/` provides standalone load/solar/wind profile generators. They
are **not** wired into the live simulation loop today — DER/load modulation
during a running simulation comes from the separate, simpler profile model in
`src/engines/der_profiles.py`. Use these generators directly for offline
scenario generation or to build a pandapower-format profile of your own:

```python
from timeseries import LoadProfileGenerator, SolarProfileGenerator

load_gen = LoadProfileGenerator(random_seed=42)
loads = load_gen.generate_multiple_loads(
    num_residential=5,
    num_commercial=2,
    num_days=7,
    season="summer",
)

solar_gen = SolarProfileGenerator(random_seed=42)
solar = solar_gen.generate_with_variability(
    num_days=7,
    rated_capacity_kw=10.0,
    season="summer",
)

load_pp = load_gen.to_pandapower_format(loads, load_indices)
solar_pp = solar_gen.to_pandapower_format(solar, sgen_indices)
```

- **Load**: residential (morning/evening peaks, seasonal, weekday/weekend) and
  commercial (office/retail/industrial) profiles with stochastic noise.
- **Solar**: cosine irradiance curve, seasonal sunrise/sunset, cloud effects,
  inverter efficiency.
- **Wind**: AR(1)-correlated wind speed through a cut-in/rated/cut-out power
  curve.

## Development

```bash
make help        # Show available commands
make init        # Create the dev conda environment
make format      # Format code (ruff format)
make lint        # Check formatting (ruff format --check)
make type-check  # mypy
make security    # bandit + pip-audit
make test        # Run the test suite
make cistage     # format + type-check + security + test
make pipbuild    # Build sdist/wheel for distribution
make run         # Run the simulator
make clean       # Remove generated files and conda environments
```

### Testing

```bash
make test
# or directly
pytest tests/ -v
pytest tests/ --cov=citadel_simulator
```

### SCADA Client Testing

```bash
# Test with a Modbus client
mbpoll -a 1 -r 0 -c 10 localhost
```

## Troubleshooting

**Power flow doesn't converge**: check load/generation balance, network
connectivity, and voltage limits.

**Modbus connection fails**: check the port isn't already in use (port 502
may need elevated privileges), and that the unit ID/register addresses match
the table above.

**Modbus writes return `DEVICE_BUSY`**: the engine lock couldn't be acquired
within the timeout — normal under sustained write load or a slow-converging
power flow (e.g. IEEE 8500); the client should retry.

**DNP3 doesn't start**: expected — DNP3 is not implemented (see
[SCADA Protocols](#scada-protocols)). Use Modbus.

### Debug Mode

```bash
export LOG_LEVEL=DEBUG
python -m src.main --mode scada --model baran-wu-33
```

## License

See LICENSE file for details.

## References

- [Pandapower Documentation](https://pandapower.readthedocs.io/)
- [OpenDSS](https://opendss.epri.com/)
- [Modbus Protocol](https://modbus.org/)
- [Grid-STIX Ontology](https://github.com/argonne-citadel/grid-stix)

## Acknowledgments

This software was developed under U.S. Department of Energy award DE-CR0000049, issued by the Office of Cybersecurity, Energy Security, and Emergency Response (CESER). The prime contractor on this work was Iowa State University, and the ideas herein are influenced by conversations with them. The submitted manuscript has been created by UChicago Argonne, LLC, operator of Argonne National Laboratory. Argonne, a DOE Office of Science laboratory, is operated under Contract No. DE-AC02-06CH11357. The U.S. Government retains for itself, and others acting on its behalf, a paid-up nonexclusive, irrevocable worldwide license in said article to reproduce, prepare derivative works, distribute copies to the public, and perform publicly and display publicly, by or on behalf of the Government.
