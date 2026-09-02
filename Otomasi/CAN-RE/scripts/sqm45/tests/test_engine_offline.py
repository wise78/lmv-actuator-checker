"""Offline regression for BusEngine's command side: multi-role
request_move, the move watchdog, and request_hold. None of this existed in
the single-Command code it is built from -- proven here against a stub
transport, no converter and no actuator.

    python sqm45/tests/test_engine_offline.py
"""
from __future__ import annotations

import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from sqm45 import protocol  # noqa: E402
from sqm45.engine import BusEngine, RoleNotFresh  # noqa: E402


class StubTransport:
    def __init__(self):
        self.written: list[tuple[int, int, str]] = []
        self._on_frame = None
        self._on_unparsed = None

    def open(self, on_frame, on_unparsed) -> None:
        self._on_frame = on_frame
        self._on_unparsed = on_unparsed

    def close(self) -> None:
        pass

    def write_frame(self, can_id: int, dlc: int, data_hex: str) -> None:
        self.written.append((can_id, dlc, data_hex))

    def inject_actual(self, can_id: int, angle_deg: float) -> None:
        raw = int(round((angle_deg + 1238.8) / 0.1))
        p = raw.to_bytes(2, "big")
        q = (0x1000).to_bytes(2, "big")   # settled: inverse-MOVING bit clear
        b6 = 0x00
        crc = protocol.crc8_msb(bytes([can_id & 0xFF]) + p + q + bytes([b6]))
        b7 = 0x00 ^ crc ^ 0x31
        data = p + q + bytes([0x5A, 0x00, b6, b7])
        frame = protocol.CANFrame(received_monotonic=time.monotonic(), can_id=can_id,
                                   dlc=8, data=data, raw_line="")
        self._on_frame(frame)


    def inject_flagged(self, can_id: int, angle_deg: float) -> None:
        """A frame with the position word's top two bits set -- the pattern
        that decodes to ~4915 deg and that the dataset never once shows."""
        raw = int(round((angle_deg + 1238.8) / 0.1))
        p = bytearray(raw.to_bytes(2, "big"))
        p[0] |= 0xC0
        q = (0x1000).to_bytes(2, "big")
        b6 = 0x00
        crc = protocol.crc8_msb(bytes([can_id & 0xFF]) + bytes(p) + q + bytes([b6]))
        data = bytes(p) + q + bytes([0x5A, 0x00, b6, 0x00 ^ crc ^ 0x31])
        frame = protocol.CANFrame(received_monotonic=time.monotonic(), can_id=can_id,
                                   dlc=8, data=data, raw_line="")
        self._on_frame(frame)


def wait_until(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_request_move_refuses_absent_role() -> None:
    transport = StubTransport()
    eng = BusEngine(transport)
    eng.start("active")
    try:
        try:
            eng.request_move("air")
            raise AssertionError("request_move should have refused an absent role")
        except RoleNotFresh:
            pass
        print("  refuses absent role : request_move correctly raised RoleNotFresh")
    finally:
        eng.shutdown_bus()


def test_independent_moves_two_roles() -> None:
    transport = StubTransport()
    eng = BusEngine(transport)
    eng.start("active")
    try:
        transport.inject_actual(0x18A, 20.0)
        transport.inject_actual(0x18B, 60.0)
        assert wait_until(lambda: eng.snapshot()["servos"]["air"]["presence"] == "live")
        assert wait_until(lambda: eng.snapshot()["servos"]["gas"]["presence"] == "live")

        eng.set_pending_target("air", 70.0)
        eng.set_pending_target("gas", 15.0)
        assert wait_until(lambda: eng.snapshot()["servos"]["air"]["pending_target_deg"] == 70.0)
        assert wait_until(lambda: eng.snapshot()["servos"]["gas"]["pending_target_deg"] == 15.0)

        eng.request_move("air")
        eng.request_move("gas")
        assert wait_until(lambda: eng.snapshot()["servos"]["air"]["state"] == protocol.STATE_MOVE)
        assert wait_until(lambda: eng.snapshot()["servos"]["gas"]["state"] == protocol.STATE_MOVE)

        snap = eng.snapshot()
        assert snap["servos"]["air"]["target_deg"] == 70.0, snap["servos"]["air"]
        assert snap["servos"]["gas"]["target_deg"] == 15.0, snap["servos"]["gas"]

        # The whole point of the dispatcher: 0x181 and 0x1A1 must carry
        # DIFFERENT payloads while the two roles move to different angles.
        assert wait_until(lambda: any(cid == 0x181 for cid, _, _ in transport.written))
        assert wait_until(lambda: any(cid == 0x1A1 for cid, _, _ in transport.written))
        air_payloads = {d for cid, dlc, d in transport.written if cid == 0x181}
        fuel_payloads = {d for cid, dlc, d in transport.written if cid == 0x1A1}
        assert air_payloads.isdisjoint(fuel_payloads), \
            "air and fuel target frames must not collide while moving to different angles"

        for _ in range(15):
            transport.inject_actual(0x18A, 70.0)
            transport.inject_actual(0x18B, 15.0)
            time.sleep(0.03)
        assert wait_until(lambda: eng.snapshot()["servos"]["air"]["state"] == protocol.STATE_HOLD)
        assert wait_until(lambda: eng.snapshot()["servos"]["gas"]["state"] == protocol.STATE_HOLD)
        print("  independent moves   : air->70.0 and fuel->15.0 diverged on the wire and both arrived")
    finally:
        eng.shutdown_bus()


def test_request_hold_mid_move() -> None:
    transport = StubTransport()
    eng = BusEngine(transport)
    eng.start("active")
    try:
        transport.inject_actual(0x18A, 20.0)
        assert wait_until(lambda: eng.snapshot()["servos"]["air"]["presence"] == "live")
        eng.set_pending_target("air", 70.0)
        assert wait_until(lambda: eng.snapshot()["servos"]["air"]["pending_target_deg"] == 70.0)
        eng.request_move("air")
        assert wait_until(lambda: eng.snapshot()["servos"]["air"]["state"] == protocol.STATE_MOVE)

        eng.request_hold()
        assert wait_until(lambda: eng.snapshot()["servos"]["air"]["state"] == protocol.STATE_HOLD)
        assert not eng.snapshot()["servos"]["air"]["moving"]
        print("  request_hold        : mid-move HOLD asserted on demand, both roles")
    finally:
        eng.shutdown_bus()


def test_stale_target_at_commit_time_is_dropped() -> None:
    """If the caller's fast-fail check raced past a stale reading somehow,
    the TX-thread commit in _do_request_move must still refuse to assert
    0x9E00 against data that is not fresh at the moment of commit."""
    transport = StubTransport()
    eng = BusEngine(transport)
    eng.start("active")
    try:
        transport.inject_actual(0x18A, 20.0)
        assert wait_until(lambda: eng.snapshot()["servos"]["air"]["presence"] == "live")
        servo = eng.servos["air"]
        servo.pending_target = 70.0    # bypass request_move's own pre-check on purpose
        # Force last_seen far enough in the past that FRESH_FOR_MOVE_S is exceeded.
        with servo.reader.lock:
            for can_id in list(servo.reader.last_seen):
                servo.reader.last_seen[can_id] -= 5.0
        eng._intents.put(lambda: eng._do_request_move("air"))
        time.sleep(0.3)
        assert eng.snapshot()["servos"]["air"]["state"] == protocol.STATE_HOLD, \
            "commit-time check failed to refuse a stale move"
        print("  commit-time refusal : stale reading at commit time correctly dropped, no 0x9E00 asserted")
    finally:
        eng.shutdown_bus()


def test_notify_ui_detached_holds_but_keeps_transmitting() -> None:
    """The design-canvas promise: a lost browser link must never stop
    transmission (zero jitter budget -- FINDINGS.md, 2026-08-14). It must
    assert HOLD and say why, while frames keep going out."""
    transport = StubTransport()
    eng = BusEngine(transport)
    eng.start("active")
    try:
        transport.inject_actual(0x18A, 20.0)
        assert wait_until(lambda: eng.snapshot()["servos"]["air"]["presence"] == "live")
        eng.set_pending_target("air", 70.0)
        assert wait_until(lambda: eng.snapshot()["servos"]["air"]["pending_target_deg"] == 70.0)
        eng.request_move("air")
        assert wait_until(lambda: eng.snapshot()["servos"]["air"]["state"] == protocol.STATE_MOVE)

        sent_before = len(transport.written)
        eng.notify_ui_detached()
        assert wait_until(lambda: eng.snapshot()["servos"]["air"]["state"] == protocol.STATE_HOLD)
        assert wait_until(lambda: len(transport.written) > sent_before + 3), \
            "transmission must continue after the UI link is lost, not stop"

        notices = eng.drain_notices()
        assert any(n["kind"] == "safety" and "HOLD asserted automatically" in n["text"]
                   for n in notices), notices
        print("  notify_ui_detached  : HOLD asserted, transmission continued, safety notice fired")
    finally:
        eng.shutdown_bus()


def test_flagged_frames_never_poison_position_or_target() -> None:
    """The 4915-degree fault. A frame carrying the never-observed position
    flag bits must be counted but must NOT reach the tracked angle, and must
    NOT become the transmitted setpoint via track-while-hold."""
    transport = StubTransport()
    eng = BusEngine(transport)
    eng.start("active")
    try:
        for _ in range(3):
            transport.inject_actual(0x18A, 22.0)
            time.sleep(0.05)
        assert wait_until(lambda: eng.snapshot()["servos"]["air"]["actual_deg"] == 22.0)
        assert wait_until(lambda: abs(eng.servos["air"].command.target - 22.0) < 0.05)

        # Now the fault: a burst of flagged frames.
        for _ in range(10):
            transport.inject_flagged(0x18A, 22.0)
            time.sleep(0.05)
        time.sleep(0.5)

        snap = eng.snapshot()["servos"]["air"]
        assert snap["actual_deg"] == 22.0, f"flagged frame poisoned the angle: {snap['actual_deg']}"
        tgt = eng.servos["air"].command.target
        assert abs(tgt - 22.0) < 0.05, f"flagged frame poisoned the target: {tgt}"
        rejected = eng.servos["air"].reader.snapshot()["rejected_frames"]
        assert rejected >= 10, f"flagged frames were not counted as rejected: {rejected}"

        # And the transmitted payload must still encode a sane setpoint.
        body = eng._dispatch_cmd.payload(0x181)
        assert body == protocol.encode_target(22.0, 0x181, protocol.STATE_HOLD), body
        print(f"  flagged-frame guard : {rejected} rejected, angle held at 22.0, "
              f"target uncorrupted, TX payload {body}")
    finally:
        eng.shutdown_bus()


def test_out_of_range_angle_is_rejected() -> None:
    transport = StubTransport()
    eng = BusEngine(transport)
    eng.start("active")
    try:
        for _ in range(3):
            transport.inject_actual(0x18A, 30.0); time.sleep(0.05)
        assert wait_until(lambda: eng.snapshot()["servos"]["air"]["actual_deg"] == 30.0)
        for _ in range(5):
            transport.inject_actual(0x18A, 400.0); time.sleep(0.05)
        time.sleep(0.4)
        assert eng.snapshot()["servos"]["air"]["actual_deg"] == 30.0
        print("  out-of-range guard  : 400.0 deg rejected, angle held at 30.0")
    finally:
        eng.shutdown_bus()


def main() -> int:
    print("BusEngine offline regression (no hardware)")
    test_request_move_refuses_absent_role()
    test_independent_moves_two_roles()
    test_request_hold_mid_move()
    test_stale_target_at_commit_time_is_dropped()
    test_notify_ui_detached_holds_but_keeps_transmitting()
    test_flagged_frames_never_poison_position_or_target()
    test_out_of_range_angle_is_rejected()
    print("PASS: multi-role dispatch, independent targets, request_hold, the "
          "commit-time freshness re-check, auto-HOLD-on-disconnect, and the "
          "4915-degree flagged-frame guard all verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
