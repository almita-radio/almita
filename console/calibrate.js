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
      U.setEnabled(document.getElementById("btn-run-real"), !busy, `MAIN SDR is not free: ${resource.detail}`);
      const realCalEl = document.getElementById("st-real-calibration");
      const reasonEl = document.getElementById("real-calibration-reason");
      realCalEl.textContent = busy ? "BLOCKED" : "AVAILABLE";
      reasonEl.hidden = false;
      if (busy) {
        reasonEl.textContent = `Reason: MAIN SDR currently in use (${resource.detail})`;
      } else {
        reasonEl.textContent = "Use the REAL CALIBRATION panel above (calibration_operational_realtest.py: real MAIN captures, no mount movement, engineering/non-science level).";
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
  U.setEnabled(document.getElementById("btn-run-real"), true, "");

  document.getElementById("btn-run-real").addEventListener("click", () => { document.getElementById("real-panel").scrollIntoView(); });
  U.poller(refreshStatus, { intervalMs: 15000 }).start();   // refreshStatus polls the backend, which reads MAIN state: slow, and never while the tab is hidden
  recover();
  window.addEventListener("pagehide", () => { if (sessionPoller) sessionPoller.stop(); });

  // ------------------------------------------------------------------ REAL CALIBRATION (calibration_operational_realtest.py through /api/ops)
  {
    const realErr = (e) => U.showError(document.getElementById("error-banner"), e);
    const job = U.jobPanel(document.getElementById("real-job-calibrate"), document.getElementById("real-badge"), { onError: realErr, onChange: () => updateReal() });
    function updateReal() {
      const running = job.last && job.last.state === "RUNNING";
      U.setEnabled(document.getElementById("real-run"), !running, "a real calibration is running");
    }
    const b = document.getElementById("real-run");
    b.addEventListener("click", U.guard(b, () => job.start("calibrate", { n_captures: Number(document.getElementById("real-n").value), capture_seconds: Number(document.getElementById("real-s").value) }), "STARTING…"));
    job.recover("calibrate");
    updateReal();
  }

  // ------------------------------------------------------------------ PHYSICAL REFERENCE WIZARD (50R / HOT / COLD through calibrate_reference_wizard.py)
  {
    const $w = (id) => document.getElementById(id);
    const wzErr = (e) => U.showError(document.getElementById("error-banner"), e);
    let sessionDir = null, lastState = null, countdownTimer = null;

    const CONNECTION_EXPLAIN = {
      LNA_INPUT: "Includes in the measurement: Nooelec SAWbird H1 (LNA + filter), cabling, Bias-T, RTL-SDR Blog V4. Excludes: the antenna itself (replaced by this reference).",
      LNA_OUTPUT: "Includes: cabling (post-LNA), Bias-T (if still downstream), RTL-SDR Blog V4. Excludes: antenna, the SAWbird H1 LNA (bypassed).",
      SDR_INPUT: "Includes: RTL-SDR Blog V4 only. Excludes: antenna, the SAWbird H1 LNA, upstream cabling/Bias-T.",
    };
    const KIND_LABEL = { AMBIENT_50R: "50 Ω TERMINATION (ambient temperature — NOT automatically “cold”)", HOT: "HOT REFERENCE", COLD: "COLD REFERENCE" };
    const ORDER = ["AMBIENT_50R", "HOT", "COLD"];

    async function pollJobUntilDone(jobId, timeoutMs) {
      const deadline = Date.now() + timeoutMs;
      for (;;) {
        const r = await U.api(`/api/ops/job/${jobId}?tail=40`, { timeoutMs: 15000 });
        if (!r.ok) throw new Error(U.errorText(r.error));
        const j = r.data.data;
        if (j.state !== "RUNNING") return j;
        if (Date.now() > deadline) throw new Error("wizard step timed out waiting for the job to finish");
        await new Promise((res) => setTimeout(res, 800));
      }
    }
    async function wizardAction(params, timeoutMs) {
      const r = await U.api("/api/ops/start/calibrate_wizard", { method: "POST", body: { params, confirm: null }, timeoutMs: 20000 });
      if (!r.ok) throw new Error(U.errorText(r.error));
      const job = await pollJobUntilDone(r.data.data.job_id, timeoutMs || 60000);
      if (job.state !== "EXITED" || !job.facts || !job.facts.step) {
        throw new Error(job.detail || `wizard step '${params.action}' did not return a usable state`);
      }
      return job;
    }

    function updateCpExplain() { $w("wz-cp-explain").textContent = CONNECTION_EXPLAIN[$w("wz-cp").value] || ""; }
    $w("wz-cp").addEventListener("change", updateCpExplain);
    updateCpExplain();

    function startCountdown(seconds) {
      if (countdownTimer) clearInterval(countdownTimer);
      let remaining = Math.round(seconds);
      const paint = () => {
        $w("wz-countdown").textContent = remaining > 0
          ? `suggested stabilization: ~${remaining}s remaining — you decide when it's actually ready`
          : "suggested stabilization time elapsed — capture only when YOU judge it's ready; this never advances by itself";
      };
      paint();
      countdownTimer = setInterval(() => { remaining -= 1; paint(); if (remaining <= 0) clearInterval(countdownTimer); }, 1000);
    }

    function renderResultTable(result) {
      const tbody = document.querySelector("#wz-result-table tbody");
      tbody.textContent = "";
      for (const c of result.per_capture) {
        const tr = document.createElement("tr");
        for (const v of [c.index, c.clipping.status, c.usable_band_fraction.toFixed(3), c.relative_digital_power.toFixed(2)]) {
          const td = document.createElement("td"); td.textContent = v; tr.appendChild(td);
        }
        tbody.appendChild(tr);
      }
    }

    function render(state) {
      lastState = state;
      const bt = state.bias_t_facts;
      $w("wizard-biast").textContent = bt ? `BIAS-T: ${bt.note} ${bt.safety}` : "";
      $w("wizard-setup").hidden = true;
      $w("wizard-active").hidden = false;
      U.setBadge($w("wizard-badge"), state.step);
      for (const id of ["wz-step-prepare", "wz-step-stabilize", "wz-step-result", "wz-step-reconnect", "wz-step-done"]) $w(id).hidden = true;
      const settled = (state.references || []).filter((r) => r.status === "DONE" || r.status === "SKIPPED").length;
      $w("wz-session-note").textContent = `session ${state.session_id} · step ${state.step} · reference ${Math.min(state.reference_index + 1, 3)}/3 (${settled} settled so far)`;
      if (state.step === "PREPARE") {
        const kind = ORDER[state.reference_index];
        $w("wz-step-prepare").hidden = false;
        $w("wz-prepare-kind").textContent = KIND_LABEL[kind] || kind;
        $w("wz-prepare-instructions").textContent = kind === "AMBIENT_50R"
          ? "Physically connect the 50 Ω termination now. Choose where below, declare its temperature if you know it (otherwise leave blank = not measured), then confirm."
          : `Physically connect your real ${kind} reference now, if you have one. If you do NOT have a real ${kind} load, use SKIP — never declare a fabricated one.`;
        updateCpExplain();
        $w("wz-temp").value = ""; $w("wz-temp-src").value = "";
      } else if (state.step === "STABILIZE") {
        $w("wz-step-stabilize").hidden = false;
        const kind = state.references[state.references.length - 1].kind;
        $w("wz-stabilize-note").textContent = `${KIND_LABEL[kind] || kind} connected. Let it settle (suggested ${state.config.stabilize_seconds}s) — capture only starts on your CAPTURE NOW click.`;
        startCountdown(state.config.stabilize_seconds);
      } else if (state.step === "RESULT") {
        $w("wz-step-result").hidden = false;
        const last = state.references[state.references.length - 1];
        const result = (state.results || {})[last.kind];
        if (result) {
          $w("wz-result-summary").textContent = `${KIND_LABEL[last.kind] || last.kind}: ${result.verdict} — ${result.verdict_reasons.join("; ")} — mean relative digital power ${result.mean_relative_digital_power.toFixed(3)}`;
          renderResultTable(result);
        }
      } else if (state.step === "RECONNECT_ANTENNA") {
        $w("wz-step-reconnect").hidden = false;
      } else if (state.step === "DONE") {
        $w("wz-step-done").hidden = false;
        const done = state.references.filter((r) => r.status === "DONE").map((r) => r.kind);
        const skipped = state.references.filter((r) => r.status === "SKIPPED").map((r) => r.kind);
        $w("wz-done-summary").textContent = `DONE. Tested: ${done.join(", ") || "none"}. Skipped: ${skipped.join(", ") || "none"}. Draft profile: ${state.profile_path || "—"}`;
        const yf = state.y_factor;
        $w("wz-yfactor").textContent = yf
          ? (yf.physical_units_justified
            ? `Y-FACTOR ${yf.y_factor.toFixed(3)} · noise temperature ${yf.noise_temperature_k.toFixed(1)} K · characterizes: ${yf.characterizes} · ${yf.caveats.join(" | ")}`
            : `Y-factor NOT computed: ${yf.reason} — OPERATIONAL_RELATIVE only, no Kelvin/gain figure claimed`)
          : "Y-factor not computed (HOT and/or COLD not both measured/done) — OPERATIONAL_RELATIVE only";
      } else if (state.step === "ABORTED") {
        $w("wizard-active").hidden = true;
        $w("wizard-setup").hidden = false;
      }
    }

    async function recoverWizard() {
      const r = await U.api("/api/ops/jobs", { timeoutMs: 15000 });
      if (!r.ok) return;
      // /api/ops/jobs returns newest-first - the most recent calibrate_wizard job (any action) carries the
      // latest state of whichever session is currently in progress.
      const rows = (r.data.data || []).filter((x) => x.stage === "calibrate_wizard");
      if (!rows.length) return;
      const last = rows[0];
      const jr = await U.api(`/api/ops/job/${last.job_id}`, { timeoutMs: 15000 });
      if (!jr.ok) return;
      const facts = jr.data.data.facts;
      if (!facts || !facts.session_dir || !facts.step || facts.step === "DONE" || facts.step === "ABORTED") return;
      sessionDir = facts.session_dir;
      render(facts);
    }

    $w("wz-start").addEventListener("click", U.guard($w("wz-start"), async () => {
      try {
        const job = await wizardAction({
          action: "start", n_captures: Number($w("wz-n").value), capture_seconds: Number($w("wz-s").value),
          stabilize_seconds: Number($w("wz-stab").value), center_frequency_hz: Number($w("wz-freq").value),
          sample_rate_hz: Number($w("wz-rate").value), gain_db: Number($w("wz-gain").value),
          clipping_threshold: Number($w("wz-clip").value), stability_threshold: Number($w("wz-stab-th").value),
          rfi_threshold: Number($w("wz-rfi").value),
        }, 20000);
        wzErr(""); sessionDir = job.facts.session_dir; render(job.facts);
      } catch (err) { wzErr(`START failed: ${msg(err)}`); }
    }, "STARTING…"));

    $w("wz-confirm-connected").addEventListener("click", U.guard($w("wz-confirm-connected"), async () => {
      try {
        const kind = ORDER[lastState.reference_index];
        const tempVal = $w("wz-temp").value;
        const job = await wizardAction({
          action: "set_reference", session_dir: sessionDir, kind, connection_point: $w("wz-cp").value,
          temperature_c: tempVal === "" ? null : Number(tempVal), temperature_source: $w("wz-temp-src").value || "not measured",
        }, 15000);
        wzErr(""); render(job.facts);
      } catch (err) { wzErr(`confirm failed: ${msg(err)}`); }
    }, "CONFIRMING…"));

    $w("wz-skip").addEventListener("click", U.guard($w("wz-skip"), async () => {
      if (!confirm("Skip this reference? It will not be measured.")) return undefined;
      try {
        const job = await wizardAction({ action: "skip_reference", session_dir: sessionDir, reason: "operator: not available" }, 15000);
        wzErr(""); render(job.facts);
      } catch (err) { wzErr(`skip failed: ${msg(err)}`); }
    }, "SKIPPING…"));

    $w("wz-capture").addEventListener("click", U.guard($w("wz-capture"), async () => {
      try {
        const sim = $w("wz-simulate").value;
        const job = await wizardAction({ action: "capture", session_dir: sessionDir, simulate: sim || undefined }, 120000);
        wzErr(""); render(job.facts);
      } catch (err) { wzErr(`capture failed: ${msg(err)}`); }
    }, "CAPTURING…"));

    $w("wz-next").addEventListener("click", U.guard($w("wz-next"), async () => {
      try {
        const job = await wizardAction({ action: "next", session_dir: sessionDir }, 15000);
        wzErr(""); render(job.facts);
      } catch (err) { wzErr(`next failed: ${msg(err)}`); }
    }, "…"));

    $w("wz-finish").addEventListener("click", U.guard($w("wz-finish"), async () => {
      if (!confirm("Confirm the antenna is physically reconnected?")) return undefined;
      try {
        const job = await wizardAction({ action: "finish", session_dir: sessionDir }, 20000);
        wzErr(""); render(job.facts);
      } catch (err) { wzErr(`finish failed: ${msg(err)}`); }
    }, "FINISHING…"));

    $w("wz-abort").addEventListener("click", U.guard($w("wz-abort"), async () => {
      if (!confirm("Abort the wizard? Progress already on disk is kept, but the wizard resets.")) return undefined;
      try {
        await wizardAction({ action: "abort", session_dir: sessionDir }, 15000);
        wzErr(""); sessionDir = null; $w("wizard-active").hidden = true; $w("wizard-setup").hidden = false;
      } catch (err) { wzErr(`abort failed: ${msg(err)}`); }
    }, "…"));

    recoverWizard().catch(() => {});
  }
})();
