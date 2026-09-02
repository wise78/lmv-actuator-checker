// Raw Sniffer page: everything on the wire, with no ID map assumed.
//
// The rest of this dashboard renders two known servos. This page renders
// whatever is actually there -- 2 servos, 4, an SQM48 nobody has decoded
// yet, or a bus that turns out to be wired to something else entirely.
//
// Rendering discipline: the census payload arrives at 2 Hz and can carry a
// few dozen identifiers. Rows are keyed by identifier and PATCHED in place
// rather than rebuilt, so a click target does not vanish under the cursor
// and the byte inspector does not flicker. Only the live frame stream is
// append-only, and it is capped.

const REFRESH_MS = 500;

let census = null;
let selectedId = null;
let sortMode = "id";
let filterText = "";
let onlyUnknown = false;
let streamPaused = false;

// ------------------------------------------------------------ subscribe --
// The server only builds the (expensive) census report while a page is
// subscribed. Tell it we are here, and tell it when we leave.

socket.emit("census_subscribe", { on: true });
socket.on("connect", () => socket.emit("census_subscribe", { on: true }));
window.addEventListener("beforeunload", () => socket.emit("census_subscribe", { on: false }));

// recent[] is incremental; every other field is a full replacement. The
// server's cursor is shared by all subscribers, so a second tab opening
// re-broadcasts the whole ring buffer to everyone -- dedupe on the frame
// sequence rather than trusting the payload to contain only new frames.
let lastSeq = 0;

socket.on("census", (payload) => {
  const carried = census ? census.recent || [] : [];
  const incoming = (payload.recent || []).filter((f) => f.seq > lastSeq);
  if (incoming.length) lastSeq = incoming[incoming.length - 1].seq;
  census = payload;
  census.recent = carried.concat(incoming).slice(-600);
  // A census reset rewinds the sequence; follow it down rather than
  // filtering out every frame until the counter climbs back.
  if (payload.seq != null && payload.seq < lastSeq) lastSeq = payload.seq;
  render();
});

// -------------------------------------------------------------- helpers --

function fmtHz(v) { return v == null ? "—" : (v >= 10 ? v.toFixed(0) : v.toFixed(2)) + " Hz"; }
function fmtS(v) { return v == null ? "—" : v < 1 ? (v * 1000).toFixed(0) + " ms" : v.toFixed(2) + " s"; }
function fmtInt(v) { return (v || 0).toLocaleString("en-US"); }

function elapsedLabel(s) {
  s = Math.floor(s || 0);
  const m = Math.floor(s / 60);
  return m ? `${m}m ${String(s % 60).padStart(2, "0")}s` : `${s} s`;
}

// Split a payload into byte cells, highlighting the ones that changed since
// the previous distinct payload. On a cyclic bus that is where the counters
// and the moving quantities are -- visible without decoding anything.
function payloadCells(hex, prevHex) {
  const out = [];
  for (let i = 0; i < hex.length; i += 2) {
    const b = hex.slice(i, i + 2);
    const p = prevHex ? prevHex.slice(i, i + 2) : b;
    out.push(`<span class="pb${b !== p ? " changed" : ""}">${b}</span>`);
  }
  return out.join("");
}

function tierClass(tier) {
  return { confirmed: "t-confirmed", probable: "t-probable", possible: "t-possible" }[tier] || "t-unlikely";
}
function tierLabel(tier) {
  return { confirmed: "ACTUATOR", probable: "PROBABLE", possible: "POSSIBLE", unlikely: "—" }[tier] || "—";
}

// --------------------------------------------------------------- stats --

function renderStats() {
  document.getElementById("stFrames").textContent = fmtInt(census.total_frames);
  document.getElementById("stIds").textContent = census.unique_ids;
  const fps = census.inst_frames_per_s != null ? census.inst_frames_per_s : census.frames_per_s;
  document.getElementById("stFps").textContent = fps ? fps.toFixed(0) : "—";
  const load = document.getElementById("stLoad");
  load.textContent = (census.bus_load_pct || 0).toFixed(1) + " %";
  load.classList.toggle("warn", (census.bus_load_pct || 0) > 60);
  document.getElementById("stElapsed").textContent = elapsedLabel(census.elapsed_s);
  const unp = document.getElementById("stUnparsed");
  unp.textContent = fmtInt(census.unparsed_lines);
  // Unparsed lines are not cosmetic: a converter emitting junk is how a
  // whole reception path died silently once before (FINDINGS 2026-08-19).
  unp.classList.toggle("warn", (census.unparsed_lines || 0) > 0);

  const rl = census.rawlog || {};
  const btn = document.getElementById("btnRecord");
  btn.classList.toggle("recording", !!rl.active);
  btn.textContent = rl.active ? "■ STOP RECORDING" : "● RECORD RAW CSV";
  const bar = document.getElementById("recordStatus");
  if (rl.active) {
    bar.className = "sniff-recordbar show";
    bar.textContent = `Recording ${rl.name} — ${fmtInt(rl.frames)} frames, ${elapsedLabel(rl.elapsed_s)}`;
  } else if (rl.error) {
    bar.className = "sniff-recordbar show error";
    bar.textContent = `Raw log stopped on error: ${rl.error}`;
  } else if (rl.name) {
    bar.className = "sniff-recordbar show done";
    bar.textContent = `Saved ${rl.name} — ${fmtInt(rl.frames)} frames`;
  } else {
    bar.className = "sniff-recordbar";
    bar.textContent = "";
  }
}

// ---------------------------------------------------------- candidates --

function renderCandidates() {
  const wrap = document.getElementById("candidates");
  const empty = document.getElementById("candEmpty");
  const list = (census.candidates || []).filter((c) => c.tier !== "unlikely");
  empty.style.display = list.length ? "none" : "";
  if (!list.length) { wrap.replaceChildren(); return; }

  const frag = document.createDocumentFragment();
  for (const c of list) {
    const card = document.createElement("div");
    card.className = "cand-card " + tierClass(c.tier);

    const passed = c.evidence.filter((e) => e.ok).length;
    const angle = candidateAngle(c);
    card.innerHTML = `
      <div class="cand-head">
        <span class="cand-id mono">${c.id_hex}</span>
        <span class="cand-tier ${tierClass(c.tier)}">${tierLabel(c.tier)}</span>
        <span class="cand-score mono">${c.score}</span>
      </div>
      <div class="cand-sub">
        ${c.label ? `<b>${c.label}</b> · ` : ""}node ${c.node != null ? c.node : "—"} ·
        ${fmtHz(c.rate_hz)} · ${fmtInt(c.count)} frames
      </div>
      <div class="cand-angle mono">${angle == null ? "—" : angle.toFixed(1) + "°"}</div>
      <div class="cand-meter"><div class="fill" style="width:${c.score}%"></div></div>
      <div class="cand-evsum">${passed} of ${c.evidence.length} checks passed</div>
      <ul class="cand-ev">
        ${c.evidence.map((e) => `<li class="${e.ok ? "ok" : "no"}">${e.text}</li>`).join("")}
      </ul>`;
    card.addEventListener("click", () => { selectedId = c.can_id; render(); });
    frag.appendChild(card);
  }
  wrap.replaceChildren(frag);
}

// Decode the candidate's newest payload with the actuator position rule.
// Shown so a technician can sanity-check the score against a shaft they can
// physically see -- a number that tracks the damper is worth more than any
// amount of scoring.
function candidateAngle(row) {
  if (!row.last_hex || row.last_hex.length < 4) return null;
  const raw = parseInt(row.last_hex.slice(0, 4), 16);
  if (Number.isNaN(raw)) return null;
  const masked = raw & 0x37FF;      // drop status bits 15/14/11
  const deg = 0.1 * masked - 1238.8;
  return deg > -30 && deg < 130 ? deg : null;
}

// ------------------------------------------------------------ node map --

function renderNodes() {
  const wrap = document.getElementById("nodeMap");
  const nodes = census.nodes || [];
  if (!nodes.length) { wrap.replaceChildren(); return; }
  const frag = document.createDocumentFragment();
  for (const n of nodes) {
    const cell = document.createElement("div");
    const kind = n.answered ? "present" : n.probed ? "silent" : "tx";
    cell.className = `node-cell ${kind}${n.in_actuator_range ? " in-range" : ""}`;
    cell.innerHTML = `
      <div class="node-n mono">${n.node}</div>
      <div class="node-v">${n.verdict}</div>
      <div class="node-ids mono">${n.pdo_ids.length ? n.pdo_ids.join(" ") : "&nbsp;"}</div>`;
    cell.title =
      `node ${n.node}\n` +
      `probes on 0x${(0x600 + n.node).toString(16).toUpperCase()}: ${n.probed}\n` +
      `replies on 0x${(0x580 + n.node).toString(16).toUpperCase()}: ${n.answered}\n` +
      (n.in_actuator_range ? "inside the LMV actuator scan range (9-15)" : "outside the actuator scan range");
    frag.appendChild(cell);
  }
  wrap.replaceChildren(frag);
}

// -------------------------------------------------------------- table --

function visibleRows() {
  let rows = (census.rows || []).slice();
  if (onlyUnknown) rows = rows.filter((r) => !r.label);
  if (filterText) {
    const q = filterText.toLowerCase();
    rows = rows.filter((r) =>
      r.id_hex.toLowerCase().includes(q) ||
      (r.label || "").toLowerCase().includes(q) ||
      r.family.includes(q) ||
      (r.tier === "confirmed" || r.tier === "probable" ? "actuator".includes(q) : false));
  }
  const by = {
    id: (a, b) => a.can_id - b.can_id,
    score: (a, b) => b.score - a.score || a.can_id - b.can_id,
    count: (a, b) => b.count - a.count,
    rate: (a, b) => (b.rate_hz || 0) - (a.rate_hz || 0),
    gap: (a, b) => b.max_gap_s - a.max_gap_s,
  }[sortMode];
  rows.sort(by);
  return rows;
}

function renderTable() {
  const tbody = document.getElementById("idRows");
  const frag = document.createDocumentFragment();
  for (const r of visibleRows()) {
    const tr = document.createElement("tr");
    tr.className = (r.can_id === selectedId ? "sel " : "") + (r.age_s > 3 ? "stale" : "");
    tr.innerHTML = `
      <td class="mono strong">${r.id_hex}</td>
      <td>${r.label || `<span class="dim">unknown</span>`}</td>
      <td><span class="fam fam-${r.family}">${r.family}</span></td>
      <td class="num mono">${r.node != null ? r.node : "—"}</td>
      <td class="num mono">${fmtInt(r.count)}</td>
      <td class="num mono">${fmtHz(r.rate_hz)}</td>
      <td class="num mono ${r.recent_max_gap_s > 0.6 ? "warn" : ""}"
          title="session max ${fmtS(r.max_gap_s)}">${fmtS(r.recent_max_gap_s)}</td>
      <td class="mono">${r.dlcs.join("/")}</td>
      <td class="mono payload">${payloadCells(r.last_hex, r.prev_hex)}</td>
      <td class="num"><span class="tier-pill ${tierClass(r.tier)}">${r.tier === "unlikely" ? "—" : r.score}</span></td>`;
    tr.addEventListener("click", () => { selectedId = r.can_id; render(); });
    frag.appendChild(tr);
  }
  tbody.replaceChildren(frag);
}

// ---------------------------------------------------------- inspector --

function renderInspector() {
  const box = document.getElementById("inspector");
  const title = document.getElementById("inspTitle");
  const row = (census.rows || []).find((r) => r.can_id === selectedId);
  if (!row) {
    title.textContent = "select an identifier";
    box.className = "sniff-empty";
    box.textContent = "Click a row above to see which bits actually move.";
    return;
  }
  title.textContent = row.id_hex + (row.label ? ` — ${row.label}` : "");
  box.className = "insp";

  const nbytes = Math.max(...row.dlcs);
  let bits = "";
  for (let i = 0; i < nbytes; i++) {
    const toggled = row.toggled_bits[i] || 0;
    const value = row.last_hex.length >= (i + 1) * 2
      ? parseInt(row.last_hex.slice(i * 2, i * 2 + 2), 16) : 0;
    let cells = "";
    for (let b = 7; b >= 0; b--) {
      const mask = 1 << b;
      const isSet = (value & mask) !== 0;
      const moves = (toggled & mask) !== 0;
      cells += `<i class="bit${isSet ? " on" : ""}${moves ? " moves" : ""}"></i>`;
    }
    // A byte that advanced by exactly +1 on most frames is a sequence
    // counter -- the single most useful thing to spot in an unknown frame,
    // because it anchors the rest of the payload to a timeline.
    const counterish = row.count > 20 && row.byte_incr[i] > row.count * 0.5;
    bits += `
      <div class="insp-row">
        <span class="insp-i mono">b${i}</span>
        <span class="insp-hex mono">${row.last_hex.slice(i * 2, i * 2 + 2) || "--"}</span>
        <span class="insp-bits">${cells}</span>
        <span class="insp-note">${
          toggled === 0 ? "constant"
          : counterish ? `<b>counter</b> (+1 on ${Math.round(100 * row.byte_incr[i] / row.count)}% of frames)`
          : `${row.byte_distinct[i] >= 24 ? "24+" : row.byte_distinct[i]} values, mask 0x${toggled.toString(16).toUpperCase().padStart(2, "0")}`
        }</span>
      </div>`;
  }

  const ev = row.evidence || [];
  box.innerHTML = `
    <div class="insp-meta">
      <span>${fmtInt(row.count)} frames</span><span>${(row.share_pct || 0).toFixed(1)}% of bus</span>
      <span>${fmtHz(row.rate_hz)}</span>
      <span>gap now ${fmtS(row.min_gap_s)}–${fmtS(row.recent_max_gap_s)}</span>
      <span class="${row.max_gap_s > 0.6 ? "warn" : ""}">worst this session ${fmtS(row.max_gap_s)}</span>
      <span>first T+${(row.first_seen || 0).toFixed(1)}s</span>
      <span>payload static ${fmtS(row.static_since_s)}</span>
      ${row.status_frames ? `<span class="warn">${fmtInt(row.status_frames)} frames with position status bits set</span>` : ""}
    </div>
    ${row.note ? `<div class="insp-note-line">${row.note}</div>` : ""}
    <div class="insp-bits-wrap">${bits}</div>
    <div class="insp-legend">
      <span><i class="bit on"></i> bit set now</span>
      <span><i class="bit moves"></i> bit toggles over time</span>
      <span><i class="bit"></i> stuck clear</span>
    </div>
    ${ev.length ? `<div class="insp-ev"><div class="insp-ev-h">Actuator scoring — ${row.score}/100 (${row.tier})</div>
      <ul class="cand-ev">${ev.map((e) => `<li class="${e.ok ? "ok" : "no"}">${e.text}</li>`).join("")}</ul></div>` : ""}`;
}

// ------------------------------------------------------------- stream --

function renderStream() {
  if (streamPaused) return;
  const box = document.getElementById("stream");
  const frames = (census.recent || []).slice(-160);
  const frag = document.createDocumentFragment();
  for (const f of frames) {
    const row = document.createElement("div");
    row.className = "stream-row" + (f.can_id === selectedId ? " sel" : "");
    const t = document.createElement("span");
    t.className = "st-t mono";
    t.textContent = "T+" + (f.t || 0).toFixed(3);
    const id = document.createElement("span");
    id.className = "st-id mono";
    id.textContent = "0x" + f.can_id.toString(16).toUpperCase().padStart(3, "0");
    const d = document.createElement("span");
    d.className = "st-d mono";
    // textContent, not innerHTML: this is raw device output and a malformed
    // payload must never become markup.
    d.textContent = (f.hex.match(/../g) || []).join(" ");
    row.append(t, id, d);
    frag.appendChild(row);
  }
  box.replaceChildren(frag);
  box.scrollTop = box.scrollHeight;
}

// ------------------------------------------------------------- render --

let renderQueued = false;
function render() {
  if (!census || renderQueued) return;
  renderQueued = true;
  requestAnimationFrame(() => {
    renderQueued = false;
    renderStats();
    renderCandidates();
    renderNodes();
    renderTable();
    renderInspector();
    renderStream();
  });
}

// ------------------------------------------------------------ controls --

document.getElementById("idFilter").addEventListener("input", (e) => {
  filterText = e.target.value.trim();
  render();
});
document.getElementById("idSort").addEventListener("change", (e) => {
  sortMode = e.target.value;
  render();
});
document.getElementById("onlyUnknown").addEventListener("change", (e) => {
  onlyUnknown = e.target.checked;
  render();
});
document.getElementById("streamPause").addEventListener("change", (e) => {
  streamPaused = e.target.checked;
});

document.getElementById("btnRecord").addEventListener("click", () => {
  const active = census && census.rawlog && census.rawlog.active;
  if (active) {
    socket.emit("stop_raw_log");
  } else {
    const note = prompt("Label for this recording (site, burner, condition):", "");
    if (note === null) return;   // cancelled -- do not start an unlabelled log
    socket.emit("start_raw_log", { note });
  }
});

document.getElementById("btnExport").addEventListener("click", () => socket.emit("export_census"));

document.getElementById("btnResetCensus").addEventListener("click", () => {
  if (confirm("Reset all bus statistics?\n\nAny raw CSV recording keeps running — only the counters restart.")) {
    socket.emit("census_reset");
  }
});

// Both saves report the same way as the health-check PDF: say what was written,
// where, and whether the file manager actually opened -- the reveal is
// best-effort, so the path must always be shown in case it did not.
function reportSaved(kind, payload) {
  const t = census ? census.elapsed_s : 0;
  const where = payload.opened === "opened"
    ? "file manager opened at it"
    : `saved (${payload.opened || "not revealed"})`;
  logRow("bus", t, `${kind}: ${payload.path} -- ${where}`);
  const note = document.getElementById("saveNote");
  if (note) {
    note.className = "sniff-savedbar show";
    note.textContent = `${kind} saved: ${payload.name || payload.path}`
      + (payload.opened === "opened" ? " - your file manager is open at it." : ` - at ${payload.path}`);
  }
}

socket.on("rawlog_status", (payload) => {
  if (census) census.rawlog = payload.status;
  render();
  if (payload.cancelled) {
    logRow("bus", census ? census.elapsed_s : 0, "recording cancelled - no file created");
    return;
  }
  if (payload.path && !payload.status.active) reportSaved("Raw CSV", payload);
  else if (payload.path) logRow("bus", census ? census.elapsed_s : 0,
                                `recording to ${payload.path}`);
});
socket.on("census_exported", (payload) => {
  if (payload.cancelled) {
    logRow("bus", census ? census.elapsed_s : 0, "census export cancelled - nothing written");
    return;
  }
  reportSaved("Census report", payload);
});
