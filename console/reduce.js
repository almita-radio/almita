// ALMITA REDUCE — calls the :8090 ops API only. All spectral/mask/calibration/velocity numbers come from
// reduce_engine (frozen) via reduce_plan/reduce/reduce_capture_plan/reduce_capture and the read-only
// /api/ops/reduce/* endpoints - this file only discovers inputs, previews metadata, shows PLAN/RUN as real
// jobs, and renders results. No science here, no reduction math duplicated in JS.
(function () {
  "use strict";
  const U = window.AlmitaUI;
  const $ = (id) => document.getElementById(id);
  U.mountHealthStrip(U.mountHeader("REDUCE", "SPECTRAL REDUCTION"));
  U.mountFooter();

  const showError = (err) => U.showError($("error-banner"), err);

  let mode = "capture";              // "capture" | "campaign"
  let selectedInput = "";            // capture path, or campaign_dir path
  let selectedProfile = "";          // "" = none (UNCALIBRATED)
  let lastMetadata = null;
  let lastPlanParams = null;         // the exact params PLAN last succeeded with - RUN refuses if current params differ
  let lastPlanFacts = null;
  let lastPlanJobId = null;          // the PLAN job's own id - RUN must send it back; the SERVER re-checks it
                                      // (not just this page's own paramsEqual gate) - see _require_fresh_plan
  let lastRunOutputDir = null;
  let lastCalibrationPreview = null; // campaign mode + a profile: per-point compatibility (all accepted points,
                                      // not just a representative sample) - drives the confirmation checkbox and
                                      // the RUN gate the server independently re-checks (_require_uncalibrated_confirmation)
  let lastSingleCaptureCompat = null; // capture mode + a profile: the one point's {status, reason} - shown again in RESULTS

  function currentParams() {
    return {
      mode, input: selectedInput, calibration_profile: selectedProfile || null,
      velocity_frame: $("in-velocity-frame").value,
    };
  }
  function paramsEqual(a, b) {
    return !!a && !!b && JSON.stringify(a) === JSON.stringify(b);
  }

  function invalidateDownstream() {
    lastPlanParams = null; lastPlanFacts = null; lastPlanJobId = null; lastRunOutputDir = null;
    lastCalibrationPreview = null;
    $("campaign-calibration-preview").hidden = true;
    $("in-confirm-uncalibrated").checked = false;
    $("campaign-points-breakdown").hidden = true;
    $("plan-panel").hidden = selectedInput === "";
    $("plan-summary").textContent = ""; $("plan-checks").textContent = ""; $("job-reduce-plan").textContent = "";
    U.setBadge($("plan-badge"), "—");
    $("run-panel").hidden = true;
    $("job-reduce-run").textContent = "";
    U.setEnabled($("btn-run"), false, "PLAN first");
    $("results-panel").hidden = true;
  }

  // ------------------------------------------------------------------ 1) input discovery + selection
  async function loadInputs() {
    $("input-note").textContent = "discovering captures and classifying campaigns… the very first load after "
      + "data changes can take up to a minute (every declared-success capture is independently opened and "
      + "validated, once - cached after that).";
    const [capRes, campListRes, campRes] = await Promise.all([
      U.api("/api/ops/reduce/captures", { timeoutMs: 15000 }),
      U.api("/api/ops/reduce/campaigns", { timeoutMs: 90000 }),
      U.api("/api/ops/campaigns", { timeoutMs: 15000 }),           // still the source for calibration profiles
    ]);
    const capSel = $("in-capture"); capSel.textContent = "";
    capSel.appendChild(new Option("(choose)", ""));
    if (capRes.ok) for (const c of capRes.data.data) capSel.appendChild(new Option(`${c.path} (${U.bytes(c.size_bytes)})`, c.path));

    const campSel = $("in-campaign"); campSel.textContent = "";
    campSel.appendChild(new Option("(choose)", ""));
    let usableCampaigns = [], noDataCampaigns = [];
    if (campListRes.ok) {
      usableCampaigns = campListRes.data.data.campaigns || [];
      noDataCampaigns = campListRes.data.data.no_data || [];
      // Newest first is already how the server orders them; COMPLETA before PARCIAL only as a tie-break within
      // that order would hide recency, so keep server order (mtime-descending) - just label each option clearly.
      for (const c of usableCampaigns) {
        const label = `${c.completeness} · ${c.points_usable}/${c.points_expected == null ? "?" : c.points_expected} usable · ${c.session_label} · ${c.name}`;
        campSel.appendChild(new Option(label, c.campaign_dir));
      }
    }
    renderNoDataCampaigns(noDataCampaigns);

    const profSel = $("in-profile");
    const keepProfile = profSel.value;
    profSel.textContent = "";
    profSel.appendChild(new Option("(none — run UNCALIBRATED)", ""));
    if (campRes.ok) for (const pr of campRes.data.data.profiles || []) profSel.appendChild(new Option(pr.name, pr.path));
    if (keepProfile) profSel.value = keepProfile;

    $("input-note").textContent = capRes.ok && campListRes.ok
      ? `${capRes.data.data.length} standalone capture(s) · ${usableCampaigns.length} campaign(s) with usable data `
        + `(${noDataCampaigns.length} more with 0 usable captures, see "campaigns with 0 usable captures" below) · `
        + `${(campRes.ok ? campRes.data.data.profiles || [] : []).length} calibration profile(s) discovered on the server`
      : "could not discover inputs";
  }

  function renderNoDataCampaigns(rows) {
    const details = $("campaign-no-data-details");
    $("campaign-no-data-count").textContent = rows.length;
    details.hidden = rows.length === 0;
    const tbody = document.querySelector("#campaign-no-data-table tbody"); tbody.textContent = "";
    for (const c of rows) {
      const tr = document.createElement("tr");
      const cells = [c.name, c.session_label || "—", c.points_expected == null ? "—" : c.points_expected,
        c.points_usable, c.completeness + (c.error ? `: ${c.error}` : "")];
      for (const v of cells) { const td = document.createElement("td"); td.textContent = v; tr.appendChild(td); }
      tbody.appendChild(tr);
    }
  }

  function setMode(newMode) {
    mode = newMode;
    $("mode-capture").classList.toggle("active", mode === "capture");
    $("mode-campaign").classList.toggle("active", mode === "campaign");
    $("row-capture").hidden = mode !== "capture";
    $("row-campaign").hidden = mode !== "campaign";
    selectedInput = mode === "capture" ? $("in-capture").value : $("in-campaign").value;
    onInputChanged();
  }
  $("mode-capture").addEventListener("click", () => setMode("capture"));
  $("mode-campaign").addEventListener("click", () => setMode("campaign"));
  $("in-capture").addEventListener("change", () => { selectedInput = $("in-capture").value; onInputChanged(); });
  $("in-campaign").addEventListener("change", () => { selectedInput = $("in-campaign").value; onInputChanged(); });
  $("btn-refresh-inputs").addEventListener("click", U.guard($("btn-refresh-inputs"), loadInputs, "…"));

  // ------------------------------------------------------------------ 2) metadata preview
  function metaCard(label, value, warn) {
    const el = document.createElement("div"); el.className = "card" + (warn ? " status-warning" : "");
    const dt = document.createElement("dt"); dt.textContent = label;
    const dd = document.createElement("dd"); dd.textContent = value == null || value === "" ? "—" : String(value);
    el.append(dt, dd); return el;
  }

  async function onInputChanged() {
    invalidateDownstream();
    if (!selectedInput) { $("metadata-panel").hidden = true; $("profile-panel").hidden = true; return; }
    $("metadata-panel").hidden = false; $("profile-panel").hidden = false;
    U.setBadge($("metadata-badge"), "LOADING");
    try {
      const endpoint = mode === "capture" ? "/api/ops/reduce/inspect_capture" : "/api/ops/reduce/inspect_campaign";
      const r = await U.api(`${endpoint}?path=${encodeURIComponent(selectedInput)}`, { timeoutMs: 20000 });
      if (!r.ok) throw new Error(U.errorText(r.error));
      lastMetadata = r.data.data;
      renderMetadata();
      showError("");
    } catch (err) {
      U.setBadge($("metadata-badge"), "ERROR");
      showError(`metadata preview failed: ${msg(err)}`);
    }
    await refreshCompatibility();
  }

  function msg(err) { return (err && err.message) || String(err); }

  function renderMetadata() {
    const cards = $("metadata-cards"); cards.textContent = "";
    const m = lastMetadata;
    if (mode === "capture") {
      $("campaign-points-breakdown").hidden = true;
      U.setBadge($("metadata-badge"), (m.attrs_missing_required.length || m.contradictions.length) ? "WARNING" : "OK");
      cards.append(
        metaCard("CAPTURE", m.capture), metaCard("STATUS", m.capture_status),
        metaCard("CENTER FREQUENCY (nominal)", U.mhz(m.center_frequency_hz_nominal)),
        metaCard("SAMPLE RATE", U.msps(m.sample_rate_hz)),
        metaCard("GAIN (requested)", m.gain_db_requested != null ? `${m.gain_db_requested} dB` : "—", m.gain_db_requested == null),
        metaCard("DURATION", U.fixed(m.duration_seconds, 2, "s")),
        metaCard("CAPTURE START (UTC)", U.utc(m.capture_start_utc)),
        metaCard("TOPOLOGY / PROVENANCE", m.topology, m.topology === "UNKNOWN"),
        metaCard("RECEIVER", m.receiver),
        metaCard("FREQUENCY AXIS (software)", m.frequency_axis_check ? (m.frequency_axis_check.frequency_axis_correct ? "internally consistent" : "INCONSISTENT") : "N/A"),
      );
      $("metadata-frequency-note").textContent = "absolute_frequency_calibrated: FALSE — " + (m.ppm_status ? m.ppm_status.status : "");
      const probs = [...m.attrs_missing_required.map((k) => `missing required: ${k}`), ...m.contradictions];
      $("metadata-problems").hidden = probs.length === 0;
      const list = $("metadata-problems-list"); list.textContent = "";
      for (const p of probs) { const row = document.createElement("div"); row.className = "obs-check-row status-warning"; row.textContent = p; list.appendChild(row); }
    } else {
      U.setBadge($("metadata-badge"), m.completeness === "COMPLETA" ? "OK" : m.completeness === "PARCIAL" ? "WARNING" : "WARNING");
      cards.append(
        metaCard("CAMPAIGN ID", m.campaign_id), metaCard("SESSION", m.session_label),
        metaCard("COMPLETENESS", m.completeness, m.completeness !== "COMPLETA"),
        metaCard("USABLE CAPTURES / EXPECTED", `${m.points_usable} / ${m.points_expected}`, m.points_usable < m.points_expected),
        metaCard("DEFERRED (excluded by the plan itself)", m.points_deferred, false),
        metaCard("MISSING OR INVALID", m.points_missing_or_invalid, m.points_missing_or_invalid > 0),
        metaCard("GRID", m.grid ? `${m.grid.rows || "?"}x${m.grid.columns || "?"} · ${U.num(m.grid.total_points)} pts · spacing ${U.deg(m.grid.nominal_spacing_deg)}` : "—"),
        metaCard("OBSERVER", m.observer && m.observer.name ? m.observer.name : "UNKNOWN", !(m.observer && m.observer.name)),
      );
      if (m.sample_point_metadata && !m.sample_point_metadata.error) {
        const sm = m.sample_point_metadata;
        cards.append(
          metaCard(`SAMPLE (point ${m.sample_point_index}) FREQUENCY`, U.mhz(sm.center_frequency_hz_nominal)),
          metaCard("SAMPLE SAMPLE RATE", U.msps(sm.sample_rate_hz)),
          metaCard("SAMPLE GAIN", sm.gain_db_requested != null ? `${sm.gain_db_requested} dB` : "—"),
          metaCard("SAMPLE TOPOLOGY", sm.topology, sm.topology === "UNKNOWN"),
        );
      }
      $("metadata-frequency-note").textContent = m.points_usable === 0
        ? "no point has a real, independently-validated capture yet — this campaign cannot be reduced until at least one point is really captured"
        : `metadata above is from a REAL, VALIDATED sample point (index ${m.sample_point_index}); other usable points share the same session config but are not individually re-checked here — PLAN itself re-validates every point`;
      const problems = (m.points_detail || []).filter((p) => p.status === "MISSING" || p.status === "INVALID");
      $("metadata-problems").hidden = problems.length === 0;
      const list = $("metadata-problems-list"); list.textContent = "";
      for (const p of problems.slice(0, 20)) {
        const row = document.createElement("div"); row.className = "obs-check-row status-warning";
        row.textContent = `point ${p.point_index} [${p.status}]: ${p.reason}`; list.appendChild(row);
      }
      if (problems.length > 20) { const row = document.createElement("div"); row.className = "muted"; row.textContent = `… and ${problems.length - 20} more`; list.appendChild(row); }
      renderCampaignPointsBreakdown(m);
    }
  }

  // "Which points have data and which don't" - shown for any campaign that is not fully COMPLETA, straight
  // from the same points_detail the selector's own classification computed (never re-derived here).
  function renderCampaignPointsBreakdown(m) {
    const box = $("campaign-points-breakdown");
    if (m.completeness === "COMPLETA" || !(m.points_detail || []).length) { box.hidden = true; return; }
    box.hidden = false;
    $("campaign-points-summary").textContent =
      `${m.points_usable} usable · ${m.points_missing_or_invalid} missing/invalid · ${m.points_deferred} deferred by the plan · of ${m.points_total} points total`;
    const list = $("campaign-points-list"); list.textContent = "";
    const byStatus = { USABLE: [], MISSING: [], INVALID: [], DEFERRED: [] };
    for (const p of m.points_detail) (byStatus[p.status] || []).push(p.point_index);
    const line = (label, cls, indices) => {
      if (!indices.length) return;
      const row = document.createElement("div"); row.className = "obs-check-row " + cls;
      const shown = indices.slice(0, 40).join(", ") + (indices.length > 40 ? `, … (${indices.length - 40} more)` : "");
      row.textContent = `${label} (${indices.length}): points ${shown}`;
      list.appendChild(row);
    };
    line("USABLE", "status-pass", byStatus.USABLE);
    line("MISSING", "status-block", byStatus.MISSING);
    line("INVALID HDF5", "status-block", byStatus.INVALID);
    line("DEFERRED (plan excluded)", "status-warning", byStatus.DEFERRED);
  }

  // ------------------------------------------------------------------ 3) profile + compatibility
  $("in-profile").addEventListener("change", () => { selectedProfile = $("in-profile").value; invalidateDownstream(); refreshCompatibility(); });
  $("in-velocity-frame").addEventListener("change", invalidateDownstream);

  function setBanner(banner, status, text) {
    const ok = status === "COMPATIBLE";
    banner.hidden = false; banner.textContent = text;
    banner.style.background = ok ? "#122a1e" : (status === "UNKNOWN" ? "" : "#2a1414");
    banner.style.color = ok ? "var(--ok)" : (status === "UNKNOWN" ? "var(--warn)" : "var(--error)");
    banner.style.borderColor = ok ? "#1e5c3d" : (status === "UNKNOWN" ? "" : "#7a2a2a");
  }

  async function refreshCompatibility() {
    selectedProfile = $("in-profile").value;
    const banner = $("compat-banner"), detail = $("compat-detail");
    const preview = $("campaign-calibration-preview");
    preview.hidden = true;
    $("in-confirm-uncalibrated").checked = false;
    lastCalibrationPreview = null;
    lastSingleCaptureCompat = null;
    if (!selectedInput) { banner.hidden = true; detail.textContent = ""; return; }
    if (!selectedProfile) {
      banner.hidden = false; banner.textContent = "UNCALIBRATED (no profile selected)";
      banner.style.background = ""; banner.style.color = "var(--muted)"; banner.style.borderColor = "";
      detail.textContent = "REDUCE will run without a relative-calibration profile — a real, supported mode (reduce_engine.calibration.apply_calibration accepts profile=None). Results are labeled calibration_level=UNCALIBRATED.";
      return;
    }
    if (mode === "campaign") { await refreshCampaignCalibrationPreview(); return; }
    banner.hidden = false; banner.textContent = "CHECKING…"; detail.textContent = "";
    try {
      const r = await U.api(`/api/ops/reduce/compatibility?capture=${encodeURIComponent(selectedInput)}&profile=${encodeURIComponent(selectedProfile)}`, { timeoutMs: 20000 });
      if (!r.ok) throw new Error(U.errorText(r.error));
      const c = r.data.data;
      lastSingleCaptureCompat = { status: c.status, reason: c.reason };
      setBanner(banner, c.status, c.status);
      detail.textContent = c.reason;
    } catch (err) {
      banner.hidden = false; banner.textContent = "CHECK FAILED"; banner.style.background = "#2a1414"; banner.style.color = "var(--error)";
      detail.textContent = msg(err);
    }
  }

  // Every accepted point in the campaign, checked individually - not a representative sample. Drives the
  // typed-confirmation gate the server independently re-checks at RUN (_require_uncalibrated_confirmation) -
  // showing anything less than this before RUN would not match what actually gates RUN.
  async function refreshCampaignCalibrationPreview() {
    const banner = $("compat-banner"), detail = $("compat-detail");
    const preview = $("campaign-calibration-preview");
    banner.hidden = false; banner.textContent = "CHECKING EVERY POINT…"; detail.textContent = "";
    try {
      const r = await U.api(`/api/ops/reduce/campaign_calibration_preview?campaign_dir=${encodeURIComponent(selectedInput)}&profile=${encodeURIComponent(selectedProfile)}`, { timeoutMs: 30000 });
      if (!r.ok) throw new Error(U.errorText(r.error));
      const d = r.data.data;
      lastCalibrationPreview = d;
      const c = d.counts;
      const overallStatus = d.all_compatible ? "COMPATIBLE" : ((c.INCOMPATIBLE || 0) > 0 ? "INCOMPATIBLE" : "UNKNOWN");
      setBanner(banner, overallStatus, d.all_compatible ? "COMPATIBLE (every accepted point)"
        : `MIXED / NOT ALL COMPATIBLE (${c.COMPATIBLE || 0} compatible, ${c.INCOMPATIBLE || 0} incompatible, ${c.UNKNOWN || 0} unverifiable of ${d.total_accepted_points})`);
      detail.textContent = d.all_compatible
        ? "every accepted point was checked individually against this profile (frequency, sample rate, gain, topology) - none are only assumed."
        : "the profile will NOT be applied to the incompatible/unverifiable points below (no partial correction) - those points will be reduced UNCALIBRATED. Compatible points still get the real relative correction.";
      preview.hidden = false;
      $("campaign-calibration-counts").textContent = `${c.COMPATIBLE || 0} compatible · ${c.INCOMPATIBLE || 0} incompatible · ${c.UNKNOWN || 0} unverifiable · of ${d.total_accepted_points} accepted points`;
      $("campaign-calibration-note").textContent = d.all_compatible ? "" :
        "Continuing will produce calibration_level=UNCALIBRATED for the incompatible/unverifiable points listed below; calibration_level=RELATIVE for the compatible ones.";
      const list = $("campaign-calibration-points"); list.textContent = "";
      for (const pt of d.points.slice(0, 30)) {
        const row = document.createElement("div");
        row.className = "obs-check-row " + (pt.status === "COMPATIBLE" ? "status-pass" : pt.status === "UNKNOWN" ? "status-warning" : "status-block");
        row.textContent = `point ${pt.point_index}: [${pt.status}] ${pt.reason}`;
        list.appendChild(row);
      }
      if (d.points.length > 30) { const row = document.createElement("div"); row.className = "muted"; row.textContent = `… and ${d.points.length - 30} more points`; list.appendChild(row); }
      $("campaign-calibration-confirm-row").hidden = d.all_compatible;
    } catch (err) {
      banner.hidden = false; banner.textContent = "CHECK FAILED"; banner.style.background = "#2a1414"; banner.style.color = "var(--error)";
      detail.textContent = msg(err);
      lastCalibrationPreview = null;
    }
  }

  // ------------------------------------------------------------------ 4) PLAN (a real job)
  async function pollJobUntilDone(jobId, timeoutMs) {
    const deadline = Date.now() + timeoutMs;
    for (;;) {
      const r = await U.api(`/api/ops/job/${jobId}?tail=60`, { timeoutMs: 15000 });
      if (!r.ok) throw new Error(U.errorText(r.error));
      const j = r.data.data;
      if (j.state !== "RUNNING") return j;
      if (Date.now() > deadline) throw new Error("timed out waiting for the job to finish");
      await new Promise((res) => setTimeout(res, 800));
    }
  }

  function stageFor(action) {
    // Real registered stage names only (almita_web_ops.py's STAGES): reduce_capture_plan/reduce_capture for a
    // single HDF5 capture, reduce_plan/reduce for a campaign - never `reduce_capture_${action}`, which for
    // action="run" produced "reduce_capture_run", a stage that has never existed (a real bug found and fixed
    // during REDUCE verification: the single-capture RUN button always 400'd with "unknown stage").
    if (mode === "capture") return action === "plan" ? "reduce_capture_plan" : "reduce_capture";
    return action === "plan" ? "reduce_plan" : "reduce";
  }
  function inputParams() {
    const p = { calibration_profile: selectedProfile || undefined, velocity_frame: $("in-velocity-frame").value };
    p[mode === "capture" ? "capture" : "campaign_dir"] = selectedInput;
    return p;
  }

  $("btn-plan").addEventListener("click", U.guard($("btn-plan"), async () => {
    if (!selectedInput) return;
    const params = inputParams();
    try {
      const r = await U.api(`/api/ops/start/${stageFor("plan")}`, { method: "POST", body: { params, confirm: null }, timeoutMs: 30000 });
      if (!r.ok) throw new Error(U.errorText(r.error));
      const job = await pollJobUntilDone(r.data.data.job_id, 60000);
      if (job.state !== "EXITED" || !job.facts) throw new Error(job.detail || "PLAN did not return a usable result");
      lastPlanFacts = job.facts;
      lastPlanParams = currentParams();
      lastPlanJobId = job.job_id;
      renderPlan(job.facts);
      showError("");
    } catch (err) { showError(`PLAN failed: ${msg(err)}`); }
  }, "PLANNING…"));

  function renderPlan(facts) {
    U.setBadge($("plan-badge"), facts.blocked ? "BLOCKED" : "READY");
    // Explicit, before RUN is enabled: how many points this PLAN will actually process - from the same real
    // usable/expected/deferred/missing classification the selector and metadata preview show (lastMetadata),
    // not just a bare accepted-point count, so an operator never sees RUN enabled without knowing the scope.
    let willProcess;
    if (mode === "campaign" && lastMetadata) {
      const m = lastMetadata;
      willProcess = `will process ${m.points_usable} of ${m.points_expected} expected point(s)`
        + (m.points_deferred ? ` (${m.points_deferred} deferred by the plan itself, not attempted)` : "")
        + (m.points_missing_or_invalid ? ` (${m.points_missing_or_invalid} missing/invalid, will be skipped or marked failed per point)` : "");
    } else {
      willProcess = "will process 1 point (this single capture)";
    }
    $("plan-summary").textContent = `${willProcess}\nconfig_hash: ${facts.config_hash}\nestimated output: ~${facts.estimated_output_mb} MB\n`
      + (facts.compatibility ? `compatibility: ${facts.compatibility.status} — ${facts.compatibility.reason}\n` : "");
    const list = $("plan-checks"); list.textContent = "";
    for (const c of facts.checks || []) {
      const row = document.createElement("div");
      row.className = "obs-check-row " + (c.ok ? "status-pass" : "status-block");
      row.textContent = `[${c.ok ? "PASS" : "BLOCKED"}] ${c.name}: ${c.detail}`;
      list.appendChild(row);
    }
    $("run-panel").hidden = false;
    if (facts.blocked) {
      U.setEnabled($("btn-run"), false, "PLAN is BLOCKED — see the checks above");
    } else {
      U.setEnabled($("btn-run"), true, "");
    }
  }

  // ------------------------------------------------------------------ 5) RUN (a real job)
  $("btn-run").addEventListener("click", U.guard($("btn-run"), async () => {
    if (!paramsEqual(lastPlanParams, currentParams())) {
      showError("inputs or parameters changed since PLAN — PLAN again before RUN");
      $("run-panel").hidden = false; U.setEnabled($("btn-run"), false, "PLAN again — inputs/parameters changed");
      return;
    }
    const needsUncalibratedConfirm = mode === "campaign" && lastCalibrationPreview && lastCalibrationPreview.requires_confirmation;
    if (needsUncalibratedConfirm && !$("in-confirm-uncalibrated").checked) {
      showError("some accepted points are not COMPATIBLE with the selected profile — check the confirmation "
        + "box in section 3 (PER-POINT COMPATIBILITY) to acknowledge those points will run UNCALIBRATED, or choose a different profile");
      return;
    }
    if (!confirm("Run REDUCE on this input? Writes a new session under data/reduced. No hardware, no mount, no SDR, nothing overwritten."
      + (needsUncalibratedConfirm ? "\n\nSome points are NOT compatible with the selected profile and will be UNCALIBRATED (no partial correction)." : ""))) return;
    try {
      const params = { ...inputParams(), plan_job_id: lastPlanJobId };
      if (needsUncalibratedConfirm) params.confirm_uncalibrated = "RUN UNCALIBRATED";
      const r = await U.api(`/api/ops/start/${stageFor("run")}`, { method: "POST", body: { params, confirm: null }, timeoutMs: 30000 });
      if (!r.ok) throw new Error(U.errorText(r.error));
      U.setBadge($("run-badge"), "RUNNING");
      const jobId = r.data.data.job_id;
      pollRun(jobId);
      showError("");
    } catch (err) { showError(`RUN failed: ${msg(err)}`); }
  }, "STARTING…"));

  async function pollRun(jobId) {
    for (;;) {
      const r = await U.api(`/api/ops/job/${jobId}?tail=100`, { timeoutMs: 15000 });
      if (!r.ok) { showError(U.errorText(r.error)); return; }
      const j = r.data.data;
      renderRunJob(j);
      if (j.state !== "RUNNING") { if (j.facts && j.facts.status) await renderResults(j); return; }
      await new Promise((res) => setTimeout(res, 2000));
    }
  }

  function renderRunJob(j) {
    U.setBadge($("run-badge"), j.state === "RUNNING" ? "RUNNING" : j.verdict);
    const container = $("job-reduce-run"); container.textContent = "";
    const head = document.createElement("div"); head.className = "obs-summary";
    head.textContent = `job ${j.job_id} · ${j.state} · ${j.verdict} · elapsed ${U.hms(j.elapsed_s)}`;
    container.appendChild(head);
    const pre = document.createElement("pre"); pre.className = "job-log"; pre.textContent = j.log_tail || "";
    container.appendChild(pre);
  }

  // ------------------------------------------------------------------ 6) RESULTS
  async function renderResults(job) {
    const facts = job.facts || {};
    lastRunOutputDir = job.output_dir;
    $("results-panel").hidden = false;
    const cl = facts.calibration_level_counts || {}, vf = facts.velocity_frame_counts || {}, qc = facts.quality_counts || {};
    $("results-summary").textContent = `status: ${facts.status} · points: ${facts.points_discovered} discovered / `
      + `${facts.points_accepted} accepted / ${facts.points_completed} completed / ${facts.points_blocked} blocked / ${facts.points_failed} failed\n`
      + `calibration: ${JSON.stringify(cl)}   velocity: ${JSON.stringify(vf)}   quality: ${JSON.stringify(qc)}\n`
      + `median usable fraction: ${U.fixed(facts.median_usable_fraction, 3)}   median baseline RMS fraction: ${U.fixed(facts.median_baseline_rms_fraction, 3)}`;
    $("results-provenance").textContent = `campaign_id: ${facts.campaign_id}   reduce_session_id: ${facts.session_id}   output: ${job.output_dir || "—"}`;
    $("results-hi-caveat").textContent = "These are INSTRUMENTAL results (relative spectra, masks, quality) — no celestial HI detection or astrophysical claim is made here. That is SCIENCE's job, not REDUCE's.";

    const artifacts = $("results-artifacts"); artifacts.textContent = "";
    if (job.output_dir) {
      for (const rel of ["manifest.json", "config.json", "provenance.json", "calibration_compatibility_record.json",
        "qc/quality_summary.json", "qc/calibration_summary.json", "qc/velocity_summary.json", "qc/mask_occupancy.json"]) {
        const a = document.createElement("a"); a.href = `/api/ops/file?path=${encodeURIComponent(job.output_dir + "/" + rel)}`;
        a.target = "_blank"; a.rel = "noopener"; a.textContent = rel; a.style.marginRight = "12px";
        artifacts.appendChild(a);
      }
    }

    const tbody = document.querySelector("#results-points-table tbody"); tbody.textContent = "";
    if (job.output_dir) {
      try {
        const mr = await U.api(`/api/ops/file?path=${encodeURIComponent(job.output_dir + "/manifest.json")}`, { timeoutMs: 15000 });
        if (mr.ok) {
          for (const pt of mr.data.points || []) {
            const tr = document.createElement("tr");
            const cells = [pt.point_index, pt.status, "—", "—", "—", "—", "—", "—", "—"];
            for (const v of cells) { const td = document.createElement("td"); td.textContent = v; tr.appendChild(td); }
            const td = document.createElement("td");
            if (pt.status === "COMPLETED") {
              const btn = document.createElement("button"); btn.type = "button"; btn.textContent = "VIEW SPECTRUM";
              btn.addEventListener("click", () => showSpectrum(job.output_dir, pt.point_index));
              td.appendChild(btn);
            }
            tr.appendChild(td);
            tbody.appendChild(tr);
          }
          // The REAL, RUN-time compatibility record - written by reduce_campaign_run.py/reduce_single_capture.py
          // right before the frozen reduce_campaign() call (see reduce_calibration_record.py) - never the
          // pre-RUN preview shown in section 3, which can go stale between preview/PLAN and RUN. Absent when no
          // profile was requested at all (nothing to record).
          let runRecord = null;
          try {
            const rr = await U.api(`/api/ops/file?path=${encodeURIComponent(job.output_dir + "/calibration_compatibility_record.json")}`, { timeoutMs: 15000 });
            if (rr.ok) runRecord = rr.data;
          } catch (err) { /* no profile was used - no record written, that is expected */ }
          fillPointDetails(job.output_dir, mr.data.points || [], runRecord);
        }
      } catch (err) { /* manifest not fetchable yet - job panel above still shows the raw artifact list */ }
    }
  }

  async function fillPointDetails(outputDir, points, runRecord) {
    const rows = document.querySelectorAll("#results-points-table tbody tr");
    // Looked up by point_index from the REAL run-time record (runRecord), not the pre-RUN preview - see
    // renderResults() above and reduce_calibration_record.py for why these can legitimately differ if a file
    // changed between preview/PLAN and RUN (a case the server now refuses before it gets this far - see
    // _require_fresh_plan - but this table reflects what RUN itself actually saw, not an assumption).
    const recordByPoint = new Map((runRecord && runRecord.points || []).map((pt) => [pt.point_index, pt]));
    let i = 0;
    for (const pt of points) {
      const tr = rows[i]; i += 1;
      if (pt.status !== "COMPLETED") continue;
      const tds = tr.querySelectorAll("td");
      const compat = recordByPoint.get(pt.point_index);
      if (compat) {
        tds[4].textContent = compat.status; tds[4].className = "status-" + (compat.status === "COMPATIBLE" ? "pass" : compat.status === "UNKNOWN" ? "warning" : "block");
        tds[5].textContent = compat.reason;
      } else if (!runRecord) {
        tds[4].textContent = "—"; tds[5].textContent = "no profile was requested for this RUN (UNCALIBRATED by choice)";
      }
      try {
        const r = await U.api(`/api/ops/reduce/point?session_dir=${encodeURIComponent(outputDir)}&point_index=${pt.point_index}`, { timeoutMs: 15000 });
        if (!r.ok) continue;
        const m = r.data.data.metadata;
        tds[2].textContent = m.quality.state; tds[2].className = "status-" + m.quality.state.toLowerCase();
        tds[3].textContent = m.calibration_level;
        tds[6].textContent = m.velocity_frame;
        tds[7].textContent = U.fixed(m.quality.metrics.usable_fraction, 3);
        tds[8].textContent = U.fixed(m.quality.metrics.baseline_fit_quality_rms_fraction, 3);
      } catch (err) { /* leave as — */ }
    }
  }

  async function showSpectrum(outputDir, pointIndex) {
    try {
      const r = await U.api(`/api/ops/reduce/point?session_dir=${encodeURIComponent(outputDir)}&point_index=${pointIndex}`, { timeoutMs: 20000 });
      if (!r.ok) throw new Error(U.errorText(r.error));
      const { metadata, arrays } = r.data.data;
      $("spectrum-view").hidden = false;
      $("spectrum-point-label").textContent = pointIndex;
      drawSpectrum(arrays);
      $("spectrum-detail").textContent = `calibration_level: ${metadata.calibration_level} · velocity_frame: ${metadata.velocity_frame} · `
        + `quality: ${metadata.quality.state} (${metadata.quality.reasons.join("; ")}) · n_bins (full): ${r.data.data.n_bins_full} `
        + `(shown decimated by ${r.data.data.decimation_step}x)`;
      $("spectrum-view").scrollIntoView({ behavior: "smooth", block: "nearest" });
    } catch (err) { showError(`spectrum view failed: ${msg(err)}`); }
  }

  function drawSpectrum(arrays) {
    const canvas = $("spectrum-canvas");
    const dpr = window.devicePixelRatio || 1;
    const cssW = canvas.clientWidth || 900, cssH = 260;
    canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(cssH * dpr);
    const ctx = canvas.getContext("2d");
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    const freq = arrays.frequency_hz, val = arrays.relative_intensity || [], mask = arrays.mask;
    if (!freq || !freq.length) { ctx.fillStyle = "#91a2ad"; ctx.fillText("no data", 10, 20); return; }
    const finiteVals = val.filter((v) => Number.isFinite(v));
    if (finiteVals.length === 0) {
      // Every relative_intensity bin is null (fully masked/unavailable) - never draw a flat/fabricated line
      // through null values; say so plainly instead.
      ctx.fillStyle = "#91a2ad"; ctx.font = "13px ui-monospace,monospace";
      ctx.fillText("no relative intensity values to plot for this point (array is entirely null/masked)", 12, cssH / 2);
      return;
    }
    const vlo = Math.min(...finiteVals), vhi = Math.max(...finiteVals);
    const pad = 24;
    const x = (i) => pad + (i / (freq.length - 1)) * (cssW - 2 * pad);
    const y = (v) => cssH - pad - ((v - vlo) / Math.max(vhi - vlo, 1e-30)) * (cssH - 2 * pad);
    ctx.strokeStyle = "#2b3942"; ctx.strokeRect(pad, pad, cssW - 2 * pad, cssH - 2 * pad);
    ctx.beginPath(); ctx.strokeStyle = "#65b7d8"; ctx.lineWidth = 1;
    for (let i = 0; i < freq.length; i++) {
      const px = x(i), py = Number.isFinite(val[i]) ? y(val[i]) : y(vlo);
      if (i === 0) ctx.moveTo(px, py); else ctx.lineTo(px, py);
    }
    ctx.stroke();
    if (mask) {
      ctx.fillStyle = "rgba(230,107,107,0.15)";
      for (let i = 0; i < mask.length; i++) if (mask[i] !== 0) ctx.fillRect(x(i) - 1, pad, 2, cssH - 2 * pad);
    }
    ctx.fillStyle = "#91a2ad"; ctx.font = "10px ui-monospace,monospace";
    ctx.fillText(U.mhz(freq[0]), pad, cssH - 6);
    ctx.textAlign = "right"; ctx.fillText(U.mhz(freq[freq.length - 1]), cssW - pad, cssH - 6); ctx.textAlign = "left";
    ctx.fillText(`${vhi.toFixed(3)}`, pad + 2, pad + 10);
    ctx.fillText(`${vlo.toFixed(3)}`, pad + 2, cssH - pad - 2);
  }

  // ------------------------------------------------------------------ init
  invalidateDownstream();
  $("metadata-panel").hidden = true; $("profile-panel").hidden = true;
  loadInputs().catch((err) => showError(`could not load inputs: ${msg(err)}`));
})();
