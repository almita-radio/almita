// ALMITA LOBE — the beam from a solar transit. Every number comes from the backend (lobe_transit.py through
// almita_web_ops.lobe_status / lobe_samples): this file shows the Sun and the mount, the solar ALIGN verdict and the
// plan, starts the real job (typed MOVE) and draws the transit live: intensity on x, time on y (downwards).
(function () {
  "use strict";
  const U = window.AlmitaUI;
  const $ = (id) => document.getElementById(id);
  U.mountHealthStrip(U.mountHeader("LOBE", "BEAM FROM A SOLAR TRANSIT"));
  U.mountFooter();
  const showError = (m) => U.showError($("error-banner"), m);

  let mode = "drift";
  let status = null;
  let liveDir = null;
  let defaultsLoaded = false;
  const chile = (iso) => { if (!iso) return "—"; const d = new Date(Date.parse(iso) - 3 * 3600e3); return d.toISOString().slice(11, 16) + " Chile"; };
  const whenText = (iso) => `${U.utc(iso)} (${chile(iso)})`;
  const kv = (dl, rows) => { dl.textContent = ""; for (const [k, v] of rows) { const dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = k; dd.textContent = v; dl.append(dt, dd); } };

  function params() {
    const n = (id) => $(id).value === "" ? undefined : Number($(id).value);
    return { mode, expected_fwhm: n("p-fwhm"), half_range_fwhm: n("p-half"), every: n("p-every"), capture_seconds: n("p-capture"),
             scan_step: n("p-step"), gain: n("p-gain"), min_elevation: n("p-minel") };
  }
  function query(p) { return Object.entries(p).filter(([, v]) => v !== undefined && !Number.isNaN(v)).map(([k, v]) => `${k}=${encodeURIComponent(v)}`).join("&"); }

  function setMode(m) {
    mode = m;
    $("mode-drift").classList.toggle("active", m === "drift");
    $("mode-scan").classList.toggle("active", m === "scan");
    $("row-step").hidden = m !== "scan";
    $("mode-note").textContent = m === "drift"
      ? "DRIFT: the mount points west of the Sun by the half range, tracking goes OFF and the Earth's rotation carries the Sun through the beam and out the other side. Nothing moves while it measures."
      : "SCAN: SOLAR tracking on, the mount steps in RA from -half range to +half range across the Sun (one capture per position).";
    refreshStatus();
  }

  async function refreshStatus() {
    const r = await U.api(`/api/ops/lobe/status?${query(params())}`, { timeoutMs: 20000 });
    if (!r.ok) { showError(r.error); return false; }
    showError(null);
    status = r.data.data;
    if (!defaultsLoaded) {
      const q = status.params;
      $("p-fwhm").value = q.expected_fwhm; $("p-half").value = q.half_range_fwhm; $("p-every").value = q.every;
      $("p-capture").value = q.capture_seconds; $("p-step").value = q.scan_step; $("p-gain").value = q.gain; $("p-minel").value = q.min_elevation;
      defaultsLoaded = true;
    }
    renderSky(); renderAlign(); renderPlan(); updateStart();
    const j = status.latest_job;
    if (j && (j.state === "RUNNING" || !liveDir)) {
      if (j.state === "RUNNING" && job.jobId !== j.job_id) job.attach(j.job_id);
    }
    return true;
  }

  function renderSky() {
    const s = status.sun, m = status.mount || {}, fwhm = status.params.expected_fwhm;
    const areas = [];
    if (U.finite(m.alt_deg)) areas.push({ label: "MOUNT", az_deg: m.az_deg, alt_deg: m.alt_deg, radius_deg: fwhm / 2 });
    U.drawSkyView($("lobe-sky"), { sun: s, areas }, { colors: { MOUNT: "#65b7d8" } });
    U.setBadge($("sun-badge"), s.above_horizon ? "SUN UP" : "SUN DOWN");
    kv($("sky-kv"), [
      ["SUN", `az ${s.az_deg.toFixed(1)}° alt ${s.alt_deg.toFixed(1)}° · RA ${s.ra_hours.toFixed(4)} h Dec ${s.dec_deg.toFixed(3)}°`],
      ["MOUNT", m.error ? m.error : (U.finite(m.alt_deg) ? `az ${m.az_deg.toFixed(1)}° alt ${m.alt_deg.toFixed(1)}° · RA ${U.raHours(m.ra_h)} Dec ${U.decDeg(m.dec_deg, 3)}` : "—")],
      ["MOUNT → SUN", U.finite(m.sun_separation_deg) ? `${m.sun_separation_deg.toFixed(2)}°` : "—"],
      ["TRACKING", String(m.tracking ?? "—").toUpperCase()],
      ["COMPUTED", whenText(status.computed_utc)],
    ]);
  }

  function renderAlign() {
    const a = status.alignment;
    U.setBadge($("align-badge"), a.ok ? "ALIGNED" : "NOT ALIGNED");
    kv($("align-kv"), [
      ["VERDICT", a.verdict],
      ["RUN", a.dir || "—"],
      ["WHEN", a.timestamp_utc ? `${whenText(a.timestamp_utc)} · ${U.finite(a.age_h) ? a.age_h.toFixed(1) + " h ago" : ""}` : "—"],
      ["OFFSET", U.finite(a.separation_deg) ? `${a.separation_deg.toFixed(2)}° (RA ${U.fixed(a.offset_ra_deg, 2)}°, Dec ${U.fixed(a.offset_dec_deg, 2)}°) · confidence ${U.fixed(a.confidence, 2)}` : "—"],
      ["SYNC", a.result_status ? (a.sync_applied ? "applied" : "NOT applied: the mount still carries that offset") : "—"],
    ]);
    $("align-note").textContent = a.detail || "";
  }

  function renderPlan() {
    const p = status.plan;
    const lines = [`${p.mode.toUpperCase()} · half range ${p.half_range_deg.toFixed(1)}° (= ${p.expected_fwhm_deg}° × ${p.half_range_fwhm}) · ` +
                   `duration ${U.hms(p.duration_s)} · ~${p.samples_expected} samples`];
    lines.push(`pointing: RA ${p.point_ra_hours.toFixed(4)} h Dec ${p.point_dec_deg.toFixed(3)}° · alt ${p.point_alt_deg.toFixed(1)}° az ${p.point_az_deg.toFixed(1)}°`);
    if (p.mode === "drift") lines.push(`if started now: Sun crosses ${whenText(p.crossing_utc)} at alt ${p.sun_alt_at_crossing_deg.toFixed(1)}° · ends ${whenText(p.end_utc)}`);
    else lines.push(`if started now: ends ${whenText(p.end_utc)}`);
    for (const q of p.problems || []) lines.push(`BLOCKED: ${q}`);
    $("plan-out").textContent = lines.join("\n");
    U.setBadge($("plan-badge"), (p.problems || []).length ? "BLOCKED" : "READY");
  }

  function updateStart() {
    const running = status && status.latest_job && status.latest_job.state === "RUNNING";
    const okAlign = status && status.alignment.ok;
    const okPlan = status && !(status.plan.problems || []).length;
    const typed = $("p-confirm").value === "MOVE";
    U.setEnabled($("btn-start"), !!(okAlign && okPlan && typed && !running),
      running ? "a LOBE transit is already running" : !okAlign ? "needs a valid real solar ALIGN (PASS, last 12 h)" :
      !okPlan ? "the plan is blocked (see above)" : !typed ? "type MOVE to allow the mount to slew" : "");
  }

  const job = U.jobPanel($("lobe-job"), $("live-badge"), {
    onError: showError, stopLabel: "STOP TRANSIT",
    onChange: (j) => { if (j.output_dir) liveDir = j.output_dir; updateStart(); },
    onEnd: () => { refreshStatus(); refreshLive(); },
  });

  // ------------------------------------------------------------------ live plot: intensity (x) vs time (y, downwards)
  function drawPlot(data) {
    const canvas = $("lobe-plot"), ctx = canvas.getContext("2d");
    const w = canvas.clientWidth || 600, h = canvas.clientHeight || 520, dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, w, h);
    const fg = "#e6edf1", muted = "rgba(145,162,173,0.75)", line = "#2b3942";
    const rows = (data.samples || []).filter((r) => U.finite(r.elapsed_s) && U.finite(r.power_lin));
    ctx.font = "11px ui-monospace,monospace"; ctx.fillStyle = muted;
    if (!rows.length) { ctx.fillText("waiting for samples…", 20, 30); return; }
    const L = 92, R = 16, T = 26, B = 34;
    const plan = data.plan || {};
    const tMax = Math.max(rows[rows.length - 1].elapsed_s, plan.duration_s || 0, 60);
    const ps = rows.map((r) => r.power_lin);
    let pMin = Math.min(...ps), pMax = Math.max(...ps);
    if (pMax - pMin < 1e-12) { pMax = pMin + 1; }
    const pad = (pMax - pMin) * 0.06; pMin -= pad; pMax += pad;
    const X = (p) => L + (w - L - R) * (p - pMin) / (pMax - pMin);
    const Y = (t) => T + (h - T - B) * t / tMax;
    ctx.strokeStyle = line; ctx.lineWidth = 1; ctx.strokeRect(L, T, w - L - R, h - T - B);
    // time axis (minutes) with the Sun's offset from the beam at that time, when known
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    const stepMin = tMax > 7200 ? 20 : tMax > 2400 ? 10 : tMax > 600 ? 5 : 1;
    for (let m = 0; m * 60 <= tMax; m += stepMin) {
      const y = Y(m * 60);
      ctx.strokeStyle = "rgba(43,57,66,0.6)"; ctx.beginPath(); ctx.moveTo(L, y); ctx.lineTo(w - R, y); ctx.stroke();
      const near = rows.reduce((b, r) => (Math.abs(r.elapsed_s - m * 60) < Math.abs(b.elapsed_s - m * 60) ? r : b), rows[0]);
      const off = Math.abs(near.elapsed_s - m * 60) < 30 && U.finite(near.offset_deg) ? ` ${near.offset_deg >= 0 ? "+" : ""}${near.offset_deg.toFixed(1)}°` : "";
      ctx.fillStyle = muted; ctx.fillText(`${m} min${off}`, L - 6, y);
    }
    ctx.textAlign = "center"; ctx.textBaseline = "top";
    ctx.fillText(`band power (linear, arb.)  ${pMin.toExponential(2)} … ${pMax.toExponential(2)}`, L + (w - L - R) / 2, h - B + 8);
    ctx.textBaseline = "bottom"; ctx.fillText("time ↓ (offset of the Sun from the beam)", L + (w - L - R) / 2, T - 6);
    // fit (background + Gaussian) evaluated at each sample's offset, drawn along time
    const f = data.fit;
    if (f && U.finite(f.fwhm_deg)) {
      const model = (x) => f.background_b0 + f.background_slope_per_deg * x + f.amplitude_lin * Math.exp(-4 * Math.LN2 * (x - f.x0_deg) ** 2 / f.fwhm_deg ** 2);
      const bg = (x) => f.background_b0 + f.background_slope_per_deg * x;
      for (const [fn, col, dash] of [[bg, "rgba(145,162,173,0.8)", [4, 4]], [model, "#e6b85c", []]]) {
        ctx.strokeStyle = col; ctx.lineWidth = 2; ctx.setLineDash(dash); ctx.beginPath();
        rows.filter((r) => U.finite(r.offset_deg)).forEach((r, i) => { const x = X(fn(r.offset_deg)), y = Y(r.elapsed_s); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); });
        ctx.stroke(); ctx.setLineDash([]);
      }
    }
    for (const r of rows) {
      ctx.fillStyle = r.status === "VALID" ? "#65b7d8" : "#e36b6b";
      ctx.beginPath(); ctx.arc(X(r.power_lin), Y(r.elapsed_s), r.status === "VALID" ? 2 : 3.5, 0, 2 * Math.PI); ctx.fill();
    }
  }

  function renderFit(data) {
    const rows = data.samples || [], f = data.fit, res = data.result;
    const clipped = rows.filter((r) => r.status === "CLIPPED").length, failed = rows.filter((r) => r.status === "CAPTURE_FAILED").length;
    const last = rows[rows.length - 1];
    const lines = [`${data.dir} · ${rows.length} samples (${clipped} clipped, ${failed} failed)` + (res ? ` · RESULT ${res.status}` : " · running")];
    if (last) lines.push(`last: ${whenText(last.utc)} · Sun offset ${U.fixed(last.offset_deg, 2, "°")} · P ${U.fixed(last.power_db, 3, "dB")} · clip ${U.fixed(100 * (last.clipping_fraction || 0), 3, "%")} · SDR ${U.fixed(last.t_sdr_c, 1, "°C")} LNA ${U.fixed(last.t_lna_c, 1, "°C")} · tracking ${last.tracking}`);
    if (f && U.finite(f.fwhm_deg)) {
      lines.push(`FIT (${f.status}, ${f.n} samples, linear power, background subtracted): FWHM ${f.fwhm_deg.toFixed(2)}° · peak at ${f.x0_deg >= 0 ? "+" : ""}${f.x0_deg.toFixed(2)}° · ` +
                 `peak/background ${U.fixed(f.peak_over_background, 4)} · SNR ${U.fixed(f.snr, 1)}`);
      for (const w of f.flags || []) lines.push(`  ! ${w}`);
    } else if (f) lines.push(`FIT: ${f.status}`);
    $("fit-out").textContent = lines.join("\n");
  }

  // ------------------------------------------------------------------ newest capture's spectrum, strongest narrow peak marked
  let specMode = "power", lastData = null;
  function drawSpectrum(data) {
    const s = data && data.last_spectrum, canvas = $("lobe-spectrum"), ctx = canvas.getContext("2d");
    const w = canvas.clientWidth || 600, h = canvas.clientHeight || 320, dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(w * dpr) || canvas.height !== Math.round(h * dpr)) { canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    const muted = "rgba(145,162,173,0.75)";
    ctx.font = "11px ui-monospace,monospace"; ctx.fillStyle = muted;
    if (!s) { ctx.fillText("no spectrum yet (runs before this version did not keep one)", 20, 30); $("spec-out").textContent = "No spectrum yet."; return; }
    const ys = specMode === "power" ? s.db : s.excess_db, xs = s.freq_mhz, use = s.usable;
    const L = 56, R = 12, T = 14, B = 30;
    const vals = ys.filter((v, i) => use[i]);
    let lo = Math.min(...vals), hi = Math.max(...vals); const pad = (hi - lo) * 0.08 || 0.1; lo -= pad; hi += pad;
    const X = (f) => L + (w - L - R) * (f - xs[0]) / (xs[xs.length - 1] - xs[0]);
    const Y = (v) => T + (h - T - B) * (1 - (Math.min(hi, Math.max(lo, v)) - lo) / (hi - lo));
    ctx.fillStyle = "rgba(43,57,66,0.35)";                                   // DC and band edges: outside the usable band
    for (let i = 0; i < xs.length; i++) if (!use[i]) ctx.fillRect(X(xs[i]) - 1, T, 2.5, h - T - B);
    ctx.strokeStyle = "#2b3942"; ctx.strokeRect(L, T, w - L - R, h - T - B);
    ctx.textAlign = "center"; ctx.textBaseline = "top"; ctx.fillStyle = muted;
    for (let f = Math.ceil(xs[0] * 4) / 4; f <= xs[xs.length - 1]; f += 0.25) { ctx.fillText(f.toFixed(2), X(f), h - B + 6); }
    ctx.textAlign = "right"; ctx.textBaseline = "middle";
    for (let k = 0; k <= 4; k++) { const v = lo + (hi - lo) * k / 4; ctx.fillText(v.toFixed(2), L - 4, Y(v)); }
    const hiLine = 1420.405752;                                             // HI rest frequency
    ctx.strokeStyle = "rgba(101,183,216,0.5)"; ctx.setLineDash([3, 4]); ctx.beginPath(); ctx.moveTo(X(hiLine), T); ctx.lineTo(X(hiLine), h - B); ctx.stroke(); ctx.setLineDash([]);
    ctx.strokeStyle = "#e6edf1"; ctx.lineWidth = 1; ctx.beginPath();
    xs.forEach((f, i) => { const x = X(f), y = Y(ys[i]); if (i) ctx.lineTo(x, y); else ctx.moveTo(x, y); }); ctx.stroke();
    const pk = s.peak, py = Y(ys[xs.indexOf(pk.freq_mhz)] ?? hi);
    ctx.strokeStyle = "#e6b85c"; ctx.lineWidth = 2; ctx.beginPath(); ctx.arc(X(pk.freq_mhz), py, 6, 0, 2 * Math.PI); ctx.stroke();
    ctx.fillStyle = "#e6b85c"; ctx.textAlign = X(pk.freq_mhz) > w * 0.7 ? "right" : "left"; ctx.textBaseline = "bottom";
    ctx.fillText(`peak ${pk.freq_mhz.toFixed(4)} MHz  +${pk.excess_db.toFixed(2)} dB`, X(pk.freq_mhz) + (ctx.textAlign === "left" ? 9 : -9), py - 4);
    $("spec-out").textContent = `sample #${s.sample_index}${s.utc ? " · " + whenText(s.utc) : ""}${U.finite(s.offset_deg) ? ` · Sun offset ${s.offset_deg.toFixed(2)}°` : ""} · ` +
      `strongest narrow peak ${pk.freq_mhz.toFixed(4)} MHz, ${pk.offset_khz >= 0 ? "+" : ""}${pk.offset_khz.toFixed(0)} kHz from the centre, +${pk.excess_db.toFixed(2)} dB over its local median ` +
      `(dashed: HI rest frequency; shaded: DC and band edges, outside the band power)`;
  }
  $("spec-power").addEventListener("click", () => { specMode = "power"; $("spec-power").classList.add("active"); $("spec-excess").classList.remove("active"); drawSpectrum(lastData); });
  $("spec-excess").addEventListener("click", () => { specMode = "excess"; $("spec-excess").classList.add("active"); $("spec-power").classList.remove("active"); drawSpectrum(lastData); });

  async function refreshLive() {
    if (!liveDir) return true;
    const r = await U.api(`/api/ops/lobe/samples?dir=${encodeURIComponent(liveDir)}`, { timeoutMs: 20000 });
    if (!r.ok) return false;
    lastData = r.data.data;
    drawPlot(lastData); renderFit(lastData); drawSpectrum(lastData);
    return true;
  }

  $("mode-drift").addEventListener("click", () => setMode("drift"));
  $("mode-scan").addEventListener("click", () => setMode("scan"));
  for (const id of ["p-fwhm", "p-half", "p-every", "p-capture", "p-step", "p-gain", "p-minel"]) $(id).addEventListener("change", refreshStatus);
  $("p-confirm").addEventListener("input", updateStart);
  $("btn-start").addEventListener("click", U.guard($("btn-start"), async () => {
    const p = status.plan;
    if (!confirm(`START the ${p.mode.toUpperCase()} transit?\n\nThe mount will slew to RA ${p.point_ra_hours.toFixed(4)} h Dec ${p.point_dec_deg.toFixed(3)}° ` +
                 `(alt ${p.point_alt_deg.toFixed(1)}°)` + (p.mode === "drift" ? " and switch tracking OFF" : " and step across the Sun") +
                 `.\nDuration ${U.hms(p.duration_s)}, gain ${status.params.gain} dB fixed.`)) return;
    if (await job.start("lobe", params(), "MOVE")) { $("p-confirm").value = ""; liveDir = null; }
    refreshStatus();
  }, "STARTING…"));

  setMode("drift");
  job.recover("lobe");
  U.poller(refreshStatus, { intervalMs: 15000 }).start();
  U.poller(refreshLive, { intervalMs: 5000 }).start();
  window.addEventListener("resize", () => refreshLive());
})();
