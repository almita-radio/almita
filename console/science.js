// ALMITA SCIENCE — calls the :8090 ops API only. All map/quality/coverage numbers come from
// science_engine (frozen) via science_web_bridge.py and the read-only /api/ops/science/* endpoints -
// this file only discovers REDUCE sessions, shows coverage/PLAN/RUN as real jobs, and renders the three
// real heatmaps (as the same PNGs a download would give) plus a point picker fed by the same per-point
// values science_web_bridge.py computed. No science math here, no gridding/integration duplicated in JS.
(function () {
  "use strict";
  const U = window.AlmitaUI;
  const $ = (id) => document.getElementById(id);
  U.mountHealthStrip(U.mountHeader("SCIENCE", "LEVEL 2 HEATMAPS"));
  U.mountFooter();

  const showError = (err) => U.showError($("error-banner"), err);
  const msg = (err) => (err && err.message) || String(err);

  let selectedSession = "";          // reduce_session_dir
  let selectedCalibLevel = "";       // "RELATIVE" | "UNCALIBRATED"
  let lastCoverage = null;
  let lastPlanParams = null;
  let lastPlanJobId = null;
  let lastRunOutputDir = null;
  let selectionSeq = 0;              // guards against a late HTTP response overwriting a newer selection

  function currentParams() {
    return {
      reduce_session_dir: selectedSession, calibration_level_filter: selectedCalibLevel,
      velocity_window_min_m_s: kms($("in-vmin").value), velocity_window_max_m_s: kms($("in-vmax").value),
      beam: $("in-beam-mode").value, beam_fwhm_deg: $("in-beam-mode").value === "fwhm" ? Number($("in-beam-fwhm").value) : null,
      beam_cutoff_b_n_fwhm: Number($("in-cutoff-b").value), beam_cutoff_c_n_fwhm: Number($("in-cutoff-c").value),
      quality_policy: $("in-quality-policy").value || null,
      min_spectral_coverage_fraction: Number($("in-min-coverage").value),
      color_vmin: $("in-color-override").checked ? Number($("in-color-vmin").value) : null,
      color_vmax: $("in-color-override").checked ? Number($("in-color-vmax").value) : null,
    };
  }
  function kms(v) { return Math.round(Number(v) * 1000); }
  function paramsEqual(a, b) { return !!a && !!b && JSON.stringify(a) === JSON.stringify(b); }

  function invalidateDownstream() {
    lastPlanParams = null; lastPlanJobId = null; lastRunOutputDir = null;
    $("plan-panel").hidden = !(selectedSession && selectedCalibLevel);
    $("plan-summary").textContent = ""; $("plan-checks").textContent = ""; $("job-science-plan").textContent = "";
    U.setBadge($("plan-badge"), "—");
    $("run-panel").hidden = true;
    $("job-science-run").textContent = "";
    U.setEnabled($("btn-run"), false, "PLAN first");
    $("results-panel").hidden = true;
  }

  // ------------------------------------------------------------------ 1) input discovery
  async function loadInputs() {
    $("input-note").textContent = "discovering REDUCE sessions…";
    const r = await U.api("/api/ops/science/reduce_sessions", { timeoutMs: 20000 });
    const sel = $("in-session"); sel.textContent = ""; sel.appendChild(new Option("(choose)", ""));
    let sessions = [], noData = [];
    if (r.ok) {
      sessions = r.data.data.sessions || []; noData = r.data.data.no_data || [];
      for (const s of sessions) {
        const cl = Object.entries(s.calibration_level_counts || {}).map(([k, v]) => `${k}:${v}`).join(",") || "—";
        const label = `${s.status} · ${s.points_completed}/${s.points_discovered} completed · [${cl}] · ${s.campaign_id} / ${s.reduce_session_id}`;
        sel.appendChild(new Option(label, s.reduce_session_dir));
      }
    }
    $("session-no-data-count").textContent = noData.length;
    $("session-no-data-details").hidden = noData.length === 0;
    const tbody = document.querySelector("#session-no-data-table tbody"); tbody.textContent = "";
    for (const s of noData) {
      const tr = document.createElement("tr");
      for (const v of [s.campaign_id, s.reduce_session_id, s.status, s.points_completed]) {
        const td = document.createElement("td"); td.textContent = v; tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    $("input-note").textContent = r.ok
      ? `${sessions.length} REDUCE session(s) with usable points (${noData.length} more with 0 usable, see the details above)`
      : "could not discover REDUCE sessions";
  }
  $("btn-refresh-inputs").addEventListener("click", U.guard($("btn-refresh-inputs"), loadInputs, "…"));
  $("in-session").addEventListener("change", () => { selectedSession = $("in-session").value; onSessionChanged(); });

  // ------------------------------------------------------------------ 2) coverage
  function metaCard(label, value, warn) {
    const el = document.createElement("div"); el.className = "card" + (warn ? " status-warning" : "");
    const dt = document.createElement("dt"); dt.textContent = label;
    const dd = document.createElement("dd"); dd.textContent = value == null || value === "" ? "—" : String(value);
    el.append(dt, dd); return el;
  }

  async function onSessionChanged() {
    const mySeq = ++selectionSeq;
    selectedCalibLevel = ""; invalidateDownstream();
    $("coverage-panel").hidden = !selectedSession; $("config-panel").hidden = !selectedSession;
    $("row-calib-filter").hidden = true; $("calib-single-note").hidden = true;
    if (!selectedSession) return;
    U.setBadge($("coverage-badge"), "LOADING");
    try {
      const r = await U.api(`/api/ops/science/inspect_reduce_session?path=${encodeURIComponent(selectedSession)}`, { timeoutMs: 20000 });
      if (mySeq !== selectionSeq) return;
      if (!r.ok) throw new Error(U.errorText(r.error));
      lastCoverage = r.data.data;
      renderCoverage();
      showError("");
    } catch (err) {
      if (mySeq !== selectionSeq) return;
      U.setBadge($("coverage-badge"), "ERROR");
      showError(`coverage preview failed: ${msg(err)}`);
    }
  }

  function renderCoverage() {
    const m = lastCoverage;
    const cards = $("coverage-cards"); cards.textContent = "";
    U.setBadge($("coverage-badge"), m.science_contract_ok ? "OK" : "BLOCKED");
    cards.append(
      metaCard("CAMPAIGN", m.campaign_id), metaCard("REDUCE SESSION", m.reduce_session_id),
      metaCard("REDUCE STATUS", m.reduce_status, m.reduce_status !== "COMPLETED"),
      metaCard("POINTS PLANNED", m.points_planned),
      metaCard("POINTS REDUCED (completed/blocked/failed)", `${m.points_reduced_completed}/${m.points_reduced_blocked}/${m.points_reduced_failed}`),
      metaCard("POINTS INGESTABLE BY SCIENCE", m.n_points_ingestable, m.n_points_ingestable !== m.points_reduced_completed),
      metaCard("CALIBRATION LEVEL COUNTS", JSON.stringify(m.calibration_level_counts || {})),
      metaCard("VELOCITY (LSRK) AVAILABLE", `${m.velocity_available_count}/${m.n_points_ingestable}`,
        m.velocity_available_count !== m.n_points_ingestable),
      metaCard("RA range (deg)", m.ra_deg_range ? m.ra_deg_range.map((v) => v.toFixed(3)).join(" .. ") : "—"),
      metaCard("DEC range (deg)", m.dec_deg_range ? m.dec_deg_range.map((v) => v.toFixed(3)).join(" .. ") : "—"),
    );
    const problems = m.science_contract_problems || [];
    $("coverage-problems").hidden = problems.length === 0;
    const plist = $("coverage-problems-list"); plist.textContent = "";
    for (const p of problems) { const row = document.createElement("div"); row.className = "obs-check-row status-block"; row.textContent = p; plist.appendChild(row); }

    const missing = m.velocity_missing_points || [];
    $("coverage-lsrk-missing").hidden = missing.length === 0;
    $("coverage-lsrk-missing-list").textContent = missing.length
      ? `points without a computed LSRK axis (excluded from any velocity-window map): ${missing.join(", ")}`
      : "";

    // calibration-level choice: forced explicit pick if both exist, auto-select if only one
    const levels = Object.keys(m.calibration_level_counts || {}).filter((k) => (m.calibration_level_counts[k] || 0) > 0);
    $("btn-calib-relative").classList.remove("active"); $("btn-calib-uncalibrated").classList.remove("active");
    if (levels.length > 1) {
      $("row-calib-filter").hidden = false;
      $("calib-single-note").hidden = true;
    } else if (levels.length === 1) {
      $("row-calib-filter").hidden = true;
      selectedCalibLevel = levels[0];
      $("calib-single-note").hidden = false;
      $("calib-single-note").textContent = `only ${levels[0]} points exist in this REDUCE session — selected automatically. `
        + (levels[0] === "UNCALIBRATED" ? "This will be an INSTRUMENTAL map (no 50Ω relative profile applied)." : "This will be a RELATIVE (calibrated) map.");
      invalidateDownstream();
      $("plan-panel").hidden = false;
    } else {
      $("row-calib-filter").hidden = true;
    }
  }
  $("btn-calib-relative").addEventListener("click", () => selectCalibLevel("RELATIVE"));
  $("btn-calib-uncalibrated").addEventListener("click", () => selectCalibLevel("UNCALIBRATED"));
  function selectCalibLevel(level) {
    selectedCalibLevel = level;
    $("btn-calib-relative").classList.toggle("active", level === "RELATIVE");
    $("btn-calib-uncalibrated").classList.toggle("active", level === "UNCALIBRATED");
    invalidateDownstream();
    $("plan-panel").hidden = false;
  }
  $("in-beam-mode").addEventListener("change", () => { $("row-beam-fwhm").hidden = $("in-beam-mode").value !== "fwhm"; invalidateDownstream(); });
  $("in-color-override").addEventListener("change", () => { $("row-color-override").hidden = !$("in-color-override").checked; invalidateDownstream(); });
  for (const id of ["in-vmin", "in-vmax", "in-beam-fwhm", "in-cutoff-b", "in-cutoff-c", "in-quality-policy",
    "in-min-coverage", "in-color-vmin", "in-color-vmax"]) {
    $(id).addEventListener("change", invalidateDownstream);
  }

  // ------------------------------------------------------------------ 4) PLAN
  async function pollJobUntilDone(jobId, timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const r = await U.api(`/api/ops/job/${jobId}?tail=80`, { timeoutMs: 15000 });
      if (!r.ok) throw new Error(U.errorText(r.error));
      const j = r.data.data;
      if (j.state !== "RUNNING") return j;
      if (Date.now() > deadline) throw new Error("timed out waiting for the job to finish");
      await new Promise((res) => setTimeout(res, 800));
    }
  }

  function buildStartParams() {
    const p = currentParams();
    const body = { reduce_session_dir: p.reduce_session_dir, calibration_level_filter: p.calibration_level_filter,
      beam: p.beam, velocity_window_min_m_s: p.velocity_window_min_m_s, velocity_window_max_m_s: p.velocity_window_max_m_s,
      beam_cutoff_b_n_fwhm: p.beam_cutoff_b_n_fwhm, beam_cutoff_c_n_fwhm: p.beam_cutoff_c_n_fwhm,
      min_spectral_coverage_fraction: p.min_spectral_coverage_fraction };
    if (p.beam === "fwhm") body.beam_fwhm_deg = p.beam_fwhm_deg;
    if (p.quality_policy) body.quality_policy = p.quality_policy;
    if (p.color_vmin != null && p.color_vmax != null) { body.color_vmin = p.color_vmin; body.color_vmax = p.color_vmax; }
    return body;
  }

  $("btn-plan").addEventListener("click", U.guard($("btn-plan"), async () => {
    if (!selectedSession || !selectedCalibLevel) return;
    const params = buildStartParams();
    try {
      const r = await U.api("/api/ops/start/science_heatmaps_plan", { method: "POST", body: { params, confirm: null }, timeoutMs: 30000 });
      if (!r.ok) throw new Error(U.errorText(r.error));
      const job = await pollJobUntilDone(r.data.data.job_id, 60000);
      if (job.state !== "EXITED" || !job.facts) throw new Error(job.detail || "PLAN did not return a usable result");
      lastPlanParams = currentParams();
      lastPlanJobId = job.job_id;
      renderPlan(job.facts);
      showError("");
    } catch (err) { showError(`PLAN failed: ${msg(err)}`); }
  }, "PLANNING…"));

  function renderPlan(facts) {
    U.setBadge($("plan-badge"), facts.blocked ? "BLOCKED" : "READY");
    const n = (facts.will_process_points || []).length;
    $("plan-summary").textContent =
      `will process ${n} ${facts.config ? facts.config.calibration_level_filter : ""} point(s): ${JSON.stringify(facts.will_process_points || [])}\n`
      + `calibration_level_counts (full session): ${JSON.stringify(facts.calibration_level_counts || {})}\n`
      + `grid: ${facts.grid ? `${facts.grid.ny}x${facts.grid.nx} px, ${facts.grid.pixel_scale_deg.toFixed(4)} deg/px` : "—"}\n`
      + `beam: ${facts.beam ? `${facts.beam.fwhm_deg} deg (${facts.beam.status})` : "—"}   velocity channels: ${facts.n_velocity_channels}\n`
      + `config_hash: ${facts.config_hash}`;
    const list = $("plan-checks"); list.textContent = "";
    for (const c of facts.checks || []) {
      const row = document.createElement("div");
      row.className = "obs-check-row " + (c.ok ? "status-pass" : "status-block");
      row.textContent = `[${c.ok ? "PASS" : "BLOCKED"}] ${c.name}: ${c.detail}`;
      list.appendChild(row);
    }
    $("run-panel").hidden = false;
    U.setEnabled($("btn-run"), !facts.blocked, facts.blocked ? "PLAN is BLOCKED — see the checks above" : "");
  }

  // ------------------------------------------------------------------ 5) RUN
  $("btn-run").addEventListener("click", U.guard($("btn-run"), async () => {
    if (!paramsEqual(lastPlanParams, currentParams())) {
      showError("inputs or parameters changed since PLAN — PLAN again before RUN");
      $("run-panel").hidden = false; U.setEnabled($("btn-run"), false, "PLAN again — inputs/parameters changed");
      return;
    }
    if (!confirm("Run SCIENCE heatmaps for this REDUCE session? Writes a new session under data/science/. No hardware, no mount, no SDR.")) return;
    try {
      const params = { ...buildStartParams(), plan_job_id: lastPlanJobId };
      const r = await U.api("/api/ops/start/science_heatmaps", { method: "POST", body: { params, confirm: null }, timeoutMs: 30000 });
      if (!r.ok) throw new Error(U.errorText(r.error));
      U.setBadge($("run-badge"), "RUNNING");
      pollRun(r.data.data.job_id);
      showError("");
    } catch (err) { showError(`RUN failed: ${msg(err)}`); }
  }, "STARTING…"));

  async function pollRun(jobId) {
    for (;;) {
      const r = await U.api(`/api/ops/job/${jobId}?tail=120`, { timeoutMs: 15000 });
      if (!r.ok) { showError(U.errorText(r.error)); return; }
      const j = r.data.data;
      renderRunJob(j);
      if (j.state !== "RUNNING") { if (j.facts && j.facts.status) await renderResults(j); return; }
      await new Promise((res) => setTimeout(res, 2000));
    }
  }

  function renderRunJob(j) {
    U.setBadge($("run-badge"), j.state === "RUNNING" ? "RUNNING" : j.verdict);
    const container = $("job-science-run"); container.textContent = "";
    const head = document.createElement("div"); head.className = "obs-summary";
    head.textContent = `job ${j.job_id} · ${j.state} · ${j.verdict} · elapsed ${U.hms(j.elapsed_s)}`;
    container.appendChild(head);
    const pre = document.createElement("pre"); pre.className = "job-log"; pre.textContent = j.log_tail || "";
    container.appendChild(pre);
  }

  // ------------------------------------------------------------------ 6) RESULTS
  let lastManifest = null, lastMapAPoints = null, lastGrid = null;

  async function renderResults(job) {
    const facts = job.facts || {};
    lastRunOutputDir = job.output_dir;
    if (!job.output_dir) { showError("RUN finished without a usable output_dir"); return; }
    $("results-panel").hidden = false;

    const mr = await U.api(`/api/ops/file?path=${encodeURIComponent(job.output_dir + "/manifest.json")}`, { timeoutMs: 15000 });
    if (!mr.ok) { showError(`could not read the SCIENCE manifest: ${U.errorText(mr.error)}`); return; }
    const manifest = mr.data;
    lastManifest = manifest;
    lastGrid = manifest.grid;

    $("results-summary").textContent =
      `campaign: ${manifest.campaign_id}   reduce_session: ${manifest.reduce_session_id}   `
      + `calibration_level_filter: ${manifest.calibration_level_filter}   data_completeness: ${manifest.data_completeness}\n`
      + `points used: ${manifest.n_points_used} of ${manifest.n_points_filtered_in} filtered-in points\n`
      + `velocity window (LSRK): [${(manifest.velocity_window_m_s[0] / 1000).toFixed(1)}, ${(manifest.velocity_window_m_s[1] / 1000).toFixed(1)}] km/s\n`
      + `color limits: [${manifest.color_vmin.toFixed(4)}, ${manifest.color_vmax.toFixed(4)}] (${manifest.color_limits_basis})\n`
      + `quality (map B): ${manifest.quality_b.state} — ${(manifest.quality_b.reasons || []).join("; ")}`
      + (manifest.used_point_set_note ? `\nNOTE: ${manifest.used_point_set_note}` : "");
    $("results-hi-caveat").textContent = "INSTRUMENTAL/RELATIVE result — relative_intensity_dimensionless only. "
      + "No celestial HI detection, Kelvin, Jy, N_HI or absolute flux claim is made here.";
    $("results-thermal").textContent = `Thermal drift: ${manifest.thermal_drift.status} — ${manifest.thermal_drift.note}`;

    const od = job.output_dir;
    $("img-map-a").src = `/api/ops/file?path=${encodeURIComponent(od + "/maps/map_a_no_interp.png")}`;
    $("img-map-b").src = `/api/ops/file?path=${encodeURIComponent(od + "/maps/map_b_smooth.png")}`;
    $("img-map-c").src = `/api/ops/file?path=${encodeURIComponent(od + "/maps/map_c_heavy.png")}`;
    $("img-map-coverage").src = `/api/ops/file?path=${encodeURIComponent(od + "/maps/map_coverage.png")}`;
    const hasSnr = (manifest.exports || {}).map_snr;
    $("fig-snr").hidden = !hasSnr;
    if (hasSnr) $("img-map-snr").src = `/api/ops/file?path=${encodeURIComponent(od + "/maps/map_snr.png")}`;

    const artifacts = $("results-artifacts"); artifacts.textContent = "";
    const files = ["manifest.json", "config.json", "provenance.json", "points.csv"];
    for (const [name, exts] of Object.entries(manifest.exports || {})) {
      for (const fn of exts) files.push(`maps/${fn}`);
    }
    for (const rel of files) {
      const a = document.createElement("a"); a.href = `/api/ops/file?path=${encodeURIComponent(od + "/" + rel)}`;
      a.target = "_blank"; a.rel = "noopener"; a.textContent = rel; a.style.marginRight = "12px";
      artifacts.appendChild(a);
    }

    const ar = await U.api(`/api/ops/science/map?dir=${encodeURIComponent(od)}&name=map_a_no_interp`, { timeoutMs: 15000 });
    if (ar.ok) { lastMapAPoints = ar.data.data.points; drawPointPicker(); }
  }

  // ------------------------------------------------------------------ point picker (same positions as map A)
  function tangentOffsetDeg(raDeg, decDeg, centerRaDeg, centerDecDeg) {
    let dRa = (raDeg - centerRaDeg + 180) % 360; if (dRa < 0) dRa += 360; dRa -= 180;
    const cosDec = Math.cos(centerDecDeg * Math.PI / 180);
    return [dRa * cosDec, decDeg - centerDecDeg];
  }

  function drawPointPicker() {
    const canvas = $("point-picker-canvas");
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 560, cssH = 480;
    canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    if (!lastGrid || !lastMapAPoints) return;
    const halfW = lastGrid.width_deg / 2, halfH = lastGrid.height_deg / 2;
    const pad = 20;
    const sx = (x) => cssW - pad - ((x + halfW) / (2 * halfW)) * (cssW - 2 * pad);  // RA increases to the left
    const sy = (y) => cssH - pad - ((y + halfH) / (2 * halfH)) * (cssH - 2 * pad);
    ctx.strokeStyle = "#2b3942"; ctx.strokeRect(pad, pad, cssW - 2 * pad, cssH - 2 * pad);
    const vmin = lastManifest.color_vmin, vmax = lastManifest.color_vmax;
    canvas.__markers = [];
    for (const r of lastMapAPoints) {
      const [x, y] = tangentOffsetDeg(r.ra_deg, r.dec_degrees, lastGrid.center_ra_deg, lastGrid.center_dec_deg);
      const px = sx(x), py = sy(y);
      const used = r.status === "USED";
      ctx.beginPath(); ctx.arc(px, py, 7, 0, 2 * Math.PI);
      if (used) {
        const t = Math.max(0, Math.min(1, (r.value - vmin) / Math.max(vmax - vmin, 1e-30)));
        ctx.fillStyle = `hsl(${(1 - t) * 260}, 70%, 50%)`;
      } else {
        ctx.fillStyle = "#3a3a3a";
      }
      ctx.fill(); ctx.strokeStyle = "#e5e5e5"; ctx.lineWidth = 1; ctx.stroke();
      canvas.__markers.push({ px, py, row: r });
    }
  }

  $("point-picker-canvas").addEventListener("click", async (ev) => {
    const canvas = $("point-picker-canvas");
    const markers = canvas.__markers || [];
    if (!markers.length) return;
    const rect = canvas.getBoundingClientRect();
    const scaleX = canvas.clientWidth ? canvas.clientWidth / rect.width : 1;
    const cx = (ev.clientX - rect.left) * scaleX, cy = (ev.clientY - rect.top) * scaleX;
    let best = null, bestD = Infinity;
    for (const m of markers) {
      const d = Math.hypot(m.px - cx, m.py - cy);
      if (d < bestD) { bestD = d; best = m; }
    }
    if (!best || bestD > 18) {
      $("point-detail").textContent = "no measured point that close — this position (if inside B/C) is a pure "
        + "spatial interpolation with no observation of its own at this exact spot.";
      $("point-spectrum-view").hidden = true;
      return;
    }
    await showPointDetail(best.row);
  });

  async function showPointDetail(row) {
    const lines = [
      `point ${row.point_index}   RA=${row.ra_deg.toFixed(4)}°  Dec=${row.dec_degrees.toFixed(4)}°`,
      `calibration_level: ${row.calibration_level}   REDUCE quality: ${row.reduce_quality_state}`,
      `status: ${row.status}${row.reason ? `  (${row.reason})` : ""}`,
      row.status === "USED" ? `mapped value (integrated relative_intensity × m/s): ${row.value.toFixed(4)} ± ${row.uncertainty.toFixed(4)}` : "no mapped value (excluded)",
      row.spectral_coverage != null ? `spectral coverage of the requested window: ${(row.spectral_coverage * 100).toFixed(1)}%` : "",
      `capture start (UTC): ${row.timestamp_start_utc || "—"}`,
    ];
    $("point-detail").textContent = lines.filter(Boolean).join("\n");
    try {
      const r = await U.api(`/api/ops/reduce/point?session_dir=${encodeURIComponent(lastManifest.reduce_session_dir)}&point_index=${row.point_index}`, { timeoutMs: 15000 });
      if (!r.ok) throw new Error(U.errorText(r.error));
      $("point-spectrum-view").hidden = false;
      drawPointSpectrum(r.data.data.arrays, lastManifest.velocity_window_m_s);
    } catch (err) {
      $("point-spectrum-view").hidden = true;
      $("point-detail").textContent += `\n(spectrum unavailable: ${msg(err)})`;
    }
  }

  function drawPointSpectrum(arrays, window_m_s) {
    const canvas = $("point-spectrum-canvas");
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 900, cssH = 240;
    canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(cssH * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    const vel = arrays.velocity_lsrk_m_s, val = arrays.relative_intensity;
    if (!vel || !vel.length) { ctx.fillStyle = "#91a2ad"; ctx.fillText("no LSRK velocity axis for this point", 10, 20); return; }
    const finite = val.filter((v) => Number.isFinite(v));
    if (!finite.length) { ctx.fillStyle = "#91a2ad"; ctx.fillText("no finite values", 10, 20); return; }
    const vlo = Math.min(...finite), vhi = Math.max(...finite);
    const xlo = Math.min(...vel), xhi = Math.max(...vel);
    const pad = 26;
    const x = (v) => pad + ((v - xlo) / (xhi - xlo)) * (cssW - 2 * pad);
    const y = (v) => cssH - pad - ((v - vlo) / Math.max(vhi - vlo, 1e-30)) * (cssH - 2 * pad);
    // shade the integration window actually used
    const [wlo, whi] = window_m_s;
    ctx.fillStyle = "rgba(101,183,216,0.15)";
    ctx.fillRect(x(Math.max(wlo, xlo)), pad, x(Math.min(whi, xhi)) - x(Math.max(wlo, xlo)), cssH - 2 * pad);
    ctx.strokeStyle = "#2b3942"; ctx.strokeRect(pad, pad, cssW - 2 * pad, cssH - 2 * pad);
    ctx.beginPath(); ctx.strokeStyle = "#65b7d8"; ctx.lineWidth = 1;
    for (let i = 0; i < vel.length; i++) {
      const px = x(vel[i]), py = Number.isFinite(val[i]) ? y(val[i]) : null;
      if (py == null) continue;
      if (i === 0 || !Number.isFinite(val[i - 1])) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.stroke();
    ctx.fillStyle = "#91a2ad"; ctx.font = "10px ui-monospace,monospace";
    ctx.fillText(`${(xlo / 1000).toFixed(1)} km/s`, pad, cssH - 6);
    ctx.textAlign = "right"; ctx.fillText(`${(xhi / 1000).toFixed(1)} km/s`, cssW - pad, cssH - 6); ctx.textAlign = "left";
    ctx.fillText("shaded = integration window used for the maps", pad, pad + 10);
  }

  // ------------------------------------------------------------------ init
  invalidateDownstream();
  $("coverage-panel").hidden = true; $("config-panel").hidden = true;
  loadInputs().catch((err) => showError(`could not load inputs: ${msg(err)}`));
})();
