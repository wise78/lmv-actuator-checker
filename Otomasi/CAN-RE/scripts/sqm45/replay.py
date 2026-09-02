"""Replay a recorded cyclic sequence and command a live SQM45.

Proven on the bench 2026-08-14: an actuator wakes in ~0.3 s and stays up
indefinitely under this transmit loop, with no LMV51 present.

The actuator needs LIVE CAN traffic to run -- cut it and the LED dies
instantly, exactly as when CAN is unplugged. There is no tolerance window,
so `transmit()` must never pause; see FINDINGS.md, 2026-08-14.

This module drives a `transport.CanTransport` -- it never opens a serial
port itself and never assumes tM-7565 ASCII framing.
"""

from __future__ import annotations

import csv
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

from .protocol import (ACTUAL_IDS, POSITION_FAULT_BITS, STATE_HOLD, STATE_MOVE,
                        SYNTH_IDS, actual_frame_trustworthy,
                        decode_position_frame, encode_target)

# Clearing the move state word is what STOPS the actuator, so whatever error
# remains at that instant is the error we then measure -- this tolerance
# therefore bounds healthcheck's MOVE_SETTLE_ERR_MAX and HYSTERESIS_MAX, and
# all three characterise OUR loop as much as the actuator.
#
# Measured on the bench 2026-08-21 over both units: BOTH park 1-2 LSB short
# of a commanded target (Air max 0.3, Fuel max 0.2 deg). At 0.1 deg a shaft
# settling at 2 LSB can never produce ARRIVED_STABLE_SAMPLES consecutive
# in-tolerance readings, so the move watchdog fired on a servo sitting
# still -- on BOTH sensor types, which is what identifies it as our
# parameter rather than an actuator or sensor fault.
#
# 0.3 stays safe against latching mid-traverse: at the measured 2.5 deg/s
# and 4.96 Hz the shaft covers ~0.5 deg per sample and crosses a 0.3 deg
# band in ~1 sample, far short of the 5 consecutive required.
ARRIVED_TOLERANCE_DEG = 0.3
ARRIVED_STABLE_SAMPLES = 5      # ~1 s at 4.96 Hz


@dataclass(frozen=True)
class Frame:
    t_ms: float
    can_id: int
    dlc: int
    data: str          # uppercase hex, no separators; empty for synthesised frames


def load_sequence(path: Path, keep: set[int]) -> list[Frame]:
    frames: list[Frame] = []
    with path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            can_id = int(row["can_id"], 16)
            if can_id not in keep:
                continue
            # Synthesised frames keep their recorded TIMING but their payload is
            # built at send time from the live command state.
            data = "" if can_id in SYNTH_IDS else row["data"]
            frames.append(Frame(float(row["t_ms"]), can_id, int(row["dlc"]), data))
    frames.sort(key=lambda f: f.t_ms)
    return frames


def sequence_span_ms(frames: list[Frame]) -> float:
    """The loop span transmit() would compute for this (un-injected) sequence."""
    return frames[-1].t_ms * len(frames) / max(1, len(frames) - 1)


def inject_synthetic_targets(frames: list[Frame], can_ids, *, after: int = 0x1A1,
                             offset_ms: float = 0.45) -> list[Frame]:
    """Add target frames the recording never contained, on the same cadence.

    The boot recording came off a two-servo bench, so it carries only 0x181 and
    0x1A1. A third or fourth actuator has a fully derived payload but no slot
    in the transmitted stream -- the right phone number and no phone. This
    clones the cadence of an existing target so the new identifier inherits
    real LMV timing rather than an invented schedule.

    offset_ms mirrors the measured 0.23-0.63 ms spacing between 0x181 and
    0x1A1: the controller fires its targets back-to-back, and sitting inside
    that same burst keeps each actuator's frame adjacent to the 0x194 tick it
    synchronises to. Payload is left empty because every injected identifier is
    in SYNTH_IDS and is therefore built at send time from live command state.

    The caller MUST take sequence_span_ms() before calling this and pass it to
    transmit(), or the loop boundary moves.
    """
    wanted = [c for c in can_ids if c not in {f.can_id for f in frames}]
    if not wanted:
        return frames
    template = [f for f in frames if f.can_id == after]
    if not template:
        raise ValueError(f"no 0x{after:03X} frames to clone cadence from")
    out = list(frames)
    for index, can_id in enumerate(sorted(wanted)):
        step = offset_ms * (index + 1)
        out.extend(Frame(f.t_ms + step, can_id, 6, "") for f in template)
    out.sort(key=lambda f: f.t_ms)
    return out


class Reader:
    """Tracks actuator presence and last-known position from frames a
    transport delivers. Owns no thread of its own -- wire on_frame /
    on_unparsed to whatever CanTransport.open() is in use.
    """

    def __init__(self):
        self.t0 = time.monotonic()
        self.lock = threading.Lock()
        self.rx_total = 0
        self.first_seen: dict[int, float] = {}
        self.last_seen: dict[int, float] = {}
        self.last_angle: float | None = None
        self.last_counter: int | None = None
        self.device_lines: list[str] = []
        self.rejected_frames = 0
        self.status_frames = 0        # bit-11 status, position still good
        self.fault_frames = 0         # bits 14-15, position rejected
        self.last_status_bits = 0

    def on_frame(self, frame) -> None:
        now = time.monotonic() - self.t0
        with self.lock:
            self.rx_total += 1
            if frame.can_id not in ACTUAL_IDS:
                return
            if frame.can_id not in self.first_seen:
                self.first_seen[frame.can_id] = now
            self.last_seen[frame.can_id] = now
            event = decode_position_frame(frame)
            # A frame whose position word carries the never-observed flag bits,
            # or whose angle is physically impossible, is counted but NEVER
            # allowed to become the tracked position -- decoding one naively
            # reports ~4915 deg for a shaft that is standing still.
            if event is not None and event.status_bits:
                self.last_status_bits = event.status_bits
                if event.status_bits & POSITION_FAULT_BITS:
                    self.fault_frames += 1
                else:
                    self.status_frames += 1
            if event is not None and not actual_frame_trustworthy(frame, event):
                self.rejected_frames += 1
                return
            if event is not None and event.angle_deg is not None:
                self.last_angle = round(event.angle_deg, 1)
                self.last_counter = event.counter

    def on_unparsed(self, raw_text: str, error: str) -> None:
        with self.lock:
            self.rx_total += 1
            if len(self.device_lines) < 40:
                self.device_lines.append(raw_text)
                # A converter complaint is the most important thing on screen.
                # Do not make the operator wait for the end-of-run summary.
                enc = getattr(sys.stdout, "encoding", None) or "ascii"
                msg = (f"\n  !! CONVERTER REPLIED: {raw_text!r}  "
                       "(non-CAN; '?' prefix is an ICP DAS error response)\n")
                # cp1252 consoles raise on non-ASCII (the ESP32 banner has
                # an em-dash); that would otherwise kill the RX thread.
                print(msg.encode(enc, "replace").decode(enc, "replace"), flush=True)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "rx_total": self.rx_total,
                "first_seen": dict(self.first_seen),
                "last_seen": dict(self.last_seen),
                "angle": self.last_angle,
                "counter": self.last_counter,
                "device_lines": list(self.device_lines),
                "rejected_frames": self.rejected_frames,
                "status_frames": self.status_frames,
                "fault_frames": self.fault_frames,
                "last_status_bits": self.last_status_bits,
            }


class Command:
    """Decides, per frame, what to put in the target payload.

    Mirrors the LMV: assert the move state until the actual reaches the target,
    then clear it. Holding 0x9E00 after arrival is not something the LMV does, so
    this does not do it either.
    """

    def __init__(self, mode: str, angle: float):
        self.mode = mode
        self.target = angle
        self.state = STATE_HOLD
        self.arrived_at: float | None = None
        self.moving = False
        self.pending_prompt = None
        self._stable = 0

    def begin_move(self) -> None:
        self.state = STATE_MOVE
        self.moving = True
        self._stable = 0
        # Clear the PREVIOUS move's arrival. Anything that asks "has it
        # arrived?" -- health-check H6, a UI, a test harness -- would
        # otherwise read a stale True the instant a second move starts and
        # conclude the new move finished before the shaft had turned.
        self.arrived_at = None

    def request_hold(self) -> None:
        self.state = STATE_HOLD
        self.moving = False
        self._stable = 0

    def update(self, actual: float | None, elapsed: float) -> None:
        """Latch arrival once the shaft is within tolerance for N samples.

        The epsilon is load-bearing: position is quantised to 0.1 deg and a
        1-LSB error evaluates to 0.10000000000000142, which is NOT <= 0.1, so
        without it a shaft parked exactly one LSB out never arrives and the
        move watchdog fires on a servo sitting still (bench, 2026-08-21).

        Latching on "the shaft stopped changing" instead was tried and
        REVERTED: the motor accelerates over the first samples, so the report
        repeats early in the traverse and arrival latched ~23 deg short.
        """
        if not self.moving or actual is None:
            return
        if abs(actual - self.target) <= ARRIVED_TOLERANCE_DEG + 1e-9:
            self._stable += 1
            if self._stable >= ARRIVED_STABLE_SAMPLES:
                self.state = STATE_HOLD
                self.moving = False
                self.arrived_at = elapsed
        else:
            self._stable = 0

    def payload(self, can_id: int) -> str:
        return encode_target(self.target, can_id, self.state)


class Prompt:
    """Ask the operator something WITHOUT stalling transmission.

    input() blocks, and a blocked transmit loop means an idle bus. The actuator
    needs live traffic, so the question is asked on its own thread and the loop
    keeps cycling while the answer is typed.
    """

    def __init__(self):
        self.answer: str | None = None
        self.done = threading.Event()
        self._asked = False

    def ask(self, text: str) -> None:
        if self._asked:
            return
        self._asked = True

        def run() -> None:
            try:
                self.answer = input(text).strip()
            except (EOFError, KeyboardInterrupt):
                self.answer = ""
            self.done.set()

        threading.Thread(target=run, daemon=True).start()


def _status(elapsed: float, duration: float, sent: int, reader: Reader,
            last_194: int | None, cmd: Command | None) -> None:
    s = reader.snapshot()
    alive = [cid for cid, t in s["last_seen"].items() if elapsed - t < 1.0]
    if alive:
        cid = alive[0]
        match = "=" if (s["counter"] is not None and last_194 is not None
                        and s["counter"] == last_194) else "~"
        state = f"0x{cid:03X} {s['angle']:6.1f}deg ctr{match}"
    elif s["first_seen"]:
        state = "actuator SILENT (was alive)"
    else:
        state = "actuator silent"
    cmdinfo = ""
    if cmd is not None:
        word = "MOVE" if cmd.state == STATE_MOVE else "hold"
        cmdinfo = f"  cmd {word} {cmd.target:5.1f}deg"
        if cmd.arrived_at is not None:
            cmdinfo += f"  ARRIVED T+{cmd.arrived_at:.1f}s"
    line = (f"  T+{elapsed:6.1f}/{duration:.0f}s TX{sent:6d} RX{s['rx_total']:5d}  "
            f"{state}{cmdinfo}")
    print(f"\r{line:<104}", end="", flush=True)


def transmit(transport, frames: list[Frame], duration_s: float, reader: Reader,
             cmd: Command, on_settled=None, stop_after: float | None = None,
             *, on_cycle=None, stop_event: threading.Event | None = None,
             quiet: bool = False, span_ms: float | None = None) -> int:
    """Transmit continuously via `transport`. The sequence is laid out so
    0x194 byte 5 -- the counter the actuator slaves to -- is continuous
    across the loop boundary at `span_ms`.

    If the process is ever delayed (disk, scheduler, a prompt), the schedule
    is re-based rather than fired off as a catch-up burst. Bursting hundreds
    of queued frames at once would flood the converter and the actuator --
    the one anomaly this bench has actually produced ('!DFAB', '?5').

    on_cycle(elapsed, cmd, reader), if given, fires once per frame after
    cmd.update(...) -- the hook a caller observes the run through without
    touching the timing loop. stop_event, if given, ends transmission early
    (checked alongside `deadline`), for a caller that cannot wait out
    --duration or send Ctrl-C. quiet suppresses every print() below -- a
    caller with its own on_cycle (a UI, an engine) does not want the CLI's
    console status line landing in its own stdout/log.
    """
    # The loop span is derived from the frame COUNT, so injecting synthetic
    # target frames would silently move the loop boundary -- and the sequence
    # is laid out precisely so 0x194 byte 5 stays continuous across it. A
    # shifted boundary puts a counter discontinuity in once per lap, which
    # presents as a dropout or a failed wake, not as an obvious bug. Callers
    # that inject frames therefore pass the ORIGINAL span; see
    # sequence_span_ms() and inject_synthetic_targets().
    if span_ms is None:
        span_ms = frames[-1].t_ms * len(frames) / max(1, len(frames) - 1)
    start = time.monotonic()
    deadline = start + duration_s
    # Absolute instant to stop transmitting, or None to transmit throughout.
    stop_at = start + stop_after if stop_after is not None else None
    skew = 0.0                      # accumulated re-base, seconds
    sent = 0
    last_status = 0.0
    lap = 0
    last_194: int | None = None
    settled_fired = False
    announced_stop = False
    warned_silent = False

    while time.monotonic() < deadline:
        if stop_event is not None and stop_event.is_set():
            return sent
        for frame in frames:
            due = start + skew + (lap * span_ms + frame.t_ms) / 1000.0
            if due >= deadline:
                return sent
            while True:
                if stop_event is not None and stop_event.is_set():
                    return sent
                remaining = due - time.monotonic()
                if remaining <= 0:
                    break
                time.sleep(min(remaining, 0.004))
            # Fell far behind: resume the cadence from now instead of bursting.
            lateness = -(due - time.monotonic())
            if lateness > 0.25:
                skew += lateness

            now = time.monotonic()
            elapsed = now - start

            # Recomputed every frame: an earlier version latched this decision at
            # T+0, when the actuator had not woken yet, and so never stopped at all.
            transmitting = (
                stop_at is None
                or now < stop_at
                or not reader.snapshot()["first_seen"]  # nothing to time out yet
            )
            if transmitting:
                body = cmd.payload(frame.can_id) if frame.can_id in SYNTH_IDS else frame.data
                transport.write_frame(frame.can_id, frame.dlc, body)
                if frame.can_id not in SYNTH_IDS and frame.can_id == 0x194 and frame.dlc >= 6:
                    last_194 = int(frame.data[10:12], 16)
                sent += 1
            elif not announced_stop:
                announced_stop = True
                if not quiet:
                    print(f"\n  TX STOPPED at T+{elapsed:.1f}s -- listening only. "
                          "Watching how long the actuator survives an idle bus.\n")

            cmd.update(reader.snapshot()["angle"], elapsed)
            if on_cycle is not None:
                on_cycle(elapsed, cmd, reader)
            if on_settled is not None and not settled_fired and elapsed >= 5.0:
                settled_fired = True
                on_settled(elapsed)
            if cmd.pending_prompt is not None and cmd.pending_prompt.done.is_set():
                answer, cmd.pending_prompt = cmd.pending_prompt.answer, None
                if answer == "MOVE":
                    cmd.begin_move()
                    if not quiet:
                        print("\n  commanding motion.\n")
                elif not quiet:
                    print("\n  holding; no motion commanded.\n")
            if (not warned_silent and elapsed > 10.0
                    and not reader.snapshot()["first_seen"]):
                warned_silent = True
                if not quiet:
                    print("\n  !! No actuator frame after 10 s of transmission.\n"
                          "     Ctrl-C and check: CAN bitrate 50k, actuator supply,\n"
                          "     ERR LED, termination ~60 ohm. Continuing anyway.\n",
                          flush=True)
            if now - last_status >= 0.5:
                last_status = now
                if not quiet:
                    _status(elapsed, duration_s, sent, reader, last_194, cmd)
        lap += 1
    return sent
