"""BusEngine: the one thing every UI (Flask, a headless script, a future
Qt view) drives instead of touching replay.transmit() directly.

Threading model: transmit() runs on a plain threading.Thread. Callers on
other threads (a SocketIO handler, a headless script) never mutate a
Command -- they call a BusEngine method, which enqueues an intent. The
intent queue is drained inside on_cycle, which transmit() calls on the TX
thread itself. That single-writer discipline is what makes Command's
missing lock correct by construction rather than merely unobserved
(see replay.Command).

Multi-servo note: replay.Reader and replay.Command are both single-role by
design -- proven that way on the bench, where only one Command ever existed.
BusEngine keeps one Reader and one Command PER ROLE (air/fuel) and routes
transmit()'s single required cmd/reader pair through a small dispatcher
(_RoleDispatchCommand) whose only load-bearing job is payload(can_id): pick
the right role's Command so 0x181 and 0x1A1 can carry different targets.
Everything else -- real per-role update(), arrival, the move watchdog --
happens in on_cycle, once per frame, after the dispatcher's inert update().
"""

from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path

from . import protocol, roster, savedialog
from .census import BusCensus
from .rawlog import RawLogWriter
from .replay import (Command, Reader, inject_synthetic_targets, load_sequence,
                      sequence_span_ms, transmit)

# The cyclic frames the recording provides verbatim, plus every target we can
# legitimately build. The recording itself only carries 0x181/0x1A1 -- the rest
# are injected onto the same cadence at start(), see _build_sequence().
BASE_CYCLIC_IDS = {0x080, 0x194, 0x2A1}
COMMANDABLE_TARGET_IDS = {
    roster.target_id(a) for a in roster.addresses() if roster.commandable(a)}
ACTIVE_IDS = BASE_CYCLIC_IDS | COMMANDABLE_TARGET_IDS

# Every commandable address needs a Command before the TX thread starts, since
# transmit() asks for a payload on each lap. Discovery adds monitor-only
# actuators on top of these at runtime.
SEEDED_ADDRESSES = tuple(a for a in roster.addresses() if roster.commandable(a))

STALE_S = 1.0            # dashboard gauge -> STALE
GONE_S = 10.0             # dashboard gauge -> removed
FRESH_FOR_MOVE_S = 1.5    # request_move refuses a role not heard this recently
MOVE_WATCHDOG_MIN_S = 15.0
MOVE_WATCHDOG_SPEED_DEG_S = 1.5   # matches the measured ~3 deg/s with margin

DEFAULT_SEQUENCE = Path(__file__).with_name("data").joinpath("boot_replay_sequence.csv")
# FALLBACK destination for a raw capture when no path was chosen -- an
# unattended --record session, or a host with no Save-as dialog. Deliberately in
# the operator's own Documents folder, NOT inside the application: this ships to
# technicians, and an install directory is both a surprising place to find your
# data and frequently not writable. Interactive recordings ask instead.
SNIFFER_DIR = savedialog.default_output_dir()


class EngineError(Exception):
    pass


class RoleNotFresh(EngineError):
    """request_move refused: this role's actual frame is stale or absent.
    STATE_HOLD on an absent channel commands nothing; 0x9E00 must never be
    asserted at nobody."""


@dataclass
class ServoState:
    role: str
    address: int
    command: Command = field(default_factory=lambda: Command("hold", 0.0))
    reader: Reader = field(default_factory=Reader)
    pending_target: float | None = None
    move_deadline: float | None = None
    appeared_at: float | None = None
    last_notice_state: str = "absent"   # absent | live | stale

    @property
    def actual_id(self) -> int:
        return roster.actual_id(self.address)

    @property
    def target_id(self) -> int:
        return roster.target_id(self.address)

    @property
    def label(self) -> str:
        return roster.label_for(self.address)

    @property
    def target_id_hex(self) -> str | None:
        return None if self.target_id is None else f"0x{self.target_id:03X}"

    def presence(self, now: float) -> str:
        s = self.reader.snapshot()
        last = s["last_seen"].get(self.actual_id)
        if last is None:
            return "absent"
        age = now - last
        if age >= GONE_S:
            return "absent"
        if age >= STALE_S:
            return "stale"
        return "live"


class _RoleDispatchCommand:
    """Satisfies transmit()'s single `cmd` parameter by routing payload(can_id)
    to the right role's real Command. update()/state/target here are inert --
    BusEngine.on_cycle does the real per-role update right after transmit()
    calls this object's update(), which is why this class does nothing in it.
    """

    def __init__(self, servos: dict[str, ServoState]):
        self._servos = servos
        self.state = protocol.STATE_HOLD
        self.target = 0.0
        self.arrived_at = None
        self.moving = False
        self.pending_prompt = None

    def payload(self, can_id: int) -> str:
        for servo in list(self._servos.values()):
            if servo.target_id == can_id:
                return servo.command.payload(can_id)
        return protocol.encode_target(0.0, can_id, protocol.STATE_HOLD)

    def update(self, actual, elapsed) -> None:
        pass


class BusEngine:
    """Owns one transport, one TX thread, and per-role Command/Reader state.

    snapshot() is lock-protected and latest-only. events is bounded and
    display-only (drop-oldest). notices is unbounded and never dropped.
    recorder is a plain list, never dropped -- the source of health-check
    statistics, which need the whole window, not a drain-once queue.
    """

    def __init__(self, transport, sequence_path: Path = DEFAULT_SEQUENCE):
        self.transport = transport
        self.sequence_path = sequence_path
        # Seeded with the addresses the TX sequence carries, then GROWN by
        # discovery in _on_frame(): any actuator answering on 0x18A-0x18F gets
        # a ServoState the first time it is heard. A bus with four or six
        # servos therefore needs no code change -- which is the whole point,
        # since a healthy actuator rendering as ABSENT is the worst thing this
        # instrument can misreport.
        self.servos: dict[str, ServoState] = {
            roster.name_for(a): ServoState(roster.name_for(a), a) for a in SEEDED_ADDRESSES}
        # ID-agnostic: counts EVERY frame, including the ones the decoder has
        # no map for. This is what lets a 4-servo plant bus be read by an
        # instrument built against a 2-servo bench.
        self.census = BusCensus()
        self.rawlog = RawLogWriter(SNIFFER_DIR)
        self._dispatch_cmd = _RoleDispatchCommand(self.servos)
        self._dispatch_reader = Reader()   # inert: transmit()'s automatic cmd.update() reads this

        self._mode = "idle"        # idle | listen | active
        self._t0 = time.monotonic()
        # Every Reader must measure against the SAME origin as presence() and
        # the event timeline, seeded and discovered alike -- see
        # _servo_for_address() for what a second clock origin does.
        for _servo in self.servos.values():
            _servo.reader.t0 = self._t0
        self._lock = threading.Lock()
        self._intents: queue.Queue = queue.Queue()
        self.events: queue.Queue = queue.Queue(maxsize=4096)
        self.notices: queue.Queue = queue.Queue()
        self._recorder: list[dict] = []
        self._recording = False
        self._procedure = None   # a healthcheck.HealthCheckRun, or None -- ticked from _tick()

        self._tx_thread: threading.Thread | None = None
        self._stop_event = threading.Event()

    @property
    def mode(self) -> str:
        return self._mode

    # -- roster ------------------------------------------------------------

    def _servo_for_address(self, address: int) -> ServoState:
        """The ServoState for this address, created on first sight.

        Called from the RX thread. dict insertion is atomic under the GIL and
        every reader iterates a copy, so no lock is taken on the hot path --
        the same single-writer reasoning the rest of this class uses.
        """
        role = roster.name_for(address)
        servo = self.servos.get(role)
        if servo is None:
            servo = ServoState(role, address)
            # Reader stamps last_seen against ITS OWN construction time. A servo
            # discovered mid-session therefore starts a second clock, and
            # presence() -- which compares against the ENGINE clock -- read every
            # freshly discovered actuator as ABSENT while its angle updated
            # normally. Share the one origin.
            servo.reader.t0 = self._t0
            self.servos[role] = servo
        return servo

    def resolve_role(self, role: str) -> str:
        """Canonical role slug, accepting the legacy 'fuel' alias."""
        return roster.ALIASES.get(role, role)

    def roster_view(self) -> list[dict]:
        """Every address this build knows about, and what may be done with it.

        Exposed so the UI never has to hard-code a servo list or re-derive the
        command rules -- it renders whatever this returns.
        """
        out = []
        for address in roster.addresses():
            role = roster.name_for(address)
            servo = self.servos.get(role)
            out.append({
                "address": address,
                "role": role,
                "label": roster.label_for(address),
                "color": roster.COLORS[role],
                "actual_id_hex": f"0x{roster.actual_id(address):03X}",
                # None for addresses past the channel field -- rendering a
                # plausible hex here is exactly how a bogus identifier ends up
                # transmitted by someone reading the UI.
                "target_id_hex": (None if roster.target_id(address) is None
                                  else f"0x{roster.target_id(address):03X}"),
                "node": roster.node_for(address),
                "target_confidence": roster.TARGET_CONFIDENCE[address],
                "experimental": roster.is_experimental(address),
                "commandable": roster.commandable(address) and roster.target_id(address) in ACTIVE_IDS,
                "discovered": servo is not None and servo.appeared_at is not None,
            })
        return out

    # -- lifecycle ---------------------------------------------------------

    def start(self, mode: str) -> None:
        """mode: 'listen' (receive-only, safe on a live bus) or 'active'
        (transmits the LMV51 identifiers -- isolated bench only)."""
        if self._mode != "idle":
            raise EngineError(f"already running in mode {self._mode!r}")
        if mode not in ("listen", "active"):
            raise EngineError(f"unknown mode {mode!r}")
        # An intent queued in a prior session (e.g. request_hold() called
        # while mode was 'listen', where nothing ever drains it) must not
        # silently apply on the next active session -- drop it here, not
        # carry it forward.
        while True:
            try:
                self._intents.get_nowait()
            except queue.Empty:
                break
        # A session that died mid-move leaves residue on each ServoState: a
        # moving flag, a 0x9E00 state word and -- worst -- a move_deadline
        # computed against the DEAD session's clock. Left alone, the next
        # session's first tick would "discover" an expired watchdog and
        # assert HOLD with a MOVE_TIMEOUT fault against a shaft that did
        # nothing wrong -- false evidence in exactly the log this instrument
        # exists to keep trustworthy. Reset it here, where no TX thread
        # exists to race (single-writer discipline still holds).
        for servo in self.servos.values():
            servo.command.request_hold()
            servo.pending_target = None
            servo.move_deadline = None
        # Parse the sequence BEFORE claiming the port: it is pure file
        # reading, and raising it after open() succeeded used to escape
        # uncaught (app.py catches only EngineError) with the port held,
        # _mode already live and NO TX thread.
        frames = None
        if mode == "active":
            try:
                frames, span_ms = self._build_sequence()
            except Exception as exc:
                raise EngineError(f"cannot load replay sequence: {exc}") from exc
        # Open BEFORE claiming the mode. Setting _mode first meant a failed
        # port open (someone else holding it, adapter unplugged) still left the
        # dashboard reporting "active" over a bus that was never opened -- the
        # single worst thing this UI can misreport.
        self._stop_event.clear()
        try:
            self.transport.open(self._on_frame, self._on_unparsed)
        except Exception as exc:
            self._mode = "idle"
            self._notice("fault", f"cannot open {getattr(self.transport, 'source_label', 'adapter')}: {exc}")
            raise EngineError(f"cannot open adapter: {exc}") from exc
        self._mode = mode
        self._notice("bus", f"mode {mode} started")
        if mode == "active":
            self._tx_thread = threading.Thread(
                target=self._run_transmit, args=(frames, span_ms), daemon=True, name="BusEngineTx")
            self._tx_thread.start()

    def shutdown_bus(self) -> None:
        """Stop transmitting entirely -- the actuator switches off. This is
        the dangerous control: press-and-hold in the UI, never a keystroke."""
        if self._procedure is not None and not self._procedure.finished:
            # The TX thread is about to stop, so nothing will ever tick this
            # procedure again -- abort it now rather than leave it frozen
            # mid-run with no verdict.
            self._procedure.abort(self, "bus shut down mid-run")
        self._stop_event.set()
        if self._tx_thread is not None and self._tx_thread.is_alive():
            self._tx_thread.join(timeout=3.0)
        self.transport.close()
        self._mode = "idle"
        # Close the raw log with the port. Left open it would keep a half
        # flushed file on disk claiming to be a recording of a bus that is
        # no longer being read.
        if self.rawlog.active:
            self.stop_raw_log()
        self._notice("bus", "bus shut down -- transmission stopped, actuator will be off")

    def request_hold(self) -> None:
        """Safe stop: assert HOLD on every role, keep transmitting."""
        self._intents.put(self._do_request_hold)

    def request_move(self, role: str, angle_deg: float | None = None) -> None:
        """Command a move. Pass the angle WITH the request wherever possible.

        set_pending_target() only queues an intent that the TX thread applies
        later, so a caller that set a target and immediately asked to move read
        pending_target as None here and had the move refused -- the operator saw
        a MOVE button that needed several clicks before anything happened, and
        no target at all if they never touched the slider. Carrying the angle
        removes the ordering dependency instead of papering over it with a
        delay.
        """
        role = self.resolve_role(role)
        if role not in self.servos:
            raise EngineError(f"unknown role {role!r}")
        servo = self.servos[role]
        self._require_commandable(servo)
        now = time.monotonic() - self._t0
        last = servo.reader.snapshot()["last_seen"].get(servo.actual_id)
        if last is None or (now - last) > FRESH_FOR_MOVE_S:
            raise RoleNotFresh(
                f"{role}: no actual frame within {FRESH_FOR_MOVE_S}s -- refusing to command a move")
        if angle_deg is None and servo.pending_target is None and not self._intents.qsize():
            # Nothing supplied and nothing in flight -- this one really is empty.
            raise EngineError(f"{role}: no target given -- move the setpoint first")
        target = None if angle_deg is None else max(0.0, min(90.0, float(angle_deg)))
        self._intents.put(lambda: self._do_request_move(role, target))

    def _require_commandable(self, servo: ServoState) -> None:
        """Refuse to command an address whose target frame we cannot prove.

        A 5th or 6th actuator is fully readable but its target identifier is
        an unverified extrapolation, and address 4's integrity constant is a
        fit with one free parameter. Transmitting either would put a guessed
        frame on a live bus -- exactly the mistake this project keeps its
        hypothesis/confirmed tiering to avoid.
        """
        if not roster.commandable(servo.address):
            raise EngineError(
                f"{servo.role}: address {servo.address} has no known target identifier "
                f"(the 2-bit channel field ends at address 4), so there is nothing to "
                f"transmit. Monitoring works; commanding does not exist for it.")
        if servo.target_id not in ACTIVE_IDS:
            raise EngineError(
                f"{servo.role}: target {servo.target_id_hex} is known but is not in the "
                f"transmitted sequence, so nothing would reach the actuator. Monitor only.")

    def set_pending_target(self, role: str, angle_deg: float) -> None:
        role = self.resolve_role(role)
        if role not in self.servos:
            raise EngineError(f"unknown role {role!r}")
        self._require_commandable(self.servos[role])
        angle_deg = max(0.0, min(90.0, angle_deg))   # the actuator's full travel
        self._intents.put(lambda: self._set_pending(role, angle_deg))

    def notify_ui_detached(self) -> None:
        """The browser/UI link is gone. Never stop transmitting for that --
        the actuator has a zero-jitter tolerance. Assert HOLD instead, so a
        lost socket degrades to 'holding position', not 'uncontrolled'."""
        self.request_hold()
        self._notice("safety", "UI link lost -- HOLD asserted automatically, transmission continues")

    # -- read side -----------------------------------------------------------

    def snapshot(self) -> dict:
        now = time.monotonic() - self._t0
        with self._lock:
            # "t" exports the ENGINE clock -- the same clock event payloads
            # use. Charts plot targets and actuals on one authoritative
            # timeline (real bus timing) instead of the browser's
            # performance.now(), which drifts with render jank.
            out = {"mode": self._mode, "t": now, "servos": {}}
            for role, servo in list(self.servos.items()):
                presence = servo.presence(now)
                s = servo.reader.snapshot()
                out["servos"][role] = {
                    "address": servo.address,
                    "label": servo.label,
                    "color": roster.COLORS[role],
                    "actual_id_hex": f"0x{servo.actual_id:03X}",
                    "target_id_hex": servo.target_id_hex,
                    "target_confidence": roster.TARGET_CONFIDENCE[servo.address],
                    "experimental": roster.is_experimental(servo.address),
                    "commandable": (roster.commandable(servo.address)
                                    and servo.target_id in ACTIVE_IDS),
                    "presence": presence,
                    "actual_deg": s["angle"],
                    "target_deg": servo.command.target,
                    "pending_target_deg": servo.pending_target,
                    "state": servo.command.state,
                    "moving": servo.command.moving,
                    "arrived_at": servo.command.arrived_at,
                    "counter": s["counter"],
                    "rx_total": s["rx_total"],
                    "status_frames": s.get("status_frames", 0),
                    "fault_frames": s.get("fault_frames", 0),
                    "rejected_frames": s.get("rejected_frames", 0),
                    "last_status_bits": s.get("last_status_bits", 0),
                }
            return out

    def drain_events(self, limit: int = 500) -> list:
        return self._drain(self.events, limit)

    def drain_notices(self, limit: int = 500) -> list:
        return self._drain(self.notices, limit)

    @staticmethod
    def _drain(q: queue.Queue, limit: int) -> list:
        out = []
        for _ in range(limit):
            try:
                out.append(q.get_nowait())
            except queue.Empty:
                break
        return out

    def start_recording(self) -> None:
        with self._lock:
            self._recorder.clear()
            self._recording = True

    def stop_recording(self) -> None:
        with self._lock:
            self._recording = False

    def recorded_samples(self) -> list[dict]:
        with self._lock:
            return list(self._recorder)

    def new_recorded_samples(self, cursor: int) -> tuple[list[dict], int]:
        """Samples appended since `cursor` -- pass back the returned cursor
        next call. Avoids a health-check tick re-copying the whole,
        ever-growing recorder every cycle; only the new tail is sliced."""
        with self._lock:
            n = len(self._recorder)
            new = self._recorder[cursor:n]
        return new, n

    # -- health-check procedure --------------------------------------------
    # A procedure (sqm45.healthcheck.HealthCheckRun) is attached/detached and
    # ticked with the same single-writer discipline as everything else here:
    # attach/detach/abort/ht-choice are queued intents drained inside _tick,
    # and tick() itself is called from _tick, all on the TX thread. Nothing
    # about a procedure is ever mutated from a SocketIO handler thread
    # directly.

    def attach_procedure(self, procedure) -> None:
        self._intents.put(lambda: self._do_attach_procedure(procedure))

    def detach_procedure(self) -> None:
        self._intents.put(self._do_detach_procedure)

    def abort_procedure(self, reason: str = "operator abort") -> None:
        def _run():
            if self._procedure is not None:
                self._procedure.abort(self, reason)
        self._intents.put(_run)

    def set_procedure_ht_choice(self, choice: str) -> None:
        def _run():
            if self._procedure is not None:
                self._procedure.set_ht_choice(choice)
        self._intents.put(_run)

    def procedure_status(self) -> dict | None:
        proc = self._procedure
        if proc is None:
            return None
        return proc.status(time.monotonic() - self._t0)

    def _do_attach_procedure(self, procedure) -> None:
        self._procedure = procedure
        now = time.monotonic() - self._t0
        procedure.start(now, self)

    def _do_detach_procedure(self) -> None:
        self._procedure = None

    # -- internals -------------------------------------------------------

    def _notice(self, kind: str, text: str) -> None:
        self.notices.put({"t": time.monotonic() - self._t0, "kind": kind, "text": text})

    def _push_event(self, item: dict) -> None:
        try:
            self.events.put_nowait(item)
        except queue.Full:
            try:
                self.events.get_nowait()   # drop-oldest
            except queue.Empty:
                pass
            try:
                self.events.put_nowait(item)
            except queue.Full:
                pass

    def _on_frame(self, frame: protocol.CANFrame) -> None:
        # Census and raw log come FIRST and are both fail-soft. Every frame
        # is evidence, including -- especially -- the ones falling through
        # to the decoder's `return None`. Neither may ever raise into the RX
        # thread: a dead reader looks exactly like dead hardware, which cost
        # this project a day already (FINDINGS.md, 2026-08-19).
        try:
            self.census.on_frame(frame)
        except Exception as exc:
            self._notice("fault", f"census error on 0x{frame.can_id:03X}: {exc}")
        self.rawlog.write(frame)

        address = roster.address_for_actual_id(frame.can_id)
        if address is not None:
            servo = self._servo_for_address(address)
            was_present = servo.reader.snapshot()["first_seen"].get(frame.can_id) is not None
            servo.reader.on_frame(frame)
            if not was_present:
                servo.appeared_at = time.monotonic() - self._t0
                extra = "" if roster.commandable(address) else " -- monitor only, target unverified"
                self._notice("presence",
                             f"{servo.label} actuator appeared "
                             f"(0x{frame.can_id:03X}, address {address}){extra}")
        event = protocol.decode_position_frame(frame)
        if event is not None:
            # redundant_sum/inverse_moving are NOT DecodeEvent fields on purpose --
            # DecodeEvent is held to the frozen golden vectors, and a new field
            # there would need a regeneration this doesn't need. Computed here
            # instead, straight from frame.data, only where decode_position_frame
            # itself would have (an 8-byte actual frame).
            extra = {}
            if event.kind == "actual":
                # Tag, don't drop. A frame that fails this is still real
                # evidence for link quality (H2/H8 count it), but its angle
                # must never enter a position statistic -- that is what put
                # 4915 deg and 204.8 deg swings into a health-check report.
                extra["trustworthy"] = protocol.actual_frame_trustworthy(frame, event)
                # H8 needs this to tell an actuator-marked transitional frame
                # from a genuine cross-check failure.
                extra["status_bits"] = event.status_bits
            if event.kind == "actual" and len(frame.data) == 8:
                s = protocol.redundant_sum(frame.data)
                q = int.from_bytes(frame.data[2:4], "big")
                extra["redundant_sum_hex"] = f"0x{s:04X}"
                extra["redundant_sum_in_band"] = protocol.REDUNDANT_SUM_BAND[0] <= s <= protocol.REDUNDANT_SUM_BAND[1]
                extra["inverse_moving"] = (q & 0x1000) == 0
            # protocol.py's role strings are decode-layer labels frozen with the
            # golden vectors ("fuel" for address 2). The UI keys everything by
            # roster name, so translate here rather than let two vocabularies
            # reach the browser and silently drop a series off the chart.
            ev_address = (roster.address_for_actual_id(frame.can_id)
                          or roster.address_for_target_id(frame.can_id))
            ev_role = roster.name_for(ev_address) if ev_address else event.role
            payload = {"t": time.monotonic() - self._t0, "can_id": frame.can_id, "dlc": frame.dlc,
                       "raw_hex": event.raw_hex,
                       "kind": event.kind, "role": ev_role, "angle_deg": event.angle_deg,
                       "moving": event.moving, "warning": event.warning,
                       "state_word": event.state_word, "counter": event.counter,
                       "marker": event.marker, "candidate_integrity_ok": event.candidate_integrity_ok,
                       "redundancy_error_deg": event.redundancy_error_deg, **extra}
            self._push_event(payload)
            with self._lock:
                if self._recording:
                    self._recorder.append(payload)

    def _on_unparsed(self, raw_text: str, error: str) -> None:
        try:
            self.census.note_unparsed()
        except Exception:
            pass
        self._notice("converter", f"{raw_text!r} ({error})")

    # -- bus census / raw sniffer ------------------------------------------

    def census_snapshot(self, include_recent: bool = True,
                        recent_since: int | None = None) -> dict:
        """Full ID-agnostic report. Call at a couple of Hz, not per frame."""
        snap = self.census.snapshot(include_recent=include_recent, recent_since=recent_since)
        snap["mode"] = self._mode
        snap["source"] = getattr(self.transport, "source_label", "unknown")
        snap["rawlog"] = self.rawlog.status()
        return snap

    def census_reset(self) -> None:
        self.census.reset()
        # The wall-clock anchor matters: after a reset the census reports
        # first_seen/elapsed relative to the NEW origin, while any raw CSV
        # recording alongside it keeps writing absolute epoch timestamps.
        # Without this line nothing on disk records where the two diverged,
        # and a report can no longer be lined up against its own evidence.
        stamp = time.strftime("%Y-%m-%d %H:%M:%S")
        self._notice("bus", f"census reset at {stamp} -- statistics restarted from zero "
                            "(raw log, if recording, continues uninterrupted)")

    def start_raw_log(self, note: str = "", dest=None) -> str:
        path = self.rawlog.start(note, dest=dest)
        self._notice("bus", f"raw frame log started: {path.name}")
        return str(path)

    def stop_raw_log(self) -> str | None:
        frames = self.rawlog.frames_written
        path = self.rawlog.stop()
        if path is not None:
            self._notice("bus", f"raw frame log stopped: {path.name} ({frames} frames)")
        return str(path) if path else None

    def _do_request_hold(self) -> None:
        for servo in self.servos.values():
            servo.command.request_hold()
            servo.pending_target = None
            servo.move_deadline = None
        self._notice("command", "HOLD asserted on all roles")

    def _set_pending(self, role: str, angle_deg: float) -> None:
        self.servos[role].pending_target = angle_deg

    def _do_request_move(self, role: str, angle_deg: float | None = None) -> None:
        """Runs on the TX thread. request_move()'s own freshness check is a
        fast-fail convenience for the caller -- it reads pending_target/
        last_seen outside the intent queue, so it can race a just-queued
        set_pending_target(). This is the check that actually gates whether
        0x9E00 gets asserted, re-done here against data that is guaranteed
        current at the moment of commit."""
        servo = self.servos[role]
        if angle_deg is not None:
            servo.pending_target = angle_deg
        if servo.pending_target is None:
            return
        now = time.monotonic() - self._t0
        last = servo.reader.snapshot()["last_seen"].get(servo.actual_id)
        if last is None or (now - last) > FRESH_FOR_MOVE_S:
            self._notice("safety", f"{role}: move intent dropped -- actual frame not fresh at commit time")
            servo.pending_target = None
            return
        actual = servo.reader.snapshot()["angle"]
        distance = abs(servo.pending_target - actual) if actual is not None else 90.0
        servo.command.target = servo.pending_target
        servo.command.begin_move()
        servo.move_deadline = now + distance / MOVE_WATCHDOG_SPEED_DEG_S + MOVE_WATCHDOG_MIN_S
        self._notice("command", f"{role}: commanding move to {servo.pending_target:.1f} deg "
                                 f"(watchdog at T+{servo.move_deadline:.1f}s)")

    def _tick(self, elapsed: float) -> None:
        """Runs on the TX thread via on_cycle: drains intents, updates every
        role's real Command from its real Reader, and enforces the move
        watchdog. This is the ONLY place any Command is mutated.
        """
        while True:
            try:
                intent = self._intents.get_nowait()
            except queue.Empty:
                break
            intent()

        now = time.monotonic() - self._t0
        for role, servo in self.servos.items():
            actual = servo.reader.snapshot()["angle"]
            if not servo.command.moving:
                # Track-while-hold: the transmitted target follows the last
                # reported actual, so a stale target field can never read as
                # a fresh command if the hold/move semantics are ever wrong
                # in a way this bench has not observed.
                # Clamp to physical travel. Without this a single bad reading
                # becomes the TRANSMITTED setpoint (4915 deg encodes to a
                # structurally valid frame), feeding the actuator nonsense and
                # keeping the fault alive after the bad frame is long gone.
                if actual is not None:
                    lo, hi = protocol.ANGLE_PLAUSIBLE_RANGE
                    if lo <= actual <= hi:
                        servo.command.target = actual
            else:
                servo.command.update(actual, now)
                if servo.move_deadline is not None and now >= servo.move_deadline and servo.command.moving:
                    servo.command.request_hold()
                    servo.pending_target = None
                    servo.move_deadline = None
                    self._notice("fault", f"{role}: MOVE_TIMEOUT -- watchdog asserted HOLD")

            presence = servo.presence(now)
            if presence != servo.last_notice_state:
                if presence == "stale" and servo.last_notice_state == "live":
                    self._notice("presence", f"{role} actuator STALE")
                elif presence == "absent" and servo.last_notice_state in ("live", "stale"):
                    self._notice("presence", f"{role} actuator removed (>{GONE_S:.0f}s silent)")
                servo.last_notice_state = presence

        if self._procedure is not None:
            self._procedure.tick(now, self)

    def _build_sequence(self):
        """Recorded cyclic frames, plus target slots the recording never had.

        The boot recording is from the two-servo bench and carries only 0x181
        and 0x1A1, so oil and aux had a fully derived payload and nowhere to
        put it. inject_synthetic_targets() clones an existing target's cadence
        for each missing identifier.

        span_ms is measured BEFORE injection and handed to transmit()
        unchanged: it is derived from the frame count, and the sequence is laid
        out so 0x194 byte 5 stays continuous across the loop boundary. Letting
        injection move that boundary would put a counter discontinuity in once
        per lap, which presents as a dropout, not as a bug.
        """
        frames = load_sequence(self.sequence_path, ACTIVE_IDS)
        span_ms = sequence_span_ms(frames)
        present = {f.can_id for f in frames}
        missing = sorted(COMMANDABLE_TARGET_IDS - present)
        if missing:
            frames = inject_synthetic_targets(frames, missing)
            self._notice("bus", "synthesised target frames for "
                                + ", ".join(f"0x{c:03X}" for c in missing)
                                + " -- absent from the recorded sequence")
        return frames, span_ms

    def _run_transmit(self, frames, span_ms=None) -> None:
        # An exception escaping here used to kill this daemon thread with no
        # trace: the UI kept reporting "active" over a bus nobody was
        # transmitting on (write_timeout now raises instead of blocking
        # forever, so this path is reachable). Fail loud instead -- flip the
        # mode to idle so every connected browser sees the truth, and leave
        # an evidence notice.
        try:
            transmit(self.transport, frames, duration_s=float("inf"), reader=self._dispatch_reader,
                     cmd=self._dispatch_cmd, on_cycle=lambda elapsed, cmd, reader: self._tick(elapsed),
                     stop_event=self._stop_event, quiet=True, span_ms=span_ms)
        except Exception as exc:
            # Same duty shutdown_bus performs before stopping its thread: a
            # procedure left attached here would freeze as "running" forever
            # (nothing ticks it once this thread is gone) and Abort -- which
            # is only an intent, drained by the now-dead _tick -- would
            # silently never run. Abort it synchronously; abort() only sets
            # flags and stops recording.
            if self._procedure is not None and not self._procedure.finished:
                try:
                    self._procedure.abort(self, f"TX fault: {type(exc).__name__}")
                except Exception:
                    pass
            self._stop_event.set()
            # Close the adapter HERE, from this thread -- close() joins the
            # RX thread, not us, so there is no join-on-self. Leaving the
            # port open made every later BUS ON fail with PermissionError
            # forever: Windows refuses a second handle on a COM port, and
            # with mode already idle on_set_mode skips shutdown_bus(), so
            # nothing else ever closed it.
            try:
                self.transport.close()
            except Exception:
                pass
            self._mode = "idle"
            self._notice("fault", f"TX STOPPED — {type(exc).__name__}: {exc}")
