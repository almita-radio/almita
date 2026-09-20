// ALMITA ALIGN — calls the :8090 ALIGN API only. All fit/offset/quality/sync-eligibility numbers come from the backend
// (alignment_engine) - this file only presents, orchestrates, and polls. No science here. Simulation only: real hardware is never reachable.
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

  function setDisabledReasons() {
    U.setEnabled($("btn-preflight"), !!currentSessionId, "plan first");
    if (!$("btn-run-sim").dataset.running) U.setEnabled($("btn-run-sim"), $("btn-run-sim").dataset.preflightOk === "1", "run PREFLIGHT first (it must pass)");
    U.setEnabled($("btn-run-real"), false, "blocked by policy: real hardware alignment is not available from the web");
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
})();
