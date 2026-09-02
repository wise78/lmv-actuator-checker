"""Hold sqm45.protocol / sqm45.transport to the behaviour frozen by
make_golden_vectors.py, forever. A before/after diff disappears once the
refactor lands; this fixture does not.

    python sqm45/tests/test_protocol_parity.py
"""
from __future__ import annotations

import csv
import sys
from dataclasses import fields as dc_fields
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from sqm45 import protocol, transport  # noqa: E402


def check_encode() -> int:
    path = HERE / "golden_encode_vectors.csv"
    n = 0
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            angle = float(row["angle_deg"])
            can_id = int(row["can_id"], 16)
            state = int(row["state"], 16)
            got = protocol.encode_target(angle, can_id, state)
            expected = row["payload"]
            assert got == expected, (row, got)
            n += 1
    print(f"  encode : {n:5d} vectors match")
    return n


def check_uart() -> int:
    """golden_uart_vectors.csv recorded the old Frame.uart() with its
    trailing \\r stripped for CSV storage. TM7565Transport.write_frame()
    still sends the \\r on the wire -- only this comparison strips it."""
    path = HERE / "golden_uart_vectors.csv"
    n = 0
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            can_id = int(row["can_id"], 16)
            dlc = int(row["dlc"])
            data = row["data"]
            expected = row["uart"]
            got = f"t{can_id:03X}{dlc}{data.upper()}"
            assert got == expected, (row, got)
            n += 1
    print(f"  uart   : {n:5d} vectors match")
    return n


def check_decode() -> int:
    path = HERE / "golden_decode_vectors.csv"
    parser = transport.StrictTM7565Parser()
    event_fields = [f.name for f in dc_fields(protocol.DecodeEvent)]
    n = 0
    with path.open(newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            raw = row["raw_line"]
            try:
                frame = parser.parse(raw, now=0.0)
            except ValueError as exc:
                assert row["parse_error"] == str(exc), (row, str(exc))
                n += 1
                continue
            assert row["parse_error"] == "", (row, "expected a parse error")
            assert row["frame_can_id"] == f"0x{frame.can_id:03X}", row
            assert int(row["frame_dlc"]) == frame.dlc, row
            event = protocol.decode_position_frame(frame)
            assert row["decoded"] == ("1" if event is not None else "0"), row
            if event is not None:
                for name in event_fields:
                    # Fields ADDED to DecodeEvent after these vectors were frozen
                    # have no column here. The fixture's job is to prove nothing
                    # it captured has regressed; a new additive field is not a
                    # regression, and failing on it would only push people to
                    # regenerate the baseline, which defeats the whole point.
                    if name not in row:
                        continue
                    got = getattr(event, name)
                    got_text = "" if got is None else str(got)
                    assert got_text == row[name], (row["source"], name, got_text, row[name])
            n += 1
    print(f"  decode : {n:5d} vectors match")
    return n


def main() -> int:
    print("protocol parity vs frozen golden vectors")
    total = check_encode() + check_uart() + check_decode()
    protocol.run_self_test()
    transport.run_self_test()
    print(f"PASS: {total} golden vectors reproduced exactly, both self-tests pass")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
