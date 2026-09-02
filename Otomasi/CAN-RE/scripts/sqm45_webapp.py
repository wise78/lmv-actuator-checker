"""SQM45 dashboard -- Flask + SocketIO launcher.

Two ways to run it:
    --replay <capture.csv>   no hardware: replays a real capture from
                             captures/raw/ as if it were live RX. Safe
                             anywhere -- it never transmits.
    --port COM12             real tM-7565 on the isolated bench. Transmits
                             the LMV51's own identifiers once you switch the
                             dashboard to KEEP ALIVE -- isolated bench only,
                             same rule as sqm45_boot_replay.py.

    --listen-only            lock the whole session to receive-only. The
                             KEEP ALIVE toggle is refused by the server, not
                             merely greyed out. Use it on any bus you do not
                             own -- a plant LMV line, a live burner.

Pages: Dashboard, Servo Checker, Health Check, and Raw Sniffer. All four
render whatever the bus actually has: the roster is server-owned
(sqm45/roster.py) and covers the LMV5's full ACT1-6, so a four- or six-servo
line needs no code change. Raw Sniffer goes further and renders identifiers
nothing in this codebase knows about, scoring each on how much it looks like
actuator feedback -- that is the page for a bus whose servo count and
addressing you have not confirmed yet.

Addresses 1-4 (air/gas/oil/aux1) are commandable: their target identifier AND
integrity constant have each been observed driving real hardware. Addresses
5-6 are monitor-only by construction, not by policy -- the target channel
field is two bits wide and 0x1E1 is its last value, so no 5th or 6th target
identifier exists to extrapolate.

Usage:
    python sqm45_webapp.py --replay simul_both_fuel_10_80_air_80_10_r1.csv
    python sqm45_webapp.py --port COM12
    python sqm45_webapp.py --port COM12 --listen-only     # plant / foreign bus
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
if str(SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPT_DIR))

from sqm45.replay_source import FileReplaySource  # noqa: E402
from sqm45.transport import transport_for_port  # noqa: E402
from sqm45_web.app import create_app  # noqa: E402

CAPTURES = SCRIPT_DIR.parent / "captures" / "raw"


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    src = parser.add_mutually_exclusive_group(required=True)
    src.add_argument("--replay", metavar="CAPTURE.CSV",
                      help="filename under captures/raw/, replayed as live RX -- no hardware")
    src.add_argument("--port", help="adapter COM port (tM-7565 or ESP32 bridge, auto-detected) -- isolated bench only")
    parser.add_argument("--baud", type=int, default=115200, help="UART baud to the converter (default 115200)")
    parser.add_argument("--speed", type=float, default=1.0, help="--replay only: playback speed multiplier")
    parser.add_argument("--loop", action="store_true", help="--replay only: loop the capture")
    parser.add_argument("--listen-only", action="store_true",
                        help="lock the session to receive-only; KEEP ALIVE is refused "
                             "for the life of the process (use on any bus you do not own)")
    parser.add_argument("--record", action="store_true",
                        help="with --listen-only: start the raw frame CSV the moment the bus "
                             "opens, so a one-shot event (an LMV boot scan, an actuator being "
                             "addressed) cannot be missed while nobody has clicked record")
    parser.add_argument("--record-note", default="",
                        help="label folded into the raw log filename")
    parser.add_argument("--output-dir", metavar="DIR",
                        help="where the Save-as dialogs OPEN (reports, census JSON, raw "
                             "captures). Defaults to 'LMV Actuator Checker' in your Documents "
                             "folder. The operator's choice in the dialog always wins; this "
                             "only sets the starting folder, and is the fallback on a host "
                             "with no dialog available.")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--http-port", type=int, default=5000)
    args = parser.parse_args()

    if args.replay:
        # A bare filename means the curated corpus; a path means exactly that
        # file, so a sniffer recording can be replayed without being copied
        # into captures/raw/ (which would silently join the labelled dataset).
        candidate = Path(args.replay)
        path = candidate if candidate.is_absolute() or candidate.parent != Path(".") else CAPTURES / args.replay
        if not path.is_file():
            print(f"ERROR: capture not found: {path}", file=sys.stderr)
            return 2
        print(f"Replaying {args.replay} at {args.speed:.0f}x -- no hardware, nothing transmitted")
        transport = FileReplaySource(path, speed=args.speed, loop=args.loop)
    else:
        # Adapter type comes from the port's USB VID:PID -- tM-7565 and the
        # ESP32 bridge speak the same ASCII framing but the ESP32 needs a boot
        # settle after the port opens (opening it resets the board).
        transport = transport_for_port(args.port, args.baud)
        if args.listen_only:
            print(f"*** {transport.source_label}: LISTEN ONLY -- transmission locked off "
                  "for this whole session ***")
            print("Nothing is sent to the bus. Set the adapter itself to listen-only too "
                  "if it supports it: at the wrong bitrate a normal-mode node emits error "
                  "frames onto the bus it is only meant to be watching.")
        else:
            print(f"*** {transport.source_label}: TRANSMITS LMV51 IDENTIFIERS "
                  "ONCE YOU SWITCH TO KEEP ALIVE ***")
            print("Isolated bench only: no LMV51, no burner, no fuel, no fan. Shaft uncoupled.")

    app, socketio, engine = create_app(transport, listen_only=args.listen_only,
                                       output_dir=args.output_dir)

    # A --listen-only session has exactly one safe mode, so waiting for
    # someone to click into it only creates a way to miss the LMV's boot SDO
    # scan -- which happens once per power cycle and is the cheapest servo
    # inventory on the bus.
    if args.listen_only:
        try:
            engine.start("listen")
            print("Bus opened in LISTEN mode automatically (--listen-only).")
            if args.record:
                # No dialog here on purpose: --record exists so an unattended
                # session cannot miss a one-shot event, and a modal dialog would
                # defeat that entirely. Goes to the default output folder.
                print("Recording raw frames to", engine.start_raw_log(args.record_note))
        except Exception as exc:
            print(f"WARNING: could not open the bus automatically: {exc}", file=sys.stderr)

    print(f"\nSQM45 dashboard: http://{args.host}:{args.http_port}/")
    try:
        socketio.run(app, host=args.host, port=args.http_port)
    finally:
        if engine.mode != "idle":
            engine.shutdown_bus()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
