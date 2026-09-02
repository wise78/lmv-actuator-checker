"""Replay a captured raw_line/timestamp_s CSV as if it were live RX.

NOT a CanTransport: this is receive-only, sourced from a file of frames that
already happened. write_frame() exists only so replay.transmit() can drive
BusEngine against it unmodified -- what it "sends" is recorded, never
transmitted, because there is nothing on the other end to receive it.

This is what lets the engine, the health check, and every UI widget be built
and regression-tested with no converter and no actuator: 201 captures in
captures/raw/ become a stand-in bench.
"""

from __future__ import annotations

import csv
import threading
import time
from pathlib import Path

from .transport import OnFrame, OnUnparsed, StrictTM7565Parser


class FileReplaySource:
    def __init__(self, path: Path, speed: float = 1.0, loop: bool = False):
        self.path = path
        self.speed = speed          # 1.0 = recorded real time; >1 = faster
        self.loop = loop
        self._parser = StrictTM7565Parser()
        self._thread: threading.Thread | None = None
        self._stop_event = threading.Event()
        self.sent: list[tuple[int, int, str]] = []   # what transmit() "wrote" -- display only
        self.frames_replayed = 0
        self.finished = threading.Event()

    @property
    def source_label(self) -> str:
        return "CSV"

    def open(self, on_frame: OnFrame, on_unparsed: OnUnparsed) -> None:
        self._stop_event.clear()
        self.finished.clear()
        self._thread = threading.Thread(
            target=self._run, args=(on_frame, on_unparsed), daemon=True, name="FileReplayRx")
        self._thread.start()

    def close(self) -> None:
        self._stop_event.set()
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)

    def write_frame(self, can_id: int, dlc: int, data_hex: str) -> None:
        self.sent.append((can_id, dlc, data_hex))

    def _run(self, on_frame: OnFrame, on_unparsed: OnUnparsed) -> None:
        with self.path.open(newline="", encoding="utf-8-sig") as f:
            rows = [r for r in csv.DictReader(f) if (r.get("raw_line") or "").strip()]
        if not rows:
            self.finished.set()
            return
        origin = float(rows[0]["timestamp_s"])
        while not self._stop_event.is_set():
            t_start = time.monotonic()
            for row in rows:
                if self._stop_event.is_set():
                    return
                due = t_start + (float(row["timestamp_s"]) - origin) / self.speed
                while True:
                    if self._stop_event.is_set():
                        return
                    remaining = due - time.monotonic()
                    if remaining <= 0:
                        break
                    time.sleep(min(remaining, 0.02))
                raw = row["raw_line"].strip()
                try:
                    frame = self._parser.parse(raw)
                except ValueError as exc:
                    on_unparsed(raw, str(exc))
                    continue
                on_frame(frame)
                self.frames_replayed += 1
            if not self.loop:
                break
        self.finished.set()
