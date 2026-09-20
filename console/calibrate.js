// ALMITA CALIBRATE — calls the :8090 CALIBRATE API only. All clipping/
// headroom/bandpass/gain/quality numbers come from calibration_engine via
// the backend - this file only presents, orchestrates, and polls.
(function () {
  "use strict";
  const U = window.AlmitaUI;
  U.mountHealthStrip(U.mountHeader("CALIBRATE", "CALIBRATION"));
  U.mountFooter();
  let scenario = "HEALTHY";
  let currentSessionId = null;
  let sessionPoller = null;
  let pollError = false;

  function showError(message) { U.showError(document.getElementById("error-banner"), message); }
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
  function badgeClass(text) { return "badge " + U.stateClass(text); }
  function verifyTag(v) {
    if (!v) return null;
    const cls = v === "VERIFIED_BY_DEVICE_READBACK" ? "verify-device"
              : v === "VERIFIED_BY_SERVICE_COMMAND_LINE" ? "verify-service" : "verify-expected";
    const span = document.createElement("span");
    span.className = "verify-tag " + cls;
    span.textContent = String(v).replace(/_/g, " ");
    return span;
  }

  // Values come from the backend (device read-back, service command line): they are inserted as TEXT, never as markup.
  function card(label, value, tagEl) {
    const el = document.createElement("div");
    el.className = "card";
    const dt = document.createElement("dt");
    dt.textContent = label;
    const dd = document.createElement("dd");
    dd.appendChild(document.createTextNode(String(value)));
    if (tagEl) { dd.appendChild(document.createTextNode(" ")); dd.appendChild(tagEl); }
    el.append(dt, dd);
    return el;
  }

  async function refreshStatus() {
    try {
      const status = await getJSON("/api/calibrate/status");
      const d = status.data || {};
      const cards = document.getElementById("receiver-cards");
      cards.textContent = "";
      const r = d.receiver || {};
      const field = (obj) => obj && typeof obj === "object" ? obj : { value: obj, verification: null };
      cards.appendChild(card("SERIAL", r.serial || "—"));
      const cf = field(r.center_frequency_hz);
      cards.appendChild(card("CENTER FREQ", U.mhz(cf.value), verifyTag(cf.verification)));
      const sr = field(r.sample_rate_hz);
      cards.appendChild(card("SAMPLE RATE", U.msps(sr.value), verifyTag(sr.verification)));
      const gain = field(r.gain_db);
      cards.appendChild(card("GAIN", U.finite(gain.value) ? `${gain.value} dB` : "—", verifyTag(gain.verification)));
      const biast = field(r.bias_t_state);
      cards.appendChild(card("BIAS-T", biast.value || "—", verifyTag(biast.verification)));
      const tuner = field(r.tuner_type);
      cards.appendChild(card("TUNER", tuner.value || (r.tuner || "—"), verifyTag(tuner.verification)));

      const fa = d.frequency_audit;
      document.getElementById("frequency-audit").textContent = fa
        ? `${fa.label}\nservice center frequency: ${U.mhz(fa.service_center_frequency_hz)} (${fa.service_center_frequency_hz} Hz)\nnominal HI rest: ${U.mhz(fa.nominal_hi_rest_hz)} (${fa.nominal_hi_rest_hz} Hz)\ndifference: ${fa.difference_hz} Hz (configuration difference - NOT a measured frequency error)`
        : "";

      // Fase A: four DELIBERATELY SEPARATE concepts, never merged into one
      // ambiguous line - "Observation: RUNNING" reads as if CALIBRATE
      // itself were running an observation, which it never is.
      const resource = d.resource || {};
      document.getElementById("st-active-observation").textContent = resource.orchestrator_state || "UNKNOWN";
      document.getElementById("st-main-resource").textContent = resource.status || "UNKNOWN";

      const workflowEl = document.getElementById("st-calibration-workflow");
      const workflowActive = d.calibration_workflow_active || sessionPoller !== null;
      workflowEl.textContent = workflowActive ? "SIMULATION RUNNING" : "IDLE";

      const busy = resource.status !== "FREE";
      U.setEnabled(document.getElementById("btn-run-real"), false, "blocked by policy: real capture is not wired to hardware from the web");
      const realCalEl = document.getElementById("st-real-calibration");
      const reasonEl = document.getElementById("real-calibration-reason");
      realCalEl.textContent = "BLOCKED";
      reasonEl.hidden = false;
      if (busy) {
        reasonEl.textContent = `Reason: MAIN SDR currently in use by active science observation (${resource.detail})`;
      } else {
        reasonEl.textContent = "Reason: not wired to hardware from the web in this iteration; use calibration_operational_realtest.py";
      }
      document.getElementById("st-simulation").textContent = "AVAILABLE";

      renderGainTable(d.gain_table);
      renderSessionList(d.sessions || []);
      renderProfileList(d.profiles || []);
      if (pollError) { showError(""); pollError = false; }
      return true;
    } catch (err) { showError(`Status request failed: ${msg(err)}`); pollError = true; return false; }
  }

  function renderGainTable(gt) {
    if (!gt) return;
    const status = gt.status === "MATCHES_DEVICE_COUNT" ? "MATCHES DEVICE COUNT" : "UNVERIFIED FOR THIS DEVICE";
    document.getElementById("gain-summary").textContent =
      `Candidate table entries: ${gt.candidate_table_size}\n` +
      `Device-reported gain_count (handshake): ${gt.device_reported_gain_count ?? "unavailable"}\n` +
      `GAIN TABLE: ${status}\n` +
      `Provenance: ${gt.provenance}`;
  }

  function renderSessionList(sessions) {
    const el = document.getElementById("session-list");
    el.textContent = "";
    if (!sessions.length) { const row = document.createElement("div"); row.className = "obs-check-row"; row.textContent = "NO CALIBRATION SESSION YET"; el.appendChild(row); return; }
    for (const s of sessions) {
      const row = document.createElement("div");
      row.className = "obs-check-row";
      row.textContent = `${s.session_id}  [${s.phase}]`;
      el.appendChild(row);
    }
  }
  function renderProfileList(profiles) {
    const el = document.getElementById("profile-list");
    el.textContent = "";
    if (!profiles.length) { const row = document.createElement("div"); row.className = "obs-check-row"; row.textContent = "NO PROFILE"; el.appendChild(row); return; }
    for (const p of profiles) {
      const row = document.createElement("div");
      row.className = "obs-check-row";
      row.textContent = `${p.session_id}  [DRAFT]`;
      el.appendChild(row);
    }
  }

  document.querySelectorAll(".scenario-btn").forEach((btn) => {
    btn.setAttribute("aria-pressed", String(btn.classList.contains("active")));
    btn.addEventListener("click", () => {
      document.querySelectorAll(".scenario-btn").forEach((b) => { b.classList.remove("active"); b.setAttribute("aria-pressed", "false"); });
      btn.classList.add("active");
      btn.setAttribute("aria-pressed", "true");
      scenario = btn.dataset.scenario;
    });
  });

  async function doRunSimulation() {
    try {
      const res = await postJSON("/api/calibrate/run", { scenario, n_captures: 5, capture_seconds: 1.0 }, 30000);
      if (res.blocked) { showError(res.reason || "RUN blocked"); return; }
      showError("");
      currentSessionId = res.data.session_id;
      document.getElementById("overview-panel").hidden = false;
      document.getElementById("st-calibration-workflow").textContent = "SIMULATION RUNNING";
      pollSession();
    } catch (err) { showError(`RUN failed: ${msg(err)}`); }
  }

  function pollSession() {
    if (sessionPoller) sessionPoller.stop();
    const sid = currentSessionId;
    const runBtn = document.getElementById("btn-run-sim");
    U.setEnabled(runBtn, false, "a calibration simulation is running");
    sessionPoller = U.poller(async () => {
      const r = await U.api(`/api/calibrate/session/${encodeURIComponent(sid)}`, { timeoutMs: 10000 });
      if (!r.ok) { showError(r.error); pollError = true; return false; }
      const res = r.data;
      if (res.blocked) return true;                    // session not visible yet: keep waiting
      if (pollError) { showError(""); pollError = false; }
      renderResult(res.data);
      if (!res.data.job_running) {
        if (sessionPoller) sessionPoller.stop();
        sessionPoller = null;
        U.setEnabled(runBtn, true, "");
        U.setEnabled(document.getElementById("btn-profile-build"), !!res.data.result, "the run has no result to build a profile from");
        await refreshStatus();
        document.getElementById("st-calibration-workflow").textContent = "COMPLETED";
      }
      return true;
    }, { intervalMs: 2000, hiddenIntervalMs: 15000 });
    sessionPoller.start();
  }

  function renderResult(data) {
    const state = data.state || {};
    const result = data.result;
    const badge = document.getElementById("run-badge");
    badge.textContent = state.phase || "RUNNING";
    badge.className = badgeClass(badge.textContent);
    if (!result) return;
    const captures = Array.isArray(result.per_capture) ? result.per_capture : [];
    const cardsEl = document.getElementById("overview-cards");
    cardsEl.textContent = "";
    if (!captures.length) {                               // empty data: say so instead of crashing on captures[0]
      cardsEl.appendChild(card("RESULT", "No captures in this result yet"));
      return;
    }
    const worstClip = captures.reduce((worst, c) => {
      const order = ["OK", "WARNING", "CLIPPED", "UNKNOWN"];
      return order.indexOf((c.clipping || {}).status) > order.indexOf(worst) ? c.clipping.status : worst;
    }, "OK");
    const bands = captures.map((c) => c.usable_band_fraction).filter(U.finite);
    const minBand = bands.length ? Math.min(...bands) : NaN;
    const first = captures[0];
    cardsEl.appendChild(card("CLIPPING", worstClip));
    cardsEl.appendChild(card("HEADROOM", `${U.fixed((first.clipping || {}).percentile_margin_codes, 0)} codes`));
    cardsEl.appendChild(card("USABLE BAND", U.finite(minBand) ? `${(minBand * 100).toFixed(1)}%` : "—"));
    cardsEl.appendChild(card("STABILITY", result.stability && U.finite(result.stability.power_rms_fraction) ? `${(result.stability.power_rms_fraction * 100).toFixed(3)}%` : "n/a"));
    cardsEl.appendChild(card("QUALITY", result.quality ? result.quality.verdict : "—"));

    document.getElementById("stability-panel").hidden = !result.stability;
    if (result.stability) {
      const exp = (v) => (U.finite(v) ? v.toExponential(3) : "—");
      document.getElementById("stability-summary").textContent =
        `power_rms_fraction: ${exp(result.stability.power_rms_fraction)}\n` +
        `drift_slope_per_hour: ${exp(result.stability.drift_slope_per_hour)} (per hour)\n` +
        `WARM-UP DETECTED: ${result.stability.warmup_detected}\n` +
        `WARM-UP RECOMMENDATION: ${result.stability.warmup_detected ? result.stability.warmup_reason : "NOT ESTABLISHED"}`;
    }

    document.getElementById("thermal-panel").hidden = false;
    const temps = result.temperature_before || {};
    document.getElementById("thermal-summary").textContent = Object.keys(temps).length
      ? JSON.stringify(temps, null, 2) : "No temperature sensors configured/available for this session.";

    document.getElementById("bandpass-panel").hidden = false;
    drawBandpass(first);
    document.getElementById("bandpass-notes").textContent =
      `usable_band_fraction: ${U.finite(minBand) ? (minBand * 100).toFixed(1) + "%" : "—"}  dc_half_width_hz: ${U.fixed(first.dc_half_width_hz, 0, "Hz")}\n` +
      "RECOMMENDED MASK: DRAFT ONLY (not computed for a single simulated run - needs multiple captures under the same config)";
  }

  function drawBandpass(pointCapture) {
    // NOTE: almita_calibrate.py run's per_capture summary does not include
    // the full normalized_bandpass array (only scalar usable_band_fraction) -
    // this draws the histogram (which IS available) as a real-data proxy
    // rather than fabricating a spectrum shape client-side.
    const canvas = document.getElementById("bandpass-canvas");
    const ctx = canvas.getContext("2d");
    ctx.clearRect(0, 0, canvas.width, canvas.height);
    const hist = ((pointCapture.sample_statistics || {}).histogram || []).map((v) => (U.finite(v) ? v : 0));
    ctx.fillStyle = "#91a2ad";
    ctx.font = "10px monospace";
    if (!hist.length || !hist.some((v) => v > 0)) {      // empty / all-zero / NaN histogram: an explicit empty state, not a blank or broken chart
      ctx.fillText("No histogram data yet", 6, 12);
      return;
    }
    const max = Math.max(...hist, 1);
    ctx.fillStyle = "#65b7d8";
    const barWidth = canvas.width / hist.length;
    hist.forEach((v, i) => {
      const h = (v / max) * (canvas.height - 10);
      ctx.fillRect(i * barWidth, canvas.height - h, Math.max(1, barWidth - 0.5), h);
    });
    ctx.fillStyle = "#91a2ad";
    ctx.fillText("ADC code histogram (raw sample statistics) — not the RF bandpass shape", 6, 12);
  }

  async function doReplay() {
    const sessionDir = document.getElementById("replay-session-dir").value.trim();
    if (!sessionDir) { showError("REPLAY: enter a session directory (data/calibration/CAL-...)"); return; }
    try {
      const res = await postJSON("/api/calibrate/replay", { session_dir: sessionDir }, 60000);
      document.getElementById("replay-summary").textContent = res.blocked ? `BLOCKED: ${res.reason}` : JSON.stringify(res.data || res, null, 2).slice(0, 4000);
      showError("");
    } catch (err) { showError(`REPLAY failed: ${msg(err)}`); }
  }
  async function doCompare() {
    const a = document.getElementById("compare-a").value.trim();
    const b = document.getElementById("compare-b").value.trim();
    if (!a || !b) { showError("COMPARE: enter both session ids"); return; }
    try {
      const res = await postJSON("/api/calibrate/compare", { session_a: a, session_b: b }, 60000);
      document.getElementById("compare-summary").textContent = res.blocked ? `BLOCKED: ${res.reason}` : JSON.stringify(res.data || res, null, 2);
      showError("");
    } catch (err) { showError(`COMPARE failed: ${msg(err)}`); }
  }
  async function doProfileBuild() {
    if (!currentSessionId) { showError("PROFILE BUILD: run a calibration first"); return; }
    try {
      const res = await postJSON("/api/calibrate/profile/build", { session_id: currentSessionId }, 60000);
      document.getElementById("profile-summary").textContent = res.blocked ? `BLOCKED: ${res.reason}` : JSON.stringify(res.data || res, null, 2);
      showError("");
      refreshStatus();
    } catch (err) { showError(`PROFILE BUILD failed: ${msg(err)}`); }
  }

  // Page reload during a run: adopt a calibration job that is still running on the server instead of assuming IDLE.
  async function recover() {
    try {
      const status = await getJSON("/api/calibrate/status");
      const d = status.data || {};
      const newest = (d.sessions || [])[0];
      if (d.calibration_workflow_active && newest) {
        currentSessionId = newest.session_id;
        document.getElementById("overview-panel").hidden = false;
        pollSession();
      }
    } catch (err) { /* best effort; the normal status poll reports connection problems */ }
  }

  const runBtn = document.getElementById("btn-run-sim");
  runBtn.addEventListener("click", U.guard(runBtn, doRunSimulation, "STARTING…"));
  const refreshBtn = document.getElementById("btn-refresh-status");
  refreshBtn.addEventListener("click", U.guard(refreshBtn, refreshStatus, "REFRESHING…"));
  for (const [id, fn, busy] of [["btn-replay", doReplay, "REPLAYING…"], ["btn-compare", doCompare, "COMPARING…"], ["btn-profile-build", doProfileBuild, "BUILDING…"]]) {
    const b = document.getElementById(id);
    b.addEventListener("click", U.guard(b, fn, busy));
  }
  U.setEnabled(document.getElementById("btn-profile-build"), false, "run a calibration first");
  U.setEnabled(document.getElementById("btn-run-real"), false, "blocked by policy: real capture is not wired to hardware from the web");

  U.poller(refreshStatus, { intervalMs: 15000 }).start();   // refreshStatus polls the backend, which reads MAIN state: slow, and never while the tab is hidden
  recover();
  window.addEventListener("pagehide", () => { if (sessionPoller) sessionPoller.stop(); });
})();
