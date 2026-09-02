"""Offline regression test for replay.transmit() -- the highest-consequence
function in this codebase (zero jitter budget: FINDINGS.md, 2026-08-14).

No hardware, no golden vectors: this exercises the loop itself against a
stub CanTransport, since the parity test only covers protocol.py/transport.py
and never runs transmit() or Command at all.

    python sqm45/tests/test_replay_loop.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from sqm45 import protocol, replay  # noqa: E402

SEQUENCE = SCRIPTS / "boot_replay_sequence.csv"
HOLD_IDS = {0x080, 0x194, 0x2A1, 0x181, 0x1A1}


class StubTransport:
    """Records every write; delivers injected frames straight to the
    callbacks transmit()/Reader would otherwise get from real hardware."""

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
        q = (0x1000).to_bytes(2, "big")  # inverse-MOVING bit clear -> settled
        b6 = 0x00
        crc = protocol.crc8_msb(bytes([can_id & 0xFF]) + p + q + bytes([b6]))
        b7 = 0x00 ^ crc ^ 0x31  # counter=0 folded in, matches candidate check
        data = p + q + bytes([0x5A, 0x00, b6, b7])
        frame = protocol.CANFrame(received_monotonic=time.monotonic(), can_id=can_id,
                                   dlc=8, data=data, raw_line="")
        self._on_frame(frame)


def test_basic_transmission() -> None:
    frames = replay.load_sequence(SEQUENCE, HOLD_IDS)
    assert frames, "sequence produced no frames for the hold ID set"
    ids_present = {f.can_id for f in frames}
    assert ids_present == HOLD_IDS, ids_present

    transport = StubTransport()
    reader = replay.Reader()
    transport.open(reader.on_frame, reader.on_unparsed)
    cmd = replay.Command("hold", 0.0)

    cycles = []
    sent = replay.transmit(transport, frames, duration_s=0.3, reader=reader, cmd=cmd,
                            on_cycle=lambda e, c, r: cycles.append(e))

    assert sent > 0, "nothing was transmitted"
    assert sent == len(transport.written), (sent, len(transport.written))
    assert len(cycles) == sent, "on_cycle must fire exactly once per frame"
    sent_ids = {cid for cid, _, _ in transport.written}
    assert sent_ids == HOLD_IDS, sent_ids
    for can_id, dlc, data_hex in transport.written:
        if can_id in protocol.SYNTH_IDS:
            assert len(data_hex) == 2 * dlc, (can_id, data_hex)
    print(f"  basic transmission : {sent:5d} frames sent, all {len(HOLD_IDS)} IDs present, "
          f"on_cycle fired {len(cycles)}x")


def test_move_arrival() -> None:
    """Feed a scripted approach to target through a stub actuator; the state
    word must clear back to HOLD only after ARRIVED_STABLE_SAMPLES settled
    readings, exactly as the LMV does."""
    frames = replay.load_sequence(SEQUENCE, HOLD_IDS)
    transport = StubTransport()
    reader = replay.Reader()
    transport.open(reader.on_frame, reader.on_unparsed)
    cmd = replay.Command("move", 50.0)
    cmd.begin_move()
    assert cmd.state == protocol.STATE_MOVE

    # Index 3 (49.95) is already within ARRIVED_TOLERANCE_DEG (0.1) of the
    # 50.0 target, so the settled streak is indices 3..7 -- exactly the 5
    # samples ARRIVED_STABLE_SAMPLES requires -- before arrival latches.
    # 49.6 was used here while the tolerance was 0.5; it is 0.4 out and no
    # longer counts as settled, which is the whole point of the change.
    approach = [10.0, 25.0, 40.0, 49.95, 50.0, 50.0, 50.0, 50.0, 50.0, 50.0]
    updates_used = 0
    for angle in approach:
        transport.inject_actual(0x18A, angle)
        cmd.update(reader.snapshot()["angle"], elapsed=0.0)
        updates_used += 1
        if cmd.arrived_at is not None:
            break

    assert cmd.state == protocol.STATE_HOLD, "state word did not clear on arrival"
    assert cmd.arrived_at is not None
    assert not cmd.moving
    assert updates_used == 8, f"expected arrival on the 8th update, got {updates_used}"
    print(f"  move arrival        : cleared to HOLD on update {updates_used}/10, "
          f"after {replay.ARRIVED_STABLE_SAMPLES} consecutive settled samples")


def test_stop_event_ends_transmission_promptly() -> None:
    frames = replay.load_sequence(SEQUENCE, HOLD_IDS)
    transport = StubTransport()
    reader = replay.Reader()
    transport.open(reader.on_frame, reader.on_unparsed)
    cmd = replay.Command("hold", 0.0)
    stop_event = threading.Event()

    def stopper():
        time.sleep(0.15)
        stop_event.set()

    threading.Thread(target=stopper, daemon=True).start()
    t0 = time.monotonic()
    sent = replay.transmit(transport, frames, duration_s=5.0, reader=reader, cmd=cmd,
                            stop_event=stop_event)
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0, f"stop_event did not end transmission promptly: {elapsed:.2f}s"
    assert sent > 0
    print(f"  stop_event          : ended in {elapsed:.2f}s against a 5.0s duration, "
          f"{sent} frames sent first")


def test_unparsed_reaches_reader() -> None:
    reader = replay.Reader()
    reader.on_unparsed("?5", "device error")
    s = reader.snapshot()
    assert s["rx_total"] == 1
    assert s["device_lines"] == ["?5"]
    print("  unparsed routing    : device reply reached Reader.device_lines")


def main() -> int:
    print("replay.transmit() offline regression (no hardware)")
    test_basic_transmission()
    test_move_arrival()
    test_stop_event_ends_transmission_promptly()
    test_unparsed_reaches_reader()
    print("PASS: replay loop, command arrival, stop_event, and unparsed routing all verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
