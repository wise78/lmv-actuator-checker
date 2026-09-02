# PyInstaller spec for the LMV Actuator Checker desktop shell.
#
# --onedir, not --onefile: onefile re-extracts the whole bundle to a fresh
# temp directory on every launch (slow, and flagged by antivirus far more
# often than a plain folder), and a folder is also what Inno Setup wants to
# install and later patch. Run with:
#   python -m PyInstaller desktop/sqm45_checker.spec --noconfirm
# from the scripts/ directory (paths below are relative to THIS file's
# location, scripts/desktop/, resolved via SPECPATH so the build works
# regardless of the caller's cwd).

import sys
from pathlib import Path

block_cipher = None
HERE = Path(SPECPATH)          # .../scripts/desktop
SCRIPTS = HERE.parent           # .../scripts

datas = [
    # "." for both: launcher.py is the ENTRY script, and PyInstaller resolves
    # its Path(__file__).parent to the bundle root (proven empirically --
    # connect.html loaded correctly at "." while demo/ at "desktop/demo" was
    # NOT found, because launcher.py looks for it at SCRIPT_DIR/"demo").
    # Regular imported modules (sqm45/engine.py etc.) do NOT work this way --
    # their __file__ stays package-relative, which is why sqm45/data and
    # sqm45_web/templates below keep their source-relative destination.
    (str(HERE / "connect.html"), "."),
    (str(HERE / "app_icon.ico"), "."),
    (str(HERE / "demo"), "demo"),
    (str(SCRIPTS / "sqm45_web" / "templates"), "sqm45_web/templates"),
    (str(SCRIPTS / "sqm45_web" / "static"), "sqm45_web/static"),
    (str(SCRIPTS / "sqm45" / "data"), "sqm45/data"),
]

hiddenimports = [
    "engineio.async_drivers.threading",
    "tkinter",
    "tkinter.filedialog",
    "serial.tools.list_ports",
    "serial.tools.list_ports_windows",
    "serial.win32",
    "webview.platforms.edgechromium",
]

a = Analysis(
    [str(HERE / "launcher.py")],
    pathex=[str(SCRIPTS)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    # The dev-only test suite and the curated 201-capture dataset are never
    # imported at runtime and would only bloat the bundle -- keep the shipped
    # app to what a technician actually needs on their machine.
    #
    # scipy/pandas/PIL/lxml/cryptography: PyMuPDF's own optional integrations
    # (table/dataframe export, image codecs we never call, PDF encryption we
    # never use) pulled in ~85 MB nothing in this app exercises -- report.py
    # only builds text/table HTML through fitz.Story, no images, no crypto.
    # If a future PDF feature genuinely needs one of these, drop it from this
    # list and rebuild -- do not silently re-add PIL "just in case", since
    # that is exactly how it got here the first time. desktop/launcher.py's
    # --sqm45-selftest exercises the actual PDF/report path against a frozen
    # build precisely so a wrong exclude here fails loudly, not silently.
    excludes=["sqm45.tests", "pytest", "numpy", "matplotlib",
             "scipy", "pandas", "PIL", "lxml", "cryptography"],
    noarchive=False,
    optimize=0,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="LMV Actuator Checker",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,          # UPX-packed exes get flagged by AV more often; not worth the size saving here
    console=False,       # windowed app -- no black terminal box behind the dashboard
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    # Taskbar/title-bar icon: on Windows this is what pywebview's window
    # actually shows (webview.start(icon=...) is honoured on some other
    # platforms but not reliably by the edgechromium backend) -- baking it
    # into the exe resource is the path that actually works here.
    icon=str(HERE / "app_icon.ico"),
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="LMV Actuator Checker",
)
