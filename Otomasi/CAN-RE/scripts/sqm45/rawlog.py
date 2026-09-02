"""Raw frame logger: every frame on the wire, in the dataset's own format.

Writes exactly the header the 201 existing captures use --

    timestamp_s,can_id,dlc,data_hex,raw_line

-- so a file recorded at a plant drops straight into captures/ and every
analysis script (analyze_dataset.py, master_decode_analysis.py, the replay
source) reads it with no conversion. A sniffer that invents its own format
would strand its own evidence.

Unlike engine.start_recording(), which records DECODED position events, this
records the bus. Anything the decoder does not understand -- which, on a bus
with servos we have never met, is the whole point -- is preserved verbatim.

write() is called from the RX thread and must never raise: a dead reader
thread looks exactly like dead hardware. Errors are latched into
last_error and the writer disables itself rather than propagating.
"""

from __future__ import annotations

import csv
import threading
import time
from datetime import datetime
from pathlib import Path

HEADER = ["timestamp_s", "can_id", "dlc", "data_hex", "raw_line"]
FLUSH_EVERY = 200        # frames between fsync-free flushes
FLUSH_INTERVAL_S = 2.0   # ...or this long, whichever comes first


class RawLogWriter:
    """Append-only CSV of every received frame. Thread-safe, fail-soft."""

    def __init__(self, directory: Path, prefix: str = "sniff"):
        self.directory = Path(directory)
        self.prefix = prefix
        self._lock = threading.Lock()
        self._fh = None
        self._writer = None
        self.path: Path | None = None
        self.frames_written = 0
        self.started_at: float | None = None
        self.last_error: str | None = None
        self._since_flush = 0
        self._last_flush = 0.0

    @property
    def active(self) -> bool:
        return self._fh is not None

    def suggested_name(self, note: str = "") -> str:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        slug = "".join(c if c.isalnum() or c in "-_" else "_" for c in note)[:40]
        return f"{self.prefix}_{stamp}{'_' + slug if slug else ''}.csv"

    def start(self, note: str = "", dest: Path | None = None) -> Path:
        """Begin recording. `dest` is the operator's chosen file, when they
        picked one; otherwise a timestamped name under `directory`."""
        with self._lock:
            if self._fh is not None:
                return self.path
            if dest is not None:
                self.path = Path(dest)
                self.path.parent.mkdir(parents=True, exist_ok=True)
            else:
                self.directory.mkdir(parents=True, exist_ok=True)
                self.path = self.directory / self.suggested_name(note)
            # newline="" is required or csv writes \r\r\n on Windows and every
            # downstream reader sees a blank line between frames.
            self._fh = open(self.path, "w", newline="", encoding="utf-8")
            self._writer = csv.writer(self._fh)
            self._writer.writerow(HEADER)
            self.frames_written = 0
            self.started_at = time.monotonic()
            self.last_error = None
            self._since_flush = 0
            self._last_flush = time.monotonic()
            return self.path

    def write(self, frame) -> None:
        if self._fh is None:
            return
        try:
            with self._lock:
                if self._writer is None:
                    return
                self._writer.writerow([
                    f"{time.time():.6f}",
                    f"0x{frame.can_id:03X}",
                    frame.dlc,
                    frame.data.hex().upper(),
                    frame.raw_line,
                ])
                self.frames_written += 1
                self._since_flush += 1
                now = time.monotonic()
                if self._since_flush >= FLUSH_EVERY or (now - self._last_flush) >= FLUSH_INTERVAL_S:
                    self._fh.flush()
                    self._since_flush = 0
                    self._last_flush = now
        except Exception as exc:      # disk full, handle closed under us, ...
            self.last_error = str(exc)
            self._safe_close()

    def stop(self) -> Path | None:
        with self._lock:
            path = self.path
            self._close_locked()
            return path

    def _safe_close(self) -> None:
        try:
            self._close_locked()
        except Exception:
            pass

    def _close_locked(self) -> None:
        if self._fh is not None:
            try:
                self._fh.flush()
                self._fh.close()
            finally:
                self._fh = None
                self._writer = None

    def status(self) -> dict:
        return {
            "active": self.active,
            "path": str(self.path) if self.path else None,
            "name": self.path.name if self.path else None,
            "frames": self.frames_written,
            "elapsed_s": (time.monotonic() - self.started_at) if self.started_at else 0.0,
            "error": self.last_error,
        }
