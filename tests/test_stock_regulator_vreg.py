"""/topology publishes each regulator's stock (DSS-compiled) setpoint.

behavior-sim's VVO persona anchors its benign nudges on this value. Before it
did, the persona random-walked every regulator across 114-126 V from a 120 V
midpoint, so its first write moved the stock IEEE-8500 substation regulators
from 126.5 V to ~120 V and the feeder sagged to 0.854 pu (smoke job 22985).

The property: the value is the compiled one and stays so after a live
setpoint write, and it reaches the HTTP response under the regulator's own id
-- the id the Modbus write band (HR 21000 + id) addresses.
"""

from pathlib import Path

from src.engines.opendss_engine import OpenDSSEngine
from src.schemas.common import TransformerID
from src.simulator import GridSimulator
from src.web_api import GridVisualizationAPI

TEST_DSS_FILE = Path(__file__).parent.parent / "examples" / "IEEE37Bus_PV.dss"


def _simulator() -> GridSimulator:
    simulator = GridSimulator(OpenDSSEngine(str(TEST_DSS_FILE)), timestep_seconds=1.0)
    simulator.cache_topology()
    return simulator


def test_stock_setpoint_is_the_compiled_vreg() -> None:
    """IEEE37Bus_PV.dss compiles both regulators at vreg=122."""
    stock = _simulator().get_stock_regulator_vreg()
    assert stock
    assert set(stock.values()) == {122.0}


def test_a_live_write_does_not_move_the_stock_value() -> None:
    simulator = _simulator()
    reg_id = next(iter(simulator.get_stock_regulator_vreg()))
    simulator.engine.set_regulator_voltage_setpoint(TransformerID(reg_id), 117.0)
    assert simulator.engine.get_regulator_voltage_setpoint(TransformerID(reg_id)) == 117.0
    assert simulator.get_stock_regulator_vreg()[reg_id] == 122.0


def test_topology_endpoint_carries_it_per_regulator_id() -> None:
    simulator = _simulator()
    client = GridVisualizationAPI(simulator).app.test_client()
    regulators = client.get("/topology").get_json()["regulators"]
    stock = simulator.get_stock_regulator_vreg()
    assert {int(k): v["stock_vreg_volts"] for k, v in regulators.items()} == stock
