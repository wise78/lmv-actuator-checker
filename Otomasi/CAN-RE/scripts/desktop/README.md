# LMV Actuator Checker -- desktop build

Turns the Flask+SocketIO dashboard (`sqm45_webapp.py`) into a double-click
app: pick a port (or a bundled demo capture) from a window, no terminal, no
`--port` flag to remember. See `FINDINGS.md` (2026-08-31, "desktop app
shipped to the team") for the full design rationale.

## Files

| File | Role |
|---|---|
| `launcher.py` | PyInstaller entry point. Hosts the pywebview window, the `Api` the connect page's JS calls, and (via a hidden CLI flag) the save-dialog child process and a headless self-test. |
| `connect.html` | The first screen: driver/safety notice (first run only), port picker, bus-mode choice. Plain HTML/JS, no Flask -- loaded directly by pywebview before any dashboard exists. |
| `demo/` | Two small bundled captures so the app is usable with zero hardware -- "try it" must never require a bench. |
| `sqm45_checker.spec` | PyInstaller build spec. `--onedir`, not `--onefile` -- see the spec's own comment for why. |
| `build.py` | `python desktop/build.py [--installer]` -- stamps the build date, runs PyInstaller, optionally compiles the Inno Setup installer. |

## Building

```
cd scripts
python desktop/build.py --installer
```

Needs `pyinstaller` and `pywebview` (`pip install pyinstaller pywebview`) and,
for `--installer`, Inno Setup on PATH (`winget install JRSoftware.InnoSetup`).
Output: `desktop/dist/LMV Actuator Checker/` (the app folder) and
`installer/output/LMVActuatorCheckerSetup-<version>.exe` (the installer).

## Verifying a build

```
"desktop/dist/LMV Actuator Checker/LMV Actuator Checker.exe" --sqm45-selftest
```

Headless -- no window, no clicking. Exercises exactly what changes between a
dev run and a frozen one: bundled data files found via `Path(__file__)`, the
demo replay building a real dashboard, a PDF rendering through `fitz.Story`,
and the save-dialog child process (which re-invokes the exe itself, since a
frozen build has no `python -c`). Run it after every build that touches
`sqm45/report.py`, the PyInstaller `excludes` list, or anything path-related.

For the real test -- does this run on a machine that has never had Python --
launch the exe with `PATH` stripped of every Python directory, from a working
directory outside this repo. That is what actually proves nothing is quietly
depending on the dev source tree or a `python.exe` on `PATH`.

## Why the bundle excludes scipy/pandas/PIL/lxml/cryptography

PyMuPDF (`fitz`) has optional integrations with all five that PyInstaller's
static analysis follows and bundles regardless of whether this app calls
them -- `report.py` only builds text/table HTML through `fitz.Story`, no
images, no dataframes, no encryption. Excluding them cut the bundle by
roughly 85 MB. If a future report feature genuinely needs one of these, the
`--sqm45-selftest` PDF check will fail loudly on a build that dropped it
without adding it back -- that failure is the signal to remove the exclude,
not a reason to avoid trying it again.

## Safety default

The bus-mode radio in `connect.html` defaults to **Listen only**, and that is
what `Api.connect()` passes to `create_app(..., listen_only=...)` -- the
dashboard's own Keep-alive toggle is then server-refused for the life of the
session, exactly like the CLI's `--listen-only` flag. Switching to Keep alive
in the picker only removes that hard lock; the dashboard's existing
isolated-bench confirmation dialog still has to be answered before any frame
is actually transmitted. Two independent gates, not one.
