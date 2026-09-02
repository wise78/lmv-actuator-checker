"""Flask + SocketIO server wrapping one BusEngine.

One engine per process, shared by every connected browser -- there is one
physical bus. Auto-HOLD fires when the last browser tab disconnects, never
when transmission itself would stop: the actuator has zero jitter tolerance
(FINDINGS.md, 2026-08-14), so losing the UI must degrade to "holding
position", never "uncontrolled". async_mode is pinned to "threading" --
eventlet/gevent monkey-patch time.sleep/threading.Thread, which is exactly
what replay.transmit()'s timing depends on. Do not add either as a
dependency of this package.
"""

from __future__ import annotations

import json
import sys
import subprocess
import threading
import time
from pathlib import Path

from flask import Flask, render_template, request
from flask_socketio import SocketIO

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from sqm45 import protocol  # noqa: E402
from sqm45._version import __version__ as APP_VERSION  # noqa: E402
from sqm45.engine import BusEngine, EngineError, RoleNotFresh  # noqa: E402
from sqm45 import report as reportmod  # noqa: E402
from sqm45 import savedialog  # noqa: E402
from sqm45.healthcheck import HealthCheckRun  # noqa: E402

BROADCAST_INTERVAL_S = 0.1   # snapshot push rate to the browser
# The census report walks and scores every identifier -- an order of
# magnitude more work than a servo snapshot, and nothing on the page changes
# meaningfully faster than this.
CENSUS_INTERVAL_S = 0.5


def write_census_report(engine, path: Path) -> Path:
    """Freeze the current census to disk as JSON, next to the raw logs.

    The raw CSV is the evidence; this is the reading of it, with the scoring
    that produced each verdict attached. Kept together so a candidate
    identifier can be re-argued later from the same numbers rather than from
    a screenshot.
    """
    snap = engine.census_snapshot(include_recent=False)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(snap, indent=2, sort_keys=True), encoding="utf-8")
    return path


def create_app(transport, listen_only: bool = False,
               output_dir: Path | None = None) -> tuple[Flask, SocketIO, BusEngine]:
    """listen_only locks the process into receive-only for its whole life.

    The BUS MODE toggle already exists, but it is a button, and a button on a
    live burner-safety bus is one mis-click from transmitting LMV51
    identifiers into a controller that is driving real dampers. When the
    operator knows in advance that a session is a sniff -- a plant visit, a
    bus they do not own -- the guarantee belongs on the command line, where
    nothing in the UI can revoke it.
    """
    # Where the Save-as dialog OPENS. Never a folder inside the application: this
    # ships to technicians, and an export landing in an install directory is
    # both surprising and often not writable. The operator's choice always wins;
    # this is only the starting point, and the fallback when no dialog can be
    # shown (headless host, tkinter missing).
    default_dir = Path(output_dir) if output_dir else savedialog.default_output_dir()

    app = Flask(__name__)
    app.config["SECRET_KEY"] = "sqm45-bench-instrument"   # local bench tool, no auth surface
    socketio = SocketIO(app, async_mode="threading")
    engine = BusEngine(transport)

    # Census subscribers are tracked BY SESSION ID, not as a counter: a
    # browser tab closed without a clean unsubscribe would otherwise leak a
    # count that never returns to zero, and the server would keep building
    # full census reports forever with nobody watching.
    state = {"clients": 0, "census_sids": set()}
    state_lock = threading.Lock()

    def role_view(role: str, s: dict) -> dict:
        return {
            "role": role,
            # Roster metadata travels WITH each servo so the browser never has
            # to hold its own copy of the address map. A UI that hard-codes the
            # servo list is exactly how a real actuator ends up invisible.
            "address": s.get("address"),
            "label": s.get("label", role.upper()),
            "color": s.get("color"),
            "actual_id_hex": s.get("actual_id_hex"),
            "target_id_hex": s.get("target_id_hex"),
            "target_confidence": s.get("target_confidence"),
            "commandable": s.get("commandable", False),
            "experimental": s.get("experimental", False),
            "presence": s["presence"],
            "actual_deg": s["actual_deg"],
            "target_deg": s["target_deg"],
            "pending_target_deg": s["pending_target_deg"],
            "state_hex": f"0x{s['state']:04X}",
            "state_label": "MOVE" if s["state"] == protocol.STATE_MOVE else "HOLD",
            "moving": s["moving"],
            # Actuator-raised status must be VISIBLE, not silently dropped. A
            # servo checker that hides the actuator saying "something is wrong"
            # is doing the opposite of its job.
            "status_frames": s.get("status_frames", 0),
            "fault_frames": s.get("fault_frames", 0),
            "rejected_frames": s.get("rejected_frames", 0),
            "status_hex": f"0x{s.get('last_status_bits', 0):04X}",
        }

    def full_snapshot() -> dict:
        snap = engine.snapshot()
        return {"mode": snap["mode"],
                "t": snap.get("t"),
                "listen_only": listen_only,
                "roster": engine.roster_view(),
                "servos": {role: role_view(role, s) for role, s in snap["servos"].items()}}

    @app.context_processor
    def inject_flags():
        # Injected globally rather than passed per route: every page carries
        # the same safety bar, so every page must agree about whether
        # transmission is possible. desktop_mode is read from the environment
        # rather than passed as a create_app() parameter deliberately -- the
        # desktop launcher sets it on the process before starting the SAME
        # create_app() the CLI uses, so no second code path exists for "is
        # this running under the desktop shell".
        import os as _os
        return {"listen_only": listen_only, "app_version": APP_VERSION,
                "desktop_mode": _os.environ.get("SQM45_DESKTOP") == "1"}

    @app.route("/")
    def dashboard():
        return render_template("dashboard.html", source_label=transport.source_label, active_page="dashboard")

    @app.route("/servo-checker")
    def servo_checker():
        return render_template("servo_checker.html", source_label=transport.source_label, active_page="servo_checker")

    @app.route("/health-check")
    def health_check():
        return render_template("health_check.html", source_label=transport.source_label, active_page="health_check")

    @app.route("/raw-sniffer")
    def raw_sniffer():
        return render_template("raw_sniffer.html", source_label=transport.source_label, active_page="raw_sniffer")

    @app.route("/api/census")
    def api_census():
        """The census as JSON, for a shell or a script rather than a browser.

        Read-only and side-effect free: it does not subscribe, reset, or touch
        the raw log. Exists so the live bus can be analysed from outside the
        page while someone is still watching the page.
        """
        return json.dumps(engine.census_snapshot(include_recent=False), sort_keys=True), 200, {
            "Content-Type": "application/json"}

    @socketio.on("connect")
    def on_connect():
        with state_lock:
            state["clients"] += 1
        socketio.emit("snapshot", full_snapshot())
        hc_status = engine.procedure_status()
        if hc_status is not None:
            socketio.emit("hc_status", hc_status)

    @socketio.on("disconnect")
    def on_disconnect():
        with state_lock:
            state["clients"] -= 1
            remaining = state["clients"]
            state["census_sids"].discard(getattr(request, "sid", None))
        if remaining <= 0 and engine.mode != "idle":
            engine.notify_ui_detached()

    @socketio.on("set_mode")
    def on_set_mode(data):
        mode = (data or {}).get("mode")
        if listen_only and mode == "active":
            socketio.emit("error", {"text": "This session was started with --listen-only. "
                                            "Transmission is disabled for its whole life."})
            socketio.emit("snapshot", full_snapshot())
            return
        try:
            if engine.mode != "idle":
                engine.shutdown_bus()
            if mode != "idle":
                engine.start(mode)
        except EngineError as exc:
            socketio.emit("error", {"text": str(exc)})
        socketio.emit("snapshot", full_snapshot())

    @socketio.on("request_hold")
    def on_request_hold():
        engine.request_hold()

    @socketio.on("shutdown_bus")
    def on_shutdown_bus():
        engine.shutdown_bus()
        socketio.emit("snapshot", full_snapshot())

    @socketio.on("set_pending_target")
    def on_set_pending_target(data):
        try:
            engine.set_pending_target(data["role"], float(data["angle"]))
        except (KeyError, TypeError, ValueError, EngineError) as exc:
            socketio.emit("error", {"text": str(exc)})

    @socketio.on("request_move")
    def on_request_move(data):
        try:
            d = data or {}
            engine.request_move(d["role"], d.get("angle"))
        except (KeyError, RoleNotFresh, EngineError) as exc:
            socketio.emit("error", {"text": str(exc)})

    @socketio.on("start_health_check")
    def on_start_health_check(data):
        data = data or {}
        role = data.get("role")
        serial = (data.get("serial") or "").strip()
        technician = (data.get("technician") or "").strip()
        work_order = (data.get("work_order") or "").strip()
        if listen_only:
            # Pointing at a button that this session has locked off is
            # un-actionable advice. Say what the session actually is, and
            # what part of the checker still works without transmitting.
            socketio.emit("error", {"text": "This session is --listen-only, and the health check "
                                            "has to command moves. Use the Raw Sniffer page: link "
                                            "quality, stability and cross-checks are all passive."})
            return
        if engine.mode != "active":
            socketio.emit("error", {"text": "Switch BUS MODE to Keep alive (HOLD) before starting a health check."})
            return
        role = engine.resolve_role(role or "")
        if role not in engine.servos:
            socketio.emit("error", {"text": "Select a discovered actuator before starting."})
            return
        if not serial or not technician:
            socketio.emit("error", {"text": "Serial number and technician name are both required."})
            return
        # attach_procedure() only queues the attach -- it is not applied
        # until the TX thread's next tick, so procedure_status() would very
        # likely still read None here. Emitting that racy None taught the
        # smoke-test client's naive handler a real lesson (browser JS already
        # guards on it) -- simplest fix is to just not emit early. The
        # broadcaster picks up the real status within BROADCAST_INTERVAL_S.
        engine.attach_procedure(HealthCheckRun(role=role, serial=serial, technician=technician,
                                                work_order=work_order))

    def reveal_in_file_manager(path: Path) -> str:
        """Open the OS file manager with the report selected.

        Best-effort and never fatal: the PDF is already written and its path is
        sent to the browser regardless, so a headless or non-Windows host still
        gets a usable result instead of an error.
        """
        try:
            if sys.platform == "win32":
                subprocess.Popen(["explorer", f"/select,{path}"])
            elif sys.platform == "darwin":
                subprocess.Popen(["open", "-R", str(path)])
            else:
                subprocess.Popen(["xdg-open", str(path.parent)])
            return "opened"
        except Exception as exc:
            return f"could not open file manager: {exc}"

    @socketio.on("export_report")
    def on_export_report():
        hc = engine.procedure_status()
        if hc is None or not hc.get("started"):
            socketio.emit("error", {"text": "No health check to report on."})
            return
        if not (hc.get("finished") or hc.get("aborted")):
            socketio.emit("error", {"text": "The health check is still running -- "
                                            "wait for it to finish before exporting."})
            return
        suggested = reportmod.report_filename(hc)
        dest = savedialog.ask_save_path(
            suggested, title="Save health check report",
            initial_dir=default_dir, description="PDF report")
        if dest is None:
            # Cancel is a normal outcome, not a failure. Say so quietly and
            # leave the button usable.
            socketio.emit("report_ready", {"cancelled": True})
            return
        try:
            snap = engine.snapshot()
            others = [f"{v.get('label', r)} {v.get('actual_id_hex', '')}".strip()
                      for r, v in sorted(snap["servos"].items(), key=lambda kv: kv[1]["address"])
                      if r != hc.get("role") and v["presence"] != "absent"]
            elapsed = hc.get("elapsed_s") or 0
            ctx = {
                "source_label": transport.source_label,
                "bitrate": "50 kbps",
                "mode": engine.mode + (" (listen-only session)" if listen_only else ""),
                "bystanders": ", ".join(others),
                "started_wall": time.strftime("%Y-%m-%d %H:%M:%S",
                                              time.localtime(time.time() - elapsed)),
                "generated_wall": time.strftime("%Y-%m-%d %H:%M:%S"),
            }
            path = reportmod.write_pdf(reportmod.build_html(hc, ctx), dest)
        except Exception as exc:
            socketio.emit("error", {"text": f"Report generation failed: {exc}"})
            return
        opened = reveal_in_file_manager(path)
        socketio.emit("report_ready", {"path": str(path), "name": path.name,
                                       "opened": opened})

    @socketio.on("abort_health_check")
    def on_abort_health_check():
        engine.abort_procedure("operator abort")

    @socketio.on("health_check_ht_choice")
    def on_health_check_ht_choice(data):
        choice = (data or {}).get("choice")
        if choice in ("applied", "not_available"):
            engine.set_procedure_ht_choice(choice)

    # -- raw sniffer -------------------------------------------------------
    # The census runs unconditionally on the RX thread (every frame is
    # evidence), but BUILDING the report walks every identifier and scores
    # each one. That is only paid for while a sniffer page is actually open,
    # which is what this subscriber count tracks.

    @socketio.on("census_subscribe")
    def on_census_subscribe(data):
        want = bool((data or {}).get("on", True))
        sid = getattr(request, "sid", None)
        with state_lock:
            if want:
                state["census_sids"].add(sid)
            else:
                state["census_sids"].discard(sid)
        if want:
            socketio.emit("census", engine.census_snapshot(include_recent=True))

    @socketio.on("census_reset")
    def on_census_reset():
        engine.census_reset()
        socketio.emit("census", engine.census_snapshot(include_recent=True))

    @socketio.on("start_raw_log")
    def on_start_raw_log(data):
        note = ((data or {}).get("note") or "").strip()
        if engine.mode == "idle":
            socketio.emit("error", {"text": "Bus is off — start LISTEN ONLY before recording."})
            return
        # Asked BEFORE the first frame is written, because this file streams --
        # there is no later moment at which the operator could still choose.
        dest = savedialog.ask_save_path(
            engine.rawlog.suggested_name(note), title="Record raw CAN frames to...",
            initial_dir=default_dir, description="Raw CAN capture (CSV)")
        if dest is None:
            socketio.emit("rawlog_status", {"status": engine.rawlog.status(),
                                            "path": None, "cancelled": True})
            return
        try:
            path = engine.start_raw_log(note, dest=dest)
        except Exception as exc:
            socketio.emit("error", {"text": f"cannot start raw log: {exc}"})
            return
        socketio.emit("rawlog_status", {"status": engine.rawlog.status(), "path": path})

    @socketio.on("stop_raw_log")
    def on_stop_raw_log():
        path = engine.stop_raw_log()
        # Reveal on STOP, not on start: the file is only complete and worth
        # handing to anyone once recording has finished.
        opened = reveal_in_file_manager(Path(path)) if path else "no file"
        socketio.emit("rawlog_status", {"status": engine.rawlog.status(), "path": path,
                                        "opened": opened})

    @socketio.on("export_census")
    def on_export_census():
        dest = savedialog.ask_save_path(
            f"census_{time.strftime('%Y%m%d_%H%M%S')}.json",
            title="Save census report", initial_dir=default_dir,
            description="Census report (JSON)")
        if dest is None:
            socketio.emit("census_exported", {"cancelled": True})
            return
        try:
            path = write_census_report(engine, dest)
        except Exception as exc:
            socketio.emit("error", {"text": f"cannot write census report: {exc}"})
            return
        opened = reveal_in_file_manager(path)
        socketio.emit("census_exported", {"path": str(path), "name": path.name,
                                          "opened": opened})

    def hc_signature(hc: dict) -> str:
        # Only fields whose CHANGE means something. "measured" strings churn
        # every tick mid-run (that is what a live readout is) and are
        # deliberately excluded -- the running throttle below carries them.
        # Without this guard a FINISHED health check was re-emitted at 10 Hz
        # forever: _live_h8_summary() produced a fresh string nobody needed,
        # and detach_procedure() has no caller.
        return json.dumps([hc.get("started"), hc.get("finished"), hc.get("verdict"),
                           hc.get("current_step"), hc.get("awaiting_ht_choice"),
                           [(s["id"], s["status"]) for s in hc.get("steps", [])]])

    def broadcaster() -> None:
        last_snap_json = None
        last_hc_sig = None
        last_hc_emit = 0.0
        last_census_emit = 0.0
        census_cursor = 0
        while True:
            socketio.sleep(BROADCAST_INTERVAL_S)
            with state_lock:
                if state["clients"] <= 0:
                    continue
                census_watchers = len(state["census_sids"])

            if census_watchers and (time.monotonic() - last_census_emit) >= CENSUS_INTERVAL_S:
                last_census_emit = time.monotonic()
                # recent_since keeps the raw stream incremental -- only frames
                # the browser has not already been sent.
                census = engine.census_snapshot(include_recent=True, recent_since=census_cursor)
                census_cursor = census.get("seq", census_cursor)
                socketio.emit("census", census)
            snap = full_snapshot()
            # The payload embeds the advancing ENGINE clock ("t"), so
            # comparing the whole dict can never skip: json.dumps differs on
            # every tick even on a fully settled bus. Compare without "t" --
            # it is a clock, not state; every field that means anything is
            # still covered.
            cmp_snap = {k: v for k, v in snap.items() if k != "t"}
            snap_json = json.dumps(cmp_snap, sort_keys=True)
            if snap_json != last_snap_json:   # settled bus => skip identical payloads
                last_snap_json = snap_json
                socketio.emit("snapshot", snap)
            events = engine.drain_events()
            if events:
                socketio.emit("events", events)
            notices = engine.drain_notices()
            if notices:
                socketio.emit("notices", notices)
            hc = engine.procedure_status()
            if hc is None:
                last_hc_sig = None   # reset: a NEW run must re-emit from scratch
                continue
            running = bool(hc.get("started")) and not hc.get("finished")
            sig = hc_signature(hc)
            now_mono = time.monotonic()
            emit_it = sig != last_hc_sig or (running and now_mono - last_hc_emit >= 0.5)
            if emit_it:
                last_hc_sig = sig
                last_hc_emit = now_mono
                socketio.emit("hc_status", hc)

    socketio.start_background_task(broadcaster)
    return app, socketio, engine
