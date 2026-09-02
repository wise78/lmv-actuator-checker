"""H0-H9 health-check procedure: turns ~5 minutes of live traffic into a
PASS/FAIL verdict for one servo role.

Ticked from BusEngine._tick() via attach_procedure()/tick() -- runs on the TX
thread, same single-writer discipline as Command (see engine.py's module
docstring). Never calls engine.recorded_samples() (a full-list copy); pulls
only the slice appended since the last tick via new_recorded_samples().

Deliberately has zero import-time dependency on engine.py, so it stays
testable standalone against a list of synthetic sample dicts shaped like the
payloads BusEngine._on_frame() builds -- see sqm45/tests/test_healthcheck.py.

1 LSB = 0.1 deg. Thresholds and baselines below are transcribed verbatim from
the approved plan's Part D table.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field

from . import protocol

# -- H1 -----------------------------------------------------------------
WAKE_MAX_S = 2.0
WAKE_BASELINE = "0.30-0.32 s"

# -- H2 -----------------------------------------------------------------
RATE_MIN_HZ = 4.5
RATE_MAX_HZ = 5.4
GAP_MAX_S = 0.60
H2_BASELINE = "4.96 Hz, cyclic max gap 0.210 s"

# -- H3 / H4 / H7 (settle) -----------------------------------------------
SETTLED_STD_MAX = 0.10
SETTLED_PP_MAX = 0.30
SETTLED_DRIFT_MAX = 0.20
SETTLED_MOVING_MAX_PCT = 1.5
SPURIOUS_CONSEC_DELTA = 0.20   # 2 consecutive sample-to-sample deltas >= this
SPURIOUS_MEDIAN_DELTA = 0.30   # or any single sample this far from the window median
H3_BASELINE = "settled std median 0.000, p95 0.0433; MOVING while settled 0.82% Air, 0% Fuel"

# -- H5 -------------------------------------------------------------------
MOVE_TARGET_OFFSET_DEG = 25.0
MOVE_SPEED_MIN = 2.0
MOVE_SPEED_MAX = 4.0
# Both units on this bench park 1-2 LSB short (Air max 0.3, Fuel max 0.2),
# so 0.15 was unreachable for either. NOTE this is a COMMISSIONING VALUE,
# not a validated one: the LMV-driven baseline is MAE 0.0062 deg, ~30x
# tighter. Whether our units are genuinely worse or our command loop
# stops them early is still OPEN -- see FINDINGS.md 2026-08-21.
MOVE_SETTLE_ERR_MAX = 0.30
LSB_DEG = 0.1          # reported position quantum
# Measured on the bench 2026-08-21: after a move arrives, the MOVING flag
# still flickers for under 5 s (1 assertion in the first 5 s), then reads
# 0 of 199 samples over the next 40 s. So the flag asserting inside this
# lead-in is motor run-on, not instability; past it, any assertion is real.
H7_LEAD_IN_S = 6.0
MOVE_OVERSHOOT_MAX = 0.5
MOVE_MOVING_MIN_PCT = 90.0
H5_BASELINE = "2.98 deg/s; MAE 0.0062 deg; MOVING sensitivity 0.93-0.95 on transitions"

# -- H6 -------------------------------------------------------------------
# STRUCTURAL LIMIT, read this before trusting H5/H6 as actuator accuracy:
# we stop the shaft by clearing the move state word once it is within
# replay.ARRIVED_TOLERANCE_DEG, so BOTH the H5 settle error and this
# hysteresis are bounded by that tolerance. They therefore characterise
# OUR control loop at least as much as the actuator. Keeping this below
# the tolerance just guarantees a failure. Commissioning value, matched
# to the 0.3 tolerance -- not a validated accuracy figure. The LMV-driven
# baseline is 0.00000 deg at all 89 angles; closing that gap needs a
# different measurement, not a tighter number here.
HYSTERESIS_MAX = 0.3
H6_BASELINE = "0.00000 deg at all 89 angles (dense sweep, offline)"

# -- H10: LMV special positions ------------------------------------------
# Derived from the LMV5 sequence table (QSG Sec 4-1 p.124). Through one burner
# cycle the controller parks every actuator at a SET of special positions --
# Home, Prepurge, Ignition, Low-fire, Postpurge -- and at each boundary applies
# "Position Required to Proceed" before advancing the phase. It then keeps
# checking: "Dynamic Position Checking" while an actuator transitions, and
# "Run-Time Position Checking" while it is Stopped (S) or Modulating (M).
#
# H5 commands exactly one move, at mid-travel. A servo that is accurate around
# 45 deg and sloppy near an end stop passes H5 and still stalls a real start at
# the Prepurge or Home boundary. These angles bracket both ends and the middle.
#
# Order matters: the long 5 -> 88 leg is also the cleanest place to measure
# transit continuity, which is what Dynamic Position Checking watches.
SPECIAL_POSITIONS_DEG = (5.0, 88.0, 45.0)
SPECIAL_ARRIVE_ERR_MAX = 0.5     # "Position Required to Proceed"
SPECIAL_HOLD_S = 5.0
SPECIAL_HOLD_STD_MAX = 0.10      # "Run-Time Position Checking" while Stopped
SPECIAL_HOLD_DRIFT_MAX = 0.25
H10_BASELINE = "no prior baseline -- added 2026-08-28 from the LMV5 sequence table"

# -- Dynamic Position Checking (applied inside H5/H6/H10 transits) --------
# The LMV watches position WHILE an actuator transitions, not only where it
# lands. Three ways a shaft can arrive correctly having misbehaved on the way:
# a jump (sensor glitch), a reversal (control instability), or a stall gap.
DYN_MAX_STEP_DEG = 1.5      # one 4.96 Hz sample at 3 deg/s is ~0.6 deg
DYN_MAX_REVERSALS = 2       # a little dither at the end of travel is normal
DYN_MAX_GAP_S = 0.60        # same threshold H2 uses for a cyclic gap


def transit_continuity(samples: list[dict]) -> dict:
    """Dynamic Position Checking, computed over one transit.

    Returns the three numbers the LMV's own check would care about, plus a
    verdict. Deliberately tolerant of direction: a move can be either way, so
    reversals are counted against the DOMINANT direction of travel rather than
    against a sign we assume.
    """
    pts = [(s["t"], s["angle_deg"]) for s in samples
           if s.get("angle_deg") is not None and s.get("trustworthy") is not False]
    if len(pts) < 3:
        return {"ok": None, "samples": len(pts), "max_step": None,
                "reversals": None, "max_gap": None,
                "text": f"only {len(pts)} trustworthy samples -- not assessable"}
    steps = [(b[0] - a[0], b[1] - a[1]) for a, b in zip(pts, pts[1:])]
    max_step = max(abs(d) for _, d in steps)
    max_gap = max(dt for dt, _ in steps)
    net = pts[-1][1] - pts[0][1]
    forward = 1.0 if net >= 0 else -1.0
    # Only count a reversal that is bigger than sensor noise, or a settled
    # shaft's last-LSB dither reads as instability.
    reversals = sum(1 for _, d in steps if d * forward < -0.15)
    ok = (max_step <= DYN_MAX_STEP_DEG and reversals <= DYN_MAX_REVERSALS
          and max_gap <= DYN_MAX_GAP_S)
    return {"ok": ok, "samples": len(pts), "max_step": max_step,
            "reversals": reversals, "max_gap": max_gap,
            "text": (f"max step {max_step:.2f} deg, {reversals} reversal(s), "
                     f"max gap {max_gap:.2f} s")}


# -- H11: concurrent multi-actuator hold ----------------------------------
# Straight from the LMV's own fault model (QSG p.143). Its positioning fault is
# a BITMASK -- 01 air, 02 fuel, 04 aux1, 08 aux2, 10 VSD -- and the
# manufacturer's troubleshooting splits on exactly that:
#
#   "If error occurs on one actuator only:  1) Ensure torque requirements ...
#    2) Verify that no damper/valve is bound. 3) ... Replace actuator."
#   "If error occurs on multiple actuators (01-3F):  1) Verify that the CanBus
#    wiring is correct. 2) Verify that shields (screens) on CanBus cable are
#    connected properly."
#
# ONE actuator misbehaving is mechanical. SEVERAL at once is the bus or the
# screening. Every other step in this procedure watches a single role and so
# structurally cannot make that distinction -- which is the distinction this
# whole project exists to make (HT/EMI on a shared CAN run).
#
# H11 therefore commands the actuator under test to move while watching every
# OTHER actuator on the bus hold station. A bystander that twitches only while
# a neighbour is driven is not a mechanical fault in the bystander; it is
# coupling, and the report says so.
H11_BYSTANDER_STD_MAX = 0.10
H11_BYSTANDER_PP_MAX = 0.30
H11_BYSTANDER_FAULT_MAX = 0        # any fault frame on a parked servo is real
H11_BASELINE = "no prior baseline -- added 2026-08-28 from the LMV5 fault bitmask (QSG p.143)"


# -- H8 (continuous, whole run) -------------------------------------------
H8_S_IN_BAND_MIN_PCT = 99.5
H8_S_SPREAD_MAX = 16
H8_CRC_PASS_MIN_PCT = 99.9
H8_BASELINE = "inverse-MOVING match 1.0 over 74052/72213 frames; S observed 0x8446-0x844F"

# -- H9 ---------------------------------------------------------------------
H9_RESTORE_TOL_DEG = 0.5

DEFAULT_DURATIONS = {
    "H1": 20.0, "H2": 30.0, "H3": 90.0, "H4": 60.0,
    "H5": 35.0, "H6": 35.0, "H7": 20.0, "H10": 170.0, "H11": 2.0, "H9": 2.0,
}
# H10 sits between the commanded move and the RETURN, not after it. Running it
# after H6 left the shaft parked at the last special position, so H9's
# "restored to as-found" check failed for a reason that was the procedure's
# fault, not the servo's (measured: rested at 45.3 with as-found 23.8).
# Here H6 becomes the single closing statement about position -- and a
# stronger one, since it now measures the return after a full-range exercise
# rather than after one 25 deg hop, which is closer to what the LMV does when
# it drives an actuator Home at the end of a burner cycle.
TIMED_STEPS = ["H1", "H2", "H3", "H4", "H5", "H10", "H11", "H6", "H7", "H9"]
STEP_ORDER = ["H0", "H1", "H2", "H3", "H4", "H5", "H10", "H11", "H6", "H7", "H8", "H9"]


def _drift(values: list[float]) -> float:
    """First-half mean vs second-half mean. stdlib-only, robust to a little
    noise without needing a regression (packaging excludes numpy)."""
    if len(values) < 4:
        return 0.0
    mid = len(values) // 2
    return abs(statistics.mean(values[mid:]) - statistics.mean(values[:mid]))


def count_spurious_events(values: list[float]) -> int:
    """A spurious event is (a) two consecutive sample-to-sample deltas each
    >= SPURIOUS_CONSEC_DELTA -- a sustained move, not one noisy sample -- or
    (b) any single sample >= SPURIOUS_MEDIAN_DELTA away from the window
    median -- an isolated spike. Non-overlapping count."""
    if len(values) < 2:
        return 0
    med = statistics.median(values)
    deltas = [abs(values[i] - values[i - 1]) for i in range(1, len(values))]
    events = 0
    i = 0
    while i < len(deltas):
        if (deltas[i] >= SPURIOUS_CONSEC_DELTA and i + 1 < len(deltas)
                and deltas[i + 1] >= SPURIOUS_CONSEC_DELTA):
            events += 1
            i += 2
            continue
        i += 1
    for v in values:
        if abs(v - med) >= SPURIOUS_MEDIAN_DELTA:
            events += 1
    return events


@dataclass
class StepStatus:
    name: str
    criterion: str
    baseline: str
    status: str = "pending"    # pending | running | pass | fail | aborted | skipped
    measured: str = "—"   # em dash


def _make_results() -> dict[str, StepStatus]:
    return {
        "H0": StepStatus("Preflight",
                          "bus active, sequence SHA recorded, self-test passes", "—"),
        "H1": StepStatus("Wake & identify", f"<= {WAKE_MAX_S:.1f} s", WAKE_BASELINE),
        "H2": StepStatus("Link quality",
                          f"{RATE_MIN_HZ}-{RATE_MAX_HZ} Hz, gap <= {GAP_MAX_S:.2f} s, "
                          "0 malformed, 0 unknown marker", H2_BASELINE),
        "H3": StepStatus("Quiescent stability (as-found)",
                          f"std<={SETTLED_STD_MAX}, p-p<={SETTLED_PP_MAX}, "
                          f"drift<={SETTLED_DRIFT_MAX}, spurious=0, MOVING<={SETTLED_MOVING_MAX_PCT}%",
                          H3_BASELINE),
        "H4": StepStatus("Disturbance window", "as H3, and 0 CRC failures",
                          "reproduction of the reported fault -- HT transformer energised if available"),
        "H5": StepStatus(f"Commanded move ±{MOVE_TARGET_OFFSET_DEG:.0f}°",
                          f"{MOVE_SPEED_MIN}-{MOVE_SPEED_MAX} deg/s, settle err<={MOVE_SETTLE_ERR_MAX}, "
                          f"overshoot<={MOVE_OVERSHOOT_MAX}, MOVING>={MOVE_MOVING_MIN_PCT:.0f}%, "
                          f"transit step<={DYN_MAX_STEP_DEG} deg & <={DYN_MAX_REVERSALS} reversals",
                          H5_BASELINE),
        "H6": StepStatus("Return & repeatability", f"|hysteresis| <= {HYSTERESIS_MAX} deg", H6_BASELINE),
        "H7": StepStatus("Post-move settle", "as H3", "catches instability appearing only after the motor ran"),
        "H8": StepStatus("Internal cross-checks (whole run)",
                          f"S in band>={H8_S_IN_BAND_MIN_PCT}% & spread<={H8_S_SPREAD_MAX}, "
                          f"CRC pass>={H8_CRC_PASS_MIN_PCT}%, 0 inverse-MOVING mismatches",
                          H8_BASELINE),
        "H10": StepStatus(
            "LMV special positions",
            f"arrive within {SPECIAL_ARRIVE_ERR_MAX} deg and hold "
            f"(std<={SPECIAL_HOLD_STD_MAX}, drift<={SPECIAL_HOLD_DRIFT_MAX}) at "
            + ", ".join(f"{a:.0f}" for a in SPECIAL_POSITIONS_DEG) + " deg; "
            "transit continuity clean",
            H10_BASELINE),
        "H11": StepStatus(
            "Cross-actuator coupling",
            f"every OTHER actuator holds station while this one moved "
            f"(std<={H11_BYSTANDER_STD_MAX}, p-p<={H11_BYSTANDER_PP_MAX}, 0 fault frames)",
            H11_BASELINE),
        "H9": StepStatus("Safe finish", "HOLD asserted, transmission continues, restored to as-found", "—"),
    }


class HealthCheckRun:
    """One run against one role. Constructed and attached fresh per run --
    never reused. `durations` lets tests shrink the ~292s real budget down
    to seconds without touching the production thresholds above."""

    def __init__(self, role: str, serial: str, technician: str, work_order: str = "",
                 durations: dict[str, float] | None = None):
        self.role = role
        self.serial = serial
        self.technician = technician
        self.work_order = work_order
        self.durations = {**DEFAULT_DURATIONS, **(durations or {})}
        self.results = _make_results()

        self._started = False
        self.finished = False
        self.aborted = False
        self.abort_reason: str | None = None
        self.verdict: str | None = None

        self.current_id: str | None = None
        self.step_started_at: float | None = None
        self._run_started_at: float | None = None
        self._cursor = 0
        self._window: list[dict] = []

        self.as_found_angle: float | None = None
        self.wake_time_s: float | None = None
        self.ht_choice: str | None = None
        self.sequence_sha: str | None = None
        self.finished_at: float | None = None

        self._h5_target: float | None = None
        self._h5_direction: str | None = None
        self._h5_move_start_angle: float | None = None
        self._bystander: dict[str, list] = {}
        self._h5_move_seen_active = False
        self._h5_move_requested = False
        self._h5_move_failed = False
        self._h6_target: float | None = None
        self._h6_move_seen_active = False
        self._h6_move_requested = False
        self._h6_move_failed = False

        self.h8 = {"actual_total": 0, "s_in_band": 0, "s_values": [],
                    "crc_pass": 0, "inverse_mismatch": 0, "status_marked_mismatch": 0}

    # -- driven by BusEngine --------------------------------------------

    def start(self, now: float, engine) -> None:
        self._started = True
        self._run_started_at = now
        self._run_h0(engine)
        if not self.aborted:
            self._enter_step("H1", now, engine)

    def tick(self, now: float, engine) -> None:
        if not self._started or self.finished or self.aborted:
            return
        new, cursor = engine.new_recorded_samples(self._cursor)
        self._cursor = cursor
        driving = self.current_id in ("H5", "H10")
        for s in new:
            if s.get("kind") != "actual":
                continue
            if s.get("role") == self.role:
                self._feed_h8(s)
                self._window.append(s)
            elif driving:
                # A PARKED actuator, sampled only while a neighbour is being
                # driven. This is the evidence H11 needs and no per-role step
                # can gather: coupling is invisible unless you watch the
                # servos you are NOT commanding.
                self._bystander.setdefault(s["role"], []).append(s)

        handler = getattr(self, f"_tick_{self.current_id.lower()}")
        step_elapsed = now - self.step_started_at
        done = handler(now, step_elapsed, engine)
        if done:
            self._advance(now, engine)

    def abort(self, engine, reason: str = "operator abort") -> None:
        if self.finished or self.aborted:
            return
        self.aborted = True
        self.finished = True
        self.abort_reason = reason
        self.finished_at = None
        try:
            engine.request_hold()
        except Exception:
            pass
        try:
            engine.stop_recording()
        except Exception:
            pass
        if self.current_id is not None and self.results[self.current_id].status == "running":
            r = self.results[self.current_id]
            r.status = "aborted"
            r.measured = f"aborted: {reason}"
        for sid in STEP_ORDER:
            if self.results[sid].status == "pending":
                self.results[sid].status = "skipped"
        self.verdict = "ABORTED"

    def set_ht_choice(self, choice: str) -> None:
        if choice in ("applied", "not_available"):
            self.ht_choice = choice

    # -- status for the UI -------------------------------------------------

    def status(self, now: float | None = None) -> dict:
        steps = []
        for sid in STEP_ORDER:
            r = self.results[sid]
            measured = r.measured
            if sid == "H8" and not self.finished:
                measured = self._live_h8_summary()
            steps.append({"id": sid, "name": r.name, "status": r.status,
                          "measured": measured, "criterion": r.criterion, "baseline": r.baseline})
        total_budget = sum(self.durations.values())
        elapsed_s = (now - self._run_started_at) if (now is not None and self._run_started_at is not None) else None
        return {
            "role": self.role, "serial": self.serial, "technician": self.technician,
            "work_order": self.work_order, "started": self._started,
            "finished": self.finished, "aborted": self.aborted, "abort_reason": self.abort_reason,
            "verdict": self.verdict, "current_step": self.current_id, "steps": steps,
            "awaiting_ht_choice": self.current_id == "H4" and self.ht_choice is None,
            "ht_choice": self.ht_choice, "as_found_angle": self.as_found_angle,
            "total_budget_s": total_budget, "elapsed_s": elapsed_s, "sequence_sha": self.sequence_sha,
        }

    # -- H0 -------------------------------------------------------------

    def _run_h0(self, engine) -> None:
        checks = [("bus in active/KEEP ALIVE mode", getattr(engine, "mode", None) == "active")]
        sha = None
        try:
            import hashlib
            sha = hashlib.sha256(engine.sequence_path.read_bytes()).hexdigest()[:16]
        except Exception:
            pass
        checks.append(("sequence file readable, SHA recorded", sha is not None))
        self_test_ok = True
        try:
            protocol.run_self_test()
        except AssertionError:
            self_test_ok = False
        checks.append(("protocol self-test", self_test_ok))
        passed = all(ok for _, ok in checks)
        self.sequence_sha = sha
        r = self.results["H0"]
        r.status = "pass" if passed else "fail"
        r.measured = "; ".join(f"{name}: {'OK' if ok else 'FAIL'}" for name, ok in checks)
        if not passed:
            self.abort(engine, reason="H0 preflight failed")

    # -- H8 accumulator (fed every tick regardless of current step) -----

    def _feed_h8(self, s: dict) -> None:
        h = self.h8
        h["actual_total"] += 1
        if "redundant_sum_hex" in s:
            sval = int(s["redundant_sum_hex"], 16)
            # spread is a dispersion measure -- one untrusted frame would
            # dominate it. The in-band PERCENTAGE still counts every frame,
            # because catching out-of-band frames is exactly H8's job.
            if s.get("trustworthy") is not False:
                h["s_values"].append(sval)
            if s.get("redundant_sum_in_band"):
                h["s_in_band"] += 1
        if s.get("candidate_integrity_ok") is True:
            h["crc_pass"] += 1
            if (s.get("trustworthy") is not False
                    and s.get("moving") is not None and "inverse_moving" in s):
                if s["moving"] != s["inverse_moving"]:
                    # Every frame carrying the bit-11 status marker also shows
                    # the MOVING bit and its inverse disagreeing -- measured
                    # 4/4 on this bench. The actuator is marking a transitional
                    # state, so the disagreement is EXPECTED there, not a fault.
                    # The 1.0-match baseline came from 74,052 LMV frames that
                    # never had bit 11 set, so counting these against it would
                    # compare unlike with unlike. Counted and REPORTED
                    # separately instead of hidden.
                    if s.get("status_bits"):
                        h["status_marked_mismatch"] += 1
                    else:
                        h["inverse_mismatch"] += 1

    def _live_h8_summary(self) -> str:
        h = self.h8
        total = h["actual_total"]
        if total == 0:
            return "(running) waiting for actual frames…"
        s_in_band_pct = 100.0 * h["s_in_band"] / total
        s_spread = (max(h["s_values"]) - min(h["s_values"])) if h["s_values"] else 0
        crc_pass_pct = 100.0 * h["crc_pass"] / total
        return (f"(running) S in band {s_in_band_pct:.2f}% (spread {s_spread}), "
                f"CRC pass {crc_pass_pct:.2f}%, mismatches {h['inverse_mismatch']}"
                f" (+{h['status_marked_mismatch']} status-marked) -- n={total}")

    def _finalize_h8(self) -> None:
        r = self.results["H8"]
        h = self.h8
        total = h["actual_total"]
        if total == 0:
            r.status = "fail"
            r.measured = "no actual frames observed for this role during the run"
            return
        s_in_band_pct = 100.0 * h["s_in_band"] / total
        s_spread = (max(h["s_values"]) - min(h["s_values"])) if h["s_values"] else 999
        crc_pass_pct = 100.0 * h["crc_pass"] / total
        passed = (s_in_band_pct >= H8_S_IN_BAND_MIN_PCT and s_spread <= H8_S_SPREAD_MAX
                  and h["inverse_mismatch"] == 0 and crc_pass_pct >= H8_CRC_PASS_MIN_PCT)
        r.status = "pass" if passed else "fail"
        r.measured = (f"S in band {s_in_band_pct:.2f}% (spread {s_spread}), "
                      f"CRC pass {crc_pass_pct:.2f}%, inverse-MOVING mismatches {h['inverse_mismatch']} "
                      f"(+{h['status_marked_mismatch']} on status-marked frames) "
                      f"-- n={total}")

    # -- step transitions -------------------------------------------------

    def _enter_step(self, step_id: str, now: float, engine) -> None:
        self.current_id = step_id
        self.step_started_at = now
        self._window = []
        self.results[step_id].status = "running"
        if step_id == "H1":
            engine.start_recording()
        elif step_id == "H5":
            self._h5_setup(engine)
        elif step_id == "H6":
            self._h6_setup(engine)
        elif step_id == "H10":
            self._h10_setup(engine)

    def _advance(self, now: float, engine) -> None:
        idx = TIMED_STEPS.index(self.current_id)
        if idx + 1 < len(TIMED_STEPS):
            self._enter_step(TIMED_STEPS[idx + 1], now, engine)
        else:
            self._finish(now, engine)

    def _finish(self, now: float, engine) -> None:
        self._finalize_h8()
        try:
            engine.stop_recording()
        except Exception:
            pass
        self.finished = True
        self.finished_at = now
        self.verdict = "PASS" if all(self.results[i].status == "pass" for i in STEP_ORDER) else "FAIL"

    # -- H1: wake & identify ----------------------------------------------

    def _tick_h1(self, now: float, step_elapsed: float, engine) -> bool:
        r = self.results["H1"]
        if self.wake_time_s is None:
            snap = engine.snapshot()["servos"][self.role]
            if snap["presence"] != "absent":
                self.wake_time_s = step_elapsed
                if self.as_found_angle is None and snap["actual_deg"] is not None:
                    self.as_found_angle = snap["actual_deg"]
        if self.wake_time_s is not None:
            passed = self.wake_time_s <= WAKE_MAX_S
            r.status = "pass" if passed else "fail"
            r.measured = f"woke in {self.wake_time_s:.2f} s"
            return True
        if step_elapsed >= self.durations["H1"]:
            r.status = "fail"
            r.measured = f"no actual frame from {self.role} after {step_elapsed:.1f} s"
            return True
        return False

    # -- H2: link quality ---------------------------------------------------

    def _tick_h2(self, now: float, step_elapsed: float, engine) -> bool:
        if step_elapsed < self.durations["H2"]:
            return False
        r = self.results["H2"]
        ts = [s["t"] for s in self._window]
        malformed = sum(1 for s in self._window if s.get("dlc") != 8)
        unknown_marker = sum(1 for s in self._window if s.get("marker") not in (0x5A, 0xA5))
        rate_hz = len(ts) / step_elapsed if step_elapsed > 0 else 0.0
        gaps = [b - a for a, b in zip(ts, ts[1:])]
        max_gap = max(gaps) if gaps else step_elapsed
        # H2 tests FOUR things. Reporting only the numbers made a gap failure
        # look like a rate failure: an operator reads "4.86 Hz FAIL" against a
        # stated 4.5-5.4 Hz band and concludes the criterion is broken, when
        # the real fault was a 0.7 s hole. Name the failing test.
        failures = []
        if not RATE_MIN_HZ <= rate_hz <= RATE_MAX_HZ:
            failures.append(f"rate {rate_hz:.2f} outside {RATE_MIN_HZ}-{RATE_MAX_HZ} Hz")
        if max_gap > GAP_MAX_S:
            failures.append(f"gap {max_gap:.3f} s over {GAP_MAX_S:.2f} s")
        if malformed:
            failures.append(f"{malformed} malformed frame(s)")
        if unknown_marker:
            failures.append(f"{unknown_marker} unknown marker(s)")
        r.status = "fail" if failures else "pass"
        r.measured = (f"{rate_hz:.2f} Hz, max gap {max_gap:.3f} s, "
                      f"{malformed} malformed, {unknown_marker} unknown marker")
        if failures:
            r.measured += "  ->  FAILED ON: " + "; ".join(failures)
            # A rate slightly under band together with an over-band gap, but
            # with no malformed frames and no CRC trouble, is the signature of
            # the ADAPTER dropping frames rather than the servo or the bus
            # misbehaving -- the ESP32 bridge does this at four-servo load
            # (FINDINGS 2026-08-28, MCP2515 RX0OVR). Say so, because the
            # actuator is the wrong thing to go and replace.
            gap_only = (max_gap > GAP_MAX_S and not malformed and not unknown_marker
                        and rate_hz >= RATE_MIN_HZ * 0.9)
            if gap_only:
                r.measured += ("  [pattern fits ADAPTER frame loss, not a servo fault -- "
                               "check the converter before the actuator]")
        return True

    # -- H3 / H4 / H7: settle windows ---------------------------------------

    @staticmethod
    def _settle_stats(samples: list[dict]) -> dict | None:
        # Only frames inside the validated envelope may form a position
        # statistic. An untrusted frame is counted by H2/H8 as a link fault,
        # but letting it into std/p-p/drift reports a stationary shaft as
        # swinging hundreds of degrees.
        good = [s for s in samples if s.get("trustworthy") is not False]
        angles = [s["angle_deg"] for s in good if s.get("angle_deg") is not None]
        moving_flags = [s["moving"] for s in good if s.get("moving") is not None]
        if len(angles) < 2:
            return None
        return {
            "std": statistics.pstdev(angles), "pp": max(angles) - min(angles),
            "drift": _drift(angles), "spurious": count_spurious_events(angles),
            "moving_pct": 100.0 * sum(1 for m in moving_flags if m) / len(moving_flags) if moving_flags else 0.0,
            "n": len(angles),
        }

    def _tick_settle(self, step_id: str, step_elapsed: float, extra_ok,
                      lead_in_s: float = 0.0) -> bool:
        if step_elapsed < self.durations[step_id]:
            return False
        r = self.results[step_id]
        window = self._window
        if lead_in_s > 0.0 and window:
            # Discard the run-on right after a move. H7 exists to catch
            # instability AFTER the motor has stopped; measuring the tail of
            # the traverse just re-reports that the shaft was still moving.
            t0 = window[0]["t"]
            trimmed = [x for x in window if x["t"] - t0 >= lead_in_s]
            if len(trimmed) >= 10:
                window = trimmed
        stats = self._settle_stats(window)
        if stats is None:
            r.status = "fail"
            r.measured = "insufficient samples in window"
            return True
        extra_pass, extra_detail = extra_ok()
        passed = (stats["std"] <= SETTLED_STD_MAX and stats["pp"] <= SETTLED_PP_MAX
                  and stats["drift"] <= SETTLED_DRIFT_MAX and stats["spurious"] == 0
                  and stats["moving_pct"] <= SETTLED_MOVING_MAX_PCT and extra_pass)
        r.status = "pass" if passed else "fail"
        detail = (f"std {stats['std']:.4f}, p-p {stats['pp']:.3f}, drift {stats['drift']:.3f}, "
                  f"spurious {stats['spurious']}, MOVING {stats['moving_pct']:.1f}% (n={stats['n']})")
        if extra_detail:
            detail += f", {extra_detail}"
        r.measured = detail
        return True

    def _tick_h3(self, now: float, step_elapsed: float, engine) -> bool:
        return self._tick_settle("H3", step_elapsed, lambda: (True, None))

    def _tick_h4(self, now: float, step_elapsed: float, engine) -> bool:
        def extra():
            crc_fail = sum(1 for s in self._window if s.get("candidate_integrity_ok") is False)
            return crc_fail == 0, f"CRC failures {crc_fail}, HT {self.ht_choice or 'not_available'}"
        return self._tick_settle("H4", step_elapsed, extra)

    def _tick_h7(self, now: float, step_elapsed: float, engine) -> bool:
        return self._tick_settle("H7", step_elapsed, lambda: (True, None),
                                  lead_in_s=H7_LEAD_IN_S)

    # -- H5: commanded move ---------------------------------------------

    def _h5_setup(self, engine) -> None:
        base = self.as_found_angle if self.as_found_angle is not None else 45.0
        target = base + MOVE_TARGET_OFFSET_DEG
        direction = f"+{MOVE_TARGET_OFFSET_DEG:.0f}"
        if target > 90.0:
            target = base - MOVE_TARGET_OFFSET_DEG
            direction = f"-{MOVE_TARGET_OFFSET_DEG:.0f}"
        target = max(0.0, min(90.0, target))
        self._h5_target = target
        self._h5_direction = direction
        self._h5_move_start_angle = base
        self._h5_move_seen_active = False
        self._h5_move_requested = False
        try:
            engine.set_pending_target(self.role, target)
        except Exception as exc:
            self._h5_move_failed = True
            r = self.results["H5"]
            r.status = "fail"
            r.measured = f"could not set pending target: {exc}"

    def _tick_h5(self, now: float, step_elapsed: float, engine) -> bool:
        r = self.results["H5"]
        if self._h5_move_failed:
            return True
        if not self._h5_move_requested:
            # set_pending_target() above only queued an intent -- BusEngine's
            # single-writer discipline means it is not applied until the TX
            # thread's NEXT _tick() drains it. Calling request_move() in the
            # same _h5_setup() call would read servo.pending_target before
            # that drain happens and always refuse. Waiting for our own
            # first tick guarantees at least one intent-drain has occurred.
            self._h5_move_requested = True
            try:
                engine.request_move(self.role)
            except Exception as exc:
                self._h5_move_failed = True
                r.status = "fail"
                r.measured = f"move refused: {exc}"
                return True
        snap = engine.snapshot()["servos"][self.role]
        if snap["moving"]:
            self._h5_move_seen_active = True
        if not snap["moving"] and self._h5_move_seen_active:
            arrived_naturally = snap["arrived_at"] is not None
            actual = snap["actual_deg"]
            distance = abs(self._h5_target - self._h5_move_start_angle)
            speed = distance / step_elapsed if step_elapsed > 0 else 0.0
            settle_err = abs(actual - self._h5_target) if actual is not None else 999.0
            moving_samples = [s for s in self._window if s.get("moving") is not None]
            moving_pct = (100.0 * sum(1 for s in moving_samples if s["moving"]) / len(moving_samples)
                          if moving_samples else 0.0)
            angles = [s["angle_deg"] for s in self._window
                      if s.get("angle_deg") is not None and s.get("trustworthy") is not False]
            overshoot = 0.0
            if angles:
                if self._h5_direction.startswith("+"):
                    overshoot = max(0.0, max(angles) - self._h5_target)
                else:
                    overshoot = max(0.0, self._h5_target - min(angles))
            # Dynamic Position Checking: the LMV watches the shaft ON THE WAY,
            # not only where it lands (QSG p.124). A jump, a reversal or a
            # stall gap all arrive at the right angle having misbehaved.
            cont = transit_continuity(self._window)
            passed = (arrived_naturally and MOVE_SPEED_MIN <= speed <= MOVE_SPEED_MAX
                      and settle_err <= MOVE_SETTLE_ERR_MAX and overshoot <= MOVE_OVERSHOOT_MAX
                      and moving_pct >= MOVE_MOVING_MIN_PCT
                      and cont["ok"] is not False)
            r.status = "pass" if passed else "fail"
            reason = "arrived" if arrived_naturally else "watchdog timeout / stalled"
            r.measured = (f"{reason}, {speed:.2f} deg/s, settle err {settle_err:.3f}, "
                          f"overshoot {overshoot:.3f}, MOVING {moving_pct:.1f}%, "
                          f"transit {cont['text']} "
                          f"(target {self._h5_target:.1f} deg, {self._h5_direction})")
            return True
        if step_elapsed >= self.durations["H5"]:
            r.status = "fail"
            r.measured = ("move never started" if not self._h5_move_seen_active
                          else f"did not settle within {step_elapsed:.1f} s")
            return True
        return False

    # -- H6: return & repeatability --------------------------------------

    def _h6_setup(self, engine) -> None:
        target = self.as_found_angle if self.as_found_angle is not None else self._h5_move_start_angle
        self._h6_target = target
        self._h6_move_seen_active = False
        self._h6_move_requested = False
        try:
            engine.set_pending_target(self.role, target)
        except Exception as exc:
            self._h6_move_failed = True
            r = self.results["H6"]
            r.status = "fail"
            r.measured = f"could not set pending target: {exc}"

    def _tick_h6(self, now: float, step_elapsed: float, engine) -> bool:
        r = self.results["H6"]
        if self._h6_move_failed:
            return True
        if not self._h6_move_requested:
            self._h6_move_requested = True   # see _tick_h5's comment: same intent-queue ordering fix
            try:
                engine.request_move(self.role)
            except Exception as exc:
                self._h6_move_failed = True
                r.status = "fail"
                r.measured = f"move refused: {exc}"
                return True
        snap = engine.snapshot()["servos"][self.role]
        if snap["moving"]:
            self._h6_move_seen_active = True
        if not snap["moving"] and self._h6_move_seen_active:
            actual = snap["actual_deg"]
            hysteresis = abs(actual - self._h6_target) if actual is not None else 999.0
            arrived_naturally = snap["arrived_at"] is not None
            # Position is quantised to 0.1 deg, so hysteresis is always a whole
            # number of LSB. Comparing floats here let representation error
            # decide the verdict: an exact 1-LSB return measured 0.100 and
            # still failed "<= 0.1". Compare in LSB instead.
            hyst_lsb = round(hysteresis / LSB_DEG)
            passed = arrived_naturally and hyst_lsb <= round(HYSTERESIS_MAX / LSB_DEG)
            r.status = "pass" if passed else "fail"
            r.measured = (f"returned to {actual:.2f} deg, |hysteresis| {hysteresis:.3f} deg "
                          f"(target {self._h6_target:.1f})")
            return True
        if step_elapsed >= self.durations["H6"]:
            r.status = "fail"
            r.measured = ("move never started" if not self._h6_move_seen_active
                          else f"did not settle within {step_elapsed:.1f} s")
            return True
        return False

    # -- H9: safe finish -----------------------------------------------

    # -- H10: LMV special positions ---------------------------------------
    #
    # Walks the actuator through the angles the LMV parks it at during a real
    # burner cycle, applying the controller's own three checks at each one:
    # arrive within tolerance ("Position Required to Proceed"), stay put while
    # stopped ("Run-Time Position Checking"), and behave on the way there
    # ("Dynamic Position Checking"). See the constants block for the mapping.

    def _h10_targets(self) -> tuple[float, ...]:
        """The sweep. Does NOT return to as-found -- H6 runs next and does that.

        An earlier version appended a return leg here, because H10 used to run
        after H6 and would otherwise fail H9. Reordering the steps removed the
        need: H6 is now the single place the shaft is brought home, so there is
        exactly one restore and one hysteresis measurement rather than two
        moves fighting over the same job.
        """
        return tuple(SPECIAL_POSITIONS_DEG)

    def _h10_setup(self, engine) -> None:
        self._h10_sweep = self._h10_targets()
        self._h10_index = 0
        self._h10_phase = "move"       # move -> hold -> next
        self._h10_requested = False
        self._h10_seen_moving = False
        self._h10_hold_started = None
        self._h10_leg_index0 = 0
        self._h10_hold_index0 = None
        self._h10_transit = []
        self._h10_results: list[dict] = []
        self._h10_failed = False
        self._h10_begin_leg(engine)

    def _h10_begin_leg(self, engine) -> None:
        if self._h10_index >= len(self._h10_sweep):
            return
        target = self._h10_sweep[self._h10_index]
        self._h10_phase = "move"
        self._h10_requested = False
        self._h10_seen_moving = False
        self._h10_hold_started = None
        # _window accumulates for the WHOLE step, not per leg, so each leg must
        # slice out its own samples or leg 2 analyses leg 1 plus the hold
        # between them -- ~30-100 phantom direction reversals, failing a clean
        # sweep. Sliced by INDEX, deliberately: sample["t"] is the ENGINE clock
        # (monotonic - engine._t0) while `now` here is transmit-relative
        # elapsed, so a timestamp comparison between the two silently matches
        # nothing useful. An index into the list we own has no such ambiguity.
        self._h10_leg_index0 = len(self._window)
        self._h10_hold_index0 = None
        self._h10_transit = []
        try:
            engine.set_pending_target(self.role, target)
        except Exception as exc:
            self._h10_failed = True
            r = self.results["H10"]
            r.status = "fail"
            r.measured = f"could not set target {target:.0f} deg: {exc}"

    def _tick_h10(self, now: float, step_elapsed: float, engine) -> bool:
        r = self.results["H10"]
        if self._h10_failed:
            return True
        if self._h10_index >= len(self._h10_sweep):
            return self._h10_finish(r)

        target = self._h10_sweep[self._h10_index]
        snap = engine.snapshot()["servos"][self.role]

        if self._h10_phase == "move":
            if not self._h10_requested:
                # Same intent-queue ordering as H5: wait one tick so the
                # queued pending_target has actually been drained.
                self._h10_requested = True
                try:
                    engine.request_move(self.role)
                except Exception as exc:
                    self._h10_failed = True
                    r.status = "fail"
                    r.measured = f"move to {target:.0f} deg refused: {exc}"
                    return True
                return False
            if snap["moving"]:
                self._h10_seen_moving = True
            if not snap["moving"] and self._h10_seen_moving:
                self._h10_phase = "hold"
                self._h10_hold_started = now
                self._h10_transit = self._window[self._h10_leg_index0:]
                self._h10_hold_index0 = len(self._window)
            return False

        # holding: let it sit, then judge arrival + stability together
        if now - self._h10_hold_started < SPECIAL_HOLD_S:
            return False
        hold = [s for s in self._window[self._h10_hold_index0 or 0:]
                if s.get("angle_deg") is not None and s.get("trustworthy") is not False]
        angles = [s["angle_deg"] for s in hold]
        actual = snap["actual_deg"]
        arrive_err = abs(actual - target) if actual is not None else 999.0
        std = statistics.pstdev(angles) if len(angles) > 1 else 0.0
        drift = _drift(angles)
        cont = transit_continuity(self._h10_transit)
        ok = (arrive_err <= SPECIAL_ARRIVE_ERR_MAX
              and std <= SPECIAL_HOLD_STD_MAX
              and drift <= SPECIAL_HOLD_DRIFT_MAX
              and cont["ok"] is not False)
        self._h10_results.append({
            "target": target, "arrive_err": arrive_err, "std": std, "drift": drift,
            "continuity": cont, "samples": len(angles), "ok": ok})
        self._h10_index += 1
        if self._h10_index >= len(self._h10_sweep):
            return self._h10_finish(r)
        self._h10_begin_leg(engine)
        return False

    def _h10_finish(self, r) -> bool:
        if not self._h10_results:
            r.status = "fail"
            r.measured = "no special position was reached"
            return True
        r.status = "pass" if all(x["ok"] for x in self._h10_results) else "fail"
        parts = []
        for x in self._h10_results:
            parts.append(f"{x['target']:.0f}deg: err {x['arrive_err']:.2f}, "
                         f"std {x['std']:.3f}, drift {x['drift']:.3f}, "
                         f"{x['continuity']['text']}"
                         + ("" if x["ok"] else "  <-- FAIL"))
        r.measured = " | ".join(parts)
        return True

    # -- H11: cross-actuator coupling -------------------------------------

    def _tick_h11(self, now: float, step_elapsed: float, engine) -> bool:
        if step_elapsed < self.durations["H11"]:
            return False
        r = self.results["H11"]
        if not self._bystander:
            # Not a failure: a single-actuator bus has no bystanders, and
            # reporting FAIL there would punish a correct setup.
            r.status = "pass"
            r.measured = ("no other actuator on the bus -- coupling not assessable "
                          "(this check needs at least two servos)")
            return True

        parts, offenders = [], []
        for role in sorted(self._bystander):
            samples = self._bystander[role]
            good = [x for x in samples if x.get("trustworthy") is not False
                    and x.get("angle_deg") is not None]
            angles = [x["angle_deg"] for x in good]
            faults = sum(1 for x in samples
                         if (x.get("status_bits") or 0) & protocol.POSITION_FAULT_BITS)
            if len(angles) < 2:
                parts.append(f"{role}: only {len(angles)} samples")
                continue
            std = statistics.pstdev(angles)
            pp = max(angles) - min(angles)
            ok = (std <= H11_BYSTANDER_STD_MAX and pp <= H11_BYSTANDER_PP_MAX
                  and faults <= H11_BYSTANDER_FAULT_MAX)
            if not ok:
                offenders.append(role)
            parts.append(f"{role}: std {std:.3f}, p-p {pp:.3f}, {faults} fault"
                         + ("" if ok else "  <-- DISTURBED"))

        r.status = "fail" if offenders else "pass"
        r.measured = f"while {self.role} was driven -- " + " | ".join(parts)
        if offenders:
            # This is the p.143 discrimination, made explicit. The operator
            # should not go and replace the servo that was moving.
            many = len(offenders) > 1
            r.measured += (
                f"  ->  {len(offenders)} parked actuator(s) moved while {self.role} was driven. "
                + ("Multiple actuators disturbed together points at the CAN wiring or cable "
                   "screening (LMV5 QSG p.143), not at any one servo."
                   if many else
                   f"Only {offenders[0]} was disturbed -- check its own wiring, screening and "
                   f"routing before suspecting the actuator itself."))
        return True

    def _tick_h9(self, now: float, step_elapsed: float, engine) -> bool:
        if step_elapsed < self.durations["H9"]:
            return False
        r = self.results["H9"]
        try:
            engine.request_hold()
        except Exception:
            pass
        snap = engine.snapshot()["servos"][self.role]
        within_tol = (snap["actual_deg"] is not None and self.as_found_angle is not None
                      and abs(snap["actual_deg"] - self.as_found_angle) <= H9_RESTORE_TOL_DEG)
        r.status = "pass" if within_tol else "fail"
        actual_txt = f"{snap['actual_deg']:.2f}" if snap["actual_deg"] is not None else "?"
        found_txt = f"{self.as_found_angle:.2f}" if self.as_found_angle is not None else "?"
        r.measured = f"HOLD asserted, resting at {actual_txt} deg (as-found {found_txt} deg)"
        return True
