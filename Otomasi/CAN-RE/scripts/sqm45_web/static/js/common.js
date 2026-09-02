// Shared across every SQM45 dashboard page: the socket connection, gauge
// geometry, the SVG live-chart engine, the safety bar (present in every
// view, wired once here so it behaves identically everywhere), and the
// events/notices log. Page-specific files (dashboard.js, servo_checker.js)
// register onSnapshot()/onEvents() listeners rather than owning the socket.

// The actuator roster is SERVER-OWNED. It used to be the literal
// ["air","fuel"] here, which meant a real third or fourth servo on the wire
// rendered nowhere -- a healthy actuator showing as absent is the worst thing
// this instrument can misreport. These are now filled from snapshot.roster,
// which the engine derives from one address model (sqm45/roster.py).
let ROLES = [];                 // every address the build knows, in address order
const COLORS = {};
const ROLE_LABEL = {};
const ROLE_CAN_ID_HEX = {};
const ROLE_META = {};           // role -> {address,label,commandable,target_confidence,...}

function applyRoster(roster) {
  if (!Array.isArray(roster) || !roster.length) return false;
  for (const r of roster) {
    COLORS[r.role] = r.color;
    ROLE_LABEL[r.role] = r.label;
    ROLE_CAN_ID_HEX[r.role] = String(r.actual_id_hex || "").replace(/^0x/i, "");
    ROLE_META[r.role] = r;
  }
  const ordered = roster.slice().sort((a, b) => a.address - b.address).map((r) => r.role);
  if (ordered.join(",") === ROLES.join(",")) return false;
  ROLES = ordered;
  return true;                  // caller may need to grow charts/tabs
}

const socket = io({ transports: ["websocket", "polling"] });
let latestSnapshot = { mode: "idle", servos: {} };

const _snapshotListeners = [];
function onSnapshot(fn) { _snapshotListeners.push(fn); }
const _eventListeners = [];
function onEvents(fn) { _eventListeners.push(fn); }
const _rosterListeners = [];
function onRoster(fn) { _rosterListeners.push(fn); }

socket.on("snapshot", (snapshot) => {
  latestSnapshot = snapshot;
  // Roster first: every listener below may render from it in the same tick.
  if (applyRoster(snapshot.roster)) {
    for (const fn of _rosterListeners) fn(ROLES);
  }
  for (const fn of _snapshotListeners) fn(snapshot);
});
socket.on("events", (events) => { for (const fn of _eventListeners) fn(events); });

// ---------------------------------------------------------------- gauge --

function clamp(v, lo, hi) { return Math.max(lo, Math.min(hi, v)); }
function bear(d) { return 150 + 240 * d / 90; }
function pt(cx, cy, r, b) {
  const a = b * Math.PI / 180;
  return [cx + r * Math.cos(a), cy + r * Math.sin(a)];
}
function fp(p) { return `${p[0].toFixed(1)} ${p[1].toFixed(1)}`; }

function gaugeSvg(role, s, opts) {
  opts = opts || {};
  const CX = 100, CY = 104, R = 76;
  const color = COLORS[role];
  const haveActual = s.actual_deg != null;
  const actual = haveActual ? clamp(s.actual_deg, 0, 90) : 0;
  const target = s.target_deg != null ? clamp(s.target_deg, 0, 90) : 0;
  const a0 = pt(CX, CY, R, bear(0)), a90 = pt(CX, CY, R, bear(90));
  const av = pt(CX, CY, R, bear(actual));
  const large = (240 * actual / 90) > 180 ? 1 : 0;

  let ticks = "";
  for (let d = 0; d <= 90; d += 10) {
    const b = bear(d);
    const i = pt(CX, CY, R - 11, b), o = pt(CX, CY, R + 1, b), l = pt(CX, CY, R - 25, b);
    ticks += `<line x1="${i[0].toFixed(1)}" y1="${i[1].toFixed(1)}" x2="${o[0].toFixed(1)}" y2="${o[1].toFixed(1)}" stroke="#CFD4DC" stroke-width="1.4"/>`;
    ticks += `<text x="${l[0].toFixed(1)}" y="${l[1].toFixed(1)}" fill="#8B93A1" font-size="9.5" text-anchor="middle" dominant-baseline="middle" font-family="Consolas, monospace">${d}</text>`;
  }
  let band = "";
  if (opts.band) {
    const [lo, hi] = opts.band;
    const bl = pt(CX, CY, R, bear(lo)), bh = pt(CX, CY, R, bear(hi));
    const bLarge = (240 * (hi - lo) / 90) > 180 ? 1 : 0;
    band = `<path d="M ${fp(bl)} A ${R} ${R} 0 ${bLarge} 1 ${fp(bh)}" fill="none" stroke="${color}" stroke-width="13" opacity="0.16"/>`;
  }
  const bn = bear(actual), tip = pt(CX, CY, R - 16, bn), nl = pt(CX, CY, 5.5, bn + 90), nr = pt(CX, CY, 5.5, bn - 90);
  const bt = bear(target), t1 = pt(CX, CY, R + 2, bt), t2 = pt(CX, CY, R + 13, bt - 3.2), t3 = pt(CX, CY, R + 13, bt + 3.2);
  const needle = `M ${fp(tip)} L ${fp(nl)} L ${fp(nr)} Z`;
  const targetMark = `M ${fp(t1)} L ${fp(t2)} L ${fp(t3)} Z`;
  const track = `M ${fp(a0)} A ${R} ${R} 0 1 1 ${fp(a90)}`;
  const value = `M ${fp(a0)} A ${R} ${R} 0 ${large} 1 ${fp(av)}`;
  const readout = haveActual ? s.actual_deg.toFixed(1) : "––.–";

  return `<svg width="188" height="188" viewBox="0 0 200 200">
    <path d="${track}" fill="none" stroke="#EDEFF3" stroke-width="13" stroke-linecap="round"/>
    ${band}
    <path d="${value}" fill="none" stroke="${color}" stroke-width="13" stroke-linecap="round" opacity="${haveActual ? 1 : 0.25}"/>
    ${ticks}
    <path d="${targetMark}" fill="${color}" opacity="0.55"/>
    <path d="${needle}" fill="${color}" opacity="${haveActual ? 1 : 0.25}"/>
    <circle cx="100" cy="104" r="7" fill="#fff" stroke="${color}" stroke-width="2.6"/>
    <text x="100" y="166" fill="#171A21" font-size="30" font-weight="700" text-anchor="middle" font-family="Consolas, monospace">${readout}</text>
    <text x="100" y="184" fill="#8B93A1" font-size="10" text-anchor="middle" letter-spacing="0.1em">DEGREES</text>
  </svg>`;
}

// ---------------------------------------------------------------- chart --
//
// SVG, not canvas. A <canvas> is a fixed-resolution bitmap: CSS-scaling it
// to fit a resized container blurs the text and lines, and font-size drawn
// via ctx.fillText scales with the container instead of staying a constant
// readable size. SVG has no backing resolution to blur -- the viewBox is
// kept in sync with the container's real pixel size on resize, so text and
// lines render crisp at any window size.
//
// Clock note: samples carry ENGINE time (snapshot.t / event.t -- the
// server's monotonic bus clock). The right edge is "now" = newest engine t
// seen, advanced locally between packets, so a stalled feed freezes the
// axis instead of sliding away, and gaps land at their true bus positions
// even when the browser tab was busy.
//
// Draw model: requestAnimationFrame capped at ~30 fps (data itself updates
// at <=25 Hz), never the old setInterval(150) -- that timer aliased against
// the 100 ms broadcast cadence like a beat: frames landed in alternating
// ~150/~300 ms steps and every trace visibly stuttered.

const SVGNS = "http://www.w3.org/2000/svg";
function svgEl(tag, attrs) {
  const el = document.createElementNS(SVGNS, tag);
  for (const k in attrs) el.setAttribute(k, attrs[k]);
  return el;
}

const MAX_KEEP_S = 302;     // history kept so 30 -> 300 s window switches lose nothing
const GAP_SHADE_T = 0.32;   // per-role actual gap that shades as a dropout band
const GAP_BREAK_T = 0.60;   // ...and breaks the polyline outright

// Below-plot furniture. The plot bottom used to be a fixed `this.h - 40`,
// which reserved room for exactly two MOVING lanes plus the time axis. At four
// actuators the lanes ran off the bottom of the SVG: they overlapped the
// -60s/-50s labels and the fourth was clipped away entirely. Everything below
// the plot is now measured from the number of lanes actually drawn.
const LANE_H = 9;           // one MOVING/status lane
const LANE_GAP = 2;
const LANE_TOP_PAD = 6;     // plot bottom -> first lane
const AXIS_H = 20;          // time labels under the lanes
const PLOT_MIN_H = 200;     // trace area; roughly what the 2-servo chart had,
                            // so adding actuators grows the card rather than
                            // shrinking the traces to make room for lanes

class LiveChart {
  constructor(svgElement, wrapElement, { windowS, roles }) {
    this.svg = svgElement;
    this.wrap = wrapElement;
    this.windowS = windowS || 60;   // visible span; switchable via setWindowS()
    this.roles = [];                // grown by ensureRoles() as the roster arrives
    this.visibleRoles = [];         // roles actually drawn -- a page can narrow this via setVisibleRoles
    this._visibleExplicit = false;
    this.yMode = "full";            // "full" = 0-90 deg | "band" = +-5 deg around newest target
    this.history = {};              // actuals: {t, v, moving, statusBits, counter}
    this.targetHistory = {};
    this.ensureRoles(roles);

    // engine-clock bridge: newest engine t and the perf-clock moment it arrived
    this._engT = null;
    this._perfAtEngT = null;

    this.w = 1000; this.h = 260;
    this.gGrid = svgEl("g", {});    // rebuilt only on resize/scale change
    this.gSeries = svgEl("g", {});  // rebuilt every frame
    this.svg.replaceChildren(this.gGrid, this.gSeries);

    this._tip = document.createElement("div");
    this._tip.className = "chart-tip";
    this._tip.style.display = "none";
    this.wrap.appendChild(this._tip);
    this.svg.addEventListener("mousemove", (e) => this._onMove(e));
    this.svg.addEventListener("mouseleave", () => { this._hoverPx = null; this._tip.style.display = "none"; });

    const winSel = document.getElementById("chartWindow");
    if (winSel) {
      winSel.value = String(this.windowS);
      winSel.addEventListener("change", () => this.setWindowS(parseFloat(winSel.value)));
    }
    const ySel = document.getElementById("chartYMode");
    if (ySel) {
      ySel.value = this.yMode;
      ySel.addEventListener("change", () => this.setYMode(ySel.value));
    }

    let lastDrawTs = 0;
    const loop = (ts) => {
      if (!document.hidden && ts - lastDrawTs >= 33) { lastDrawTs = ts; this.draw(); }
      requestAnimationFrame(loop);
    };
    requestAnimationFrame(loop);

    const resize = () => {
      const rect = this.wrap.getBoundingClientRect();
      this.w = Math.max(240, Math.round(rect.width));
      this.h = Math.max(140, Math.round(rect.height));
      this.svg.setAttribute("viewBox", `0 0 ${this.w} ${this.h}`);
      this._rebuildGrid();
    };
    this._resize = resize;
    if (window.ResizeObserver) new ResizeObserver(resize).observe(this.wrap);
    else window.addEventListener("resize", resize);
    resize();
  }

  _laneCount() { return Math.max(1, this.visibleRoles.length); }

  // Total height this chart wants: plot + one lane per visible actuator + axis.
  desiredHeight() {
    return PLOT_MIN_H + LANE_TOP_PAD + this._laneCount() * (LANE_H + LANE_GAP) + AXIS_H;
  }

  _geom() {
    const lanes = this._laneCount();
    const below = LANE_TOP_PAD + lanes * (LANE_H + LANE_GAP) + AXIS_H;
    return {
      left: 42, right: this.w - 10, top: 14,
      bottom: Math.max(60, this.h - below),
      stripTop: Math.max(60, this.h - below) + LANE_TOP_PAD,
    };
  }

  // Grow the container so added actuators get their own lane instead of
  // overrunning the axis. Only ever grows the wrapper it owns.
  _applyHeight() {
    if (!this.wrap) return;
    const want = this.desiredHeight();
    if (this.wrap.clientHeight !== want) {
      this.wrap.style.height = want + "px";
      if (this._resize) this._resize();
    }
  }

  ensureRoles(roles) {
    // The roster arrives from the server AFTER construction, and an actuator
    // can join a live bus at any moment. Growing in place keeps whatever the
    // already-tracked roles have accumulated -- rebuilding would blank the
    // chart every time a servo was addressed.
    for (const r of roles || []) {
      if (!this.history[r]) this.history[r] = [];
      if (!this.targetHistory[r]) this.targetHistory[r] = [];
    }
    this.roles = Object.keys(this.history);
    if (!this._visibleExplicit) this.visibleRoles = this.roles;
    this._applyHeight();
  }

  setVisibleRoles(roles) {
    this._visibleExplicit = true;
    this.visibleRoles = roles;
    this._applyHeight();
  }

  now() {
    if (this._engT == null) return performance.now() / 1000;
    return this._engT + (performance.now() / 1000 - this._perfAtEngT);
  }
  _noteEngineT(t) {
    if (typeof t !== "number") return;
    // A server RESTART rewinds the engine clock to ~0 while this tab stays
    // open (socket.io auto-reconnects; no reload happens). Refusing backward
    // anchors stranded now() far in the future: every new sample looked
    // older than the window, draw() skipped it and _trim() discarded it --
    // the chart went silently blank while the gauges kept working. A jump
    // BACKWARD well beyond ordinary out-of-order jitter is therefore a new
    // session: clear history recorded on the old clock and re-anchor.
    if (this._engT != null && t < this._engT - 2) {
      for (const r of this.roles) { this.history[r] = []; this.targetHistory[r] = []; }
      this._rebuildGrid();
    }
    if (this._engT == null || t >= this._engT) {
      this._engT = t;
      this._perfAtEngT = performance.now() / 1000;
    }
  }

  pushActual(role, t, v, extra) {
    if (!this.history[role]) return;
    this._noteEngineT(t);
    this.history[role].push({
      t, v,
      moving: extra ? extra.moving : null,
      statusBits: extra ? extra.statusBits : null,
      counter: extra ? extra.counter : null,
    });
    this._trim();
  }

  pushTarget(role, t, v) {
    if (!this.targetHistory[role]) return;
    this._noteEngineT(t);
    const arr = this.targetHistory[role];
    const last = arr[arr.length - 1];
    if (!last || Math.abs(last.v - v) > 0.05) arr.push({ t, v });
    this._trim();
  }

  setWindowS(s) {
    if (!(s >= 5 && s <= MAX_KEEP_S)) return;
    this.windowS = s;
    this._rebuildGrid();
  }
  setYMode(m) {
    if (m !== "full" && m !== "band") return;
    this.yMode = m;
    this._rebuildGrid();
  }

  _trim() {
    const cutoff = this.now() - MAX_KEEP_S;
    for (const r of this.roles) {
      while (this.history[r].length && this.history[r][0].t < cutoff) this.history[r].shift();
      while (this.targetHistory[r].length && this.targetHistory[r][0].t < cutoff) this.targetHistory[r].shift();
    }
  }

  _yRange() {
    if (this.yMode === "band") {
      // UNION of every visible role's band. Centring on the first role with
      // data alone clamped any other role flat against the plot edge -- which
      // read as "that servo is steady", the opposite of what the instrument
      // must reveal. With one visible role this is identical to ±5 around it.
      let lo = null, hi = null;
      for (const r of this.visibleRoles) {
        const tgt = this.targetHistory[r], act = this.history[r];
        const c = tgt.length ? tgt[tgt.length - 1].v : (act.length ? act[act.length - 1].v : null);
        if (c == null) continue;
        if (lo == null || c - 5 < lo) lo = c - 5;
        if (hi == null || c + 5 > hi) hi = c + 5;
      }
      if (lo != null) return [Math.max(0, lo), Math.min(90, hi)];
    }
    return [0, 90];
  }

  _rebuildGrid() {
    // draw() re-runs the grid whenever the y-range drifts (band mode
    // recentres on every target change) -- this record is what it compares.
    this._gridYRange = this._yRange();
    const { left, right, top, bottom } = this._geom();
    const g = this.gGrid;
    g.replaceChildren();
    g.appendChild(svgEl("rect", { x: left, y: top, width: right - left, height: bottom - top, fill: "#FBFCFD", stroke: "#E4E7EC" }));
    const [yLo, yHi] = this._yRange();
    const span = yHi - yLo;
    const majorStep = span <= 12 ? 5 : 15;
    const minorStep = span <= 12 ? 1 : 5;
    const yPix = (v) => bottom - ((v - yLo) / span) * (bottom - top);
    for (let d = Math.ceil(yLo / minorStep) * minorStep; d <= yHi + 1e-9; d += minorStep) {
      if (d < yLo - 1e-9 || d > yHi + 1e-9) continue;
      const major = Math.abs(d % majorStep) < 1e-9;
      const y = yPix(d);
      if (major) g.appendChild(svgEl("line", { x1: left, x2: right, y1: y, y2: y, stroke: "#EDEFF3", "stroke-width": 1 }));
      else g.appendChild(svgEl("line", { x1: left, x2: right, y1: y, y2: y, stroke: "#F4F6F8", "stroke-width": 1 }));
      if (major) {
        const t = svgEl("text", { x: 6, y: y + 3, fill: "#8B93A1", "font-size": 10, "font-family": "Consolas, monospace" });
        t.textContent = String(Math.round(d));
        g.appendChild(t);
      }
    }
    const xStep = Math.max(5, Math.round(this.windowS / 6 / 5) * 5);
    for (let s = 0; s <= this.windowS; s += xStep) {
      const x = right - (s / this.windowS) * (right - left);
      const t = svgEl("text", { x, y: this.h - 6, fill: "#8B93A1", "font-size": 10, "text-anchor": "middle", "font-family": "Consolas, monospace" });
      t.textContent = "-" + s + "s";
      g.appendChild(t);
    }
  }

  _onMove(e) {
    const rect = this.svg.getBoundingClientRect();
    this._hoverPx = (e.clientX - rect.left) * (this.w / rect.width);
    this._hoverClient = { x: e.clientX - rect.left, y: e.clientY - rect.top };
  }

  _nearest(px, plotL, plotR, now, winS) {
    // arrays are time-ascending: scan newest-first, stop at the window edge
    let best = null;
    for (const role of this.visibleRoles) {
      const arr = this.history[role];
      for (let i = arr.length - 1; i >= 0; i--) {
        const p = arr[i];
        const age = now - p.t;
        if (age > winS) break;
        if (age < -0.05) continue;
        const x = plotR - (clamp(age, 0, winS) / winS) * (plotR - plotL);
        const dx = Math.abs(x - px);
        if (dx < 14 && (!best || dx < best.dx)) best = { dx, p, role, x };
      }
    }
    return best;
  }

  draw() {
    const yr = this._yRange();
    if (!this._gridYRange || this._gridYRange[0] !== yr[0] || this._gridYRange[1] !== yr[1]) {
      this._rebuildGrid();   // band scale drifted with the newest target -- keep labels honest
    }
    const { left, right, top, bottom, stripTop } = this._geom();
    const stripH = LANE_H;   // MOVING/status lane under the plot, one per actuator
    const frag = document.createDocumentFragment();
    const [yLo, yHi] = this._yRange();
    const span = Math.max(1e-6, yHi - yLo);
    const now = this.now();
    const winS = this.windowS;
    const xPix = (t) => right - (clamp(now - t, 0, winS) / winS) * (right - left);
    const yPix = (v) => bottom - ((clamp(v, yLo, yHi) - yLo) / span) * (bottom - top);

    // dropout bands first so the traces draw on top of them -- a dropout
    // drawn as a straight line would hide exactly the fault this instrument
    // exists to find.
    for (const role of this.visibleRoles) {
      const arr = this.history[role];
      for (let i = 1; i < arr.length; i++) {
        const dt = arr[i].t - arr[i - 1].t;
        if (dt <= GAP_SHADE_T) continue;
        if (now - arr[i].t > winS) continue;
        const x1 = xPix(arr[i - 1].t), x2 = Math.max(xPix(arr[i].t), x1 + 2);
        frag.appendChild(svgEl("rect", {
          x: x1, y: top, width: Math.max(2, x2 - x1), height: bottom - top,
          fill: "#FBE9E9", stroke: "#E8B4B4", "stroke-width": 0.5,
        }));
      }
    }

    // MOVING / status-bit lanes: filled where MOVING asserted; red tick =
    // bit11 status frame; dark tick = bit14. One lane per visible role.
    this.visibleRoles.forEach((role, li) => {
      const ly = stripTop + li * (stripH + LANE_GAP);
      frag.appendChild(svgEl("rect", { x: left, y: ly, width: right - left, height: stripH, fill: "#F4F6F8" }));
      frag.appendChild(svgEl("text", { x: left - 4, y: ly + stripH - 2, fill: "#8B93A1", "font-size": 7.5, "text-anchor": "end", "font-family": "Consolas, monospace" })).textContent = (ROLE_META[role] && ROLE_META[role].address) ? String(ROLE_META[role].address) : ROLE_LABEL[role][0];
      const arr = this.history[role];
      let runStart = null;
      for (let i = 0; i < arr.length; i++) {
        const p = arr[i], inWin = (now - p.t) <= winS && (now - p.t) >= -0.05;
        if (p.moving && inWin && runStart === null) runStart = p.t;
        if ((!p.moving || !inWin) && runStart !== null) {
          frag.appendChild(svgEl("rect", { x: xPix(runStart), y: ly, width: Math.max(1.5, xPix(arr[i].t) - xPix(runStart)), height: stripH, fill: "#E8CE92", opacity: 0.85 }));
          runStart = null;
        }
        if (!inWin) continue;
        if (p.statusBits != null) {
          if (p.statusBits & 0x800) frag.appendChild(svgEl("rect", { x: xPix(p.t), y: ly, width: 1.4, height: stripH, fill: "#B3261E" }));
          if (p.statusBits & 0x4000) frag.appendChild(svgEl("rect", { x: xPix(p.t) + 1.5, y: ly, width: 1.4, height: stripH, fill: "#565E6D" }));
        }
      }
      if (runStart !== null) frag.appendChild(svgEl("rect", { x: xPix(runStart), y: ly, width: Math.max(1.5, right - xPix(runStart)), height: stripH, fill: "#E8CE92", opacity: 0.85 }));
    });

    for (const role of this.visibleRoles) {
      const tgt = this.targetHistory[role];
      if (tgt.length) {
        let d = "", prevY = null;
        for (const p of tgt) {
          const age = now - p.t;
          if (age < -1 || age > winS) continue;
          const x = xPix(p.t), y = yPix(p.v);
          d += prevY === null ? `M ${x.toFixed(1)} ${y.toFixed(1)} ` : `L ${x.toFixed(1)} ${prevY.toFixed(1)} L ${x.toFixed(1)} ${y.toFixed(1)} `;
          prevY = y;
        }
        if (prevY !== null) d += `L ${right} ${prevY.toFixed(1)}`;
        if (d) frag.appendChild(svgEl("path", { d, fill: "none", stroke: COLORS[role], "stroke-width": 1.6, "stroke-dasharray": "5 4", opacity: 0.75 }));
      }
      // actual: solid, broken past GAP_BREAK_T of silence
      const arr = this.history[role];
      if (!arr.length) continue;
      let d = "", lastT = null;
      for (const p of arr) {
        const age = now - p.t;
        if (age < -0.05 || age > winS) continue;
        const x = xPix(p.t).toFixed(1), y = yPix(p.v).toFixed(1);
        d += (lastT === null || (p.t - lastT) > GAP_BREAK_T) ? `M ${x} ${y} ` : `L ${x} ${y} `;
        lastT = p.t;
      }
      if (d) frag.appendChild(svgEl("path", { d, fill: "none", stroke: COLORS[role], "stroke-width": 2.4, "stroke-linejoin": "round" }));
    }

    // crosshair + tooltip from the mouse position captured by _onMove()
    if (this._hoverPx != null && this._hoverPx >= left && this._hoverPx <= right) {
      const hit = this._nearest(this._hoverPx, left, right, now, winS);
      if (hit) {
        frag.appendChild(svgEl("line", { x1: hit.x, y1: top, x2: hit.x, y2: bottom, stroke: "#C6CBD4", "stroke-width": 1, "stroke-dasharray": "3 3" }));
        frag.appendChild(svgEl("circle", { cx: hit.x, cy: yPix(hit.p.v), r: 3.5, fill: COLORS[hit.role], stroke: "#fff", "stroke-width": 1.2 }));
        const b = hit.p.statusBits;
        this._tip.innerHTML =
          `<b style="color:${COLORS[hit.role]}">${ROLE_LABEL[hit.role]}</b> T+${hit.p.t.toFixed(2)}s<br>` +
          `${hit.p.v.toFixed(1)}° · counter ${hit.p.counter != null ? fmtCounter(hit.p.counter) : "—"}` +
          `<br>MOVING ${hit.p.moving ? "SET" : "clear"}` +
          (b != null ? ` · bit11 ${b & 0x800 ? "SET" : "-"} · bit14 ${b & 0x4000 ? "SET" : "-"}` : "");
        this._tip.style.display = "";
        const cw = this.wrap.clientWidth;
        const tx = clamp(this._hoverClient.x + 12, 0, cw - 170);
        this._tip.style.left = tx + "px";
        this._tip.style.top = Math.max(0, this._hoverClient.y - 54) + "px";
      } else {
        this._tip.style.display = "none";
      }
    } else {
      this._tip.style.display = "none";
    }

    this.gSeries.replaceChildren(frag);
  }
}

function fmtCounter(c) {
  return typeof c === "number" ? "0x" + c.toString(16).toUpperCase().padStart(2, "0") : String(c);
}

// ----------------------------------------------------------------- log --

function logRow(kind, t, text) {
  const log = document.getElementById("log");
  if (!log) return;
  const row = document.createElement("div");
  row.className = `log-row kind-${kind}`;
  // Built from nodes, not innerHTML: converter notices embed RAW device
  // output, and a malformed line must never become markup.
  const ts = document.createElement("span");
  ts.className = "t mono";
  ts.textContent = `T+${(typeof t === "number" ? t : 0).toFixed(1)}s`;
  const body = document.createElement("span");
  body.textContent = String(text);
  row.append(ts, body);
  log.appendChild(row);
  while (log.children.length > 200) log.removeChild(log.firstChild);
  log.scrollTop = log.scrollHeight;
}

const lostBanner = document.getElementById("lostBanner");
socket.on("notices", (notices) => {
  for (const n of notices) {
    logRow(n.kind, n.t, n.text);
    if (n.kind === "safety" && n.text.includes("HOLD asserted automatically") && lostBanner) {
      lostBanner.classList.add("show");
    }
  }
});
socket.on("error", (payload) => {
  // Timestamp server refusals on the ENGINE clock -- the same T+ origin as
  // every notice row. performance.now() is seconds-since-page-load and used
  // to mix two unrelated origins in one column, breaking timeline
  // correlation during fault analysis (which is the log's whole job).
  logRow("fault", typeof latestSnapshot.t === "number" ? latestSnapshot.t : 0, payload.text);
});

// -------------------------------------------------------------- link chip --

const linkChip = document.getElementById("linkChip");
socket.on("connect", () => { linkChip.textContent = "UI LINK OK"; linkChip.className = "chip link-ok"; });
socket.on("disconnect", () => { linkChip.textContent = "UI LINK LOST"; linkChip.className = "chip link-lost"; });

// -------------------------------------------------------------- safety bar --
// Present, and wired identically, in every view -- this is deliberate:
// HOLD/mode/bus-switch must behave the same whichever page a technician is
// looking at, since the actuator does not care which page is open.

function setMode(mode) {
  socket.emit("set_mode", { mode });
  document.getElementById("btnListen").classList.toggle("active", mode === "listen");
  document.getElementById("btnActive").classList.toggle("active", mode === "active");
}
document.getElementById("btnListen").addEventListener("click", () => setMode("listen"));
document.getElementById("btnActive").addEventListener("click", () => {
  if (confirm(
    "Confirm the isolated bench before transmitting:\n\n" +
    "• No LMV51 connected\n• No burner, fuel or fan connected\n" +
    "• Shaft mechanically uncoupled\n• CAN bitrate 50 kbps (set in VxCAN Utility)\n" +
    "• Bus termination ≈ 60 Ω"
  )) setMode("active");
});

function doHold() {
  socket.emit("request_hold");
  if (lostBanner) lostBanner.classList.remove("show");
}
document.getElementById("btnHold").addEventListener("click", doHold);
document.addEventListener("keydown", (e) => {
  const el = document.activeElement;
  const tag = el && el.tagName;
  if (tag === "INPUT" || tag === "TEXTAREA" || tag === "SELECT" || (el && el.isContentEditable)) return;
  if (e.repeat) return;   // a held key autorepeats ~30/s -- one HOLD is enough
  if (e.code === "Escape" || e.code === "Space") { e.preventDefault(); doHold(); }
});

// One button, two directions, same press-and-hold-1s animation both ways:
//   idle -> holding (fill fills over 1s) -> pending (held 1s, request sent,
//   fill locked full + pulsing label while we wait for the SERVER to
//   actually confirm, which can take a few seconds on real hardware --
//   transmit's TX thread join has a 3s timeout) -> confirmed (server
//   snapshot really did report the requested mode -- snap instantly to an
//   inverted style, no transition, so the moment of confirmation is
//   unmistakable) -> fades back to idle after a beat.
// Direction is read from the snapshot: idle -> "on" (button turns the bus
// back on, always into the safe LISTEN ONLY mode -- KEEP ALIVE still goes
// through the BUS MODE toggle's own isolated-bench confirmation); anything
// else -> "off". Captured once when a hold STARTS so a mode change arriving
// mid-press can't swap the action under your finger.
const btnBusSwitch = document.getElementById("btnBusSwitch");
const btnBusSwitchLabel = btnBusSwitch.querySelector("span");
const btnBusSwitchCaption = document.getElementById("busSwitchCaption");
let holdTimer = null;
let busSwitchFallbackTimer = null;
let busSwitchBusy = false;
let busSwitchCommittedDirection = null;

function busSwitchDirection() {
  return latestSnapshot.mode === "idle" ? "on" : "off";
}
function setBusSwitchPhase(phase) {
  btnBusSwitch.classList.remove("holding", "pending", "confirmed", "fading");
  if (phase) btnBusSwitch.classList.add(phase);
}
function syncBusSwitchIdle() {
  if (busSwitchBusy) return;
  const dir = busSwitchDirection();
  btnBusSwitch.classList.toggle("turn-on", dir === "on");
  btnBusSwitchLabel.textContent = dir === "on" ? "BUS ON" : "SHUT DOWN BUS";
  btnBusSwitchCaption.textContent = dir === "on"
    ? "Press and hold 1 s — resumes listening"
    : "Press and hold 1 s — actuator switches off";
}
function startBusSwitchHold() {
  if (busSwitchBusy) return;
  busSwitchBusy = true;
  busSwitchCommittedDirection = busSwitchDirection();
  btnBusSwitch.classList.toggle("turn-on", busSwitchCommittedDirection === "on");
  setBusSwitchPhase("holding");
  holdTimer = setTimeout(() => {
    setBusSwitchPhase("pending");
    btnBusSwitchLabel.textContent = busSwitchCommittedDirection === "on" ? "STARTING…" : "SHUTTING DOWN…";
    if (busSwitchCommittedDirection === "on") {
      socket.emit("set_mode", { mode: "listen" });
    } else {
      socket.emit("shutdown_bus");
    }
    clearTimeout(busSwitchFallbackTimer);
    busSwitchFallbackTimer = setTimeout(() => {
      if (btnBusSwitch.classList.contains("pending")) {
        setBusSwitchPhase(null);
        busSwitchBusy = false;
        syncBusSwitchIdle();
      }
    }, 8000);
  }, 1000);
}
function cancelBusSwitchHold() {
  clearTimeout(holdTimer);
  if (btnBusSwitch.classList.contains("holding")) {
    setBusSwitchPhase(null);
    busSwitchBusy = false;
    syncBusSwitchIdle();
  }
}
btnBusSwitch.addEventListener("mousedown", startBusSwitchHold);
btnBusSwitch.addEventListener("mouseup", cancelBusSwitchHold);
btnBusSwitch.addEventListener("mouseleave", cancelBusSwitchHold);
btnBusSwitch.addEventListener("touchstart", startBusSwitchHold);
btnBusSwitch.addEventListener("touchend", cancelBusSwitchHold);
// The OS can CANCEL a touch mid-press (notification banner, palm rejection,
// edge gesture). Without these the button stayed in "holding" forever and
// busSwitchBusy locked the whole BUS ON/OFF control until a reload.
btnBusSwitch.addEventListener("touchcancel", cancelBusSwitchHold);
btnBusSwitch.addEventListener("pointercancel", cancelBusSwitchHold);

function confirmBusSwitchIfPending(snapshot) {
  if (!btnBusSwitch.classList.contains("pending")) return;
  const satisfied = busSwitchCommittedDirection === "on"
    ? snapshot.mode === "listen"
    : snapshot.mode === "idle";
  if (!satisfied) return;
  clearTimeout(busSwitchFallbackTimer);
  setBusSwitchPhase("confirmed");
  btnBusSwitchLabel.textContent = busSwitchCommittedDirection === "on" ? "BUS ON" : "BUS OFF";
  setTimeout(() => {
    btnBusSwitch.classList.add("fading");
    setTimeout(() => {
      setBusSwitchPhase(null);
      busSwitchBusy = false;
      syncBusSwitchIdle();
    }, 650);
  }, 1200);
}

// The BUS MODE buttons toggle optimistically on click for responsiveness --
// but a REFUSED mode change (port held by another process, start() failure)
// leaves the bus idle while the highlight claims otherwise, on an instrument
// where transmit-state is the one thing that must never be misreported.
// Every snapshot reconciles the highlight with what the server actually
// reports, so a refusal self-corrects within one broadcast tick (~100 ms).
function syncModeButtons(snapshot) {
  document.getElementById("btnListen").classList.toggle("active", snapshot.mode === "listen");
  document.getElementById("btnActive").classList.toggle("active", snapshot.mode === "active");
}
onSnapshot((snapshot) => { confirmBusSwitchIfPending(snapshot); syncBusSwitchIdle(); syncModeButtons(snapshot); });

// -------------------------------------------------------- change adapter --
// Only rendered by base.html when desktop_mode is true, so this element is
// simply absent under a plain `python sqm45_webapp.py` launch -- no guard
// needed for that case beyond the null check. Restarting is a full process
// re-spawn owned by desktop/launcher.py's Api.change_adapter(), NOT a Flask
// route: killing and relaunching the process is not something an HTTP
// response can safely straddle, and the pywebview bridge already gives us a
// synchronous Python call that does not depend on the HTTP connection
// surviving it.
const changeAdapterLink = document.getElementById("changeAdapterLink");
if (changeAdapterLink) {
  changeAdapterLink.addEventListener("click", (e) => {
    e.preventDefault();
    if (!window.pywebview || !window.pywebview.api || !window.pywebview.api.change_adapter) {
      alert("Change adapter is only available in the desktop app.");
      return;
    }
    if (!confirm("Disconnect and return to the port picker?\n\nThe app will restart -- "
                + "transmission stops immediately and the actuator will need live traffic "
                + "again once you reconnect."))
      return;
    changeAdapterLink.textContent = "Restarting…";
    changeAdapterLink.style.pointerEvents = "none";
    window.pywebview.api.change_adapter();
  });
}
