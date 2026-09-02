"""Freeze the behaviour of the CURRENT, bench-proven protocol code.

Run this BEFORE the protocol is moved into the sqm45 package. The fixtures it
writes are the durable record: a before/after diff disappears once the edit is
made, a frozen fixture does not. `test_protocol_parity.py` then holds the new
module to exactly this behaviour, forever.

    python sqm45/tests/make_golden_vectors.py
"""
from __future__ import annotations

import csv
import importlib.util
import sys
from dataclasses import fields, is_dataclass
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent.parent
CAPTURES = SCRIPTS.parent / "captures" / "raw"

# Captures chosen to span the conditions the decoder must handle: a settled
# hold, a commanded traverse, a cold boot, a live dropout, and a topology with
# only one actuator present.
SAMPLE_CAPTURES = [
    "idle_fuel_00_00_r1.csv",
    "move_air_10_70_r1.csv",
    "boot_all_na_na_r1.csv",
    "commsdep_canl_air_r1.csv",
    "topo_lmv_air_only_noazl_r2.csv",
]
LINES_PER_CAPTURE = 1000


def load(name: str, filename: str):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


def main() -> int:
    br = load("_golden_br", "sqm45_boot_replay.py")
    pm = load("_golden_pm", "sqm45_passive_monitor_ui.py")

    # --- encode side ------------------------------------------------------
    angles = [round(i * 0.1, 1) for i in range(0, 901)]
    # Clamp boundaries and the values that produce raw 0x0000 / 0xFFFF.
    angles += [-10.0, -0.1, 90.1, 100.0, 6543.5, 6543.6, 5000.0]
    rows = []
    for angle in angles:
        for can_id in (0x181, 0x1A1):
            for state in (br.STATE_HOLD, br.STATE_MOVE):
                rows.append({
                    "angle_deg": f"{angle:.1f}",
                    "can_id": f"0x{can_id:03X}",
                    "state": f"0x{state:04X}",
                    "payload": br.encode_target(angle, can_id, state),
                })
    out = HERE / "golden_encode_vectors.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, ["angle_deg", "can_id", "state", "payload"])
        w.writeheader(); w.writerows(rows)
    print(f"  encode : {len(rows):5d} vectors -> {out.name}")

    # --- uart framing -----------------------------------------------------
    frame_rows = []
    for can_id, dlc, data in ((0x080, 8, "00000C0074004D76"), (0x194, 8, "0000B106A5556E8A"),
                              (0x2A1, 6, "000006641E5B"), (0x181, 6, "FF9B00006B8C"),
                              (0x1A1, 6, "FF9B9E00F50C")):
        frame_rows.append({
            "can_id": f"0x{can_id:03X}", "dlc": dlc, "data": data,
            "uart": br.Frame(0.0, can_id, dlc, data).uart().decode("ascii").strip(),
        })
    out = HERE / "golden_uart_vectors.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, ["can_id", "dlc", "data", "uart"])
        w.writeheader(); w.writerows(frame_rows)
    print(f"  uart   : {len(frame_rows):5d} vectors -> {out.name}")

    # --- decode side ------------------------------------------------------
    parser = pm.StrictTM7565Parser()
    event_fields = [f.name for f in fields(pm.DecodeEvent)] if is_dataclass(pm.DecodeEvent) else []
    dec_rows = []
    for capture in SAMPLE_CAPTURES:
        path = CAPTURES / capture
        if not path.is_file():
            print(f"  WARN   missing capture {capture}")
            continue
        taken = 0
        with path.open(newline="", encoding="utf-8-sig") as f:
            for row in csv.DictReader(f):
                if taken >= LINES_PER_CAPTURE:
                    break
                raw = (row.get("raw_line") or "").strip()
                if not raw:
                    continue
                rec = {"source": capture, "raw_line": raw}
                try:
                    frame = parser.parse(raw, now=0.0)
                except ValueError as exc:
                    rec["parse_error"] = str(exc)
                    dec_rows.append(rec); taken += 1
                    continue
                rec["parse_error"] = ""
                rec["frame_can_id"] = f"0x{frame.can_id:03X}"
                rec["frame_dlc"] = frame.dlc
                event = pm.decode_position_frame(frame)
                rec["decoded"] = "1" if event is not None else "0"
                for name in event_fields:
                    rec[name] = "" if event is None else getattr(event, name)
                dec_rows.append(rec); taken += 1
        print(f"  decode : {taken:5d} lines from {capture}")

    cols = ["source", "raw_line", "parse_error", "frame_can_id", "frame_dlc", "decoded"] + event_fields
    out = HERE / "golden_decode_vectors.csv"
    with out.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, cols, extrasaction="ignore")
        w.writeheader(); w.writerows(dec_rows)
    print(f"  decode : {len(dec_rows):5d} vectors -> {out.name}")

    pm.run_self_test()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
