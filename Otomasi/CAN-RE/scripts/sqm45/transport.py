"""CAN adapters: turn a specific wire format into sqm45.protocol.CANFrame.

TM7565Transport is the ICP DAS tM-7565 USB-CAN converter's ASCII UART
protocol, proven on the bench (2026-08-14 onward). A second adapter -- an
ESP32 + MCP2515 board -- is planned as a cheaper/more available alternative
CAN-USB interface. CanTransport is the seam it plugs into: sqm45.protocol and
sqm45.replay never import this module or know a wire format, so a second
implementation of CanTransport is the only thing a second adapter requires.
"""

from __future__ import annotations

import abc
import re
import threading
import time
from typing import Callable

from .protocol import CANFrame


HEX_RE = re.compile(r"^[0-9A-Fa-f]*$")

OnFrame = Callable[[CANFrame], None]
OnUnparsed = Callable[[str, str], None]  # (raw_text, error)


class CanTransport(abc.ABC):
    """A CAN adapter: writes (can_id, dlc, payload) and delivers CANFrame."""

    @abc.abstractmethod
    def open(self, on_frame: OnFrame, on_unparsed: OnUnparsed) -> None:
        """Start the adapter. on_frame fires per decoded CAN frame;
        on_unparsed(raw_text, error) fires per line that was not a CAN frame
        (a device error reply, a malformed line, ...)."""

    @abc.abstractmethod
    def close(self) -> None:
        """Stop the adapter and release the underlying link."""

    @abc.abstractmethod
    def write_frame(self, can_id: int, dlc: int, data_hex: str) -> None:
        """Transmit one CAN frame. data_hex is uppercase hex, no separators,
        exactly 2*dlc characters (empty string when dlc == 0)."""

    @property
    @abc.abstractmethod
    def source_label(self) -> str:
        """Short, human-facing description of what's actually plugged in --
        e.g. 'Port COM12 tM-7565'. A UI showing live data without saying
        where it came from is how a replayed capture gets mistaken for a
        live bus, or vice versa."""


class StrictTM7565Parser:
    """Strict ICP DAS UART-frame parser.

    Supported receive strings match the device manual and captured evidence:
    ``t`` standard data, ``T`` standard remote, ``e`` extended data and ``E``
    extended remote. Payload length must be exactly DLC*2 hexadecimal chars.
    Timestamp-appended/device-checksum modes must be disabled for this link.
    """

    def parse(self, line: str, now: float | None = None) -> CANFrame:
        text = line.strip()
        if not text:
            raise ValueError("empty serial line")
        kind = text[0]
        if kind not in {"t", "T", "e", "E"}:
            raise ValueError(f"unsupported tM frame kind {kind!r}")
        extended = kind in {"e", "E"}
        remote = kind in {"T", "E"}
        id_len = 8 if extended else 3
        minimum = 1 + id_len + 1
        if len(text) < minimum:
            raise ValueError("truncated identifier/DLC")
        id_hex = text[1 : 1 + id_len]
        dlc_hex = text[1 + id_len]
        rest = text[minimum:]
        if not HEX_RE.fullmatch(id_hex) or not HEX_RE.fullmatch(dlc_hex):
            raise ValueError("non-hex identifier/DLC")
        can_id = int(id_hex, 16)
        dlc = int(dlc_hex, 16)
        if not 0 <= dlc <= 8:
            raise ValueError(f"invalid Classical-CAN DLC {dlc}")
        if remote:
            if rest:
                raise ValueError("remote frame unexpectedly carries payload/timestamp")
            data = b""
        else:
            if len(rest) != 2 * dlc:
                raise ValueError(f"payload has {len(rest)} hex chars, expected {2*dlc}")
            if not HEX_RE.fullmatch(rest):
                raise ValueError("payload contains non-hex characters")
            data = bytes.fromhex(rest)
        return CANFrame(
            received_monotonic=time.monotonic() if now is None else now,
            can_id=can_id,
            dlc=dlc,
            data=data,
            raw_line=text,
            extended=extended,
            remote=remote,
        )


class TM7565Transport(CanTransport):
    """ICP DAS tM-7565 over its ASCII UART protocol.

    Wire format: ``t{id:03X}{dlc}{data_hex}\\r`` to send, the same shape
    received back for every CAN frame the converter sees. A non-CAN reply
    (``?...`` error, ``!...`` status) is handed to on_unparsed rather than
    raised -- a converter error is bench-relevant, not a bug in this code.

    The CAN bitrate is NOT set here: it lives inside the tM-7565 and is set
    with the ICP DAS VxCAN Utility. This class only speaks the UART link.
    """

    def __init__(self, port: str, baud: int = 115200):
        self.port_name = port
        self.baud = baud
        self._serial = None
        self._parser = StrictTM7565Parser()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.callback_errors = 0

    @property
    def source_label(self) -> str:
        return f"Port {self.port_name} tM-7565"

    def open(self, on_frame: OnFrame, on_unparsed: OnUnparsed) -> None:
        import serial

        # A previous session may still be live here -- the engine restarts
        # after a TX fault, or any caller that reopened without an explicit
        # close(). Reopening straight over it leaked the old COM handle
        # (Windows then refuses the second open with ACCESS_DENIED forever)
        # AND orphaned the old RX thread onto the NEW serial object, letting
        # two readers interleave and corrupt line reassembly. Tear the old
        # session down completely before claiming the port.
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass

        # write_timeout matters as much as timeout: without it a wedged USB
        # stack blocks inside pyserial's GetOverlappedResult FOREVER (bWait
        # is unconditional on Windows), the TX thread dies silently and the
        # UI keeps reporting "active" over a bus nobody transmits on -- the
        # exact signature that once looked like dead hardware.
        self._serial = serial.Serial(port=self.port_name, baudrate=self.baud,
                                      bytesize=8, parity="N", stopbits=1,
                                      timeout=0.05, write_timeout=0.05)
        # This only clears the PC-side receive queue. It sends no UART/CAN data.
        self._serial.reset_input_buffer()
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._run_rx, args=(on_frame, on_unparsed), daemon=True, name="TM7565Rx")
        self._thread.start()

    def close(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        if self._serial is not None:
            try:
                self._serial.close()
            except Exception:
                pass

    def write_frame(self, can_id: int, dlc: int, data_hex: str) -> None:
        self._serial.write(f"t{can_id:03X}{dlc}{data_hex.upper()}\r".encode("ascii"))

    def _run_rx(self, on_frame: OnFrame, on_unparsed: OnUnparsed) -> None:
        buf = b""
        while not self._stop_event.is_set():
            try:
                chunk = self._serial.read(256)
            except Exception as exc:
                if self._stop_event.is_set():
                    return   # our own close() -- deliberate, not a fault
                # The read side used to die silently here: an unplugged
                # adapter or driver hiccup looked EXACTLY like a silent
                # actuator, the ambiguity this project was burned by once
                # already (a dead reader thread masquerading as dead
                # hardware). Fail loud through the same channel unparsed
                # lines take; reception cannot continue either way.
                try:
                    on_unparsed("<RX thread>", f"RX STOPPED -- {type(exc).__name__}: {exc}")
                except Exception:
                    self.callback_errors += 1
                return
            if not chunk:
                continue
            buf += chunk
            while b"\r" in buf:
                line, buf = buf.split(b"\r", 1)
                text = line.decode("ascii", errors="replace").strip()
                if not text:
                    continue
                try:
                    frame = self._parser.parse(text)
                except ValueError as exc:
                    try:
                        on_unparsed(text, str(exc))
                    except Exception:
                        self.callback_errors += 1
                    continue
                try:
                    on_frame(frame)
                except Exception:
                    # A dead RX thread makes a live actuator look silent for the
                    # rest of the session. Never let one callback kill reception.
                    self.callback_errors += 1


class ESP32Transport(TM7565Transport):
    """ESP32 + MCP2515 bridge running Hardware/esp32-can-bridge.

    The wire format is byte-identical to the tM-7565 -- verified on the bench
    2026-08-19 with both adapters wired to the same SQM45 bus: the ESP32
    reproduced every frame the tM-7565 transmitted (119/119) and its own
    transmissions were confirmed on the wire by the tM-7565, with 100% byte-7
    integrity on both sides. So the parser and framing are inherited whole and
    only the differences below are overridden.

    Two things genuinely differ from the tM-7565:

    1. Opening the port RESETS the board (the CP210x DTR/RTS auto-reset
       circuit), so it replays the ESP32 ROM bootloader chatter plus its own
       banner -- about 17 non-CAN lines -- and cannot bridge anything for
       roughly the first 0.8 s. BOOT_SETTLE_S covers that, otherwise the first
       second of transmission is silently dropped on the floor.
    2. It is a different device, so it must say so in source_label.
    """

    BOOT_SETTLE_S = 1.2

    # Lines the board is EXPECTED to emit on every open: ESP32 ROM bootloader
    # chatter plus the sketch banner. Reporting these as converter faults
    # trains the operator to ignore the fault log, which is the one place a
    # real converter error has to be visible.
    BOOT_NOISE = re.compile(
        r"^(ets |rst:0x|configsip:|clk_drv:|mode:DIO|load:0x|ho \d|entry 0x"
        r"|ESP32 CAN Bridge|Protocol: tM-7565|CAN bitrate:|MCP2515 init"
        r"|Ready\. Forwarding|Entering Configuration|Setting Baudrate)")

    def __init__(self, port: str, baud: int = 115200):
        super().__init__(port, baud)
        self.boot_lines: list[str] = []   # recorded, just not raised as faults

    @property
    def source_label(self) -> str:
        return f"Port {self.port_name} ESP32"

    def open(self, on_frame: OnFrame, on_unparsed: OnUnparsed) -> None:
        def filtered(raw_text: str, error: str) -> None:
            if self.BOOT_NOISE.match(raw_text):
                self.boot_lines.append(raw_text)
                return
            on_unparsed(raw_text, error)

        super().open(on_frame, filtered)
        # Opening reset the board. Give it time to finish booting before any
        # caller starts transmitting into a device that is not listening yet.
        time.sleep(self.BOOT_SETTLE_S)


# USB VID:PID -> adapter class. Lets any tool take a bare --port and do the
# right thing instead of making the operator remember which box is on which
# COM number.
_ADAPTER_BY_VIDPID = {
    (0x1B5C, 0x0109): TM7565Transport,   # ICP DAS tM-7565
    (0x10C4, 0xEA60): ESP32Transport,    # Silicon Labs CP210x (ESP32 DevKit)
    (0x1A86, 0x7523): ESP32Transport,    # CH340 (common ESP32 clone)
    (0x1A86, 0x55D4): ESP32Transport,    # CH9102
}


def transport_for_port(port: str, baud: int = 115200) -> CanTransport:
    """Return the right adapter for `port`, identified by USB VID:PID.

    Falls back to TM7565Transport for an unrecognised device: the ASCII
    framing is what actually matters and both known adapters share it, so the
    fallback is the compatible choice rather than a refusal.
    """
    try:
        from serial.tools import list_ports
        for info in list_ports.comports():
            if info.device.upper() != port.upper():
                continue
            if info.vid is not None and info.pid is not None:
                cls = _ADAPTER_BY_VIDPID.get((info.vid, info.pid))
                if cls is not None:
                    return cls(port, baud)
            break
    except Exception:
        pass
    return TM7565Transport(port, baud)


def run_self_test() -> None:
    """Parser correctness, verified end-to-end through protocol.decode_position_frame."""
    from . import protocol

    parser = StrictTM7565Parser()
    samples = {
        "air_target_0": ("t1816FF9B00006B8C", 0x181, 0.0),
        "fuel_target_50": ("t1A16FDA700006F74", 0x1A1, 50.0),
        "air_actual_0": ("t18A8306453E65A45153F", 0x18A, 0.0),
        "fuel_actual_50": ("t18B8325851F45A841922", 0x18B, 50.0),
    }
    for name, (raw, expected_id, expected_angle) in samples.items():
        frame = parser.parse(raw, now=1.0)
        assert frame.can_id == expected_id, name
        event = protocol.decode_position_frame(frame)
        assert event is not None and event.angle_deg is not None, name
        assert abs(event.angle_deg - expected_angle) < 1e-9, (name, event.angle_deg)
        if event.kind == "target":
            assert event.integrity_ok is True, name
        if event.kind == "actual":
            assert event.candidate_integrity_ok is True, name
    invalid = [
        "t18A83064",                         # truncated
        "t18A8306453E65A45153F00",           # trailing bytes
        "t18A8306453E65A4515GG",             # non-hex
        "x18A8306453E65A45153F",             # unsupported kind
    ]
    for raw in invalid:
        try:
            parser.parse(raw)
        except ValueError:
            pass
        else:
            raise AssertionError(f"invalid line accepted: {raw}")
    print("PASS: strict tM-7565 parser and four confirmed position decodes")


if __name__ == "__main__":
    run_self_test()
