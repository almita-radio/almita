// ALMITA STATUS: one compact, read-only view of services, dependencies, workflows and operational readiness.
// Service UP is not operational READY: the dependencies decide (the backend computes it; this page only shows it).
(function () {
  "use strict";
  const U = window.AlmitaUI;
  const $ = (id) => document.getElementById(id);
  const strip = U.mountHeader("STATUS", "SYSTEM HEALTH");
  U.mountFooter();

  function fill(tbodyId, rows) {
    const tb = $(tbodyId);
    tb.textContent = "";
    if (!rows.length) {
      const tr = tb.insertRow();
      const td = tr.insertCell();
      td.colSpan = 3;
      td.textContent = "No data yet";
      return;
    }
    for (const [name, item] of rows) {
      const tr = tb.insertRow();
      tr.insertCell().textContent = name.replace(/_/g, " ").toUpperCase();
      const st = tr.insertCell();
      const badge = document.createElement("span");
      badge.className = "badge " + U.stateClass(item.state);
      badge.textContent = item.state;
      st.appendChild(badge);
      tr.insertCell().textContent = item.detail || "—";
    }
  }

  function render(h) {
    const op = h.operational || {};
    U.setBadge($("op-badge"), op.level || "UNKNOWN");
    const lines = op.reasons && op.reasons.length ? op.reasons.map((r) => "• " + r) : ["• all required services and dependencies are up"];
    if (op.degraded_by && op.degraded_by.length) lines.push("", "Degraded by: " + op.degraded_by.join(", "));
    lines.push("", op.note || "");
    $("op-reasons").textContent = lines.join("\n");
    fill("tbl-services", Object.entries(h.services || {}));
    fill("tbl-deps", Object.entries(h.dependencies || {}));
    fill("tbl-workflows", Object.entries(h.workflows || {}));
    $("updated").textContent = "generated " + U.utc(h.generated_utc);
    const v = h.version || {};
    const kv = $("version-kv");
    kv.textContent = "";
    for (const [k, val] of [["BUILD (git)", v.git_short_sha], ["STARTED", U.utc(v.started_utc)], ["HOSTNAME", v.hostname], ["TRANSPORT", v.transport], ["PROJECT", v.project_url]]) {
      const dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = k;
      dd.textContent = val || "—";
      kv.append(dt, dd);
    }
  }

  const poller = U.poller(async () => {
    const r = await U.api("/api/system/health", { timeoutMs: 8000 });
    if (!r.ok) { U.showError($("error-banner"), r.error); return false; }
    U.showError($("error-banner"), "");
    render(r.data.data);
    return true;
  }, { intervalMs: 5000, onState: (state) => {
    strip.textContent = "";
    strip.appendChild(U.chip("LINK", state, U.stateClass(state)));
  } });
  $("btn-refresh").addEventListener("click", U.guard($("btn-refresh"), () => { tempPoller1h.now(); tempPoller6h.now(); return poller.now(); }, "REFRESHING…"));
  poller.start();

  // ------------------------------------------------------------------ TEMPERATURES (temperature_logger.py, operator 2026-10-17)
  // One line per DS18B20; each sensor keeps its own colour whatever else is configured (colour follows the sensor, never
  // its position), and every line carries its name at its end, so identity never depends on colour alone. The table
  // below is the text view of the same data. STABLE / CHANGING / STALE come from the server's own rule.
  const ROLE_COLORS = { sdr: "#3987e5", lna: "#d95926", load: "#199e70", cable: "#c98500" };   // data colours: U.dataColor darkens the light one on DÍA
  const EXTRA_COLORS = ["#d55181", "#9085e9", "#e66767", "#008300"];
  const extraRoles = [];
  function roleColor(role) {
    if (ROLE_COLORS[role]) return U.dataColor(ROLE_COLORS[role]);
    if (!extraRoles.includes(role)) extraRoles.push(role);
    return U.dataColor(EXTRA_COLORS[extraRoles.indexOf(role) % EXTRA_COLORS.length]);
  }
  const STATE_TEXT = { STABLE: "STABLE", CHANGING: "CHANGING", STALE: "NO RECENT DATA", NO_DATA: "NO DATA", WARMING_UP_LOG: "UNDER 10 MIN OF DATA" };
  const STATE_CLASS = { STABLE: "status-ok", CHANGING: "status-warning", STALE: "status-down", NO_DATA: "status-down", WARMING_UP_LOG: "status-warning" };
  // Two charts, same drawing: the last hour (refreshed every 20 s) and, below it, the last 6 h (operator 2026-10-17).
  const charts = {
    h1: { canvas: "temp-chart", tip: "temp-tip", data: null, geom: null, tickMin: 10 },
    h6: { canvas: "temp-chart-6h", tip: "temp-tip-6h", data: null, geom: null, tickMin: 60 },
  };

  function drawTemps(ch) {
    const c = $(ch.canvas), d = ch.data;
    const dpr = U.pixelRatio(), w = c.clientWidth || 800, h = 260;
    c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
    const ctx = c.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    ctx.font = "11px ui-monospace,monospace";
    const lines = d ? Object.entries(d.series).map(([role, pts]) => [role, pts.filter((p) => p[1] != null).map((p) => [Date.parse(p[0]), p[1]])]) : [];
    const all = lines.flatMap(([, pts]) => pts);
    if (!all.length) { ctx.fillStyle = U.cssVar("--muted", "#91a2ad"); ctx.fillText(d ? `no temperature samples in the last ${d.hours} h (sensors not connected?)` : "loading…", 12, 24); ch.geom = null; return; }
    const t1 = Date.parse(d.generated_utc), t0 = t1 - d.hours * 3600e3;
    let lo = Math.min(...all.map((p) => p[1])), hi = Math.max(...all.map((p) => p[1]));
    if (hi - lo < 2) { const m = (hi + lo) / 2; lo = m - 1; hi = m + 1; }
    const padL = 44, padR = 64, padT = 10, padB = 24;
    const x = (t) => padL + ((t - t0) / (t1 - t0)) * (w - padL - padR);
    const y = (v) => padT + (1 - (v - lo) / (hi - lo)) * (h - padT - padB);
    ctx.strokeStyle = U.cssRgba("--muted", U.isDay() ? 0.3 : 0.18, "#91a2ad"); ctx.fillStyle = U.cssVar("--muted", "#91a2ad"); ctx.lineWidth = 1;
    for (let k = 0; k <= 4; k++) {                                     // recessive grid, °C labels in muted ink
      const v = lo + (k / 4) * (hi - lo), yy = y(v);
      ctx.beginPath(); ctx.moveTo(padL, yy); ctx.lineTo(w - padR, yy); ctx.stroke();
      ctx.textAlign = "right"; ctx.fillText(v.toFixed(1) + "°", padL - 6, yy + 4);
    }
    ctx.textAlign = "left";
    const step = ch.tickMin * 60e3;
    for (let tt = Math.ceil(t0 / step) * step; tt <= t1; tt += step) ctx.fillText(new Date(tt).toISOString().slice(11, 16), x(tt) - 14, h - 6);
    for (const [role, pts] of lines) {
      if (!pts.length) continue;
      ctx.strokeStyle = roleColor(role); ctx.lineWidth = 2; ctx.lineJoin = "round"; ctx.beginPath();
      pts.forEach((p, i) => { const px = x(p[0]), py = y(p[1]); if (i && p[0] - pts[i - 1][0] > 3 * d.interval_s * 1000) ctx.moveTo(px, py); else if (i) ctx.lineTo(px, py); else ctx.moveTo(px, py); });
      ctx.stroke();
      const lp = pts[pts.length - 1];                                    // direct label at the line's end (text ink, not the series colour)
      ctx.fillStyle = roleColor(role); ctx.fillRect(x(lp[0]) + 4, y(lp[1]) - 4, 8, 8);
      ctx.fillStyle = U.cssVar("--text", "#e6edf1"); ctx.fillText(role.toUpperCase(), x(lp[0]) + 15, y(lp[1]) + 4);
    }
    ch.geom = { x, y, t0, t1, padL, padR, w, h, lines };
  }

  function tipAt(ch, ev) {
    const tip = $(ch.tip), g = ch.geom;
    if (!g) { tip.hidden = true; return; }
    const mx = U.pointerXY(ev, $(ch.canvas)).x;
    if (mx < g.padL || mx > g.w - g.padR) { tip.hidden = true; drawTemps(ch); return; }
    const t = g.t0 + ((mx - g.padL) / (g.w - g.padL - g.padR)) * (g.t1 - g.t0);
    const rows = [];
    for (const [role, pts] of g.lines) {
      let best = null;
      for (const p of pts) if (!best || Math.abs(p[0] - t) < Math.abs(best[0] - t)) best = p;
      if (best && Math.abs(best[0] - t) < 5 * 60e3) rows.push(`${role.toUpperCase()}  ${best[1].toFixed(2)} °C`);
    }
    drawTemps(ch);
    const ctx = $(ch.canvas).getContext("2d");
    ctx.strokeStyle = U.cssRgba("--text", 0.45, "#e6edf1"); ctx.lineWidth = 1; ctx.beginPath(); ctx.moveTo(mx, 10); ctx.lineTo(mx, g.h - 24); ctx.stroke();
    tip.textContent = [new Date(t).toISOString().slice(11, 16) + " UTC", ...rows].join("\n");
    tip.style.left = Math.min(mx + 12, g.w - 150) + "px"; tip.hidden = false;
  }
  for (const ch of Object.values(charts)) {
    $(ch.canvas).addEventListener("mousemove", (ev) => tipAt(ch, ev));
    $(ch.canvas).addEventListener("mouseleave", () => { $(ch.tip).hidden = true; drawTemps(ch); });
  }
  window.addEventListener("resize", () => Object.values(charts).forEach(drawTemps));
  U.onThemeChange(() => {                       // DÍA/NOCHE: same data, new colours (chart lines + table swatches), no request
    Object.values(charts).forEach(drawTemps);
    for (const sw of document.querySelectorAll("#tbl-temps .temp-swatch")) if (sw.dataset.role) sw.style.background = roleColor(sw.dataset.role);
  });

  function renderTemps(d) {                     // the 6 h answer: its chart + the table (STABLE since needs the long window)
    charts.h6.data = d; drawTemps(charts.h6);
    const tb = $("tbl-temps"); tb.textContent = "";
    const roles = Object.keys(d.status);
    if (!roles.length) { const td = tb.insertRow().insertCell(); td.colSpan = 5; td.textContent = "no temperature_sensors configured in observer_config.json"; }
    for (const role of roles) {
      const s = d.status[role], tr = tb.insertRow();
      const name = tr.insertCell(), sw = document.createElement("span");
      sw.className = "temp-swatch"; sw.dataset.role = role; sw.style.background = roleColor(role); name.append(sw, document.createTextNode(role.toUpperCase()));
      tr.insertCell().textContent = s.last_c == null ? "—" : `${s.last_c.toFixed(2)} °C (${U.utcTime(s.last_utc)})`;
      tr.insertCell().textContent = s.change_last_window_c == null ? "—" : `${s.change_last_window_c >= 0 ? "+" : ""}${s.change_last_window_c.toFixed(2)} °C`;
      const st = tr.insertCell(), b = document.createElement("span");
      b.className = "badge " + (STATE_CLASS[s.state] || "status-unknown"); b.textContent = STATE_TEXT[s.state] || s.state;
      st.appendChild(b);
      if (s.state === "STABLE" && s.stable_since) st.appendChild(document.createTextNode(` since ${U.utcTime(s.stable_since)}`));
      else if (s.last_error && s.state !== "STABLE") st.appendChild(document.createTextNode(` ${s.last_error}`));
      tr.insertCell().textContent = s.sensor_id || "—";
    }
    const states = roles.map((r) => d.status[r].state);
    U.setBadge($("temp-badge"), !roles.length ? "NOT CONFIGURED" : states.every((x) => x === "STABLE") ? "ALL STABLE" : states.some((x) => x === "CHANGING") ? "CHANGING" : "INCOMPLETE");
  }

  // ------------------------------------------------------------------ MAIN LNA POWER (Bias-T), operator 2026-10-17
  // Switched OFF by hand while idle; every MAIN acquisition switches it ON by itself. The state is what rtl_tcp itself
  // last acknowledged (no voltage readback exists). The server refuses while anything holds MAIN.
  let lna = null;
  function renderLna(d) {
    lna = d;
    const on = d.state === "on", off = d.state === "off";
    U.setBadge($("lna-badge"), on ? "ON" : off ? "OFF" : "UNKNOWN");
    $("lna-detail").textContent = (d.since_utc ? `${d.state.toUpperCase()} since ${U.utc(d.since_utc)} (${d.source})` : `state unknown${d.source ? " - " + d.source : ""}`)
      + (d.service_has_T === false ? "\nrtl_tcp runs WITHOUT -T: the LNA is never switched on from the web." : "")
      + (off ? "\nThe next acquisition switches it on, but it will not be thermally settled: switch it on here first and wait for STABLE below." : "");
    const b = $("btn-lna");
    b.textContent = on ? "SWITCH LNA OFF" : "SWITCH LNA ON (warm up)";
    U.setEnabled(b, (on || off) && !(off && d.service_has_T === false), "LNA state unknown");
  }
  async function loadLna() {
    const r = await U.api("/api/sdr/lna", { timeoutMs: 15000 });
    if (r.ok) renderLna(r.data.data);
  }
  $("btn-lna").addEventListener("click", U.guard($("btn-lna"), async () => {
    if (!lna) return;
    const turnOn = lna.state !== "on";
    if (!window.confirm(turnOn ? "Switch MAIN's LNA ON now?\n\nIt needs time to settle thermally before measuring (see TEMPERATURES)."
      : "Switch MAIN's LNA OFF now?\n\nThe next acquisition (OBSERVE, ALIGN, CALIBRATE, LOBE) switches it back on by itself.")) return;
    const r = await U.api("/api/sdr/lna", { method: "POST", body: { on: turnOn, confirm: true }, timeoutMs: 20000 });
    if (!r.ok) { window.alert("LNA NOT switched: " + U.errorText(r.error)); await loadLna(); return; }
    renderLna(r.data.data);
  }, "SWITCHING…"));

  U.poller(async () => { await loadLna(); return true; }, { intervalMs: 300000 }).start();   // and after every click

  // ------------------------------------------------------------------ LOCAL RFI CHECK (local_rfi_check.py, operator 2026-10-29)
  // The excess of each channel over a rolling median, so narrow lines stand out from the band shape; the comb teeth
  // (every 357.14 kHz) are marked on the axis. The verdict lines come from the 2026-10-28 hunt.
  const RFI_GAINS = [0.0, 0.9, 1.4, 2.7, 3.7, 7.7, 8.7, 10.0, 10.6, 11.2, 12.5, 14.4, 15.7, 16.6, 19.7, 20.7, 22.9, 25.4, 28.0,
    29.7, 32.8, 33.8, 36.4, 37.2, 38.6, 40.2, 42.1, 43.4, 43.9, 44.5, 48.0, 49.6];   // calibration_engine.gain_sweep table
  let rfi = null, rfiGeom = null;
  function drawRfi() {
    const c = $("rfi-chart"), r = rfi && rfi.last;
    const dpr = U.pixelRatio(), w = c.clientWidth || 800, h = 260;
    c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
    const ctx = c.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    ctx.font = "11px ui-monospace,monospace";
    if (!r || !r.spectrum) { ctx.fillStyle = U.cssVar("--muted", "#91a2ad"); ctx.fillText(rfi ? "no check yet: press RUN CHECK" : "loading…", 12, 24); rfiGeom = null; return; }
    const f = r.spectrum.frequency_hz, e = r.spectrum.excess_db;
    const f0 = f[0], f1 = f[f.length - 1];
    const lo = -3, hi = Math.max(12, Math.ceil(Math.max(...e) + 1));
    const padL = 40, padR = 12, padT = 10, padB = 24;
    const x = (v) => padL + ((v - f0) / (f1 - f0)) * (w - padL - padR);
    const y = (v) => padT + (1 - (Math.max(lo, Math.min(hi, v)) - lo) / (hi - lo)) * (h - padT - padB);
    ctx.strokeStyle = U.cssRgba("--muted", U.isDay() ? 0.3 : 0.18, "#91a2ad"); ctx.fillStyle = U.cssVar("--muted", "#91a2ad"); ctx.lineWidth = 1;
    for (let v = 0; v <= hi; v += hi > 24 ? 6 : 3) {
      ctx.beginPath(); ctx.moveTo(padL, y(v)); ctx.lineTo(w - padR, y(v)); ctx.stroke();
      ctx.textAlign = "right"; ctx.fillText(`${v > 0 ? "+" : ""}${v}`, padL - 6, y(v) + 4);
    }
    ctx.textAlign = "center";
    for (let mhz = Math.ceil(f0 / 2e5) * 0.2; mhz * 1e6 <= f1; mhz += 0.2) ctx.fillText((mhz).toFixed(1), x(mhz * 1e6), h - 6);
    ctx.textAlign = "left";
    ctx.strokeStyle = U.cssVar("--warn", "#e6b85c"); ctx.setLineDash([4, 4]);                 // the line threshold
    ctx.beginPath(); ctx.moveTo(padL, y(r.line_threshold_db)); ctx.lineTo(w - padR, y(r.line_threshold_db)); ctx.stroke(); ctx.setLineDash([]);
    ctx.strokeStyle = U.dataColor("#3987e5"); ctx.lineWidth = 1.2; ctx.beginPath();
    e.forEach((v, i) => { const px = x(f[i]), py = y(v); i ? ctx.lineTo(px, py) : ctx.moveTo(px, py); });
    ctx.stroke();
    for (const t of r.teeth) {                                                                   // comb teeth: marker + value
      const px = x(t.frequency_hz), bad = t.excess_db >= 6, faint = t.excess_db >= 3;
      ctx.fillStyle = bad ? U.cssVar("--error", "#e36b6b") : faint ? U.cssVar("--warn", "#e6b85c") : U.cssVar("--muted", "#91a2ad");
      ctx.beginPath(); ctx.moveTo(px, h - padB); ctx.lineTo(px - 5, h - padB + 8); ctx.lineTo(px + 5, h - padB + 8); ctx.fill();
      ctx.fillText(`${t.excess_db >= 0 ? "+" : ""}${t.excess_db.toFixed(1)}`, px + 4, padT + 10);
    }
    rfiGeom = { x, f0, f1, padL, padR, w, h, f, e };
  }
  function renderRfi(d) {
    rfi = d;
    const nv = d.nanovna || {};
    $("rfi-nanovna").hidden = !nv.present;
    $("rfi-nanovna").textContent = nv.present ? "⚠ " + nv.detail : "";
    $("rfi-nanovna").style.color = "var(--error)";
    const r = d.last;
    U.setBadge($("rfi-badge"), r ? r.verdict : "NO CHECK YET");
    $("rfi-detail").textContent = !r ? "No check recorded yet."
      : `${r.verdict}: ${r.verdict_text}
`
        + `comb teeth: max ${r.tooth_max_db >= 0 ? "+" : ""}${r.tooth_max_db} dB, sum ${r.tooth_sum_db} dB · lines over +${r.line_threshold_db} dB: ${r.lines_over_threshold}`
        + ` · ADC rms ${r.adc_rms_codes} codes, rails ${(100 * r.adc_rail_hit_fraction).toFixed(3)}%
`
        + `${U.utc(r.utc)} · gain ${r.gain_db} dB · ${r.capture_seconds} s`
        + (r.nanovna && r.nanovna.present ? " · the NanoVNA WAS plugged in during this check" : "");
    const sel = $("rfi-gain");
    if (!sel.options.length) {
      for (const g of RFI_GAINS) sel.add(new Option(g.toFixed(1), String(g)));
      sel.value = String(r ? r.gain_db : d.default_gain_db);
    }
    drawRfi();
  }
  async function loadRfi() {
    const r = await U.api("/api/sdr/rfi_check", { timeoutMs: 15000 });
    if (r.ok) renderRfi(r.data.data);
  }
  $("btn-rfi").addEventListener("click", U.guard($("btn-rfi"), async () => {
    const gain = Number($("rfi-gain").value);
    if (!window.confirm(`Run a local RFI check now?\n\nMAIN captures a few seconds at ${gain} dB (the LNA is switched on). Refused while anything else uses MAIN.`)) return;
    const r = await U.api("/api/sdr/rfi_check", { method: "POST", body: { gain_db: gain, confirm: true }, timeoutMs: 60000 });
    if (!r.ok) { window.alert("CHECK NOT RUN: " + U.errorText(r.error)); return; }
    renderRfi(r.data.data);
  }, "MEASURING…"));
  $("rfi-chart").addEventListener("mousemove", (ev) => {
    const g = rfiGeom, tip = $("rfi-tip");
    if (!g) { tip.hidden = true; return; }
    const mx = U.pointerXY(ev, $("rfi-chart")).x;
    if (mx < g.padL || mx > g.w - g.padR) { tip.hidden = true; return; }
    const fq = g.f0 + ((mx - g.padL) / (g.w - g.padL - g.padR)) * (g.f1 - g.f0);
    let i = Math.round(((fq - g.f0) / (g.f1 - g.f0)) * (g.f.length - 1));
    let best = i;                                                   // snap to the strongest bin within ±3 px
    for (let k = -6; k <= 6; k++) { const j = i + k; if (j >= 0 && j < g.e.length && g.e[j] > g.e[best]) best = j; }
    tip.textContent = `${(g.f[best] / 1e6).toFixed(4)} MHz\n${g.e[best] >= 0 ? "+" : ""}${g.e[best].toFixed(1)} dB over the median`;
    tip.style.left = Math.min(mx + 12, g.w - 170) + "px"; tip.hidden = false;
  });
  $("rfi-chart").addEventListener("mouseleave", () => { $("rfi-tip").hidden = true; });
  U.onThemeChange(drawRfi);
  window.addEventListener("resize", drawRfi);
  U.poller(async () => { await loadRfi(); return true; }, { intervalMs: 60000 }).start();   // the NanoVNA warning follows the USB

  const tempPoller1h = U.poller(async () => {
    const r = await U.api("/api/system/temperatures?hours=1", { timeoutMs: 15000 });
    if (!r.ok) return false;
    charts.h1.data = r.data.data; drawTemps(charts.h1);
    return true;
  }, { intervalMs: 20000 });                      // last hour, every 20 s (operator 2026-10-17)
  const tempPoller6h = U.poller(async () => {
    const r = await U.api("/api/system/temperatures?hours=6", { timeoutMs: 15000 });
    if (!r.ok) return false;
    renderTemps(r.data.data);
    return true;
  }, { intervalMs: 60000 });                      // last 6 h + table, every minute (one sample a minute)
  tempPoller1h.start(); tempPoller6h.start();
})();
