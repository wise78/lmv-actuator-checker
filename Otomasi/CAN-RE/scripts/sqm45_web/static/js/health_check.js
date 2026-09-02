// Health Check page: start a run, watch it progress through H0-H9, see the
// verdict once finished. Socket, gauge geometry, safety bar and log all come
// from common.js. The server (sqm45/healthcheck.py, ticked from the TX
// thread) is the sole source of truth for step status -- this file only
// renders whatever hc_status carries.
//
// Render model: the steps <tbody> rows are built ONCE and patched -- the
// previous code replaced the whole tbody at 10 Hz during a run AND forever
// after it finished (the server used to keep streaming a fresh "measured"
// string nobody needed). Rows are keyed by step id; pill/status/class get
// patched only when they actually change, the measured CELL every tick
// (cheap textContent write, no DOM churn). The verdict re-render is guarded
// by a JSON compare so a finished run costs nothing.

const STATUS_LABEL = { pending: "—", running: "RUNNING", pass: "PASS", fail: "FAIL",
                        aborted: "ABORTED", skipped: "SKIPPED" };

let hcRole = null;              // chosen once the roster arrives
let forcingSetup = false;
let latestHcStatus = null;
let lastHcJson = null;

const hcRoleTabs = document.getElementById("hcRoleTabs");

// Tabs come from the server roster. H5/H6/H10 command real moves, so an
// actuator whose target frame is unproven must not be selectable here at all
// -- a health check that cannot command is not a health check, and offering
// it would produce a run that fails for a reason the servo is not to blame for.
onRoster((roles) => {
  hcRoleTabs.replaceChildren();
  for (const r of roles) {
    const meta = ROLE_META[r] || {};
    const btn = document.createElement("button");
    btn.dataset.role = r;
    btn.textContent = ROLE_LABEL[r] || r.toUpperCase();
    if (!meta.commandable) {
      btn.disabled = true;
      btn.classList.add("tab-absent");
      btn.title = "monitor only -- no proven target frame, so H5/H6/H10 cannot command it";
    } else if (meta.experimental) {
      btn.classList.add("tab-experimental");
      btn.title = `UNVERIFIED target constant. A run here also TESTS ${meta.target_id_hex}: `
                + `if the constant is wrong the actuator rejects the frames and H5 reports `
                + `"move never started" -- that is a finding about our decode, not the servo.`;
    }
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      hcRole = r;
      hcRoleTabs.querySelectorAll("button").forEach((b) => b.classList.toggle("active", b === btn));
      renderExperimentalNote();
    });
    hcRoleTabs.appendChild(btn);
  }
  const first = roles.find((r) => (ROLE_META[r] || {}).commandable);
  if (!hcRole || !(ROLE_META[hcRole] || {}).commandable) hcRole = first || null;
  hcRoleTabs.querySelectorAll("button").forEach(
    (b) => b.classList.toggle("active", b.dataset.role === hcRole));
  renderExperimentalNote();
});

function renderExperimentalNote() {
  const box = document.getElementById("hcExperimentalNote");
  if (!box) return;
  const meta = ROLE_META[hcRole] || {};
  const on = !!(meta.commandable && meta.experimental);
  box.hidden = !on;
  if (on) {
    box.textContent =
      `${ROLE_LABEL[hcRole]}: target ${meta.target_id_hex} is the last channel slot and is solid, `
      + `but its integrity constant has never been observed. This run doubles as the test of that `
      + `constant — if H5 reports "move never started", suspect the constant before the actuator.`;
  }
}

function updateHcRoleAvailability(snapshot) {
  hcRoleTabs.querySelectorAll("button").forEach((btn) => {
    const r = btn.dataset.role;
    const meta = ROLE_META[r] || {};
    if (!meta.commandable) return;          // already disabled with its own reason
    const present = snapshot.servos[r] && snapshot.servos[r].presence !== "absent";
    btn.disabled = !present;
    btn.title = present ? (btn.title || "") : "not detected on the bus";
  });
}

document.getElementById("btnStartHc").addEventListener("click", () => {
  const serial = document.getElementById("hcSerial").value.trim();
  const technician = document.getElementById("hcTechnician").value.trim();
  const work_order = document.getElementById("hcWorkOrder").value.trim();
  if (!serial || !technician) { alert("Serial number and technician name are both required."); return; }
  if (!hcRole) { alert("No commandable actuator is available to test."); return; }
  socket.emit("start_health_check", { role: hcRole, serial, technician, work_order });
});

document.getElementById("btnAbortHc").addEventListener("click", () => {
  if (confirm("Abort the health check?\n\nHOLD will be asserted, transmission continues, and a partial (ABORTED) result is recorded.")) {
    socket.emit("abort_health_check");
  }
});

document.getElementById("btnHtApplied").addEventListener("click", () => socket.emit("health_check_ht_choice", { choice: "applied" }));
document.getElementById("btnHtNotAvailable").addEventListener("click", () => socket.emit("health_check_ht_choice", { choice: "not_available" }));

document.getElementById("btnNewHc").addEventListener("click", () => {
  forcingSetup = true;
  applyView({ started: false, finished: false });
  document.getElementById("hcSerial").value = "";
  document.getElementById("hcTechnician").value = "";
  document.getElementById("hcWorkOrder").value = "";
});

function applyView(status) {
  const showSetup = forcingSetup || !status.started;
  const showRunning = !showSetup && status.started && !status.finished;
  const showVerdict = !showSetup && status.finished;
  document.getElementById("hcSetup").style.display = showSetup ? "" : "none";
  document.getElementById("hcRunning").style.display = showRunning ? "" : "none";
  document.getElementById("hcVerdict").style.display = showVerdict ? "" : "none";
}

function fmtDeg(v) { return v == null ? "—" : v.toFixed(2) + "°"; }

// ------------------------------------------------------------- steps table --
// One row store per tbody selector; rows built once per id-set, patched after.

const stepsStores = {};

function patchStepsInto(tbodySelector, status) {
  const tbody = document.querySelector(tbodySelector);
  let store = stepsStores[tbodySelector];
  const ids = status.steps.map((s) => s.id).join(",");
  if (!store || store.ids !== ids) {
    tbody.innerHTML = "";
    store = { ids, rows: {} };
    // The '#' column is the order steps RUN in; s.id is permanent identity.
    // They deliberately differ: H10/H11 were added between H5 and H6 and the
    // existing ids were NOT renumbered, because 108 references across
    // FINDINGS.md (H6 alone appears 13 times meaning 'Return & repeatability')
    // would otherwise mean two different tests depending on when they were
    // written. Showing both keeps the sequence readable without rewriting
    // history.
    status.steps.forEach((s, i) => {
      const tr = document.createElement("tr");
      tr.innerHTML = `
        <td class="mono hc-order">${i + 1}</td>
        <td class="mono">${s.id}</td>
        <td>${s.name}</td>
        <td><span class="hc-pill"></span></td>
        <td class="mono hc-measured"></td>
        <td class="hc-criterion">${s.criterion}<br><span class="hc-baseline">baseline: ${s.baseline}</span></td>`;
      tbody.appendChild(tr);
      store.rows[s.id] = { tr, pill: tr.querySelector(".hc-pill"), measured: tr.querySelector(".hc-measured"),
                           wasStatus: null, wasCur: null, lastMeasured: null };
    });
    stepsStores[tbodySelector] = store;
  }
  for (const s of status.steps) {
    const r = store.rows[s.id];
    const cur = s.id === status.current_step;
    if (r.wasStatus !== s.status || r.wasCur !== cur) {
      r.tr.className = `hc-step-row status-${s.status}${cur ? " current" : ""}`;
      r.pill.textContent = STATUS_LABEL[s.status] || s.status;
      r.pill.className = `hc-pill hc-pill-${s.status}`;
      r.wasStatus = s.status;
      r.wasCur = cur;
    }
    if (r.lastMeasured !== s.measured) {   // live readout -- cheap text swap only
      r.measured.textContent = s.measured;
      r.lastMeasured = s.measured;
    }
  }
}

function renderRunning(status) {
  document.getElementById("hcRoleLabel").textContent = ROLE_LABEL[status.role] || status.role;
  const identity = `${status.serial} · ${status.technician}${status.work_order ? " · " + status.work_order : ""}`;
  document.getElementById("hcIdentity").textContent = identity;

  const elapsed = status.elapsed_s || 0;
  const total = status.total_budget_s || 1;
  const pct = Math.max(0, Math.min(100, 100 * elapsed / total));
  document.getElementById("hcProgressFill").style.width = pct.toFixed(1) + "%";
  document.getElementById("hcProgressText").textContent =
    `T+${elapsed.toFixed(0)}s / ~${total.toFixed(0)}s`;

  const cur = status.steps.find((s) => s.id === status.current_step);
  document.getElementById("hcCurrentStep").textContent = cur ? `${cur.id} ${cur.name}` : "—";

  const snap = latestSnapshot.servos[status.role];
  document.getElementById("hcCurrentAngle").textContent = snap ? fmtDeg(snap.actual_deg) : "—";
  document.getElementById("hcAsFound").textContent = fmtDeg(status.as_found_angle);

  document.getElementById("hcHtPrompt").style.display = status.awaiting_ht_choice ? "flex" : "none";

  patchStepsInto("#hcStepsTable tbody", status);
}

function renderVerdict(status) {
  const banner = document.getElementById("hcVerdictBanner");
  const cls = status.verdict === "PASS" ? "pass" : (status.verdict === "ABORTED" ? "aborted" : "fail");
  banner.className = "hc-verdict-banner " + cls;
  const roleTxt = ROLE_LABEL[status.role] || status.role;
  let text = `${status.verdict} — ${roleTxt} · ${status.serial} · ${status.technician}`;
  if (status.aborted && status.abort_reason) text += ` (${status.abort_reason})`;
  banner.textContent = text;
  patchStepsInto("#hcVerdictStepsTable tbody", status);
}

socket.on("hc_status", (status) => {
  if (!status) return;
  const json = JSON.stringify(status);
  if (json === lastHcJson) return;   // duplicate push costs nothing
  lastHcJson = json;
  latestHcStatus = status;
  if (status.started && !status.finished) forcingSetup = false;
  applyView(status);
  if (status.started && !status.finished) renderRunning(status);
  if (status.finished) renderVerdict(status);
});

onSnapshot((snapshot) => {
  updateHcRoleAvailability(snapshot);
  // refresh the live angle readout between hc_status pushes -- all cheap
  // textContent writes now, so snapshot rate does not matter here anymore
  if (latestHcStatus && latestHcStatus.started && !latestHcStatus.finished) renderRunning(latestHcStatus);
});

// ------------------------------------------------------------- PDF report --
// The server writes the PDF and reveals it in the OS file manager, rather than
// the browser downloading it. This is a bench instrument on localhost: the
// technician wants the file sitting in a folder they can attach to a work
// order, not buried in a browser Downloads directory under a random name.

const btnReport = document.getElementById("btnReport");
const hcReportNote = document.getElementById("hcReportNote");

if (btnReport) {
  btnReport.addEventListener("click", () => {
    btnReport.disabled = true;
    btnReport.textContent = "Generating…";
    hcReportNote.className = "hint";
    hcReportNote.textContent = "";
    // Re-enable even if the server never answers, so one failed export cannot
    // leave the button dead until a reload.
    setTimeout(() => {
      if (btnReport.disabled) {
        btnReport.disabled = false;
        btnReport.textContent = "Download PDF report";
      }
    }, 15000);
    socket.emit("export_report");
  });
}

socket.on("report_ready", (p) => {
  if (!btnReport) return;
  btnReport.disabled = false;
  btnReport.textContent = "Download PDF report";
  if (p.cancelled) {                    // operator closed the Save-as dialog
    hcReportNote.className = "hint";
    hcReportNote.textContent = "Save cancelled - nothing written.";
    return;
  }
  hcReportNote.className = "hint ok";
  hcReportNote.textContent = p.opened === "opened"
    ? `Saved ${p.name} — file manager opened at that folder.`
    : `Saved to ${p.path} (${p.opened})`;
});
