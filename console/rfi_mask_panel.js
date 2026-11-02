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
  let last = null, lastStamp = null, stackMode = 0, stackBtn = null;
  // RAW (quicklook, default) -> CLEAN (masked, pulse-free) -> DIFF (clean minus the session median per bin)
  const STACK_MODES = [{ name: "RAW", url: null },
    { name: "CLEAN", url: `${ROOT}/main_rfi_mask_session_waterfall_state.json` },
    { name: "DIFF", url: `${ROOT}/main_rfi_mask_session_waterfall_diff.json` }];

  // RAW / CLEAN switch on SPECTRAL STACK 3D: shown only while clean rows exist for the current session; default RAW
  function stackButton(show) {
    const stack = window.SpectralStack3D;
    if (!stack || typeof stack.setDataUrl !== "function") return;
    if (!stackBtn) {
      const header = document.querySelector("#spectral-stack-panel > header");
      if (!header) return;
      stackBtn = document.createElement("button");
      stackBtn.type = "button"; stackBtn.className = "spectral-stack-reset"; stackBtn.style.position = "static";
      stackBtn.addEventListener("click", () => {
        stackMode = (stackMode + 1) % STACK_MODES.length;
        stack.setDataUrl(STACK_MODES[stackMode].url);
        label();
        upStamp = null;
        const cur = last && last.session_id;
        if (cur) drawUnknownPleasures(cur);
      });
      header.insertBefore(stackBtn, header.lastElementChild);
    }
    const label = () => { stackBtn.textContent = `${STACK_MODES[stackMode].name} · show ${STACK_MODES[(stackMode + 1) % STACK_MODES.length].name}`; };
    label();
    stackBtn.hidden = !show;
    if (!show && stackMode) { stackMode = 0; stack.setDataUrl(null); label(); }
  }

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

  // UNKNOWN PLEASURES (operator 2026-10-30): the stacked spectra drawn like CP 1919's pulses on Joy Division's cover -
  // white lines on black, each point's line hiding the ones behind it. Masked/empty bins break the line (nothing filled).
  // At most UP_LINES lines (operator 2026-11-02: "tiene 80 líneas"); past that only the latest UP_LINES are drawn.
  // Fewer lines are spread over the whole height ("espaciadas pero sin disparar los gráficos"): the wiggles are scaled
  // from the UP_LINES spacing and may grow at most UP_MAX_GROW times, however far apart the lines are.
  const UP_LINES = 80, UP_MAX_GROW = 3;
  let upStamp = null;
  async function drawUnknownPleasures(sessionId) {
    const mode = STACK_MODES[2];                                                // always DIFF: the view that looks like the cover
    const url = mode.url || `${ROOT}/quicklook_products/session_waterfall_state.json`;
    let doc = null;
    try { const r = await fetch(url, { cache: "no-store" }); doc = r.ok ? await r.json() : null; } catch (e) { doc = null; }
    if (!doc || String(doc.session_id) !== String(sessionId) || !Array.isArray(doc.rows) || !doc.rows.length) return;
    const canvas = $("rfi-mask-up"), box = canvas.getBoundingClientRect();
    if (box.width < 50 || box.height < 50) return;                              // not laid out yet: draw on the next tick
    // drawn at least 3x the screen pixels (whatever the screen) and shrunk by the browser: smooth lines, sharp PNG
    const scale = Math.min(Math.max(3, 2 * (window.devicePixelRatio || 1)), 4000 / box.width);
    const stamp = `${mode.name}|${doc.updated_utc}|${doc.rows.length}|${Math.round(box.width)}x${Math.round(box.height)}|${scale}`;
    if (stamp === upStamp) return;
    upStamp = stamp;
    const rows = doc.rows.slice().sort((a, b) => parseInt(a.point_id, 10) - parseInt(b.point_id, 10)).slice(-UP_LINES);
    const w = box.width, h = box.height;
    canvas.width = Math.round(w * scale); canvas.height = Math.round(h * scale);
    const ctx = canvas.getContext("2d"); ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = "high";
    ctx.fillStyle = "#000"; ctx.fillRect(0, 0, w, h);
    const n = rows[0].relative_db.length;
    // ART VIEW, not a measurement: each line around its own median, smoothed (gaussian, SIGMA bins) (empty bins stay empty),
    // and tapered hard towards the band edges: signal in the middle, flat margins on both sides as on the cover
    const SIGMA = 2, RADIUS = 6;                                                // gaussian smoothing, in bins
    const centered = rows.map((r) => {
      const v = r.relative_db.filter((x) => x !== null && Number.isFinite(x)).sort((a, b) => a - b);
      const med = v.length ? v[Math.floor(v.length / 2)] : 0;
      const c = r.relative_db.map((x) => (x === null || !Number.isFinite(x) ? null : x - med));
      return c.map((x, i) => {
        const u = (i - (c.length - 1) / 2) / (c.length * 0.20);
        const wgt = 0.03 + 0.97 * Math.exp(-(u ** 6));                       // centre ~half alive, outer ~quarter each side flat
        if (x === null) return wgt < 0.05 ? 0 : null;                          // flat edges stay one unbroken line
        let s = 0, k = 0;
        for (let j = Math.max(0, i - RADIUS); j <= Math.min(c.length - 1, i + RADIUS); j++) {
          if (c[j] === null) continue;
          const g = Math.exp(-0.5 * ((j - i) / SIGMA) ** 2); s += c[j] * g; k += g;
        }
        return (s / k) * wgt;
      });
    });
    const all = []; centered.forEach((r) => r.forEach((x, i) => { if (x !== null && Math.abs(i - (n - 1) / 2) < n * 0.2) all.push(Math.abs(x)); }));   // scale from the live middle only
    all.sort((a, b) => a - b);
    const at = (q) => (all.length ? all[Math.floor(q * (all.length - 1))] : 0) || 1e-3;
    const med = at(0.5), p999 = at(0.999);
    const top = h * 0.16, bottom = h * 0.94, left = w * 0.18, right = w * 0.82;
    const step = (bottom - top) / Math.max(rows.length - 1, 1);              // spacing: the lines fill the height
    const ampStep = Math.min(step, UP_MAX_GROW * (bottom - top) / (UP_LINES - 1));   // wiggle size: capped
    const amp = Math.min(ampStep * 0.5 / med, ampStep * 6 / p999);                  // noise a tremor, the strongest ~5 lines
    const X = (i) => left + (right - left) * i / (n - 1);
    ctx.lineWidth = Math.max(1, w / 450); ctx.lineJoin = "round"; ctx.lineCap = "round";
    // a smooth curve through the points (quadratic through the midpoints), not straight segments with corners
    const trace = (pts, start) => {
      if (start) ctx.moveTo(pts[0][0], pts[0][1]); else ctx.lineTo(pts[0][0], pts[0][1]);
      for (let i = 1; i < pts.length - 1; i++)
        ctx.quadraticCurveTo(pts[i][0], pts[i][1], (pts[i][0] + pts[i + 1][0]) / 2, (pts[i][1] + pts[i + 1][1]) / 2);
      if (pts.length > 1) ctx.lineTo(pts[pts.length - 1][0], pts[pts.length - 1][1]);
    };
    centered.forEach((r, k) => {                                                // back (top) first, front (bottom) last
      const base = top + k * step;
      const floor = ampStep * 0.4;                                                 // dips compressed softly, never cut flat
      const Y = (x) => { const y = x * amp; return base - (y >= 0 ? y : -floor * Math.tanh(-y / floor)); };
      ctx.beginPath();                                                          // the black body that hides the lines behind
      trace(r.map((x, i) => [X(i), x === null ? base : Y(x)]), true);
      ctx.lineTo(X(n - 1), h); ctx.lineTo(X(0), h); ctx.closePath();
      ctx.fillStyle = "#000"; ctx.fill();
      ctx.strokeStyle = "#fff"; ctx.beginPath();
      let run = [];
      for (let i = 0; i <= n; i++) {                                            // each unbroken run of bins is one curve
        if (i < n && r[i] !== null) { run.push([X(i), Y(r[i])]); continue; }
        if (run.length) trace(run, true);
        run = [];
      }
      ctx.stroke();
    });
    ctx.fillStyle = "#fff"; ctx.font = `${Math.round(w / 40)}px ui-monospace,monospace`; ctx.textAlign = "center";
    ctx.fillText("ALMITA", w / 2, h * 0.07);
    ctx.font = `${Math.round(w / 60)}px ui-monospace,monospace`;
    ctx.fillText(`art view · ${mode.name} · session ${sessionId} · ${rows.length}/${UP_LINES} lines · ${Array.isArray(doc.frequency_mhz) && doc.frequency_mhz.length ? `${doc.frequency_mhz[0].toFixed(1)}-${doc.frequency_mhz[doc.frequency_mhz.length - 1].toFixed(1)} MHz` : ""}`, w / 2, h * 0.11);
    try { $("rfi-mask-up-png").href = canvas.toDataURL("image/png"); } catch (e) { /* tainted canvas: no link */ }
  }

  async function tick() {
    if (document.hidden) return;
    const [d, cur] = await Promise.all([getJson("main_rfi_mask_latest.json"), getJson("current_session.json")]);
    const panel = $("rfi-mask-panel");
    const live = d && cur && d.session_id && String(d.session_id) === String(cur.session_id);
    panel.hidden = !live;
    stackButton(!!live);
    if (!live) { last = null; upStamp = null; return; }
    drawUnknownPleasures(d.session_id);
    const stamp = d.updated_utc + "|" + document.documentElement.getAttribute("data-theme");
    $("rfi-mask-badge").textContent = d.masked_channel_fraction > 0 || d.pulse_fraction > 0 ? "MASKING" : "CLEAN";
    if (stamp === lastStamp) return;
    last = d; lastStamp = stamp;
    const sess = await getJson("main_rfi_mask_session.json");                 // only when a new capture was masked
    drawClean(d); drawRemoved(d); text(d, sess && sess.rows ? sess.rows.length : "?");
  }

  window.addEventListener("resize", () => { if (last) { drawClean(last); drawRemoved(last); drawUnknownPleasures(last.session_id); } });
  document.addEventListener("DOMContentLoaded", () => { tick(); setInterval(tick, POLL_MS); });
})();
