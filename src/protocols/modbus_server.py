"""Modbus TCP server interface for grid simulator.

This module provides a Modbus TCP server that maps grid measurements to Modbus registers
and handles control commands from a Modbus master (SCADA MTU).

Register Mapping — see REGISTER_BANDS below for the canonical band table.
The new regulator bands (6000s/7000s/21000s) are generated from it; the
older bands are still hand-maintained here and must match REGISTER_BANDS
by inspection (a pre-existing gap, not introduced by this addition — see
the comment on REGISTER_BANDS).
- Holding Registers 0-999: Bus voltages (scaled to int, pu * 1000)
- Holding Registers 1000-1999: Line active power flows (scaled to int, MW * 100)
- Holding Registers 2000-2999: Line reactive power flows (scaled to int, MVAR * 100)
- Holding Registers 3000-3999: Line loading (scaled to int, % * 10)
- Holding Registers 4000-4999: DER active power (scaled to int, MW * 100)
- Holding Registers 5000-5999: Load active power (scaled to int, MW * 100)
- Holding Registers 6000-6999: Regulator tap position (readback, signed int16
  two's-complement, ANSI tap number)
- Holding Registers 7000-7999: Regulator voltage setpoint (readback, unsigned int,
  ForwardVreg volts * 10, PT-secondary base)
- Holding Registers 8000-8999: Transformer loading (readback, scaled to int,
  % * 10 — same convention as line_loading). Plain transformers only; a
  transformer that OpenDSS's RegControl has turned into a regulator is
  excluded (see TransformerState's docstring) and reported instead in the
  6000/7000 regulator bands. Further restricted to transformers a real
  utility would actually SCADA-instrument — see the substation/DER-step-up
  disjunction documented above _DER_STEP_UP_TRANSFORMER_NAME_PREFIX; a
  filtered-out transformer gets no register at all, not a zero.
- Coils 0-999: Breaker status (closed=1, open=0)
- Coils 1000-1999: Breaker control (write to open/close)
- Holding Registers 10000-10999: DER setpoints (write, integer kW)
- Holding Registers 11000-11999: Load adjustments (write, integer kW)
- Holding Registers 12000-19999: retired. Formerly bus voltage setpoints
  (write, integer mvar * 10, HR 12000-12999) — removed per
  docs/decisions/bus_voltage_setpoints_on_opendss.md (Redmine #1293): the
  dispatch branch called set_bus_reactive_injection, a PandaPowerEngine-only
  capability neither shipped feeder (both OpenDSS) implements, so no write to
  this band ever had physical effect. A distribution operator commands a
  regulator's ForwardVreg, not a bus voltage directly — see the
  regulator_setpoint_write band below.
- Holding Registers 20000-20999: MTU-authorized safety-class setpoints
  (SAFETY_REGISTER_START/END). No dispatch branch here — see
  GridModbusWriteHandler._handle_register_write's module-level note on the
  safety-band exemption.
- Holding Registers 21000-21999: Regulator voltage setpoint (write, unsigned int,
  ForwardVreg volts * 10, PT-secondary base). NOT a tap command — see
  set_transformer_tap's docstring in opendss_engine.py for why a forced tap
  does not survive a solve under automatic RegControl operation, which is
  why this scenario commands the setpoint instead. Placed at 21000-21999,
  matching MTU's ModbusAddressMap.REGULATOR_SETPOINT_START/END, immediately
  above MTU's safety-register band (20000-20999) rather than inside the
  retired bus-voltage-setpoint band: at the time this was placed, that space
  still carried live IEEE-8500 bus-voltage traffic across its full
  12000-19999 width, so carving a sub-range out of it would have
  reclassified real bus writes as regulator writes. That band is retired
  now, but the regulator band was not moved back into it after the fact.

pymodbus 3.14+ note: master-originated writes are intercepted through
SimDevice.action, not through a data block subclass. 3.14 routes every request
through SimCore/SimRuntime and treats ModbusSequentialDataBlock as an inert
SimData carrier, so an overridden setValues is constructed and never called —
the server would accept breaker and setpoint commands into a register bank and
never apply them to the engine, with nothing failing. Internally computed
measurements take the opposite path: they go straight into the register bank
via simcore_registers so they do NOT look like commands.
"""

import asyncio
import logging
import math
import threading
import time
from typing import Any, Dict, List, Optional, Union

from pymodbus.constants import ExcCodes
from pymodbus.exceptions import NoSuchIdException
from pymodbus.server import ModbusTcpServer
from pymodbus.simulator import DataType, SimData, SimDevice
from pymodbus.simulator.simcore import SimCore

from ..engines.base import PowerSystemEngine
from ..schemas.common import BusID, BusType, GeneratorID, LineID, LoadID, TransformerID
from ..schemas.topology import NetworkTopology, TransformerInfo
from .base import ModbusHandler
from .simcore_registers import write_bits, write_registers

logger = logging.getLogger(__name__)

# Modelled-deployment assumption, not an implementation limit: a real
# distribution utility does not put SCADA telemetry on every transformer it
# owns. Service transformers that step down to customer utilization voltage
# are far too numerous and far too low-value individually to be worth
# polling, and are not in real deployments. Two classes of transformer ARE
# real SCADA points: the substation transformer (the feeder head, SCADA-
# monitored by definition) and a DER interconnection step-up (part of the
# interconnection and visible to the DER management system even though its
# LV side sits at inverter utilization voltage, not primary distribution).
#
# A voltage-only cut cannot separate these classes on physical grounds: a
# DER step-up and an ordinary service transformer both step down to a North
# American utilization voltage, and which one is SCADA-worthy depends on
# what the transformer is FOR, not what voltage it operates at. Measured
# hv_voltage_kv across both shipped feeders' plain transformers (excludes
# regulators — see NetworkTopology.regulators's docstring):
#   IEEE-123:  4.16 kV x1  (xfm1, an ordinary mid-feeder service
#              transformer, bus 61s -> 610), 2.4 kV x25 (all pv_up_* DER
#              step-ups)
#   IEEE-8500: 115 kV x1   (hvmv_sub, the substation transformer, bus
#              hvmv_sub_hsb -> regxfmr_hvmv_sub_lsb), 7.2 kV x1197 (1177
#              service transformers + 20 pv_up_* DER step-ups)
# hv_voltage_kv does not separate "substation" from "everything else fed at
# primary voltage" either: IEEE-123's xfm1 sits at the SAME 4.16 kV primary
# class as the feeder itself despite being an ordinary customer service
# transformer deep in the network, not the feeder head.
#
# The two classes below are each identified structurally instead, from data
# already in NetworkTopology — no engine-specific access needed:
#
# 1. Substation transformer: identified by adjacency to the circuit's
#    slack/source bus (BusType.SLACK, which OpenDSSEngine derives from
#    dss.Vsources — see OpenDSSEngine._build_id_mappings), not by voltage.
#    topology.lines already carries a synthetic edge for every non-line
#    PDElement (see OpenDSSEngine.get_topology's transformer/reactor
#    synthetic-line block), so a bus one hop from the slack bus is adjacent
#    whether that hop is the transformer's own winding or an intervening
#    impedance element — IEEE-8500 connects sourcebus to hvmv_sub's HV bus
#    through a series Reactor, not directly, and this still catches it via
#    that synthetic line. This is unambiguous: exactly one bus is ever
#    marked BusType.SLACK, and standing at or next to it is a structural
#    fact of the model, not a voltage coincidence.
#
#    Measuring it: this correctly finds IEEE-8500's hvmv_sub (1), and
#    correctly finds NONE on IEEE-123. IEEE-123 models its substation
#    regulation as RegControl-tapped transformer reg1a, which
#    OpenDSSEngine already relabels out of topology.transformers into
#    topology.regulators (reported via the existing 6000/7000 regulator
#    bands, not this one) — so IEEE-123 legitimately has zero substation-
#    class entries among its plain transformers, not one. xfm1 (the 4.16 kV
#    transformer the old voltage cut swept in) is NOT adjacent to bus 150
#    and is therefore correctly excluded: it is an ordinary service
#    transformer that happens to share the feeder's primary voltage class,
#    structurally identical to an IEEE-8500 service transformer.
#
# 2. DER step-up transformer: identified by name, not position. Both
#    shipped overlays (examples/Run_IEEE123_ZTCARD.dss,
#    examples/Run_IEEE8500_ZTCARD.dss) name every DER step-up
#    "Transformer.pv_up_*" — 25 on IEEE-123, 20 on IEEE-8500, matching the
#    known DER counts exactly — and that name survives compilation
#    unchanged into TransformerInfo.name (OpenDSSEngine derives it directly
#    from the PDElement name; see _iter_plain_transformers). A previous
#    attempt matched GeneratorInfo.bus (the PVSystem's resolved feeder-side
#    bus — see OpenDSSEngine.get_generator_info) against a transformer's
#    hv_bus, and produced false positives on IEEE-8500 because a step-up and
#    an unrelated service transformer can share a phase-stripped primary bus
#    ID once phase suffixes are stripped. The overlay's naming convention
#    has no such collision risk: it names the transformer directly, not a
#    bus it happens to share with something else.
#
# Measured result of the disjunction below, both shipped feeders' plain
# transformers: IEEE-8500 instruments 21 of 1,198 (1 substation + 20 DER),
# excluding 1,177 service transformers — matching the a-priori expectation
# exactly. IEEE-123 instruments 25 of 26 (0 substation + 25 DER), excluding
# xfm1 — one fewer than a naively-expected "1 substation + 25 DER" would
# predict, because IEEE-123's substation was never a plain transformer to
# begin with (see class 1 above). That is a finding about the model, not a
# defect in this criterion.
_DER_STEP_UP_TRANSFORMER_NAME_PREFIX = "pv_up_"


def _substation_adjacent_bus_ids(topology: NetworkTopology) -> frozenset[BusID]:
    """Bus IDs at, or one hop from, the circuit's slack (source) bus.

    One hop is enough to reach a substation transformer's HV bus even when
    it connects through an intervening impedance element rather than
    directly to the slack bus — see this module's docstring comment above
    _DER_STEP_UP_TRANSFORMER_NAME_PREFIX for IEEE-8500's
    Reactor.hvmv_sub_hsb, caught here via the synthetic line entry
    OpenDSSEngine.get_topology adds for every non-line PDElement.

    Raises:
        ValueError: No bus in the topology is marked BusType.SLACK. Every
            OpenDSS-backed topology this simulator ships marks exactly one
            (see OpenDSSEngine._build_id_mappings's Vsource lookup); an
            empty result here means the engine failed to identify its
            source, not that this feeder legitimately has none.
    """
    slack_bus_ids = {
        bus_id
        for bus_id, bus_info in topology.buses.items()
        if bus_info.bus_type == BusType.SLACK
    }
    if not slack_bus_ids:
        raise ValueError(
            "NetworkTopology has no bus with bus_type == BusType.SLACK — "
            "cannot identify the substation transformer without a source "
            "bus to measure adjacency from."
        )
    adjacent = set(slack_bus_ids)
    for line_info in topology.lines.values():
        if line_info.from_bus in slack_bus_ids:
            adjacent.add(line_info.to_bus)
        if line_info.to_bus in slack_bus_ids:
            adjacent.add(line_info.from_bus)
    return frozenset(adjacent)


def _is_substation_transformer(
    transformer_info: TransformerInfo, substation_adjacent_bus_ids: frozenset[BusID]
) -> bool:
    """Whether transformer_info sits at, or next to, the circuit's source bus.

    See _substation_adjacent_bus_ids for the derivation.
    """
    return (
        transformer_info.hv_bus in substation_adjacent_bus_ids
        or transformer_info.lv_bus in substation_adjacent_bus_ids
    )


def _is_der_step_up_transformer(transformer_info: TransformerInfo) -> bool:
    """Whether transformer_info is a DER interconnection step-up.

    Identified by the overlay's own naming convention — see this module's
    docstring comment above _DER_STEP_UP_TRANSFORMER_NAME_PREFIX for why
    this is reliable where a bus-based match was not.
    """
    return transformer_info.name.lower().startswith(
        _DER_STEP_UP_TRANSFORMER_NAME_PREFIX
    )


def _is_scada_instrumented_transformer(
    transformer_info: TransformerInfo, substation_adjacent_bus_ids: frozenset[BusID]
) -> bool:
    """Whether a real utility would put SCADA telemetry on this transformer.

    See this module's docstring comment above
    _DER_STEP_UP_TRANSFORMER_NAME_PREFIX for the derivation of both
    disjuncts and the evidence each is reliable.
    """
    return _is_substation_transformer(
        transformer_info, substation_adjacent_bus_ids
    ) or _is_der_step_up_transformer(transformer_info)


def _instrumented_transformer_ids_in_hr_order(
    topology: NetworkTopology,
) -> List[TransformerID]:
    """SCADA-instrumented transformer IDs, in the order they occupy HR
    `transformer_loading` (8000+).

    Single source of truth for that ordering: `_build_point_mapping`'s
    transformer_to_hr_index block and `build_transformer_hr_index_to_name`
    (consumed by the MTU to name those same registers) both enumerate this
    list rather than re-filtering `topology.transformers` independently,
    so the two can never assign register N to different transformers.
    """
    substation_adjacent_bus_ids = _substation_adjacent_bus_ids(topology)
    return [
        transformer_id
        for transformer_id, transformer_info in topology.transformers.items()
        if _is_scada_instrumented_transformer(
            transformer_info, substation_adjacent_bus_ids
        )
    ]


def build_transformer_hr_index_to_name(topology: NetworkTopology) -> Dict[int, str]:
    """Register-local HR index -> canonical `transformer-{id}` name, for the
    SCADA-instrumented transformer subset only.

    Published alongside the rest of `grid_topology.json` (see
    `main.py::export_topology_from_schema`) so the MTU can bound its
    holding-register classifier by the same point list this server itself
    uses to populate HR 8000+ (`transformer_to_hr_index` in
    `_build_point_mapping`). The topology's full transformer count is the
    wrong bound for this band: `transformer_loading` is dense only over the
    instrumented subset (25 of 26 on IEEE-123, 21 of 1198 on IEEE-8500), so
    a register-local index here counts positions in that subset, not
    `transformer_id` values, and cannot be produced by the MTU from the
    topology file's `transformers` list alone.

    `transformer_id` is used verbatim in the exported name (matching
    `main.py`'s `topology_data["transformers"][i]["index"] = int(trafo.
    transformer_id)`, which `grid_topology.py`'s `_create_transformers_
    from_data` turns into `x_grid_component_type=f"transformer-{index}"`)
    so a register here names the exact same GridComponent the topology
    export already publishes to Neo4j.
    """
    return {
        idx: f"transformer-{transformer_id}"
        for idx, transformer_id in enumerate(
            _instrumented_transformer_ids_in_hr_order(topology)
        )
    }


# Single source of truth for the *new* register bands introduced alongside
# voltage regulators. get_server_config() and the register-band disjointness
# contract test both derive from this table rather than hand-maintaining a
# duplicate of it (see AGENTS.md's "hand-maintained value that must agree
# with a computed one" defect class).
#
# The pre-3.14 bands (bus/line/DER/load telemetry, breaker coils, DER/load
# writes) are NOT re-derived from here — their bounds are still
# hardcoded as literals in _build_point_mapping/_create_sim_device/
# update_measurements/_handle_register_write, unchanged by this addition.
# Folding them into this table would mean touching the DER/load/bus-voltage
# dispatch branches in _handle_register_write, which is explicitly out of
# scope (S7 comparability). They are listed here anyway so the table is a
# complete picture for the disjointness test and for get_server_config();
# nothing enforces that a literal changed elsewhere still matches its entry
# here — that is a residual, pre-existing gap this change does not close.
# Ranges are half-open [start, end).
REGISTER_BANDS: Dict[str, tuple[int, int]] = {
    "bus_voltages": (0, 1000),
    "line_p_from": (1000, 2000),
    "line_q_from": (2000, 3000),
    "line_loading": (3000, 4000),
    "der_power": (4000, 5000),
    "load_power": (5000, 6000),
    "regulator_tap_readback": (6000, 7000),
    "regulator_setpoint_readback": (7000, 8000),
    "transformer_loading": (8000, 9000),
    "der_setpoints": (10000, 11000),
    "load_adjustments": (11000, 12000),
    "regulator_setpoint_write": (21000, 22000),
}

# ForwardVreg is a voltage on the RegControl's PT secondary base (typically
# ~120V nominal for a single-phase regulator) — always non-negative in
# practice, unlike tap position, so the setpoint write/readback bands use
# plain unsigned tenths-of-a-volt scaling rather than the two's-complement
# int16 convention _safe_float_to_int applies to signed quantities.
_VREG_SCALE = 10.0


# Function codes whose payload targets coils. Read codes are absent because
# SimDevice.action passes set_values=None for reads and the handler returns
# before dispatching.
_COIL_WRITE_FUNCTION_CODES = frozenset({5, 15})

# The one unit this simulator answers as. The MTU's southbound client polls it
# and _create_sim_device registers the SimDevice under it.
_DEVICE_ID = 1

# A Modbus holding register is unsigned 16-bit, but line active/reactive power
# is signed — reverse flow is ordinary. Signed measurements are therefore
# written as two's-complement int16 and saturated at these bounds rather than
# wrapped. See _safe_float_to_int for the defect this fixes.
_INT16_MAX = 32767
_INT16_MIN = -32768


class UnmappedRegisterWriteError(Exception):
    """A holding-register write address matches no declared dispatch band.

    Raised by GridModbusWriteHandler._handle_register_write's fail-loud
    `else`. A programming/configuration defect, not a runtime condition: MTU
    authorized (or a test/tool sent) a write to an address this dispatcher
    has never declared, or the dispatcher's own bounds have drifted from what
    it declares. See Redmine #1293 for the defect class this replaces —
    dropping such a write silently, with no log and no error.
    """


class StrictDeviceSimCore(SimCore):
    """Reject unknown unit IDs the way the pre-3.14 server context did.

    SimCore resolves an unknown device id by falling back to device 0, and
    raises a bare `KeyError` when no such catch-all exists. pymodbus reports
    that as an unhandled exception — a full traceback for every request. The
    old ModbusServerContext(single=False) raised NoSuchIdException, which the
    request handler recognizes and turns into a one-line log plus a
    GATEWAY_NO_RESPONSE exception response.

    That difference matters here specifically because unit-ID scanning is a
    modelled attack behaviour (S6 reconnaissance): the unknown-device path is
    hot, and filling the simulator's log with tracebacks during the scenario
    being measured is its own kind of data loss.
    """

    def _require_device(self, device_id: int) -> None:
        """Raise NoSuchIdException unless the id resolves to a real device."""
        if device_id not in self.devices and 0 not in self.devices:
            raise NoSuchIdException(
                f"device_id - {device_id} does not exist, or is out of range"
            )

    async def async_getValues(
        self, device_id: int, func_code: int, address: int, count: int = 1
    ) -> Any:
        """Read, refusing unknown unit IDs with a proper Modbus exception."""
        self._require_device(device_id)
        return await super().async_getValues(device_id, func_code, address, count)

    async def async_setValues(
        self,
        device_id: int,
        func_code: int,
        address: int,
        values: Union[List[int], List[bool]],
    ) -> Any:
        """Write, refusing unknown unit IDs with a proper Modbus exception."""
        self._require_device(device_id)
        return await super().async_setValues(device_id, func_code, address, values)


class GridModbusWriteHandler:
    """
    Applies master-originated Modbus writes to the simulation engine.

    Installed as `SimDevice.action`, which SimRuntime calls before committing
    any register change and which can veto the request by returning an
    ExcCodes. Returning None lets the register commit proceed, so the datastore
    and the engine only ever agree.

    Addresses arrive here as Modbus protocol addresses. Pre-3.14 code
    subtracted 1 to undo an offset the deprecated ModbusSequentialDataBlock
    introduced (it built its SimData at `address - 1`); building SimData
    directly removes that offset, so no adjustment is correct here.
    """

    def __init__(self, server: "GridModbusServer") -> None:
        """
        Initialize the write handler.

        Args:
            server: GridModbusServer whose engine the writes are applied to
        """
        self.server = server

    async def action(
        self,
        func_code: int,
        start_address: int,
        address: int,
        count: int,
        current_registers: List[int],
        set_values: Optional[Union[List[int], List[bool]]],
    ) -> Optional[ExcCodes]:
        """
        Dispatch a write to the coil or holding-register handler.

        Matches pymodbus's SimAction signature.

        Args:
            func_code: Modbus function code
            start_address: Address of current_registers[0] (unused)
            address: Modbus protocol address being written
            count: Register span of the request — NOT the value count for
                coils, where SimRuntime reports 16-bit registers touched
            current_registers: Live register list (unused; this handler
                permits or refuses, it does not rewrite values)
            set_values: Values being written, or None for a read

        Returns:
            None on success, or ExcCodes.DEVICE_BUSY if the engine lock could
            not be acquired within the configured timeout.
        """
        del start_address, count, current_registers  # part of the hook contract

        if set_values is None:
            return None

        is_coil = func_code in _COIL_WRITE_FUNCTION_CODES
        logger.info(
            f"Modbus write ({'coil' if is_coil else 'HR'}): "
            f"address={address}, "
            f"values={set_values[:10] if len(set_values) > 10 else set_values}"
        )

        if is_coil:
            return self._handle_coil_write(address, list(set_values))
        return self._handle_register_write(address, list(set_values))

    def _handle_coil_write(
        self, address: int, values: Union[List[int], List[bool]]
    ) -> Optional[ExcCodes]:
        """
        Handle coil write operations (breaker control).

        Acquires engine_lock with a bounded wait (matches the simulator's own
        timestep period) instead of blocking indefinitely behind a running
        power-flow solve. A real RTU acks a breaker command in near-constant
        time; a physics solve holding this lock for a whole timestep has no
        real-world analog, so a write that can't get in promptly should
        report DEVICE_BUSY (0x06) -- pymodbus turns this into a real Modbus
        exception response -- rather than silently exhausting the master's
        retry/timeout budget. The lock is acquired once for the whole batch
        (not per value) since a multi-coil write is one device interaction.

        Args:
            address: Starting coil address
            values: Coil values (True=closed, False=open)

        Returns:
            None on success, or ExcCodes.DEVICE_BUSY if the engine lock
            could not be acquired within the configured timeout.
        """
        # Breaker control coils (1000-1999)
        if not (1000 <= address < 2000):
            return None

        server = self.server

        acquired = server._engine_lock.acquire(timeout=server._engine_lock_timeout_s)
        if not acquired:
            logger.warning(
                "Breaker write busy: could not acquire engine lock within "
                f"{server._engine_lock_timeout_s}s (address={address}); "
                "returning DEVICE_BUSY"
            )
            return ExcCodes.DEVICE_BUSY

        try:
            for idx, value in enumerate(values):
                point_index = address + idx
                line_id = LineID(point_index - 1000)

                try:
                    server.engine.get_line_info(line_id)
                    server.engine.set_breaker_status(line_id, bool(value))
                    logger.info(
                        f"Breaker {'CLOSE' if value else 'OPEN'} on line-{line_id}"
                    )
                except KeyError:
                    logger.debug(
                        f"Modbus: Ignoring command for non-existent line {line_id}"
                    )
        finally:
            server._engine_lock.release()

        return None

    def _handle_register_write(  # noqa: C901, PLR0915
        self, address: int, values: Union[List[int], List[bool]]
    ) -> Optional[ExcCodes]:
        """
        Handle holding register write operations.

        Register ranges:
        - 10000-10999: DER active power setpoints (integer kW)
        - 11000-11999: Load demand adjustments (integer kW)
        - 12000-19999: unmapped (see the "Retired bands" note below)
        - 20000-20999: safety-class setpoints — deliberate no-op (see the
          "Safety-band exemption" note below), not an unmapped address
        - 21000-21999: Regulator voltage setpoints (unsigned int, ForwardVreg
          volts * 10, PT-secondary base — not a tap command, see REGISTER_BANDS)

        Acquires engine_lock once for the whole batch with a bounded wait (see
        _handle_coil_write for the rationale) instead of blocking indefinitely
        per value. Returns DEVICE_BUSY immediately if the lock can't be
        acquired in time, rather than exhausting the master's timeout budget.

        Retired bands: 12000-12999 (bus voltage setpoints) was removed per
        docs/decisions/bus_voltage_setpoints_on_opendss.md (Redmine #1293) —
        it called set_bus_reactive_injection, a PandaPowerEngine-only
        capability neither shipped feeder implements, so no write there ever
        had physical effect. An address in 12000-19999 now falls through to
        the final `else` below and raises `UnmappedRegisterWriteError`, same
        as any other address this dispatcher does not recognize.

        Safety-band exemption: 20000-20999 (SAFETY_REGISTER_START/END in
        MTU's ModbusAddressMap) is declared and authorized by MTU, but this
        simulator models no protection-IED state for such a write to act on.
        That is intentional, not the same defect the fail-loud `else` below
        exists to catch: a safety-interlock bypass is scored purely on
        MTU/OPA's allow-vs-deny decision (S3 safety_system_bypass), which
        happens before the write ever reaches this dispatcher, so a no-op
        here changes nothing about what the write measures. Without the
        explicit `elif` branch below, every authorized safety-class write
        would hit the new fail-loud `else` and raise — that would be wrong,
        since this is a known, accounted-for gap rather than an address the
        dispatcher failed to declare.

        Args:
            address: Starting register address
            values: Register values (scaled integers)

        Returns:
            None on success, or ExcCodes.DEVICE_BUSY if the engine lock
            could not be acquired within the configured timeout.
        """
        server = self.server

        acquired = server._engine_lock.acquire(timeout=server._engine_lock_timeout_s)
        if not acquired:
            logger.warning(
                "Register write busy: could not acquire engine lock within "
                f"{server._engine_lock_timeout_s}s (address={address}); "
                "returning DEVICE_BUSY"
            )
            return ExcCodes.DEVICE_BUSY

        try:
            for idx, raw_value in enumerate(values):
                point_address = address + idx

                # DER active power setpoints (HR 10000-10999): value in integer kW
                if 10000 <= point_address < 11000:
                    generator_id = GeneratorID(point_address - 10000)
                    p_mw = raw_value / 1000.0  # kW → MW

                    try:
                        server.engine.set_generator_setpoint(generator_id, p_mw=p_mw)
                        logger.info(
                            f"DER setpoint: generator-{generator_id} -> {p_mw * 1000:.1f} kW"
                        )
                    except KeyError:
                        logger.debug(
                            f"Modbus: Ignoring setpoint for non-existent generator {generator_id}"
                        )
                    except ValueError as e:
                        logger.warning(
                            f"Modbus: Invalid DER setpoint for generator {generator_id}: {e}"
                        )

                # Load demand adjustments (HR 11000-11999): value in integer kW
                elif 11000 <= point_address < 12000:
                    load_id = LoadID(point_address - 11000)
                    p_mw = raw_value / 1000.0  # kW → MW

                    try:
                        server.engine.set_load_demand(load_id, p_mw=p_mw)
                        logger.info(
                            f"Load setpoint: load-{load_id} -> {p_mw * 1000:.1f} kW"
                        )
                    except KeyError:
                        logger.debug(
                            f"Modbus: Ignoring setpoint for non-existent load {load_id}"
                        )
                    except ValueError as e:
                        logger.warning(
                            f"Modbus: Invalid load setpoint for load {load_id}: {e}"
                        )

                # Safety-class setpoints (HR 20000-20999): deliberate no-op.
                # MTU declares and authorizes this band
                # (SAFETY_REGISTER_START/END) and OPA's allow/deny decision on
                # it is what S3 safety_system_bypass measures — that happens
                # upstream of this dispatcher. The simulator models no
                # protection-IED state for a write here to act on, so there is
                # nothing to apply; this branch exists so the write does not
                # fall through to the fail-loud `else` below, which would
                # wrongly treat a known, accounted-for gap as an unmapped
                # address. See this method's docstring, "Safety-band
                # exemption".
                elif 20000 <= point_address < 21000:
                    logger.debug(
                        "Modbus: safety-class write to address "
                        f"{point_address} accepted (allow/deny already "
                        "decided by MTU/OPA); no protection-IED state to "
                        "update in this simulator"
                    )

                # Regulator voltage setpoint (HR 21000-21999): value is an
                # unsigned int, ForwardVreg volts * 10 (PT secondary base).
                # This is deliberately NOT a tap command — OpenDSS's
                # RegControl re-targets the tap to hold its own ForwardVreg
                # on every solve, so a forced tap does not persist (see
                # set_transformer_tap's docstring). Commanding the setpoint
                # is the supervisory-legitimate path and the one whose
                # effect survives a solve. set_regulator_voltage_setpoint is
                # an OpenDSS-only capability (not part of the
                # PowerSystemEngine base interface, like
                # set_bus_reactive_injection above), so it needs the same
                # getattr/fail-loud guard.
                elif (
                    REGISTER_BANDS["regulator_setpoint_write"][0]
                    <= point_address
                    < REGISTER_BANDS["regulator_setpoint_write"][1]
                ):
                    regulator_id = TransformerID(
                        point_address - REGISTER_BANDS["regulator_setpoint_write"][0]
                    )
                    vreg = raw_value / _VREG_SCALE

                    set_vreg = getattr(
                        server.engine, "set_regulator_voltage_setpoint", None
                    )
                    if set_vreg is None:
                        raise TypeError(
                            f"Engine {type(server.engine).__name__} does not "
                            "support regulator voltage setpoints (missing "
                            "set_regulator_voltage_setpoint)"
                        )

                    try:
                        set_vreg(regulator_id, vreg)
                        logger.info(
                            f"Regulator voltage setpoint: regulator-{regulator_id} "
                            f"-> {vreg:.1f} V"
                        )
                    except KeyError:
                        logger.debug(
                            "Modbus: Ignoring voltage setpoint command for "
                            f"non-existent regulator {regulator_id}"
                        )
                    except ValueError as e:
                        logger.warning(
                            "Modbus: Invalid voltage setpoint for regulator "
                            f"{regulator_id}: {e}"
                        )

                # An address matching none of the declared bands above. This
                # used to fall through silently (Redmine #1293: 768 IEEE-8500
                # bus-voltage writes per corpus vanished this way, with no log
                # and no error, before the underlying capability defect was
                # even found). Raising here is deliberate, not merely "safer
                # than silence": pymodbus's SimAction contract already
                # propagates an uncaught exception from this method into an
                # exception response to the master (the missing-capability
                # `TypeError`s above take the same path today), so this is a
                # difference in address coverage, not in error-handling
                # mechanism. A write here means either MTU authorizes an
                # address this dispatcher has never declared, or point_address
                # arithmetic drifted — both are configuration defects that
                # must be visible immediately, not discovered later as a
                # missing auth_decision.
                # Telemetry bands are exempt. REGISTER_BANDS covers both
                # measurement ranges (bus voltages, line flows, DER/load
                # power, regulator readbacks, transformer loading — HR
                # 0-8999) and command ranges.
                # A write into a measurement range is not a defect: the
                # register bank stores it and nothing is commanded, which is
                # what `test_measurement_range_write_does_not_command_the_
                # engine` pins. Raising there would turn an ordinary
                # store-only write into an exception response.
                #
                # So the condition is membership in ANY declared band, not
                # the presence of a dispatch branch. An address in no band at
                # all is the real defect this guard exists for — including
                # the retired bus-voltage range 12000-19999, which is now in
                # no band and must therefore raise rather than vanish the way
                # 13000-19999 silently did before (Redmine #1293).
                elif not any(
                    start <= point_address < end
                    for start, end in REGISTER_BANDS.values()
                ):
                    raise UnmappedRegisterWriteError(
                        f"No declared register band contains holding-register "
                        f"address {point_address}. Declared write bands are "
                        "10000-10999 (DER), 11000-11999 (load), 20000-20999 "
                        "(safety, no-op), and "
                        f"{REGISTER_BANDS['regulator_setpoint_write'][0]}-"
                        f"{REGISTER_BANDS['regulator_setpoint_write'][1] - 1} "
                        "(regulator); measurement bands 0-8999 accept "
                        "store-only writes. Either MTU authorizes an address "
                        "this dispatcher does not declare, or point_address "
                        "arithmetic has drifted from the declared bands. Note "
                        "12000-19999 is the retired bus-voltage band and "
                        "reaching it here means something still targets it."
                    )
        finally:
            server._engine_lock.release()

        return None


class GridModbusServer(ModbusHandler):
    """
    Modbus TCP server that exposes grid measurements and accepts control commands.

    This class bridges the grid simulator with Modbus protocol, allowing SCADA
    systems to monitor and control the grid using standard Modbus TCP communications.
    """

    def __init__(
        self,
        engine: PowerSystemEngine,
        config: Optional[Dict[str, Any]] = None,
        host: str = "0.0.0.0",  # nosec B104 — containerized Modbus server; bind-to-all required for docker port mapping
        port: int = 502,
        enable_authorization: bool = False,
        opa_url: str = "http://opa:8181",
        envoy_url: Optional[str] = None,
        engine_lock: Optional[Union[threading.Lock, threading.RLock]] = None,
        engine_lock_timeout_s: float = 1.0,
    ) -> None:
        """
        Initialize Modbus server.

        Args:
            engine: PowerSystemEngine instance to interface with.
            config: Protocol configuration.
            host: IP address to bind to (default: 0.0.0.0 for all interfaces).
            port: TCP port to listen on (default: 502, standard Modbus).
            enable_authorization: Whether to enable Zero-Trust authorization.
            opa_url: OPA REST API URL (default: http://opa:8181).
            envoy_url: Envoy proxy URL for ext_authz (optional).
            engine_lock: Threading lock shared with the simulation loop. When
                provided, all engine mutations (breaker, setpoint, load) acquire
                this lock to prevent concurrent OpenDSS C-library access.
            engine_lock_timeout_s: Max seconds a write handler waits to acquire
                engine_lock before returning DEVICE_BUSY instead of blocking.
                Default matches the simulator's default timestep so a write
                gets one full step's chance to land before reporting busy.
        """
        super().__init__(engine, config)
        self.host = host
        self.port = port
        self._engine_lock = engine_lock or threading.Lock()
        self._engine_lock_timeout_s = engine_lock_timeout_s
        # Cached topology — set once during start(), avoids calling
        # engine.get_topology() every step in update_measurements().
        self._cached_topology: Optional[NetworkTopology] = None
        self.enable_authorization = enable_authorization
        self.opa_url = opa_url
        self.envoy_url = envoy_url

        self.init_point_mapping_caches()

        # Modbus server context
        # Live register bank, owned by the pymodbus server. Assigned in start()
        # AFTER the server is constructed — a SimDevice only describes the
        # layout; ModbusBaseServer builds the SimCore that actually holds the
        # registers. update_measurements() runs on the simulation thread and
        # returns early while this is None.
        self.context: Optional[SimCore] = None
        self._modbus_server: Optional[ModbusTcpServer] = None
        self.server_task: Optional[asyncio.Task] = None
        self.start_time: Optional[float] = None

        # Statistics
        self.messages_sent = 0
        self.messages_received = 0
        self.errors = 0
        self._nan_count = 0  # NaN/inf conversions in current update cycle (item 22)
        # Values that hit the int16 bounds this cycle. Not expected to fire on
        # either shipped feeder (max |value x 100| ~1100 vs a 32767 limit), so
        # a nonzero count means the scaling assumption for some quantity is
        # wrong, not that the grid is extreme.
        self._saturated_count = 0

        logger.info(
            f"GridModbusServer initialized for {host}:{port}, "
            f"authorization={'enabled' if enable_authorization else 'disabled'}"
        )

    def init_point_mapping_caches(self) -> None:
        """Set every point-mapping cache to empty. Called by `__init__`.

        Public, and a method rather than eight inline assignments, because
        one test fixture legitimately bypasses `__init__` via `__new__` --
        `GridModbusServer` needs a live engine and an OPA URL it has no
        business requiring to exercise the Modbus request path. That fixture
        used to re-declare each cache by hand, which meant every cache added
        here had to be mirrored there with nothing enforcing the agreement.
        It silently desynced twice (both regulator bands, then transformer
        loading), and each time the symptom was all seven tests in that file
        failing at once on an AttributeError from `_create_sim_device`.

        That is this repo's most-repeated defect shape -- a hand-maintained
        value that must agree with a computed one -- so the fix is to remove
        the second copy rather than to add a third line to it. The fixture
        now calls this, and a cache added here reaches it for free.

        Not a dataclass field or class-level default: these are mutable dicts,
        and a class-level `{}` would be shared across instances.
        """
        self.bus_to_hr_index: Dict[int, int] = {}
        self.line_to_hr_index: Dict[int, Dict[str, int]] = {}
        self.line_to_coil_index: Dict[int, int] = {}
        self.der_to_hr_index: Dict[int, int] = {}
        self.load_to_hr_index: Dict[int, int] = {}
        self.regulator_to_hr_index: Dict[int, int] = {}
        self.regulator_setpoint_to_hr_index: Dict[int, int] = {}
        self.transformer_to_hr_index: Dict[int, int] = {}

    def _build_point_mapping(self) -> None:
        """Build mapping between grid elements and Modbus register addresses."""
        # Lazy-fetch so callers (tests, ad-hoc usage) don't have to invoke
        # start() first. start() pre-populates the cache to avoid a redundant
        # query, but a None cache here just means we're being called early.
        if self._cached_topology is None:
            self._cached_topology = self.engine.get_topology()
        topology = self._cached_topology

        # Map buses to holding registers (0-999)
        for idx, bus_id in enumerate(topology.buses.keys()):
            self.bus_to_hr_index[bus_id] = idx
            logger.debug(f"Bus {bus_id} -> HR {idx}")

        # Map lines to holding registers and coils
        for idx, line_id in enumerate(topology.lines.keys()):
            # Active power: HR 1000+
            # Reactive power: HR 2000+
            # Loading: HR 3000+
            self.line_to_hr_index[line_id] = {
                "p_from": 1000 + idx,
                "q_from": 2000 + idx,
                "loading": 3000 + idx,
            }
            # Breaker status: Coil 0+
            self.line_to_coil_index[line_id] = idx
            logger.debug(
                f"Line {line_id} -> HR P:{1000 + idx} Q:{2000 + idx} L:{3000 + idx}, Coil {idx}"
            )

        # Map DER to holding registers (4000+)
        for idx, der_id in enumerate(topology.generators.keys()):
            self.der_to_hr_index[der_id] = 4000 + idx
            logger.debug(f"DER {der_id} -> HR {4000 + idx}")

        # Map loads to holding registers (5000+)
        for idx, load_id in enumerate(topology.loads.keys()):
            self.load_to_hr_index[load_id] = 5000 + idx
            logger.debug(f"Load {load_id} -> HR {5000 + idx}")

        # Map regulators to holding registers (6000+) — readback of tap
        # position, the observable that shows the regulator responding.
        # Setpoint write band is 21000-21999; setpoint readback is 7000+,
        # see REGISTER_BANDS.
        regulator_tap_start = REGISTER_BANDS["regulator_tap_readback"][0]
        regulator_setpoint_start = REGISTER_BANDS["regulator_setpoint_readback"][0]
        for idx, regulator_id in enumerate(topology.regulators.keys()):
            self.regulator_to_hr_index[regulator_id] = regulator_tap_start + idx
            self.regulator_setpoint_to_hr_index[regulator_id] = (
                regulator_setpoint_start + idx
            )
            logger.debug(
                f"Regulator {regulator_id} -> HR tap:{regulator_tap_start + idx} "
                f"setpoint:{regulator_setpoint_start + idx}"
            )

        # Map plain transformers to holding registers (8000+) — loading
        # readback. Regulators are excluded: topology.transformers and
        # topology.regulators are disjoint ID spaces (see
        # NetworkTopology.regulators's docstring), so a regulator never
        # collides with this band. Further restricted to transformers a real
        # utility would SCADA-instrument — see the substation/DER-step-up
        # disjunction documented above _DER_STEP_UP_TRANSFORMER_NAME_PREFIX —
        # a transformer that fails the check gets no register-index entry at
        # all, so update_measurements below never writes it and a poll of
        # its would-be address returns whatever the datastore floor is, not
        # a value this simulator ever wrote. idx counts only instrumented
        # transformers, so the band stays dense with no gaps for the ones
        # skipped.
        transformer_loading_start = REGISTER_BANDS["transformer_loading"][0]
        for idx, transformer_id in enumerate(
            _instrumented_transformer_ids_in_hr_order(topology)
        ):
            self.transformer_to_hr_index[transformer_id] = (
                transformer_loading_start + idx
            )
            logger.debug(
                f"Transformer {transformer_id} -> HR {transformer_loading_start + idx}"
            )

        # Warn -- deliberately NOT raise -- if DERs exist but no transformer
        # matched the step-up name prefix.
        #
        # `_is_der_step_up_transformer` matches a NAME PREFIX, which is the
        # overlay's own convention and enforced nowhere. A renamed overlay
        # would silently instrument zero DER step-ups.
        #
        # Raising was the first instinct and it was wrong: synthetic test
        # topologies legitimately carry generators without step-up
        # transformers, so a raise breaks valid fixtures to catch a
        # hypothetical. It also duplicates a detector that already exists --
        # the MTU's startup coverage log reports "transformers: 0 of N
        # polled", which is precisely this condition, surfaced against a
        # real topology rather than guessed at from inside the simulator.
        #
        # So this is a warning next to that log, not a second gate.
        if topology.generators and not any(
            _is_der_step_up_transformer(info) for info in topology.transformers.values()
        ):
            logger.warning(
                "%d DERs present but no transformer name starts with %r, so no "
                "DER step-up is instrumented. Expected for a synthetic topology; "
                "on a real feeder it means the overlay renamed its step-ups and "
                "the transformer band will be near-empty. Cross-check the MTU's "
                "startup transformer coverage line.",
                len(topology.generators),
                _DER_STEP_UP_TRANSFORMER_NAME_PREFIX,
            )

        logger.info(
            f"Point mapping complete: {len(self.bus_to_hr_index)} buses, "
            f"{len(self.line_to_hr_index)} lines, {len(self.der_to_hr_index)} DERs, "
            f"{len(self.load_to_hr_index)} loads, "
            f"{len(self.regulator_to_hr_index)} regulators, "
            f"{len(self.transformer_to_hr_index)} transformers"
        )

    def _create_sim_device(self) -> SimDevice:
        """
        Create the Modbus datastore definition with appropriate sizes.

        Registered under _DEVICE_ID, matching the unit the MTU's southbound
        client polls. Unknown ids are refused by StrictDeviceSimCore rather
        than silently served.
        `SimDevice` is only a description: the live register bank is built by
        the server when it constructs its SimCore, which is why
        `update_measurements` writes through `self.context` (the SimCore) and
        not through this object.

        Returns:
            SimDevice configured for grid data, with writes routed to
            GridModbusWriteHandler.action
        """
        # Calculate required sizes
        max_hr = (
            max(
                [
                    max(self.bus_to_hr_index.values()) if self.bus_to_hr_index else 0,
                    (
                        max([v["loading"] for v in self.line_to_hr_index.values()])
                        if self.line_to_hr_index
                        else 0
                    ),
                    max(self.der_to_hr_index.values()) if self.der_to_hr_index else 0,
                    max(self.load_to_hr_index.values()) if self.load_to_hr_index else 0,
                    (
                        max(self.regulator_to_hr_index.values())
                        if self.regulator_to_hr_index
                        else 0
                    ),
                    (
                        max(self.regulator_setpoint_to_hr_index.values())
                        if self.regulator_setpoint_to_hr_index
                        else 0
                    ),
                    (
                        max(self.transformer_to_hr_index.values())
                        if self.transformer_to_hr_index
                        else 0
                    ),
                    # Include write registers up to the regulator setpoint band's
                    # highest address; derived so a moved band automatically
                    # widens the datastore instead of leaving it too small.
                    REGISTER_BANDS["regulator_setpoint_write"][1] - 1,
                ]
            )
            + 1
        )

        max_coil = (
            max(
                [
                    (
                        max(self.line_to_coil_index.values())
                        if self.line_to_coil_index
                        else 0
                    ),
                    1999,  # Include control coils
                ]
            )
            + 1
        )

        # Coils (discrete outputs) - for breaker status and control.
        # SimDevice packs 16 coils per register and pads the tail itself, so
        # max_coil needs no alignment here.
        initial_coil_values = [False] * max_coil
        if self._cached_topology is None:
            self._cached_topology = self.engine.get_topology()
        topology = self._cached_topology
        for line_id, coil_index in self.line_to_coil_index.items():
            # Set status coils (0-999) to match actual breaker state
            line_info = topology.lines[LineID(line_id)]
            initial_coil_values[coil_index] = bool(line_info.in_service)

        device = SimDevice(
            _DEVICE_ID,
            simdata=(
                [SimData(0, values=initial_coil_values, datatype=DataType.BITS)],
                # Discrete inputs - not used, but required by Modbus
                [SimData(0, values=False, count=100, datatype=DataType.BITS)],
                [SimData(0, values=0, count=max_hr, datatype=DataType.REGISTERS)],
                # Input registers - not used, but required by Modbus
                [SimData(0, values=0, count=100, datatype=DataType.REGISTERS)],
            ),
            action=GridModbusWriteHandler(self).action,
        )

        logger.info(f"Datastore created: HR={max_hr}, Coils={max_coil}")
        return device

    def _safe_float_to_int(
        self, value: float, scale: float = 1.0, default: int = 0
    ) -> int:
        """Scale a float into a value a 16-bit holding register can hold.

        Returns `default` (0) and increments self._nan_count when the value
        is invalid. Callers in update_measurements() accumulate this counter
        and log a warning at the end of each update cycle so persistent NaN
        storms surface in logs without crashing the server.

        **Signed quantities are encoded as two's-complement int16.** This is
        the fix for a defect that silently destroyed telemetry on every cell
        of every corpus:

        A Modbus holding register is unsigned 16-bit. Line active and
        reactive power are signed — reverse flow is ordinary physics, and on
        IEEE-8500 72 of 4904 lines carry negative P and 141 negative Q. This
        function used to return `int(value * scale)` unclamped, so a register
        could hold -1. pymodbus then raises inside `struct.pack('>H', -1)`
        while building the response and **never replies at all** — not an
        exception response, silence.

        Measured consequence: MTU's southbound poll reads holding registers
        in blocks of 125. Any block containing one such register hangs
        forever (the client had no timeout), so the poll loop wedged on its
        first iteration. Every ieee_123 baseline recorded exactly 2 poll
        deltas and every ieee_8500 baseline recorded 0, against windows that
        should have produced tens to hundreds. Register 1038 on IEEE-8500 is
        line 38, P = -0.0165 MW -> int(-1.648) = -1.

        Two's complement rather than clamping to [0, 65535]: clamping would
        report reverse flow as zero, which is wrong data rather than absent
        data. Values 0..32767 encode identically either way, so nothing that
        works today changes — the only values affected are the ones that
        currently kill the server.

        Saturates at the int16 bounds rather than wrapping. A wrap turns a
        large positive flow into a large negative one, which is worse than a
        clamp because it is physically plausible and therefore invisible.
        Measured headroom on IEEE-8500: max |value x 100| is ~1100 against a
        32767 limit, so saturation is not expected to fire — it is a guard,
        and it counts when it does.

        Args:
            value: Float value to convert
            scale: Scaling factor to apply before conversion
            default: Default value to use if value is invalid

        Returns:
            A value in [0, 65535], being the two's-complement encoding of the
            scaled input. Consumers must decode signed bands as int16 — see
            MTU's protocols/south/modbus_client.decode_signed_register.
        """
        if math.isnan(value) or math.isinf(value):
            self._nan_count += 1
            return default
        try:
            scaled = int(value * scale)
        except (ValueError, OverflowError):
            self._nan_count += 1
            return default

        if scaled > _INT16_MAX:
            self._saturated_count += 1
            scaled = _INT16_MAX
        elif scaled < _INT16_MIN:
            self._saturated_count += 1
            scaled = _INT16_MIN

        return scaled & 0xFFFF

    def update_measurements(self) -> None:  # noqa: C901, PLR0915
        """
        Update Modbus registers with current grid state.

        This is called periodically to sync grid state to Modbus registers.
        Can accept either GridState object or legacy dict format for compatibility.
        """
        if not self.context:
            logger.warning("Server context not initialized, cannot update measurements")
            return

        try:
            # Get current grid state from the engine (called from simulation
            # thread callback, so OpenDSS access is safe — same thread as step())
            state = self.engine.get_current_state()

            # Handle None state
            if state is None:
                logger.warning("No grid state available")
                return

            core = self.context

            # Update bus voltages (HR 0-999)
            for bus_id, bus_state in state.buses.items():
                if bus_id in self.bus_to_hr_index:
                    hr_index = self.bus_to_hr_index[bus_id]
                    value = self._safe_float_to_int(bus_state.voltage_pu, 1000, 0)
                    write_registers(core, _DEVICE_ID, "h", hr_index, [value])

            # Update line flows and loading (HR 1000-3999)
            for line_id, line_state in state.lines.items():
                if line_id in self.line_to_hr_index:
                    indices = self.line_to_hr_index[line_id]

                    # Active power (MW * 100)
                    value_p = self._safe_float_to_int(line_state.p_from_mw, 100, 0)
                    write_registers(core, _DEVICE_ID, "h", indices["p_from"], [value_p])

                    # Reactive power (MVAR * 100)
                    value_q = self._safe_float_to_int(line_state.q_from_mvar, 100, 0)
                    write_registers(core, _DEVICE_ID, "h", indices["q_from"], [value_q])

                    # Loading (% * 10)
                    value_l = self._safe_float_to_int(line_state.loading_percent, 10, 0)
                    write_registers(
                        core, _DEVICE_ID, "h", indices["loading"], [value_l]
                    )

            # Update transformer loading (HR 8000+). Plain transformers only
            # (state.transformers excludes regulators — see TransformerState's
            # docstring); regulators are handled separately below via the
            # engine-direct tap/setpoint readback.
            for transformer_id, transformer_state in state.transformers.items():
                if transformer_id in self.transformer_to_hr_index:
                    hr_index = self.transformer_to_hr_index[transformer_id]
                    value = self._safe_float_to_int(
                        transformer_state.loading_percent, 10, 0
                    )
                    write_registers(core, _DEVICE_ID, "h", hr_index, [value])

            # Update breaker status (Coils 0-999)
            if self._cached_topology is None:
                return  # Not initialized yet
            topology = self._cached_topology
            open_lines = self.engine.open_line_ids()
            for line_id_int, coil_index in self.line_to_coil_index.items():
                line_id = LineID(line_id_int)
                if line_id in topology.lines:
                    write_bits(
                        core, _DEVICE_ID, "c", coil_index, [line_id not in open_lines]
                    )

            # Update DER power (HR 4000+)
            for der_id, der_state in state.generators.items():
                if der_id in self.der_to_hr_index:
                    hr_index = self.der_to_hr_index[der_id]
                    value = self._safe_float_to_int(der_state.p_mw, 100, 0)
                    write_registers(core, _DEVICE_ID, "h", hr_index, [value])

            # Update load power (HR 5000+)
            for load_id, load_state in state.loads.items():
                if load_id in self.load_to_hr_index:
                    hr_index = self.load_to_hr_index[load_id]
                    value = self._safe_float_to_int(load_state.p_mw, 100, 0)
                    write_registers(core, _DEVICE_ID, "h", hr_index, [value])

            # Update regulator tap positions (HR 6000+) and voltage
            # setpoints (HR 7000+). Neither is part of GridState (tap is
            # near-static, not a power-flow result; setpoint is a control
            # target, not a measurement), so both are read straight from
            # the engine rather than from `state` like the blocks above —
            # mirroring the getattr/fail-loud pattern used for
            # set_bus_reactive_injection, since these are engine-specific
            # capabilities, not part of PowerSystemEngine. Tap position
            # stays readable even though this scenario commands the
            # setpoint, not the tap: it is the observable that shows the
            # regulator actually responding to a setpoint command.
            if self.regulator_to_hr_index:
                get_tap_position = getattr(
                    self.engine, "get_regulator_tap_position", None
                )
                if get_tap_position is None:
                    raise TypeError(
                        f"Engine {type(self.engine).__name__} populated "
                        "topology.regulators but does not implement "
                        "get_regulator_tap_position — readback would "
                        "silently report stale tap values."
                    )
                for regulator_id, hr_index in self.regulator_to_hr_index.items():
                    tap_position = get_tap_position(regulator_id)
                    value = self._safe_float_to_int(float(tap_position), 1, 0)
                    write_registers(core, _DEVICE_ID, "h", hr_index, [value])

            if self.regulator_setpoint_to_hr_index:
                get_vreg = getattr(self.engine, "get_regulator_voltage_setpoint", None)
                if get_vreg is None:
                    raise TypeError(
                        f"Engine {type(self.engine).__name__} populated "
                        "topology.regulators but does not implement "
                        "get_regulator_voltage_setpoint — readback would "
                        "silently report stale setpoint values."
                    )
                for (
                    regulator_id,
                    hr_index,
                ) in self.regulator_setpoint_to_hr_index.items():
                    vreg = get_vreg(regulator_id)
                    value = self._safe_float_to_int(float(vreg), _VREG_SCALE, 0)
                    write_registers(core, _DEVICE_ID, "h", hr_index, [value])

            self.messages_sent += 1
            if self._nan_count > 0:
                logger.warning(
                    "update_measurements: %d NaN/inf value(s) substituted with 0 "
                    "this cycle — grid engine may be in a transient invalid state. "
                    "If this persists, check pandapower convergence.",
                    self._nan_count,
                )
            if self._saturated_count > 0:
                logger.warning(
                    "update_measurements: %d value(s) saturated at the int16 "
                    "register bounds this cycle. Headroom on the shipped "
                    "feeders is ~30x, so this means a scale factor is wrong "
                    "for some quantity, not that the grid is extreme — the "
                    "affected registers are now clamped and no longer track "
                    "the engine.",
                    self._saturated_count,
                )
            self._nan_count = 0  # reset per-cycle counter
            self._saturated_count = 0

        except Exception as e:
            logger.error(f"Error updating Modbus registers: {e}")
            self.errors += 1

    async def start(self) -> None:  # type: ignore[override]
        """Start the Modbus TCP server."""
        try:
            # Cache topology once at startup — used by _build_point_mapping,
            # _create_sim_device, and update_measurements without re-querying.
            self._cached_topology = self.engine.get_topology()

            # Build point mapping
            self._build_point_mapping()

            # Construct the server directly rather than via StartAsyncTcpServer:
            # that helper discards the server object, and the server owns the
            # SimCore that update_measurements has to write into.
            sim_device = self._create_sim_device()
            self._modbus_server = ModbusTcpServer(
                sim_device, address=(self.host, self.port)
            )
            # Replace the plain SimCore ModbusBaseServer built for itself.
            # There is no injection point, and self.context must be the object
            # the request path actually uses: update_measurements writes into
            # its register lists, so a stale reference means telemetry goes
            # nowhere with nothing raised.
            self.context = StrictDeviceSimCore(sim_device)
            self._modbus_server.context = self.context

            # Mark as running
            self._running = True
            self.start_time = time.time()

            # Start server
            logger.info(f"Starting Modbus TCP server on {self.host}:{self.port}")
            await self._modbus_server.serve_forever()

        except Exception as e:
            logger.error(f"Error starting Modbus server: {e}")
            self._running = False
            raise

    async def stop(self) -> None:  # type: ignore[override]
        """Stop the Modbus TCP server."""
        try:
            if self._modbus_server is not None:
                await self._modbus_server.shutdown()
                self._modbus_server = None

            if self.server_task:
                self.server_task.cancel()
                self.server_task = None

            self.context = None
            self._running = False
            logger.info("Modbus TCP server stopped")

        except Exception as e:
            logger.error(f"Error stopping Modbus server: {e}")

    def is_running(self) -> bool:
        """Check if Modbus server is running."""
        return self._running

    def get_statistics(self) -> Dict[str, Any]:
        """Get Modbus server statistics."""
        uptime = time.time() - self.start_time if self.start_time else 0
        return {
            "num_connections": 1 if self._running else 0,  # Simplified
            "messages_sent": self.messages_sent,
            "messages_received": self.messages_received,
            "errors": self.errors,
            "uptime_seconds": uptime,
        }

    def get_server_config(self) -> Dict[str, Any]:
        """Get Modbus server configuration."""
        return {
            "unit_id": 1,
            "port": self.port,
            "host": self.host,
            "register_blocks": {
                **{
                    name: f"HR {start}-{end - 1}"
                    for name, (start, end) in REGISTER_BANDS.items()
                },
                "breaker_status": "Coils 0-999",
                "breaker_control": "Coils 1000-1999",
            },
        }

    def set_holding_register(self, address: int, value: int) -> None:
        """Set a holding register value (internal write, no engine dispatch)."""
        if self.context:
            write_registers(self.context, _DEVICE_ID, "h", address, [value])

    def set_input_register(self, address: int, value: int) -> None:
        """Set an input register value (internal write, no engine dispatch)."""
        if self.context:
            write_registers(self.context, _DEVICE_ID, "i", address, [value])

    def set_coil(self, address: int, value: bool) -> None:
        """Set a coil value (internal write, no engine dispatch)."""
        if self.context:
            write_bits(self.context, _DEVICE_ID, "c", address, [bool(value)])

    def set_discrete_input(self, address: int, value: bool) -> None:
        """Set a discrete input value (internal write, no engine dispatch)."""
        if self.context:
            write_bits(self.context, _DEVICE_ID, "d", address, [bool(value)])

    def get_point_mapping(self) -> Dict[str, Any]:
        """
        Get the current point mapping configuration.

        Returns:
            Dictionary describing the point mapping
        """
        return {
            "buses": dict(self.bus_to_hr_index),
            "lines": {
                "holding_registers": dict(self.line_to_hr_index),
                "coils": dict(self.line_to_coil_index),
            },
            "ders": dict(self.der_to_hr_index),
            "loads": dict(self.load_to_hr_index),
            "regulators": dict(self.regulator_to_hr_index),
            "regulator_setpoints": dict(self.regulator_setpoint_to_hr_index),
            "transformers": dict(self.transformer_to_hr_index),
        }
