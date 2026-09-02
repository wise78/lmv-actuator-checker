"""Bus census: ID-agnostic statistics over EVERY frame on the wire.

Why this exists
---------------
engine._on_frame() decodes only {0x181, 0x18A, 0x18B, 0x1A1} and drops
everything else on the floor. That was fine for a 2-servo bench. It is
actively dangerous anywhere else: a healthy actuator on 0x18C renders as
ABSENT, which is the single worst thing this instrument can misreport.

This module never assumes an ID map. It counts what is there, measures its
shape, and SCORES each identifier on how much it looks like an actuator
feedback frame -- so a bus with 2, 4 or 7 servos, SQM45 or SQM48, needs no
code change to be readable.

Discovery, not assumption
-------------------------
Everything the scorer uses is derived from evidence already in FINDINGS.md
and the 201-capture dataset, generalised off the hard-coded pair:

  node          = can_id - 0x180        (TPDO1 slot)
  node          = 9 + actuator address  (CONFIRMED on a live 4-servo LMV,
                                        2026-08-28: addresses 1-4 answered on
                                        nodes 10-13 / 0x18A-0x18D)
  check word    = b7 == b5 ^ crc8 ^ R   (crc8 already folds can_id, so the
                                        check word generalises to any ID for
                                        free -- R is the only open constant)

The redundant sum is a PER-UNIT constant, not a per-node one -- see the note
in _score(). It was briefly modelled as 0x8440+node and that is disproved.

R is NOT assumed here. The census accumulates the IMPLIED residue
(b7 ^ b5 ^ crc8) per identifier and reports the distribution. If one value
dominates, the frame carries a check word and we have just measured its
residue; if the known SQM45 value 0x31 dominates, the new servo is the same
protocol family. A flat distribution means it is not a check word at all.
That is the difference between "we decoded it" and "we assumed it".

The LMV scans for actuators it does not have
--------------------------------------------
Across all 201 bench captures the LMV51 emits SDO probes on 0x609 and
0x60C-0x60F that nothing ever answers, alongside 0x60A/0x60B which Air and
Fuel do answer on 0x58A/0x58B. So the controller itself publishes the
actuator address space -- nodes 9..15 -- every boot. node_map() turns that
into a servo inventory before a single position byte is decoded.

Threading
---------
on_frame() is called from the RX thread and must never raise: a dead reader
thread looks exactly like dead hardware (FINDINGS.md, 2026-08-19). The
caller wraps it, and everything here is kept allocation-light and O(1) per
frame. snapshot() takes the lock and builds the report; call it at a couple
of Hz, not per frame.
"""

from __future__ import annotations

import threading
import time
from collections import deque

from . import protocol

# -- CANopen-shaped identifier families -----------------------------------
# The LMV51 is not a conformant CANopen master, but its identifier ALLOCATION
# follows the standard's function-code layout, which is what lets us map an
# unknown ID to a node number at all.
SYNC_ID = 0x080
TIME_ID = 0x100
TPDO1_BASE = 0x180
TPDO2_BASE = 0x280
TPDO3_BASE = 0x380
TPDO4_BASE = 0x480
SDO_REPLY_BASE = 0x580      # device -> controller
SDO_REQUEST_BASE = 0x600    # controller -> device
HEARTBEAT_BASE = 0x700

# Measured: the LMV51 probes exactly these node numbers, every boot, in all
# 201 captures. This is the actuator address space, published by the
# controller itself. Kept as a range rather than a set so a plant LMV that
# scans wider still scores its extra slots.
ACTUATOR_NODE_LO = 9
ACTUATOR_NODE_HI = 15

# Deliberately wider than protocol.ANGLE_PLAUSIBLE_RANGE. That constant
# guards a MEASUREMENT; this one only has to recognise "these bytes move
# like an angle" on hardware we have never seen (SQM48, different travel
# limits, mid-stroke at connect).
ANGLE_RECOGNISE_RANGE = (-15.0, 105.0)

CYCLIC_RATE_RANGE = (3.0, 8.0)      # actuator feedback measured at 4.96 Hz
RATE_WINDOW = 48                     # timestamps kept per ID for the rate fit
MAX_TRACKED_VALUES = 24              # per-byte distinct-value cap
MAX_TRACKED_RESIDUES = 12

# Frames whose meaning is already established. Purely cosmetic -- scoring
# never consults this, so a known ID and an unknown one are judged the same
# way and a mislabelled entry cannot flatter a candidate.
KNOWN_IDS = {
    0x080: ("SYNC", "CANopen sync object, LMV cyclic tick"),
    0x100: ("TIME", "CANopen TIME slot; DLC 7, byte 6 increments"),
    0x181: ("AIR TARGET", "LMV -> air actuator setpoint + state word"),
    # Roles confirmed on a live 4-servo LMV, 2026-08-28, against both the
    # actuator LED blink codes and the AZL52 addressing menu. node = 9 + address.
    0x18A: ("AIR ACTUAL", "address 1, node 10 -- air actuator position"),
    0x18B: ("GAS ACTUAL", "address 2, node 11 -- gas actuator position"),
    0x18C: ("OIL ACTUAL", "address 3, node 12 -- oil actuator position"),
    0x18D: ("AUX ACTUAL", "address 4, node 13 -- auxiliary actuator position"),
    0x194: ("LMV CYCLIC", "carries the counter actuators synchronise to"),
    0x195: ("LMV CYCLIC", "second LMV cyclic channel"),
    0x1A1: ("FUEL TARGET", "LMV -> fuel actuator setpoint + state word"),
    0x2A1: ("LMV MUX", "4-slot multiplexed frame, mux in top 2 bits of byte 0"),
    0x2C1: ("LMV BOOT", "sporadic, boot/shutdown only"),
    0x581: ("SDO reply", "LMV service channel response"),
    0x601: ("SDO request", "LMV service channel request"),
}


def _u16(data: bytes, off: int) -> int:
    return (data[off] << 8) | data[off + 1]


class IdStats:
    """Everything measured about one identifier. O(1) update per frame."""

    __slots__ = ("can_id", "count", "first_seen", "last_seen", "dlcs",
                 "last_data", "prev_data", "byte_or", "byte_and",
                 "byte_values", "byte_incr", "byte_prev", "stamps",
                 "max_gap", "min_gap", "changed_at",
                 "pos_plausible", "pos_shape", "sum_in_band", "sum_centers",
                 "marker_ok", "check_total", "residues", "status_seen",
                 "dlc8_frames", "unparsed")

    def __init__(self, can_id: int, now: float):
        self.can_id = can_id
        self.count = 0
        self.first_seen = now
        self.last_seen = now
        self.changed_at = now
        self.dlcs: set[int] = set()
        self.last_data = b""
        self.prev_data = b""
        self.byte_or = [0] * 8
        self.byte_and = [0xFF] * 8
        self.byte_values: list[set[int]] = [set() for _ in range(8)]
        self.byte_incr = [0] * 8      # times byte[i] advanced by exactly +1
        self.byte_prev: list[int | None] = [None] * 8
        self.stamps: deque[float] = deque(maxlen=RATE_WINDOW)
        self.max_gap = 0.0
        self.min_gap = None
        # actuator-shape evidence
        self.pos_plausible = 0
        self.pos_shape = 0
        self.sum_in_band = 0
        self.sum_centers: dict[int, int] = {}
        self.marker_ok = 0
        self.check_total = 0
        self.residues: dict[int, int] = {}
        self.status_seen = 0
        self.dlc8_frames = 0
        self.unparsed = 0

    # -- update ----------------------------------------------------------

    def update(self, frame: protocol.CANFrame, now: float) -> None:
        if self.count:
            gap = now - self.last_seen
            if gap > self.max_gap:
                self.max_gap = gap
            if self.min_gap is None or gap < self.min_gap:
                self.min_gap = gap
        self.count += 1
        self.last_seen = now
        self.stamps.append(now)
        self.dlcs.add(frame.dlc)

        data = frame.data
        if data != self.last_data:
            self.prev_data = self.last_data
            self.changed_at = now
        self.last_data = data

        for i, byte in enumerate(data[:8]):
            self.byte_or[i] |= byte
            self.byte_and[i] &= byte
            values = self.byte_values[i]
            if len(values) < MAX_TRACKED_VALUES:
                values.add(byte)
            prev = self.byte_prev[i]
            if prev is not None and ((prev + 1) & 0xFF) == byte:
                self.byte_incr[i] += 1
            self.byte_prev[i] = byte

        if len(data) == 8:
            self.dlc8_frames += 1
            self._score_actuator_shape(data)

    def _score_actuator_shape(self, b: bytes) -> None:
        """Accumulate the fingerprint of an actuator feedback frame.

        Every test here is structural: none of them needs to know which ID
        this is, or that the ID belongs to an actuator at all. That is the
        point -- an unknown 0x18C must be judged by the same evidence that
        would confirm the known 0x18A.
        """
        p_raw = _u16(b, 0)
        p = p_raw & protocol.POSITION_FIELD_MASK
        angle = 0.1 * p - 1238.8
        lo, hi = ANGLE_RECOGNISE_RANGE
        if lo <= angle <= hi:
            self.pos_plausible += 1
        # bits 12-13 sit at 0b11 through the whole 0-90 deg travel
        if (p_raw & 0x3000) == 0x3000:
            self.pos_shape += 1
        if p_raw & protocol.POSITION_STATUS_BITS:
            self.status_seen += 1

        q = _u16(b, 2)
        s = p + (q | 0x1000)
        band_lo, band_hi = protocol.REDUNDANT_SUM_BAND
        if band_lo <= s <= band_hi:
            self.sum_in_band += 1
        if len(self.sum_centers) < MAX_TRACKED_VALUES or s in self.sum_centers:
            self.sum_centers[s] = self.sum_centers.get(s, 0) + 1

        marker = b[4]
        if marker == 0x5A:
            logical_b6 = b[6]
        elif marker == 0xA5:
            logical_b6 = (~b[6]) & 0xFF
        else:
            return
        self.marker_ok += 1
        # can_id is already folded into the CRC input, so this generalises to
        # any identifier with no new constant. The residue is DERIVED, never
        # assumed -- see the module docstring.
        crc = protocol.crc8_msb(bytes([self.can_id & 0xFF, b[0], b[1], b[2], b[3], logical_b6]))
        residue = b[7] ^ b[5] ^ crc
        self.check_total += 1
        if len(self.residues) < MAX_TRACKED_RESIDUES or residue in self.residues:
            self.residues[residue] = self.residues.get(residue, 0) + 1

    # -- derived ---------------------------------------------------------

    @property
    def rate_hz(self) -> float | None:
        if len(self.stamps) < 2:
            return None
        span = self.stamps[-1] - self.stamps[0]
        if span <= 0:
            return None
        return (len(self.stamps) - 1) / span

    def recent_max_gap(self) -> float | None:
        """Largest gap inside the rolling window, not since the session began.

        max_gap never resets, so on a long run it reports the worst gap ever
        seen -- including the silence before the bus came up. That number
        belongs in a report, not on a live readout: the ~810 ms dual dropout
        this project has been chasing is only meaningful as "is it happening
        NOW", and a stale session maximum reads exactly like a live one.
        """
        if len(self.stamps) < 2:
            return None
        return max(self.stamps[i] - self.stamps[i - 1] for i in range(1, len(self.stamps)))

    @property
    def node(self) -> int | None:
        """Node number if the ID sits in a CANopen-shaped function slot."""
        for base in (TPDO1_BASE, TPDO2_BASE, TPDO3_BASE, TPDO4_BASE,
                     SDO_REPLY_BASE, SDO_REQUEST_BASE, HEARTBEAT_BASE):
            if base < self.can_id <= base + 0x7F:
                return self.can_id - base
        return None

    def dominant_residue(self) -> tuple[int | None, float]:
        if not self.residues or not self.check_total:
            return None, 0.0
        value = max(self.residues, key=lambda k: self.residues[k])
        return value, self.residues[value] / self.check_total

    def dominant_center(self) -> tuple[int | None, float]:
        if not self.sum_centers or not self.dlc8_frames:
            return None, 0.0
        value = max(self.sum_centers, key=lambda k: self.sum_centers[k])
        return value, self.sum_centers[value] / self.dlc8_frames

    def toggled_bits(self) -> list[int]:
        """Per byte, the bits that were ever seen both set and clear.

        A byte reading 0x00 here is constant; 0xFF is fully exercised. This
        is what separates a counter (0xFF) from a flag byte (a few bits)
        from padding (0x00) without decoding anything.
        """
        return [self.byte_or[i] & (~self.byte_and[i] & 0xFF) for i in range(8)]


class BusCensus:
    """ID-agnostic accumulator over the whole bus, plus actuator scoring."""

    def __init__(self, bitrate: int = 50000):
        self.bitrate = bitrate
        self._lock = threading.Lock()
        self._ids: dict[int, IdStats] = {}
        self._t0 = time.monotonic()
        self.total_frames = 0
        self.total_bits = 0
        self.unparsed_lines = 0
        # Bus load is bits-on-the-wire over TIME ON THE WIRE, so it is
        # measured against the span of the frames themselves, not against
        # wall-clock uptime. Using time.monotonic() here reported 13576% on a
        # capture replayed faster than real time -- the arithmetic was right
        # and the clock was wrong.
        self._first_frame_t: float | None = None
        self._last_frame_t: float | None = None
        self._recent: deque[dict] = deque(maxlen=600)
        self._stamps: deque[float] = deque(maxlen=256)
        # Monotonic per-frame sequence so a live stream can ask for "only
        # what I have not seen". Without it the browser re-receives the whole
        # ring buffer several times a second and the raw stream costs more
        # bandwidth than the bus it is watching.
        self._seq = 0

    # -- ingest ----------------------------------------------------------

    def on_frame(self, frame: protocol.CANFrame) -> None:
        now = frame.received_monotonic
        with self._lock:
            stats = self._ids.get(frame.can_id)
            if stats is None:
                stats = IdStats(frame.can_id, now)
                self._ids[frame.can_id] = stats
            stats.update(frame, now)
            self.total_frames += 1
            if self._first_frame_t is None:
                self._first_frame_t = now
            self._last_frame_t = now
            # Classical CAN standard frame: 47 bits of overhead + payload,
            # ~15% average bit stuffing. Approximate by construction -- it is
            # a load indicator, not a conformance measurement.
            self.total_bits += int((47 + 8 * frame.dlc) * 1.15)
            self._stamps.append(now)
            self._seq += 1
            self._recent.append({
                "seq": self._seq,
                "t": now - self._t0,
                "can_id": frame.can_id,
                "dlc": frame.dlc,
                "hex": frame.data.hex().upper(),
                "ext": frame.extended,
                "rtr": frame.remote,
            })

    def note_unparsed(self) -> None:
        with self._lock:
            self.unparsed_lines += 1

    def reset(self) -> None:
        with self._lock:
            self._ids.clear()
            self._recent.clear()
            self._stamps.clear()
            self._t0 = time.monotonic()
            self.total_frames = 0
            self.total_bits = 0
            self.unparsed_lines = 0
            self._seq = 0
            self._first_frame_t = None
            self._last_frame_t = None

    # -- node inventory --------------------------------------------------

    def _node_map(self, ids: dict[int, IdStats]) -> list[dict]:
        """Which node numbers this bus talks to, and how.

        `answered` is the strong signal: a reply on 0x580+n proves a device
        with that address is powered and on the wire, whatever its PDOs do.
        `probed without answer` is the controller telling us which addresses
        it expects -- the actuator address space, for free.
        """
        nodes: dict[int, dict] = {}

        def slot(number: int) -> dict:
            entry = nodes.get(number)
            if entry is None:
                entry = {"node": number, "probed": 0, "answered": 0,
                         "pdo_ids": [], "heartbeat": 0}
                nodes[number] = entry
            return entry

        for can_id, stats in ids.items():
            if SDO_REQUEST_BASE < can_id <= SDO_REQUEST_BASE + 0x7F:
                slot(can_id - SDO_REQUEST_BASE)["probed"] += stats.count
            elif SDO_REPLY_BASE < can_id <= SDO_REPLY_BASE + 0x7F:
                slot(can_id - SDO_REPLY_BASE)["answered"] += stats.count
            elif HEARTBEAT_BASE < can_id <= HEARTBEAT_BASE + 0x7F:
                slot(can_id - HEARTBEAT_BASE)["heartbeat"] += stats.count
            else:
                for base in (TPDO1_BASE, TPDO2_BASE, TPDO3_BASE, TPDO4_BASE):
                    if base < can_id <= base + 0x7F:
                        slot(can_id - base)["pdo_ids"].append(f"0x{can_id:03X}")
                        break

        out = []
        for entry in nodes.values():
            entry["pdo_ids"].sort()
            entry["in_actuator_range"] = ACTUATOR_NODE_LO <= entry["node"] <= ACTUATOR_NODE_HI
            if entry["answered"]:
                entry["verdict"] = "present"
            elif entry["probed"]:
                entry["verdict"] = "probed, silent"
            elif entry["pdo_ids"]:
                entry["verdict"] = "transmitting"
            else:
                entry["verdict"] = "seen"
            out.append(entry)
        out.sort(key=lambda e: e["node"])
        return out

    # -- actuator scoring -------------------------------------------------

    def _score(self, stats: IdStats, ids: dict[int, IdStats]) -> dict:
        """How much does this identifier look like actuator feedback?

        Returns a score, a tier, and the EVIDENCE LIST -- because a number
        alone is not something a technician can check or disagree with. Each
        entry says what was tested, what came back, and whether it counted.
        """
        evidence: list[dict] = []
        score = 0

        def add(ok: bool, points: int, text: str) -> None:
            nonlocal score
            if ok:
                score += points
            evidence.append({"ok": bool(ok), "points": points if ok else 0, "text": text})

        can_id = stats.can_id
        in_tpdo1 = TPDO1_BASE < can_id <= TPDO1_BASE + 0x7F
        node = can_id - TPDO1_BASE if in_tpdo1 else None

        if in_tpdo1 and ACTUATOR_NODE_LO <= node <= ACTUATOR_NODE_HI:
            add(True, 20, f"TPDO1 slot, node {node} inside the LMV's actuator scan range "
                          f"({ACTUATOR_NODE_LO}-{ACTUATOR_NODE_HI})")
        elif in_tpdo1:
            add(True, 8, f"TPDO1 slot, node {node} (outside the {ACTUATOR_NODE_LO}-"
                         f"{ACTUATOR_NODE_HI} range the LMV scans)")
        else:
            add(False, 20, "identifier is not in a TPDO1 slot (0x181-0x1FF)")

        if node is not None:
            replied = ids.get(SDO_REPLY_BASE + node)
            probed = ids.get(SDO_REQUEST_BASE + node)
            add(replied is not None, 15,
                f"node {node} answers SDO on 0x{SDO_REPLY_BASE + node:03X}"
                if replied else
                f"no SDO reply seen on 0x{SDO_REPLY_BASE + node:03X}")
            add(probed is not None, 8,
                f"LMV probes node {node} on 0x{SDO_REQUEST_BASE + node:03X}"
                if probed else
                f"LMV never probes 0x{SDO_REQUEST_BASE + node:03X}")

        add(stats.dlcs == {8}, 7, f"DLC {sorted(stats.dlcs)} "
                                  f"({'always 8, as actuator feedback is' if stats.dlcs == {8} else 'expected always 8'})")

        rate = stats.rate_hz
        lo, hi = CYCLIC_RATE_RANGE
        add(rate is not None and lo <= rate <= hi, 8,
            f"cyclic at {rate:.2f} Hz (actuator feedback measured 4.96 Hz)" if rate
            else "not enough frames to measure a rate")

        total = stats.dlc8_frames
        if not total:
            # No 8-byte frames means the position tests never ran, and a
            # skipped test must not read as a passed one. Without this, an
            # identifier could reach a "possible" tier on bus topology alone
            # -- which is how 0x181 (a 6-byte TARGET frame) scored 39 with
            # zero position evidence behind it.
            add(False, 12, "no 8-byte frames — actuator feedback is always DLC 8, "
                           "so no position test could run")
            score = min(score, 25)
        else:
            pos_pct = 100.0 * stats.pos_plausible / total
            shape_pct = 100.0 * stats.pos_shape / total
            band_pct = 100.0 * stats.sum_in_band / total
            add(pos_pct >= 95.0, 12,
                f"{pos_pct:.1f}% of frames decode to a plausible angle "
                f"({ANGLE_RECOGNISE_RANGE[0]:.0f}..{ANGLE_RECOGNISE_RANGE[1]:.0f} deg)")
            add(shape_pct >= 95.0, 8,
                f"{shape_pct:.1f}% carry the 0b11 marker in position bits 12-13")
            center, center_pct = stats.dominant_center()
            add(band_pct >= 95.0, 8,
                f"{band_pct:.1f}% have the redundant sum inside "
                f"0x{protocol.REDUNDANT_SUM_BAND[0]:04X}-0x{protocol.REDUNDANT_SUM_BAND[1]:04X}")
            # DISPROVED 2026-08-28 on a live 4-servo LMV: the redundant centre
            # is a per-UNIT constant, not a per-node one. One physical servo was
            # re-addressed from node 10 to node 12 mid-capture and its centre
            # stayed 0x844C throughout -- it did not follow the node. The old
            # 0x8440+node rule matched only 1 of 4 real units (measured 0x8448,
            # 0x8449, 0x844C, 0x844B on nodes 10-13); it had fit the two-servo
            # bench by coincidence.
            #
            # What survives, and is the stronger test anyway: a real unit holds
            # ONE dominant centre. Collision debris and non-actuator frames have
            # no stable centre at all.
            add(center is not None and center_pct >= 0.90, 10,
                f"redundant centre stable at 0x{center:04X} on {100 * center_pct:.1f}% of "
                f"frames (a per-unit constant)" if center is not None
                else "no stable redundant centre -- not a single actuator")

            marker_pct = 100.0 * stats.marker_ok / total
            add(marker_pct >= 95.0, 6,
                f"{marker_pct:.1f}% carry the 0x5A/0xA5 marker in byte 4")

            residue, residue_pct = stats.dominant_residue()
            if residue is not None and residue_pct >= 0.95:
                known = residue == protocol.ACTUAL_CHECK_RESIDUE
                add(True, 15,
                    f"check word validates on {100 * residue_pct:.1f}% of frames at residue "
                    f"0x{residue:02X}" + (" — the known SQM45 family value"
                                          if known else " — a NEW residue, not 0x31"))
            else:
                add(False, 15, "no single check-word residue dominates — byte 7 is "
                               "probably not a check word for this identifier")

        score = max(0, min(100, score))
        if score >= 78:
            tier = "confirmed"
        elif score >= 55:
            tier = "probable"
        elif score >= 32:
            tier = "possible"
        else:
            tier = "unlikely"
        return {"score": score, "tier": tier, "evidence": evidence, "node": node}

    # -- classification ---------------------------------------------------

    @staticmethod
    def _family(can_id: int) -> str:
        if can_id == SYNC_ID:
            return "sync"
        if can_id == TIME_ID:
            return "time"
        if TPDO1_BASE < can_id <= TPDO1_BASE + 0x7F:
            return "tpdo1"
        if TPDO2_BASE < can_id <= TPDO2_BASE + 0x7F:
            return "tpdo2"
        if TPDO3_BASE < can_id <= TPDO3_BASE + 0x7F:
            return "tpdo3"
        if TPDO4_BASE < can_id <= TPDO4_BASE + 0x7F:
            return "tpdo4"
        if SDO_REPLY_BASE < can_id <= SDO_REPLY_BASE + 0x7F:
            return "sdo-reply"
        if SDO_REQUEST_BASE < can_id <= SDO_REQUEST_BASE + 0x7F:
            return "sdo-request"
        if HEARTBEAT_BASE < can_id <= HEARTBEAT_BASE + 0x7F:
            return "heartbeat"
        return "other"

    # -- report -----------------------------------------------------------

    def snapshot(self, include_recent: bool = True, recent_since: int | None = None) -> dict:
        now = time.monotonic()
        with self._lock:
            ids = dict(self._ids)
            elapsed = max(1e-6, now - self._t0)
            total_frames = self.total_frames
            total_bits = self.total_bits
            unparsed = self.unparsed_lines
            seq = self._seq
            wire_span = 0.0
            if self._first_frame_t is not None and self._last_frame_t is not None:
                wire_span = self._last_frame_t - self._first_frame_t
            if not include_recent:
                recent = []
            elif recent_since is None:
                recent = list(self._recent)
            elif recent_since > seq:
                # The census was reset under the client: its cursor points
                # past the end. Resend the buffer rather than starving the
                # stream until the counter catches up again.
                recent = list(self._recent)
            else:
                recent = [f for f in self._recent if f["seq"] > recent_since]
            stamps = list(self._stamps)

            rows = []
            for can_id in sorted(ids):
                stats = ids[can_id]
                label, note = KNOWN_IDS.get(can_id, ("", ""))
                scored = self._score(stats, ids)
                rate = stats.rate_hz
                rows.append({
                    "can_id": can_id,
                    "id_hex": f"0x{can_id:03X}",
                    "label": label,
                    "note": note,
                    "family": self._family(can_id),
                    "node": scored["node"] if scored["node"] is not None else stats.node,
                    "count": stats.count,
                    "share_pct": 100.0 * stats.count / total_frames if total_frames else 0.0,
                    "rate_hz": rate,
                    "dlcs": sorted(stats.dlcs),
                    "first_seen": stats.first_seen - self._t0,
                    "last_seen": stats.last_seen - self._t0,
                    "age_s": now - stats.last_seen,
                    "max_gap_s": stats.max_gap,
                    "recent_max_gap_s": stats.recent_max_gap(),
                    "min_gap_s": stats.min_gap,
                    "last_hex": stats.last_data.hex().upper(),
                    "prev_hex": stats.prev_data.hex().upper(),
                    "static_since_s": now - stats.changed_at,
                    "toggled_bits": stats.toggled_bits(),
                    "byte_distinct": [len(v) for v in stats.byte_values],
                    "byte_incr": list(stats.byte_incr),
                    "status_frames": stats.status_seen,
                    "score": scored["score"],
                    "tier": scored["tier"],
                    "evidence": scored["evidence"],
                })

            nodes = self._node_map(ids)

        # Instantaneous frame rate over the sliding stamp window -- the
        # session average hides a bus that just went quiet, which is exactly
        # the condition an operator needs to notice immediately.
        inst_fps = None
        if len(stamps) >= 2 and stamps[-1] > stamps[0]:
            inst_fps = (len(stamps) - 1) / (stamps[-1] - stamps[0])

        candidates = [r for r in rows if r["tier"] in ("confirmed", "probable", "possible")]
        candidates.sort(key=lambda r: (-r["score"], r["can_id"]))
        load_span = wire_span if wire_span > 0.5 else elapsed
        return {
            "elapsed_s": elapsed,
            "wire_span_s": wire_span,
            "total_frames": total_frames,
            "unique_ids": len(rows),
            "unparsed_lines": unparsed,
            "frames_per_s": total_frames / load_span,
            "inst_frames_per_s": inst_fps,
            "bus_load_pct": min(100.0, 100.0 * total_bits / (self.bitrate * load_span)),
            "bitrate": self.bitrate,
            "rows": rows,
            "candidates": candidates,
            "nodes": nodes,
            "recent": recent,
            "seq": seq,
        }
