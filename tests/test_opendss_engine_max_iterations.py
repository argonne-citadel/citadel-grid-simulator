"""Tests for wiring PowerFlowConfig.max_iterations into the OpenDSS solution.

Pins the defect found while investigating a 24-hour convergence sweep for
docs/decisions/opendss_diurnal_hour_from_sim_clock.md (parent repo):
pandapower_engine.py passes `config.max_iterations` to `pp.runpp()`, but
opendss_engine.py never applied the same field to `dss.Solution` — OpenDSS
silently ran on its own internal default (MaxIterations=15) regardless of
what the caller configured. IEEE8500 at hour=17 was the one (circuit, hour)
pair out of a 24x2 sweep that needed more than 15 Newton iterations to
converge; the config field existed and was validated by pydantic, but never
reached the solver, which is the config-delivery defect class this repo
repeats most often.

Measurement (see run_simulation()'s comment for the exact numbers): at
IEEE8500 hour=17, `Solution.MostIterationsDone()` hit exactly 15 (the
unwired default) while `Solution.ControlIterations()` reached only 2 of
its own default cap of 10 — MaxIterations is the cap that bound, not
MaxControlIterations, so only MaxIterations is wired to config.
"""

from datetime import datetime, timezone
from pathlib import Path

import opendssdirect as dss
import pytest

from src.engines import opendss_engine as opendss_engine_module
from src.engines.opendss_engine import OpenDSSEngine
from src.schemas.results import PowerFlowConfig
from src.sim_time import ClockAnchor

TEST_DSS_FILE = Path(__file__).parent.parent / "examples" / "IEEE37Bus_PV.dss"
IEEE8500_DSS_FILE = (
    Path(__file__).parent.parent / "examples" / "Run_IEEE8500_ZTCARD.dss"
)
# The wrapper script above is committed and always exists; it `Redirect`s
# into ieee8500/Master.dss, which is the PowerGym data actually fetched at
# Docker build time and absent on a bare checkout. Check for that instead.
IEEE8500_FEEDER_DATA = (
    Path(__file__).parent.parent / "examples" / "ieee8500" / "Master.dss"
)


@pytest.fixture
def dss_file_path() -> str:
    if not TEST_DSS_FILE.exists():
        pytest.skip(f"Test DSS file not found: {TEST_DSS_FILE}")
    return str(TEST_DSS_FILE)


def _frozen_anchor(sim_dt: datetime) -> ClockAnchor:
    """An anchor whose sim_rate is negligible, so compute_sim_now(anchor) is
    sim_dt regardless of how much real wall-clock time elapses while the
    test runs — the same technique test_opendss_engine_clock.py uses.
    """
    return ClockAnchor(
        sim_epoch=sim_dt, wall_start=datetime.now(timezone.utc), sim_rate=1e-9
    )


class _MaxIterationsRecordingSolution:
    """Wraps the real `dss.Solution` object, recording every explicit
    `MaxIterations(value)` setter call while forwarding everything else
    (including the getter and `Solve()`) to the real object.

    Same wrapper-proxy technique as test_opendss_engine_clock.py's
    `_HourRecordingSolution`: OpenDSSDirect.py's API objects override
    `__setattr__` and reject direct patching, so this sits between the
    `opendss_engine` module's `dss` name binding and the real opendssdirect
    object instead of touching the real object's attributes.
    """

    def __init__(self, real_solution: object) -> None:
        self._real_solution = real_solution
        self.max_iterations_setter_calls: list[int] = []

    def MaxIterations(self, *args: int) -> object:
        if args:
            self.max_iterations_setter_calls.append(args[0])
        return self._real_solution.MaxIterations(*args)  # type: ignore[attr-defined]

    def __getattr__(self, name: str) -> object:
        return getattr(self._real_solution, name)


class _RecordingDSS:
    """Forwards every attribute except `Solution` to the real `dss` module."""

    def __init__(
        self, real_dss: object, solution: _MaxIterationsRecordingSolution
    ) -> None:
        self._real_dss = real_dss
        self.Solution = solution

    def __getattr__(self, name: str) -> object:
        return getattr(self._real_dss, name)


class TestMaxIterationsWiring:
    """The configured max_iterations must actually reach dss.Solution —
    able to fail if someone removes the wiring, in the style of the
    anti-auto-advance test in test_opendss_engine_clock.py."""

    def test_configured_max_iterations_applied_before_solve(
        self, dss_file_path, monkeypatch
    ):
        recording_solution = _MaxIterationsRecordingSolution(dss.Solution)
        monkeypatch.setattr(
            opendss_engine_module, "dss", _RecordingDSS(dss, recording_solution)
        )

        engine = OpenDSSEngine(dss_file_path)
        recording_solution.max_iterations_setter_calls.clear()

        distinctive_max_iterations = 33
        config = PowerFlowConfig(max_iterations=distinctive_max_iterations)
        result = engine.run_simulation(config)

        assert result.converged
        assert recording_solution.max_iterations_setter_calls == [
            distinctive_max_iterations
        ], (
            "dss.Solution.MaxIterations(...) must be set from "
            "config.max_iterations before every Solve() — if it is not, "
            "OpenDSS silently falls back to its own internal default (15) "
            "regardless of what the caller configured."
        )

    def test_default_config_max_iterations_applied(self, dss_file_path, monkeypatch):
        recording_solution = _MaxIterationsRecordingSolution(dss.Solution)
        monkeypatch.setattr(
            opendss_engine_module, "dss", _RecordingDSS(dss, recording_solution)
        )

        engine = OpenDSSEngine(dss_file_path)
        recording_solution.max_iterations_setter_calls.clear()

        result = engine.run_simulation()

        assert result.converged
        assert recording_solution.max_iterations_setter_calls == [
            PowerFlowConfig().max_iterations
        ]


class TestIEEE8500Hour17Convergence:
    """Regression test for the one non-convergent (circuit, hour) pair found
    by the 24-hour sweep across IEEE123 and IEEE8500: IEEE8500 at hour=17
    did not converge under OpenDSS's unwired default MaxIterations=15.

    Requires the Siemens PowerGym IEEE 8500-Node feeder data, which is
    fetched into examples/ieee8500/ at Docker build time (Dockerfile) and is
    not committed to this repository — skipped on a bare checkout.
    """

    def test_ieee8500_solves_at_hour_17_with_configured_max_iterations(
        self, monkeypatch
    ):
        if not IEEE8500_FEEDER_DATA.exists():
            pytest.skip(
                f"IEEE8500 feeder data not found: {IEEE8500_FEEDER_DATA} — "
                "fetched from Siemens PowerGym at Docker build time, not "
                "committed to this repo."
            )

        anchor = _frozen_anchor(datetime(2024, 6, 15, 17, 0, 0, tzinfo=timezone.utc))
        monkeypatch.setattr(opendss_engine_module, "get_anchor", lambda: anchor)

        engine = OpenDSSEngine(str(IEEE8500_DSS_FILE))
        assert engine.solution_hour() == 17

        # max_iterations=20 matches simulator.py's production PowerFlowConfig.
        config = PowerFlowConfig(max_iterations=20)
        result = engine.run_simulation(config)

        assert result.converged, (
            f"IEEE8500 must converge at hour=17 with MaxIterations wired to "
            f"config.max_iterations=20 — got converged=False, "
            f"iterations={result.iterations}, error={result.error_message!r}"
        )
