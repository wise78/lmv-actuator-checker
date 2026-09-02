"""Build the desktop shell: stamp the version, run PyInstaller, and (if Inno
Setup is on PATH) compile the installer. One command instead of remembering
three, and it stamps BUILD_DATE from THIS machine's clock at build time --
never hand-edited, so a report can never carry a stale or wrong build date.

Usage (from scripts/):
    python desktop/build.py            # PyInstaller only
    python desktop/build.py --installer  # also compile the Inno Setup .iss
"""

from __future__ import annotations

import argparse
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

SCRIPTS_DIR = Path(__file__).resolve().parent.parent
VERSION_FILE = SCRIPTS_DIR / "sqm45" / "_version.py"
SPEC_FILE = SCRIPTS_DIR / "desktop" / "sqm45_checker.spec"
ISS_FILE = SCRIPTS_DIR.parent / "installer" / "sqm45_checker.iss"


def read_version() -> str:
    text = VERSION_FILE.read_text(encoding="utf-8")
    m = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if not m:
        raise SystemExit(f"could not find __version__ in {VERSION_FILE}")
    return m.group(1)


def stamp_build_date() -> None:
    text = VERSION_FILE.read_text(encoding="utf-8")
    stamped = time.strftime("%Y-%m-%d %H:%M")
    new_text = re.sub(r'BUILD_DATE\s*=\s*"[^"]*"', f'BUILD_DATE = "{stamped}"', text)
    VERSION_FILE.write_text(new_text, encoding="utf-8")
    print(f"stamped BUILD_DATE = {stamped}")


def check_iss_version_matches(app_version: str) -> None:
    if not ISS_FILE.is_file():
        return
    text = ISS_FILE.read_text(encoding="utf-8")
    m = re.search(r'#define MyAppVersion "([^"]+)"', text)
    if m and m.group(1) != app_version:
        print(f"WARNING: installer/sqm45_checker.iss says version {m.group(1)!r} "
              f"but sqm45/_version.py says {app_version!r} -- update the .iss "
              f"#define before compiling the installer, or the two will disagree.",
              file=sys.stderr)


def run_pyinstaller() -> None:
    dist = SCRIPTS_DIR / "desktop" / "dist"
    build = SCRIPTS_DIR / "desktop" / "build"
    print(f"\n== PyInstaller: building into {dist} ==")
    subprocess.run(
        [sys.executable, "-m", "PyInstaller", str(SPEC_FILE), "--noconfirm",
         "--distpath", str(dist), "--workpath", str(build)],
        cwd=str(SCRIPTS_DIR), check=True)


def run_inno_setup() -> None:
    iscc = shutil.which("ISCC.exe") or shutil.which("ISCC")
    if not iscc:
        for candidate in (r"C:\Program Files (x86)\Inno Setup 6\ISCC.exe",
                          r"C:\Program Files\Inno Setup 6\ISCC.exe",
                          # winget's default per-user install location (no
                          # admin prompt) -- confirmed 2026-08-31, not on PATH
                          # even in a freshly-opened shell.
                          str(Path.home() / "AppData" / "Local" / "Programs"
                              / "Inno Setup 6" / "ISCC.exe")):
            if Path(candidate).is_file():
                iscc = candidate
                break
    if not iscc:
        print("\nInno Setup (ISCC.exe) not found -- skipping installer compile. "
              "Install it (winget install JRSoftware.InnoSetup) or compile "
              f"{ISS_FILE} manually in the Inno Setup Compiler.", file=sys.stderr)
        return
    print(f"\n== Inno Setup: compiling {ISS_FILE.name} ==")
    subprocess.run([iscc, str(ISS_FILE)], check=True)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--installer", action="store_true",
                    help="also compile the Inno Setup installer after building")
    args = ap.parse_args()

    version = read_version()
    print(f"LMV Actuator Checker v{version}")
    stamp_build_date()
    check_iss_version_matches(version)
    run_pyinstaller()

    exe = SCRIPTS_DIR / "desktop" / "dist" / "LMV Actuator Checker" / "LMV Actuator Checker.exe"
    if exe.is_file():
        print(f"\nBuilt: {exe}")
    else:
        print(f"\nWARNING: expected exe not found at {exe} -- check the PyInstaller "
              "output above for errors.", file=sys.stderr)
        return 1

    if args.installer:
        run_inno_setup()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
