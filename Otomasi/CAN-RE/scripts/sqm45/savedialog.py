"""Native "Save as..." dialog, for a tool that ships to other people.

Why this exists
---------------
Every export used to be written straight into a folder inside the source tree.
That is fine for the developer's own machine and wrong for everyone else: a
technician running this on their own laptop should be asked where their report
goes, and should never have files appear inside an application directory they
did not choose and may not be able to write to.

Why a SUBPROCESS rather than calling tkinter directly
-----------------------------------------------------
The Flask/SocketIO handlers run on worker threads. Tk is not thread-safe and a
root window created off the main thread is unreliable -- on Windows it can hang
the calling thread or die taking the server with it. Spawning a short-lived
child process to own the dialog makes the failure modes containable: the worst
case is a non-zero exit and a None return, which the caller already handles as
"the operator cancelled".

This is only sane because the app is a LOCALHOST bench instrument -- the server
and the person clicking are the same machine. A dialog opened by a remote server
would be nonsense, so `available()` also lets callers degrade gracefully.

Why the child command differs when FROZEN (PyInstaller)
---------------------------------------------------------
`sys.executable -c CHILD_CODE` only works when sys.executable is a real
python.exe. Under PyInstaller, sys.executable IS THE APPLICATION ITSELF -- a
frozen exe has no `-c` flag, so that invocation would just relaunch the whole
app. Instead, frozen builds re-invoke their own exe with a hidden sentinel
argument (SAVEDIALOG_CHILD_FLAG); desktop/launcher.py checks for that flag as
the very first thing it does, before importing Flask or pywebview, runs the
same Tk dialog code, prints the result, and exits. One exe, acting as its own
helper process.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

# Recognised by desktop/launcher.py's entry point. Kept here (not just there)
# so this module's own docstring and _child_command() stay the single place
# that documents and constructs the frozen invocation -- no second literal to
# drift out of sync.
SAVEDIALOG_CHILD_FLAG = "--sqm45-savedialog-child"

# DEV-MODE ONLY: `python -c _CHILD` runs a real interpreter, so it can afford
# to duplicate run_child()'s body as a standalone script -- it cannot import
# `sqm45.savedialog` without knowing this package's location on sys.path,
# and threading that through argv is more fragile than the small duplication.
# Frozen builds do NOT use this string at all; they call run_child() directly
# from desktop/launcher.py. Keep the two in sync if either changes.
_CHILD = r'''
import sys, tkinter, tkinter.filedialog
title, initial_dir, initial_file, ext, desc = sys.argv[1:6]
root = tkinter.Tk()
root.withdraw()
root.attributes("-topmost", True)   # a dialog behind the browser looks like a hang
try:
    path = tkinter.filedialog.asksaveasfilename(
        parent=root, title=title, initialdir=initial_dir or None,
        initialfile=initial_file or None, defaultextension=ext,
        filetypes=[(desc, "*" + ext), ("All files", "*.*")])
finally:
    try:
        root.destroy()
    except Exception:
        pass
if path:
    sys.stdout.write(path)
'''

DIALOG_TIMEOUT_S = 300.0   # the operator may reasonably take a while to choose


def available() -> bool:
    try:
        import tkinter  # noqa: F401
        import tkinter.filedialog  # noqa: F401
    except Exception:
        return False
    return True


def run_child(argv: list[str]) -> None:
    """Execute the dialog and print the result. Called from TWO places:
    the `python -c CHILD` subprocess in dev mode (see _child_command), and
    desktop/launcher.py's own entry point when frozen and re-invoked with
    SAVEDIALOG_CHILD_FLAG. Both must produce byte-identical stdout, which is
    why this is one function rather than duplicated inline code.
    """
    import tkinter
    import tkinter.filedialog
    title, initial_dir, initial_file, ext, desc = argv[:5]
    root = tkinter.Tk()
    root.withdraw()
    root.attributes("-topmost", True)   # a dialog behind the browser looks like a hang
    try:
        path = tkinter.filedialog.asksaveasfilename(
            parent=root, title=title, initialdir=initial_dir or None,
            initialfile=initial_file or None, defaultextension=ext,
            filetypes=[(desc, "*" + ext), ("All files", "*.*")])
    finally:
        try:
            root.destroy()
        except Exception:
            pass
    if path:
        sys.stdout.write(path)


def _child_command(args: list[str]) -> list[str]:
    """The subprocess argv for one dialog invocation, dev or frozen."""
    if getattr(sys, "frozen", False):
        return [sys.executable, SAVEDIALOG_CHILD_FLAG, *args]
    return [sys.executable, "-c", _CHILD, *args]


def default_output_dir() -> Path:
    """Where exports go when the operator does not pick somewhere else.

    The user's own Documents folder, never inside the application. Falls back to
    the home directory if Documents does not exist (some Windows setups redirect
    it, and a missing folder must not stop an export).
    """
    docs = Path.home() / "Documents"
    base = docs if docs.is_dir() else Path.home()
    return base / "LMV Actuator Checker"


def ask_save_path(default_name: str, *, title: str = "Save as",
                  initial_dir: Path | None = None,
                  description: str = "File") -> Path | None:
    """Native Save-as dialog. Returns None if cancelled or unavailable.

    None is NOT an error: it is the ordinary outcome of the operator pressing
    Cancel, and callers must treat it as "do nothing" rather than as a failure
    worth reporting.
    """
    if not available():
        return None
    start = Path(initial_dir) if initial_dir else default_output_dir()
    try:
        start.mkdir(parents=True, exist_ok=True)
    except Exception:
        start = Path.home()
    ext = Path(default_name).suffix or ""
    try:
        proc = subprocess.run(
            _child_command([title, str(start), default_name, ext, description]),
            capture_output=True, text=True, timeout=DIALOG_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return None
    except Exception:
        return None
    chosen = (proc.stdout or "").strip()
    return Path(chosen) if chosen else None
