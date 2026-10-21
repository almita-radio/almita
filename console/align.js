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
    // This section is the simulation engine; the real SYNC (Sun and HI) is the checkbox in REAL ALIGNMENT above.
    $("sync-status").textContent = "SYNC not available in simulation — the real SYNC is the option in REAL ALIGNMENT (top of this page)";
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
  // ---- SUN SYNC: the operator pointed the antenna at the Sun by hand; the backend script re-checks every guard itself
  function sunKv(d) {
    const dl = $("sun-sync-kv"); dl.textContent = "";
    const m = d.mount || {}, s = d.sun_eod || {}, f = (x, n) => (x == null ? "—" : Number(x).toFixed(n));
    const rows = [["CHECKED (UTC)", d.utc], ["MOUNT", d.device], ["MOUNT BELIEVES RA / DEC", `${f(m.ra_h, 4)} h / ${f(m.dec_deg, 3)}°`],
      ["PIER SIDE", (m.pier || []).join(", ") || "—"], ["TRACKING", m.tracking || "—"], ["SUN NOW (EOD) RA / DEC", `${f(s.ra_h, 4)} h / ${f(s.dec_deg, 3)}°`],
      ["SEPARATION", d.separation_deg == null ? "unreadable" : `${f(d.separation_deg, 2)}°`]];
    for (const [k, v] of rows) { const dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = k; dd.textContent = v == null ? "—" : String(v); dl.append(dt, dd); }
  }
  async function sunCheck() {
    $("sun-sync-result").textContent = "";
    let d;
    try { d = await getJSON("/api/align/sun_sync"); } catch (err) { showError(msg(err)); U.setEnabled($("btn-sun-sync"), false, "CHECK failed"); return; }
    $("sun-sync-max").textContent = d.max_sep_deg;
    sunKv(d);
    const why = (d.blocked || []).slice();
    if (d.separation_deg == null) why.push("mount coordinates unreadable");
    else if (d.separation_deg > d.max_sep_deg) why.push(`separation ${d.separation_deg.toFixed(2)}° > ${d.max_sep_deg}° - check the pier side`);
    U.setBadge($("sun-sync-badge"), why.length ? "BLOCKED" : "READY");
    U.setEnabled($("btn-sun-sync"), !why.length, why.join("; "));
  }
  async function sunSync() {
    if (!window.confirm("SYNC the mount on the Sun now?\n\nOnly if the antenna is pointed at the Sun by hand and the clutches are locked. " +
                        "The mount's position is overwritten (SYNC) and tracking is switched ON.")) return;
    let r;
    try { r = await postJSON("/api/align/sun_sync", { confirm: true }, 120000); } catch (err) { showError(msg(err)); return; }
    U.setEnabled($("btn-sun-sync"), false, "done: CHECK again before another SYNC");
    if (r.refused) { U.setBadge($("sun-sync-badge"), "REFUSED"); $("sun-sync-result").textContent = "REFUSED: " + r.refused; return; }
    U.setBadge($("sun-sync-badge"), r.ok ? "SYNCED" : "FAILED");
    const post = r.post || {}, rb = r.readback || {};
    $("sun-sync-result").textContent = `${r.ok ? "SYNC sent and accepted" : "SYNC FAILED"} · tracking ON: ${r.tracking_on_ok} · ` +
      `mount now reads ${rb.ra_h == null ? "—" : Number(rb.ra_h).toFixed(4)} h / ${rb.dec_deg == null ? "—" : Number(rb.dec_deg).toFixed(3)}° · ` +
      `separation from the Sun ${post.separation_deg == null ? "—" : Number(post.separation_deg).toFixed(3)}° · saved ${r.saved}`;
    if (r.post) sunKv(r.post);
  }
  U.setEnabled($("btn-sun-sync"), false, "CHECK first");
  $("btn-sun-check").addEventListener("click", U.guard($("btn-sun-check"), sunCheck, "CHECKING…"));
  $("btn-sun-sync").addEventListener("click", U.guard($("btn-sun-sync"), sunSync, "SYNCING…"));

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
    // A refused PLAN/RUN (e.g. "min_elevation must be between ...") is also shown right under its own button: the
    // banner alone sits at the top of the page, out of sight of the operator at the buttons (2026-10-15).
    const inlineErr = (id) => (e) => {
      realErr(e);
      const el = $(id); if (!el) return;
      el.hidden = !e; el.textContent = e ? "REFUSED: " + U.errorText(e) : "";
    };
    const planJob = U.jobPanel($("real-job-align_plan"), null, { onError: inlineErr("real-plan-error"), onChange: () => updateReal(), stopLabel: "STOP ALIGNMENT PLAN" });
    const liveBox = $("real-live");                        // kept by reference: jobPanel moves it under the job log
    const lv = (id) => liveBox.querySelector("#" + id);
    const runJob = U.jobPanel($("real-job-align"), $("real-badge"), { onError: inlineErr("real-run-error"), onChange: (j) => { updateReal(); refreshLive(j); },
                                                                     stopLabel: "STOP ALIGNMENT", afterLog: liveBox });

    // ---------------- LIVE READING: alignment.py's alignment_live.json, one quick reading per point as it is captured
    // (display only - the offset comes from the full analysis). Sun: band power vs the centre point, in dB. HI: the
    // HI-window excess over the side bands, in % points vs the centre point. Meant for a person watching: is the reading changing where it should?
    const SVGNS = "http://www.w3.org/2000/svg";
    const RAMP_NIGHT = ["#2a5468", "#2f7396", "#3f93bb", "#62b3d9", "#9ad6f2"];   // one hue, dark -> light = low -> high
    const RAMP_DAY = ["#0b2f42", "#134f73", "#1d6fa0", "#3a8fc4", "#7fb9dd"];     // DÍA: same hue and direction, darker so the top end shows on white
    const ramp = () => (U.isDay() ? RAMP_DAY : RAMP_NIGHT);
    let lastLive = null;
    let liveFinal = null;
    function svg(tag, attrs, parent, text) {
      const el = document.createElementNS(SVGNS, tag);
      for (const k of Object.keys(attrs || {})) el.setAttribute(k, attrs[k]);
      if (text !== undefined) el.textContent = text;
      if (parent) parent.appendChild(el);
      return el;
    }
    function rampColor(t) {
      const RAMP = ramp();
      t = Math.max(0, Math.min(1, t)) * (RAMP.length - 1);
      const i = Math.min(RAMP.length - 2, Math.floor(t)), f = t - i;
      const a = RAMP[i].match(/\w\w/g).map((h) => parseInt(h, 16)), b = RAMP[i + 1].match(/\w\w/g).map((h) => parseInt(h, 16));
      return "rgb(" + a.map((v, k) => Math.round(v + (b[k] - v) * f)).join(",") + ")";
    }
    const fmt = (v, d) => (v >= 0 ? "+" : "−") + Math.abs(v).toFixed(d);
    const ringOf = (p) => Math.hypot(p.east_deg, p.north_deg);
    function liveValues(live) {
      const sun = live.reference === "sun";
      const pts = (live.points || []).filter((p) => p.power_db != null);
      const centre = pts.find((p) => p.index === 1) || pts[0];
      // both relative to the centre point: the absolute HI excess also carries the receiver's bandpass shape
      const val = (p) => sun ? p.power_db - centre.power_db
        : (p.hi_excess_pct == null || centre.hi_excess_pct == null ? null : p.hi_excess_pct - centre.hi_excess_pct);
      const rows = pts.filter((p) => val(p) != null && isFinite(val(p))).map((p) => ({ ...p, v: val(p) }));
      return { sun, rows, unit: sun ? "dB vs centre" : "% HI vs centre", digits: sun ? 2 : 1 };
    }
    U.onThemeChange(() => { if (lastLive && !liveBox.hidden) renderLive(lastLive); });   // DÍA/NOCHE: the ramp is computed, so redraw (no request)
    function renderLive(live) {
      lastLive = live;
      const box = liveBox; box.hidden = false;
      const planned = live.planned || [], all = live.points || [];
      const { sun, rows, unit, digits } = liveValues(live);
      const n = live.planned_points || planned.length;
      const last = all[all.length - 1];
      lv("real-live-sub").textContent = `${sun ? "SUN · total band power" : "HI · line window vs side bands"} · ${all.length}/${n} points`;
      const lastRow = last && rows.find((r) => r.index === last.index);
      if (!last) lv("real-live-big").textContent = "waiting for the first capture…";
      else if (!lastRow) lv("real-live-big").textContent = `point ${last.index}/${n}: ${last.status}${last.error ? " — " + last.error : ""}`;
      else lv("real-live-big").textContent = `point ${last.index}/${n}   ${fmt(lastRow.v, digits)} ${unit}`;
      lv("real-live-where").textContent = last ? `latest: ring ${ringOf(last).toFixed(1)}° · E ${fmt(last.east_deg, 2)}° N ${fmt(last.north_deg, 2)}° from the centre · ${U.utc(last.utc)}` : "";
      const best = rows.reduce((a, b) => (!a || b.v > a.v ? b : a), null);
      lv("real-live-best").textContent = best && rows.length > 1
        ? `highest so far: point ${best.index} ${fmt(best.v, digits)} ${unit} at E ${fmt(best.east_deg, 2)}° N ${fmt(best.north_deg, 2)}° (ring ${ringOf(best).toFixed(1)}°)` +
          (sun ? (best.index === 1 ? " — the centre is still the strongest" : " — stronger than the centre") : "")
        : "";
      drawTrend(rows, n, planned, unit, digits, last && last.index);
      drawMap(rows, planned, all, last && last.index, digits, unit);
    }
    function drawTrend(rows, n, planned, unit, digits, lastIdx) {
      const el = lv("real-live-trend"); el.textContent = "";
      const W = 460, H = 200, L = 44, R = 10, T = 18, B = 26;
      const vals = rows.map((r) => r.v).concat([0]);
      let lo = Math.min(...vals), hi = Math.max(...vals);
      if (hi - lo < 1e-6) { lo -= 1; hi += 1; }
      const raw = (hi - lo) / 4, mag = Math.pow(10, Math.floor(Math.log10(raw)));
      const step = [1, 2, 2.5, 5, 10].map((m) => m * mag).find((st) => st >= raw);   // round tick values
      lo = Math.floor(lo / step) * step; hi = Math.ceil(hi / step) * step;
      const x = (i) => L + (n > 1 ? (i - 1) / (n - 1) : 0.5) * (W - L - R);
      const y = (v) => T + (hi - v) / (hi - lo) * (H - T - B);
      for (let v = lo; v <= hi + step / 2; v += step) {    // recessive grid + y labels
        svg("line", { x1: L, x2: W - R, y1: y(v), y2: y(v), stroke: "var(--line)", "stroke-width": 1 }, el);
        svg("text", { x: L - 6, y: y(v) + 4, "text-anchor": "end", class: "live-axis" }, el, (Math.abs(v) < step / 2 ? 0 : v).toFixed(step < 1 ? 1 : 0));
      }
      svg("line", { x1: L, x2: W - R, y1: y(0), y2: y(0), stroke: "var(--muted)", "stroke-width": 1, "stroke-dasharray": "3 3" }, el);
      let prevRing = null;                                 // ring boundaries from the planned pattern
      for (const p of planned) {
        const ring = Math.round(ringOf(p) * 100) / 100;
        if (p.index > 1 && ring !== prevRing) {
          const xb = x(p.index - 0.5);
          svg("line", { x1: xb, x2: xb, y1: T - 4, y2: H - B, stroke: "var(--line)", "stroke-width": 1 }, el);
          svg("text", { x: xb + 4, y: T - 6, class: "live-axis" }, el, `ring ${ring.toFixed(1)}°`);
        }
        prevRing = ring;
      }
      svg("text", { x: L, y: H - 6, class: "live-axis" }, el, "point 1 (centre)");
      svg("text", { x: W - R, y: H - 6, "text-anchor": "end", class: "live-axis" }, el, `point ${n}`);
      if (rows.length > 1) svg("path", { d: rows.map((r, i) => `${i ? "L" : "M"}${x(r.index).toFixed(1)},${y(r.v).toFixed(1)}`).join(""),
                                        fill: "none", stroke: "var(--accent)", "stroke-width": 2, "stroke-linejoin": "round", opacity: 0.55 }, el);
      for (const r of rows) {
        const isLast = r.index === lastIdx;
        svg("circle", { cx: x(r.index), cy: y(r.v), r: isLast ? 6 : 4, fill: "var(--accent)", stroke: isLast ? "var(--text)" : "var(--panel)", "stroke-width": 2 }, el);
        const hit = svg("circle", { cx: x(r.index), cy: y(r.v), r: 11, fill: "transparent" }, el);
        svg("title", {}, hit, `point ${r.index}: ${fmt(r.v, digits)} ${unit}\nring ${ringOf(r).toFixed(1)}° · E ${fmt(r.east_deg, 2)}° N ${fmt(r.north_deg, 2)}°\npower ${r.power_db.toFixed(2)} dB` +
                                (r.hi_excess_pct != null ? ` · HI ${fmt(r.hi_excess_pct, 1)} %` : "") + (r.clipping_fraction != null ? ` · clipping ${(r.clipping_fraction * 100).toFixed(3)} %` : ""));
      }
      lv("real-live-trend-cap").textContent = `reading per point (${unit}) in capture order; dashed line = 0`;
    }
    function drawMap(rows, planned, all, lastIdx, digits, unit) {
      const el = lv("real-live-map"); el.textContent = "";
      const S = 240, C = S / 2, M = 18;
      const maxR = Math.max(0.1, ...planned.map(ringOf), ...all.map(ringOf));
      const k = (C - M) / maxR;
      const px = (p) => C - p.east_deg * k, py = (p) => C - p.north_deg * k;   // E left, N up (sky as seen from below)
      svg("line", { x1: M, x2: S - M, y1: C, y2: C, stroke: "var(--line)" }, el);
      svg("line", { x1: C, x2: C, y1: M, y2: S - M, stroke: "var(--line)" }, el);
      svg("text", { x: C, y: 12, "text-anchor": "middle", class: "live-axis" }, el, "N");
      svg("text", { x: 6, y: C + 4, class: "live-axis" }, el, "E");
      svg("text", { x: S - 6, y: C + 4, "text-anchor": "end", class: "live-axis" }, el, "W");
      const byIdx = new Map(rows.map((r) => [r.index, r]));
      const failed = new Set(all.filter((p) => !byIdx.has(p.index)).map((p) => p.index));
      const vs = rows.map((r) => r.v), lo = Math.min(...vs), hi = Math.max(...vs);
      for (const p of planned.length ? planned : all) {
        const r = byIdx.get(p.index);
        const c = svg("circle", { cx: px(p), cy: py(p), r: 6,
          fill: r ? rampColor(hi > lo ? (r.v - lo) / (hi - lo) : 0.5) : "none",
          stroke: failed.has(p.index) ? "var(--error)" : r ? "var(--panel)" : "var(--muted)", "stroke-width": r ? 2 : 1, opacity: r || failed.has(p.index) ? 1 : 0.6 }, el);
        if (p.index === lastIdx) svg("circle", { cx: px(p), cy: py(p), r: 9.5, fill: "none", stroke: "var(--text)", "stroke-width": 2 }, el);
        const hit = svg("circle", { cx: px(p), cy: py(p), r: 11, fill: "transparent" }, el);
        svg("title", {}, hit, r ? `point ${p.index}: ${fmt(r.v, digits)} ${unit}` : failed.has(p.index) ? `point ${p.index}: failed` : `point ${p.index}: not yet`);
        void c;
      }
      if (rows.length) {                                   // colour legend: low -> high, with the real values
        const g = svg("defs", {}, el), lg = svg("linearGradient", { id: "live-ramp" }, g);
        const RAMP = ramp();
        RAMP.forEach((col, i) => svg("stop", { offset: `${(i / (RAMP.length - 1)) * 100}%`, "stop-color": col }, lg));
        svg("rect", { x: 40, y: S + 4, width: S - 80, height: 8, rx: 4, fill: "url(#live-ramp)" }, el);
        svg("text", { x: 36, y: S + 12, "text-anchor": "end", class: "live-axis" }, el, lo.toFixed(digits));
        svg("text", { x: S - 36, y: S + 12, class: "live-axis" }, el, hi.toFixed(digits));
      }
    }
    async function refreshLive(j) {
      const box = liveBox;
      if (!j || j.stage !== "align" || !j.output_dir) { box.hidden = true; liveFinal = null; return; }
      if (j.state !== "RUNNING" && liveFinal === j.job_id) return;     // a finished run: read once, then leave it
      const r = await U.api("/api/ops/file?path=" + encodeURIComponent(j.output_dir + "/alignment_live.json"), { timeoutMs: 8000 });
      if (r.ok && r.data && Array.isArray(r.data.points)) renderLive(r.data);
      else if (j.state === "RUNNING") { box.hidden = false; lv("real-live-big").textContent = "waiting for the first capture…"; }
      else box.hidden = true;                              // an older run without a live file
      if (j.state !== "RUNNING") liveFinal = j.job_id;
    }
    let mode = "hi";              // "hi" | "solar" - the operator's explicit choice; never switched automatically
    let selectedArea = null;      // HI only: "A" | "B" | "C" (the operator's pick); solar always uses the Sun itself
    let sky = null;               // last real /api/ops/align/sky response
    let durationModel = null;     // from /api/ops/align/defaults
    // How long an approved (EXITED, PASS) PLAN stays valid for RUN, in seconds - read from the server
    // (align_defaults()'s own plan_validity_seconds) rather than hardcoded here, so the countdown shown below
    // can never drift from what almita_web_ops.py's build_command() actually enforces server-side when RUN is
    // submitted (the real, authoritative check - this page's own countdown is display-only).
    let planValiditySeconds = null;
    let sunPlanValiditySeconds = null;   // a solar PLAN lasts longer: RUN recomputes the Sun and re-checks altitude/meridian
    function validityFor(params) { return params && params.reference === "sun" && sunPlanValiditySeconds != null ? sunPlanValiditySeconds : planValiditySeconds; }
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
      sunPlanValiditySeconds = d.sun_plan_validity_seconds ?? null;
      $("real-ring-radii").value = d.ring_radii_deg.join(",");
      $("real-ring-points").value = d.ring_points;
      $("real-capture-time").value = d.capture_time_s;
      $("real-beam-fwhm").value = d.beam_fwhm_deg;
      $("real-min-elevation").value = d.min_elevation_deg;
      if (d.max_sync_offset_deg != null) $("real-max-sync").value = d.max_sync_offset_deg;
      if (d.confidence_threshold != null) $("real-sync-threshold").textContent = d.confidence_threshold;
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

    // One sky request at a time, and only the latest one counts: the HI ranking takes ~15 s on the Pi and the
    // server computes them one by one, so every keystroke in a field queued another one and the page sat on
    // "Loading sky view…" past its 60 s timeout (2026-10-15). A refresh asked for meanwhile runs once, after.
    let skyInFlight = false, skyAgain = false, skyDebounce = null;
    function refreshSkySoon() { clearTimeout(skyDebounce); skyDebounce = setTimeout(refreshSky, 1200); }
    async function refreshSky() {
      if (skyInFlight) { skyAgain = true; return; }
      skyInFlight = true;
      try { await fetchSky(); } finally { skyInFlight = false; }
      if (skyAgain) { skyAgain = false; refreshSky(); }
    }
    async function fetchSky() {
      const rc = ringConfig();
      const radii = rc.valid ? rc.radii : [5.0, 2.0, 0.6];
      const points = rc.valid ? rc.points : 16;
      // ring_points and capture_time both matter here: the candidate check now walks the WHOLE ring pattern
      // forward through the real wall-clock schedule of a run (not just an instant), so it needs to know how many
      // points per ring AND how long each capture takes to know when each point would actually be captured.
      const q = `mode=${mode}&ring_radii=${radii.join(",")}&ring_points=${points}&min_elevation=${$("real-min-elevation").value || 20}&beam_fwhm=${$("real-beam-fwhm").value || 20}&capture_time=${$("real-capture-time").value || 20}`;
      // 60 s: the HI ranking took ~21 s on 2026-10-15 with a capture + QUICKLOOK running, past the old 20 s - no
      // areas arrived, none could be picked, and PLAN stayed disabled.
      const r = await U.api(`/api/ops/align/sky?${q}`, { timeoutMs: 60000 });
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

    // The result of the last real RUN, always shown at the end: the estimated offset (or why there is none) and
    // what happened to SYNC - straight from alignment_result.json via the job's facts, never computed here.
    function renderOffset(r) {
      const box = $("real-offset");
      if (!r || r.stage !== "align" || r.state !== "EXITED" || !r.facts) { box.hidden = true; return; }
      const o = r.facts.offset, sd = r.facts.sync_decision || {};
      const lines = [`ALIGNMENT RESULT — ${r.facts.result_status || "?"} (${U.utc(r.ended_utc)})`];
      if (o) {
        lines.push(`OFFSET   ΔRA (east) ${o.offset_ra_deg >= 0 ? "+" : ""}${o.offset_ra_deg.toFixed(3)}°   ΔDec ${o.offset_dec_deg >= 0 ? "+" : ""}${o.offset_dec_deg.toFixed(3)}°   total ${o.separation_deg.toFixed(3)}°`);
        lines.push(`confidence ${o.confidence.toFixed(3)} (threshold ${sd.confidence_threshold != null ? sd.confidence_threshold : "?"}) · residual ${o.residual.toFixed(3)} · ${o.samples} samples`);
      } else {
        lines.push("OFFSET   none estimated (see the result above: not enough valid/robust positions)");
      }
      lines.push(r.facts.sync_applied ? "SYNC     APPLIED — the mount now uses this correction"
        : `SYNC     NOT SENT${sd.reason ? " — " + sd.reason : ""}`);
      box.textContent = lines.join("\n");
      box.className = "align-offset " + (r.facts.sync_applied ? "ok" : o ? "warn" : "muted");
      box.hidden = false;
    }

    function updateReal() {
      const p = planJob.last, r = runJob.last;
      renderOffset(r);
      const exited = p && p.state === "EXITED";
      // Whether the last PLAN's own recorded request params (server-side, verbatim) still match what's currently
      // configured/selected - independent of whether that PLAN PASSed or FAILed (e.g. on its own temporal check).
      // If the ring/capture/beam/min-elevation params changed, or the operator explicitly picked a different A/B/C
      // area or switched mode since, the plan is OBSOLETE regardless of its verdict - it must never be shown as if
      // it still describes the current selection. comparisonParams() (not planParams()) is used here so that the
      // periodic refreshSky() below - which only recomputes A/B/C's live coordinates, not what's selected - can
      // never invalidate an approved PLAN on its own; only an explicit operator change does.
      const paramsMatch = exited && U.paramsEqual(p.params, comparisonParams());
      // Only a PLAN requested from this page load can go OBSOLETE: one recovered from an earlier visit (hours old,
      // no area picked yet) showed the PLAN OBSOLETE box on every page open (2026-10-15); it is simply not shown.
      const stale = exited && !paramsMatch && planApprovedFor.mode !== null;
      // PLAN_VALIDITY_SECONDS countdown - DISPLAY ONLY. The server independently re-derives and enforces the
      // SAME deadline, from the SAME server-recorded p.ended_utc (when that PLAN job's real subprocess
      // exited), the moment RUN is actually submitted - see almita_web_ops.py build_command()'s "align" stage.
      // Nothing here is authoritative; it exists only so the operator can see time passing without needing to
      // guess almita_web_ops.py's own PLAN_VALIDITY_SECONDS. Timed from ended_utc, never from when the operator
      // happened to click PLAN or load this page, and never reset by refreshSky()'s periodic redraw below,
      // which never touches planJob at all.
      const validityS = exited ? validityFor(p.params) : null;
      const passedFresh = exited && paramsMatch && p.verdict === "PASS" && p.ended_utc && validityS != null;
      const ageS = passedFresh ? (Date.now() - Date.parse(p.ended_utc)) / 1000 : null;
      const expired = passedFresh && ageS > validityS;
      const remainingS = passedFresh ? Math.max(0, validityS - ageS) : null;
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
        running ? "an alignment is running" : expired ? `the PLAN's ${validityS}s validity window has passed - PLAN again`
          : stale ? "the PLAN is obsolete (parameters or the selected area changed) - PLAN again"
          : !planned ? "run the real PLAN first (it must PASS)" : "type MOVE (uppercase) to allow movement");
      U.renderRingPatternTotal(rc, $("real-pattern-total"));
      if (!n) $("real-duration-estimate").textContent = "";     // never leave an estimate for a ring pattern that is no longer valid
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
          validityBox.textContent = `PLAN EXPIRED — more than ${validityS}s have passed since it finished with PASS ` +
            `(${U.utc(p.ended_utc)}). RUN is disabled: PLAN again before RUN.`;
        } else if (passedFresh) {
          validityBox.hidden = false;
          validityBox.style.color = "";
          const expiresUtc = U.utc(new Date(Date.parse(p.ended_utc) + validityS * 1000).toISOString());
          validityBox.textContent = `PLAN valid for ${Math.ceil(remainingS)}s more (finished ${U.utc(p.ended_utc)}; expires ${expiresUtc}). ` +
            "If RUN starts before then, it is allowed to finish even if this window elapses during the run.";
        } else {
          validityBox.hidden = true;
        }
      }
    }
    $("real-confirm").addEventListener("input", updateReal);
    $("real-ring-radii").addEventListener("input", () => { refreshSkySoon(); updateReal(); });
    $("real-ring-points").addEventListener("input", () => { refreshSkySoon(); updateReal(); });
    $("real-capture-time").addEventListener("input", () => { refreshSkySoon(); updateReal(); });
    $("real-min-elevation").addEventListener("input", () => { refreshSkySoon(); updateReal(); });
    $("real-beam-fwhm").addEventListener("input", () => { refreshSkySoon(); updateReal(); });

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
    // PLAN always starts over (operator 2026-10-17): a PLAN or RUN still running is stopped first (clean SIGINT, after a
    // confirmation) and waited for; the previous PLAN/RUN shown here is cleared. Their files stay on the server.
    $("real-plan").addEventListener("click", U.guard($("real-plan"), async () => {
      const rc = ringConfig();
      if (!rc.valid || (mode === "hi" && !selectedArea) || (mode === "solar" && !(sky && sky.sun.above_horizon))) return undefined;
      const params = planParams();
      const busy = [planJob, runJob].filter((jp) => jp.last && jp.last.state === "RUNNING");
      if (busy.length) {
        const ids = busy.map((jp) => jp.last.job_id).join(", ");
        if (!confirm(`An alignment job is still running (${ids}).\n\nStop it (clean stop, SIGINT) and PLAN again?\n\nIt does NOT brake the mount: `
                     + "a GOTO already in progress may finish. To halt the mount at once use STOP MOUNT, Ekos or the hand controller.")) return undefined;
        for (const jp of busy) { if (!(await jp.stopAndWait(90000))) return undefined; }
      }
      runJob.clear(); planJob.clear();
      planApprovedFor = { mode, selectedArea };   // record identity at click time - see declaration above
      return planJob.start("align_plan", params);
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
      const jValidityS = validityFor(j.params);
      if (j.ended_utc && jValidityS != null && (Date.now() - Date.parse(j.ended_utc)) / 1000 > jValidityS) {
        alert(`This PLAN's ${jValidityS}s validity window has passed - PLAN again before RUN.`);
        return undefined;
      }
      const ok = confirm(`REAL ALIGNMENT (${mode.toUpperCase()}): the mount will slew and capture MAIN across ${pc ? pc.total_positions : "?"} positions ` +
        `(center + ${pc ? pc.ring_radii_deg.length : "?"} ring(s) x ${pc ? pc.ring_points_per_ring : "?"}, max radius ${pc ? Math.max(...pc.ring_radii_deg) : "?"}°).\n` +
        (mode === "hi" && f && f.center ? `Approved centre: RA ${Number(f.center.ra_hours).toFixed(3)} h, Dec ${Number(f.center.dec_deg).toFixed(2)}° (exactly what RUN will use).\n`
          : mode === "solar" ? "The Sun's position is recomputed fresh at RUN time (it moves); the reference (Sun) and pattern are exactly what was approved.\n" : "") +
        (mode === "solar" ? "RUN will switch OnStep to SOLAR tracking rate and verify it before moving; it restores the previous tracking mode when done or stopped.\n" : "") +
        ($("real-sync").checked
          ? `SYNC at the end: YES, only if confidence ≥ ${$("real-sync-threshold").textContent} and the offset ≤ ${$("real-max-sync").value}° (the mount will GOTO the corrected position and SYNC there).\n`
          : "SYNC at the end: NO (the offset is only shown).\n") +
        "Confirm physically: free travel, cables, antenna, nobody in the way.\nProceed?");
      if (!ok || !j) return undefined;
      // Send the PLAN job's OWN recorded params verbatim (never a fresh live planParams()/comparisonParams()) -
      // this is the actual approved target; it must be exactly what RUN executes, unaffected by anything that
      // happened on screen since PLAN completed.
      return runJob.start("align", { ...j.params, approved_plan_dir: j.output_dir, sync: $("real-sync").checked,
                                     max_sync_offset_deg: Number($("real-max-sync").value) }, $("real-confirm").value);
    }, "STARTING…"));
    // a reload starts clean: only a job still RUNNING is re-adopted (to see and STOP it); finished ones stay on the server
    planJob.recover("align_plan", { runningOnly: true }); runJob.recover("align", { runningOnly: true });
    loadDefaults().then(() => { setMode("hi"); });
    // only while the tab is visible: a forgotten background ALIGN tab kept the server computing the ~20 s HI
    // ranking every 30 s for hours (2026-10-15); coming back to the tab refreshes at once.
    setInterval(() => { if (!document.hidden && (!planJob.last || planJob.last.state !== "RUNNING")) refreshSky(); }, 30000);
    document.addEventListener("visibilitychange", () => { if (!document.hidden) refreshSky(); });
    setInterval(updateReal, 1000);
  }
})();
