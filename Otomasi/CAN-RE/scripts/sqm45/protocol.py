"""SQM45 / LMV51 CAN payload protocol: decode and encode, no transport.

Confirmed observed-state decodes
---------------------------------
Air target/reference  0x181 bytes0:1 BE u16: deg = -0.1*raw + 6543.5
Fuel target/reference 0x1A1 bytes0:1 BE u16: deg = -0.1*raw + 6543.5
Air actual             0x18A bytes0:1 BE u16: deg =  0.1*raw - 1238.8
Fuel actual            0x18B bytes0:1 BE u16: deg =  0.1*raw - 1238.8

Bytes 2:3 of a target frame are a state word:
    0x0000  hold -- "no command active". The actuator keeps its position.
    0x9E00  move -- "drive to this target".
The target is a setpoint, not a trajectory: send the final angle and the
actuator ramps itself at about 3 deg/s (see FINDINGS.md, 2026-08-14).

This module operates only on (can_id, dlc, bytes) already extracted from the
wire. See transport.py for turning a specific adapter's byte stream into
that shape.
"""

from __future__ import annotations

from dataclasses import dataclass


POSITION_IDS = {0x181, 0x18A, 0x18B, 0x1A1}
# Role strings are the DECODE-LAYER labels the golden vectors were frozen
# against; "fuel" is address 2 (gas). engine/roster.py owns the operator-
# facing naming and maps these across. Extending the tables lets a 3rd-6th
# actuator decode at all -- previously decode_position_frame returned None
# for 0x18C-0x18F, so a healthy servo produced no event whatsoever.
TARGET_IDS = {0x181: "air", 0x1A1: "fuel", 0x1C1: "oil", 0x1E1: "aux1"}
ACTUAL_IDS = {0x18A: "air", 0x18B: "fuel", 0x18C: "oil",
              0x18D: "aux1", 0x18E: "aux2", 0x18F: "aux3"}
# 0x1C1 measured 2026-08-28 on a live 4-servo LMV: 100.00% of 698 oil-target
# frames, with 0x181/0x1A1 revalidating at 100% in the same capture.
# 0x1E1 (aux) is NOT here on purpose -- its constant is a fit with one free
# parameter, never observed, and decoding against a guessed constant would
# manufacture integrity failures rather than report real ones.
TARGET_INTEGRITY_CONSTANT = {0x181: 0x94BA, 0x1A1: 0x943A, 0x1C1: 0x95BA,
                             # 0x1E1 promoted from prediction to fact on the
                             # bench 2026-08-28: the frame built with it moved a
                             # real SQM48 accurately with zero fault frames.
                             0x1E1: 0x953A}
# Measured per unit on the bench pair. NOT a per-node rule -- a re-addressed
# servo kept its centre across nodes 10->12 (FINDINGS 2026-08-28) -- so an
# actuator absent from this table has no known centre and the redundant
# cross-check is simply not computed for it. Never index this blind.
REDUNDANT_CENTER = {0x18A: 0x844A, 0x18B: 0x844B}

# Second encoding of position, inside the same actual-position frame:
# S = p + (q | 0x1000), where p, q are the two big-endian u16 fields.
# Stated on the raw sum rather than on rescaled degrees so Air and Fuel share
# one criterion. Band is the observed 0x8446-0x844F with margin either side.
# NOTE: this is a self-consistency check, not a second sensor -- physical
# independence of the two encodings is UNKNOWN (protocol_specification.md).
REDUNDANT_SUM_BAND = (0x8440, 0x8455)


def redundant_sum(data: bytes) -> int:
    # Masked: a status bit in the position word would otherwise throw S far out
    # of band and fail H8 on a frame that is actually fine. No-op on all 165,062
    # validated frames.
    return ((int.from_bytes(data[0:2], "big") & POSITION_FIELD_MASK)
            + (int.from_bytes(data[2:4], "big") | 0x1000))


# Physical travel of an SQM45 on this bench. Anything outside this is not a
# position, whatever the bytes say.
ANGLE_PLAUSIBLE_RANGE = (-5.0, 95.0)

# Measured 2026-08-21 over ALL 201 captures: in 165,062 actuator frames the top
# two bits of the position word are zero 100.0000% of the time -- never once set
# while a real LMV51 drove the bus. A frame with them set is therefore outside
# every envelope this decoder was validated against, and decoding it naively
# yields ~4915 deg from an actuator that is physically sitting still.
#
# Do NOT "fix" such a frame by masking the bits off and using the remainder.
# Masking does recover a plausible-looking angle, but the byte-7 check word then
# fails under every candidate polynomial, so there is no evidence the rest of
# the frame is intact. Treat the whole frame as untrustworthy and drop it.
POSITION_FLAG_BITS = 0xC0

# The position word is NOT a bare u16. Measured 2026-08-22 on this bench with
# both units, and cross-checked against all 201 captures:
#
#   bits 12-13  always 0b11 in normal travel (0x3064 = 0 deg .. 0x33E8 = 90 deg)
#   bit  11     STATUS. Set transiently during rapid/interrupted moves. The
#               frame still passes the NORMAL check word (residue 0x31), and the
#               masked position is continuous with its neighbours to within one
#               sample-step -- so the position is GOOD and only the flag is extra.
#   bits 14-15  FAULT. Raised when the actuator rejects our target frames
#               (proven: 0/123 frames flagged with valid targets, 117/121 with a
#               deliberately wrong XOR word, clearing again within ~2 s of valid
#               targets resuming). These frames use a DIFFERENT check-word
#               residue, 0x3D, and their position field is NOT trustworthy --
#               during a sustained fault it ranged to -1190 deg.
#
# Decoding the word naively turns bit 11 into ~245 deg and bits 14-15 into
# ~4915 deg, which is what put impossible spikes on the dashboard chart while
# the shaft was physically fine.
#
# Masking is a NO-OP on every frame this decoder was validated against: applied
# to all 165,062 LMV-driven actuator frames, 0 changed.
POSITION_FIELD_MASK = 0x37FF     # keep position, drop bits 15, 14 and 11
POSITION_STATUS_BITS = 0xC800    # every non-position bit
POSITION_FAULT_BITS = 0xC000     # the subset that invalidates the position

ACTUAL_CHECK_RESIDUE = 0x31        # normal frames
ACTUAL_CHECK_RESIDUE_FAULT = 0x3D  # frames carrying POSITION_FAULT_BITS


def position_status_bits(data: bytes) -> int:
    """Non-position bits of the actual-position word (0 when healthy)."""
    return int.from_bytes(data[0:2], "big") & POSITION_STATUS_BITS


def actual_frame_trustworthy(frame: "CANFrame", event: "DecodeEvent") -> bool:
    """May this actual-position frame be used as a MEASUREMENT?

    Deliberately stricter than "did it decode". A frame that fails here is not
    necessarily a bus fault -- it must still be counted for link-quality
    statistics -- but its angle must never reach a gauge, a transmitted target,
    or a health-check statistic.
    """
    if frame.can_id not in ACTUAL_IDS or len(frame.data) != 8:
        return False
    # Bit 11 alone is a status flag on an otherwise good frame -- the position
    # is continuous and the check word validates, so it stays usable. Only the
    # fault bits invalidate the measurement.
    if int.from_bytes(frame.data[0:2], "big") & POSITION_FAULT_BITS:
        return False
    if event is not None and event.candidate_integrity_ok is False:
        return False
    if event is None or event.angle_deg is None:
        return False
    lo, hi = ANGLE_PLAUSIBLE_RANGE
    return lo <= event.angle_deg <= hi


STATE_HOLD = 0x0000   # "no command active" -- actuator holds position
STATE_MOVE = 0x9E00   # "drive to this target"

REPLAY_IDS = {0x080, 0x194, 0x2A1}          # check word unsolved -> replay verbatim
# Encoding is deliberately narrower than decoding: only addresses whose
# constant was observed driving real hardware may be SYNTHESISED, because a
# wrong constant here is transmitted onto a bus. roster.commandable() gates
# the same decision at the engine level.
SYNTH_IDS = {0x181: 0x94BA, 0x1A1: 0x943A, 0x1C1: 0x95BA,
             # Predicted from the XOR rule then CONFIRMED on hardware
             # 2026-08-28 -- see TARGET_INTEGRITY_CONSTANT above.
             0x1E1: 0x953A}


@dataclass(frozen=True)
class CANFrame:
    received_monotonic: float
    can_id: int
    dlc: int
    data: bytes
    raw_line: str
    extended: bool = False
    remote: bool = False


@dataclass
class DecodeEvent:
    kind: str
    role: str
    can_id: int
    timestamp: float
    raw_hex: str
    angle_deg: float | None = None
    integrity_ok: bool | None = None
    state_word: int | None = None
    state_label: str = "UNKNOWN"
    moving: bool | None = None
    counter: int | None = None
    redundant_deg: float | None = None
    redundancy_error_deg: float | None = None
    marker: int | None = None
    candidate_integrity_ok: bool | None = None
    status_bits: int = 0
    warning: str = ""


def crc8_msb(data: bytes, poly: int = 0x17, init: int = 0) -> int:
    crc = init & 0xFF
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ poly) & 0xFF if (crc & 0x80) else (crc << 1) & 0xFF
    return crc


def state_word_label(word: int) -> str:
    return {
        0x0000: "BASELINE / OTHER",
        0x9E00: "MOTION / TRANSITION candidate",
        0x8A00: "SYSTEM TEST / MISSING NODE candidate",
    }.get(word, f"UNKNOWN 0x{word:04X}")


def decode_position_frame(frame: CANFrame) -> DecodeEvent | None:
    """Decode a confirmed position family; return None for unrelated IDs."""
    if frame.can_id in TARGET_IDS:
        if frame.dlc != 6 or len(frame.data) != 6:
            return DecodeEvent(
                kind="target", role=TARGET_IDS[frame.can_id], can_id=frame.can_id,
                timestamp=frame.received_monotonic, raw_hex=frame.data.hex().upper(),
                warning=f"wrong DLC {frame.dlc}; expected 6",
            )
        w0 = int.from_bytes(frame.data[0:2], "big")
        w1 = int.from_bytes(frame.data[2:4], "big")
        w2 = int.from_bytes(frame.data[4:6], "big")
        expected = (((w0 << 1) & 0xFFFF) ^ w1 ^ TARGET_INTEGRITY_CONSTANT[frame.can_id]) & 0xFFFF
        integrity_ok = w2 == expected
        missing_signature = frame.data[0:2] == b"\xFF\xA5"
        angle = None if missing_signature or not integrity_ok else (-0.1 * w0 + 6543.5)
        warnings = []
        if missing_signature:
            warnings.append("FFA5 missing/unavailable actuator signature")
        if not integrity_ok:
            warnings.append(f"target XOR word mismatch: got 0x{w2:04X}, expected 0x{expected:04X}")
        if angle is not None and not -5.0 <= angle <= 95.0:
            warnings.append(f"decoded target outside expected bench range: {angle:.1f} deg")
        return DecodeEvent(
            kind="target", role=TARGET_IDS[frame.can_id], can_id=frame.can_id,
            timestamp=frame.received_monotonic, raw_hex=frame.data.hex().upper(),
            angle_deg=angle, integrity_ok=integrity_ok, state_word=w1,
            state_label=state_word_label(w1), warning="; ".join(warnings),
        )

    if frame.can_id in ACTUAL_IDS:
        if frame.dlc != 8 or len(frame.data) != 8:
            return DecodeEvent(
                kind="actual", role=ACTUAL_IDS[frame.can_id], can_id=frame.can_id,
                timestamp=frame.received_monotonic, raw_hex=frame.data.hex().upper(),
                warning=f"wrong DLC {frame.dlc}; expected 8",
            )
        b = frame.data
        p_raw = int.from_bytes(b[0:2], "big")
        status_bits = p_raw & POSITION_STATUS_BITS
        p = p_raw & POSITION_FIELD_MASK
        q = int.from_bytes(b[2:4], "big")
        angle = 0.1 * p - 1238.8
        marker = b[4]
        warnings = []
        if marker == 0x5A:
            logical_b6 = b[6]
        elif marker == 0xA5:
            logical_b6 = (~b[6]) & 0xFF
        else:
            logical_b6 = None
            warnings.append(f"unknown marker 0x{marker:02X}; status/integrity not decoded")
        moving = bool(logical_b6 & 0x20) if logical_b6 is not None else None
        counter = b[5]
        redundant = None
        discrepancy = None
        candidate_integrity_ok = None
        if logical_b6 is not None:
            q_pos = q | 0x1000
            centre = REDUNDANT_CENTER.get(frame.can_id)
            if centre is not None:
                secondary_raw = centre - q_pos
                redundant = 0.1 * secondary_raw - 1238.8
                discrepancy = redundant - angle
            inverse_moving = (q & 0x1000) == 0
            if inverse_moving != moving:
                warnings.append("redundant movement bit disagrees")
            crc = crc8_msb(bytes([frame.can_id & 0xFF, b[0], b[1], b[2], b[3], logical_b6]))
            residue = (ACTUAL_CHECK_RESIDUE_FAULT if status_bits & POSITION_FAULT_BITS
                       else ACTUAL_CHECK_RESIDUE)
            candidate_integrity_ok = b[7] == (b[5] ^ crc ^ residue)
            if not candidate_integrity_ok:
                warnings.append("actuator CRC-family candidate mismatch")
        if status_bits & POSITION_FAULT_BITS:
            warnings.append(f"actuator FAULT bits 0x{status_bits:04X} set -- position not trustworthy")
        elif status_bits:
            warnings.append(f"actuator status bit 0x{status_bits:04X} set (position still valid)")
        if not -5.0 <= angle <= 95.0:
            warnings.append(f"decoded actual outside expected bench range: {angle:.1f} deg")
        if discrepancy is not None and abs(discrepancy) > 0.5:
            warnings.append(f"primary/redundant discrepancy {discrepancy:+.1f} deg")
        return DecodeEvent(
            kind="actual", role=ACTUAL_IDS[frame.can_id], can_id=frame.can_id,
            timestamp=frame.received_monotonic, raw_hex=frame.data.hex().upper(),
            angle_deg=angle, marker=marker, moving=moving, counter=counter,
            redundant_deg=redundant, redundancy_error_deg=discrepancy,
            candidate_integrity_ok=candidate_integrity_ok, status_bits=status_bits,
            warning="; ".join(warnings),
        )
    return None


def encode_target(angle_deg: float, can_id: int, state: int) -> str:
    """Synthesise an LMV target frame. The XOR word is confirmed and reproduces
    every observed payload, in both the hold and the move state."""
    raw = max(0, min(0xFFFF, int(round((6543.5 - angle_deg) / 0.1))))
    check = (((raw << 1) & 0xFFFF) ^ state ^ SYNTH_IDS[can_id]) & 0xFFFF
    return (raw.to_bytes(2, "big") + state.to_bytes(2, "big")
            + check.to_bytes(2, "big")).hex().upper()


def run_self_test() -> None:
    """Exercise decode_position_frame directly -- no transport involved."""

    def frame(can_id: int, dlc: int, data_hex: str) -> CANFrame:
        return CANFrame(received_monotonic=0.0, can_id=can_id, dlc=dlc,
                         data=bytes.fromhex(data_hex), raw_line="",
                         extended=False, remote=False)

    samples = {
        "air_target_0": (frame(0x181, 6, "FF9B00006B8C"), 0.0),
        "fuel_target_50": (frame(0x1A1, 6, "FDA700006F74"), 50.0),
        "air_actual_0": (frame(0x18A, 8, "306453E65A45153F"), 0.0),
        "fuel_actual_50": (frame(0x18B, 8, "325851F45A841922"), 50.0),
    }
    for name, (f, expected_angle) in samples.items():
        event = decode_position_frame(f)
        assert event is not None and event.angle_deg is not None, name
        assert abs(event.angle_deg - expected_angle) < 1e-9, (name, event.angle_deg)
        if event.kind == "target":
            assert event.integrity_ok is True, name
        if event.kind == "actual":
            assert event.candidate_integrity_ok is True, name
    print("PASS: decode_position_frame on four confirmed position samples")


if __name__ == "__main__":
    run_self_test()
