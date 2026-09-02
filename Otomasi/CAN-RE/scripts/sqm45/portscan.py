"""Enumerate serial ports for the desktop picker, with a friendly guess at
what each one is.

Deliberately thin: `transport.py` already owns the VID:PID -> adapter CLASS
map that decides actual behaviour (which ASCII quirks to expect). This module
only borrows that same map to produce a LABEL for display -- so the picker's
"looks like a tM-7565" guess and the engine's actual protocol choice can never
disagree, because they are reading the same table.
"""

from __future__ import annotations

from . import transport as _transport

_LABELS = {
    _transport.TM7565Transport: "ICP DAS tM-7565",
    _transport.ESP32Transport: "ESP32 CAN bridge",
}


def list_ports() -> list[dict]:
    """Every serial port Windows currently sees, most likely candidate first.

    A port whose VID:PID matches a known adapter is not just labelled -- it is
    sorted ahead of the generic COM ports (Bluetooth SPP, modems, other USB
    serial devices) a technician's laptop typically has several of, so the
    right choice is usually the first item in the dropdown rather than
    something they have to hunt for by COM number.
    """
    try:
        from serial.tools import list_ports as _lp
    except Exception:
        return []

    out = []
    for info in _lp.comports():
        cls = None
        if info.vid is not None and info.pid is not None:
            cls = _transport._ADAPTER_BY_VIDPID.get((info.vid, info.pid))
        out.append({
            "device": info.device,
            "description": info.description or "",
            "vid": f"{info.vid:04X}" if info.vid is not None else None,
            "pid": f"{info.pid:04X}" if info.pid is not None else None,
            "guessed_type": _LABELS.get(cls),
            "recognised": cls is not None,
        })
    out.sort(key=lambda p: (not p["recognised"], p["device"]))
    return out
