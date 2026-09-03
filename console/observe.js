// ALMITA OBSERVE — calls the orchestrator's local JSON API only. Never sends
// raw shell commands; every value is read from typed form fields and sent as
// a strict JSON object matching the observation schema. Offline: no CDN, no
// external requests.
(function () {
  "use strict";
  const API_PORT = 8090;
  const MONITOR_PORT = 8088;
  const apiRoot = `${location.protocol}//${location.hostname}:${API_PORT}`;

  document.getElementById("nav-monitor").href = `${location.protocol}//${location.hostname}:${MONITOR_PORT}/`;
  document.getElementById("nav-last-session").href = `${location.protocol}//${location.hostname}:${MONITOR_PORT}/#last-session`;

  let lastResolvedPlan = null;
  let statusTimer = null;

  function showError(message) {
    const banner = document.getElementById("error-banner");
    banner.textContent = message;
    banner.hidden = !message;
  }

  function readSpec() {
    const placement = document.getElementById("f-placement").value;
    const centerDecRaw = document.getElementById("f-center-dec").value;
    return {
      session: { name: document.getElementById("f-name").value },
      grid: {
        mode: document.getElementById("f-mode").value,
        placement: placement,
        center_ra_hours: placement === "FIXED_CENTER" ? Number(document.getElementById("f-center-ra").value) : null,
        center_dec_deg: centerDecRaw === "" ? null : Number(centerDecRaw),
        width_deg: Number(document.getElementById("f-width").value),
        height_deg: Number(document.getElementById("f-height").value),
        rows: Number(document.getElementById("f-rows").value),
        cols: Number(document.getElementById("f-cols").value),
        min_altitude_deg: Number(document.getElementById("f-min-alt").value),
        traversal: "SERPENTINE",
      },
      capture: {
        seconds: Number(document.getElementById("f-capture").value),
        settle_seconds: Number(document.getElementById("f-settle").value),
      },
      main: {
        center_frequency_hz: Number(document.getElementById("f-freq").value),
        sample_rate: Number(document.getElementById("f-rate").value),
        gain_db: Number(document.getElementById("f-gain").value),
        bias_tee: true,
      },
      rfi_ref: {
        enabled: document.getElementById("f-rfi-enabled").checked,
        serial: "00000002",
        gain_db: Number(document.getElementById("f-rfi-gain").value),
        bias_tee: document.getElementById("f-rfi-bias-tee").checked,
      },
      quicklook: {
        enabled: document.getElementById("f-ql-enabled").checked,
        native_grid: document.getElementById("f-ql-native").checked,
        interpolated_preview: document.getElementById("f-ql-interp").checked,
        calibration_profile_path: document.getElementById("f-ql-cal").value || null,
      },
      console: { enabled: document.getElementById("f-console-enabled").checked },
      execution: { unattended: true },
    };
  }

  function fmtHms(totalSeconds) {
    const s = Math.round(totalSeconds);
    const h = String(Math.floor(s / 3600)).padStart(2, "0");
    const m = String(Math.floor((s % 3600) / 60)).padStart(2, "0");
    const sec = String(s % 60).padStart(2, "0");
    return `${h}:${m}:${sec}`;
  }

  function gib(bytes) { return (bytes / (1024 ** 3)).toFixed(2); }

  function renderPlan(plan) {
    lastResolvedPlan = plan;
    const r = plan.resolved;
    const lines = [
      `Session             ${plan.observation_name}`,
      `Grid                ${r.rows} x ${r.cols} = ${r.point_count}`,
      `Footprint           ${r.footprint_width_deg.toFixed(2)} deg x ${r.footprint_height_deg.toFixed(2)} deg`,
      `Center              RA ${r.center_ra_hours.toFixed(4)}h  DEC ${r.center_dec_deg.toFixed(4)} deg`,
      `Spacing             ${r.spacing_deg.toFixed(3)} deg`,
      `Capture             ${plan.requested.capture.seconds} s`,
      `Settle              ${plan.requested.capture.settle_seconds} s`,
      "",
      `RFI_REF             ${plan.rfi_ref.enabled ? "ENABLED" : "DISABLED"}`,
      `  Bias-T            ${plan.rfi_ref.enabled ? (plan.rfi_ref.bias_tee ? "ON" : "OFF") : "N/A"}`,
      "",
      `Estimated duration  ${fmtHms(plan.duration.estimated_seconds)}`,
      `Conservative        ${fmtHms(plan.duration.conservative_seconds)}`,
      `Estimated storage   ${gib(plan.storage.required_bytes)} GiB`,
      `Free storage        ${gib(plan.storage.free_bytes_at_plan_time)} GiB`,
      "",
      `Visibility          ${plan.visibility}`,
      `Min predicted alt   ${plan.min_predicted_altitude_deg.toFixed(1)} deg`,
      "",
      r.placement_reasoning,
      "",
      "NO HARDWARE HAS MOVED.",
    ];
    document.getElementById("plan-summary").textContent = lines.join("\n");

    const badge = document.getElementById("plan-badge");
    const preflight = plan.preflight || { overall: "WARNING", checks: [] };
    const blocked = plan.visibility === "BLOCK" || preflight.overall === "BLOCK";
    badge.textContent = blocked ? "BLOCKED" : (preflight.overall === "WARNING" ? "WARNING" : "READY");
    badge.className = "badge status-" + badge.textContent.toLowerCase();

    const checksEl = document.getElementById("preflight-list");
    checksEl.innerHTML = "";
    for (const c of preflight.checks) {
      const row = document.createElement("div");
      row.className = "obs-check-row status-" + c.status.toLowerCase();
      row.textContent = `[${c.status}] ${c.name} (${c.criticality}): ${c.detail}`;
      checksEl.appendChild(row);
    }

    const imagesEl = document.getElementById("plan-images");
    imagesEl.innerHTML = "";
    const dirName = plan.grid_session_dir.split("/").filter(Boolean).pop();
    for (const [label, file] of [["PLAN", "grid_plan.png"], ["COVERAGE", "grid_coverage.png"]]) {
      const wrap = document.createElement("a");
      wrap.href = `${apiRoot}/api/observe/plan-assets/${encodeURIComponent(dirName)}/${file}`;
      wrap.target = "_blank"; wrap.rel = "noopener";
      const img = document.createElement("img");
      img.src = wrap.href; img.alt = label;
      const caption = document.createElement("span");
      caption.textContent = label;
      wrap.appendChild(img); wrap.appendChild(caption);
      imagesEl.appendChild(wrap);
    }

    document.getElementById("plan-result").hidden = false;
    document.getElementById("btn-start").disabled = blocked;
    showError("");
  }

  async function submitPlan(event) {
    event.preventDefault();
    showError("");
    document.getElementById("btn-plan").disabled = true;
    try {
      const spec = readSpec();
      const res = await fetch(`${apiRoot}/api/observe/plan`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(spec),
      });
      const body = await res.json();
      if (!res.ok) { showError(body.error || `PLAN failed (${res.status})`); return; }
      renderPlan(body);
    } catch (err) {
      showError(`PLAN request failed: ${err}`);
    } finally {
      document.getElementById("btn-plan").disabled = false;
    }
  }

  async function startObservation() {
    if (!lastResolvedPlan) return;
    if (!window.confirm("START OBSERVATION now? This will move the mount and begin acquisition.")) return;
    showError("");
    try {
      const res = await fetch(`${apiRoot}/api/observe/start`, {
        method: "POST", headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resolved_plan_path: lastResolvedPlan._resolved_plan_path, confirm: true }),
      });
      const body = await res.json();
      if (!res.ok) { showError(body.error || `START failed (${res.status})`); return; }
      document.getElementById("plan-result").hidden = true;
      document.getElementById("run-status").hidden = false;
      document.getElementById("btn-monitor").href = `${location.protocol}//${location.hostname}:${MONITOR_PORT}/`;
      pollStatus();
    } catch (err) {
      showError(`START request failed: ${err}`);
    }
  }

  async function stopObservation() {
    if (!window.confirm("STOP OBSERVATION now? This sends SIGINT to the running capture (clean stop).")) return;
    showError("");
    try {
      const res = await fetch(`${apiRoot}/api/observe/stop`, {
        method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ confirm: true }),
      });
      const body = await res.json();
      if (!res.ok) { showError(body.error || `STOP failed (${res.status})`); return; }
      renderRunStatus({ orchestrator: body });
    } catch (err) {
      showError(`STOP request failed: ${err}`);
    }
  }

  function renderRunStatus(status) {
    const orch = status.orchestrator || {};
    const badge = document.getElementById("run-badge");
    badge.textContent = orch.orchestrator_state || "UNKNOWN";
    badge.className = "badge status-" + badge.textContent.toLowerCase();
    const lines = [
      `orchestrator_state: ${orch.orchestrator_state}`,
      `session_id: ${orch.session_id || "—"}`,
      `capture_pid: ${orch.capture_pid || "—"}`,
      `quicklook_pid: ${orch.quicklook_pid || "—"}`,
    ];
    if (status.current_session) {
      lines.push("", "acquisition:",
        `  state: ${status.current_session.state}`,
        `  points: ${status.current_session.points_success || 0}/${status.current_session.points_total || 0}`);
    }
    document.getElementById("run-summary").textContent = lines.join("\n");
    const terminal = ["COMPLETED", "ABORTED", "FAILED"].includes(orch.orchestrator_state);
    if (terminal && statusTimer) { clearInterval(statusTimer); statusTimer = null; }
  }

  async function pollStatus() {
    if (statusTimer) clearInterval(statusTimer);
    const tick = async () => {
      try {
        const res = await fetch(`${apiRoot}/api/observe/status`, { cache: "no-store" });
        const body = await res.json();
        renderRunStatus(body);
      } catch (err) { /* transient; next tick retries */ }
    };
    await tick();
    statusTimer = setInterval(tick, 2000);
  }

  async function loadDefaults() {
    try {
      const res = await fetch(`${apiRoot}/api/observe/defaults`, { cache: "no-store" });
      if (!res.ok) return;
      const d = await res.json();
      document.getElementById("f-freq").value = d.main.center_frequency_hz;
      document.getElementById("f-rate").value = d.main.sample_rate;
      document.getElementById("f-gain").value = d.main.gain_db;
      document.getElementById("f-min-alt").value = d.grid.min_altitude_deg;
      document.getElementById("f-rfi-gain").value = d.rfi_ref.gain_db;
    } catch (err) { /* keep static defaults if the API isn't reachable yet */ }
  }

  function updateRfiBiasTeeAvailability() {
    const rfiEnabled = document.getElementById("f-rfi-enabled").checked;
    document.getElementById("f-rfi-bias-tee").disabled = !rfiEnabled;
  }

  document.getElementById("f-placement").addEventListener("change", (e) => {
    document.getElementById("row-center-ra").hidden = e.target.value !== "FIXED_CENTER";
  });
  document.getElementById("f-rfi-enabled").addEventListener("change", updateRfiBiasTeeAvailability);
  updateRfiBiasTeeAvailability();
  document.getElementById("observe-form").addEventListener("submit", submitPlan);
  document.getElementById("btn-replan").addEventListener("click", () => {
    document.getElementById("plan-result").hidden = true;
  });
  document.getElementById("btn-start").addEventListener("click", startObservation);
  document.getElementById("btn-stop").addEventListener("click", stopObservation);

  loadDefaults();
  // If an observation is already RUNNING (e.g. this page was reloaded after
  // a browser disconnect), reflect that immediately instead of showing the
  // form as if nothing were happening — the run is server-side and does not
  // depend on this page being open.
  fetch(`${apiRoot}/api/observe/status`, { cache: "no-store" }).then((r) => r.json()).then((status) => {
    const state = (status.orchestrator || {}).orchestrator_state;
    if (state === "RUNNING" || state === "STOPPING") {
      document.getElementById("run-status").hidden = false;
      pollStatus();
    }
  }).catch(() => {});
})();
