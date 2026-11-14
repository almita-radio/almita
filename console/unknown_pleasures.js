// UNKNOWN PLEASURES (operator 2026-10-30): the stacked spectra of a session drawn like CP 1919's pulses on Joy Division's
// cover - white lines on black, each point's line hiding the ones behind it. Masked/empty bins break the line (nothing
// filled). ART VIEW, not a measurement. Shared by MONITOR (the live session) and SESSIONS (any stored session,
// operator 2026-11-13: "quiero que unknown pleasures se pueda ver en sessions/captures").
//
// AlmitaUnknownPleasures.draw(canvas, doc, opts) draws doc = {rows: [{point_id, relative_db: [..]}], frequency_mhz,
// session_id} and returns the PNG data URL (or null when nothing was drawn).
//   opts.label      text after "art view ·" (MONITOR passes its stack mode)
//   opts.diff       subtract each bin's median over the rows first (the MONITOR DIFF view, computed here for stored
//                   sessions that only keep the raw rows)
//   opts.pick       "latest" (MONITOR: the last maxLines points) or "spread" (SESSIONS: maxLines points spread over
//                   the whole session, so a 400-point run is shown from start to end)
//   opts.maxLines   default 80 (operator 2026-11-02: "tiene 80 líneas")
(function () {
  "use strict";
  const UP_LINES = 80, UP_MAX_GROW = 3;
  const SIGMA = 2, RADIUS = 6;                                                  // gaussian smoothing, in bins

  function pickRows(rows, maxLines, pick) {
    const sorted = rows.slice().sort((a, b) => parseInt(a.point_id, 10) - parseInt(b.point_id, 10));
    if (sorted.length <= maxLines) return sorted;
    if (pick !== "spread") return sorted.slice(-maxLines);
    const out = [];
    for (let k = 0; k < maxLines; k++) out.push(sorted[Math.round(k * (sorted.length - 1) / (maxLines - 1))]);
    return out;
  }

  function binMedians(rows, n) {
    const med = new Array(n).fill(null);
    for (let i = 0; i < n; i++) {
      const v = rows.map((r) => r.relative_db[i]).filter((x) => x !== null && Number.isFinite(x)).sort((a, b) => a - b);
      if (v.length) med[i] = v[Math.floor(v.length / 2)];
    }
    return med;
  }

  function draw(canvas, doc, opts) {
    opts = opts || {};
    const maxLines = opts.maxLines || UP_LINES;
    if (!canvas || !doc || !Array.isArray(doc.rows) || !doc.rows.length) return null;
    // opts.pixels: a fixed square size in real pixels, for print (an offscreen canvas, no layout needed); otherwise
    // drawn at least 3x the screen pixels (whatever the screen) and shrunk by the browser: smooth lines, sharp PNG
    const box = opts.pixels ? { width: 1200, height: 1200 } : canvas.getBoundingClientRect();
    if (box.width < 50 || box.height < 50) return null;                        // not laid out yet
    const scale = opts.pixels ? opts.pixels / box.width : Math.min(Math.max(3, 2 * (window.devicePixelRatio || 1)), 4000 / box.width);
    // opts.transparent: no black background (a print on a black shirt); the lines behind are still hidden, by erasing
    const clear = !!opts.transparent;
    let rows = pickRows(doc.rows.filter((r) => Array.isArray(r.relative_db) && r.relative_db.length), maxLines, opts.pick);
    if (!rows.length) return null;
    const n = rows[0].relative_db.length;
    if (opts.diff) {
      const med = binMedians(rows, n);
      rows = rows.map((r) => ({ point_id: r.point_id, relative_db: r.relative_db.map((x, i) =>
        (x === null || !Number.isFinite(x) || med[i] === null ? null : x - med[i])) }));
    }
    const w = box.width, h = box.height;
    canvas.width = Math.round(w * scale); canvas.height = Math.round(h * scale);
    const ctx = canvas.getContext("2d"); ctx.setTransform(scale, 0, 0, scale, 0, 0);
    ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = "high";
    if (!clear) { ctx.fillStyle = "#000"; ctx.fillRect(0, 0, w, h); } else ctx.clearRect(0, 0, w, h);
    // each line around its own median, smoothed (empty bins stay empty), and tapered hard towards the band edges:
    // signal in the middle, flat margins on both sides as on the cover
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
    let top = h * 0.16;
    const bottom = h * 0.94, left = w * 0.18, right = w * 0.82;
    let step = (bottom - top) / Math.max(rows.length - 1, 1);                // spacing: the lines fill the height
    const ampStep = Math.min(step, UP_MAX_GROW * (bottom - top) / (UP_LINES - 1));   // wiggle size: capped
    const amp = Math.min(ampStep * 0.5 / med, ampStep * 6 / p999);                  // noise a tremor, the strongest ~5 lines
    // the back line's peaks never reach the title: with few lines (big wiggles) the stack starts a little lower
    const up0 = Math.max(0, ...centered[0].map((x) => (x === null ? 0 : x * amp)));
    if (top - up0 < h * 0.135) { top = Math.min(h * 0.135 + up0, bottom - h * 0.3); step = (bottom - top) / Math.max(rows.length - 1, 1); }
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
      if (clear) { ctx.globalCompositeOperation = "destination-out"; ctx.fillStyle = "#000"; ctx.fill(); ctx.globalCompositeOperation = "source-over"; }
      else { ctx.fillStyle = "#000"; ctx.fill(); }
      ctx.strokeStyle = "#fff"; ctx.beginPath();
      let run = [];
      for (let i = 0; i <= n; i++) {                                            // each unbroken run of bins is one curve
        if (i < n && r[i] !== null) { run.push([X(i), Y(r[i])]); continue; }
        if (run.length) trace(run, true);
        run = [];
      }
      ctx.stroke();
    });
    const fr = Array.isArray(doc.frequency_mhz) && doc.frequency_mhz.length
      ? `${doc.frequency_mhz[0].toFixed(1)}-${doc.frequency_mhz[doc.frequency_mhz.length - 1].toFixed(1)} MHz` : "";
    ctx.fillStyle = "#fff"; ctx.font = `${Math.round(w / 40)}px ui-monospace,monospace`; ctx.textAlign = "center";
    ctx.fillText("ALMITA", w / 2, h * 0.07);
    ctx.font = `${Math.round(w / 60)}px ui-monospace,monospace`;
    ctx.fillText(`art view · ${opts.label || "DIFF"} · session ${doc.session_id || "?"} · ${rows.length}/${doc.rows.length} lines · ${fr}`, w / 2, h * 0.11);
    try { return canvas.toDataURL("image/png"); } catch (e) { return null; }  // tainted canvas: no PNG
  }

  window.AlmitaUnknownPleasures = { draw, UP_LINES };
})();
