// ALMITA ALIGN — calls the :8090 ALIGN API only. All fit/offset/quality/
// sync-eligibility numbers come from the backend (alignment_engine) - this
// file only presents, orchestrates, and polls. No science here.
(function () {
  "use strict";
  const apiRoot = `${location.protocol}//${location.hostname}:${location.port || 8090}`;
  let mode = "solar";
  let currentSessionId = null;
  let pollTimer = null;

  function showError(message) {
    const banner = document.getElementById("error-banner");
    banner.textContent = message;
    banner.hidden = !message;
  }

  async function getJSON(path) {
    const res = await fetch(`${apiRoot}${path}`, { cache: "no-store" });
    return res.json();
  }
  async function postJSON(path, body) {
    const res = await fetch(`${apiRoot}${path}`, {
      method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}),
    });
    return res.json();
  }

  function badgeClass(text) {
    return "badge status-" + String(text || "unknown").toLowerCase().replace(/[^a-z0-9]/g, "-");
  }

  async function refreshStatus() {
    try {
      const status = await getJSON("/api/align/status");
      const d = status.data || {};
      document.getElementById("st-deployment").textContent = d.deployment_state || "—";
      document.getElementById("st-resource").textContent = (d.resource || {}).status || "—";
      document.getElementById("st-hi-phase").textContent = (d.hi || {}).phase || "—";
      document.getElementById("st-hi-sync").textContent = ((d.hi || {}).sync || {}).sync_allowed ? "ALLOWED" : "BLOCKED";
      document.getElementById("st-solar-sync").textContent = (d.solar || {}).sync_authorized ? "ALLOWED" : "BLOCKED";
      renderSessionList((d.sessions || {})[mode] || []);
      showError("");
    } catch (err) {
      showError(`Status request failed: ${err}`);
    }
  }

  function renderSessionList(sessions) {
    const el = document.getElementById("session-list");
    el.innerHTML = "";
    if (!sessions.length) {
      el.innerHTML = '<div class="obs-check-row">NO ALIGNMENT SESSION</div>';
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
    document.getElementById("mode-solar").classList.toggle("active", mode === "solar");
    document.getElementById("mode-hi").classList.toggle("active", mode === "hi");
    document.getElementById("mode-title").textContent = mode === "solar" ? "SOLAR ALIGNMENT" : "HI ALIGNMENT";
    document.getElementById("hi-replay-panel").hidden = mode !== "hi";
    document.getElementById("hi-compare-panel").hidden = mode !== "hi";
    document.getElementById("mode-body").textContent = "Not planned yet - click PLAN.";
    document.getElementById("mode-badge").textContent = "—";
    document.getElementById("btn-preflight").disabled = true;
    document.getElementById("btn-run-sim").disabled = true;
    document.getElementById("result-panel").hidden = true;
    currentSessionId = null;
    refreshStatus();
  }

  async function doPlan() {
    try {
      const res = await postJSON(`/api/align/plan/${mode}`, {});
      if (res.blocked) { showError(res.reason || "PLAN blocked"); return; }
      currentSessionId = res.data.session_id;
      document.getElementById("mode-badge").textContent = res.data.state;
      document.getElementById("mode-badge").className = badgeClass(res.data.state);
      document.getElementById("mode-body").textContent = `Session: ${currentSessionId}\nState: ${res.data.state}`;
      document.getElementById("btn-preflight").disabled = false;
      showError("");
    } catch (err) { showError(`PLAN failed: ${err}`); }
  }

  async function doPreflight() {
    if (!currentSessionId) return;
    try {
      const res = await postJSON(`/api/align/preflight/${mode}`, { session_id: currentSessionId });
      const checksEl = document.getElementById("preflight-checks");
      checksEl.innerHTML = "";
      const checks = (res.data || {}).checks || [];
      for (const c of checks) {
        const row = document.createElement("div");
        row.className = "obs-check-row " + (c.ok ? "status-pass" : "status-block");
        row.textContent = `[${c.ok ? "PASS" : "BLOCKED"}] ${c.name}: ${c.detail}`;
        checksEl.appendChild(row);
      }
      const ready = !res.blocked;
      document.getElementById("mode-badge").textContent = ready ? "READY" : "BLOCKED";
      document.getElementById("mode-badge").className = badgeClass(ready ? "ready" : "blocked");
      document.getElementById("btn-run-sim").disabled = !ready;
      document.getElementById("btn-run-real").disabled = true; // never enabled this iteration - see status panel
      if (!ready) showError(res.reason || "preflight blocked"); else showError("");
    } catch (err) { showError(`PREFLIGHT failed: ${err}`); }
  }

  async function doRunSimulation() {
    if (!currentSessionId) return;
    document.getElementById("btn-run-sim").disabled = true;
    try {
      const res = await postJSON(`/api/align/run/${mode}`, { session_id: currentSessionId, true_offset_east: 0, true_offset_north: 0 });
      if (res.blocked) { showError(res.reason || "RUN blocked"); return; }
      pollSession();
    } catch (err) { showError(`RUN failed: ${err}`); }
  }

  function pollSession() {
    if (pollTimer) clearInterval(pollTimer);
    const tick = async () => {
      try {
        const res = await getJSON(`/api/align/session/${encodeURIComponent(currentSessionId)}`);
        if (res.blocked) return;
        renderResult(res.data);
        if (!res.data.job_running) { clearInterval(pollTimer); pollTimer = null; document.getElementById("btn-run-sim").disabled = false; }
      } catch (err) { /* transient */ }
    };
    tick();
    pollTimer = setInterval(tick, 2000);
  }

  function renderResult(data) {
    const panel = document.getElementById("result-panel");
    panel.hidden = false;
    document.getElementById("simulation-flag").hidden = false; // every run_simulation result IS a simulation
    const state = data.state || {};
    const result = data.result;
    const badge = document.getElementById("result-badge");
    badge.textContent = state.state || "RUNNING";
    badge.className = badgeClass(badge.textContent);
    if (!result) {
      document.getElementById("result-summary").textContent = `State: ${state.state || "running"}...`;
      return;
    }
    const fit = result.final_fit || {};
    const lines = [
      `East offset:  ${fmt(result.tangent_east_deg)} deg`,
      `North offset: ${fmt(result.tangent_north_deg)} deg`,
      `Quality: ${((fit.quality || {}).rating) || "—"}  confidence=${fmt((fit.quality || {}).confidence)}`,
    ];
    if (fit.refinement) {
      lines.push(`Uncertainty east:  ${fmt(fit.refinement.uncertainty_east_deg)} deg`);
      lines.push(`Uncertainty north: ${fmt(fit.refinement.uncertainty_north_deg)} deg`);
    }
    if (result.quality) {
      lines.push("", "POINTING QUALITY (bootstrap):", `  verdict: ${result.quality.verdict}`);
    }
    document.getElementById("result-summary").textContent = lines.join("\n");
    const syncStatus = document.getElementById("sync-status");
    syncStatus.textContent = mode === "hi" ? "SYNC BLOCKED — FIRST_LIGHT_HI requires physical repeatability before correction"
                                            : "SYNC BLOCKED — real solar SYNC not yet authorized";
  }

  function fmt(v) { return typeof v === "number" ? v.toFixed(4) : "—"; }

  async function doReplay() {
    const sessionDir = document.getElementById("replay-session-dir").value.trim();
    if (!sessionDir) return;
    try {
      const res = await postJSON("/api/align/replay", { session_dir: sessionDir });
      document.getElementById("replay-summary").textContent = JSON.stringify(res.data || res, null, 2);
    } catch (err) { showError(`REPLAY failed: ${err}`); }
  }

  async function doCompare() {
    const a = document.getElementById("compare-a").value.trim();
    const b = document.getElementById("compare-b").value.trim();
    if (!a || !b) return;
    try {
      const res = await postJSON("/api/align/compare", { analysis_dir_a: a, analysis_dir_b: b });
      document.getElementById("compare-summary").textContent = JSON.stringify(res.data || res, null, 2);
    } catch (err) { showError(`COMPARE failed: ${err}`); }
  }

  document.getElementById("mode-solar").addEventListener("click", () => setMode("solar"));
  document.getElementById("mode-hi").addEventListener("click", () => setMode("hi"));
  document.getElementById("btn-plan").addEventListener("click", doPlan);
  document.getElementById("btn-preflight").addEventListener("click", doPreflight);
  document.getElementById("btn-run-sim").addEventListener("click", doRunSimulation);
  document.getElementById("btn-replay").addEventListener("click", doReplay);
  document.getElementById("btn-compare").addEventListener("click", doCompare);

  setMode("solar");
  setInterval(refreshStatus, 5000);
})();
