// Servo Checker page: one role at a time, full command surface, and a
// frame-level inspector. Socket, gauge geometry, chart engine, safety bar
// and log all come from common.js.
//
// Render model: skeletons are built once and patched (textContent/style)
// instead of rebuilding innerHTML every snapshot -- five unsynchronised
// writers were producing ~a thousand DOM nodes a second here. The gauge is
// rebuilt ONLY when the selected role changes (it recolours); the inspector
// refreshes on its own 200 ms timer so FRAME AGE free-runs between
// snapshots, and renderAll no longer calls it a second time.

const MOVE_SPEED_DEG_S = 2.98;

let selectedRole = null;          // set once the roster arrives
const pendingAngle = {};
const asFoundAngle = {};
const latestActual = {};          // fullest 'actual' event per role, for the inspector
let firstMoveConfirmed = false;

const chart = new LiveChart(document.getElementById("scChart"), document.querySelector(".chart-wrap"),
  { windowS: 40, roles: ROLES });

// ---------------------------------------------------------------- role tabs --
// Tabs are BUILT from the roster, not written into the template. With up to
// six actuators a hard-coded pair silently hides every servo past the second.

const roleTabs = document.getElementById("roleTabs");

function buildRoleTabs(roles) {
  roleTabs.replaceChildren();
  for (const r of roles) {
    if (pendingAngle[r] == null) pendingAngle[r] = 45.0;
    if (!(r in asFoundAngle)) asFoundAngle[r] = null;
    if (!(r in latestActual)) latestActual[r] = null;
    const btn = document.createElement("button");
    btn.dataset.role = r;
    btn.textContent = ROLE_LABEL[r] || r.toUpperCase();
    btn.addEventListener("click", () => {
      if (btn.disabled) return;
      selectRole(r);
    });
    roleTabs.appendChild(btn);
  }
  if (!selectedRole || !roles.includes(selectedRole)) selectRole(roles[0]);
  else selectRole(selectedRole);
}

function selectRole(role) {
  if (!role) return;
  selectedRole = role;
  chart.setVisibleRoles([role]);
  roleTabs.querySelectorAll("button").forEach(
    (b) => b.classList.toggle("active", b.dataset.role === role));
  renderAll();
}

function updateRoleTabAvailability(snapshot) {
  roleTabs.querySelectorAll("button").forEach((btn) => {
    const r = btn.dataset.role;
    const present = snapshot.servos[r] && snapshot.servos[r].presence !== "absent";
    btn.disabled = !present;
    btn.title = present ? "" : "not detected on the bus";
    btn.classList.toggle("tab-absent", !present);
  });
  // Never leave the page pointed at a servo that has dropped off the bus --
  // every readout below would keep displaying its last values as if live.
  if (selectedRole && snapshot.servos[selectedRole]
      && snapshot.servos[selectedRole].presence === "absent") {
    const live = ROLES.find((r) => snapshot.servos[r] && snapshot.servos[r].presence !== "absent");
    if (live) selectRole(live);
  }
}

onRoster((roles) => { chart.ensureRoles(roles); buildRoleTabs(roles); });

// ---------------------------------------------------------------- gauge --

const GX = 100, GY = 104, GR = 76;
const gA0 = pt(GX, GY, GR, bear(0));
let scGauge = null;   // { role, value, target, needle, readout }

function renderGauge(snapshot) {
  const wrap = document.getElementById("scGauge");
  const s = snapshot.servos[selectedRole] || { presence: "absent", actual_deg: null, target_deg: null };
  if (!scGauge || scGauge.role !== selectedRole) {
    // full rebuild only on a role switch -- the SVG colours are per-role
    wrap.innerHTML = gaugeSvg(selectedRole, s);
    const paths = wrap.querySelectorAll("path");
    const texts = wrap.querySelectorAll("text");
    scGauge = { role: selectedRole, value: paths[1], target: paths[2], needle: paths[3], readout: texts[texts.length - 2] };
    return;
  }
  const haveActual = s.actual_deg != null;
  const actual = haveActual ? clamp(s.actual_deg, 0, 90) : 0;
  const target = s.target_deg != null ? clamp(s.target_deg, 0, 90) : 0;
  const av = pt(GX, GY, GR, bear(actual));
  const large = (240 * actual / 90) > 180 ? 1 : 0;
  const op = haveActual ? "1" : "0.25";
  scGauge.value.setAttribute("d", `M ${fp(gA0)} A ${GR} ${GR} 0 ${large} 1 ${fp(av)}`);
  scGauge.value.setAttribute("opacity", op);
  const bn = bear(actual), tip = pt(GX, GY, GR - 16, bn);
  const nl = pt(GX, GY, 5.5, bn + 90), nr = pt(GX, GY, 5.5, bn - 90);
  scGauge.needle.setAttribute("d", `M ${fp(tip)} L ${fp(nl)} L ${fp(nr)} Z`);
  scGauge.needle.setAttribute("opacity", op);
  const bt = bear(target);
  const t1 = pt(GX, GY, GR + 2, bt), t2 = pt(GX, GY, GR + 13, bt - 3.2), t3 = pt(GX, GY, GR + 13, bt + 3.2);
  scGauge.target.setAttribute("d", `M ${fp(t1)} L ${fp(t2)} L ${fp(t3)} Z`);
  scGauge.readout.textContent = haveActual ? s.actual_deg.toFixed(1) : "––.–";
}

// ---------------------------------------------------------------- readouts --

const scReadouts = {};
(function ensureReadouts() {
  const el = document.getElementById("scReadouts");
  el.innerHTML = `
    <div class="readout"><span class="k">DIALED TARGET</span><span class="v mono js-dial"></span></div>
    <div class="readout"><span class="k">DISTANCE TO GO</span><span class="v mono js-dist">—</span></div>
    <div class="readout"><span class="k">ETA @ ${MOVE_SPEED_DEG_S}°/s</span><span class="v mono js-eta">—</span></div>`;
  scReadouts.dial = el.querySelector(".js-dial");
  scReadouts.dist = el.querySelector(".js-dist");
  scReadouts.eta = el.querySelector(".js-eta");
})();

function renderReadouts(snapshot) {
  const s = snapshot.servos[selectedRole];
  const actual = s ? s.actual_deg : null;
  const dial = pendingAngle[selectedRole];
  const distance = actual != null ? Math.abs(dial - actual) : null;
  const eta = distance != null ? distance / MOVE_SPEED_DEG_S : null;
  scReadouts.dial.textContent = dial.toFixed(1) + "°";
  scReadouts.dist.textContent = distance != null ? distance.toFixed(1) + "°" : "—";
  scReadouts.eta.textContent = eta != null ? eta.toFixed(1) + " s" : "—";
}

function renderStateChip(snapshot) {
  const s = snapshot.servos[selectedRole];
  const chip = document.getElementById("scStateChip");
  if (!s) { chip.textContent = "—"; chip.style.background = "#F1F3F6"; chip.style.color = "var(--muted2)"; return; }
  const isMove = s.state_hex === "0x9E00";
  let hexSpan = chip.querySelector(".hex");
  if (!hexSpan) {
    chip.innerHTML = `<span class="hex"></span> <span class="txt"></span>`;
    hexSpan = chip.querySelector(".hex");
  }
  chip.style.background = isMove ? "var(--air-soft)" : "var(--good-soft)";
  chip.style.color = isMove ? "var(--air)" : "var(--good)";
  hexSpan.textContent = s.state_hex;
  const txt = chip.querySelector(".txt");
  txt.textContent = isMove ? "MOVE — driving to target" : "HOLD — commanding nothing";
}

// ---------------------------------------------------------------- setpoint --

function syncSetpointUI() {
  const v = pendingAngle[selectedRole];
  document.getElementById("setpointNum").textContent = v.toFixed(1);
  document.getElementById("setpointSlider").value = v;
}
function setPending(v) {
  v = clamp(Math.round(v * 2) / 2, 0, 90);
  pendingAngle[selectedRole] = v;
  syncSetpointUI();
  const m = ROLE_META[selectedRole];
  if (m && !m.commandable) return;
  socket.emit("set_pending_target", { role: selectedRole, angle: v });
}
document.getElementById("setpointSlider").addEventListener("input", (e) => setPending(parseFloat(e.target.value)));
document.getElementById("spinUp").addEventListener("click", () => setPending(pendingAngle[selectedRole] + 0.5));
document.getElementById("spinDown").addEventListener("click", () => setPending(pendingAngle[selectedRole] - 0.5));

document.getElementById("btnReturnAsFound").addEventListener("click", () => {
  const v = asFoundAngle[selectedRole];
  if (v == null) return;
  setPending(v);
});

document.getElementById("btnCommandMove").addEventListener("click", () => {
  const s = latestSnapshot.servos[selectedRole];
  if (!s || s.presence !== "live") return;
  const actual = s.actual_deg;
  const target = pendingAngle[selectedRole];
  const distance = actual != null ? Math.abs(target - actual) : null;
  if (!firstMoveConfirmed) {
    const eta = distance != null ? (distance / MOVE_SPEED_DEG_S).toFixed(0) : "?";
    const ok = confirm(
      "THE SHAFT WILL TURN. Keep it uncoupled.\n\n" +
      `${ROLE_LABEL[selectedRole]}: ${actual != null ? actual.toFixed(1) : '?'}° → ${target.toFixed(1)}°\n` +
      `Distance ${distance != null ? distance.toFixed(1) : '?'}°, ETA ~${eta} s`
    );
    if (!ok) return;
    firstMoveConfirmed = true;
  }
  const meta = ROLE_META[selectedRole];
  if (meta && !meta.commandable) return;   // server would refuse; do not pretend otherwise
  socket.emit("request_move", { role: selectedRole, angle: pendingAngle[selectedRole] });
});

// ---------------------------------------------------------------- inspector --

function fmtHex(n, digits) {
  return n == null ? "—" : "0x" + n.toString(16).toUpperCase().padStart(digits, "0");
}

const INSPECTOR_KEYS = ["ACTUAL", "REDUNDANT SUM S", "BYTE-7 CHECK", "MARKER",
                        "MOVING BIT", "INVERSE MOVING", "CYCLE COUNTER", "FRAME AGE"];
const inspCells = {};   // key -> { v, note }
(function ensureInspectorSkeleton() {
  const el = document.getElementById("scInspector");
  el.innerHTML = INSPECTOR_KEYS.map((k) => `
    <div class="inspector-cell">
      <span class="k">${k}</span>
      <span class="v mono"></span>
      <span class="note"></span>
    </div>`).join("");
  el.querySelectorAll(".inspector-cell").forEach((cell) => {
    inspCells[cell.querySelector(".k").textContent] =
      { v: cell.querySelector(".v"), note: cell.querySelector(".note") };
  });
})();

function setCell(key, val, note, flag) {
  const c = inspCells[key];
  c.v.textContent = val;
  c.v.style.color = flag === "bad" ? "#B3261E" : (flag === "good" ? "var(--good)" : "var(--text)");
  c.note.textContent = note || "";
}

function renderInspector() {
  const ev = latestActual[selectedRole];
  const hexEl = document.getElementById("scFrameHex");
  if (!ev) {
    setCell("ACTUAL", "—", "", null);
    setCell("REDUNDANT SUM S", "waiting for first 'actual' frame…", ROLE_LABEL[selectedRole], null);
    hexEl.textContent = "—";
    return;
  }
  hexEl.textContent = `0x${ev.can_id.toString(16).toUpperCase()} · DLC ${ev.dlc} · ${ev.raw_hex}`;
  const age = performance.now() / 1000 - ev._recvT;
  const agreeInverse = ev.inverse_moving != null && ev.moving != null ? (ev.inverse_moving === ev.moving) : null;
  setCell("ACTUAL", ev.angle_deg != null ? ev.angle_deg.toFixed(1) + "°" : "—", "0.1·p − 1238.8", null);
  setCell("REDUNDANT SUM S", ev.redundant_sum_hex ?? "—",
          ev.redundant_sum_in_band ? "in band" : "OUT OF BAND",
          ev.redundant_sum_in_band === false ? "bad" : "good");
  setCell("BYTE-7 CHECK", ev.candidate_integrity_ok == null ? "—" : (ev.candidate_integrity_ok ? "PASS" : "FAIL"),
          "CRC family", ev.candidate_integrity_ok === false ? "bad" : "good");
  setCell("MARKER", fmtHex(ev.marker, 2),
          ev.marker === 0x5A ? "b6 as-is" : (ev.marker === 0xA5 ? "b6 inverted" : "unknown"), null);
  setCell("MOVING BIT", ev.moving == null ? "—" : (ev.moving ? "SET" : "CLEAR"), "b6 bit 5", null);
  setCell("INVERSE MOVING", ev.inverse_moving == null ? "—" : (ev.inverse_moving ? "SET" : "CLEAR"),
          agreeInverse == null ? "" : (agreeInverse ? "agrees" : "DISAGREES"),
          agreeInverse === false ? "bad" : null);
  setCell("CYCLE COUNTER", fmtHex(ev.counter, 2), "tracks 0x194", null);
  setCell("FRAME AGE", age.toFixed(2) + " s", "", age > 1.0 ? "bad" : "good");
}
setInterval(renderInspector, 200);

// ---------------------------------------------------------------- slope --

function renderSlope() {
  const s = latestSnapshot.servos[selectedRole];
  const el = document.getElementById("scSlope");
  if (!s || s.state_hex !== "0x9E00") { el.textContent = ""; return; }
  const arr = chart.history[selectedRole];
  const now = chart.now();          // history carries ENGINE time -- measure against the same clock
  const recent = [];
  for (let i = arr.length - 1; i >= 0 && arr[i].t > now - 3.0; i--) recent.push(arr[i]);
  if (recent.length < 2) { el.textContent = ""; return; }
  const dt = recent[0].t - recent[recent.length - 1].t;
  if (dt < 0.5) { el.textContent = ""; return; }
  const slope = (recent[0].v - recent[recent.length - 1].v) / dt;
  el.textContent = `slope ${slope.toFixed(2)} °/s · ref ${MOVE_SPEED_DEG_S}`;
}
setInterval(renderSlope, 400);

// ---------------------------------------------------------------- wiring --

function renderAll() {
  // selectedRole is null until the first snapshot carries the roster; every
  // renderer below indexes by it, so bail rather than render "undefined".
  if (!selectedRole) return;
  updateRoleTabAvailability(latestSnapshot);
  renderGauge(latestSnapshot);
  renderReadouts(latestSnapshot);
  renderStateChip(latestSnapshot);
  syncSetpointUI();
  // NOTE: renderInspector() deliberately NOT called here -- it runs on its
  // own 200 ms timer; calling it twice made it rebuild at snapshot rate.
  document.getElementById("btnCommandMove").disabled =
    !latestSnapshot.servos[selectedRole] || latestSnapshot.servos[selectedRole].presence !== "live";
  document.getElementById("btnReturnAsFound").disabled = asFoundAngle[selectedRole] == null;
}

onSnapshot((snapshot) => {
  const t = typeof snapshot.t === "number" ? snapshot.t : performance.now() / 1000;
  for (const role of ROLES) {
    const s = snapshot.servos[role];
    if (!s) continue;
    if (s.target_deg != null) chart.pushTarget(role, t, s.target_deg);
    if (asFoundAngle[role] == null && s.actual_deg != null && s.presence !== "absent") {
      asFoundAngle[role] = s.actual_deg;
    }
  }
  renderAll();
});

onEvents((events) => {
  for (const e of events) {
    if (e.kind !== "actual" || e.angle_deg == null) continue;
    chart.pushActual(e.role, e.t, e.angle_deg,
      { moving: e.moving, statusBits: e.status_bits ?? null, counter: e.counter ?? null });
    latestActual[e.role] = Object.assign({}, e, { _recvT: performance.now() / 1000 });
  }
});

renderAll();
