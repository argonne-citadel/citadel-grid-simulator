"""
Tests for OpenDSSEngine implementation.

Tests the PowerSystemEngine interface implementation for OpenDSS,
including circuit loading, state management, and control operations.
"""

from datetime import datetime
from pathlib import Path

import pytest

from src.engines.opendss_engine import OpenDSSEngine
from src.schemas.common import BusType, DERType, DeviceStatus
from src.schemas.results import PowerFlowConfig
from src.schemas.state import BusState, GeneratorState, LineState, LoadState
from src.schemas.topology import BusInfo, GeneratorInfo, LineInfo, LoadInfo

# Path to test DSS file
TEST_DSS_FILE = Path(__file__).parent.parent / "examples" / "IEEE37Bus_PV.dss"


@pytest.fixture
def dss_file_path():
    """Return path to test DSS file."""
    if not TEST_DSS_FILE.exists():
        pytest.skip(f"Test DSS file not found: {TEST_DSS_FILE}")
    return str(TEST_DSS_FILE)


@pytest.fixture
def opendss_engine(dss_file_path):
    """Create OpenDSS engine with test circuit."""
    try:
        engine = OpenDSSEngine(dss_file_path)
        return engine
    except Exception as e:
        pytest.skip(f"Could not initialize OpenDSS engine: {e}")


class TestEngineInitialization:
    """Test engine initialization and setup."""

    def test_create_engine_from_dss_file(self, dss_file_path):
        """Test creating engine from DSS file."""
        engine = OpenDSSEngine(dss_file_path)
        assert engine is not None
        assert engine.dss_file_path == dss_file_path

    def test_engine_loads_circuit(self, opendss_engine):
        """Test that circuit is loaded successfully."""
        # Check that ID mappings are populated
        assert len(opendss_engine._bus_name_to_id) > 0
        assert len(opendss_engine._line_name_to_id) > 0
        assert len(opendss_engine._load_name_to_id) > 0

    def test_engine_has_pv_systems(self, opendss_engine):
        """Test that PV systems are detected."""
        # IEEE37Bus_PV should have PV systems
        assert len(opendss_engine._pv_name_to_id) > 0


class TestTopologyRetrieval:
    """Test topology information retrieval."""

    def test_get_topology(self, opendss_engine):
        """Test getting complete network topology."""
        topology = opendss_engine.get_topology()

        assert topology is not None
        assert len(topology.buses) > 0
        assert len(topology.lines) > 0
        assert len(topology.loads) > 0
        assert topology.name is not None
        assert topology.base_mva == 1.0
        assert topology.frequency_hz == 60.0

    def test_get_bus_info(self, opendss_engine):
        """Test getting bus information."""
        topology = opendss_engine.get_topology()
        bus_ids = list(topology.buses.keys())

        if bus_ids:
            bus_id = bus_ids[0]
            bus_info = opendss_engine.get_bus_info(bus_id)

            assert isinstance(bus_info, BusInfo)
            assert bus_info.bus_id == bus_id
            assert bus_info.name is not None
            assert bus_info.voltage_nominal_kv > 0

    def test_get_line_info(self, opendss_engine):
        """Test getting line information."""
        topology = opendss_engine.get_topology()
        line_ids = list(topology.lines.keys())

        if line_ids:
            line_id = line_ids[0]
            line_info = opendss_engine.get_line_info(line_id)

            assert isinstance(line_info, LineInfo)
            assert line_info.line_id == line_id
            assert line_info.name is not None
            assert line_info.from_bus is not None
            assert line_info.to_bus is not None

    def test_get_load_info(self, opendss_engine):
        """Test getting load information."""
        topology = opendss_engine.get_topology()
        load_ids = list(topology.loads.keys())

        if load_ids:
            load_id = load_ids[0]
            load_info = opendss_engine.get_load_info(load_id)

            assert isinstance(load_info, LoadInfo)
            assert load_info.load_id == load_id
            assert load_info.name is not None
            assert load_info.bus is not None

    def test_get_generator_info(self, opendss_engine):
        """Test getting generator (PV) information."""
        topology = opendss_engine.get_topology()
        gen_ids = list(topology.generators.keys())

        if gen_ids:
            gen_id = gen_ids[0]
            gen_info = opendss_engine.get_generator_info(gen_id)

            assert isinstance(gen_info, GeneratorInfo)
            assert gen_info.generator_id == gen_id
            assert gen_info.name is not None
            assert gen_info.bus is not None
            assert gen_info.der_type == DERType.SOLAR_PV

    def test_storage_not_implemented(self, opendss_engine):
        """Test that storage raises NotImplementedError."""
        from src.schemas.common import StorageID

        with pytest.raises(KeyError, match="Storage not implemented"):
            opendss_engine.get_storage_info(StorageID(0))


class TestSimulation:
    """Test power flow simulation."""

    def test_run_simulation_default_config(self, opendss_engine):
        """Test running simulation with default configuration."""
        result = opendss_engine.run_simulation()

        assert result is not None
        assert hasattr(result, "converged")
        assert hasattr(result, "iterations")
        assert result.iterations >= 0

    def test_run_simulation_custom_config(self, opendss_engine):
        """Test running simulation with custom configuration."""
        config = PowerFlowConfig(max_iterations=50)
        result = opendss_engine.run_simulation(config)

        assert result is not None
        assert result.config == config

    def test_get_convergence_status(self, opendss_engine):
        """Test getting convergence status."""
        # Run simulation first
        opendss_engine.run_simulation()

        # Check convergence status
        converged = opendss_engine.get_convergence_status()
        assert isinstance(converged, bool)


class TestStateRetrieval:
    """Test state information retrieval."""

    def test_get_current_state_after_simulation(self, opendss_engine):
        """Test getting state after running simulation."""
        # Run simulation first
        opendss_engine.run_simulation()

        # Get current state
        state = opendss_engine.get_current_state()

        assert state is not None
        assert len(state.buses) > 0
        assert len(state.lines) > 0
        assert len(state.loads) > 0
        assert isinstance(state.timestamp, datetime)
        assert isinstance(state.converged, bool)

    def test_bus_state_has_voltages(self, opendss_engine):
        """Test that bus states contain voltage information."""
        opendss_engine.run_simulation()
        state = opendss_engine.get_current_state()

        if state.buses:
            bus_state = next(iter(state.buses.values()))
            assert isinstance(bus_state, BusState)
            assert bus_state.voltage_pu is not None
            assert bus_state.angle_deg is not None

    def test_line_state_has_power_flows(self, opendss_engine):
        """Test that line states contain power flow information."""
        opendss_engine.run_simulation()
        state = opendss_engine.get_current_state()

        if state.lines:
            line_state = next(iter(state.lines.values()))
            assert isinstance(line_state, LineState)
            assert line_state.p_from_mw is not None
            assert line_state.q_from_mvar is not None

    def test_load_state_has_power(self, opendss_engine):
        """Test that load states contain power information."""
        opendss_engine.run_simulation()
        state = opendss_engine.get_current_state()

        if state.loads:
            load_state = next(iter(state.loads.values()))
            assert isinstance(load_state, LoadState)
            assert load_state.p_mw is not None
            assert load_state.q_mvar is not None

    def test_generator_state_has_power(self, opendss_engine):
        """Test that generator states contain power information."""
        opendss_engine.run_simulation()
        state = opendss_engine.get_current_state()

        if state.generators:
            gen_state = next(iter(state.generators.values()))
            assert isinstance(gen_state, GeneratorState)
            assert gen_state.p_mw is not None
            assert gen_state.q_mvar is not None


class TestControlOperations:
    """Test control operations."""

    def test_set_breaker_status(self, opendss_engine):
        """Test setting breaker (line enable) status."""
        topology = opendss_engine.get_topology()
        line_ids = list(topology.lines.keys())

        if line_ids:
            line_id = line_ids[0]

            # Open breaker
            opendss_engine.set_breaker_status(line_id, False)

            # Close breaker
            opendss_engine.set_breaker_status(line_id, True)

            # Should not raise exception

    def test_set_load_demand(self, opendss_engine):
        """Test setting load demand."""
        topology = opendss_engine.get_topology()
        load_ids = list(topology.loads.keys())

        if load_ids:
            load_id = load_ids[0]

            # Set load power
            opendss_engine.set_load_demand(load_id, p_mw=0.1, q_mvar=0.05)

            # Should not raise exception

    def test_set_generator_setpoint(self, opendss_engine):
        """Test setting generator setpoint."""
        topology = opendss_engine.get_topology()
        gen_ids = list(topology.generators.keys())

        if gen_ids:
            gen_id = gen_ids[0]

            # Set generator power
            opendss_engine.set_generator_setpoint(gen_id, p_mw=0.05)

            # Should not raise exception

    def test_storage_control_not_implemented(self, opendss_engine):
        """Test that storage control raises NotImplementedError."""
        from src.schemas.common import StorageID

        with pytest.raises(NotImplementedError):
            opendss_engine.set_storage_power(StorageID(0), 0.1)

    def test_set_transformer_tap_updates_tap_position(self, opendss_engine):
        """set_transformer_tap must actually move the regulator's tap.

        This replaces a stale test that asserted set_transformer_tap raised
        NotImplementedError — that was true before regulator control was
        wired up (contract: `dss.RegControls.TapNumber()`) and is not true
        of the code under test. Kept at the same altitude as the old test:
        it exercises the public set/read round trip rather than the
        RegControls internals directly. Uses a tap value (10) distinct from
        the regulator's own control-solved resting position so a no-op
        set_transformer_tap could not accidentally look correct.
        """
        from src.schemas.common import TransformerID

        topology = opendss_engine.get_topology()
        regulator_id = next(iter(topology.regulators))

        opendss_engine.set_transformer_tap(TransformerID(regulator_id), 10)
        assert (
            opendss_engine.get_regulator_tap_position(TransformerID(regulator_id)) == 10
        )

    def test_set_transformer_tap_does_not_persist_across_solve(self, opendss_engine):
        """A tap forced via `set_transformer_tap` does not survive
        `run_simulation()`.

        This is the property that motivated the Modbus regulator-control
        write band's move from tap-forcing to voltage-setpoint commanding
        (see test_voltage_regulator_modbus.py): OpenDSS's RegControl runs
        closed-loop automatic voltage regulation on every `Solve()` and
        reverts any externally-forced tap back to its own equilibrium.
        `set_transformer_tap` is retained as a direct-control primitive,
        but this is exactly why it is not what the Modbus band uses — a
        forced tap is only true until the next solve.

        The equilibrium value is read from the engine's own topology
        before forcing anything, rather than a hardcoded constant, so this
        test does not depend on which regulator happens to be first or
        what its particular resting tap is — only that forcing-then-solving
        returns to whatever that resting value was.
        """
        from src.schemas.common import TransformerID

        topology = opendss_engine.get_topology()
        regulator_id = next(iter(topology.regulators))
        equilibrium_tap = topology.regulators[regulator_id].tap_position

        forced_tap = -10
        assert forced_tap != equilibrium_tap
        opendss_engine.set_transformer_tap(TransformerID(regulator_id), forced_tap)
        assert (
            opendss_engine.get_regulator_tap_position(TransformerID(regulator_id))
            == forced_tap
        ), "set_transformer_tap did not move the tap immediately"

        opendss_engine.run_simulation()

        tap_after_solve = opendss_engine.get_regulator_tap_position(
            TransformerID(regulator_id)
        )
        assert tap_after_solve != forced_tap, (
            f"tap held at the forced value {forced_tap} through "
            "run_simulation(); expected RegControl's closed-loop control "
            "to override it."
        )
        assert tap_after_solve == equilibrium_tap, (
            f"tap reverted to {tap_after_solve} after run_simulation(), not "
            f"to the regulator's own pre-force equilibrium ({equilibrium_tap})."
        )


class TestRegulatorControl:
    """Test voltage regulator identification and tap-bound enforcement.

    IEEE37Bus_PV.dss defines exactly two RegControls (`creg1a` on
    `reg1a`, `creg1c` on `reg1c`) among ten `New Transformer.*` objects
    total (SubXF, XFM1, reg1a, reg1c, pv_up, pv_up1..pv_up5). Both counts
    are hand-derived from reading the .dss source, not from running the
    engine.
    """

    def test_regulators_identified_and_not_duplicated_as_transformers(
        self, opendss_engine
    ):
        """Regulators must be relabelled out of `transformers`, not copied.

        Fails if either: (a) RegControl-bearing transformers are missed
        entirely (regulators empty), or (b) they are exported into BOTH
        `regulators` and `transformers` (device count would then exceed 10,
        double-counting real hardware into the topology).
        """
        topology = opendss_engine.get_topology()
        regulator_names = {info.name for info in topology.regulators.values()}
        transformer_names = {info.name for info in topology.transformers.values()}

        assert len(regulator_names) > 0

        # Disjoint by NAME: `regulators` and `transformers` are independent
        # dicts with independently-assigned integer keys, so key-based
        # disjointness would be meaningless (both legitimately contain 0).
        assert regulator_names.isdisjoint(transformer_names)

        # 10 total `New Transformer.*` objects in the .dss source; relabelling
        # must partition them, not drop or double-count any.
        assert len(regulator_names) + len(transformer_names) == 10

    def test_zero_regcontrols_raises_runtime_error(self, tmp_path):
        """A circuit with a transformer but no RegControl must fail loudly.

        Per AGENTS.md fail-loud: silently returning an empty regulators dict
        would leave a regulated-feeder scenario with nothing to command and
        no signal that anything is wrong.
        """
        from src.engines.opendss_engine import OpenDSSEngine

        dss_content = """
        Clear
        New object=circuit.no_regs basekv=4.8 pu=1.0 phases=3
        New Transformer.T1 Phases=3 Windings=2 Xhl=2
        ~ wdg=1 bus=sourcebus conn=wye kv=4.8 kva=500
        ~ wdg=2 bus=loadbus conn=wye kv=4.8 kva=500
        New Load.L1 Bus1=loadbus Phases=3 kV=4.8 kW=100 kVAR=50
        Set VoltageBases = "4.8"
        CalcVoltageBases
        Solve
        """
        dss_file = tmp_path / "no_regs.dss"
        dss_file.write_text(dss_content)

        with pytest.raises(RuntimeError):
            OpenDSSEngine(str(dss_file))

    def test_tap_beyond_num_taps_bound_is_rejected(self, opendss_engine):
        """Tap bounds must derive from the regulator's own NumTaps, not a
        hardcoded constant.

        IEEE37Bus_PV's reg1a/reg1c both report NumTaps()=32 (hand-checked via
        opendssdirect against the live fixture), giving a symmetric valid
        range of [-16, 16] (32 taps split about neutral). This test derives
        that bound itself from the fixture rather than hardcoding 16, so it
        stays correct if the fixture ever changes regulator hardware. Values
        at the boundary (+-16) must be accepted; one step beyond must not.
        """
        import opendssdirect as dss

        from src.schemas.common import TransformerID

        topology = opendss_engine.get_topology()
        regulator_id = next(iter(topology.regulators))
        regulator_name = topology.regulators[regulator_id].name

        dss.Transformers.Name(regulator_name)
        num_taps = dss.Transformers.NumTaps()
        max_tap = num_taps // 2

        # Boundary values must be accepted.
        opendss_engine.set_transformer_tap(TransformerID(regulator_id), max_tap)
        assert (
            opendss_engine.get_regulator_tap_position(TransformerID(regulator_id))
            == max_tap
        )
        opendss_engine.set_transformer_tap(TransformerID(regulator_id), -max_tap)
        assert (
            opendss_engine.get_regulator_tap_position(TransformerID(regulator_id))
            == -max_tap
        )

        # One step beyond the bound in either direction must be rejected.
        with pytest.raises(ValueError):
            opendss_engine.set_transformer_tap(TransformerID(regulator_id), max_tap + 1)
        with pytest.raises(ValueError):
            opendss_engine.set_transformer_tap(
                TransformerID(regulator_id), -(max_tap + 1)
            )


class TestErrorHandling:
    """Test error handling."""

    def test_invalid_bus_id(self, opendss_engine):
        """Test handling of invalid bus ID."""
        from src.schemas.common import BusID

        with pytest.raises(KeyError):
            opendss_engine.get_bus_info(BusID(99999))

    def test_invalid_line_id(self, opendss_engine):
        """Test handling of invalid line ID."""
        from src.schemas.common import LineID

        with pytest.raises(KeyError):
            opendss_engine.get_line_info(LineID(99999))

    def test_invalid_load_id(self, opendss_engine):
        """Test handling of invalid load ID."""
        from src.schemas.common import LoadID

        with pytest.raises(KeyError):
            opendss_engine.get_load_info(LoadID(99999))

    def test_invalid_generator_id(self, opendss_engine):
        """Test handling of invalid generator ID."""
        from src.schemas.common import GeneratorID

        with pytest.raises(KeyError):
            opendss_engine.get_generator_info(GeneratorID(99999))

    def test_nonexistent_dss_file(self):
        """Test handling of nonexistent DSS file."""
        with pytest.raises(RuntimeError, match="Failed to load circuit"):
            OpenDSSEngine("/nonexistent/file.dss")


class TestCompileFailuresSurfaceLoudly:
    """Redmine #1294: a failed OpenDSS Compile must raise, not silently
    leave the caller with a cleared/empty circuit.

    These exercise the `dss.Text.Command` compile path directly, distinct
    from `test_nonexistent_dss_file` above: that case is intercepted by
    the pre-existing `Path.exists()` check before OpenDSS ever sees the
    path, so it never reaches Compile at all. The cases here pass the
    `Path.exists()` check and only fail inside OpenDSS itself, which is
    exactly the code path `dss.run_command` used to fail silently on.
    """

    def test_unreadable_path_raises_with_dss_error_text(self):
        """A path that exists but is not a valid DSS file (a directory)
        reaches Compile and must surface OpenDSS's own error text, not an
        `AttributeError` from code downstream that assumed a loaded
        circuit, and not silence.
        """
        import tempfile

        with tempfile.TemporaryDirectory() as unreadable_dir:
            with pytest.raises(RuntimeError, match="Failed to load circuit"):
                OpenDSSEngine(unreadable_dir)

    def test_compile_success_with_empty_circuit_raises(self, tmp_path):
        """A Compile that raises no DSSException but produces a circuit
        with zero buses must still fail loudly.

        `New Circuit.*` alone (no lines/loads/transformers) is a
        syntactically valid DSS file — OpenDSS compiles it without error —
        but `dss.Circuit.NumBuses()` reports 0 immediately afterward. This
        is the exact "successful compile, empty circuit" hazard described
        in Redmine #1294 that `dss.Text.Command` raising alone would not
        catch, since no exception is raised here.
        """
        empty_circuit_dss = tmp_path / "empty_circuit.dss"
        empty_circuit_dss.write_text(
            "Clear\nNew Circuit.empty_test basekv=4.8 pu=1.0 phases=3\n"
        )

        with pytest.raises(RuntimeError, match="empty circuit"):
            OpenDSSEngine(str(empty_circuit_dss))

    def test_happy_path_still_loads_real_model(self, dss_file_path):
        """The IEEE37 fixture used elsewhere in this file must still load
        cleanly through the Text.Command + non-empty-circuit guard.
        """
        engine = OpenDSSEngine(dss_file_path)
        assert len(engine._bus_name_to_id) > 0


class TestFuzzSeedDeterminism:
    """`fuzz_seed` seeds a private `random.Random` instance that drives all
    randomness in `reset()`: +/-5% load kW/kvar fuzz, +/-3% PVSystem Pmpp
    fuzz, and `_pinned_hour` drawn uniformly from [6, 18].

    Property under test: two engines built with the same seed consume the
    same RNG stream in the same order, so their post-reset state is exactly
    (bit-for-bit) identical -- not approximately equal. This is the basis
    for paired-baseline determinism, so a tolerance-based comparison would
    not actually test the contract.

    Uses the shared IEEE37 `dss_file_path` fixture, NOT the IEEE-123
    ZT-CARD feeder. Run_IEEE123_ZTCARD.dss is tracked, but line 35 of it
    does `Compile (ieee123/IEEE123Master.dss)`, and `examples/ieee123/` is
    upstream feeder data the Dockerfile provisions into the image at build
    time rather than something tracked in git -- so a test depending on it
    passes on a dev machine and fails in a bare CI checkout. The
    determinism properties under test are feeder-independent: they concern
    the engine's RNG, not the circuit it solves.

    IMPORTANT: opendssdirect wraps a single process-global circuit, not a
    per-instance one -- constructing or reset()-ing a second `OpenDSSEngine`
    overwrites the first engine's live circuit state in place. Every test
    below therefore captures one engine's post-reset voltages immediately,
    before constructing or reset()-ing the next engine; comparing two
    `get_current_state()` snapshots taken from live engine objects after
    *both* have reset would silently compare the same (globally last-written)
    circuit against itself.
    """

    @staticmethod
    def _bus_voltages(engine: OpenDSSEngine) -> dict:
        """Map bus_id -> voltage_pu for every bus in the engine's state."""
        state = engine.get_current_state()
        return {bus_id: bus.voltage_pu for bus_id, bus in state.buses.items()}

    def test_same_seed_produces_identical_voltages_after_one_reset(self, dss_file_path):
        """Same seed, single reset each: per-bus voltages must be exactly
        equal (==), and _pinned_hour must match exactly.
        """
        engine_a = OpenDSSEngine(dss_file_path, fuzz_seed=42)
        engine_a.reset()
        voltages_a = self._bus_voltages(engine_a)
        pinned_hour_a = engine_a._pinned_hour

        engine_b = OpenDSSEngine(dss_file_path, fuzz_seed=42)
        engine_b.reset()
        voltages_b = self._bus_voltages(engine_b)
        pinned_hour_b = engine_b._pinned_hour

        assert voltages_a.keys() == voltages_b.keys()
        for bus_id in voltages_a:
            assert voltages_a[bus_id] == voltages_b[bus_id], (
                f"bus {bus_id}: {voltages_a[bus_id]} != {voltages_b[bus_id]} "
                "-- same fuzz_seed must yield bit-identical state"
            )
        assert pinned_hour_a == pinned_hour_b

    def test_same_seed_stream_order_holds_across_multiple_resets(self, dss_file_path):
        """Resets are not individually reproducible in isolation, but the
        Nth reset of one seeded engine must match the Nth reset of another
        seeded identically, because both consume the same RNG stream in the
        same order. Reset three times on each before comparing.
        """
        engine_a = OpenDSSEngine(dss_file_path, fuzz_seed=7)
        for _ in range(3):
            engine_a.reset()
        voltages_a = self._bus_voltages(engine_a)
        pinned_hour_a = engine_a._pinned_hour

        engine_b = OpenDSSEngine(dss_file_path, fuzz_seed=7)
        for _ in range(3):
            engine_b.reset()
        voltages_b = self._bus_voltages(engine_b)
        pinned_hour_b = engine_b._pinned_hour

        assert voltages_a.keys() == voltages_b.keys()
        for bus_id in voltages_a:
            assert voltages_a[bus_id] == voltages_b[bus_id]
        assert pinned_hour_a == pinned_hour_b

    def test_different_seeds_produce_different_voltages(self, dss_file_path):
        """Anti-vacuous-pass guard: without this, an implementation that
        fuzzed nothing at all would pass the same-seed tests above. Assert
        the max absolute per-bus voltage difference is strictly > 0.
        """
        engine_a = OpenDSSEngine(dss_file_path, fuzz_seed=1)
        engine_a.reset()
        voltages_a = self._bus_voltages(engine_a)

        engine_b = OpenDSSEngine(dss_file_path, fuzz_seed=2)
        engine_b.reset()
        voltages_b = self._bus_voltages(engine_b)

        assert voltages_a.keys() == voltages_b.keys()
        max_abs_diff = max(
            abs(voltages_a[bus_id] - voltages_b[bus_id]) for bus_id in voltages_a
        )
        assert max_abs_diff > 0, (
            "different fuzz_seed values produced identical voltages -- "
            "fuzzing does not appear to be seed-dependent"
        )

    def test_unseeded_engines_produce_different_voltages(self, dss_file_path):
        """Two separately-constructed default (fuzz_seed=None) engines must
        differ after one reset each -- across-episode variation must remain
        genuine when no seed is supplied, since repeated-measures statistics
        depend on it.
        """
        engine_a = OpenDSSEngine(dss_file_path)
        engine_a.reset()
        voltages_a = self._bus_voltages(engine_a)

        engine_b = OpenDSSEngine(dss_file_path)
        engine_b.reset()
        voltages_b = self._bus_voltages(engine_b)

        assert voltages_a.keys() == voltages_b.keys()
        max_abs_diff = max(
            abs(voltages_a[bus_id] - voltages_b[bus_id]) for bus_id in voltages_a
        )
        assert max_abs_diff > 0, (
            "two unseeded engines produced identical voltages -- unseeded "
            "construction must not be accidentally deterministic"
        )

    def test_unseeded_engines_unaffected_by_global_random_seed(self, dss_file_path):
        """Seeding Python's global `random` module to the same value before
        constructing two unseeded engines must NOT make their output
        identical. The engine is specified to use a private `random.Random`
        instance, never the global module -- this is the test that would
        fail if it did.
        """
        import random

        random.seed(12345)
        engine_a = OpenDSSEngine(dss_file_path)
        engine_a.reset()
        voltages_a = self._bus_voltages(engine_a)

        random.seed(12345)
        engine_b = OpenDSSEngine(dss_file_path)
        engine_b.reset()
        voltages_b = self._bus_voltages(engine_b)

        assert voltages_a.keys() == voltages_b.keys()
        max_abs_diff = max(
            abs(voltages_a[bus_id] - voltages_b[bus_id]) for bus_id in voltages_a
        )
        assert max_abs_diff > 0, (
            "two unseeded engines produced identical voltages after the "
            "global random module was seeded identically before each "
            "construction -- engine appears to be coupled to global random "
            "state rather than using a private random.Random instance"
        )

    def test_seeded_engine_unaffected_by_global_random_perturbation(
        self, dss_file_path
    ):
        """A seeded engine's output must not change when the global
        `random` module's state is perturbed between construction and
        reset -- confirms the private RNG is truly isolated, not merely
        coincidentally matching in the no-perturbation case above.
        """
        import random

        engine_a = OpenDSSEngine(dss_file_path, fuzz_seed=99)
        engine_a.reset()
        voltages_a = self._bus_voltages(engine_a)

        engine_b = OpenDSSEngine(dss_file_path, fuzz_seed=99)
        # Perturb the global RNG between construction and reset.
        random.seed(0)
        for _ in range(1000):
            random.random()
        engine_b.reset()
        voltages_b = self._bus_voltages(engine_b)

        assert voltages_a.keys() == voltages_b.keys()
        for bus_id in voltages_a:
            assert voltages_a[bus_id] == voltages_b[bus_id], (
                f"bus {bus_id}: perturbing the global random module between "
                "construction and reset() changed a seeded engine's output "
                "-- engine is coupled to global random state"
            )


class TestPinnedHour:
    """`pinned_hour` makes the no-anchor fallback hour an explicit,
    reproducible parameter instead of an RNG draw from `_pinned_hour`'s
    range of [6, 18] (see `TestFuzzSeedDeterminism`'s docstring for the full
    RNG-stream contract).

    IMPORTANT: same process-global-circuit caveat as `TestFuzzSeedDeterminism`
    applies here -- each engine's state is read immediately after its own
    reset(), before the next engine is constructed.
    """

    @staticmethod
    def _bus_voltages(engine: OpenDSSEngine) -> dict:
        """Map bus_id -> voltage_pu for every bus in the engine's state."""
        state = engine.get_current_state()
        return {bus_id: bus.voltage_pu for bus_id, bus in state.buses.items()}

    def test_explicit_pinned_hour_is_honoured(self, dss_file_path):
        """An explicit `pinned_hour` must be used as `_pinned_hour` after
        reset(), not overridden by the RNG draw.
        """
        engine = OpenDSSEngine(dss_file_path, pinned_hour=13)
        engine.reset()
        assert engine._pinned_hour == 13

    def test_out_of_range_pinned_hour_raises(self, dss_file_path):
        """pinned_hour outside [0, 23] must raise ValueError at construction,
        before any circuit work happens.
        """
        with pytest.raises(ValueError, match="pinned_hour"):
            OpenDSSEngine(dss_file_path, pinned_hour=24)
        with pytest.raises(ValueError, match="pinned_hour"):
            OpenDSSEngine(dss_file_path, pinned_hour=-1)

    def test_same_fuzz_seed_fuzzes_identically_regardless_of_pinned_hour(
        self, dss_file_path
    ):
        """Load-bearing property: two engines sharing `fuzz_seed` must reach
        the same fuzz draw whether or not `pinned_hour` is supplied, because
        reset() must consume the hour-draw from the RNG stream unconditionally
        and only discard it when `pinned_hour` overrides the result. If the
        draw were skipped when `pinned_hour` is set, the RNG stream would
        shift and this test would fail.

        Compares loads directly (not bus voltages) since voltages also
        depend on which hour each engine solved at -- pinned_hour=13 and
        pinned_hour=None (which draws from [6, 18]) would differ in PV
        output even with identical fuzz, confounding a voltage comparison.
        Load kW is fuzzed independently of hour, so it isolates the RNG
        stream-position property under test.
        """
        engine_a = OpenDSSEngine(dss_file_path, fuzz_seed=5, pinned_hour=13)
        engine_a.reset()
        topology_a = engine_a.get_topology()
        loads_a = {load_id: info.p_mw for load_id, info in topology_a.loads.items()}

        engine_b = OpenDSSEngine(dss_file_path, fuzz_seed=5)
        engine_b.reset()
        topology_b = engine_b.get_topology()
        loads_b = {load_id: info.p_mw for load_id, info in topology_b.loads.items()}

        assert loads_a.keys() == loads_b.keys()
        for load_id in loads_a:
            assert loads_a[load_id] == loads_b[load_id], (
                f"load {load_id}: {loads_a[load_id]} != {loads_b[load_id]} "
                "-- same fuzz_seed with/without pinned_hour must fuzz loads "
                "identically (RNG stream position must not shift)"
            )


class TestSetGeneratorSetpointReactivePowerUnsupported:
    """OpenDSSEngine.set_generator_setpoint only supports active power.
    PandaPowerEngine honours q_mvar; OpenDSSEngine must reject it loudly
    rather than silently dropping it.
    """

    def test_q_mvar_zero_raises_not_implemented(self, opendss_engine):
        """0.0 is the trap case: `if q_mvar:` is falsy for 0.0, so an
        implementation checking truthiness rather than `is not None` would
        silently accept it instead of raising. Must raise even for 0.0.
        """
        topology = opendss_engine.get_topology()
        gen_id = next(iter(topology.generators.keys()))

        with pytest.raises(NotImplementedError):
            opendss_engine.set_generator_setpoint(gen_id, q_mvar=0.0)

    def test_q_mvar_nonzero_raises_not_implemented(self, opendss_engine):
        """Non-trap-case confirmation: an ordinary non-zero q_mvar must also
        raise.
        """
        topology = opendss_engine.get_topology()
        gen_id = next(iter(topology.generators.keys()))

        with pytest.raises(NotImplementedError):
            opendss_engine.set_generator_setpoint(gen_id, q_mvar=0.5)

    def test_not_implemented_message_names_q_mvar_and_pandapower(self, opendss_engine):
        """The message exists so a caller porting code between engines
        knows OpenDSSEngine and PandaPowerEngine differ here. Assert on
        substrings, not the whole message, since exact wording is not part
        of the contract.
        """
        topology = opendss_engine.get_topology()
        gen_id = next(iter(topology.generators.keys()))

        with pytest.raises(NotImplementedError) as exc_info:
            opendss_engine.set_generator_setpoint(gen_id, q_mvar=0.5)

        message = str(exc_info.value)
        assert "q_mvar" in message
        assert "PandaPowerEngine" in message

    def test_p_mw_alone_succeeds_and_is_observable(self, opendss_engine):
        """Setting only p_mw (q_mvar left as default None) must succeed and
        be observable via run_simulation + get_current_state -- the one
        documented capability of this method.
        """
        topology = opendss_engine.get_topology()
        gen_id = next(iter(topology.generators.keys()))

        opendss_engine.set_generator_setpoint(gen_id, p_mw=0.05)
        opendss_engine.run_simulation()
        state = opendss_engine.get_current_state()

        assert gen_id in state.generators
        assert state.generators[gen_id].p_mw is not None

    def test_unknown_generator_id_raises(self, opendss_engine):
        """Unknown generator_id must raise, even when q_mvar is also
        invalid (non-None). Precedence between the KeyError (unknown id)
        and NotImplementedError (unsupported q_mvar) checks is unspecified
        by the contract -- this test accepts either, rather than guessing
        which check runs first.
        """
        from src.schemas.common import GeneratorID

        with pytest.raises((KeyError, NotImplementedError)):
            opendss_engine.set_generator_setpoint(GeneratorID(99999), q_mvar=0.5)

    def test_unknown_generator_id_alone_raises_key_error(self, opendss_engine):
        """With q_mvar left at its valid default (None), an unknown
        generator_id in isolation must raise KeyError specifically -- this
        isolates the id-validity check from the q_mvar-rejection check
        exercised (with unspecified precedence) above.
        """
        from src.schemas.common import GeneratorID

        with pytest.raises(KeyError):
            opendss_engine.set_generator_setpoint(GeneratorID(99999), p_mw=0.05)


class TestIDMapping:
    """Test ID mapping functionality."""

    def test_bus_name_to_id_mapping(self, opendss_engine):
        """Test bus name to ID mapping."""
        assert len(opendss_engine._bus_name_to_id) > 0
        assert len(opendss_engine._bus_id_to_name) > 0
        assert len(opendss_engine._bus_name_to_id) == len(
            opendss_engine._bus_id_to_name
        )

    def test_line_name_to_id_mapping(self, opendss_engine):
        """Test line name to ID mapping."""
        assert len(opendss_engine._line_name_to_id) > 0
        assert len(opendss_engine._line_id_to_name) > 0
        assert len(opendss_engine._line_name_to_id) == len(
            opendss_engine._line_id_to_name
        )

    def test_load_name_to_id_mapping(self, opendss_engine):
        """Test load name to ID mapping."""
        assert len(opendss_engine._load_name_to_id) > 0
        assert len(opendss_engine._load_id_to_name) > 0
        assert len(opendss_engine._load_name_to_id) == len(
            opendss_engine._load_id_to_name
        )

    def test_generator_name_to_id_mapping(self, opendss_engine):
        """Test generator name to ID mapping."""
        # IEEE37Bus_PV should have PV systems
        assert len(opendss_engine._pv_name_to_id) > 0
        assert len(opendss_engine._pv_id_to_name) > 0
        assert len(opendss_engine._pv_name_to_id) == len(opendss_engine._pv_id_to_name)
