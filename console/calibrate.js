// ALMITA CALIBRATE — calls the :8090 CALIBRATE API only. All clipping/
// headroom/bandpass/gain/quality numbers come from calibration_engine via
// the backend - this file only presents, orchestrates, and polls.
(function () {
  "use strict";
  const apiRoot = `${location.protocol}//${location.hostname}:${location.port || 8090}`;
  let scenario = "HEALTHY";
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
  function badgeClass(text) { return "badge status-" + String(text || "unknown").toLowerCase().replace(/[^a-z0-9]/g, "-"); }
  function verifyTag(v) {
    if (!v) return "";
    const cls = v === "VERIFIED_BY_DEVICE_READBACK" ? "verify-device"
              : v === "VERIFIED_BY_SERVICE_COMMAND_LINE" ? "verify-service" : "verify-expected";
    return `<span class="verify-tag ${cls}">${v.replace(/_/g, " ")}</span>`;
  }

  function card(label, value, tag) {
    const el = document.createElement("div");
    el.className = "card";
    el.innerHTML = `<dt>${label}</dt><dd>${value}${tag ? " " + tag : ""}</dd>`;
    return el;
  }

  async function refreshStatus() {
    try {
      const status = await getJSON("/api/calibrate/status");
      const d = status.data || {};
      const cards = document.getElementById("receiver-cards");
      cards.innerHTML = "";
      const r = d.receiver || {};
      const field = (obj) => obj && typeof obj === "object" ? obj : { value: obj, verification: null };
      cards.appendChild(card("SERIAL", r.serial || "—"));
      const cf = field(r.center_frequency_hz);
      cards.appendChild(card("CENTER FREQ", cf.value ? `${(cf.value / 1e6).toFixed(6)} MHz` : "—", verifyTag(cf.verification)));
      const sr = field(r.sample_rate_hz);
      cards.appendChild(card("SAMPLE RATE", sr.value ? `${(sr.value / 1e6).toFixed(2)} MS/s` : "—", verifyTag(sr.verification)));
      const gain = field(r.gain_db);
      cards.appendChild(card("GAIN", gain.value != null ? `${gain.value} dB` : "—", verifyTag(gain.verification)));
      const biast = field(r.bias_t_state);
      cards.appendChild(card("BIAS-T", biast.value || "—", verifyTag(biast.verification)));
      const tuner = field(r.tuner_type);
      cards.appendChild(card("TUNER", tuner.value || (r.tuner || "—"), verifyTag(tuner.verification)));

      const fa = d.frequency_audit;
      document.getElementById("frequency-audit").textContent = fa
        ? `${fa.label}\nservice center frequency: ${fa.service_center_frequency_hz} Hz\nnominal HI rest: ${fa.nominal_hi_rest_hz} Hz\ndifference: ${fa.difference_hz} Hz (configuration difference - NOT a measured frequency error)`
        : "";

      const resource = d.resource || {};
      document.getElementById("resource-summary").textContent = `${resource.status}: ${resource.detail}`;
      const busy = resource.status !== "FREE";
      document.getElementById("btn-run-real").disabled = true; // real capture from web not wired this iteration
      const reasonEl = document.getElementById("run-blocked-reason");
      if (busy) {
        reasonEl.hidden = false;
        reasonEl.textContent = `REAL CALIBRATION: BLOCKED — ${resource.detail}`;
      } else {
        reasonEl.hidden = false;
        reasonEl.textContent = "REAL CALIBRATION: BLOCKED — not wired to hardware from the web in this iteration; use calibration_operational_realtest.py";
      }

      renderGainTable(d.gain_table);
      renderSessionList(d.sessions || []);
      renderProfileList(d.profiles || []);
      showError("");
    } catch (err) { showError(`Status request failed: ${err}`); }
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
    el.innerHTML = "";
    if (!sessions.length) { el.innerHTML = '<div class="obs-check-row">NO CALIBRATION SESSION YET</div>'; return; }
    for (const s of sessions) {
      const row = document.createElement("div");
      row.className = "obs-check-row";
      row.textContent = `${s.session_id}  [${s.phase}]`;
      el.appendChild(row);
    }
  }
  function renderProfileList(profiles) {
    const el = document.getElementById("profile-list");
    el.innerHTML = "";
    if (!profiles.length) { el.innerHTML = '<div class="obs-check-row">NO PROFILE</div>'; return; }
    for (const p of profiles) {
      const row = document.createElement("div");
      row.className = "obs-check-row";
      row.textContent = `${p.session_id}  [DRAFT]`;
      el.appendChild(row);
    }
  }

  document.querySelectorAll(".scenario-btn").forEach((btn) => {
    btn.addEventListener("click", () => {
      document.querySelectorAll(".scenario-btn").forEach((b) => b.classList.remove("active"));
      btn.classList.add("active");
      scenario = btn.dataset.scenario;
    });
  });

  async function doRunSimulation() {
    document.getElementById("btn-run-sim").disabled = true;
    try {
      const res = await postJSON("/api/calibrate/run", { scenario, n_captures: 5, capture_seconds: 1.0 });
      if (res.blocked) { showError(res.reason || "RUN blocked"); document.getElementById("btn-run-sim").disabled = false; return; }
      currentSessionId = res.data.session_id;
      document.getElementById("overview-panel").hidden = false;
      pollSession();
    } catch (err) { showError(`RUN failed: ${err}`); document.getElementById("btn-run-sim").disabled = false; }
  }

  function pollSession() {
    if (pollTimer) clearInterval(pollTimer);
    const tick = async () => {
      try {
        const res = await getJSON(`/api/calibrate/session/${encodeURIComponent(currentSessionId)}`);
        if (res.blocked) return;
        renderResult(res.data);
        if (!res.data.job_running) {
          clearInterval(pollTimer); pollTimer = null;
          document.getElementById("btn-run-sim").disabled = false;
          document.getElementById("btn-profile-build").disabled = !res.data.result;
          refreshStatus();
        }
      } catch (err) { /* transient */ }
    };
    tick();
    pollTimer = setInterval(tick, 2000);
  }

  function renderResult(data) {
    const state = data.state || {};
    const result = data.result;
    const badge = document.getElementById("run-badge");
    badge.textContent = state.phase || "RUNNING";
    badge.className = badgeClass(badge.textContent);
    if (!result) return;

    const cardsEl = document.getElementById("overview-cards");
    cardsEl.innerHTML = "";
    const worstClip = result.per_capture.reduce((worst, c) => {
      const order = ["OK", "WARNING", "CLIPPED", "UNKNOWN"];
      return order.indexOf(c.clipping.status) > order.indexOf(worst) ? c.clipping.status : worst;
    }, "OK");
    const minBand = Math.min(...result.per_capture.map((c) => c.usable_band_fraction));
    cardsEl.appendChild(card("CLIPPING", worstClip));
    cardsEl.appendChild(card("HEADROOM", `${result.per_capture[0].clipping.percentile_margin_codes?.toFixed(0) ?? "—"} codes`));
    cardsEl.appendChild(card("USABLE BAND", `${(minBand * 100).toFixed(1)}%`));
    cardsEl.appendChild(card("STABILITY", result.stability ? `${(result.stability.power_rms_fraction * 100).toFixed(3)}%` : "n/a"));
    cardsEl.appendChild(card("QUALITY", result.quality ? result.quality.verdict : "—"));

    document.getElementById("stability-panel").hidden = !result.stability;
    if (result.stability) {
      document.getElementById("stability-summary").textContent =
        `power_rms_fraction: ${result.stability.power_rms_fraction.toExponential(3)}\n` +
        `drift_slope_per_hour: ${result.stability.drift_slope_per_hour.toExponential(3)}\n` +
        `WARM-UP DETECTED: ${result.stability.warmup_detected}\n` +
        `WARM-UP RECOMMENDATION: ${result.stability.warmup_detected ? result.stability.warmup_reason : "NOT ESTABLISHED"}`;
    }

    document.getElementById("thermal-panel").hidden = false;
    const temps = result.temperature_before || {};
    document.getElementById("thermal-summary").textContent = Object.keys(temps).length
      ? JSON.stringify(temps, null, 2) : "No temperature sensors configured/available for this session.";

    document.getElementById("bandpass-panel").hidden = false;
    drawBandpass(result.per_capture[0]);
    document.getElementById("bandpass-notes").textContent =
      `usable_band_fraction: ${(minBand * 100).toFixed(1)}%  dc_half_width_hz: ${result.per_capture[0].dc_half_width_hz?.toFixed(0) ?? "—"}\n` +
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
    const hist = pointCapture.sample_statistics.histogram;
    const max = Math.max(...hist, 1);
    ctx.fillStyle = "#65b7d8";
    const barWidth = canvas.width / hist.length;
    hist.forEach((v, i) => {
      const h = (v / max) * (canvas.height - 10);
      ctx.fillRect(i * barWidth, canvas.height - h, Math.max(1, barWidth - 0.5), h);
    });
    ctx.fillStyle = "#91a2ad";
    ctx.font = "10px monospace";
    ctx.fillText("ADC code histogram (raw sample statistics) — not the RF bandpass shape", 6, 12);
  }

  async function doReplay() {
    const sessionDir = document.getElementById("replay-session-dir").value.trim();
    if (!sessionDir) return;
    try {
      const res = await postJSON("/api/calibrate/replay", { session_dir: sessionDir });
      document.getElementById("replay-summary").textContent = JSON.stringify(res.data || res, null, 2).slice(0, 4000);
    } catch (err) { showError(`REPLAY failed: ${err}`); }
  }
  async function doCompare() {
    const a = document.getElementById("compare-a").value.trim();
    const b = document.getElementById("compare-b").value.trim();
    if (!a || !b) return;
    try {
      const res = await postJSON("/api/calibrate/compare", { session_a: a, session_b: b });
      document.getElementById("compare-summary").textContent = JSON.stringify(res.data || res, null, 2);
    } catch (err) { showError(`COMPARE failed: ${err}`); }
  }
  async function doProfileBuild() {
    if (!currentSessionId) return;
    try {
      const res = await postJSON("/api/calibrate/profile/build", { session_id: currentSessionId });
      document.getElementById("profile-summary").textContent = JSON.stringify(res.data || res, null, 2);
      refreshStatus();
    } catch (err) { showError(`PROFILE BUILD failed: ${err}`); }
  }

  document.getElementById("btn-run-sim").addEventListener("click", doRunSimulation);
  document.getElementById("btn-refresh-status").addEventListener("click", refreshStatus);
  document.getElementById("btn-replay").addEventListener("click", doReplay);
  document.getElementById("btn-compare").addEventListener("click", doCompare);
  document.getElementById("btn-profile-build").addEventListener("click", doProfileBuild);

  refreshStatus();
  setInterval(refreshStatus, 5000);
})();
