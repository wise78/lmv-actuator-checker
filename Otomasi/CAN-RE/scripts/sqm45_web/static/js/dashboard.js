// Dashboard page: multi-role gauge grid + a dual-role live chart. Socket,
// gauge geometry, chart engine, safety bar and log all come from common.js.
//
// Render model: each card's skeleton is built ONCE per role and PATCHED
// afterwards (setAttribute / textContent / classList writes). The previous
// version rebuilt every card's innerHTML at 10 Hz -- about a thousand DOM
// nodes a second -- which broke slider drags (the element was swapped out
// from under a captured pointer), dropped MOVE clicks that straddled a
// rebuild, and poisoned the number input with NaN when focus died mid-edit.
// Listeners are DELEGATED on the container once, so nothing is re-bound.

// A local setpoint edit still IN FLIGHT: { v, at }. It exists only to bridge
// the ~100 ms between emitting set_pending_target and the echoed bus value
// catching up -- without expiry, one lost emit used to pin the slider/num/
// MOVE to the last dialled angle FOREVER, so a health-check move or another
// tab's command was invisible here and MOVE transmitted the stale angle.
const PENDING_TTL_MS = 2000;
// Keyed by whatever roles the server reports, not a fixed pair -- a bus with
// four or six actuators fills these on discovery.
const pendingInput = {};
const dragging = {};
const gaugeEls = {};          // role -> cached element refs
let emptyStateEl = null;

const GX = 100, GY = 104, GR = 76;
const gA0 = pt(GX, GY, GR, bear(0));

function gaugePaths(svg) {
  // structural order produced by gaugeSvg(): [track, valueArc, targetMark, needle]
  const paths = svg.querySelectorAll("path");
  const texts = svg.querySelectorAll("text");
  return { value: paths[1], target: paths[2], needle: paths[3], readout: texts[texts.length - 2] };
}

function monitorReason(meta) {
  if (!meta) return "";
  if (meta.target_confidence === "hypothesis") {
    return `Target ${meta.target_id_hex} and its integrity constant are extrapolated, never observed `
         + `driving hardware. Readable, not commandable.`;
  }
  if (meta.target_confidence === "unknown") {
    return `Target identifier for address ${meta.address} is outside the verified block. `
         + `Readable, not commandable.`;
  }
  return `Target ${meta.target_id_hex} is confirmed but not in the transmitted sequence.`;
}

function experimentReason(meta) {
  // Commanding address 4 TESTS its integrity constant rather than using a known
  // one. Whoever presses MOVE here is running the experiment, and the card must
  // say so -- rendering it identically to oil would hide that.
  return `Target ${meta.target_id_hex} is the last channel slot and is solid, but its `
       + `integrity constant is extrapolated and never observed. The first MOVE proves `
       + `or disproves it: if wrong, the actuator rejects the frame and raises transient `
       + `fault bits that clear within ~2 s. Isolated bench only.`;
}

function buildCard(role) {
  const meta = ROLE_META[role];
  const root = document.createElement("div");
  root.className = "gauge-card";
  root.innerHTML = `
    <div class="gauge-svg-wrap">${gaugeSvg(role, { actual_deg: null, target_deg: null })}</div>
    <div class="gauge-info">
      <div class="gauge-title">
        <span class="dot" style="background:${COLORS[role] || '#8B93A1'};"></span>
        <span class="role-name">${ROLE_LABEL[role] || role.toUpperCase()}</span>
        <span class="mono chip static" style="font-size:10px; padding:2px 7px;">0x${ROLE_CAN_ID_HEX[role] || '???'}</span>
        ${meta && !meta.commandable ? '<span class="chip static monitor-only" title="' + monitorReason(meta) + '">MONITOR ONLY</span>' : ''}
        ${meta && meta.commandable && meta.experimental ? '<span class="chip static experimental" title="' + experimentReason(meta) + '">UNVERIFIED</span>' : ''}
        <div style="flex-grow:1;"></div>
        <span class="presence-pill absent">ABSENT</span>
      </div>
      <div class="readouts">
        <div class="readout"><span class="k">TARGET</span><span class="v mono js-target">—</span></div>
        <div class="readout"><span class="k">ERROR</span><span class="v mono js-err">—</span></div>
      </div>
      <div><span class="mono js-chip" style="font-size:11px; padding:3px 8px; border-radius:999px;"></span></div>
      <div class="setpoint-row">
        <input type="range" min="0" max="90" step="0.1" value="45" data-role="${role}" class="setpointRange" ${meta && !meta.commandable ? "disabled" : ""}>
        <input type="number" min="0" max="90" step="0.1" value="45.0" data-role="${role}" class="setpointNum mono" ${meta && !meta.commandable ? "disabled" : ""}>
        <button class="btn-move" data-role="${role}" disabled title="${meta && !meta.commandable ? monitorReason(meta) : "actual frame not fresh"}">MOVE</button>
      </div>
    </div>`;
  const q = (sel) => root.querySelector(sel);
  gaugeEls[role] = {
    root,
    pill: q(".presence-pill"),
    target: q(".js-target"),
    err: q(".js-err"),
    chip: q(".js-chip"),
    slider: q(".setpointRange"),
    num: q(".setpointNum"),
    move: q(".btn-move"),
    svg: gaugePaths(q(".gauge-svg-wrap svg")),
  };
  return root;
}

function ensureCards(snapshot) {
  const grid = document.getElementById("gauges");
  let any = false;
  for (const role of ROLES) {                       // ROLES order == Air left, never reorder
    const s = snapshot.servos[role];
    const present = !!(s && s.presence !== "absent");
    if (present && !gaugeEls[role]) {
      // Insert in ROLES order -- Air must stay left even when Fuel appeared
      // first and Air only woke up later.
      const idx = ROLES.indexOf(role);
      const later = Object.keys(gaugeEls).find((r) => ROLES.indexOf(r) > idx);
      grid.insertBefore(buildCard(role), later ? gaugeEls[later].root : null);
    }
    if (!present && gaugeEls[role]) {               // server removes after 10 s of silence
      gaugeEls[role].root.remove();
      delete gaugeEls[role];
    }
    any = any || present;
  }
  if (!any) {
    if (!emptyStateEl) {
      emptyStateEl = document.createElement("div");
      emptyStateEl.className = "panel";
      emptyStateEl.style.cssText = "grid-column: 1 / -1; color: var(--muted); font-size:13px;";
      emptyStateEl.textContent =
        "Not transmitting, or no actuator has answered yet. The SQM45 needs live CAN traffic to run.";
      grid.appendChild(emptyStateEl);
    }
  } else if (emptyStateEl) {
    emptyStateEl.remove();
    emptyStateEl = null;
  }
}

function setValueIfSettled(el, v) {
  // Never fight the operator: while an input is focused or mid-drag its
  // value is theirs, not the bus's.
  if (document.activeElement === el || (el.classList.contains("setpointRange") && dragging[el.dataset.role])) return;
  if (el.value !== v) el.value = v;
}

function patchCard(role, s) {
  const g = gaugeEls[role];
  if (!g) return;

  const haveActual = s.actual_deg != null;
  const actual = haveActual ? clamp(s.actual_deg, 0, 90) : 0;
  const target = s.target_deg != null ? clamp(s.target_deg, 0, 90) : 0;
  const av = pt(GX, GY, GR, bear(actual));
  const large = (240 * actual / 90) > 180 ? 1 : 0;
  const op = haveActual ? "1" : "0.25";
  g.svg.value.setAttribute("d", `M ${fp(gA0)} A ${GR} ${GR} 0 ${large} 1 ${fp(av)}`);
  g.svg.value.setAttribute("opacity", op);

  const bn = bear(actual), tip = pt(GX, GY, GR - 16, bn);
  const nl = pt(GX, GY, 5.5, bn + 90), nr = pt(GX, GY, 5.5, bn - 90);
  g.svg.needle.setAttribute("d", `M ${fp(tip)} L ${fp(nl)} L ${fp(nr)} Z`);
  g.svg.needle.setAttribute("opacity", op);

  const bt = bear(target);
  const t1 = pt(GX, GY, GR + 2, bt), t2 = pt(GX, GY, GR + 13, bt - 3.2), t3 = pt(GX, GY, GR + 13, bt + 3.2);
  g.svg.target.setAttribute("d", `M ${fp(t1)} L ${fp(t2)} L ${fp(t3)} Z`);

  g.svg.readout.textContent = haveActual ? s.actual_deg.toFixed(1) : "––.–";

  g.target.textContent = s.target_deg != null ? s.target_deg.toFixed(1) + "°" : "—";

  const err = (s.actual_deg != null && s.target_deg != null) ? (s.actual_deg - s.target_deg) : null;
  g.err.textContent = err != null ? (err >= 0 ? "+" : "") + err.toFixed(1) + "°" : "—";
  g.err.style.color = err != null && Math.abs(err) <= 0.15 ? "var(--good)" : "inherit";

  const presence = s.presence;
  if (g.pill.textContent !== presence.toUpperCase()) {
    g.pill.textContent = presence.toUpperCase();
    g.pill.className = `presence-pill ${presence}`;
  }

  const stateIsMove = s.state_hex === "0x9E00";
  const chipTxt = `${s.state_hex} ${stateIsMove ? "MOVE" : "HOLD"}`;
  if (g.chip.textContent !== chipTxt) {
    g.chip.textContent = chipTxt;
    g.chip.style.background = stateIsMove ? "var(--air-soft)" : "var(--good-soft)";
    g.chip.style.color = stateIsMove ? "var(--air)" : "var(--good)";
  }

  // The operator's just-dialled value overrides the echoed bus value ONLY
  // while that edit is in flight: fresh (within TTL) and not yet confirmed
  // by an equal echo. Confirmed or expired edits are discarded so the
  // controls track the bus again -- including moves commanded elsewhere
  // (health check, a second tab).
  const p = pendingInput[role];
  const echoed = s.pending_target_deg != null ? s.pending_target_deg : s.target_deg;
  const confirmed = echoed != null && p != null && Math.abs(echoed - p.v) <= 0.05;
  let pendingVal;
  if (p && !confirmed && performance.now() - p.at < PENDING_TTL_MS) {
    pendingVal = p.v;
  } else {
    if (p) pendingInput[role] = null;   // confirmed or expired -- discard
    pendingVal = echoed != null ? echoed : 45;
  }
  setValueIfSettled(g.slider, String(pendingVal));
  setValueIfSettled(g.num, Number(pendingVal).toFixed(1));
  const meta = ROLE_META[role];
  const canCommand = !meta || meta.commandable;
  g.move.disabled = presence !== "live" || !canCommand;
  g.move.title = !canCommand ? monitorReason(meta)
               : presence !== "live" ? "actual frame not fresh" : "";
}

function renderLegend() {
  // Built from the roster, not written into the template: with up to six
  // actuators a hard-coded legend is guaranteed to disagree with the chart.
  const box = document.getElementById("chartLegend");
  if (!box) return;
  const present = ROLES.filter((r) => latestSnapshot.servos && latestSnapshot.servos[r]
                                   && latestSnapshot.servos[r].presence !== "absent");
  const show = present.length ? present : ROLES;
  box.replaceChildren();
  for (const r of show) {
    const c = COLORS[r] || "#8B93A1";
    const item = document.createElement("span");
    item.className = "legend-item";
    item.innerHTML =
      `<svg class="legend-swatch" viewBox="0 0 16 2"><line x1="0" y1="1" x2="16" y2="1" stroke="${c}" stroke-width="2"/></svg>`
      + `${ROLE_LABEL[r] || r} actual`
      + `<svg class="legend-swatch" viewBox="0 0 16 2" style="margin-left:8px;"><line x1="0" y1="1" x2="16" y2="1" stroke="${c}" stroke-width="2" stroke-dasharray="4 3"/></svg>`
      + `target`;
    box.appendChild(item);
  }
}

function renderGauges(snapshot) {
  ensureCards(snapshot);
  for (const role of ROLES) {
    const s = snapshot.servos[role];
    if (s && s.presence !== "absent") patchCard(role, s);
  }
}

// ---------------------------------------------------------------- wiring --
// Delegated ONCE on the grid container -- survives card rebuilds because
// there are none, and survives add/remove of individual cards by design.

const gaugesGrid = document.getElementById("gauges");

gaugesGrid.addEventListener("input", (e) => {
  const t = e.target;
  if (!t.matches(".setpointRange, .setpointNum")) return;
  const role = t.dataset.role;
  let v = parseFloat(t.value);
  if (!Number.isFinite(v)) v = pendingInput[role] ? pendingInput[role].v : null;   // half-typed garbage never reaches the bus
  if (v == null) return;
  v = clamp(v, 0, 90);
  pendingInput[role] = { v, at: performance.now() };
  const other = t.classList.contains("setpointRange") ? ".setpointNum" : ".setpointRange";
  const o = gaugesGrid.querySelector(`[data-role="${role}"]${other}`);
  if (o && document.activeElement !== o) o.value = other === ".setpointNum" ? v.toFixed(1) : String(v);
  socket.emit("set_pending_target", { role, angle: v });
});

gaugesGrid.addEventListener("pointerdown", (e) => {
  if (e.target.matches(".setpointRange")) dragging[e.target.dataset.role] = true;
});
window.addEventListener("pointerup", () => { for (const r of Object.keys(dragging)) dragging[r] = false; });

gaugesGrid.addEventListener("click", (e) => {
  if (!e.target.matches(".btn-move")) return;
  const r = e.target.dataset.role;
  // Send the angle WITH the request: set_pending_target is applied
  // asynchronously on the TX thread, so relying on it having landed is what
  // made this button need several clicks. Read what the operator SEES.
  const slider = gaugesGrid.querySelector(`[data-role="${r}"].setpointRange`);
  const a = slider ? parseFloat(slider.value) : (pendingInput[r] ? pendingInput[r].v : null);
  socket.emit("request_move", { role: r, angle: a });
});

const chart = new LiveChart(document.getElementById("chart"), document.querySelector(".chart-wrap"),
  { windowS: 60, roles: ROLES });
// The roster arrives with the first snapshot, and an actuator can be addressed
// mid-session; grow the chart and rebuild the legend when that happens.
onRoster((roles) => { chart.ensureRoles(roles); renderLegend(); });

// Lanes are drawn per VISIBLE role. The roster always carries all six ACT
// slots, so tracking it directly drew six MOVING lanes on a four-servo bus --
// two of them permanently blank, and the whole block taller than it needed to
// be. Draw only what is actually answering, and only re-apply when the set
// changes (this runs at 10 Hz; resizing every tick would thrash the layout).
let lastVisibleKey = "";
function syncVisibleRoles(snapshot) {
  const present = ROLES.filter((r) => snapshot.servos[r]
                                   && snapshot.servos[r].presence !== "absent");
  const key = present.join(",");
  if (key === lastVisibleKey) return;
  lastVisibleKey = key;
  chart.setVisibleRoles(present.length ? present : ROLES.slice(0, 1));
}

onSnapshot((snapshot) => {
  const t = typeof snapshot.t === "number" ? snapshot.t : performance.now() / 1000;
  for (const role of ROLES) {
    const s = snapshot.servos[role];
    if (s && s.target_deg != null) chart.pushTarget(role, t, s.target_deg);
  }
  syncVisibleRoles(snapshot);
  renderGauges(snapshot);
  renderLegend();
});

onEvents((events) => {
  for (const e of events) {
    if (e.kind === "actual" && e.angle_deg != null) {
      chart.pushActual(e.role, e.t, e.angle_deg,
        { moving: e.moving, statusBits: e.status_bits ?? null, counter: e.counter ?? null });
    }
  }
});
