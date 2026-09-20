// ALMITA OBSERVE — calls the orchestrator's local JSON API only. Never sends raw shell commands; every value is read from typed form
// fields and sent as a strict JSON object matching the observation schema. Offline: no CDN, no external requests.
// The backend is the authority: a green button never means "allowed" — START/STOP are validated (and can be refused, 409) server-side.
(function () {
  "use strict";
  const U = window.AlmitaUI;
  const $ = (id) => document.getElementById(id);
  const MONITOR_URL = `${U.consoleBase()}/`;

  U.mountHealthStrip(U.mountHeader("OBSERVE", "ORCHESTRATOR"));
  U.mountFooter();
  $("btn-monitor").href = MONITOR_URL;

  const ACTIVE = ["PREFLIGHT", "READY", "RUNNING", "STOPPING"];
  const TERMINAL = ["COMPLETED", "ABORTED", "FAILED"];
  // Orchestrator states keep their own vocabulary; this only says what each one means for the operator.
  const STATE_HELP = {
    PLANNED: "IDLE — nothing has been started", PREFLIGHT: "RUNNING — preflight checks in progress", READY: "RUNNING — starting acquisition",
    RUNNING: "RUNNING — acquisition in progress", STOPPING: "STOP REQUESTED — waiting for capture to finish",
    COMPLETED: "SUCCESS — capture finished", ABORTED: "PARTIAL — stopped before completion; captured points are kept",
    FAILED: "FAILED — see the message below", DEGRADED: "BLOCKED — needs operator attention (see the note)",
  };

  let lastPlan = null;
  let planDeadlineMs = null;
  let observationState = null;      // last orchestrator_state seen from the backend
  let stopRequestedAt = null;
  let runPoller = null;

  const showError = (err) => U.showError($("error-banner"), err);
  let pollError = false;             // the banner currently shows a POLLING failure (it may be cleared by a good poll; other errors may not)

  // ------------------------------------------------------------------ form
  function readSpec() {
    const placement = $("f-placement").value;
    const centerDecRaw = $("f-center-dec").value;
    return {
      session: { name: $("f-name").value },
      grid: {
        mode: $("f-mode").value, placement,
        center_ra_hours: placement === "FIXED_CENTER" ? Number($("f-center-ra").value) : null,
        center_dec_deg: centerDecRaw === "" ? null : Number(centerDecRaw),
        width_deg: Number($("f-width").value), height_deg: Number($("f-height").value),
        rows: Number($("f-rows").value), cols: Number($("f-cols").value),
        min_altitude_deg: Number($("f-min-alt").value), traversal: "SERPENTINE",
      },
      capture: { seconds: Number($("f-capture").value), settle_seconds: Number($("f-settle").value) },
      main: { center_frequency_hz: Number($("f-freq").value), sample_rate: Number($("f-rate").value), gain_db: Number($("f-gain").value), bias_tee: true },
      rfi_ref: { enabled: $("f-rfi-enabled").checked, serial: "00000002", gain_db: Number($("f-rfi-gain").value), bias_tee: $("f-rfi-bias-tee").checked },
      quicklook: { enabled: $("f-ql-enabled").checked, native_grid: $("f-ql-native").checked, interpolated_preview: $("f-ql-interp").checked,
                   calibration_profile_path: $("f-ql-cal").value || null },
      console: { enabled: $("f-console-enabled").checked },
      execution: { unattended: true },
    };
  }

  // ------------------------------------------------------------------ plan
  function planLines(plan) {
    const r = plan.resolved || {};
    const req = plan.requested || {};
    const cap = req.capture || {};
    const dur = plan.duration || {};
    const sto = plan.storage || {};
    return [
      `Campaign            ${plan.observation_name}`,
      `Grid                ${r.rows} x ${r.cols} = ${r.point_count} points`,
      `Footprint           ${U.deg(r.footprint_width_deg, 2)} x ${U.deg(r.footprint_height_deg, 2)}`,
      `Center              RA ${U.raHours(r.center_ra_hours)}  Dec ${U.decDeg(r.center_dec_deg)}`,
      `Spacing             ${U.deg(r.spacing_deg, 3)}`,
      `Capture / settle    ${U.num(cap.seconds, 2, "s")} / ${U.num(cap.settle_seconds, 2, "s")}`,
      "",
      `RFI_REF             ${plan.rfi_ref && plan.rfi_ref.enabled ? "ENABLED" : "DISABLED"}`,
      `  Bias-T            ${plan.rfi_ref && plan.rfi_ref.enabled ? (plan.rfi_ref.bias_tee ? "ON" : "OFF") : "N/A"}`,
      "",
      `Estimated duration  ${U.hms(dur.estimated_seconds)}  (conservative ${U.hms(dur.conservative_seconds)})`,
      `Required storage    ${U.bytes(sto.required_bytes)}   free ${U.bytes(sto.free_bytes_at_plan_time)}`,
      "",
      `Visibility          ${plan.visibility}`,
      `Min predicted alt   ${U.deg(plan.min_predicted_altitude_deg, 1)}`,
      "",
      r.placement_reasoning || "",
      "",
      "NO HARDWARE HAS MOVED.",
    ];
  }

  function startSummaryLines(plan) {
    const r = plan.resolved || {};
    const req = plan.requested || {};
    const main = plan.main || req.main || {};
    const cap = req.capture || {};
    const dur = plan.duration || {};
    return [
      `Campaign            ${plan.observation_name}`,
      `Points / captures   ${r.point_count} / ${r.point_count}  (${U.num(cap.seconds, 2, "s")} each after ${U.num(cap.settle_seconds, 2, "s")} settle)`,
      `MAIN center         ${U.mhz(main.center_frequency_hz)}`,
      `MAIN sample rate    ${U.msps(main.sample_rate)}`,
      `MAIN gain           ${U.num(main.gain_db, 1, "dB")}   Bias-T ${main.bias_tee === false ? "OFF" : "ON"}`,
      `Estimated duration  ${U.hms(dur.estimated_seconds)} (conservative ${U.hms(dur.conservative_seconds)})`,
      "",
      "MOUNT: it WILL slew (GOTO) to every point and tracking will be enabled.",
      "REQUIRES: mount aligned, clear movement path, INDI connected, MAIN SDR free.",
      "STOP sends a clean SIGINT to the capture; points already captured are kept.",
    ];
  }

  function updateStartButton() {
    const btn = $("btn-start");
    if (!lastPlan) { U.setEnabled(btn, false, "plan first"); return; }
    const preflight = lastPlan.preflight || { overall: "WARNING" };
    const blocked = lastPlan.visibility === "BLOCK" || preflight.overall === "BLOCK";
    if (blocked) return U.setEnabled(btn, false, "the plan is BLOCKED — see the preflight checks");
    if (observationState && ACTIVE.includes(observationState)) return U.setEnabled(btn, false, `an observation is already ${observationState}`);
    if (planDeadlineMs && Date.now() > planDeadlineMs) return U.setEnabled(btn, false, "the plan is stale (start-delay budget exceeded): re-plan");
    U.setEnabled(btn, true, "");
  }

  function renderPlan(plan) {
    lastPlan = plan;
    const t = Date.parse(plan.planning_timestamp_utc);
    planDeadlineMs = Number.isFinite(t) && Number.isFinite(plan.max_recommended_start_delay_minutes) ? t + plan.max_recommended_start_delay_minutes * 60000 : null;
    $("plan-summary").textContent = planLines(plan).join("\n") + (planDeadlineMs ? `\nStart before        ${U.utc(new Date(planDeadlineMs).toISOString())} (plan validity)` : "");
    $("start-summary").textContent = startSummaryLines(plan).join("\n");

    const preflight = plan.preflight || { overall: "WARNING", checks: [] };
    const blocked = plan.visibility === "BLOCK" || preflight.overall === "BLOCK";
    U.setBadge($("plan-badge"), blocked ? "BLOCKED" : (preflight.overall === "WARNING" ? "WARNING" : "READY"));

    const checksEl = $("preflight-list");
    checksEl.textContent = "";
    if (!(preflight.checks || []).length) {
      const row = document.createElement("div");
      row.className = "obs-check-row";
      row.textContent = "No preflight checks reported";
      checksEl.appendChild(row);
    }
    for (const c of preflight.checks || []) {
      const row = document.createElement("div");
      row.className = "obs-check-row " + U.stateClass(c.status);
      row.textContent = `[${c.status}] ${c.name} (${c.criticality}): ${c.detail}`;
      checksEl.appendChild(row);
    }

    const imagesEl = $("plan-images");
    imagesEl.textContent = "";
    const dirName = String(plan.grid_session_dir || "").split("/").filter(Boolean).pop();
    if (dirName) {
      for (const [label, file] of [["PLAN", "grid_plan.png"], ["COVERAGE", "grid_coverage.png"]]) {
        const wrap = document.createElement("a");
        wrap.href = `/api/observe/plan-assets/${encodeURIComponent(dirName)}/${file}`;
        wrap.target = "_blank"; wrap.rel = "noopener";
        const img = document.createElement("img");
        img.src = wrap.href; img.alt = `${label} map of the planned grid`;
        const caption = document.createElement("span");
        caption.textContent = label;
        img.addEventListener("error", () => { img.hidden = true; caption.textContent = `${label} (image unavailable)`; });
        wrap.append(img, caption);
        imagesEl.appendChild(wrap);
      }
    }
    $("plan-result").hidden = false;
    updateStartButton();
    showError("");
  }

  const submitPlan = U.guard($("btn-plan"), async () => {
    showError("");
    const r = await U.api("/api/observe/plan", { method: "POST", body: readSpec(), timeoutMs: U.timeouts.plan });
    if (!r.ok) { showError(r.error); return; }
    renderPlan(r.data);
  }, "PLANNING… (may take a minute)");

  // ------------------------------------------------------------------ run status
  function runLines(status) {
    const orch = status.orchestrator || {};
    const cs = status.current_session || null;
    const lines = [
      `orchestrator_state: ${orch.orchestrator_state || "UNKNOWN"}`,
      `session_id: ${orch.session_id || "—"}`,
      `capture_pid: ${orch.capture_pid || "—"}   quicklook_pid: ${orch.quicklook_pid || "—"}`,
    ];
    if (orch.capture_process_alive === false && ACTIVE.includes(orch.orchestrator_state)) lines.push("WARNING: the recorded capture process is NOT alive");
    if (orch.note) lines.push(`note: ${orch.note}`);
    if (cs) {
      const done = Number(cs.point_current), total = Number(cs.points_total), ok = Number(cs.points_success);
      lines.push("", "acquisition:",
        `  state: ${cs.state}   session: ${cs.session_name || "—"}`,
        `  point index: ${U.finite(done) ? done : "—"} / ${U.finite(total) ? total : "—"}   current point id: ${cs.current_point_id || "—"}`,
        `  succeeded: ${cs.points_success ?? 0}   failed: ${cs.points_failed ?? 0}   deferred: ${cs.points_deferred ?? 0}`,
        `  last completed point: ${cs.last_successful_point_id || "—"} at ${U.utc(cs.last_capture_utc)}`,
        `  started: ${U.utc(cs.started_utc)}   updated: ${U.utc(cs.updated_utc)}`);
      const started = Date.parse(cs.started_utc);
      const live = ACTIVE.includes(orch.orchestrator_state);
      if (Number.isFinite(started)) {
        const elapsed = ((live ? Date.now() : Date.parse(cs.updated_utc) || Date.now()) - started) / 1000;
        lines.push(`  elapsed: ${U.hms(elapsed)}`);
        // No invented ETA: an estimate is only shown after enough points, labelled as an estimate from the average pace.
        if (live && ok >= 5 && total > ok) lines.push(`  ~remaining (estimate from the average pace, not a promise): ${U.hms((elapsed / ok) * (total - ok))}`);
      }
      const age = (Date.now() - Date.parse(cs.updated_utc)) / 1000;
      if (live && Number.isFinite(age) && age > 90) lines.push(`  WARNING: no progress update for ${U.hms(age)} (STALE)`);
    }
    return lines;
  }

  function renderRunStatus(status) {
    const orch = status.orchestrator || {};
    const state = orch.orchestrator_state || "UNKNOWN";
    observationState = state;
    U.setBadge($("run-badge"), state);
    const cs = status.current_session || {};
    const total = Number(cs.points_total), success = Number(cs.points_success);
    let kind = STATE_HELP[state] || state;
    if (TERMINAL.includes(state) && U.finite(total) && U.finite(success) && success < total) kind = `PARTIAL — ${success} of ${total} points captured (${state})`;
    if (!ACTIVE.includes(state) && state !== "PLANNED") kind = "LAST RUN — " + kind;
    $("run-kind").textContent = kind;
    const wrap = $("run-progress-wrap");
    if (U.finite(total) && total > 0) {
      wrap.hidden = false;
      $("run-progress").value = Math.max(0, Math.min(100, (100 * (U.finite(success) ? success : 0)) / total));
      $("run-progress-text").textContent = ` ${U.finite(success) ? success : 0} / ${total} points`;
    } else {
      wrap.hidden = true;
    }
    $("run-summary").textContent = runLines(status).join("\n");
    const stopBtn = $("btn-stop");
    if (state === "STOPPING") U.setEnabled(stopBtn, false, "stop already requested — waiting for the capture to finish");
    else if (!ACTIVE.includes(state)) U.setEnabled(stopBtn, false, "no observation is running");
    else U.setEnabled(stopBtn, true, "");
    const note = $("stop-note");
    if (stopRequestedAt && state === "STOPPING") { note.hidden = false; note.textContent = `STOP REQUESTED at ${U.utc(stopRequestedAt)} — the capture is still shutting down (this is not completion).`; }
    else if (stopRequestedAt && TERMINAL.includes(state)) { note.hidden = false; note.textContent = `Stop completed: capture ended as ${state} (requested ${U.utc(stopRequestedAt)}).`; stopRequestedAt = null; }
    else if (state !== "STOPPING" && !stopRequestedAt) { note.hidden = true; }
    updateStartButton();
    // A terminal state ends live polling (the page keeps showing it as LAST RUN); DEGRADED keeps polling: it may need the operator.
    if (TERMINAL.includes(state) && runPoller) { runPoller.stop(); runPoller = null; }
  }

  function showRunPanel() {
    $("run-status").hidden = false;
    $("plan-result").hidden = true;
  }

  function pollRun() {
    if (runPoller) runPoller.stop();
    runPoller = U.poller(async () => {
      const r = await U.api("/api/observe/status", { timeoutMs: 8000 });
      if (!r.ok) { showError(r.error); pollError = true; return false; }
      if (pollError) { showError(""); pollError = false; }
      renderRunStatus(r.data);
      return true;
    }, { intervalMs: 2000, hiddenIntervalMs: 15000 });
    runPoller.start();
  }

  // ------------------------------------------------------------------ start / stop (dangerous actions: explicit summary + confirm + no double submit)
  const startObservation = U.guard($("btn-start"), async () => {
    if (!lastPlan) return;
    const summary = startSummaryLines(lastPlan).join("\n");
    if (!window.confirm(`START OBSERVATION now?\n\n${summary}`)) return;
    showError("");
    const r = await U.api("/api/observe/start", { method: "POST", timeoutMs: U.timeouts.start,
      body: { resolved_plan_path: lastPlan._resolved_plan_path, confirm: true } });
    if (!r.ok) {
      // a timeout/network error on START does NOT prove it did not start: say so, and look at the real state
      const unsure = r.error.kind === "timeout" || r.error.kind === "network";
      showError(unsure ? { ...r.error, message: r.error.message + " — START may have taken effect: check the run status below" } : r.error);
      if (unsure) { showRunPanel(); pollRun(); }
      return;
    }
    showRunPanel();
    stopRequestedAt = null;
    pollRun();
  }, "STARTING…");

  const stopObservation = U.guard($("btn-stop"), async () => {
    if (!window.confirm("STOP OBSERVATION now?\n\nThis sends SIGINT to the running capture (clean stop). Points already captured are kept.\n"
                        + "It can take up to two minutes for the capture to finish closing its files.")) return;
    showError("");
    stopRequestedAt = new Date().toISOString();
    $("stop-note").hidden = false;
    $("stop-note").textContent = `STOP REQUESTED at ${U.utc(stopRequestedAt)} — waiting for the capture to finish (this is not completion).`;
    pollRun();                                   // watch the real state (STOPPING -> ABORTED/COMPLETED) while the stop request is pending
    const r = await U.api("/api/observe/stop", { method: "POST", body: { confirm: true }, timeoutMs: U.timeouts.stop });
    if (!r.ok) { showError(r.error); if (r.error.kind === "http") stopRequestedAt = null; return; }
    renderRunStatus({ orchestrator: r.data, current_session: null });
  }, "STOP REQUESTED…");

  // ------------------------------------------------------------------ defaults and startup
  async function loadDefaults() {
    const r = await U.api("/api/observe/defaults", { timeoutMs: 8000 });
    if (!r.ok) return;            // keep the static defaults; the header LINK chip shows when the backend is unreachable
    const d = r.data;
    $("f-freq").value = d.main.center_frequency_hz;
    $("f-rate").value = d.main.sample_rate;
    $("f-gain").value = d.main.gain_db;
    $("f-min-alt").value = d.grid.min_altitude_deg;
    $("f-rfi-gain").value = d.rfi_ref.gain_db;
  }

  function updateRfiBiasTeeAvailability() { $("f-rfi-bias-tee").disabled = !$("f-rfi-enabled").checked; }

  $("f-placement").addEventListener("change", (e) => { $("row-center-ra").hidden = e.target.value !== "FIXED_CENTER"; });
  $("f-rfi-enabled").addEventListener("change", updateRfiBiasTeeAvailability);
  updateRfiBiasTeeAvailability();
  $("observe-form").addEventListener("submit", submitPlan);
  $("btn-replan").addEventListener("click", () => { $("plan-result").hidden = true; lastPlan = null; updateStartButton(); });
  $("btn-start").addEventListener("click", startObservation);
  $("btn-stop").addEventListener("click", stopObservation);
  U.setEnabled($("btn-start"), false, "plan first");
  U.setEnabled($("btn-stop"), false, "no observation is running");

  loadDefaults();
  // Page reload during a run: rebuild the state from the backend (the run is server-side and never depended on this page).
  (async function recover() {
    const r = await U.api("/api/observe/status", { timeoutMs: 8000 });
    if (!r.ok) { showError(r.error); return; }
    const state = ((r.data || {}).orchestrator || {}).orchestrator_state;
    observationState = state || null;
    if (ACTIVE.includes(state) || state === "DEGRADED") { showRunPanel(); pollRun(); }
    else if (TERMINAL.includes(state)) { $("run-status").hidden = false; renderRunStatus(r.data); }
    updateStartButton();
  })();
  window.addEventListener("pagehide", () => { if (runPoller) runPoller.stop(); });
})();
