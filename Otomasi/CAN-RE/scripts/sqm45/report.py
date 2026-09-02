"""Health-check report: one self-contained PDF a technician can hand over.

Design goals, in priority order
------------------------------
1. **Readable without this codebase.** Every step says what it PROVES in plain
   language, not just a number and a threshold. Someone reading the PDF six
   months from now, who has never opened healthcheck.py, must be able to tell
   what was tested and whether the servo is fit to fit.
2. **Honest about limits.** The report states what it CANNOT see -- there is no
   independent shaft encoder, so a stable scale or offset error in the Hall
   sensor is invisible to it, and the accuracy figures are bounded by our own
   stopping tolerance rather than by the actuator. A test report that implies
   more certainty than it has is worse than no report.
3. **Traceable.** Serial, technician, work order, adapter, bitrate, sequence
   SHA, wall-clock start and duration, and the exact CAN identifiers involved.
   A result nobody can tie back to a specific unit and a specific rig is not
   evidence.

Rendered with PyMuPDF's Story (HTML/CSS -> paginated PDF). Story is used rather
than hand-placed text because the step tables have unpredictable length --
measured strings run long when a step fails, which is exactly when the report
matters most -- and Story reflows across pages instead of clipping.
"""

from __future__ import annotations

import html
import time
from pathlib import Path

import fitz

from . import roster
from ._version import __version__ as APP_VERSION

# What each step is FOR, in the language of the machine rather than the code.
# Keyed by step id so a renumbering or reordering cannot desynchronise them.
STEP_PURPOSE = {
    "H0": "Confirms the instrument itself is sane before it judges anything: the bus is "
          "transmitting, the replay sequence is the expected file, and the protocol decoder "
          "passes its own self-test.",
    "H1": "How quickly the actuator answers once traffic starts. A slow or absent wake points "
          "at power, wiring or the CAN link rather than the mechanism.",
    "H2": "Quality of the CAN link itself, NOT the servo: frame rate, the largest hole in the "
          "stream, and any malformed frames. A failure here usually means the cable, the "
          "converter or bus loading -- check those before suspecting the actuator.",
    "H3": "With nothing commanding it, does the reported position stay still? Drift or noise "
          "here is the position sensor or its wiring, since the shaft is not being driven.",
    "H4": "The same stillness test, but with the suspected interference source energised "
          "(typically the ignition transformer). This is the step that reproduces an "
          "EMI complaint.",
    "H5": "Does it obey a command? Travel speed, how close it stops to the target, whether it "
          "overshoots, and -- new -- whether the position tracked smoothly ON THE WAY rather "
          "than jumping or reversing.",
    "H10": "The angles a burner controller actually parks an actuator at across one cycle "
           "(home, prepurge, ignition, low-fire, postpurge). A servo can be accurate at "
           "mid-travel and sloppy near an end stop; testing one angle would hide that.",
    "H11": "While THIS actuator was driven, did the others hold station? Several actuators "
           "disturbed together points at CAN wiring or cable screening; one alone points at "
           "that unit. This is the distinction a single-servo test cannot make.",
    "H6": "Does it return to where it started? The gap between the outbound and return "
          "positions is mechanical hysteresis -- backlash in the coupling or the gear train.",
    "H7": "Stillness again, but AFTER the motor has run. Catches instability that only appears "
          "once the drive has been exercised and is warm.",
    "H8": "Runs across the whole session: the actuator's own redundant position word and "
          "check byte are verified on every frame. Failures here mean the actuator is "
          "reporting internally inconsistent data.",
    "H9": "Leaves the rig in a known safe state -- HOLD asserted, transmission continuing so "
          "the actuator stays powered, and the shaft returned to where it was found.",
}

GLOSSARY = [
    ("std", "Standard deviation of the reported angle over the window. How much the number "
            "wobbles; 0.000 means perfectly steady."),
    ("p-p", "Peak-to-peak: the full spread between the smallest and largest reading."),
    ("drift", "First half of the window compared with the second half. Separates a slow one-way "
              "creep from symmetric noise."),
    ("MOVING", "Percentage of frames where the actuator asserted its own moving flag. Should be "
               "near 0 when parked and near 100 during a commanded move."),
    ("transit", "Behaviour during travel: largest step between consecutive samples, number of "
                "direction reversals, and the largest gap in the stream."),
    ("S in band", "The actuator encodes its position twice per frame. This is the share of frames "
                  "where the two encodings agree."),
    ("CRC pass", "Share of frames whose check byte validated -- the actuator's own integrity field."),
    ("hysteresis", "Difference between the starting angle and the angle after returning to it."),
]


def _esc(v) -> str:
    return html.escape("" if v is None else str(v))


def _status_style(status: str) -> tuple[str, str]:
    return {
        "pass": ("#17734A", "#E3F2EA"),
        "fail": ("#B3261E", "#FDE4E4"),
        "aborted": ("#A96A00", "#FBF1DB"),
        "skipped": ("#8B93A1", "#F1F3F6"),
        "pending": ("#8B93A1", "#F1F3F6"),
        "running": ("#2D62C4", "#E9F0FD"),
    }.get(status, ("#565E6D", "#F1F3F6"))


def _kv_rows(pairs) -> str:
    out = []
    for k, v in pairs:
        if v in (None, ""):
            continue
        out.append(f'<tr><td class="k">{_esc(k)}</td><td class="v">{_esc(v)}</td></tr>')
    return "".join(out)


def _split_detail(measured: str) -> list[str]:
    """H10/H11 pack per-leg or per-bystander results into one ' | ' string."""
    if not measured:
        return []
    parts = [p.strip() for p in measured.split(" | ")]
    return parts if len(parts) > 1 else []


def build_html(status: dict, context: dict) -> str:
    verdict = status.get("verdict") or ("ABORTED" if status.get("aborted") else "INCOMPLETE")
    vcol, vbg = _status_style("pass" if verdict == "PASS" else
                              "aborted" if verdict == "ABORTED" else "fail")
    role = status.get("role") or "?"
    address = roster.address_for_name(role)
    label = roster.label_for(address) if address else role.upper()
    tid = roster.target_id(address) if address else None

    ident = _kv_rows([
        ("Actuator", f"{label}  (address {address}, node {roster.node_for(address)})"
                     if address else label),
        ("Feedback identifier", f"0x{roster.actual_id(address):03X}" if address else "-"),
        ("Command identifier", f"0x{tid:03X}" if tid else "none (monitor-only address)"),
        ("Serial number", status.get("serial")),
        ("Technician", status.get("technician")),
        ("Work order", status.get("work_order")),
    ])
    rig = _kv_rows([
        ("Adapter", context.get("source_label")),
        ("CAN bitrate", context.get("bitrate", "50 kbps")),
        ("Bus mode", context.get("mode")),
        ("Other actuators present", context.get("bystanders") or "none detected"),
        ("Replay sequence SHA", status.get("sequence_sha")),
        ("Started", context.get("started_wall")),
        ("Duration", f"{status.get('elapsed_s'):.0f} s" if status.get("elapsed_s") else None),
        ("As-found angle", f"{status.get('as_found_angle'):.2f}°"
                           if status.get("as_found_angle") is not None else None),
        ("Interference source (H4)", {"applied": "energised during H4",
                                      "not_available": "NOT available on this rig"}
                                     .get(status.get("ht_choice"), "not recorded")),
    ])

    steps = status.get("steps", [])
    summary_cells = []
    for i, s in enumerate(steps, 1):
        col, bg = _status_style(s["status"])
        summary_cells.append(
            f'<td class="sumcell" style="background:{bg};color:{col}">'
            f'<b>{_esc(s["id"])}</b><br/>{_esc(s["status"].upper())}</td>')
    # six per row keeps the grid readable at A4 width
    summary_rows = "".join(
        "<tr>" + "".join(summary_cells[i:i + 6]) + "</tr>"
        for i in range(0, len(summary_cells), 6))

    detail = []
    for i, s in enumerate(steps, 1):
        col, bg = _status_style(s["status"])
        purpose = STEP_PURPOSE.get(s["id"], "")
        legs = _split_detail(s.get("measured") or "")
        if legs:
            body = "".join(f'<div class="leg">{_esc(p)}</div>' for p in legs)
        else:
            body = f'<div class="meas">{_esc(s.get("measured") or "-")}</div>'
        detail.append(f"""
        <table class="step"><tr>
          <td class="stepno">{i}</td>
          <td class="stepbody">
            <div class="steptitle"><b>{_esc(s["id"])}</b> &nbsp; {_esc(s["name"])}
              <span class="pill" style="background:{bg};color:{col}">{_esc(s["status"].upper())}</span>
            </div>
            <div class="purpose">{_esc(purpose)}</div>
            <div class="lbl">Measured</div>{body}
            <div class="lbl">Pass criterion</div>
            <div class="crit">{_esc(s.get("criterion"))}</div>
            <div class="base">Reference: {_esc(s.get("baseline"))}</div>
          </td></tr></table>""")

    gloss = "".join(f'<tr><td class="gk">{_esc(k)}</td><td class="gv">{_esc(v)}</td></tr>'
                    for k, v in GLOSSARY)

    # The banner must never overstate the result. An ABORTED run has no "fail"
    # steps -- they are "aborted"/"skipped" -- so keying only on failures printed
    # "All steps met their criteria" on a run that never finished. In a document
    # somebody may file against a work order, that is the worst possible defect.
    failed = [s for s in steps if s["status"] == "fail"]
    incomplete = [s for s in steps if s["status"] in ("pending", "skipped", "aborted", "running")]
    if status.get("aborted"):
        reason = status.get("abort_reason") or "no reason recorded"
        head = ("<div class='banner fail'><b>Run aborted -- this is NOT a pass.</b> "
                f"Reason: {_esc(reason)}. "
                + (f"{len(incomplete)} of {len(steps)} steps did not run, so no conclusion may "
                   "be drawn about them. " if incomplete else "")
                + "Any step marked PASS below was genuinely measured and remains valid on its "
                  "own; the ones that never ran are simply unknown.</div>")
    elif failed:
        head = ("<div class='banner fail'><b>Attention.</b> "
                + _esc(", ".join(s["id"] for s in failed))
                + " did not meet criteria. Read those steps' Measured lines before "
                  "condemning the actuator -- H2 in particular reports the CAN link and "
                  "the converter, not the servo.</div>")
    elif incomplete or not status.get("finished"):
        head = ("<div class='banner fail'><b>Incomplete run.</b> "
                f"{len(incomplete)} of {len(steps)} steps did not produce a result, so this is "
                "not a verdict on the actuator. Re-run before drawing any conclusion.</div>")
    else:
        head = ("<div class='banner ok'>All steps met their criteria. Note the limitations "
                "below: this instrument reads the actuator's own reported position and has "
                "no independent shaft encoder.</div>")

    return f"""<html><head><style>
      body {{ font-family: sans-serif; font-size: 9pt; color: #171A21; }}
      h1 {{ font-size: 17pt; margin: 0 0 2pt 0; }}
      h2 {{ font-size: 11pt; margin: 14pt 0 4pt 0; border-bottom: 1px solid #C6CBD4;
            padding-bottom: 2pt; }}
      .sub {{ color: #565E6D; font-size: 8.5pt; margin-bottom: 8pt; }}
      .verdict {{ background: {vbg}; color: {vcol}; padding: 8pt 10pt; font-size: 15pt;
                  font-weight: bold; }}
      .banner {{ padding: 6pt 8pt; font-size: 8.5pt; margin: 6pt 0 2pt 0; }}
      .banner.ok {{ background: #E3F2EA; color: #17734A; }}
      .banner.fail {{ background: #FDE4E4; color: #B3261E; }}
      table {{ width: 100%; }}
      /* ABSOLUTE widths, not percentages. Story honours pt widths on table
         cells but silently ignores percentage widths on a single column, which
         drew the label and the value on top of each other. The step table
         below always worked because its first cell is sized in pt. */
      td.k {{ color: #565E6D; width: 145pt; padding: 2pt 0; vertical-align: top; }}
      td.v {{ width: 350pt; padding: 2pt 0; vertical-align: top; }}
      td.sumcell {{ text-align: center; padding: 5pt 2pt; font-size: 8pt; width: 82pt; }}
      table.step {{ margin: 5pt 0; }}
      td.stepno {{ width: 18pt; color: #8B93A1; font-size: 12pt; vertical-align: top; }}
      td.stepbody {{ vertical-align: top; }}
      .steptitle {{ font-size: 10pt; margin-bottom: 2pt; }}
      .pill {{ padding: 1pt 5pt; font-size: 7.5pt; }}
      .purpose {{ color: #565E6D; font-size: 8pt; margin-bottom: 4pt; }}
      .lbl {{ color: #8B93A1; font-size: 7pt; margin-top: 3pt; }}
      .meas, .leg {{ font-family: monospace; font-size: 8pt; background: #F6F7F9;
                     padding: 3pt 5pt; margin-top: 1pt; }}
      .crit {{ font-size: 8pt; }}
      .base {{ color: #8B93A1; font-size: 7.5pt; margin-top: 2pt; }}
      td.gk {{ font-family: monospace; width: 95pt; vertical-align: top; padding: 2pt 0; }}
      td.gv {{ width: 400pt; font-size: 8.5pt; padding: 2pt 0; vertical-align: top; }}
      .limit {{ background: #FBF1DB; padding: 7pt 9pt; font-size: 8.5pt; }}
      .foot {{ color: #8B93A1; font-size: 7.5pt; margin-top: 10pt; }}
    </style></head><body>

    <h1>Actuator Health Check</h1>
    <div class="sub">Siemens SQM4x on an LMV5-family CAN bus &nbsp;·&nbsp;
      report generated {_esc(context.get('generated_wall'))}</div>

    <div class="verdict">VERDICT: {_esc(verdict)} &nbsp;—&nbsp; {_esc(label)}</div>
    {head}

    <h2>Identification</h2><table>{ident}</table>
    <h2>Rig &amp; session</h2><table>{rig}</table>

    <h2>Summary</h2>
    <table>{summary_rows}</table>

    <h2>Detailed results</h2>
    {''.join(detail)}

    <h2>How to read the numbers</h2>
    <table>{gloss}</table>

    <h2>Limitations</h2>
    <div class="limit">
      Every angle in this report is the actuator's <b>own internally reported position</b>,
      read over CAN. There is no independent shaft encoder on this rig, so a stable scale or
      offset error in the position sensor is invisible to this instrument -- the actuator
      would report the wrong angle consistently and pass.<br/><br/>
      The settle-error and hysteresis figures are bounded by the tolerance at which this tool
      stops the shaft, so they characterise the test loop at least as much as the actuator.
      Treat them as an upper bound on error, not a measurement of accuracy.<br/><br/>
      H2 measures the CAN link and the USB converter, not the mechanism. A converter that drops
      frames fails H2 with a perfectly healthy servo attached.
    </div>

    <div class="foot">Generated by LMV Actuator Checker v{_esc(APP_VERSION)}.
      Steps run in the order shown; step identifiers are permanent and are never renumbered,
      so an identifier means the same test in every report ever issued. Recorded against a
      specific build so a result can be traced back to exactly what produced it.</div>
    </body></html>"""


def write_pdf(html_text: str, path: Path) -> Path:
    """Render to A4, flowing across as many pages as the content needs."""
    path.parent.mkdir(parents=True, exist_ok=True)
    story = fitz.Story(html=html_text)
    writer = fitz.DocumentWriter(str(path))
    page_rect = fitz.paper_rect("a4")
    content_rect = page_rect + (40, 44, -40, -40)   # margins
    more = True
    while more:
        device = writer.begin_page(page_rect)
        more, _ = story.place(content_rect)
        story.draw(device)
        writer.end_page()
    writer.close()
    return path


def report_filename(status: dict) -> str:
    stamp = time.strftime("%Y%m%d_%H%M%S")
    role = (status.get("role") or "servo").upper()
    serial = "".join(c for c in (status.get("serial") or "noserial")
                     if c.isalnum() or c in "-_")[:24]
    verdict = (status.get("verdict") or "INCOMPLETE").upper()
    return f"healthcheck_{role}_{serial}_{verdict}_{stamp}.pdf"


def generate(status: dict, context: dict, directory: Path) -> Path:
    return write_pdf(build_html(status, context), directory / report_filename(status))
