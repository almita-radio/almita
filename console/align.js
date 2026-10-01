// ALMITA ALIGN — calls the same-origin ALIGN API only. All fit/offset/quality/sync-eligibility numbers come from the backend
// (alignment_engine) - this file only presents, orchestrates, and polls. No science here. The REAL ALIGNMENT panel runs alignment.py on the real mount + MAIN (typed MOVE); the sections below are the simulation.
(function () {
  "use strict";
  const U = window.AlmitaUI;
  const $ = (id) => document.getElementById(id);
  U.mountHealthStrip(U.mountHeader("ALIGN", "ALIGNMENT ASSISTANT"));
  U.mountFooter();

  let mode = "solar";
  let currentSessionId = null;
  let sessionPoller = null;
  let pollError = false;

  const showError = (m) => U.showError($("error-banner"), m);
  async function getJSON(path) {
    const r = await U.api(path);
    if (!r.ok) throw new Error(U.errorText(r.error));
    return r.data;
  }
  async function postJSON(path, body, timeoutMs) {
    const r = await U.api(path, { method: "POST", body: body || {}, timeoutMs: timeoutMs || 30000 });
    if (!r.ok) throw new Error(U.errorText(r.error));
    return r.data;
  }
  const msg = (err) => (err && err.message) || String(err);

  $("btn-run-real").addEventListener("click", () => { $("real-panel").scrollIntoView(); $("real-confirm").focus(); });

  function setDisabledReasons() {
    U.setEnabled($("btn-preflight"), !!currentSessionId, "plan first");
    if (!$("btn-run-sim").dataset.running) U.setEnabled($("btn-run-sim"), $("btn-run-sim").dataset.preflightOk === "1", "run PREFLIGHT first (it must pass)");
    U.setEnabled($("btn-run-real"), true, "");        // real alignment is run from the PIPELINE page (alignment.py, typed MOVE confirmation, real preflight)
    for (const id of ["btn-sync-prepare", "btn-sync-apply"]) U.setEnabled($(id), false, "SYNC is blocked by policy (see the SYNC status)");
  }

  async function refreshStatus() {
    try {
      const status = await getJSON("/api/align/status");
      const d = status.data || {};
      $("st-deployment").textContent = d.deployment_state || "—";
      $("st-active-observation").textContent = (d.resource || {}).orchestrator_state || "UNKNOWN";
      $("st-resource").textContent = (d.resource || {}).status || "—";
      $("st-hi-phase").textContent = (d.hi || {}).phase || "—";
      $("st-hi-sync").textContent = ((d.hi || {}).sync || {}).sync_allowed ? "ALLOWED" : "BLOCKED";
      $("st-solar-sync").textContent = (d.solar || {}).sync_authorized ? "ALLOWED" : "BLOCKED";
      renderSessionList((d.sessions || {})[mode] || []);
      if (pollError) { showError(""); pollError = false; }
      return true;
    } catch (err) {
      showError(`Status request failed: ${msg(err)}`);
      pollError = true;
      return false;
    }
  }

  function renderSessionList(sessions) {
    const el = $("session-list");
    el.textContent = "";
    if (!sessions.length) {
      const row = document.createElement("div");
      row.className = "obs-check-row";
      row.textContent = "NO ALIGNMENT SESSION";
      el.appendChild(row);
      return;
    }
    for (const s of sessions) {
      const row = document.createElement("div");
      row.className = "obs-check-row";
      row.textContent = `${s.session_id}  [${s.phase}]`;
      el.appendChild(row);
    }
  }

  function setMode(newMode) {
    mode = newMode;
    $("mode-solar").classList.toggle("active", mode === "solar");
    $("mode-hi").classList.toggle("active", mode === "hi");
    $("mode-solar").setAttribute("aria-pressed", String(mode === "solar"));
    $("mode-hi").setAttribute("aria-pressed", String(mode === "hi"));
    $("mode-title").textContent = mode === "solar" ? "SOLAR ALIGNMENT" : "HI ALIGNMENT";
    $("hi-replay-panel").hidden = mode !== "hi";
    $("hi-compare-panel").hidden = mode !== "hi";
    $("mode-body").textContent = "Not planned yet - click PLAN.";
    U.setBadge($("mode-badge"), "IDLE");
    $("btn-run-sim").dataset.preflightOk = "";
    $("preflight-checks").textContent = "";
    $("result-panel").hidden = true;
    if (sessionPoller) { sessionPoller.stop(); sessionPoller = null; }
    currentSessionId = null;
    setDisabledReasons();
    refreshStatus();
  }

  async function doPlan() {
    try {
      const res = await postJSON(`/api/align/plan/${mode}`, {});
      if (res.blocked) { showError(res.reason || "PLAN blocked"); return; }
      currentSessionId = res.data.session_id;
      U.setBadge($("mode-badge"), res.data.state);
      $("mode-body").textContent = `Session: ${currentSessionId}\nState: ${res.data.state}`;
      $("btn-run-sim").dataset.preflightOk = "";
      setDisabledReasons();
      showError("");
    } catch (err) { showError(`PLAN failed: ${msg(err)}`); }
  }

  async function doPreflight() {
    if (!currentSessionId) return;
    try {
      const res = await postJSON(`/api/align/preflight/${mode}`, { session_id: currentSessionId }, U.timeouts.preflight);
      const checksEl = $("preflight-checks");
      checksEl.textContent = "";
      const checks = (res.data || {}).checks || [];
      if (!checks.length) {
        const row = document.createElement("div");
        row.className = "obs-check-row";
        row.textContent = "No preflight checks reported";
        checksEl.appendChild(row);
      }
      for (const c of checks) {
        const row = document.createElement("div");
        row.className = "obs-check-row " + (c.ok ? "status-pass" : "status-block");
        row.textContent = `[${c.ok ? "PASS" : "BLOCKED"}] ${c.name}: ${c.detail}`;
        checksEl.appendChild(row);
      }
      const ready = !res.blocked;
      U.setBadge($("mode-badge"), ready ? "READY" : "BLOCKED");
      $("btn-run-sim").dataset.preflightOk = ready ? "1" : "";
      setDisabledReasons();
      showError(ready ? "" : (res.reason || "preflight blocked"));
    } catch (err) { showError(`PREFLIGHT failed: ${msg(err)}`); }
  }

  async function doRunSimulation() {
    if (!currentSessionId) return;
    try {
      const res = await postJSON(`/api/align/run/${mode}`, { session_id: currentSessionId, true_offset_east: 0, true_offset_north: 0 });
      if (res.blocked) { showError(res.reason || "RUN blocked"); return; }
      showError("");
      pollSession();
    } catch (err) { showError(`RUN failed: ${msg(err)}`); }
  }

  function pollSession() {
    if (sessionPoller) sessionPoller.stop();
    const sid = currentSessionId;
    const btn = $("btn-run-sim");
    btn.dataset.running = "1";
    U.setEnabled(btn, false, "a simulation is running");
    sessionPoller = U.poller(async () => {
      const r = await U.api(`/api/align/session/${encodeURIComponent(sid)}`, { timeoutMs: 10000 });
      if (!r.ok) { showError(r.error); pollError = true; return false; }
      const res = r.data;
      if (res.blocked) return true;                     // session not visible yet: keep waiting
      if (pollError) { showError(""); pollError = false; }
      renderResult(res.data);
      if (!res.data.job_running) {
        if (sessionPoller) sessionPoller.stop();
        sessionPoller = null;
        delete btn.dataset.running;
        setDisabledReasons();
      }
      return true;
    }, { intervalMs: 2000, hiddenIntervalMs: 15000 });
    sessionPoller.start();
  }

  function renderResult(data) {
    $("result-panel").hidden = false;
    $("simulation-flag").hidden = false;                // every run_simulation result IS a simulation
    const state = data.state || {};
    const result = data.result;
    U.setBadge($("result-badge"), state.state || "RUNNING");
    if (!result) {
      $("result-summary").textContent = `State: ${state.state || "running"}...`;
      return;
    }
    const fit = result.final_fit || {};
    const lines = [
      `East offset:  ${U.fixed(result.tangent_east_deg, 4, "deg")}`,
      `North offset: ${U.fixed(result.tangent_north_deg, 4, "deg")}`,
      `Quality: ${((fit.quality || {}).rating) || "—"}  confidence=${U.fixed((fit.quality || {}).confidence, 4)}`,
    ];
    if (fit.refinement) {
      lines.push(`Uncertainty east:  ${U.fixed(fit.refinement.uncertainty_east_deg, 4, "deg")}`);
      lines.push(`Uncertainty north: ${U.fixed(fit.refinement.uncertainty_north_deg, 4, "deg")}`);
    }
    if (result.quality) lines.push("", "POINTING QUALITY (bootstrap):", `  verdict: ${result.quality.verdict}`);
    $("result-summary").textContent = lines.join("\n");
    $("sync-status").textContent = mode === "hi" ? "SYNC BLOCKED — FIRST_LIGHT_HI requires physical repeatability before correction"
                                                 : "SYNC BLOCKED — real solar SYNC not yet authorized";
  }

  async function doReplay() {
    const sessionDir = $("replay-session-dir").value.trim();
    if (!sessionDir) { showError("REPLAY: enter a session directory (data/alignment/HI-...)"); return; }
    try {
      const res = await postJSON("/api/align/replay", { session_dir: sessionDir }, 60000);
      $("replay-summary").textContent = res.blocked ? `BLOCKED: ${res.reason}` : JSON.stringify(res.data || res, null, 2);
      showError("");
    } catch (err) { showError(`REPLAY failed: ${msg(err)}`); }
  }

  async function doCompare() {
    const a = $("compare-a").value.trim();
    const b = $("compare-b").value.trim();
    if (!a || !b) { showError("COMPARE: enter both analysis directories"); return; }
    try {
      const res = await postJSON("/api/align/compare", { analysis_dir_a: a, analysis_dir_b: b }, 60000);
      $("compare-summary").textContent = res.blocked ? `BLOCKED: ${res.reason}` : JSON.stringify(res.data || res, null, 2);
      showError("");
    } catch (err) { showError(`COMPARE failed: ${msg(err)}`); }
  }

  // Page reload during a run: adopt a session whose job is still running on the server instead of assuming IDLE.
  async function recover() {
    for (const m of ["solar", "hi"]) {
      try {
        const status = await getJSON("/api/align/status");
        const newest = (((status.data || {}).sessions || {})[m] || [])[0];
        if (!newest) continue;
        const r = await U.api(`/api/align/session/${encodeURIComponent(newest.session_id)}`, { timeoutMs: 10000 });
        if (r.ok && r.data && r.data.data && r.data.data.job_running) {
          setMode(m);
          currentSessionId = newest.session_id;
          U.setBadge($("mode-badge"), "RUNNING");
          $("mode-body").textContent = `Session: ${currentSessionId}\nA run is in progress on the server (page reloaded).`;
          pollSession();
          return;
        }
      } catch (err) { /* recovery is best effort; the normal status poll reports connection problems */ }
    }
  }

  $("mode-solar").addEventListener("click", () => setMode("solar"));
  $("mode-hi").addEventListener("click", () => setMode("hi"));
  $("btn-plan").addEventListener("click", U.guard($("btn-plan"), doPlan, "PLANNING…"));
  $("btn-preflight").addEventListener("click", U.guard($("btn-preflight"), doPreflight, "CHECKING…"));
  $("btn-run-sim").addEventListener("click", U.guard($("btn-run-sim"), doRunSimulation, "STARTING…"));
  $("btn-replay").addEventListener("click", U.guard($("btn-replay"), doReplay, "REPLAYING…"));
  $("btn-compare").addEventListener("click", U.guard($("btn-compare"), doCompare, "COMPARING…"));

  setMode("solar");
  U.poller(refreshStatus, { intervalMs: 10000 }).start();
  recover();
  window.addEventListener("pagehide", () => { if (sessionPoller) sessionPoller.stop(); });

  // ------------------------------------------------------------------ REAL ALIGNMENT (alignment.py through /api/ops; the backend decides the verdict)
  {
    U.mountReadout($("real-mount-kv"), $("real-mount-badge"), 5000);
    const realErr = (e) => U.showError($("error-banner"), e);
    // onChange fires on every poll of this job (including the very first, while it is still RUNNING, and the
    // final one that detects PASS) - it must never reset selectedArea itself. It used to ("selectedArea = null;"
    // here), which cleared the operator's HI candidate pick within ~2s of clicking PLAN, well before the job
    // even finished: planParams() then stopped including center_ra_hours/center_dec_deg, while the job's own
    // recorded params (posted at click time) still had them, so U.paramsEqual() found a mismatch and the UI
    // showed PLAN OBSOLETE immediately on PASS - reproduced live on real HI-mode PLANs, never on solar (solar's
    // selectedCenter() is always null regardless of selectedArea, so this had no effect there). runJob's own
    // onChange below never touched selectedArea - this now matches that same, correct pattern.
    const planJob = U.jobPanel($("real-job-align_plan"), null, { onError: realErr, onChange: () => updateReal() });
    const runJob = U.jobPanel($("real-job-align"), $("real-badge"), { onError: realErr, onChange: () => updateReal() });
    let mode = "hi";              // "hi" | "solar" - the operator's explicit choice; never switched automatically
    let selectedArea = null;      // HI only: "A" | "B" | "C" (the operator's pick); solar always uses the Sun itself
    let sky = null;               // last real /api/ops/align/sky response
    let durationModel = null;     // from /api/ops/align/defaults
    // How long an approved (EXITED, PASS) PLAN stays valid for RUN, in seconds - read from the server
    // (align_defaults()'s own plan_validity_seconds) rather than hardcoded here, so the countdown shown below
    // can never drift from what almita_web_ops.py's build_command() actually enforces server-side when RUN is
    // submitted (the real, authoritative check - this page's own countdown is display-only).
    let planValiditySeconds = null;
    // {mode, selectedArea} exactly as they were at the moment the operator clicked PLAN (set synchronously in the
    // click handler below, so there is no race window - JS is single-threaded and nothing else writes mode/
    // selectedArea except an operator click). Used only to decide identity ("is the candidate I approved still the
    // one selected"), never to re-derive its coordinates - those always come from the PLAN job's own recorded
    // params (see comparisonParams()). This is what lets an approved HI PLAN survive the 30s auto refreshSky()
    // below (which recomputes A/B/C's coordinates, not their identity) without expiring on its own.
    let planApprovedFor = { mode: null, selectedArea: null };

    function ringConfig() { return U.ringConfig($("real-ring-radii"), $("real-ring-points")); }

    async function loadDefaults() {
      const r = await U.api("/api/ops/align/defaults", { timeoutMs: 15000 });
      if (!r.ok) { realErr(r.error); return; }
      const d = r.data.data;
      durationModel = d.duration_model;
      planValiditySeconds = d.plan_validity_seconds;
      $("real-ring-radii").value = d.ring_radii_deg.join(",");
      $("real-ring-points").value = d.ring_points;
      $("real-capture-time").value = d.capture_time_s;
      $("real-beam-fwhm").value = d.beam_fwhm_deg;
      $("real-min-elevation").value = d.min_elevation_deg;
      $("real-beam-note").textContent = `source: ${d.beam_fwhm_source}. ${d.beam_fwhm_note}`;
      updateReal();
    }

    function setMode(next) {
      mode = next; selectedArea = null;
      $("real-mode-hi").className = "mode-btn" + (mode === "hi" ? " active" : "");
      $("real-mode-solar").className = "mode-btn" + (mode === "solar" ? " active" : "");
      $("real-track-mode-row").hidden = mode !== "solar";
      refreshSky();
    }
    $("real-mode-hi").addEventListener("click", () => setMode("hi"));
    $("real-mode-solar").addEventListener("click", () => setMode("solar"));

    async function refreshSky() {
      const rc = ringConfig();
      const radii = rc.valid ? rc.radii : [5.0, 2.0, 0.6];
      const points = rc.valid ? rc.points : 16;
      // ring_points and capture_time both matter here: the candidate check now walks the WHOLE ring pattern
      // forward through the real wall-clock schedule of a run (not just an instant), so it needs to know how many
      // points per ring AND how long each capture takes to know when each point would actually be captured.
      const q = `mode=${mode}&ring_radii=${radii.join(",")}&ring_points=${points}&min_elevation=${$("real-min-elevation").value || 20}&beam_fwhm=${$("real-beam-fwhm").value || 20}&capture_time=${$("real-capture-time").value || 20}`;
      const r = await U.api(`/api/ops/align/sky?${q}`, { timeoutMs: 20000 });
      if (!r.ok) { $("real-sky-note").textContent = "sky view unavailable: " + U.errorText(r.error); return; }
      sky = r.data.data;
      if (selectedArea && !(sky.areas || []).some((a) => a.label === selectedArea)) selectedArea = null;
      drawSky();
    }

    function drawSky() {
      if (!sky) return;
      U.drawSkyView($("real-sky"), sky, { selected: mode === "solar" ? "SUN" : selectedArea, onPick: mode === "hi" ? (label) => { selectedArea = label; drawSky(); renderAreas(); updateReal(); } : null });
      if ($("real-sky-legend")) {
        if (mode === "hi" && sky.hi4pi_grid) { $("real-sky-legend").hidden = false; U.drawHi4piLegend($("real-sky-legend"), sky.hi4pi_grid.value_range_1e20cm2, "N_HI (cm⁻²)"); }
        else { $("real-sky-legend").hidden = true; }
      }
      const parts = [`sky computed ${U.utc(sky.computed_utc)} · Sun: az ${sky.sun.az_deg.toFixed(1)}° alt ${sky.sun.alt_deg.toFixed(1)}° ${sky.sun.above_horizon ? "(up)" : "(below horizon)"}`];
      if (mode === "hi") {
        if (sky.catalog_source) {
          const cs = sky.catalog_source;
          parts.push(`HI map: ${cs.name} - ${cs.product} - ${cs.citation} - unit ${cs.unit} - NOT an Almita measurement`);
        } else if (sky.hi4pi_error) {
          parts.push(`HI map unavailable: ${sky.hi4pi_error}`);
        }
      }
      parts.push(sky.ranking_note || "");
      $("real-sky-note").textContent = parts.join(" · ");
      renderAreas();
    }

    function renderAreas() {
      const box = $("real-hi-areas");
      box.textContent = "";
      if (mode !== "hi" || !sky) return;
      if (!sky.ranking_defendible || !(sky.areas || []).length) {
        box.appendChild(document.createTextNode("No defendible HI ranking right now (" + (sky.ranking_note || "reason unavailable") + "). "));
        const b = document.createElement("button"); b.type = "button"; b.textContent = "USE MANUAL HI ALIGN (no suggested area)";
        b.addEventListener("click", () => { selectedArea = "MANUAL"; drawSky(); updateReal(); });
        box.appendChild(b);
        return;
      }
      for (const a of sky.areas) {
        const row = document.createElement("div"); row.className = "obs-check-row";
        const b = document.createElement("button"); b.type = "button"; b.textContent =
          `${a.label}: az ${a.az_deg.toFixed(1)}° alt ${a.alt_deg.toFixed(1)}° · score ${a.score.toFixed(2)} (contrast ${a.contrast_1e20cm2.toFixed(3)}, mean ${a.mean_1e20cm2.toFixed(3)} ×10²⁰ cm⁻²)` +
          U.temporalMarginText(a);
        b.className = selectedArea === a.label ? "mode-btn active" : "mode-btn";
        b.addEventListener("click", () => { selectedArea = a.label; drawSky(); updateReal(); });
        row.appendChild(b);
        box.appendChild(row);
      }
    }
    $("real-sky-refresh").addEventListener("click", U.guard($("real-sky-refresh"), refreshSky, "..."));

    function selectedCenter() {
      if (mode !== "hi" || selectedArea === "MANUAL" || !sky) return null;
      const a = (sky.areas || []).find((x) => x.label === selectedArea);
      return a ? { ra_hours: a.ra_hours, dec_deg: a.dec_deg } : null;
    }

    function estimateDuration(n) {
      if (!durationModel) return null;
      const ct = Number($("real-capture-time").value) || 20;
      const acquire = n * (durationModel.overhead_per_point_s + ct);
      const analysis = mode === "hi"
        ? durationModel.ensemble_fixed_s + n * (durationModel.psd_per_20s_capture_s + durationModel.metric_per_20s_capture_s) * (ct / 20)
        : n * durationModel.solar_metric_per_20s_capture_s * (ct / 20);
      return acquire + analysis;
    }

    function updateReal() {
      const p = planJob.last, r = runJob.last;
      const exited = p && p.state === "EXITED";
      // Whether the last PLAN's own recorded request params (server-side, verbatim) still match what's currently
      // configured/selected - independent of whether that PLAN PASSed or FAILed (e.g. on its own temporal check).
      // If the ring/capture/beam/min-elevation params changed, or the operator explicitly picked a different A/B/C
      // area or switched mode since, the plan is OBSOLETE regardless of its verdict - it must never be shown as if
      // it still describes the current selection. comparisonParams() (not planParams()) is used here so that the
      // periodic refreshSky() below - which only recomputes A/B/C's live coordinates, not what's selected - can
      // never invalidate an approved PLAN on its own; only an explicit operator change does.
      const paramsMatch = exited && U.paramsEqual(p.params, comparisonParams());
      const stale = exited && !paramsMatch;
      // PLAN_VALIDITY_SECONDS countdown - DISPLAY ONLY. The server independently re-derives and enforces the
      // SAME deadline, from the SAME server-recorded p.ended_utc (when that PLAN job's real subprocess
      // exited), the moment RUN is actually submitted - see almita_web_ops.py build_command()'s "align" stage.
      // Nothing here is authoritative; it exists only so the operator can see time passing without needing to
      // guess almita_web_ops.py's own PLAN_VALIDITY_SECONDS. Timed from ended_utc, never from when the operator
      // happened to click PLAN or load this page, and never reset by refreshSky()'s periodic redraw below,
      // which never touches planJob at all.
      const passedFresh = exited && paramsMatch && p.verdict === "PASS" && p.ended_utc && planValiditySeconds != null;
      const ageS = passedFresh ? (Date.now() - Date.parse(p.ended_utc)) / 1000 : null;
      const expired = passedFresh && ageS > planValiditySeconds;
      const remainingS = passedFresh ? Math.max(0, planValiditySeconds - ageS) : null;
      const planned = exited && p.verdict === "PASS" && paramsMatch && !expired;
      const running = r && r.state === "RUNNING";
      const rc = ringConfig();
      const n = rc.valid ? 1 + rc.radii.length * rc.points : null;
      const needsArea = mode === "hi";
      const areaOk = !needsArea || !!selectedArea;
      const sunOk = mode !== "solar" || (sky && sky.sun.above_horizon);
      U.setEnabled($("real-plan"), rc.valid && areaOk && sunOk,
        !rc.valid ? "fix the ring radii / points above" : !areaOk ? "pick area A, B, C or MANUAL above" : "the Sun is below the horizon right now");
      U.setEnabled($("real-run"), !!planned && $("real-confirm").value === "MOVE" && !running,
        running ? "an alignment is running" : expired ? `the PLAN's ${planValiditySeconds}s validity window has passed - PLAN again`
          : stale ? "the PLAN is obsolete (parameters or the selected area changed) - PLAN again"
          : !planned ? "run the real PLAN first (it must PASS)" : "type MOVE (uppercase) to allow movement");
      U.renderRingPatternTotal(rc, $("real-pattern-total"));
      if (n) {
        const secs = estimateDuration(n);
        $("real-duration-estimate").textContent = secs ? `estimated duration: ~${Math.round(secs / 60)} min (${Math.round(secs)} s) for ${n} positions — movement+settle+capture and both analysis passes; ` +
          "see REAL PREFLIGHT panel note for the real runs this is based on" : "";
      }
      const staleBox = $("real-plan-stale");
      if (exited && paramsMatch) {
        if (staleBox) staleBox.hidden = true;
        U.renderAlignPreview(p.facts, $("real-preview"), $("real-preview-summary"), "#real-preview-table",
          { computedUtc: p.started_utc, areaLabel: mode === "hi" ? selectedArea : (mode === "solar" ? "SUN" : null) },
          $("real-preview-temporal"));
      } else {
        $("real-preview").hidden = true;
        if (staleBox) {
          if (stale) {
            staleBox.hidden = false;
            const c = p.facts && p.facts.center;
            staleBox.textContent = `PLAN OBSOLETE — computed ${U.utc(p.started_utc)}` +
              (c ? ` for centre RA ${Number(c.ra_hours).toFixed(4)} h / Dec ${Number(c.dec_deg).toFixed(3)}°` : "") +
              ". Current parameters or the selected area no longer match that PLAN — it is hidden and RUN is disabled. PLAN again before RUN.";
          } else {
            staleBox.hidden = true;
          }
        }
      }
      const validityBox = $("real-plan-validity");
      if (validityBox) {
        if (passedFresh && expired) {
          validityBox.hidden = false;
          validityBox.style.color = "var(--warn,#e6b85c)";
          validityBox.textContent = `PLAN EXPIRED — more than ${planValiditySeconds}s have passed since it finished with PASS ` +
            `(${U.utc(p.ended_utc)}). RUN is disabled: PLAN again before RUN.`;
        } else if (passedFresh) {
          validityBox.hidden = false;
          validityBox.style.color = "";
          const expiresUtc = U.utc(new Date(Date.parse(p.ended_utc) + planValiditySeconds * 1000).toISOString());
          validityBox.textContent = `PLAN valid for ${Math.ceil(remainingS)}s more (finished ${U.utc(p.ended_utc)}; expires ${expiresUtc}). ` +
            "If RUN starts before then, it is allowed to finish even if this window elapses during the run.";
        } else {
          validityBox.hidden = true;
        }
      }
    }
    $("real-confirm").addEventListener("input", updateReal);
    $("real-ring-radii").addEventListener("input", () => { refreshSky(); updateReal(); });
    $("real-ring-points").addEventListener("input", () => { refreshSky(); updateReal(); });
    $("real-capture-time").addEventListener("input", () => { refreshSky(); updateReal(); });
    $("real-min-elevation").addEventListener("input", () => { refreshSky(); updateReal(); });
    $("real-beam-fwhm").addEventListener("input", () => { refreshSky(); updateReal(); });

    U.poller(async () => {
      if (mode !== "solar") return true;
      const r = await U.api("/api/ops/mount", { timeoutMs: 10000 });
      if (!r.ok) return false;
      $("real-track-mode").textContent = (r.data.data.track_mode || "unknown").toUpperCase();
      return true;
    }, { intervalMs: 5000 }).start();

    $("real-preflight").addEventListener("click", U.guard($("real-preflight"), async () => {
      const r = await U.api("/api/ops/preflight", { method: "POST", body: {}, timeoutMs: U.timeouts.preflight });
      if (!r.ok) { realErr(r.error); return; }
      realErr(null);
      const d = r.data.data, out = $("real-preflight-out");
      out.hidden = false;
      out.textContent = `REAL PREFLIGHT ${d.overall} (${U.utc(d.generated_utc)})\n` + d.checks.map((c) => `[${c.status}] ${c.name}: ${c.detail}`).join("\n");
    }, "CHECKING…"));

    // Shared by planParams() (what to actually POST) and comparisonParams() (what to compare an approved PLAN
    // against) so both always build identical key order for U.paramsEqual()'s plain JSON.stringify comparison.
    function buildParams(center) {
      const rc = ringConfig();
      const p = { reference: mode === "solar" ? "sun" : "hi", ring_radii: rc.radii, ring_points: rc.points, capture_time: Number($("real-capture-time").value),
        beam_fwhm: Number($("real-beam-fwhm").value), min_elevation: Number($("real-min-elevation").value) };
      if (center) { p.center_ra_hours = center.ra_hours; p.center_dec_deg = center.dec_deg; }
      return p;
    }
    function planParams() { return buildParams(selectedCenter()); }
    // What to compare the last APPROVED PLAN against, for staleness. If the operator has not explicitly changed
    // mode or the selected HI area since that PLAN was requested (planApprovedFor still matches), the centre comes
    // from the PLAN job's own recorded params - frozen, never a live sky.areas lookup - so refreshSky() recomputing
    // A/B/C's coordinates for the SAME label can never make an approved PLAN look obsolete. The ring/capture/beam/
    // min-elevation fields and the reference itself are still read live: an explicit edit to any of them, or an
    // explicit area/mode change, correctly falls through to selectedCenter() (or simply differs) and DOES require
    // a new PLAN, as it always did.
    function comparisonParams() {
      const p = planJob.last;
      const frozen = p && p.state === "EXITED" && planApprovedFor.mode === mode && planApprovedFor.selectedArea === selectedArea &&
        mode === "hi" && selectedArea && selectedArea !== "MANUAL" && p.params && p.params.center_ra_hours != null;
      return buildParams(frozen ? { ra_hours: p.params.center_ra_hours, dec_deg: p.params.center_dec_deg } : selectedCenter());
    }
    $("real-plan").addEventListener("click", U.guard($("real-plan"), () => {
      const rc = ringConfig();
      if (!rc.valid || (mode === "hi" && !selectedArea) || (mode === "solar" && !(sky && sky.sun.above_horizon))) return undefined;
      planApprovedFor = { mode, selectedArea };   // record identity synchronously, at click time - see declaration above
      return planJob.start("align_plan", planParams());
    }, "PLANNING…"));
    $("real-run").addEventListener("click", U.guard($("real-run"), () => {
      const j = planJob.last, f = j && j.facts, pc = f && f.pattern_config;
      if (!j || j.state !== "EXITED" || j.verdict !== "PASS" || !U.paramsEqual(j.params, comparisonParams())) {
        alert("PLAN is obsolete or missing (parameters, or the selected area/mode, changed since PLAN ran) - PLAN again before RUN.");
        return undefined;
      }
      // Client-side belt only (the server is the actual authority - almita_web_ops.py build_command()'s
      // "align" stage re-derives and enforces this same deadline from its own job record before ever starting
      // alignment.py, regardless of what this check decides): catches an expired PLAN immediately, with a clear
      // message, instead of letting the click reach the server just to be refused there a moment later.
      if (j.ended_utc && planValiditySeconds != null && (Date.now() - Date.parse(j.ended_utc)) / 1000 > planValiditySeconds) {
        alert(`This PLAN's ${planValiditySeconds}s validity window has passed - PLAN again before RUN.`);
        return undefined;
      }
      const ok = confirm(`REAL ALIGNMENT (${mode.toUpperCase()}): the mount will slew and capture MAIN across ${pc ? pc.total_positions : "?"} positions ` +
        `(center + ${pc ? pc.ring_radii_deg.length : "?"} ring(s) x ${pc ? pc.ring_points_per_ring : "?"}, max radius ${pc ? Math.max(...pc.ring_radii_deg) : "?"}°).\n` +
        (mode === "hi" && f && f.center ? `Approved centre: RA ${Number(f.center.ra_hours).toFixed(3)} h, Dec ${Number(f.center.dec_deg).toFixed(2)}° (exactly what RUN will use).\n`
          : mode === "solar" ? "The Sun's position is recomputed fresh at RUN time (it moves); the reference (Sun) and pattern are exactly what was approved.\n" : "") +
        (mode === "solar" ? "RUN will switch OnStep to SOLAR tracking rate and verify it before moving; it restores the previous tracking mode when done or stopped.\n" : "") +
        "SYNC is never sent.\nConfirm physically: free travel, cables, antenna, nobody in the way.\nProceed?");
      if (!ok || !j) return undefined;
      // Send the PLAN job's OWN recorded params verbatim (never a fresh live planParams()/comparisonParams()) -
      // this is the actual approved target; it must be exactly what RUN executes, unaffected by anything that
      // happened on screen since PLAN completed.
      return runJob.start("align", { ...j.params, approved_plan_dir: j.output_dir }, $("real-confirm").value);
    }, "STARTING…"));
    planJob.recover("align_plan"); runJob.recover("align");
    loadDefaults().then(() => { setMode("hi"); });
    setInterval(() => { if (!planJob.last || planJob.last.state !== "RUNNING") refreshSky(); }, 30000);
    setInterval(updateReal, 1000);
  }
})();
