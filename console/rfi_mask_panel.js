// MAIN · RFI MASK (operator 2026-10-30): the masked MAIN spectrum and what the mask takes out of it, from
// rfi_mask_live.py's runtime/main_rfi_mask_latest.json. Self-contained: shown only while that file belongs to the
// session capture.py announces now, so without rfi_mask_live.py the monitor looks exactly as before.
(function () {
  "use strict";
  const ROOT = (new URLSearchParams(location.search)).get("root") || window.ALMITA_RUNTIME_ROOT || "/runtime";
  const POLL_MS = 10000;                                                       // 2 small files / 10 s: no extra load on the monitor
  const $ = (id) => document.getElementById(id);
  const css = (name, fallback) => (getComputedStyle(document.documentElement).getPropertyValue(name) || "").trim() || fallback;
  const COLORS = () => ({ comb: css("--error", "#e36b6b"), line: css("--warn", "#e6b85c"), clean: css("--accent", "#65b7d8"),
    raw: css("--muted", "#91a2ad"), text: css("--muted", "#91a2ad"), grid: css("--line", "#2b3942"), bg: css("--inset", "#0d1317") });
  let last = null, lastStamp = null;

  async function getJson(name) {
    try {
      const r = await fetch(`${ROOT}/${name}`, { cache: "no-store" });
      return r.ok ? await r.json() : null;
    } catch (e) { return null; }
  }

  function setup(canvas) {
    const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth || 600, h = canvas.clientHeight || 220;
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h };
  }

  // shared axes: same frequency range and power range on both plots, so they line up column by column
  function axes(s, w, h) {
    const padL = 46, padR = 10, padT = 10, padB = 24;
    const f = s.frequency_hz, inner = s.inner;
    let lo = Infinity, hi = -Infinity;
    for (let i = 0; i < f.length; i++) if (inner[i]) { lo = Math.min(lo, s.raw_power_db[i], s.power_db[i]); hi = Math.max(hi, s.raw_power_db[i], s.power_db[i]); }
    lo = Math.floor(lo - 1); hi = Math.ceil(hi + 1);
    const f0 = f[0], f1 = f[f.length - 1];
    return { padL, padR, padT, padB, lo, hi, f0, f1,
      x: (hz) => padL + (hz - f0) / (f1 - f0) * (w - padL - padR),
      y: (db) => padT + (hi - db) / (hi - lo) * (h - padT - padB) };
  }

  function frame(ctx, a, w, h, c) {
    ctx.fillStyle = c.bg; ctx.fillRect(0, 0, w, h);
    ctx.font = "11px ui-monospace,monospace"; ctx.fillStyle = c.text; ctx.strokeStyle = c.grid; ctx.lineWidth = 1;
    const stepDb = Math.max(1, Math.round((a.hi - a.lo) / 5));
    ctx.textAlign = "right";
    for (let v = Math.ceil(a.lo / stepDb) * stepDb; v <= a.hi; v += stepDb) {
      ctx.beginPath(); ctx.moveTo(a.padL, a.y(v)); ctx.lineTo(w - a.padR, a.y(v)); ctx.stroke(); ctx.fillText(String(v), a.padL - 5, a.y(v) + 4);
    }
    ctx.textAlign = "center";
    const tick = w < 700 ? 4e5 : 2e5;
    for (let k = Math.ceil(a.f0 / tick); k * tick <= a.f1; k++) ctx.fillText((k * tick / 1e6).toFixed(1), a.x(k * tick), h - 7);
    ctx.textAlign = "left";
  }

  function trace(ctx, s, a, key, color, skipMasked) {
    ctx.strokeStyle = color; ctx.lineWidth = 1.2; ctx.beginPath();
    let pen = false;
    for (let i = 0; i < s.frequency_hz.length; i++) {
      const gap = !s.inner[i] || (skipMasked && s.code[i] !== 0);
      if (gap) { pen = false; continue; }                                       // masked: a gap, never a filled value
      const X = a.x(s.frequency_hz[i]), Y = a.y(s[key][i]);
      if (pen) ctx.lineTo(X, Y); else ctx.moveTo(X, Y);
      pen = true;
    }
    ctx.stroke();
  }

  function drawClean(d) {
    const s = d.spectrum, c = COLORS(), { ctx, w, h } = setup($("rfi-mask-clean"));
    const a = axes(s, w, h); frame(ctx, a, w, h, c);
    trace(ctx, s, a, "power_db", c.clean, true);
    ctx.fillStyle = c.text; ctx.fillText("dB (relative)", a.padL + 4, a.padT + 11);
  }

  function drawRemoved(d) {
    const s = d.spectrum, c = COLORS(), { ctx, w, h } = setup($("rfi-mask-removed"));
    const a = axes(s, w, h); frame(ctx, a, w, h, c);
    trace(ctx, s, a, "raw_power_db", c.raw, false);                             // the raw spectrum, every frame
    for (let i = 0; i < s.frequency_hz.length; i++) {                          // what the mask takes out, at its real height
      if (!s.inner[i] || s.code[i] === 0) continue;
      const X = a.x(s.frequency_hz[i]);
      ctx.strokeStyle = s.code[i] === 1 ? c.comb : c.line; ctx.lineWidth = 2;
      ctx.beginPath(); ctx.moveTo(X, a.y(a.lo)); ctx.lineTo(X, a.y(s.raw_power_db[i])); ctx.stroke();
    }
    ctx.font = "11px ui-monospace,monospace";
    ctx.fillStyle = c.raw; ctx.fillText("raw (all frames)", a.padL + 4, a.padT + 11);
    ctx.fillStyle = c.comb; ctx.fillText("comb", a.padL + 130, a.padT + 11);
    ctx.fillStyle = c.line; ctx.fillText("line", a.padL + 175, a.padT + 11);
  }

  function text(d, rows) {
    const pct = (v) => (100 * v).toFixed(2) + " %";
    const teeth = (d.teeth || []).filter((t) => t.masked);
    const lines = d.line_list || [];
    const tl = teeth.map((t) => `${(t.peak_hz / 1e6).toFixed(3)} (+${t.excess_db.toFixed(1)})`).join(" · ") || "none";
    const ll = lines.map((l) => `${(l.frequency_hz / 1e6).toFixed(4)} (+${l.excess_db.toFixed(1)}${l.known ? ", known" : ""})`).join(" · ") || "none";
    $("rfi-mask-summary").textContent =
      `point ${d.point_id} · ${rows} masked so far · gain ${d.gain_db} dB · ${d.capture_start_utc || ""}\n`
      + `channels masked ${pct(d.masked_channel_fraction)} (comb ${d.comb_channels}, lines ${d.line_channels}) · `
      + `time cut by pulses ${pct(d.pulse_fraction)} · ADC rails ${pct(d.adc_rail_hit_fraction || 0)}\n`
      + `comb teeth masked (MHz, dB over floor): ${tl}\n`
      + `lines masked: ${ll}`;
  }

  async function tick() {
    if (document.hidden) return;
    const [d, cur] = await Promise.all([getJson("main_rfi_mask_latest.json"), getJson("current_session.json")]);
    const panel = $("rfi-mask-panel");
    const live = d && cur && d.session_id && String(d.session_id) === String(cur.session_id);
    panel.hidden = !live;
    if (!live) { last = null; return; }
    const stamp = d.updated_utc + "|" + document.documentElement.getAttribute("data-theme");
    $("rfi-mask-badge").textContent = d.masked_channel_fraction > 0 || d.pulse_fraction > 0 ? "MASKING" : "CLEAN";
    if (stamp === lastStamp) return;
    last = d; lastStamp = stamp;
    const sess = await getJson("main_rfi_mask_session.json");                 // only when a new capture was masked
    drawClean(d); drawRemoved(d); text(d, sess && sess.rows ? sess.rows.length : "?");
  }

  window.addEventListener("resize", () => { if (last) { drawClean(last); drawRemoved(last); } });
  document.addEventListener("DOMContentLoaded", () => { tick(); setInterval(tick, POLL_MS); });
})();
