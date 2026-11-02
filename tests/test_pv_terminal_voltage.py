"""Exercise phase-specific inverter feedback through the real /state response.

A small unbalanced feeder puts a PV on phase B or C while the host-bus scalar
still reads phase A. This reproduces the IEEE8500 resiting defect without
depending on untracked PowerGym files or a running network service.
"""

import threading
from pathlib import Path
from unittest.mock import MagicMock

import opendssdirect as dss
import pytest

from src.engines.opendss_engine import OpenDSSEngine
from src.web_api import GridVisualizationAPI


@pytest.mark.parametrize("phase", [2, 3])
def test_state_exposes_actual_pv_phase_voltage_and_live_reset_nameplate(
    tmp_path: Path, phase: int
) -> None:
    model = tmp_path / "phase.dss"
    model.write_text(
        f"""Clear
New Circuit.phase basekv=4.16 pu=1 phases=3
New Transformer.reg phases=3 windings=2 buses=[sourcebus regulated] conns=[wye wye] kvs=[4.16 4.16] kvas=[5000 5000] xhl=0.1
New RegControl.control transformer=reg winding=2 vreg=120 ptratio=20 band=100
New Line.feeder bus1=regulated.1.2.3 bus2=primary.1.2.3 phases=3 r1=0.5 x1=0.1 r0=0.5 x0=0.1 c1=0 c0=0 length=1 units=km
New Load.unbalanced bus1=primary.{phase} phases=1 conn=wye kv=2.40177712 kw=300 kvar=0
New Transformer.pv_up phases=1 windings=2 xhl=2
~ wdg=1 bus=primary.{phase} conn=wye kv=2.40177712 kva=100
~ wdg=2 bus=secondary.1 conn=wye kv=0.24 kva=100
New PVSystem.pv_target phases=1 bus1=secondary.1 conn=wye kv=0.24 kva=12 Pmpp=10 irradiance=1
Set VoltageBases=[4.16, 0.415692]
CalcVoltageBases
Solve
""",
        encoding="utf-8",
    )
    engine = OpenDSSEngine(str(model), pinned_hour=12, fuzz_seed=0)
    engine.reset()
    simulator = MagicMock()
    simulator.get_cached_topology.return_value = engine.get_topology()
    simulator.get_current_state.side_effect = engine.get_current_state
    simulator.engine = engine
    simulator.current_time = 0
    simulator.last_step_converged = True
    # Exercise response assembly directly; no socket/server is involved.
    api = GridVisualizationAPI.__new__(GridVisualizationAPI)
    api.simulator = simulator
    api._lock = threading.Lock()
    api._topology_version = 0
    api._topology_cache = {
        "generators": [{"id": 0, "name": "pv_target", "max_p_kw": 10.0}]
    }
    response = api._get_state_data()
    measurement = response["topology"]["generators"][0]
    dss.Circuit.SetActiveElement("PVSystem.pv_target")
    expected_voltage = dss.CktElement.VoltagesMagAng()[0] / 240.0
    dss.Circuit.SetActiveBus("primary")
    wrong_phase_voltage = dss.Bus.puVmagAngle()[0]
    assert abs(expected_voltage - wrong_phase_voltage) > 0.005
    assert measurement["voltage_pu"] == pytest.approx(expected_voltage, abs=1e-9)
    dss.PVsystems.Name("pv_target")
    first_nameplate = measurement["max_p_kw"]
    assert first_nameplate == pytest.approx(dss.PVsystems.Pmpp())

    # Cached topology intentionally survives reset; the response must carry
    # the new state's voltage and newly fuzzed Pmpp rather than the old cache.
    engine.reset()
    response = api._get_state_data()
    measurement = response["topology"]["generators"][0]
    dss.PVsystems.Name("pv_target")
    assert measurement["max_p_kw"] == pytest.approx(dss.PVsystems.Pmpp())
    assert abs(measurement["max_p_kw"] - first_nameplate) > 0.001
    assert measurement["voltage_pu"] == pytest.approx(
        dss.CktElement.VoltagesMagAng()[0] / 240.0, abs=1e-9
    )
