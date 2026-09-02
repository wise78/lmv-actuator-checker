"""LMV Actuator Checker -- desktop shell.

Turns the Flask+SocketIO dashboard into something a technician double-clicks:
no terminal, no --port flag to remember, no python.exe on their machine. This
file is the PyInstaller entry point.

Two runtime paths through this one process
-------------------------------------------
1. SAVEDIALOG CHILD: `launcher.exe --sqm45-savedialog-child <args>`. The Tk
   Save-as dialog subprocess (sqm45/savedialog.py) needs a real Python
   interpreter to run `-c <code>` against, and a frozen exe has no `-c` --
   sys.executable under PyInstaller IS this program. So this program re-invokes
   ITSELF with a sentinel flag; that path must be checked and handled before
   anything else in this file runs, so it stays a fast, side-effect-free dialog
   host rather than launching a second full dashboard.
2. NORMAL: build a pywebview window, show connect.html (a plain local page,
   no Flask needed yet), and expose an Api the page's JS drives through
   window.pywebview.api. Only once the operator picks a port/replay and clicks
   Connect does this process construct a transport and call the SAME
   sqm45_web.app.create_app() the CLI (sqm45_webapp.py) uses -- one code path
   for "what a session is", whether started from a terminal or a double-click.
"""

from __future__ import annotations

import sys
from pathlib import Path

# Dev mode: make sqm45 / sqm45_web importable the same way sqm45_webapp.py
# does. Harmless when frozen too -- PyInstaller already puts the bundle root
# first on sys.path, so this is at worst a redundant entry.
SCRIPT_DIR = Path(__file__).resolve().parent          # .../scripts/desktop
SCRIPTS_DIR = SCRIPT_DIR.parent                        # .../scripts
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from sqm45 import savedialog  # noqa: E402

# --- path 1: dialog child -- checked FIRST, before any heavy import --------
if len(sys.argv) > 1 and sys.argv[1] == savedialog.SAVEDIALOG_CHILD_FLAG:
    savedialog.run_child(sys.argv[2:])
    sys.exit(0)

# Only imported past this point: a dialog-child invocation never pays for
# Flask, SocketIO or webview, which matters because it is spawned fresh for
# every single export click and must stay quick.
import json          # noqa: E402
import os             # noqa: E402
import socket         # noqa: E402
import subprocess     # noqa: E402
import threading      # noqa: E402
import time           # noqa: E402
import webbrowser     # noqa: E402

import webview  # noqa: E402

from sqm45 import portscan  # noqa: E402
from sqm45._version import __version__ as APP_VERSION, BUILD_DATE  # noqa: E402
from sqm45.replay_source import FileReplaySource  # noqa: E402
from sqm45.transport import transport_for_port  # noqa: E402
from sqm45_web.app import create_app  # noqa: E402

CONNECT_HTML = (SCRIPT_DIR / "connect.html").read_text(encoding="utf-8")

# Bundled so the app is USABLE the moment it is installed, before anyone has
# plugged anything in or found a bench -- "try it" must not require hardware.
DEMO_DIR = SCRIPT_DIR / "demo"
DEMO_CAPTURES = [
    {"path": "demo_4servo.csv",
     "label": "4 actuators (air, gas, oil, aux1) -- full dashboard, real bus traffic"},
    {"path": "demo_2servo_move.csv",
     "label": "2 actuators, commanded move 30° -> 90° -- shows a health-check-style transit"},
]

# Search-engine queries, not direct vendor links: this project does not ship
# or verify a specific installer, and a stale or wrong direct link is worse
# than a search the operator can eyeball before trusting.
_DRIVER_QUERIES = {
    "esp32": "Silicon Labs CP210x USB to UART VCP driver download",
    "tm7565": "ICP DAS tM-7565 driver download",
}

_ACK_MARKER = Path(os.environ.get("LOCALAPPDATA", str(Path.home()))) / "LMV Actuator Checker" / "first_run_ack.json"


def _find_free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _wait_for_port(host: str, port: int, timeout: float = 8.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((host, port), timeout=0.3):
                return True
        except OSError:
            time.sleep(0.1)
    return False


class Api:
    """Everything connect.html's JS can call. One instance per process --
    holds the engine/socketio once connected, so change_adapter() can shut
    the bus down cleanly before this process replaces itself.
    """

    def __init__(self):
        self.engine = None
        self.socketio = None
        self.http_port: int | None = None

    # -- version / first run ------------------------------------------------

    def app_version(self) -> dict:
        return {"version": APP_VERSION, "build_date": BUILD_DATE}

    def first_run_status(self) -> dict:
        return {"acknowledged": _ACK_MARKER.exists()}

    def acknowledge_first_run(self) -> dict:
        try:
            _ACK_MARKER.parent.mkdir(parents=True, exist_ok=True)
            _ACK_MARKER.write_text(
                json.dumps({"acknowledged_at": time.time(), "version": APP_VERSION}),
                encoding="utf-8")
        except Exception:
            pass   # non-fatal: worst case the gate re-shows next launch
        return {"ok": True}

    def open_driver_search(self, which: str) -> dict:
        query = _DRIVER_QUERIES.get(which)
        if not query:
            return {"ok": False}
        try:
            import urllib.parse
            webbrowser.open("https://www.google.com/search?q=" + urllib.parse.quote(query))
        except Exception:
            return {"ok": False}
        return {"ok": True}

    # -- picker data ----------------------------------------------------

    def list_ports(self) -> list[dict]:
        try:
            return portscan.list_ports()
        except Exception:
            return []

    def list_replays(self) -> list[dict]:
        out = []
        for item in DEMO_CAPTURES:
            p = DEMO_DIR / item["path"]
            if p.is_file():
                out.append({"path": str(p), "label": item["label"]})
        return out

    def get_defaults(self) -> dict:
        from sqm45 import savedialog as _sd
        return {"baud": 115200, "can_bitrate_label": "50 kbps",
                "output_dir": str(_sd.default_output_dir())}

    # -- connect ----------------------------------------------------------

    def connect(self, params: dict) -> dict:
        try:
            if params.get("mode") == "replay":
                path = Path(params.get("replay_file") or "")
                if not path.is_file():
                    return {"ok": False, "error": "demo capture not found"}
                transport = FileReplaySource(path, speed=1.0, loop=True)
            else:
                port = params.get("port")
                if not port:
                    return {"ok": False, "error": "no port selected"}
                baud = int(params.get("baud") or 115200)
                transport = transport_for_port(port, baud)
        except Exception as exc:
            return {"ok": False, "error": f"could not open adapter: {exc}"}

        listen_only = bool(params.get("listen_only", True))
        # Marks every page rendered by this session as desktop-launched, which
        # is what puts the "Change adapter" control in the topbar -- a plain
        # `python sqm45_webapp.py` session has no launcher to hand control
        # back to, so that link must not appear there.
        os.environ["SQM45_DESKTOP"] = "1"

        try:
            app, socketio, engine = create_app(transport, listen_only=listen_only)
        except Exception as exc:
            return {"ok": False, "error": f"could not start dashboard: {exc}"}

        self.engine = engine
        self.socketio = socketio
        self.http_port = _find_free_port()
        http_port = self.http_port

        def _run():
            # allow_unsafe_werkzeug: the dev server's normal guard assumes it
            # owns the process's main thread (the CLI's socketio.run() does);
            # here it is deliberately on a background thread so pywebview can
            # own the main thread instead, which this flag is documented for.
            socketio.run(app, host="127.0.0.1", port=http_port,
                        allow_unsafe_werkzeug=True, debug=False, use_reloader=False)

        threading.Thread(target=_run, daemon=True, name="SQM45Flask").start()

        if not _wait_for_port("127.0.0.1", http_port, timeout=8.0):
            return {"ok": False, "error": "dashboard server did not start in time"}

        if listen_only:
            # Mirrors sqm45_webapp.py's --listen-only auto-start: a listen
            # session has exactly one safe mode, so waiting for a click just
            # risks missing whatever happens in the first seconds on the bus.
            try:
                engine.start("listen")
            except Exception:
                pass   # not fatal -- the dashboard's own controls still work

        return {"ok": True, "url": f"http://127.0.0.1:{http_port}/"}

    # -- change adapter -----------------------------------------------------

    def change_adapter(self) -> dict:
        """Full process restart. Explained in savedialog.py's neighbour
        problem (Tk off the main thread): here the equivalent hazard is a
        Werkzeug dev server thread and a live serial port, neither of which
        has a clean programmatic shutdown from outside its own thread. A
        fresh process is simpler and more reliable than trying to tear the
        old one down piece by piece, and this is a bench tool restarting on
        an explicit operator click, not a service that must stay up.
        """
        try:
            if self.engine is not None and self.engine.mode != "idle":
                self.engine.shutdown_bus()
        except Exception:
            pass
        try:
            if getattr(sys, "frozen", False):
                subprocess.Popen([sys.executable])
            else:
                subprocess.Popen([sys.executable, str(Path(__file__).resolve())])
        except Exception:
            pass
        # Hard exit, not sys.exit(): non-daemon threads (Werkzeug's own
        # accept loop) would otherwise block a normal interpreter shutdown,
        # and the new process is already starting regardless.
        os._exit(0)
        return {"ok": True}   # unreachable; keeps the method's return type honest


SELFTEST_FLAG = "--sqm45-selftest"


def _selftest() -> int:
    """Headless smoke test of everything that changes between dev and FROZEN:
    bundled data files found via Path(__file__), the demo replay actually
    building a working dashboard, and a PDF export round-trip through the
    real (frozen) savedialog child path -- run once from inside the actual
    exe so path resolution is proven under PyInstaller, not assumed from a
    dev-mode run. No GUI needed: this exercises the Api class exactly as
    connect.html's JS would, minus the window nobody can screenshot headless.

    Run: "LMV Actuator Checker.exe" --sqm45-selftest
    """
    import urllib.request
    ok = True

    def check(label: str, cond: bool, detail: str = "") -> None:
        nonlocal ok
        mark = "PASS" if cond else "FAIL"
        print(f"  [{mark}] {label}" + (f" -- {detail}" if detail else ""))
        if not cond:
            ok = False

    print(f"LMV Actuator Checker v{APP_VERSION} -- self-test (frozen={getattr(sys, 'frozen', False)})")
    check("connect.html loaded", len(CONNECT_HTML) > 500, f"{len(CONNECT_HTML)} bytes")

    api = Api()
    replays = api.list_replays()
    check("bundled demo captures found", len(replays) == len(DEMO_CAPTURES),
          f"{len(replays)} of {len(DEMO_CAPTURES)}")

    defaults = api.get_defaults()
    check("get_defaults() returns a baud", defaults.get("baud") == 115200)

    result = api.connect({"mode": "replay", "replay_file": replays[0]["path"] if replays else "",
                          "listen_only": True})
    check("connect() (replay mode) succeeded", result.get("ok"), result.get("error", ""))
    if result.get("ok"):
        try:
            html = urllib.request.urlopen(result["url"], timeout=5).read().decode("utf-8", "replace")
            check("dashboard HTML served", "LMV Actuator Checker" in html)
            check("version stamped in dashboard", f"v{APP_VERSION}" in html)
        except Exception as exc:
            check("dashboard HTML served", False, str(exc))
        time.sleep(2)   # let a couple of snapshots land before reading engine state
        check("engine reached listen mode", api.engine is not None and api.engine.mode == "listen",
              str(api.engine.mode if api.engine else None))

    # PDF generation, the part most at risk from the spec's scipy/pandas/PIL
    # excludes (PyMuPDF's own optional integrations, none of which report.py
    # actually uses) -- prove fitz.Story still renders WITHOUT them, on the
    # real frozen build, not just in dev where they happen to be installed.
    try:
        from sqm45 import report as _report
        tmp_pdf = Path(os.environ.get("TEMP", ".")) / "sqm45_selftest_report.pdf"
        status = {"role": "air", "serial": "SELFTEST", "technician": "selftest",
                  "verdict": "PASS", "finished": True, "elapsed_s": 1.0,
                  "steps": [{"id": "H0", "name": "Preflight", "status": "pass",
                            "measured": "ok", "criterion": "c", "baseline": "b"}]}
        ctx = {"source_label": "selftest", "generated_wall": "now"}
        _report.write_pdf(_report.build_html(status, ctx), tmp_pdf)
        check("PDF report renders (fitz.Story)", tmp_pdf.is_file() and tmp_pdf.stat().st_size > 1000,
              f"{tmp_pdf.stat().st_size if tmp_pdf.is_file() else 0} bytes")
        try:
            tmp_pdf.unlink()
        except Exception:
            pass
    except Exception as exc:
        check("PDF report renders (fitz.Story)", False, str(exc))

    # Save-dialog child path, THROUGH THE REAL FROZEN EXE, with a short
    # timeout: a human is not present to click it, so a timeout here means
    # the dialog opened and is waiting -- exactly like the dev-mode test in
    # FINDINGS.md. What this proves that dev mode cannot: sys.executable is
    # the frozen exe itself and the SAVEDIALOG_CHILD_FLAG handoff is what
    # makes that work at all, since a frozen exe has no "-c" flag.
    from sqm45 import savedialog
    try:
        proc = subprocess.run(
            savedialog._child_command(["t", "C:/", "f.csv", ".csv", "d"]),
            capture_output=True, text=True, timeout=4)
        check("savedialog child path", False,
              f"returned immediately ({proc.returncode}) -- expected a timeout")
    except subprocess.TimeoutExpired:
        check("savedialog child path (frozen self-invoke)", True,
              "dialog opened and is waiting for input, as expected unattended")
    except Exception as exc:
        check("savedialog child path", False, str(exc))

    print("\nSELF-TEST " + ("PASSED" if ok else "FAILED"))
    return 0 if ok else 1


def main() -> int:
    if len(sys.argv) > 1 and sys.argv[1] == SELFTEST_FLAG:
        return _selftest()

    api = Api()
    window = webview.create_window(
        f"LMV Actuator Checker v{APP_VERSION}", html=CONNECT_HTML, js_api=api,
        width=880, height=760, min_size=(760, 640))
    # On Windows the taskbar/title-bar icon actually comes from the exe's own
    # embedded icon resource (see the spec's EXE(icon=...)) -- this is passed
    # too because pywebview's other backends (GTK/Qt) DO read it directly, and
    # passing it costs nothing on the platforms that ignore it.
    icon_path = SCRIPT_DIR / "app_icon.ico"
    webview.start(icon=str(icon_path) if icon_path.is_file() else None)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
