"""Offline regression for HealthCheckRun: the H0-H9 state machine driven
against a minimal FakeEngine double -- no transport, no real threads, no
wall-clock waiting. Each step's tick() is called directly with a controlled
`now`, so every branch (pass, fail, timeout, watchdog, abort) is exercised in
milliseconds instead of the ~292s a real run takes.

Fault injection is the point, not a nicety: a pass criterion never shown to
fail is not a criterion (see the plan's Part D/Verification). test_h3_*_fails
and test_h4_crc_failure_fails inject the exact faults the plan calls out.

    python sqm45/tests/test_healthcheck.py
"""
from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
SCRIPTS = HERE.parent.parent
if str(SCRIPTS) not in sys.path:
    sys.path.insert(0, str(SCRIPTS))

from sqm45 import protocol  # noqa: E402
from sqm45.engine import BusEngine  # noqa: E402
from sqm45.healthcheck import HYSTERESIS_MAX, HealthCheckRun  # noqa: E402


class FakeEngine:
    """Just enough of BusEngine's surface for HealthCheckRun to drive: mode,
    sequence_path (H0 reads it for a SHA), new_recorded_samples (fed
    manually via push()), snapshot()/set_pending_target()/request_move()/
    request_hold()/start_recording()/stop_recording()."""

    def __init__(self):
        self.mode = "active"
        self.sequence_path = Path(__file__)   # any real, readable file
        self._recorder: list[dict] = []
        self._servos = {
            "air": {"presence": "absent", "actual_deg": None, "moving": False, "arrived_at": None},
            "fuel": {"presence": "absent", "actual_deg": None, "moving": False, "arrived_at": None},
        }
        self._pending: tuple[str, float] | None = None
        self.moves: list[tuple[str, float]] = []
        self.holds = 0
        self.request_move_error: Exception | None = None

    def push(self, sample: dict) -> None:
        self._recorder.append(sample)

    def new_recorded_samples(self, cursor: int):
        return self._recorder[cursor:], len(self._recorder)

    def snapshot(self) -> dict:
        return {"servos": self._servos}

    def set_pending_target(self, role: str, angle: float) -> None:
        self._pending = (role, angle)

    def request_move(self, role: str) -> None:
        if self.request_move_error is not None:
            err, self.request_move_error = self.request_move_error, None   # one-shot
            raise err
        self.moves.append((role, self._pending[1]))

    def request_hold(self) -> None:
        self.holds += 1

    def start_recording(self) -> None:
        pass

    def stop_recording(self) -> None:
        pass


def make_sample(role: str, t: float, angle_deg: float, moving: bool = False,
                 candidate_integrity_ok: bool | None = True, redundant_sum_in_band: bool = True,
                 s_hex: str = "0x844A", inverse_moving: bool | None = None,
                 dlc: int = 8, marker: int = 0x5A) -> dict:
    if inverse_moving is None:
        inverse_moving = moving
    return {"t": t, "can_id": 0x18A if role == "air" else 0x18B, "dlc": dlc, "raw_hex": "",
            "kind": "actual", "role": role, "angle_deg": angle_deg, "moving": moving,
            "warning": "", "state_word": None, "counter": 0, "marker": marker,
            "candidate_integrity_ok": candidate_integrity_ok, "redundancy_error_deg": 0.0,
            "redundant_sum_hex": s_hex, "redundant_sum_in_band": redundant_sum_in_band,
            "inverse_moving": inverse_moving}


SHORT = {"H1": 3.0, "H2": 2.0, "H3": 2.0, "H4": 2.0, "H5": 40.0, "H10": 400.0,
         "H11": 0.5, "H6": 40.0, "H7": 2.0, "H9": 0.5}
# H5/H6 durations are simulated timestamps, not real sleeps (this test never
# sleeps) -- 40s of *simulated* budget just needs to safely exceed a 25 deg
# move at MOVE_SPEED_MIN (12.5s) plus margin for the "never arrives" test.


def test_h0_preflight_and_h1_wake() -> None:
    fe = FakeEngine()
    hc = HealthCheckRun("air", "SN1", "Bijak", durations=SHORT)
    hc.start(0.0, fe)
    assert hc.results["H0"].status == "pass", hc.results["H0"].measured
    assert hc.current_id == "H1"
    hc.tick(0.5, fe)
    assert hc.results["H1"].status == "running"
    fe._servos["air"].update(presence="live", actual_deg=42.0)
    hc.tick(0.8, fe)
    assert hc.results["H1"].status == "pass", hc.results["H1"].measured
    assert hc.current_id == "H2"
    assert hc.as_found_angle == 42.0
    print("  H0/H1               : preflight passes, wake at 0.8s recorded as PASS, as-found captured")


def test_h0_fails_when_bus_not_active() -> None:
    fe = FakeEngine()
    fe.mode = "listen"
    hc = HealthCheckRun("air", "SN1", "Bijak", durations=SHORT)
    hc.start(0.0, fe)
    assert hc.results["H0"].status == "fail"
    assert hc.aborted and hc.verdict == "ABORTED"
    print("  H0 gate              : bus not in active mode correctly aborts before H1")


def test_h1_timeout_fails_but_advances() -> None:
    fe = FakeEngine()
    hc = HealthCheckRun("air", "SN1", "Bijak", durations=SHORT)
    hc.start(0.0, fe)
    hc.tick(SHORT["H1"] + 0.01, fe)
    assert hc.results["H1"].status == "fail", hc.results["H1"].measured
    assert hc.current_id == "H2", "a failed step must still advance, not stall the run"
    print("  H1 timeout            : no wake within budget -> FAIL, run still advances")


def _advance_to(hc: HealthCheckRun, fe: FakeEngine, target_step: str, t0: float = 0.0) -> float:
    """Drive H0->H1(wake)->... up to (not including) target_step using
    steady, well-formed samples, returning the `now` at which target_step
    was entered."""
    now = t0
    hc.start(now, fe)
    fe._servos["air"].update(presence="live", actual_deg=42.0)
    now += 0.1
    hc.tick(now, fe)   # resolves H1
    assert hc.current_id in ("H2",) or hc.current_id == target_step
    while hc.current_id != target_step:
        step = hc.current_id
        dur = hc.durations[step]
        n = max(6, int(dur * 5))
        for i in range(1, n + 1):
            now = hc.step_started_at + dur * i / n
            fe.push(make_sample("air", now, 42.0, moving=False))
            hc.tick(now, fe)
            if hc.current_id != step:
                break
        else:
            now = hc.step_started_at + dur + 0.01
            hc.tick(now, fe)
    # One settling tick at entry: H5/H6 defer request_move() to their first
    # tick (the intent-queue ordering fix -- see _tick_h5's comment), so the
    # caller needs this to have happened before inspecting fe.moves.
    now = hc.step_started_at + 0.001
    hc.tick(now, fe)
    return now


def test_h2_link_quality_pass_and_fail() -> None:
    fe = FakeEngine()
    hc = HealthCheckRun("air", "SN1", "Bijak", durations=SHORT)
    now = _advance_to(hc, fe, "H2")
    dur = hc.durations["H2"]
    n = round(dur * 4.96)
    for i in range(1, n + 1):
        now = hc.step_started_at + dur * i / n
        fe.push(make_sample("air", now, 42.0, moving=False))
        hc.tick(now, fe)
    now = hc.step_started_at + dur + 0.01
    hc.tick(now, fe)
    assert hc.results["H2"].status == "pass", hc.results["H2"].measured
    print("  H2 pass               : 4.96 Hz steady traffic -> PASS —", hc.results["H2"].measured)

    fe2 = FakeEngine()
    hc2 = HealthCheckRun("air", "SN1", "Bijak", durations=SHORT)
    now = _advance_to(hc2, fe2, "H2")
    dur = hc2.durations["H2"]
    for i in range(1, 4):   # far too few samples -> rate way under band
        now = hc2.step_started_at + dur * i / 4
        fe2.push(make_sample("air", now, 42.0, moving=False))
        hc2.tick(now, fe2)
    now = hc2.step_started_at + dur + 0.01
    hc2.tick(now, fe2)
    assert hc2.results["H2"].status == "fail", hc2.results["H2"].measured
    print("  H2 fail               : sparse traffic correctly FAILs —", hc2.results["H2"].measured)


def test_h3_pass_steady() -> None:
    fe = FakeEngine()
    hc = HealthCheckRun("air", "SN1", "Bijak", durations=SHORT)
    now = _advance_to(hc, fe, "H3")
    dur = hc.durations["H3"]
    n = max(20, round(dur * 4.96))
    for i in range(1, n + 1):
        now = hc.step_started_at + dur * i / n
        fe.push(make_sample("air", now, 42.0, moving=False))
        hc.tick(now, fe)
    now = hc.step_started_at + dur + 0.01
    hc.tick(now, fe)
    assert hc.results["H3"].status == "pass", hc.results["H3"].measured
    print("  H3 pass               : dead-steady 42.0 deg -> PASS —", hc.results["H3"].measured)


def test_h3_fault_injection_jitter_fails() -> None:
    """The plan's required fault injection: +/-0.3 deg jitter into the
    actual position must fail H3 (spurious events / std / p-p)."""
    fe = FakeEngine()
    hc = HealthCheckRun("air", "SN1", "Bijak", durations=SHORT)
    now = _advance_to(hc, fe, "H3")
    dur = hc.durations["H3"]
    n = max(20, round(dur * 4.96))
    for i in range(1, n + 1):
        now = hc.step_started_at + dur * i / n
        jitter = 0.3 if (i % 4 == 0) else 0.0   # periodic +0.3 deg spike, well above SPURIOUS_MEDIAN_DELTA
        fe.push(make_sample("air", now, 42.0 + jitter, moving=False))
        hc.tick(now, fe)
    now = hc.step_started_at + dur + 0.01
    hc.tick(now, fe)
    assert hc.results["H3"].status == "fail", \
        f"injected +/-0.3 deg jitter must fail H3, got: {hc.results['H3'].measured}"
    print("  H3 fault injection     : +/-0.3 deg jitter correctly FAILs —", hc.results["H3"].measured)


def test_h4_crc_failure_fails_even_if_settled() -> None:
    fe = FakeEngine()
    hc = HealthCheckRun("air", "SN1", "Bijak", durations=SHORT)
    now = _advance_to(hc, fe, "H4")
    dur = hc.durations["H4"]
    n = max(20, round(dur * 4.96))
    for i in range(1, n + 1):
        now = hc.step_started_at + dur * i / n
        crc_ok = not (i in (5, 10, 15))   # a few CRC-family failures, otherwise dead steady
        fe.push(make_sample("air", now, 42.0, moving=False, candidate_integrity_ok=crc_ok))
        hc.tick(now, fe)
    now = hc.step_started_at + dur + 0.01
    hc.tick(now, fe)
    assert hc.results["H4"].status == "fail", hc.results["H4"].measured
    assert "CRC failures 3" in hc.results["H4"].measured
    print("  H4 fault injection     : settled but 3 CRC failures -> FAIL —", hc.results["H4"].measured)


def test_h5_and_h6_pass_roundtrip() -> None:
    fe = FakeEngine()
    hc = HealthCheckRun("air", "SN1", "Bijak", durations=SHORT)
    now = _advance_to(hc, fe, "H5")
    assert hc.as_found_angle == 42.0
    target = hc._h5_target
    assert abs(target - 67.0) < 1e-9, target   # 42 + 25, within 0-90
    assert fe.moves[-1] == ("air", 67.0)

    # Simulate a realistic ~3 deg/s ramp AT THE REAL FRAME RATE. This used to
    # push 9 samples across the whole 8.3 s ramp (~1.1 Hz), which made each
    # step 2.5 deg and each gap 0.83 s -- fine for the speed/overshoot maths
    # it was written for, but it is not what the bus does, and H5's Dynamic
    # Position Checking correctly flagged it as discontinuous. Sample at the
    # measured 4.96 Hz so the fixture exercises the real geometry: ~0.6 deg
    # per sample, 0.2 s apart.
    fe._servos["air"]["moving"] = True
    duration = 25.0 / 3.0
    n_samples = round(duration * 4.96)
    for i in range(1, n_samples):
        frac = i / n_samples
        now = hc.step_started_at + frac * duration
        angle = 42.0 + frac * 25.0
        fe._servos["air"]["actual_deg"] = angle
        fe.push(make_sample("air", now, angle, moving=True))
        hc.tick(now, fe)
        assert hc.current_id == "H5", "must not resolve before moving=False is observed"
    now = hc.step_started_at + 25.0 / 3.0
    fe._servos["air"].update(moving=False, actual_deg=67.0, arrived_at=now)
    fe.push(make_sample("air", now, 67.0, moving=False))
    hc.tick(now, fe)
    assert hc.results["H5"].status == "pass", hc.results["H5"].measured
    # H10 now runs between the move and the return, so H6 is reached through it.
    assert hc.current_id == "H10", hc.current_id
    print("  H5 pass                : 3 deg/s ramp 42->67 -> PASS —", hc.results["H5"].measured)
    hc.durations["H10"] = 400.0
    now = _drive_h10_sweep(hc, fe, now)
    assert hc.results["H10"].status == "pass", hc.results["H10"].measured
    now = _clear_h11(hc, fe, now)
    assert hc.current_id == "H6", hc.current_id

    assert hc._h6_target == 42.0
    fe._servos["air"]["moving"] = True
    now = hc.step_started_at + 0.01
    fe.push(make_sample("air", now, 67.0, moving=True))
    hc.tick(now, fe)   # H6's first tick -- this is where request_move() actually fires
    assert fe.moves[-1] == ("air", 42.0)
    # ...and the return leg at the real rate too, for the same reason.
    duration = 25.0 / 3.0
    n_samples = round(duration * 4.96)
    for i in range(1, n_samples):
        frac = i / n_samples
        now = hc.step_started_at + frac * duration
        angle = 67.0 - frac * 25.0
        fe._servos["air"]["actual_deg"] = angle
        fe.push(make_sample("air", now, angle, moving=True))
        hc.tick(now, fe)
    now = hc.step_started_at + duration
    fe._servos["air"].update(moving=False, actual_deg=42.02, arrived_at=now)
    fe.push(make_sample("air", now, 42.02, moving=False))
    hc.tick(now, fe)
    assert hc.results["H6"].status == "pass", hc.results["H6"].measured
    print("  H6 pass                : returned to 42.02 (target 42.0) -> PASS —", hc.results["H6"].measured)


def _clear_h11(hc, fe, now):
    """H11 commands nothing -- it just judges the bystanders. Tick past it."""
    assert hc.current_id == "H11", hc.current_id
    now += hc.durations["H11"] + 0.01
    hc.tick(now, fe)
    return now


def _drive_h10_sweep(hc, fe, now, sim_error: float = 0.0,
                     sim_jump_on_leg: int | None = None):
    """Drive H10's sweep from wherever the run currently is.

    Split out because H10 runs BETWEEN H5 and H6, so the H5/H6 roundtrip has
    to pass through it too -- H6 is no longer reachable directly from H5.
    """
    from sqm45.healthcheck import SPECIAL_HOLD_S
    assert hc.current_id == "H10", hc.current_id
    for idx, target in enumerate(hc._h10_sweep):
        start = fe._servos["air"]["actual_deg"]
        fe._servos["air"]["moving"] = True
        now += 0.2
        hc.tick(now, fe)                       # first tick fires request_move()
        dur = max(0.5, abs(target - start) / 3.0)
        n = max(4, round(dur * 4.96))          # the real 4.96 Hz frame rate
        for i in range(1, n + 1):
            now += dur / n
            angle = start + (target - start) * i / n
            if sim_jump_on_leg == idx and i == n // 2:
                angle += 6.0                   # a sensor glitch mid-transit
            fe._servos["air"]["actual_deg"] = angle
            fe.push(make_sample("air", now, angle, moving=True))
            hc.tick(now, fe)
        landed = target + sim_error
        fe._servos["air"].update(moving=False, actual_deg=landed, arrived_at=now)
        fe.push(make_sample("air", now, landed, moving=False))
        hc.tick(now, fe)
        for _ in range(int(SPECIAL_HOLD_S * 4.96) + 3):
            now += 0.2
            fe.push(make_sample("air", now, landed, moving=False))
            hc.tick(now, fe)
    return now


def _drive_h10(sim_error: float = 0.0, sim_jump_on_leg: int | None = None):
    fe = FakeEngine()
    hc = HealthCheckRun("air", "SN1", "Bijak", durations=dict(SHORT, H10=400.0))
    now = _advance_to(hc, fe, "H10")
    _drive_h10_sweep(hc, fe, now, sim_error, sim_jump_on_leg)
    return hc


def test_h10_clean_sweep_passes() -> None:
    hc = _drive_h10()
    assert hc.results["H10"].status == "pass", hc.results["H10"].measured
    # Regression guard: _window accumulates for the WHOLE step, so slicing the
    # transit per leg is what stops leg 2 from including leg 1 plus the hold
    # between them. That bug read as ~60 direction reversals and failed a
    # perfectly clean sweep.
    assert "reversal" in hc.results["H10"].measured
    for part in hc.results["H10"].measured.split(" | "):
        assert "0 reversal(s)" in part, f"phantom reversals leaked in: {part}"
    print("  H10 clean sweep        : all special positions PASS —", hc.results["H10"].measured[:88])


def test_h10_arrival_error_fails() -> None:
    hc = _drive_h10(sim_error=1.4)
    assert hc.results["H10"].status == "fail"
    assert "err 1.40" in hc.results["H10"].measured
    print("  H10 arrival fault      : 1.4 deg off target correctly FAILs (Position Required to Proceed)")


def test_h10_transit_jump_fails_only_that_leg() -> None:
    """Dynamic Position Checking must localise the fault, not smear it."""
    hc = _drive_h10(sim_jump_on_leg=0)
    r = hc.results["H10"]
    assert r.status == "fail"
    legs = r.measured.split(" | ")
    assert "FAIL" in legs[0], legs[0]
    for later in legs[1:]:
        assert "FAIL" not in later, f"clean leg wrongly failed: {later}"
    print("  H10 transit-jump fault : 6 deg glitch fails ONLY its own leg —", legs[0][:76])


def _run_h11(bystanders: dict) -> "HealthCheckRun":
    """bystanders: role -> list of angles observed while `air` was driven."""
    fe = FakeEngine()
    hc = HealthCheckRun("air", "SN1", "Bijak", durations=SHORT)
    now = _advance_to(hc, fe, "H5")
    hc.current_id = "H11"                    # jump straight to the judgement
    hc.step_started_at = now
    hc._bystander = {}
    for role, angles in bystanders.items():
        hc._bystander[role] = [
            dict(make_sample(role, now + i * 0.2, a), status_bits=0)
            for i, a in enumerate(angles)]
    hc.tick(now + SHORT["H11"] + 0.01, fe)
    return hc


def test_h11_quiet_bystanders_pass() -> None:
    hc = _run_h11({"gas": [20.0] * 30, "oil": [45.0] * 30})
    assert hc.results["H11"].status == "pass", hc.results["H11"].measured
    print("  H11 quiet bystanders   : parked servos held station -> PASS")


def test_h11_single_disturbed_points_at_that_unit() -> None:
    """One bystander moving is that unit's own wiring, per QSG p.143."""
    hc = _run_h11({"gas": [20.0] * 30,
                   "oil": [45.0 + (0.9 if i % 5 == 0 else 0.0) for i in range(30)]})
    r = hc.results["H11"]
    assert r.status == "fail", r.measured
    assert "Only oil was disturbed" in r.measured, r.measured
    assert "CAN wiring" not in r.measured, "must not blame the bus for a single unit"
    print("  H11 one disturbed      : correctly points at that unit —", r.measured[-92:])


def test_h11_multiple_disturbed_points_at_the_bus() -> None:
    """Several at once is wiring/screening, NOT the servo that was moving."""
    wobble = [0.9 if i % 5 == 0 else 0.0 for i in range(30)]
    hc = _run_h11({"gas": [20.0 + w for w in wobble],
                   "oil": [45.0 + w for w in wobble]})
    r = hc.results["H11"]
    assert r.status == "fail", r.measured
    assert "CAN wiring or cable" in r.measured, r.measured
    print("  H11 several disturbed  : correctly points at the bus —", r.measured[-96:])


def test_h11_single_actuator_bus_is_not_a_failure() -> None:
    hc = _run_h11({})
    assert hc.results["H11"].status == "pass"
    assert "not assessable" in hc.results["H11"].measured
    print("  H11 lone actuator      : no bystanders -> PASS, not a false FAIL")


def test_h5_speed_too_fast_fails() -> None:
    fe = FakeEngine()
    hc = HealthCheckRun("air", "SN1", "Bijak", durations=SHORT)
    now = _advance_to(hc, fe, "H5")
    fe._servos["air"]["moving"] = True
    now = hc.step_started_at + 0.05
    hc.tick(now, fe)
    now = hc.step_started_at + 1.0   # 25 deg in 1.0s == 25 deg/s, way over MOVE_SPEED_MAX
    fe._servos["air"].update(moving=False, actual_deg=hc._h5_target, arrived_at=now)
    hc.tick(now, fe)
    assert hc.results["H5"].status == "fail", hc.results["H5"].measured
    print("  H5 too-fast fault      : 25 deg/s arrival correctly FAILs —", hc.results["H5"].measured)


def test_h5_stall_watchdog_timeout_fails() -> None:
    fe = FakeEngine()
    hc = HealthCheckRun("air", "SN1", "Bijak", durations=SHORT)
    now = _advance_to(hc, fe, "H5")
    fe._servos["air"]["moving"] = True
    now = hc.step_started_at + 0.05
    hc.tick(now, fe)
    # moving stays True the whole window -- never arrives, never seen False
    now = hc.step_started_at + hc.durations["H5"] + 0.01
    hc.tick(now, fe)
    assert hc.results["H5"].status == "fail"
    assert "did not settle" in hc.results["H5"].measured
    print("  H5 stall fault         : never-arriving move correctly FAILs on timeout —",
          hc.results["H5"].measured)


def test_h5_move_refused_fails_immediately() -> None:
    fe = FakeEngine()
    fe.request_move_error = RuntimeError("air: no actual frame within 1.5s")
    hc = HealthCheckRun("air", "SN1", "Bijak", durations=SHORT)
    now = _advance_to(hc, fe, "H5")
    assert hc.results["H5"].status == "fail"
    assert "move refused" in hc.results["H5"].measured
    now = hc.step_started_at + 0.01
    hc.tick(now, fe)
    assert hc.current_id == "H10", "a refused move must still advance the run"
    print("  H5 move refused        : engine refusal correctly FAILs and advances —",
          hc.results["H5"].measured)


def test_h6_hysteresis_fail() -> None:
    fe = FakeEngine()
    hc = HealthCheckRun("air", "SN1", "Bijak", durations=SHORT)
    now = _advance_to(hc, fe, "H5")
    fe._servos["air"]["moving"] = True
    now = hc.step_started_at + 0.05
    hc.tick(now, fe)
    now = hc.step_started_at + 25.0 / 3.0
    fe._servos["air"].update(moving=False, actual_deg=hc._h5_target, arrived_at=now)
    hc.tick(now, fe)
    # H10 sits between the move and the return -- pass through it to reach H6.
    assert hc.current_id == "H10", hc.current_id
    hc.durations["H10"] = 400.0
    now = _drive_h10_sweep(hc, fe, now)
    now = _clear_h11(hc, fe, now)
    assert hc.current_id == "H6", hc.current_id
    fe._servos["air"]["moving"] = True
    now = hc.step_started_at + 0.05
    hc.tick(now, fe)
    now = hc.step_started_at + 25.0 / 3.0
    # Deliberately well over HYSTERESIS_MAX. Derived from the constant rather
    # than hardcoded: this asserted 0.3 while the limit was 0.1, and silently
    # became a no-op fault when the limit was raised to 0.3 on measurement.
    offset = HYSTERESIS_MAX + 0.5
    fe._servos["air"].update(moving=False, actual_deg=hc._h6_target + offset, arrived_at=now)
    hc.tick(now, fe)
    assert hc.results["H6"].status == "fail", hc.results["H6"].measured
    print(f"  H6 hysteresis fault    : {offset:.1f} deg return offset correctly FAILs "
          f"(limit {HYSTERESIS_MAX}) — {hc.results['H6'].measured}")


def test_h8_accumulates_across_steps_independently_of_windows() -> None:
    fe = FakeEngine()
    hc = HealthCheckRun("air", "SN1", "Bijak", durations=SHORT)
    now = _advance_to(hc, fe, "H3")
    dur = hc.durations["H3"]
    n = max(20, round(dur * 4.96))
    for i in range(1, n + 1):
        now = hc.step_started_at + dur * i / n
        # S out of band on every sample -- H3's own std/pp/drift criteria don't
        # look at S at all, so H3 stays clean while H8's separate accumulator
        # (fed every tick regardless of current step) correctly sees it.
        fe.push(make_sample("air", now, 42.0, moving=False, redundant_sum_in_band=False, s_hex="0x9000"))
        hc.tick(now, fe)
    now = hc.step_started_at + dur + 0.01
    hc.tick(now, fe)
    assert hc.results["H3"].status == "pass", hc.results["H3"].measured
    # _advance_to also fed in-band samples while passing through H2 -- H8
    # accumulates from H1 onward, across every step, so the point here is
    # only that the H3-phase out-of-band samples pulled the ratio down.
    assert hc.h8["actual_total"] > 0
    assert hc.h8["s_in_band"] < hc.h8["actual_total"], \
        "out-of-band H3 samples must show up in the whole-run accumulator"
    print("  H8 accumulator         : out-of-band S tracked continuously, independent of H3's own window —",
          hc._live_h8_summary())


def test_h8_finalize_pass_and_fail() -> None:
    hc = HealthCheckRun("air", "SN1", "Bijak")
    hc.h8 = {"actual_total": 1000, "s_in_band": 995, "s_values": [0x8446, 0x844F], "crc_pass": 999, "inverse_mismatch": 0, "status_marked_mismatch": 0}
    hc._finalize_h8()
    assert hc.results["H8"].status == "pass", hc.results["H8"].measured

    hc2 = HealthCheckRun("air", "SN1", "Bijak")
    hc2.h8 = {"actual_total": 1000, "s_in_band": 900, "s_values": [0x8000, 0x9000], "crc_pass": 999, "inverse_mismatch": 0, "status_marked_mismatch": 0}
    hc2._finalize_h8()
    assert hc2.results["H8"].status == "fail", hc2.results["H8"].measured

    hc3 = HealthCheckRun("air", "SN1", "Bijak")
    hc3.h8 = {"actual_total": 1000, "s_in_band": 998, "s_values": [0x8446, 0x844F], "crc_pass": 998, "inverse_mismatch": 1, "status_marked_mismatch": 0}
    hc3._finalize_h8()
    assert hc3.results["H8"].status == "fail", hc3.results["H8"].measured   # a single mismatch is disqualifying
    print("  H8 finalize             : band/spread/CRC/mismatch aggregation pass+fail both verified")


def test_abort_marks_current_aborted_and_rest_skipped() -> None:
    fe = FakeEngine()
    hc = HealthCheckRun("air", "SN1", "Bijak", durations=SHORT)
    hc.start(0.0, fe)
    fe._servos["air"].update(presence="live", actual_deg=42.0)
    hc.tick(0.5, fe)
    assert hc.current_id == "H2"
    hc.abort(fe, "operator abort")
    assert hc.finished and hc.aborted and hc.verdict == "ABORTED"
    assert hc.results["H2"].status == "aborted"
    assert hc.results["H3"].status == "skipped"
    assert hc.results["H9"].status == "skipped"
    assert fe.holds >= 1
    print("  abort()                : current step -> aborted, rest -> skipped, HOLD requested")


class _RealStubTransport:
    """Same shape as test_engine_offline.StubTransport -- kept local so this
    file stays self-contained. Used for the one test below that must run
    against a real BusEngine: FakeEngine's set_pending_target/request_move
    are synchronous, so it structurally cannot catch a bug in how
    HealthCheckRun deals with BusEngine's real single-writer intent queue."""

    def __init__(self):
        self.written: list[tuple[int, int, str]] = []
        self._on_frame = None

    def open(self, on_frame, on_unparsed) -> None:
        self._on_frame = on_frame

    def close(self) -> None:
        pass

    def write_frame(self, can_id: int, dlc: int, data_hex: str) -> None:
        self.written.append((can_id, dlc, data_hex))

    def inject_actual(self, can_id: int, angle_deg: float) -> None:
        raw = int(round((angle_deg + 1238.8) / 0.1))
        p = raw.to_bytes(2, "big")
        q = (0x1000).to_bytes(2, "big")
        b6 = 0x00
        crc = protocol.crc8_msb(bytes([can_id & 0xFF]) + p + q + bytes([b6]))
        b7 = 0x00 ^ crc ^ 0x31
        data = p + q + bytes([0x5A, 0x00, b6, b7])
        frame = protocol.CANFrame(received_monotonic=time.monotonic(), can_id=can_id,
                                   dlc=8, data=data, raw_line="")
        self._on_frame(frame)


def _wait_until(predicate, timeout: float = 3.0, interval: float = 0.02) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(interval)
    return False


def test_h5_against_real_busengine_intent_queue_ordering() -> None:
    """The bug this guards: _h5_setup() used to call set_pending_target()
    then request_move() back to back. set_pending_target() only queues an
    intent against the real BusEngine (applied on the TX thread's NEXT
    tick), so request_move()'s synchronous pending_target-is-set precheck
    would always see the stale (pre-attach) value and refuse -- every real
    H5/H6 would fail with 'no pending target set', 100% of the time, even
    though every FakeEngine-based test above stayed green because
    FakeEngine's methods are synchronous. Only a real BusEngine can catch
    this ordering bug -- that's the whole reason this test exists.

    A background thread keeps injecting a fresh actual frame throughout, so
    request_move()'s SEPARATE freshness precheck (FRESH_FOR_MOVE_S=1.5s)
    never trips and masks the thing being tested here."""
    transport = _RealStubTransport()
    eng = BusEngine(transport)
    eng.start("active")
    stop = threading.Event()

    def feeder():
        while not stop.is_set():
            transport.inject_actual(0x18A, 42.0)
            time.sleep(0.2)

    feeder_thread = threading.Thread(target=feeder, daemon=True)
    feeder_thread.start()
    try:
        assert _wait_until(lambda: eng.snapshot()["servos"]["air"]["presence"] == "live")

        hc = HealthCheckRun("air", "SN1", "Bijak", durations=SHORT)
        eng.attach_procedure(hc)
        assert _wait_until(lambda: hc.results["H0"].status == "pass")
        assert _wait_until(lambda: hc.results["H1"].status == "pass", timeout=5.0)
        assert _wait_until(lambda: hc.current_id == "H5",
                            timeout=SHORT["H2"] + SHORT["H3"] + SHORT["H4"] + 5.0), \
            f"never reached H5, stuck at {hc.current_id}"

        # H5 must actually have been allowed to command a move -- not
        # immediately fail with "move refused: ... no pending target set".
        assert _wait_until(lambda: hc._h5_move_requested, timeout=3.0), \
            "request_move() was never even attempted"
        assert hc.results["H5"].status != "fail" or "no pending target set" not in hc.results["H5"].measured, \
            hc.results["H5"].measured
        assert eng.snapshot()["servos"]["air"]["pending_target_deg"] == hc._h5_target
        print("  H5 vs real BusEngine   : set_pending_target()->request_move() ordering "
              "correctly survives the real intent queue (no false 'no pending target set')")
    finally:
        stop.set()
        feeder_thread.join(timeout=2.0)
        eng.shutdown_bus()


def main() -> int:
    print("HealthCheckRun offline regression (no hardware, no wall-clock waiting)")
    test_h0_preflight_and_h1_wake()
    test_h0_fails_when_bus_not_active()
    test_h1_timeout_fails_but_advances()
    test_h2_link_quality_pass_and_fail()
    test_h3_pass_steady()
    test_h3_fault_injection_jitter_fails()
    test_h4_crc_failure_fails_even_if_settled()
    test_h5_and_h6_pass_roundtrip()
    test_h10_clean_sweep_passes()
    test_h10_arrival_error_fails()
    test_h10_transit_jump_fails_only_that_leg()
    test_h11_quiet_bystanders_pass()
    test_h11_single_disturbed_points_at_that_unit()
    test_h11_multiple_disturbed_points_at_the_bus()
    test_h11_single_actuator_bus_is_not_a_failure()
    test_h5_speed_too_fast_fails()
    test_h5_stall_watchdog_timeout_fails()
    test_h5_move_refused_fails_immediately()
    test_h6_hysteresis_fail()
    test_h8_accumulates_across_steps_independently_of_windows()
    test_h8_finalize_pass_and_fail()
    test_abort_marks_current_aborted_and_rest_skipped()
    test_h5_against_real_busengine_intent_queue_ordering()
    print("PASS: H0-H9 state machine, pass/fail criteria, H8's continuous accumulator, "
          "abort-safe partial results, the H3 fault-injection case, and the real-BusEngine "
          "intent-queue ordering all verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
