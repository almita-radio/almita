/* STATUS · ALMITA'S SKY (operator 2026-11-18: "el estatus final del proyecto, como primer valor").
   The whole sky in galactic Mollweide: HI4PI as a faint ghost, and ~5 deg tiles lit wherever ALMITA has measured.
   Data: GET /api/status/sky-map (site_sky_export.cached_payload: VALID sessions only - real mount + REDUCE +
   frequency switching), background GET /api/status/sky-ghost.png. Display only; nothing is written. */
(() => {
  "use strict";
  const U = window.AlmitaUI;
  const $ = (id) => document.getElementById(id);
  const canvas = $("sky-canvas");
  if (!canvas) return;
  const ctx = canvas.getContext("2d");
  const W = canvas.width, H = canvas.height, R2 = Math.SQRT2, RAD = Math.PI / 180;
  const CELL = 2.5;                                 // tile size, deg of galactic latitude (finer than any grid spacing)
  const REACH = 0.75;                               // a tile belongs to a pointing within 0.75 x its grid spacing (cell corner = 0.71)
  let D = null, ghost = null, tiles = [];
  let mode = "measured";                            // "measured" | "decon" (remembered per browser)
  let showTiles = true;                             // TILES ON/OFF: compare with the HI4PI ghost underneath
  try { if (localStorage.getItem("almita-sky-mode") === "decon") mode = "decon"; } catch (e) { /* private window */ }

  function project(l, b) {                          // l grows to the LEFT, l=0 in the centre
    const lam = -((((l + 180) % 360) + 360) % 360 - 180) * RAD, phi = b * RAD;
    let th = phi;
    for (let k = 0; k < 30; k++) {
      const d = 2 + 2 * Math.cos(2 * th);
      if (Math.abs(d) < 1e-9) break;
      const s = (2 * th + Math.sin(2 * th) - Math.PI * Math.sin(phi)) / d;
      th -= s; if (Math.abs(s) < 1e-9) break;
    }
    return [(2 * R2 / Math.PI * lam * Math.cos(th) + 2 * R2) / (4 * R2) * W, (R2 - R2 * Math.sin(th)) / (2 * R2) * H];
  }
  // signed longitude (-180..180] so a tile never straddles the map's edge
  const projS = (ls, b) => project(ls === -180 ? -179.9999 : ls === 180 ? 179.9999 : ls, b);
  // ICRS (J2000) -> galactic, the standard rotation (deg in, deg out)
  const GAL = [[-0.0548755604, -0.8734370902, -0.4838350155], [0.4941094279, -0.4448296300, 0.7469822445], [-0.8676661490, -0.1980763734, 0.4559837762]];
  function eqToGal(raDeg, decDeg) {
    const a = raDeg * RAD, d = decDeg * RAD, e = [Math.cos(d) * Math.cos(a), Math.cos(d) * Math.sin(a), Math.sin(d)];
    const g = GAL.map((r) => r[0] * e[0] + r[1] * e[1] + r[2] * e[2]);
    return [((Math.atan2(g[1], g[0]) / RAD) + 360) % 360, Math.asin(Math.max(-1, Math.min(1, g[2]))) / RAD];
  }
  const unit = (l, b) => [Math.cos(b * RAD) * Math.cos(l * RAD), Math.cos(b * RAD) * Math.sin(l * RAD), Math.sin(b * RAD)];
  // ALMITA's own reading, LINEAR, on a cool "moonlight" ramp that belongs with the HI4PI ghost behind it (operator
  // 2026-11-18: viridis was too disruptive - "algo más elegante"): deep blue -> steel -> ice -> near white, and the
  // tiles grow more opaque with the signal so faint sky melts into the background.
  const STOPS = [[0, [14, 28, 58]], [0.3, [34, 78, 138]], [0.6, [78, 150, 210]], [0.82, [150, 210, 240]], [1, [240, 250, 255]]];
  const tOf = (v) => { const [mn, mx] = D.tiles_scale_pct_kms; return Math.max(0, Math.min(1, (v - mn) / (mx - mn))); };
  const colour = (v) => {
    const t = tOf(v);
    for (let i = 1; i < STOPS.length; i++) {
      if (t <= STOPS[i][0]) {
        const [t0, a] = STOPS[i - 1], [t1, b] = STOPS[i], f = (t - t0) / (t1 - t0);
        return a.map((x, k) => Math.round(x + (b[k] - x) * f));
      }
    }
    return STOPS[STOPS.length - 1][1];
  };
  const alphaOf = (v) => 0.42 + 0.48 * tOf(v);

  // Tiles: an equal-area-ish lat/long board. A tile belongs to its NEAREST pointing when that pointing lies within
  // 0.75 x the pointing's own grid spacing (the cell the grid gave it, not the 20 deg beam), and shows that
  // pointing's own reading - no averaging between pointings, every tile is a real measurement.
  function buildTiles() {
    const pts = D.points.map((p) => ({ u: unit(p.l_disp ?? p.l, p.b_disp ?? p.b), v: p.integral_pct_kms, p,
      reach: Math.cos(REACH * (D.sessions[p.s].spacing_deg || D.beam_fwhm_deg / 2) * RAD) }));
    // the deconvolved model, per session: fine-grid points (1.25 deg) with their unit vectors
    const dec = D.sessions.map((s) => ((s.decon && s.decon.grid) || []).map(([l, b, v]) => ({ u: unit(l, b), v })));
    const decAt = (sid, c) => {
      let best = -2, v = null;
      for (const g of dec[sid] || []) { const dot = c[0] * g.u[0] + c[1] * g.u[1] + c[2] * g.u[2]; if (dot > best) { best = dot; v = g.v; } }
      return v;
    };
    tiles = [];
    let area = 0;
    for (let b0 = -90; b0 < 90; b0 += CELL) {
      const bc = b0 + CELL / 2, n = Math.max(1, Math.round(360 * Math.cos(bc * RAD) / CELL)), dl = 360 / n;
      for (let k = 0; k < n; k++) {
        const l0 = -180 + k * dl, lc = l0 + dl / 2, c = unit(lc, bc);
        let near = 0, best = -2, bq = null;
        for (const q of pts) {
          const dot = c[0] * q.u[0] + c[1] * q.u[1] + c[2] * q.u[2];
          if (dot > best) { best = dot; bq = q; }
          if (dot >= q.reach) near++;
        }
        if (!bq || best < bq.reach) continue;
        const vd = decAt(bq.p.s, c);
        tiles.push({ l0, dl, b0, lc: (lc + 360) % 360, bc, vm: bq.v, vd: vd === null ? bq.v : vd, near, p: bq.p });
        area += dl * RAD * (Math.sin((b0 + CELL) * RAD) - Math.sin(b0 * RAD));
      }
    }
    return area / (4 * Math.PI);
  }

  function tilePath(t) {
    ctx.beginPath();
    const steps = Math.max(2, Math.ceil(t.dl / 2.5)), pts = [];
    for (let i = 0; i <= steps; i++) pts.push(projS(t.l0 + t.dl * i / steps, t.b0));
    for (let i = 0; i <= 2; i++) pts.push(projS(t.l0 + t.dl, t.b0 + CELL * i / 2));
    for (let i = steps; i >= 0; i--) pts.push(projS(t.l0 + t.dl * i / steps, t.b0 + CELL));
    for (let i = 2; i >= 0; i--) pts.push(projS(t.l0, t.b0 + CELL * i / 2));
    pts.forEach(([x, y], i) => (i ? ctx.lineTo(x, y) : ctx.moveTo(x, y)));
    ctx.closePath();
  }

  function draw(hover) {
    ctx.clearRect(0, 0, W, H);
    ctx.save(); ctx.beginPath(); ctx.ellipse(W / 2, H / 2, W / 2 - 1, H / 2 - 1, 0, 0, 2 * Math.PI); ctx.fillStyle = "#05070a"; ctx.fill(); ctx.restore();
    if (ghost) {      // the ghost: HI4PI faint, grey-blue, so the tiles carry the eye
      ctx.save(); ctx.filter = showTiles ? "grayscale(100%) brightness(42%) contrast(115%)" : "grayscale(100%) brightness(80%) contrast(115%)";
      ctx.drawImage(ghost, 0, 0, W, H); ctx.restore();
      ctx.save(); ctx.globalCompositeOperation = "multiply"; ctx.fillStyle = "#7d9ad6";
      ctx.beginPath(); ctx.ellipse(W / 2, H / 2, W / 2, H / 2, 0, 0, 2 * Math.PI); ctx.fill(); ctx.restore();
    }
    ctx.save(); ctx.strokeStyle = "rgba(255,255,255,.08)"; ctx.lineWidth = 1.2;
    for (let l = -180; l <= 180; l += 30) { ctx.beginPath(); for (let b = -90; b <= 90; b += 2) { const [x, y] = projS(l, b); b === -90 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); } ctx.stroke(); }
    for (let b = -60; b <= 60; b += 30) { ctx.beginPath(); for (let l = -180; l <= 180; l += 2) { const [x, y] = projS(l, b); l === -180 ? ctx.moveTo(x, y) : ctx.lineTo(x, y); } ctx.stroke(); }
    ctx.restore();
    // celestial equator (Dec 0): northern sky on one side, southern on the other
    ctx.save(); ctx.strokeStyle = "rgba(127,216,255,.75)"; ctx.lineWidth = 2.5; ctx.setLineDash([14, 10]);
    ctx.beginPath(); let prev = null;
    for (let ra = 0; ra <= 360; ra += 1) {
      const [l, b] = eqToGal(ra, 0), xy = project(l, b);
      if (!prev || Math.abs(xy[0] - prev[0]) > W / 3) ctx.moveTo(xy[0], xy[1]); else ctx.lineTo(xy[0], xy[1]);
      prev = xy;
    }
    ctx.stroke(); ctx.setLineDash([]);
    ctx.font = "600 20px system-ui,sans-serif"; ctx.textAlign = "center"; ctx.fillStyle = "rgba(127,216,255,.9)"; ctx.shadowColor = "#000"; ctx.shadowBlur = 6;
    for (const [dec, t] of [[90, "N celestial pole"], [-90, "S celestial pole"]]) {
      const [l, b] = eqToGal(0, dec), [x, y] = project(l, b);
      ctx.beginPath(); ctx.arc(x, y, 5, 0, 7); ctx.fill();
      ctx.textAlign = "right"; ctx.fillText(t, x - 12, y + 7); ctx.textAlign = "center";
    }
    { const [l1, b1] = eqToGal(15, 0), [x, y] = project(l1, b1); ctx.fillText("celestial equator", x, y - 14); }
    for (const [ra, dec, t] of [[170, 55, "NORTHERN SKY"], [100, -50, "SOUTHERN SKY"]]) {
      const [l, b] = eqToGal(ra, dec), [x, y] = project(l, b); ctx.font = "700 18px system-ui,sans-serif"; ctx.fillText(t, x, y);
    }
    ctx.restore();
    // the tiles, with dark grout between them
    if (!showTiles) {         // only a faint outline of where the tiles are
      ctx.save(); ctx.strokeStyle = "rgba(200,225,255,.28)"; ctx.lineWidth = 1;
      for (const t of tiles) { tilePath(t); ctx.stroke(); }
      if (hover) { tilePath(hover); ctx.strokeStyle = "#fff"; ctx.lineWidth = 3; ctx.stroke(); }
      ctx.restore();
    }
    if (showTiles) {
    ctx.save(); ctx.shadowColor = "rgba(150,200,255,.22)"; ctx.shadowBlur = 10;
    for (const t of tiles) {
      const v = mode === "decon" ? t.vd : t.vm, c = colour(v);
      tilePath(t); ctx.fillStyle = `rgba(${c[0]},${c[1]},${c[2]},${alphaOf(v).toFixed(3)})`; ctx.fill();
    }
    ctx.restore();
    ctx.save(); ctx.strokeStyle = "rgba(5,9,16,.45)"; ctx.lineWidth = 1;
    for (const t of tiles) { tilePath(t); ctx.stroke(); }
    if (hover) { tilePath(hover); ctx.strokeStyle = "#fff"; ctx.lineWidth = 3; ctx.stroke(); }
    ctx.restore();
    }
    // a few landmarks
    ctx.save(); ctx.font = "600 22px system-ui,sans-serif"; ctx.textAlign = "center"; ctx.fillStyle = "rgba(255,255,255,.6)";
    ctx.shadowColor = "#000"; ctx.shadowBlur = 6;
    for (const [l, b, t, dy] of [[0, 0, "galactic centre", 44], [280.47, -32.89, "LMC", -14], [302.8, -44.3, "SMC", -14], [121.17, -21.57, "Andromeda", -14], [80, 1, "Cygnus", -14]]) {
      const [x, y] = project(l, b); ctx.fillText(t, x, y + dy); ctx.beginPath(); ctx.arc(x, y, 3, 0, 7); ctx.fill();
    }
    ctx.restore();
  }

  function stats(frac) {
    const nights = new Set(D.sessions.map((s) => s.started_chile.slice(0, 10)));
    const last = D.sessions.length ? D.sessions[D.sessions.length - 1].started_chile : null;
    $("sky-stats").innerHTML = [
      [nights.size, nights.size === 1 ? "night with valid data" : "nights with valid data"], [D.points.length, "pointings"], [(frac * 100).toFixed(1) + " %", "of the sky measured"],
      [D.r_vs_hi4pi == null ? "–" : D.r_vs_hi4pi.toFixed(2), "match with HI4PI (r, 50 pointings)".replace("50", D.points.length)], [last ? last.slice(0, 10) : "–", "last valid night"],
    ].map(([v, k]) => `<div class="sky-stat"><b>${v}</b><span>${k}</span></div>`).join("");
    $("sky-badge").textContent = (frac * 100).toFixed(1) + " % OF THE SKY";
    $("sky-badge").className = "badge " + (frac > 0 ? "status-ok" : "status-unknown");
    $("sky-note").textContent = `tile = ${CELL}° · each pointing fills its own grid cell (${D.sessions.map((s) => s.spacing_deg + "°").join(", ")} spacing) with its own reading · reading = area of the line over the window's edges, ±60 km/s, in ALMITA's own units (linear, moonlight ramp; fainter = more transparent) · `
      + `${D.sessions.length} sessions: ${D.selection} · background: HI4PI (Ben Bekhti et al. 2016)`
      + D.sessions.filter((s) => s.display_shift_deg).map((s) => ` · DISPLAY ONLY: ${s.started_chile} drawn moved ${s.display_shift_deg.db}° in b`
        + `${s.display_shift_deg.dl ? ` and ${s.display_shift_deg.dl}° in l` : ""} (dish not aligned that night; readings, r and the deconvolution use the true pointing)`).join("");
    const bar = $("sky-bar"), bx = bar.getContext("2d"), [mn, mx] = D.tiles_scale_pct_kms;
    for (let i = 0; i < bar.width; i++) { const c = colour(mn + i / (bar.width - 1) * (mx - mn)); bx.fillStyle = `rgb(${c})`; bx.fillRect(i, 0, 1, bar.height); }
    $("sky-lo").textContent = mn.toFixed(0); $("sky-hi").textContent = mx.toFixed(0) + " %·km/s";
  }

  // hover: which tile is under the mouse
  function tileAt(ev) {
    const r = canvas.getBoundingClientRect(), mx = (ev.clientX - r.left) * W / r.width, my = (ev.clientY - r.top) * H / r.height;
    for (const t of tiles) { tilePath(t); if (ctx.isPointInPath(mx, my)) return t; }
    return null;
  }
  let hovered = null;
  canvas.addEventListener("mousemove", (ev) => {
    if (!D) return;
    const t = tileAt(ev), tip = $("sky-tip");
    if (t !== hovered) { hovered = t; draw(t); }
    if (!t) { tip.hidden = true; return; }
    const r = canvas.getBoundingClientRect();
    const s = D.sessions[t.p.s];
    tip.innerHTML = `pointing ${t.p.i} · ${s.started_chile.slice(0, 10)} · l ${t.p.l.toFixed(1)}° b ${t.p.b.toFixed(1)}°`
      + `${s.display_shift_deg ? ` (drawn ${s.display_shift_deg.db}° in b)` : ""}<br>`
      + `measured <b>${t.vm.toFixed(0)}</b> · deconvolved <b>${t.vd.toFixed(0)}</b> %·km/s<br>`
      + `HI4PI (20° beam) ${(t.p.hi4pi_beam_1e20 / 100).toFixed(2)}×10²² cm⁻²`;
    tip.hidden = false; tip.style.left = (ev.clientX - r.left + 16) + "px"; tip.style.top = (ev.clientY - r.top + 10) + "px";
  });
  canvas.addEventListener("mouseleave", () => { hovered = null; $("sky-tip").hidden = true; if (D) draw(null); });

  function setMode(m) {
    mode = m;
    try { localStorage.setItem("almita-sky-mode", m); } catch (e) { /* private window */ }
    for (const b of document.querySelectorAll("#sky-mode button")) b.classList.toggle("active", b.dataset.mode === m);
    $("sky-mode-note").textContent = m === "decon"
      ? "DECONVOLVED: a model of the sky consistent with the readings and a 20° Gaussian beam (Richardson–Lucy; iterations per night chosen by leave-one-out: "
        + D.sessions.map((s) => `${s.started_chile.slice(5, 16)} → ${s.decon ? s.decon.iterations : 0}`).join(", ")
        + ") · expected ~11–15° instead of 20–24° where the signal is strong · NOT a measurement: the measured view is the evidence"
      : "MEASURED: each tile is a pointing's own reading";
    draw(hovered);
  }
  for (const b of document.querySelectorAll("#sky-mode button")) b.addEventListener("click", () => { if (D) setMode(b.dataset.mode); });
  for (const b of document.querySelectorAll("#sky-tiles button")) b.addEventListener("click", () => {
    showTiles = b.dataset.tiles === "on";
    for (const o of document.querySelectorAll("#sky-tiles button")) o.classList.toggle("active", o === b);
    if (D) draw(hovered);
  });

  async function load() {
    const r = await U.api("/api/status/sky-map", { timeoutMs: 60000 });
    const body = r && r.data;
    if (!r || !r.ok || !body || !body.ok) {
      $("sky-stats").textContent = "sky map unavailable: " + ((body && body.error) || (r && r.error) || "no answer");
      return;
    }
    D = body.data;
    const frac = buildTiles();
    stats(frac); setMode(mode);
    const im = new Image();
    im.onload = () => { ghost = im; draw(hovered); };
    im.src = "/api/status/sky-ghost.png?kind=full";
  }
  load();
})();
