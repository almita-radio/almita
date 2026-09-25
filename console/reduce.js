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
    const [capRes, campRes] = await Promise.all([
      U.api("/api/ops/reduce/captures", { timeoutMs: 15000 }),
      U.api("/api/ops/campaigns", { timeoutMs: 15000 }),
    ]);
    const capSel = $("in-capture"); capSel.textContent = "";
    capSel.appendChild(new Option("(choose)", ""));
    if (capRes.ok) for (const c of capRes.data.data) capSel.appendChild(new Option(`${c.path} (${U.bytes(c.size_bytes)})`, c.path));
    const campSel = $("in-campaign"); campSel.textContent = "";
    campSel.appendChild(new Option("(choose)", ""));
    if (campRes.ok) for (const c of campRes.data.data.campaigns) campSel.appendChild(new Option(c.path, c.path));
    const profSel = $("in-profile");
    const keepProfile = profSel.value;
    profSel.textContent = "";
    profSel.appendChild(new Option("(none — run UNCALIBRATED)", ""));
    if (campRes.ok) for (const pr of campRes.data.data.profiles || []) profSel.appendChild(new Option(pr.name, pr.path));
    if (keepProfile) profSel.value = keepProfile;
    $("input-note").textContent = capRes.ok && campRes.ok
      ? `${capRes.data.data.length} standalone capture(s) · ${campRes.data.data.campaigns.length} campaign(s) · ${(campRes.data.data.profiles || []).length} calibration profile(s) discovered on the server`
      : "could not discover inputs";
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
      U.setBadge($("metadata-badge"), m.points_accepted > 0 ? "OK" : "WARNING");
      cards.append(
        metaCard("CAMPAIGN ID", m.campaign_id), metaCard("ROOT", m.root),
        metaCard("POINTS DISCOVERED", m.points_discovered), metaCard("POINTS ACCEPTED", m.points_accepted, m.points_accepted === 0),
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
      $("metadata-frequency-note").textContent = m.points_accepted === 0
        ? "no accepted point has a real capture file yet — this campaign cannot be reduced until at least one point is really captured"
        : `metadata above is from a REPRESENTATIVE point (index ${m.sample_point_index}); other points share the same session config but are not individually re-checked here`;
      $("metadata-problems").hidden = !(m.points_rejected && m.points_rejected.length);
      const list = $("metadata-problems-list"); list.textContent = "";
      for (const rjr of (m.points_rejected || []).slice(0, 20)) {
        const row = document.createElement("div"); row.className = "obs-check-row status-warning";
        row.textContent = `point ${rjr.point_index}: ${rjr.reason}`; list.appendChild(row);
      }
      if ((m.points_rejected || []).length > 20) { const row = document.createElement("div"); row.className = "muted"; row.textContent = `… and ${m.points_rejected.length - 20} more`; list.appendChild(row); }
    }
  }

  // ------------------------------------------------------------------ 3) profile + compatibility
  $("in-profile").addEventListener("change", () => { selectedProfile = $("in-profile").value; invalidateDownstream(); refreshCompatibility(); });
  $("in-velocity-frame").addEventListener("change", invalidateDownstream);

  async function refreshCompatibility() {
    selectedProfile = $("in-profile").value;
    const banner = $("compat-banner"), detail = $("compat-detail");
    if (!selectedInput) { banner.hidden = true; detail.textContent = ""; return; }
    if (!selectedProfile) {
      banner.hidden = false; banner.textContent = "UNCALIBRATED (no profile selected)";
      banner.style.background = ""; banner.style.color = "var(--muted)"; banner.style.borderColor = "";
      detail.textContent = "REDUCE will run without a relative-calibration profile — a real, supported mode (reduce_engine.calibration.apply_calibration accepts profile=None). Results are labeled calibration_level=UNCALIBRATED.";
      return;
    }
    banner.hidden = false; banner.textContent = "CHECKING…"; detail.textContent = "";
    try {
      const q = mode === "capture" ? `capture=${encodeURIComponent(selectedInput)}` : `campaign_dir=${encodeURIComponent(selectedInput)}`;
      const r = await U.api(`/api/ops/reduce/compatibility?${q}&profile=${encodeURIComponent(selectedProfile)}`, { timeoutMs: 20000 });
      if (!r.ok) throw new Error(U.errorText(r.error));
      const c = r.data.data;
      const ok = c.status === "COMPATIBLE";
      banner.textContent = `${c.status}${c.basis === "campaign_representative_point" ? ` (checked against representative point ${c.sample_point_index})` : ""}`;
      banner.style.background = ok ? "#122a1e" : (c.status === "UNKNOWN" ? "" : "#2a1414");
      banner.style.color = ok ? "var(--ok)" : (c.status === "UNKNOWN" ? "var(--warn)" : "var(--error)");
      banner.style.borderColor = ok ? "#1e5c3d" : (c.status === "UNKNOWN" ? "" : "#7a2a2a");
      detail.textContent = c.reason + (c.basis === "campaign_representative_point" ? " — other points in this campaign share the same session config but are not individually verified here; the real per-point check happens again during RUN itself, honestly, for every point." : "");
    } catch (err) {
      banner.hidden = false; banner.textContent = "CHECK FAILED"; banner.style.background = "#2a1414"; banner.style.color = "var(--error)";
      detail.textContent = msg(err);
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
    $("plan-summary").textContent = `config_hash: ${facts.config_hash}\nestimated output: ~${facts.estimated_output_mb} MB\n`
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
    if (!confirm("Run REDUCE on this input? Writes a new session under data/reduced. No hardware, no mount, no SDR, nothing overwritten.")) return;
    try {
      const params = { ...inputParams(), plan_job_id: lastPlanJobId };
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
      for (const rel of ["manifest.json", "config.json", "provenance.json", "qc/quality_summary.json", "qc/calibration_summary.json", "qc/velocity_summary.json", "qc/mask_occupancy.json"]) {
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
            const cells = [pt.point_index, pt.status, "—", "—", "—", "—", "—"];
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
          fillPointDetails(job.output_dir, mr.data.points || []);
        }
      } catch (err) { /* manifest not fetchable yet - job panel above still shows the raw artifact list */ }
    }
  }

  async function fillPointDetails(outputDir, points) {
    const rows = document.querySelectorAll("#results-points-table tbody tr");
    let i = 0;
    for (const pt of points) {
      const tr = rows[i]; i += 1;
      if (pt.status !== "COMPLETED") continue;
      try {
        const r = await U.api(`/api/ops/reduce/point?session_dir=${encodeURIComponent(outputDir)}&point_index=${pt.point_index}`, { timeoutMs: 15000 });
        if (!r.ok) continue;
        const m = r.data.data.metadata;
        const tds = tr.querySelectorAll("td");
        tds[2].textContent = m.quality.state; tds[2].className = "status-" + m.quality.state.toLowerCase();
        tds[3].textContent = m.calibration_level;
        tds[4].textContent = m.velocity_frame;
        tds[5].textContent = U.fixed(m.quality.metrics.usable_fraction, 3);
        tds[6].textContent = U.fixed(m.quality.metrics.baseline_fit_quality_rms_fraction, 3);
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
    const freq = arrays.frequency_hz, val = arrays.relative_intensity, mask = arrays.mask;
    if (!freq || !freq.length) { ctx.fillStyle = "#91a2ad"; ctx.fillText("no data", 10, 20); return; }
    const finiteVals = val.filter((v) => Number.isFinite(v));
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
