"""simcore_registers.py — internal (unauthorized) writes into a live SimCore.

pymodbus 3.14+ routes every wire request through ``SimCore`` → ``SimRuntime``,
and the only extension point is ``SimDevice.action``, which fires on reads and
writes alike. Both Modbus servers in this stack need a *second* path: one that
mirrors internally-computed state (grid measurements, polled telemetry) into
the register bank WITHOUT tripping that hook, because those writes did not come
from a Modbus master. They must not be authorized, must not emit auth_decision
events, and must not be re-applied to the simulation engine as commands.

Before 3.14 that path was the synchronous ``ModbusDeviceContext.setValues``,
with ``async_setValues`` reserved for wire traffic; both servers relied on the
split. 3.14 deleted it. This module restores it by writing straight into the
register list ``SimRuntime`` holds.

Public interface:
    ``write_registers(core, device_id, kind, address, values)``
    ``write_bits(core, device_id, kind, address, values)``

``kind`` is ``SimRuntime``'s block key — ``"c"`` coils, ``"d"`` discrete
inputs, ``"h"`` holding registers, ``"i"`` input registers — or ``"x"`` when
the device was built with a single shared block.

Design constraints:

- This reaches into ``SimRuntime.block``, which upstream marks internal. That
  is a deliberate trade: ``update_measurements()`` runs on the grid simulator's
  simulation thread and cannot await, so the supported
  ``SimCore.async_setValues`` path would need one ``run_coroutine_threadsafe``
  hop per register per timestep — thousands per second.
  ``tests/stack/test_pymodbus_signature_contract_unit.py`` pins the block
  tuple's shape and the block keys so an upstream change fails at test time
  rather than inside a running cell.
- Bit blocks (``"c"``, ``"d"``) store 16 coils per register and their block
  start address is in REGISTER units, not coil units — ``SimDevice`` divides by
  16 when it builds them. Register blocks store their start address as-is.
  Conflating the two silently shifts every coil by a factor of 16.
- Every failure raises. An address outside the block, an unknown device, or an
  unknown block kind means the point map and the datastore disagree. Clamping
  or skipping would leave those points permanently absent from telemetry while
  the server reports healthy — the defect class this stack keeps rediscovering
  (Redmine #1237).

Duplicated deliberately. ``dockerfiles/common/simcore_registers.py`` in the
ztcard-stack repo is the MTU's copy of this module. The grid simulator is a
git submodule with its own CI that builds and tests the repo standalone, so a
bind mount from the parent is a deployment detail, not a source dependency —
importing across the boundary passes locally and fails in this repo's
pipeline. Keep the two in sync by hand; the behaviour is pinned on both sides
by round-trip tests, and the pymodbus surface it relies on is frozen by
tests/test_basic.py here and
tests/stack/test_pymodbus_signature_contract_unit.py in the parent.
"""

from typing import List, Tuple

from pymodbus.simulator.simcore import SimCore
from pymodbus.simulator.simutils import DataType, SimUtils

_BIT_BLOCK_KINDS = frozenset({"c", "d"})
_VALID_BLOCK_KINDS = frozenset({"c", "d", "h", "i", "x"})

# SimRuntime.block[kind] == (start_address, register_count, registers, flags).
_Block = Tuple[int, int, List[int], List[int]]


def _resolve_block(core: SimCore, device_id: int, kind: str) -> _Block:
    """Return the live ``(start, count, registers, flags)`` tuple for a block.

    Resolves ``device_id`` the way ``SimCore`` itself does — an exact match
    first, then the catch-all device 0 — so an internal write lands in the same
    bank a wire request for that unit ID would.

    Raises:
        ValueError: unknown ``kind``.
        KeyError: no such device, or the device has no block of that kind.
    """
    if kind not in _VALID_BLOCK_KINDS:
        raise ValueError(
            f"Unknown SimRuntime block kind {kind!r}; "
            f"expected one of {sorted(_VALID_BLOCK_KINDS)}"
        )
    if device_id in core.devices:
        runtime = core.devices[device_id]
    elif 0 in core.devices:
        runtime = core.devices[0]
    else:
        raise KeyError(
            f"No SimDevice for device_id={device_id} and no catch-all device 0; "
            f"configured ids are {sorted(core.devices)}"
        )
    block_id = "x" if "x" in runtime.block else kind
    if block_id not in runtime.block:
        raise KeyError(
            f"Device {device_id} has no {kind!r} block; "
            f"available blocks are {sorted(runtime.block)}"
        )
    return runtime.block[block_id]


def _check_offset(
    flags: List[int],
    register_count: int,
    offset: int,
    span: int,
    address: int,
    kind: str,
) -> None:
    """Raise unless ``[offset, offset + span)`` is in range and addressable.

    Mirrors ``SimRuntime.get_reg_block`` bounds plus ``__check_block``'s
    ``DataType.INVALID`` test, so an internal write can never reach a register
    a wire request would be refused. ``DataType.INVALID`` marks the padding
    ``SimDevice`` inserts between non-contiguous ``SimData`` entries and the
    guard register it appends to every block.
    """
    if offset < 0 or offset + span > register_count:
        raise IndexError(
            f"Internal write to {kind!r} address {address} resolves to register "
            f"offset {offset}..{offset + span - 1}, outside the block's "
            f"0..{register_count - 1}. The point mapping and the datastore "
            f"disagree about this block's size."
        )
    for i in range(offset, offset + span):
        if flags[i] & SimUtils.RunTimeFlag_TYPE == DataType.INVALID:
            raise IndexError(
                f"Internal write to {kind!r} address {address} reaches register "
                f"offset {i}, which SimDevice marked INVALID (unallocated "
                f"padding or the block's trailing guard register)."
            )


def write_registers(
    core: SimCore, device_id: int, kind: str, address: int, values: List[int]
) -> None:
    """Write holding/input registers in place, bypassing ``SimDevice.action``.

    Args:
        core: The server's live datastore (``server.context``).
        device_id: Modbus unit ID; falls back to the catch-all device 0.
        kind: ``"h"`` holding registers or ``"i"`` input registers.
        address: Modbus protocol address of ``values[0]``.
        values: Register values to store.

    Raises:
        ValueError: ``kind`` is a bit block or unrecognized.
        IndexError: the range is outside the block or lands on INVALID.
    """
    if kind in _BIT_BLOCK_KINDS:
        raise ValueError(f"Use write_bits for bit block {kind!r}, not write_registers")
    if not values:
        return
    start_address, register_count, registers, flags = _resolve_block(
        core, device_id, kind
    )
    offset = address - start_address
    _check_offset(flags, register_count, offset, len(values), address, kind)
    registers[offset : offset + len(values)] = values


def write_bits(
    core: SimCore, device_id: int, kind: str, address: int, values: List[bool]
) -> None:
    """Write coils/discrete inputs in place, bypassing ``SimDevice.action``.

    ``address`` is the coil address as a Modbus master would send it. The
    containing registers are read, the bit window is spliced in, and the
    registers are written back — the same round trip ``SimRuntime.get_bit_block``
    performs, so partial-register writes preserve their neighbours.

    Args:
        core: The server's live datastore (``server.context``).
        device_id: Modbus unit ID; falls back to the catch-all device 0.
        kind: ``"c"`` coils or ``"d"`` discrete inputs.
        address: Modbus protocol coil address of ``values[0]``.
        values: Bit values to store.

    Raises:
        ValueError: ``kind`` is not a bit block.
        IndexError: the range is outside the block or lands on INVALID.
    """
    if kind not in _BIT_BLOCK_KINDS:
        raise ValueError(f"Use write_registers for register block {kind!r}")
    if not values:
        return
    start_address, register_count, registers, flags = _resolve_block(
        core, device_id, kind
    )
    # Bit blocks index registers, not bits: SimDevice.__create_block_bits stores
    # int(first_coil_address / 16) as the block start.
    offset = address // 16 - start_address
    bit_offset = address % 16
    span = (bit_offset + len(values) + 15) // 16
    _check_offset(flags, register_count, offset, span, address, kind)
    window = registers[offset : offset + span]
    SimUtils.mergeBitsToRegisters(bit_offset, window, values)
    registers[offset : offset + span] = window
