"""Single source of truth for the shipped version.

Stamped into: the PDF report footer, the dashboard topbar, and the desktop
window title. Bump this by hand when cutting a build for the team -- a result
nobody can tie to a specific build is not fully traceable, the same argument
FINDINGS.md already makes for serial numbers and work orders.
"""

from __future__ import annotations

__version__ = "1.0.0"

# Set at build time by desktop/build.py (stamped into this file before
# PyInstaller runs); "dev" means "not a packaged build" -- source checkout or
# a Flask-only launch via sqm45_webapp.py.
BUILD_DATE = "2026-09-01 15:14"
