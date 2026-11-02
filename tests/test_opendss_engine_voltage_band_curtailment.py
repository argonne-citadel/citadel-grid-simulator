"""Tests for the post-reset voltage sanity band and %Pmpp curtailment.

IEEE-37 only: the 123/8500 feeders are not tracked (Docker-provisioned at
build time), so these tests must not depend on them.

Covers two defects fixed together on this feeder:

1. Missing or misassigned voltage bases silently produced nonsense per-unit
   readings (0.853-0.866 pu on IEEE-123, ~280 pu on IEEE-37) that a
   converged power flow does not catch on its own -- OpenDSS solves happily
   against a wrong kVBase. `_assert_post_reset_voltage_band()` is the
   write-boundary guard against that defect class; see
   `_RESET_VOLTAGE_BAND_PU`'s derivation comment in opendss_engine.py.

2. `set_generator_setpoint` used to write `dss.PVsystems.Pmpp()` directly,
   resizing the array's nameplate on every dispatch instead of curtailing
   its output. It now writes `%Pmpp` (OpenDSS's upper limit on output as a
   percentage of nameplate), leaving Pmpp untouched.

Known gap (see docs/research-log/2026.md): IEEE-37's PV secondaries do not
all land within the tighter 0.95-1.05 pu band at every hour. The 3-phase
`trafo_pv` bus is wye-wye coupled straight through to primary bus 733,
whose phase B reads ~0.91-0.93 pu at every hour 0-23 -- the same pre-existing
3-wire-delta node-to-ground asymmetry the fixture's own header warns about,
not a voltage-base defect, and not something a VoltageBases change can
correct without altering the transformer's connection topology (out of
scope here). These tests assert the achievable property instead: every node
is within `_RESET_VOLTAGE_BAND_PU` (0.9-1.1), which is what
`_assert_post_reset_voltage_band()` actually enforces.
"""

import logging
from pathlib import Path

import opendssdirect as dss
import pytest

from src.engines.opendss_engine import (
    _RESET_VOLTAGE_BAND_PU,
    _RESET_VOLTAGE_FLOOR_IGNORE_PU,
    OpenDSSEngine,
)

TEST_DSS_FILE = Path(__file__).parent.parent / "examples" / "IEEE37Bus_PV.dss"

# Hour 12 is full daylight on IEEE37Bus_PV.dss's MyIrrad loadshape
# (mult=1.0) -- the only kind of hour where a PV system has non-zero
# headroom to curtail.
DAYLIGHT_HOUR = 12


@pytest.fixture
def dss_file_path() -> str:
    return str(TEST_DSS_FILE)


@pytest.fixture
def opendss_engine(dss_file_path: str) -> OpenDSSEngine:
    # The tracked feeder is required: a missing input or broken compile must
    # fail this regression gate, never turn it into a passing skipped suite.
    return OpenDSSEngine(dss_file_path, fuzz_seed=0, pinned_hour=DAYLIGHT_HOUR)


class TestPostResetVoltageBand:
    """reset() must leave every energized node within the sanity band."""

    @pytest.mark.parametrize("bad_base_kv", [2.4, 0.00024])
    def test_mislabelled_nonzero_base_fails_guard(
        self, opendss_engine: OpenDSSEngine, bad_base_kv: float
    ) -> None:
        """Catch both a low-pu reading and a high-pu reading without rescue.

        The 10x base gives ~0.095 pu, below the former 0.1 ignore floor;
        the 0.001x base gives ~950 pu, tempting a second volts conversion.
        Both describe an energized bus with a wrong, nonzero kVBase.
        """
        opendss_engine.reset()
        dss.Text.Command(f"SetKVBase bus=trafo_pv1 kvln={bad_base_kv}")
        dss.Circuit.SetActiveBus("trafo_pv1")
        assert dss.Bus.kVBase() == pytest.approx(bad_base_kv)
        reading = dss.Bus.puVmagAngle()[0]
        assert reading > _RESET_VOLTAGE_FLOOR_IGNORE_PU
        if bad_base_kv > 1.0:
            assert reading < 0.1
        else:
            assert reading > 2
        with pytest.raises(RuntimeError, match="Offenders.*trafo_pv1"):
            opendss_engine._assert_post_reset_voltage_band()

    def test_every_node_within_reset_voltage_band(
        self, opendss_engine: OpenDSSEngine
    ) -> None:
        opendss_engine.reset()

        offenders = []
        for bus_name in dss.Circuit.AllBusNames():
            dss.Circuit.SetActiveBus(bus_name)
            voltages_angles = dss.Bus.puVmagAngle()
            voltages_pu = [
                voltages_angles[i] for i in range(0, len(voltages_angles), 2)
            ]
            for v in voltages_pu:
                if v < _RESET_VOLTAGE_FLOOR_IGNORE_PU:
                    continue
                if not (_RESET_VOLTAGE_BAND_PU[0] <= v <= _RESET_VOLTAGE_BAND_PU[1]):
                    offenders.append((bus_name, v))

        assert not offenders, f"Nodes outside {_RESET_VOLTAGE_BAND_PU} pu: {offenders}"

    def test_pv_secondary_buses_report_plausible_voltage(
        self, opendss_engine: OpenDSSEngine
    ) -> None:
        """PV secondaries must at least be real per-unit readings, not the
        ~280 pu symptom of a missing kVBase. Does not assert the tighter
        0.95-1.05 band -- see module docstring for why that band is not
        achievable on every trafo_pv* node on this fixture.
        """
        opendss_engine.reset()

        for bus_name in (
            "trafo_pv",
            "trafo_pv1",
            "trafo_pv2",
            "trafo_pv3",
            "trafo_pv4",
            "trafo_pv5",
        ):
            dss.Circuit.SetActiveBus(bus_name)
            voltages_angles = dss.Bus.puVmagAngle()
            voltages_pu = [
                voltages_angles[i] for i in range(0, len(voltages_angles), 2)
            ]
            for v in voltages_pu:
                assert _RESET_VOLTAGE_BAND_PU[0] <= v <= _RESET_VOLTAGE_BAND_PU[1], (
                    f"{bus_name} reads {v} pu, outside {_RESET_VOLTAGE_BAND_PU}"
                )


class TestPVCurtailmentSetpoint:
    """set_generator_setpoint curtails output via %Pmpp, never resizes Pmpp."""

    def _solved_output_kw(self, pv_name: str) -> float:
        dss.Circuit.SetActiveElement(f"PVSystem.{pv_name}")
        num_phases = dss.CktElement.NumPhases()
        powers = dss.CktElement.Powers()
        return -sum(powers[i] for i in range(0, num_phases * 2, 2))

    @pytest.mark.parametrize("pv_name", ["pv", "pv1", "pv2", "pv3", "pv4", "pv5"])
    def test_zero_half_full_and_above_nameplate_restore(
        self,
        opendss_engine: OpenDSSEngine,
        pv_name: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Every PV must obey curtailment, including the formerly overvolted ones."""
        caplog.set_level(logging.WARNING)
        opendss_engine.reset()
        gen_id = next(
            gen_id
            for gen_id, name in opendss_engine._pv_id_to_name.items()
            if name == pv_name
        )

        dss.PVsystems.Name(pv_name)
        nameplate_kw = dss.PVsystems.Pmpp()

        opendss_engine.run_simulation()
        output_100_kw = self._solved_output_kw(pv_name)
        assert output_100_kw > 0, "Daylight hour must produce non-zero PV output"

        # Half nameplate is below irradiance/kVA limits for all six units at
        # hour 12. The tolerance matches the solver's 1e-4 power convergence
        # tolerance with a 1 W absolute floor; the former 2% hid model errors.
        for fraction, expected_kw in (
            (0.0, 0.0),
            (0.5, 0.5 * nameplate_kw),
            (1.0, output_100_kw),
            (2.0, output_100_kw),
        ):
            opendss_engine.set_generator_setpoint(
                gen_id, p_mw=fraction * nameplate_kw / 1000.0
            )
            opendss_engine.run_simulation()
            assert self._solved_output_kw(pv_name) == pytest.approx(
                expected_kw, rel=1e-4, abs=1e-3
            ), (pv_name, fraction)
            dss.PVsystems.Name(pv_name)
            assert dss.PVsystems.Pmpp() == pytest.approx(nameplate_kw, abs=1e-9)
            assert float(dss.Properties.Value("%Pmpp")) == pytest.approx(
                min(100.0, fraction * 100.0), abs=1e-9
            )
        assert any("exceeds nameplate" in record.message for record in caplog.records)

    def test_negative_setpoint_raises_value_error(
        self, opendss_engine: OpenDSSEngine
    ) -> None:
        opendss_engine.reset()
        gen_id = next(iter(opendss_engine._pv_id_to_name))

        with pytest.raises(ValueError, match="must be >= 0"):
            opendss_engine.set_generator_setpoint(gen_id, p_mw=-0.01)

    @pytest.mark.parametrize("nameplate_kw", [0.0, -1.0])
    def test_nonpositive_nameplate_fails_loudly(
        self, opendss_engine: OpenDSSEngine, nameplate_kw: float
    ) -> None:
        opendss_engine.reset()
        gen_id = next(iter(opendss_engine._pv_id_to_name))
        dss.PVsystems.Name(opendss_engine._pv_id_to_name[gen_id])
        dss.PVsystems.Pmpp(nameplate_kw)
        with pytest.raises(RuntimeError, match="non-positive nameplate"):
            opendss_engine.set_generator_setpoint(gen_id, p_mw=0.0)
