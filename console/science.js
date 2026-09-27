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

  function numOrNull(id) { const v = $(id).value; return v === "" ? null : Number(v); }
  function currentParams() {
    return {
      reduce_session_dir: selectedSession, calibration_level_filter: selectedCalibLevel,
      velocity_window_min_m_s: kms($("in-vmin").value), velocity_window_max_m_s: kms($("in-vmax").value),
      beam: $("in-beam-mode").value, beam_fwhm_deg: $("in-beam-mode").value === "fwhm" ? Number($("in-beam-fwhm").value) : null,
      support_radius_deg: numOrNull("in-support-radius"), smoothing_fwhm_deg: numOrNull("in-smoothing"),
      interp_factor_b: Number($("in-interp-factor-b").value), interp_factor_c: Number($("in-interp-factor-c").value),
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
  for (const id of ["in-vmin", "in-vmax", "in-beam-fwhm", "in-support-radius", "in-smoothing",
    "in-interp-factor-b", "in-interp-factor-c",
    "in-quality-policy", "in-min-coverage", "in-color-vmin", "in-color-vmax"]) {
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
      min_spectral_coverage_fraction: p.min_spectral_coverage_fraction };
    if (p.beam === "fwhm") body.beam_fwhm_deg = p.beam_fwhm_deg;
    if (p.support_radius_deg != null) body.support_radius_deg = p.support_radius_deg;
    if (p.smoothing_fwhm_deg != null) body.smoothing_fwhm_deg = p.smoothing_fwhm_deg;
    if (p.interp_factor_b) body.interp_factor_b = p.interp_factor_b;
    if (p.interp_factor_c) body.interp_factor_c = p.interp_factor_c;
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
      + `grid dims: A=${facts.n_rows}x${facts.n_cols}`
      + (facts.grid_b ? `  B=${facts.grid_b.ny}x${facts.grid_b.nx}  C=${facts.grid_c.ny}x${facts.grid_c.nx}` : "  B/C=—") + "\n"
      + `support radius (B & C, shared): ${facts.spatial_params ? facts.spatial_params.support_radius_deg.toFixed(4) : "—"} deg   `
      + `smoothing kernel (B & C, shared): ${facts.spatial_params ? facts.spatial_params.smoothing_fwhm_deg.toFixed(4) : "—"} deg\n`
      + `real instrument beam (reported only): ${facts.real_instrument_beam_fwhm_deg} deg   velocity channels: ${facts.n_velocity_channels}\n`
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
  let lastManifest = null, lastBoard = null, lastSelectedCell = null;

  async function renderResults(job) {
    const facts = job.facts || {};
    lastRunOutputDir = job.output_dir;
    if (!job.output_dir) { showError("RUN finished without a usable output_dir"); return; }
    $("results-panel").hidden = false;

    const mr = await U.api(`/api/ops/file?path=${encodeURIComponent(job.output_dir + "/manifest.json")}`, { timeoutMs: 15000 });
    if (!mr.ok) { showError(`could not read the SCIENCE manifest: ${U.errorText(mr.error)}`); return; }
    const manifest = mr.data;
    lastManifest = manifest;
    const dims = manifest.grid_dims || {};

    $("results-summary").textContent =
      `campaign: ${manifest.campaign_id}   reduce_session: ${manifest.reduce_session_id}   `
      + `calibration_level_filter: ${manifest.calibration_level_filter}   data_completeness: ${manifest.data_completeness}\n`
      + `points used: ${manifest.n_points_used} of ${manifest.n_points_filtered_in} filtered-in points\n`
      + `velocity window (LSRK): [${(manifest.velocity_window_m_s[0] / 1000).toFixed(1)}, ${(manifest.velocity_window_m_s[1] / 1000).toFixed(1)}] km/s\n`
      + `color limits (shared by A/B/C): [${manifest.color_vmin.toFixed(4)}, ${manifest.color_vmax.toFixed(4)}] (${manifest.color_limits_basis})\n`
      + `grid dims: A=${dims.a ? dims.a.join("x") : "—"} → B=${dims.b ? dims.b.join("x") : "—"} `
      + `(${manifest.config.interp_factor_b}x) → C=${dims.c ? dims.c.join("x") : "—"} (${manifest.config.interp_factor_c}x) `
      + `— same footprint, increasing resolution\n`
      + `support radius (B & C, shared): ${manifest.spatial_params.support_radius_deg.toFixed(4)} deg   `
      + `smoothing kernel (B & C, shared): ${manifest.spatial_params.smoothing_fwhm_deg.toFixed(4)} deg — `
      + `the ONLY difference between B and C is raster density\n`
      + `real instrument beam (reported only): ${manifest.real_instrument_beam.fwhm_deg} deg\n`
      + `single-point-only pixels (no corroborating 2nd measurement): B=${(manifest.spatial_confidence.b.single_point_fraction * 100).toFixed(0)}%   `
      + `C=${(manifest.spatial_confidence.c.single_point_fraction * 100).toFixed(0)}% of valid pixels — see COVERAGE DENSITY below\n`
      + (manifest.noise_dominance ? (
          `nearest-neighbour value check: median |Δ|=${manifest.noise_dominance.median_nearest_neighbor_abs_value_diff.toFixed(4)} `
          + `(x${manifest.noise_dominance.ratio_observed_to_expected_noise.toFixed(2)} vs sqrt(2)×median σ=${manifest.noise_dominance.expected_abs_diff_if_independent_noise.toFixed(4)}), `
          + `correlation r=${manifest.noise_dominance.nearest_neighbor_value_correlation.toFixed(2)}`
          + (manifest.noise_dominance.consistent_with_pure_noise_at_point_spacing
            ? "  → CONSISTENT WITH PURE NOISE at this point spacing: any multi-pixel bump/dip in B/C may be chance noise, not real structure\n"
            : "  → neighbours correlate more than pure noise would predict\n")
        ) : "")
      + (manifest.loo_cross_validation && manifest.loo_cross_validation.predicted_vs_measured_correlation != null ? (
          `LEAVE-ONE-OUT CHECK: predicting each of ${manifest.loo_cross_validation.n_predictable} real point(s) from `
          + `ONLY its neighbours (same kernel as B/C) correlates with its own measured value at `
          + `r=${manifest.loo_cross_validation.predicted_vs_measured_correlation.toFixed(2)}, RMS held-out error=`
          + `${manifest.loo_cross_validation.rms.toFixed(4)} vs field std=${manifest.loo_cross_validation.field_value_std.toFixed(4)}`
          + (manifest.loo_cross_validation.rms_worse_than_predicting_the_field_mean
            ? "  → WORSE than just guessing the field's mean: no verified predictive skill at this point density\n"
            : "  → better than guessing the field's mean\n")
        ) : "")
      + `quality (map B): ${manifest.quality_b.state} — ${(manifest.quality_b.reasons || []).join("; ")}`
      + (manifest.used_point_set_note ? `\nNOTE: ${manifest.used_point_set_note}` : "");
    $("results-hi-caveat").textContent = "INSTRUMENTAL/" + manifest.calibration_level_filter + " result — "
      + "relative_intensity_dimensionless only. No HI detection, Kelvin, Jy, N_HI or absolute flux claim is made here "
      + "(indoor/UNCALIBRATED runs carry real instrumental residual - never a celestial signal).";
    $("results-thermal").textContent = `Thermal drift: ${manifest.thermal_drift.status} — ${manifest.thermal_drift.note}`;
    $("caption-a").textContent = `A — MEASURED (${dims.a ? dims.a.join("x") : "?"}, no interpolation)`;
    $("caption-b").textContent = `B — INTERPOLATED (${dims.b ? dims.b.join("x") : "?"})`;
    $("caption-c").textContent = `C — INTERPOLATED, finer raster (${dims.c ? dims.c.join("x") : "?"}, same kernel as B)`;

    const od = job.output_dir;
    // ON-SCREEN images are the chrome-free "_bare" exports (no title/colorbar/caption baked in - see
    // render_all_maps._bare_export) so B/C display at the SAME visible size as A's own canvas, next to the
    // SAME external HTML legend below. The full titled/colorbar'd PNGs remain available under DOWNLOADS.
    $("img-map-b").src = `/api/ops/file?path=${encodeURIComponent(od + "/maps/map_b_smooth_bare.png")}`;
    $("img-map-c").src = `/api/ops/file?path=${encodeURIComponent(od + "/maps/map_c_heavy_bare.png")}`;
    $("img-map-coverage").src = `/api/ops/file?path=${encodeURIComponent(od + "/maps/map_coverage.png")}`;
    const hasSnr = (manifest.exports || {}).map_snr;
    $("fig-snr").hidden = !hasSnr;
    if (hasSnr) $("img-map-snr").src = `/api/ops/file?path=${encodeURIComponent(od + "/maps/map_snr.png")}`;
    const hasCovDensity = (manifest.exports || {}).map_coverage_density;
    $("fig-coverage-density").hidden = !hasCovDensity;
    if (hasCovDensity) $("img-map-coverage-density").src = `/api/ops/file?path=${encodeURIComponent(od + "/maps/map_coverage_density.png")}`;
    const hasCombined = (manifest.exports || {}).map_abc_combined;
    if (hasCombined) {
      const combinedUrl = `/api/ops/file?path=${encodeURIComponent(od + "/maps/map_abc_combined.png")}`;
      $("img-abc-combined").src = combinedUrl; $("link-abc-combined").href = combinedUrl;
    }

    const artifacts = $("results-artifacts"); artifacts.textContent = "";
    const files = ["manifest.json", "config.json", "provenance.json", "points.csv", "maps/board.json"];
    for (const [name, exts] of Object.entries(manifest.exports || {})) {
      for (const fn of exts) files.push(`maps/${fn}`);
    }
    for (const rel of files) {
      const a = document.createElement("a"); a.href = `/api/ops/file?path=${encodeURIComponent(od + "/" + rel)}`;
      a.target = "_blank"; a.rel = "noopener"; a.textContent = rel; a.style.marginRight = "12px";
      artifacts.appendChild(a);
    }

    const br = await U.api(`/api/ops/science/map?dir=${encodeURIComponent(od)}&name=board`, { timeoutMs: 15000 });
    if (br.ok) {
      lastBoard = br.data.data; lastSelectedCell = null;
      drawBoardA();
      renderColorLegend("legend-a", lastBoard);
      renderColorLegend("legend-b", lastBoard);
      renderColorLegend("legend-c", lastBoard);
    }
  }

  // ------------------------------------------------------------------ map A: the ONLY interactive panel.
  // B and C are plain <img> exports (see renderResults) - a new interpolated pixel is never a stand-in for a
  // real sample, so it gets no click handler and no per-cell payload at all (board.json now describes A only
  // - see science_web_bridge.board_json_payload's own docstring).
  function drawBoardA() {
    const canvas = $("board-canvas-a");
    const ctx = canvas.getContext("2d");
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 440, cssH = canvas.clientWidth || 440;   // square board
    canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(cssH * dpr);
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = "#0d1317"; ctx.fillRect(0, 0, cssW, cssH);
    if (!lastBoard) return;
    const { n_rows: nRows, n_cols: nCols } = lastBoard;
    const cw = cssW / nCols, ch = cssH / nRows;
    canvas.__cells = [];
    for (const cell of lastBoard.cells) {
      // SAME orientation as the exported PNGs: RA increases to the left (col index n_cols-1 drawn leftmost),
      // row 0 (southernmost) drawn at the bottom (origin="lower") - see build_mosaic_grid/render_all_maps.
      const px = (nCols - 1 - cell.col) * cw, py = (nRows - 1 - cell.row) * ch;
      if (cell.valid) { ctx.fillStyle = cell.color; ctx.fillRect(px, py, cw, ch); }
      ctx.strokeStyle = "#33414a"; ctx.lineWidth = 1; ctx.strokeRect(px, py, cw, ch);
      canvas.__cells.push({ px, py, cw, ch, cell });
    }
    if (lastSelectedCell) {
      const hit = canvas.__cells.find((c) => c.cell.row === lastSelectedCell.row && c.cell.col === lastSelectedCell.col);
      if (hit) { ctx.strokeStyle = "#ffffff"; ctx.lineWidth = 3; ctx.strokeRect(hit.px + 1.5, hit.py + 1.5, hit.cw - 3, hit.ch - 3); }
    }
  }

  function cellAtEvent(canvas, ev) {
    const rect = canvas.getBoundingClientRect();
    const scale = canvas.clientWidth ? canvas.clientWidth / rect.width : 1;
    const x = (ev.clientX - rect.left) * scale, y = (ev.clientY - rect.top) * scale;
    for (const c of canvas.__cells || []) {
      if (x >= c.px && x < c.px + c.cw && y >= c.py && y < c.py + c.ch) return c.cell;
    }
    return null;
  }
  $("board-canvas-a").addEventListener("click", (ev) => {
    const cell = cellAtEvent($("board-canvas-a"), ev);
    if (!cell) return;
    // unambiguous identification: exactly one real cell (or none) is ever hit, never an overlapping marker.
    lastSelectedCell = { row: cell.row, col: cell.col };
    drawBoardA();
    showCellDetail(cell);
  });

  async function showCellDetail(cell) {
    const lines = [
      `cell row ${cell.row + 1} col ${cell.col + 1}   RA=${cell.ra_deg.toFixed(4)}°  Dec=${cell.dec_degrees.toFixed(4)}°`,
      cell.point_index != null
        ? `real point ${cell.point_index}   status=${cell.point_status}${cell.point_reason ? ` (${cell.point_reason})` : ""}`
        : "no pointing at this mosaic position — nothing was measured here.",
      cell.valid ? `measured value: ${cell.value.toFixed(4)} ± ${cell.uncertainty.toFixed(4)}` : "no measured value",
    ];
    $("cell-detail").textContent = lines.join("\n");
    if (cell.point_index == null) { $("point-spectrum-view").hidden = true; return; }
    try {
      const r = await U.api(`/api/ops/reduce/point?session_dir=${encodeURIComponent(lastManifest.reduce_session_dir)}&point_index=${cell.point_index}`, { timeoutMs: 15000 });
      if (!r.ok) throw new Error(U.errorText(r.error));
      $("point-spectrum-view").hidden = false;
      drawPointSpectrum(r.data.data.arrays, lastManifest.velocity_window_m_s);
    } catch (err) {
      $("point-spectrum-view").hidden = true;
      $("cell-detail").textContent += `\n(spectrum unavailable: ${msg(err)})`;
    }
  }

  // ONE shared HTML colour legend, next to EACH of the three panels (request: "una barra de color visible
  // junto a cada imagen... alinea las barras sin alterar el tamano de las imagenes") - built from the SAME
  // color_vmin/color_vmax/color_stops the server computed with viridis_hex() (the identical function baked
  // into every PNG export), so A's canvas and B/C's images never drift apart from their own legend or from
  // each other; a plain CSS gradient div never resizes the image elements it sits beside.
  function renderColorLegend(elId, board) {
    const el = $(elId);
    const stops = board.color_stops || [];
    const grad = el.querySelector(".legend-gradient");
    const [maxLbl, midLbl, minLbl] = el.querySelectorAll(".legend-labels span");
    if (stops.length) {
      const stepPct = 100 / (stops.length - 1);
      grad.style.background = `linear-gradient(to top, ${stops.map((c, i) => `${c} ${(i * stepPct).toFixed(2)}%`).join(", ")})`;
    }
    maxLbl.textContent = board.color_vmax.toFixed(1);
    midLbl.textContent = ((board.color_vmax + board.color_vmin) / 2).toFixed(1);
    minLbl.textContent = board.color_vmin.toFixed(1);
    el.title = board.color_units || "";
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
