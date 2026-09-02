"""Synthesised target frames: does a 3rd/4th actuator actually get commanded?

The boot recording is from the two-servo bench and carries only 0x181/0x1A1.
Oil and aux had a validated payload and no slot in the transmitted stream.
This covers the injection that gives them one, and the two ways it could go
quietly wrong:

  * the loop boundary moving, because transmit() derives span_ms from the
    frame COUNT and the sequence is laid out so 0x194 byte 5 stays continuous
    across it -- a shifted boundary reads as a dropout, not as a bug;
  * a synthesised frame going out with the wrong payload, which on a real
    bench is an actuator that silently refuses to move.

Run: python sqm45/tests/test_target_injection.py
"""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2]))

from sqm45 import protocol, roster                                    # noqa: E402
from sqm45.engine import ACTIVE_IDS, COMMANDABLE_TARGET_IDS, BusEngine  # noqa: E402
from sqm45.replay import (inject_synthetic_targets, load_sequence,    # noqa: E402
                          sequence_span_ms)

SEQUENCE = Path(__file__).resolve().parents[1] / "data" / "boot_replay_sequence.csv"


class CollectingTransport:
    """Captures what would have gone on the wire. Never opens a port."""

    source_label = "test-collector"

    def __init__(self):
        self.sent: list[tuple[int, int, str]] = []
        self.lock = threading.Lock()
        self._on_frame = None

    def open(self, on_frame, on_unparsed):
        self._on_frame = on_frame

    def close(self):
        pass

    def write_frame(self, can_id, dlc, data_hex):
        with self.lock:
            self.sent.append((can_id, dlc, data_hex))

    def ids(self):
        with self.lock:
            return [c for c, _, _ in self.sent]

    def payloads(self, can_id):
        with self.lock:
            return [d for c, _, d in self.sent if c == can_id]


def test_loop_span_survives_injection():
    """The whole point of measuring span BEFORE injection."""
    frames = load_sequence(SEQUENCE, ACTIVE_IDS)
    before = sequence_span_ms(frames)
    injected = inject_synthetic_targets(frames, [0x1C1, 0x1E1])
    naive = sequence_span_ms(injected)

    assert len(injected) > len(frames), "injection added nothing"
    # The naive recomputation MUST differ -- that is the trap this guards.
    assert abs(naive - before) > 1e-6, (
        "recomputing span after injection happened to match; the test no "
        "longer proves anything")
    # ...and the engine must keep the original.
    eng = BusEngine(CollectingTransport(), sequence_path=SEQUENCE)
    _, span = eng._build_sequence()
    assert abs(span - before) < 1e-9, f"engine span {span} != original {before}"
    print(f"PASS: loop span preserved at {before:.3f} ms "
          f"(naive recompute would have shifted it to {naive:.3f} ms, "
          f"{naive - before:+.3f} ms)")


def test_injected_frames_keep_the_recorded_cadence():
    frames = load_sequence(SEQUENCE, ACTIVE_IDS)
    injected = inject_synthetic_targets(frames, [0x1C1])
    gas = [f.t_ms for f in injected if f.can_id == 0x1A1]
    oil = [f.t_ms for f in injected if f.can_id == 0x1C1]
    assert len(oil) == len(gas), f"{len(oil)} oil frames vs {len(gas)} gas"
    gaps = [o - g for g, o in zip(gas, oil)]
    assert all(0.0 < g < 1.0 for g in gaps), f"offsets out of burst: {gaps[:5]}"
    # cadence, not just count: consecutive oil frames must step like gas frames
    step_gas = [b - a for a, b in zip(gas, gas[1:])][:200]
    step_oil = [b - a for a, b in zip(oil, oil[1:])][:200]
    # Compared with tolerance, not equality: each oil stamp is a gas stamp plus
    # a constant, and that addition rounds differently at different magnitudes.
    worst = max(abs(a - b) for a, b in zip(step_gas, step_oil))
    assert worst < 1e-6, f"oil cadence diverges from the template by {worst} ms"
    print(f"PASS: {len(oil)} oil frames on the gas cadence, "
          f"offset {min(gaps):.2f}-{max(gaps):.2f} ms inside the target burst")


def test_engine_transmits_every_commandable_target():
    """End to end: run the real TX loop briefly and read the wire."""
    transport = CollectingTransport()
    eng = BusEngine(transport, sequence_path=SEQUENCE)
    eng.start("active")
    try:
        deadline = time.monotonic() + 6.0
        while time.monotonic() < deadline:
            if all(t in transport.ids() for t in COMMANDABLE_TARGET_IDS):
                break
            time.sleep(0.1)
    finally:
        eng.shutdown_bus()

    seen = set(transport.ids())
    for address in roster.addresses():
        tid = roster.target_id(address)
        if tid is None:
            continue
        if roster.commandable(address):
            assert tid in seen, (
                f"address {address} ({roster.label_for(address)}) is commandable "
                f"but 0x{tid:03X} never reached the wire")
        else:
            assert tid not in seen, (
                f"0x{tid:03X} transmitted for non-commandable address {address}")
    print("PASS: wire carries " + ", ".join(
        f"0x{t:03X}" for t in sorted(seen & COMMANDABLE_TARGET_IDS)))
    return transport


def test_synthesised_payloads_are_correct(transport):
    """A frame at the right time with the wrong bytes is an actuator that
    silently refuses to move -- check the bytes, not just the identifier."""
    for address in roster.addresses():
        tid = roster.target_id(address)
        if tid is None or not roster.commandable(address):
            continue
        payloads = transport.payloads(tid)
        assert payloads, f"no payload captured for 0x{tid:03X}"
        expected = protocol.encode_target(0.0, tid, protocol.STATE_HOLD)
        assert all(p == expected for p in payloads), (
            f"0x{tid:03X} payload {payloads[0]} != encode_target {expected}")
        # and it must round-trip back through the decoder
        frame = protocol.CANFrame(received_monotonic=0.0, can_id=tid, dlc=6,
                                  data=bytes.fromhex(payloads[0]), raw_line="",
                                  extended=False, remote=False)
        ev = protocol.decode_position_frame(frame)
        if tid in protocol.TARGET_INTEGRITY_CONSTANT:
            assert ev is not None and ev.integrity_ok, (
                f"0x{tid:03X} synthesised frame fails its own integrity check")
            assert abs(ev.angle_deg) < 0.05, f"0x{tid:03X} decoded {ev.angle_deg}"
        print(f"PASS: 0x{tid:03X} ({roster.label_for(address):>5}) payload {payloads[0]}"
              + ("  [decode-verified]" if tid in protocol.TARGET_INTEGRITY_CONSTANT
                 else "  [encode only -- constant unproven, decode table excludes it]"))


def test_oil_encoder_matches_a_real_captured_frame():
    """The one payload we can check against hardware we actually watched."""
    got = protocol.encode_target(90.0, 0x1C1, protocol.STATE_MOVE)
    assert got == "FC179E00F394", f"{got} != captured FC179E00F394"
    print("PASS: encode_target(90, 0x1C1, MOVE) == FC179E00F394 "
          "(byte-for-byte the frame the LMV sent on 2026-08-28)")


def test_addresses_without_a_scheme_are_never_transmitted():
    for address in (5, 6):
        assert roster.target_id(address) is None
        assert not roster.commandable(address)
    assert all(0x181 <= t <= 0x1FF for t in COMMANDABLE_TARGET_IDS), (
        f"a target outside the TPDO1 block leaked into ACTIVE_IDS: "
        f"{[hex(t) for t in COMMANDABLE_TARGET_IDS]}")
    print("PASS: addresses 5-6 have no target identifier and cannot be transmitted")


if __name__ == "__main__":
    test_loop_span_survives_injection()
    test_injected_frames_keep_the_recorded_cadence()
    tr = test_engine_transmits_every_commandable_target()
    test_synthesised_payloads_are_correct(tr)
    test_oil_encoder_matches_a_real_captured_frame()
    test_addresses_without_a_scheme_are_never_transmitted()
    print("\nall target-injection tests passed")
