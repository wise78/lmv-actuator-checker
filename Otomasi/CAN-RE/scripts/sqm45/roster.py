"""Actuator roster: address -> role name, CAN identifiers, and confidence.

One place that answers "what is actuator N called and which identifiers does
it use", so the engine, the web API and the UI cannot drift apart.

Addressing model (CONFIRMED on a live 4-servo LMV, 2026-08-28)
--------------------------------------------------------------
The LMV5 supports ACT1-6 plus a VSD (LMV5-quickstartguide.pdf p.114). An
actuator's address is ordinal by role and is set with the red button on the
unit itself; the AZL52 and the actuator's own LED blink count agree with it
(p.66: 1 blink = air, 2 = gas/gas-oil, 3 = oil, 4 = aux1, "and so on").

    node        = 9 + address                 measured: addr 1-4 -> nodes 10-13
    actual  ID  = 0x180 + node                measured: 0x18A 0x18B 0x18C 0x18D
    target  ID  = 0x181 + 0x20 * (address-1)  measured: 0x181 0x1A1 0x1C1

Bits 5-6 of the target identifier are a 2-bit channel field counting
00/01/10/11, which is what makes the +0x20 stride an extrapolation of a
confirmed sequence rather than a guess -- for addresses 1-4.

Where the scheme runs out
-------------------------
The channel field is TWO BITS, so it holds exactly four targets and 0x1E1 is
the last of them. There is no 5th or 6th target identifier to extrapolate --
0x201 is the RPDO1 function code, a different object class, not "the stride
one step further". target_id() returns None for addresses 5-6 accordingly.

Their FEEDBACK identifiers (0x18E, 0x18F) are fine, because those follow node
numbering rather than the channel field. So actuators 5-6 are fully
DISCOVERABLE, DECODABLE and MONITORABLE, and are un-commandable BY
CONSTRUCTION rather than by policy.

Address 4 WAS the open case: 0x1E1 is the last channel slot and its constant
0x953A was fitted over three points with one free parameter. It was tested on
the bench 2026-08-28 -- commanded a real SQM48 from 70.3 to 62.5 deg, +0.20 deg
error, zero fault frames -- so the XOR rule (constant bits 7-8 = channel bits
XOR 0b01) is now confirmed at all four of its values. is_experimental() is kept
for the next unproven channel, and currently returns False for everything.
"""

from __future__ import annotations

MAX_ADDRESS = 6            # LMV5 ACT1-6 (LMV5-quickstartguide.pdf p.114)
NODE_OFFSET = 9            # node = NODE_OFFSET + address
ACTUAL_BASE = 0x180        # actual ID = ACTUAL_BASE + node
TARGET_BASE = 0x181        # target ID = TARGET_BASE + TARGET_STRIDE*(address-1)
TARGET_STRIDE = 0x20

# Role slugs are the dictionary keys used everywhere (engine, socket payloads,
# UI). Address 2 was called "fuel" before the 4-servo session showed it is
# specifically the GAS actuator, with oil a separate address -- see ALIASES.
NAMES = {1: "air", 2: "gas", 3: "oil", 4: "aux1", 5: "aux2", 6: "aux3"}
LABELS = {1: "AIR", 2: "GAS", 3: "OIL", 4: "AUX 1", 5: "AUX 2", 6: "AUX 3"}

# Kept so an older caller, saved URL or script does not silently address the
# wrong servo: "fuel" resolves to the gas actuator, which is what it always
# meant on a 2-servo bench.
ALIASES = {"fuel": "gas"}

# How far the target identifier + integrity constant for this address have
# actually been verified against hardware.
#   confirmed  -- observed on the wire driving that actuator
#   hypothesis -- identifier extrapolated and constant fitted, never observed
#   unknown    -- identifier itself outside the verified block
TARGET_CONFIDENCE = {
    1: "confirmed",    # 0x181 / 0x94BA
    2: "confirmed",    # 0x1A1 / 0x943A
    3: "confirmed",    # 0x1C1 / 0x95BA  (held-out validated 2026-08-28)
    4: "confirmed",    # 0x1E1 / 0x953A  CONFIRMED on hardware 2026-08-28: predicted
                       #                 from the XOR rule, then commanded a real
                       #                 SQM48 70.3->62.5 deg, err +0.20, 0 faults
    5: "no-scheme",    # channel field is full; no target identifier exists for it
    6: "no-scheme",
}

# Per-role accent colours, so N cards stay distinguishable in the UI and the
# chart. Air/gas keep the two colours the 2-servo dashboard always used.
COLORS = {
    "air":  "#2D62C4",
    "gas":  "#C0701A",
    "oil":  "#6B4FA8",
    "aux1": "#17734A",
    "aux2": "#A8324A",
    "aux3": "#0F6E78",
}


def addresses() -> list[int]:
    return list(range(1, MAX_ADDRESS + 1))


def node_for(address: int) -> int:
    return NODE_OFFSET + address


def actual_id(address: int) -> int:
    return ACTUAL_BASE + node_for(address)


# Bits 5-6 of the target identifier are a TWO-BIT channel field:
#   0x181=00 air, 0x1A1=01 gas, 0x1C1=10 oil, 0x1E1=11 aux1
# Four values, all four used. 0x1E1 is therefore the LAST target inside the
# TPDO1 block (0x181-0x1FF) and the +0x20 stride cannot continue: the next
# step, 0x201, is not "the same scheme one further along" -- 0x200 is the
# RPDO1 function code, a different CANopen object class entirely.
#
# So addresses 5 and 6 have NO KNOWN target identifier, which is a stronger
# statement than "unverified". target_id() returns None for them rather than a
# plausible-looking hex value someone might transmit.
TARGET_SCHEME_MAX = 4


def target_id(address: int) -> int | None:
    if address > TARGET_SCHEME_MAX:
        return None
    return TARGET_BASE + TARGET_STRIDE * (address - 1)


def sdo_request_id(address: int) -> int:
    return 0x600 + node_for(address)


def sdo_reply_id(address: int) -> int:
    return 0x580 + node_for(address)


def name_for(address: int) -> str:
    return NAMES[address]


def label_for(address: int) -> str:
    return LABELS[address]


def address_for_name(role: str) -> int | None:
    role = ALIASES.get(role, role)
    for address, name in NAMES.items():
        if name == role:
            return address
    return None


def address_for_actual_id(can_id: int) -> int | None:
    """Which actuator address does this feedback identifier belong to?"""
    address = can_id - ACTUAL_BASE - NODE_OFFSET
    return address if 1 <= address <= MAX_ADDRESS else None


def address_for_target_id(can_id: int) -> int | None:
    offset = can_id - TARGET_BASE
    if offset < 0 or offset % TARGET_STRIDE:
        return None
    address = offset // TARGET_STRIDE + 1
    # Bounded by the channel field, not by MAX_ADDRESS: 0x201 would otherwise
    # decode to "address 5" and give a bogus identifier a name.
    return address if 1 <= address <= TARGET_SCHEME_MAX else None


def has_target(address: int) -> bool:
    """Is there a known target identifier for this address at all?"""
    return target_id(address) is not None


def commandable(address: int) -> bool:
    """May this actuator be COMMANDED, as opposed to merely monitored?

    True for a proven channel AND for the one experimental channel (address 4),
    because there the first move is a legitimate bench test with a known,
    self-limiting failure mode. False only where no target identifier exists
    at all, which no amount of intent can fix.

    Callers that need to distinguish "known good" from "under test" must ask
    is_experimental() -- the UI does, so aux1 never renders like oil.
    """
    return TARGET_CONFIDENCE.get(address) in ("confirmed", "hypothesis")


def is_experimental(address: int) -> bool:
    """Commanding this address tests an unproven constant rather than using one."""
    return TARGET_CONFIDENCE.get(address) == "hypothesis"


ACTUAL_IDS = {actual_id(a): a for a in addresses()}
TARGET_IDS = {target_id(a): a for a in addresses() if target_id(a) is not None}
