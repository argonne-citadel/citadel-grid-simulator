"""Tests for voltage-regulator control over the Modbus interface.

Covers the OpenDSS RegControl / Modbus integration described in the
regulator-control contract:

* `REGISTER_BANDS` allocates non-overlapping address ranges, with regulator
  tap readback in the HR 6000s, voltage-setpoint readback in the HR 7000s,
  and voltage-setpoint write in the HR 21000s.
* The write band commands a regulator's **voltage setpoint**
  (`RegControl.ForwardVreg`), not its tap position. Direct tap forcing was
  tried first and abandoned: OpenDSS's RegControl re-solves its own tap to
  hit its target voltage on every `Solve()`, so an externally-forced tap
  is silently overwritten on the very next solve — that is correct
  closed-loop regulator behaviour, not a bug, but it means a tap write can
  never persist. Commanding the voltage target instead lets the
  regulator's own control do the moving, and that target *does* persist.
  `set_transformer_tap()` / `get_regulator_tap_position()` are retained as
  a lower-level primitive (e.g. for a future inhibit-then-force scenario)
  but are not what this Modbus band uses, and a value set through them
  still does not survive a solve.
* Setpoints are unsigned volts-on-the-PT-secondary-base times ten
  (`ForwardVreg` is a magnitude and is never negative, unlike a tap
  position). A master-originated write reaches `SimDevice.action`, and
  from there the live RegControl — it must not merely land in a register
  bank that nothing reads back out to the physical model (the pymodbus
  3.14 "inert data block" failure mode `modbus_server.py` itself
  documents).

The decisive property is that a setpoint write changes BOTH the engine's
reported setpoint immediately, AND the physical voltage at the regulated
bus after the next solve — and that the tap/voltage effect *holds* through
a second solve with no new command, which is exactly the property
tap-forcing lacked. These are asserted as two separate tests deliberately:
`test_setpoint_write_reaches_engine_before_next_solve` and
`test_setpoint_write_persists_through_next_solve_and_changes_voltage`, so a
partial result (one half true, one false) is visible directly in the
pytest summary rather than folded into a single ambiguous failure.

Trap for the next person touching this file: `GridState.buses[id]
.voltage_pu` always reports phase/node index 0, unconditionally, regardless
of which phase is actually regulated. The fixture's two regulators are
single-phase — `creg1a` on phase 1 (node index 0), `creg1c` on phase 3
(node index 2) — so a physical-effect test that reads bus voltage MUST use
`creg1a`. Picking `creg1c` makes a correctly-working setpoint write look
like it barely moved anything, because the scalar being read is the wrong
phase entirely.
"""

import asyncio
from pathlib import Path

import pytest

from src.engines.opendss_engine import OpenDSSEngine
from src.protocols.modbus_server import GridModbusServer, StrictDeviceSimCore
from src.schemas.common import TransformerID

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


def _regulator_server(engine, port):
    """Build a Modbus server wired to `engine` with a live SimCore context.

    Mirrors the pattern in test_modbus.py::test_value_scaling: the core is
    built directly from `_create_sim_device()` since no socket is bound in
    these tests.
    """
    server = GridModbusServer(engine, host="127.0.0.1", port=port)
    server._build_point_mapping()
    sim_device = server._create_sim_device()
    core = StrictDeviceSimCore(sim_device)
    server.context = core
    return server, core


class TestRegisterBandLayout:
    """Test the static REGISTER_BANDS table itself."""

    def test_register_bands_do_not_overlap(self):
        """No two bands may share an address.

        If they did, a write intended for one point (e.g. a regulator
        setpoint command) would silently alias another point's register
        (e.g. a load setpoint), corrupting whichever one happens to be read
        back. Checked generically over REGISTER_BANDS rather than against
        one hardcoded pair, so a badly-placed new band added later is also
        caught.
        """
        from src.protocols.modbus_server import REGISTER_BANDS

        bands = sorted(REGISTER_BANDS.items(), key=lambda kv: kv[1][0])
        for name, (start, end) in bands:
            assert start < end, f"{name} band ({start}, {end}) is empty or inverted"

        for (name_a, (_, end_a)), (name_b, (start_b, _)) in zip(bands, bands[1:]):
            assert end_a <= start_b, f"{name_a} and {name_b} bands overlap: {bands}"

    def test_regulator_bands_match_the_documented_hr_ranges(self):
        """Pin the three regulator-specific bands the contract names.

        Disjointness alone would not catch bands drifting together to a
        different (still non-overlapping) set of decades; this pins them
        to the documented 6000s / 7000s / 21000s ranges specifically.
        Tap readback (6000s) is unchanged from the old tap-forcing design;
        setpoint readback (7000s) is new; setpoint write reuses the old tap
        write's address range (21000s, moved from 13000s once IEEE-8500 was
        measured writing buses into 13000-16999) but carries volts, not a tap.
        """
        from src.protocols.modbus_server import REGISTER_BANDS

        assert REGISTER_BANDS["regulator_tap_readback"] == (6000, 7000)
        assert REGISTER_BANDS["regulator_setpoint_readback"] == (7000, 8000)
        assert REGISTER_BANDS["regulator_setpoint_write"] == (21000, 22000)


class TestVregBoundsFromPt:
    """The PT-secondary bounds arithmetic, winding type by winding type.

    The regression target is a line-to-line vs line-to-neutral bug: for a
    3-phase WYE winding `dss.Transformers.kV()` reports the line-to-line
    rating, but the PT is referenced line-to-neutral, so the per-phase base
    is kV/√3. The old formula omitted the √3 and computed bounds ~1.73x too
    high for such windings, falsely rejecting in-range setpoints (observed on
    IEEE-123's one 3-phase regulator, creg1a: bounds [187.2, 228.8] that
    excluded its own shipped 120 V setpoint).

    This cannot be caught by a feeder-loading test in CI: the only
    git-tracked feeder is IEEE-37, whose regulators are single-phase. So the
    arithmetic is tested directly with hand-computed values for all three
    winding types that appear across the claim feeders.
    """

    def test_single_phase_delta_winding_ieee37_shape(self):
        """1-phase delta, kV 4.8, PT 40, ±10% tap → [108.0, 132.0].

        IEEE-37's shape. No √3 applies (single-phase); the OLD formula was
        already correct here, so this pins that the fix did NOT regress it.
        """
        from src.engines.opendss_regulators import _vreg_bounds_from_pt

        vmin, vmax = _vreg_bounds_from_pt(
            winding_kv=4.8,
            n_phases=1,
            is_delta=True,
            pt_ratio=40.0,
            min_tap=0.9,
            max_tap=1.1,
        )
        assert vmin == pytest.approx(108.0)  # 4800 * 0.9 / 40
        assert vmax == pytest.approx(132.0)  # 4800 * 1.1 / 40

    def test_single_phase_wye_winding_ieee8500_shape(self):
        """1-phase wye, kV 7.2, PT 60, ±10% tap → [108.0, 132.0].

        IEEE-8500's shape. Single-phase, so no √3; also unaffected by the fix.
        """
        from src.engines.opendss_regulators import _vreg_bounds_from_pt

        vmin, vmax = _vreg_bounds_from_pt(
            winding_kv=7.2,
            n_phases=1,
            is_delta=False,
            pt_ratio=60.0,
            min_tap=0.9,
            max_tap=1.1,
        )
        assert vmin == pytest.approx(108.0)  # 7200 * 0.9 / 60
        assert vmax == pytest.approx(132.0)  # 7200 * 1.1 / 60

    def test_three_phase_wye_winding_ieee123_shape_is_the_regression(self):
        """3-phase wye, kV 4.16, PT 20, ±10% tap → [108.1, 132.1].

        IEEE-123's creg1a — the exact case the bug mis-handled. kV 4.16 is
        line-to-line; the PT base is 4160/√3 = 2402.2 V, so the bound is
        2402.2 * 0.9 / 20 = 108.1, NOT the old 4160 * 0.9 / 20 = 187.2. The
        corrected lower bound admits the regulator's shipped 120 V setpoint;
        the old one wrongly excluded it, which is what made S9 look infeasible
        on IEEE-123.
        """
        from src.engines.opendss_regulators import _vreg_bounds_from_pt

        vmin, vmax = _vreg_bounds_from_pt(
            winding_kv=4.16,
            n_phases=3,
            is_delta=False,
            pt_ratio=20.0,
            min_tap=0.9,
            max_tap=1.1,
        )
        # 4160 / sqrt(3) = 2402.2; * 0.9 / 20 = 108.10, * 1.1 / 20 = 132.12
        assert vmin == pytest.approx(108.10, abs=0.05)
        assert vmax == pytest.approx(132.12, abs=0.05)
        # The load-bearing assertion: 120 V (creg1a's shipped setpoint) and
        # 114 V (the S9 Range-A lower edge) both fall inside the fixed band.
        assert vmin <= 114.0
        assert vmin <= 120.0 <= vmax

    def test_three_phase_delta_winding_takes_no_sqrt3(self):
        """3-phase DELTA, kV 4.16, PT 20 → [187.2, 228.8], no √3.

        A delta winding's rated kV already is the terminal voltage, so the √3
        correction must NOT be applied just because the winding is 3-phase —
        the branch keys on wye specifically. Guards against "fix it for all
        3-phase windings" over-correcting the delta case.
        """
        from src.engines.opendss_regulators import _vreg_bounds_from_pt

        vmin, vmax = _vreg_bounds_from_pt(
            winding_kv=4.16,
            n_phases=3,
            is_delta=True,
            pt_ratio=20.0,
            min_tap=0.9,
            max_tap=1.1,
        )
        assert vmin == pytest.approx(187.2)  # 4160 * 0.9 / 20, no sqrt3
        assert vmax == pytest.approx(228.8)  # 4160 * 1.1 / 20


class TestSetpointVoltsEncoding:
    """Test the unsigned volts-times-ten encoding of the `ForwardVreg`
    voltage setpoint on the Modbus wire.

    Replaces the deleted two's-complement signed-tap-encoding test.
    `ForwardVreg` is a voltage magnitude and OpenDSS's RegControl does not
    support a negative regulation target, so there is no sign to encode and
    the signed decoder the old test exercised is gone as dead code.
    """

    def test_voltage_setpoint_round_trips_through_volts_times_ten_encoding(
        self, opendss_engine
    ):
        """A voltage setpoint written through HR 21000s must read back
        correctly through HR 7000s.

        Hand-computed: 114.0 V on the RegControl PT-secondary base, at the
        documented volts-times-ten wire scaling, is 114.0 * 10 = 1140 — an
        unsigned value comfortably inside a 16-bit register (max 65535).
        A scale-factor bug (e.g. writing raw volts as 114, or centivolts as
        11400) would produce a readback this test catches immediately,
        which is the analogous failure mode to the sign bug the deleted
        test caught for tap encoding.
        """
        server, core = _regulator_server(opendss_engine, port=15901)

        topology = opendss_engine.get_topology()
        regulator_id = next(iter(topology.regulators))
        write_address = 21000 + regulator_id
        readback_address = server.get_point_mapping()["regulator_setpoints"][
            regulator_id
        ]

        target_setpoint_v = 114.0
        expected_encoded = 1140
        assert round(target_setpoint_v * 10) == expected_encoded  # hand-computed

        write_result = asyncio.run(
            core.async_setValues(1, 6, write_address, [expected_encoded])
        )
        assert write_result is None, f"write was rejected: {write_result}"

        server.update_measurements()
        readback = asyncio.run(core.async_getValues(1, 3, readback_address, 1))
        assert readback[0] == expected_encoded, (
            f"readback register reported {readback[0]}, expected "
            f"{expected_encoded} for a {target_setpoint_v} V setpoint write"
        )


class TestRegulatorModbusPhysicalEffect:
    """The decisive tests: a setpoint write must actually move the plant,
    and the move must survive a solve.

    modbus_server.py's own module docstring documents that pymodbus 3.14
    treats ModbusSequentialDataBlock as an inert carrier — a write can land
    in a register bank and never reach anything else, with no exception
    raised anywhere. `SimDevice.action` is the seam that is supposed to
    prevent this by routing writes to the engine directly instead of
    letting them just sit in the block.
    """

    def test_setpoint_write_reaches_engine_before_next_solve(self, opendss_engine):
        """The write must change the engine's reported setpoint
        immediately — before any solve — proving it went through
        `SimDevice.action` to the live RegControl, not into an inert
        register bank nothing reads.

        Deliberately does not check tap or bus voltage here: unlike the
        setpoint itself, those only change on the *next* `Solve()` (the
        RegControl is a closed-loop controller that only acts during a
        solve), so checking them immediately would conflate "the write
        worked but hasn't been acted on yet" with "the write never reached
        anything." That distinction is exactly what
        `test_setpoint_write_persists_through_next_solve_and_changes_voltage`
        below is for.
        """
        server, core = _regulator_server(opendss_engine, port=15902)

        topology = opendss_engine.get_topology()
        regulator_id = next(iter(topology.regulators))
        write_address = 21000 + regulator_id
        readback_address = server.get_point_mapping()["regulator_setpoints"][
            regulator_id
        ]

        target_setpoint_v = 114.0
        target_encoded = 1140  # hand-computed: 114.0 V x 10
        asyncio.run(core.async_setValues(1, 6, write_address, [target_encoded]))

        server.update_measurements()
        readback_immediately_after = asyncio.run(
            core.async_getValues(1, 3, readback_address, 1)
        )[0]
        assert readback_immediately_after == target_encoded, (
            f"setpoint read back as {readback_immediately_after} immediately "
            f"after a Modbus write of {target_encoded} ({target_setpoint_v} V); "
            "the write did not reach the engine (it may have landed inertly "
            "in the register bank)."
        )

    def test_setpoint_write_persists_through_next_solve_and_changes_voltage(
        self, opendss_engine
    ):
        """A voltage-setpoint write must move the regulated bus voltage via
        the regulator's own closed-loop tap control, and that move must
        hold through a *second* solve with no new command.

        Persistence is the entire point of the setpoint-write redesign
        (see module docstring): tap-forcing did not survive a solve
        because RegControl re-targets its own tap on every `Solve()`.
        Commanding the voltage target instead means the regulator's own
        control converges toward it and then holds — a second solve should
        change nothing but floating-point noise.

        Regulator and direction are load-bearing choices, not arbitrary:

        * Uses `creg1a` (transformer `reg1a`, regulator index 0), never
          `creg1c`. See the module docstring's phase-index trap:
          `GridState.buses[id].voltage_pu` always reports phase/node index
          0 regardless of which phase is actually regulated, and `creg1a`
          is the one on phase 1 (node index 0). `creg1c` is on phase 3
          (node index 2); reading its regulated bus through
          `voltage_pu` would make a working setpoint write look broken.
        * Moves the setpoint DOWN (122.0 -> 114.0 V), not up. `creg1a`
          starts at its own tap_max ceiling (asserted below as a
          precondition) — a further upward move would ask the regulator
          for taps it does not have, making "stuck at the ceiling" and
          "the command didn't take" indistinguishable.

        122.0 V is the fixture's own starting setpoint
        (`regcontrol.creg1a ... vreg=122` in IEEE37Bus_PV.dss), so the
        pre-write readback below is a hand-computed value (122.0 * 10 =
        1220), not a value copied from a run of the code. The nominal
        120.0 V PT-secondary base is likewise hand-derived from the same
        file: `transformer.reg1a` winding 2 is 4.8 kV, and
        `regcontrol.creg1a` declares `ptratio=40`, so 4800 V / 40 = 120 V.

        Voltage-shift materiality is asserted with a generous margin
        rather than a pinned float, since an exact power-flow solution is
        brittle across OpenDSS versions: naive expectation is
        |122.0 - 114.0| / 120.0 = 6.67% of nominal, and the threshold below
        is 10% of that naive figure. The unchanged-after-second-solve
        check is tight by contrast, since drift there is exactly the
        regression to non-persistent (tap-forcing-like) behaviour that
        this test exists to catch.
        """
        server, core = _regulator_server(opendss_engine, port=15903)

        topology = opendss_engine.get_topology()
        regulator_id = next(
            rid for rid, info in topology.regulators.items() if info.name == "reg1a"
        )
        regulated_bus_id = topology.regulators[regulator_id].lv_bus
        tap_max = topology.regulators[regulator_id].tap_max
        assert topology.regulators[regulator_id].tap_position == tap_max, (
            "test assumes creg1a starts at its own tap ceiling; if the "
            "fixture's default operating point changes, the down-only "
            "direction chosen here needs re-deriving"
        )

        write_address = 21000 + regulator_id
        setpoint_readback_address = server.get_point_mapping()["regulator_setpoints"][
            regulator_id
        ]

        opendss_engine.run_simulation()
        voltage_before = (
            opendss_engine.get_current_state().buses[regulated_bus_id].voltage_pu
        )

        server.update_measurements()
        setpoint_before = asyncio.run(
            core.async_getValues(1, 3, setpoint_readback_address, 1)
        )[0]
        assert setpoint_before == 1220  # hand-computed: fixture's vreg=122 x 10

        initial_setpoint_v = 122.0
        target_setpoint_v = 114.0
        target_encoded = 1140  # hand-computed: 114.0 V x 10
        nominal_v = 120.0  # hand-derived: 4.8kV winding / ptratio 40 (see docstring)

        write_result = asyncio.run(
            core.async_setValues(1, 6, write_address, [target_encoded])
        )
        assert write_result is None, f"write was rejected: {write_result}"

        opendss_engine.run_simulation()

        server.update_measurements()
        setpoint_after = asyncio.run(
            core.async_getValues(1, 3, setpoint_readback_address, 1)
        )[0]
        tap_after_first_solve = opendss_engine.get_regulator_tap_position(
            TransformerID(regulator_id)
        )
        voltage_after_first_solve = (
            opendss_engine.get_current_state().buses[regulated_bus_id].voltage_pu
        )

        # 1. The engine's reported setpoint changed.
        assert setpoint_after == target_encoded, (
            f"setpoint readback is {setpoint_after}, expected {target_encoded} "
            f"({target_setpoint_v} V) after the Modbus write of a value that "
            f"was previously {setpoint_before} ({initial_setpoint_v} V)"
        )

        # 2. The tap moved in response (the regulator acting on the new
        # target, not just the setpoint register changing in isolation).
        assert tap_after_first_solve != tap_max, (
            f"tap stayed at its ceiling ({tap_max}) after a downward "
            "setpoint move; the regulator did not act on the new target"
        )

        # 3. The regulated bus voltage moved materially...
        naive_expected_shift_pu = (
            abs(initial_setpoint_v - target_setpoint_v) / nominal_v
        )
        expected_min_shift_pu = naive_expected_shift_pu * 0.1
        voltage_drop = voltage_before - voltage_after_first_solve
        assert voltage_drop >= expected_min_shift_pu, (
            f"regulated bus voltage barely moved ({voltage_before} -> "
            f"{voltage_after_first_solve}, drop {voltage_drop}) despite an "
            f"{abs(initial_setpoint_v - target_setpoint_v)} V downward "
            f"setpoint command; expected at least {expected_min_shift_pu} pu. "
            "The write may be disconnected from the physical model entirely."
        )

        opendss_engine.run_simulation()
        tap_after_second_solve = opendss_engine.get_regulator_tap_position(
            TransformerID(regulator_id)
        )
        voltage_after_second_solve = (
            opendss_engine.get_current_state().buses[regulated_bus_id].voltage_pu
        )

        # ...and holds after a second solve with no new command: float
        # noise only, not another RegControl re-optimization.
        assert tap_after_second_solve == tap_after_first_solve, (
            f"tap drifted from {tap_after_first_solve} to "
            f"{tap_after_second_solve} on a second solve with no new "
            "command; the setpoint did not actually persist."
        )
        assert voltage_after_second_solve == pytest.approx(
            voltage_after_first_solve, abs=1e-4
        ), (
            f"regulated bus voltage drifted from {voltage_after_first_solve} "
            f"to {voltage_after_second_solve} on a second solve with no new "
            "command; the setpoint did not actually persist."
        )


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
