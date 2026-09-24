// ALMITA PIPELINE: the real end-to-end operation on one page. Every button runs the EXISTING ALMITA command through /api/ops/* (almita_web_ops.py);
// the backend decides PASS / PARTIAL / FAIL from the command's real exit code and result files. This page only shows what the backend says.
// ALIGN and CALIBRATE also have their real panels on their own pages (align.html / calibrate.html); the job panel code is shared (common.js).
(function () {
  "use strict";
  const U = window.AlmitaUI;
  const $ = (id) => document.getElementById(id);
  U.mountHealthStrip(U.mountHeader("PIPELINE", "OPERATIONS · REAL"));
  U.mountFooter();
  function el(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
  const banner = (err) => U.showError($("error-banner"), err);
  U.mountReadout($("mount-kv"), $("mount-badge"), 5000);

  // ------------------------------------------------------------------ preflight
  async function runPreflight() {
    const r = await U.api("/api/ops/preflight", { method: "POST", body: {}, timeoutMs: U.timeouts.preflight });
    if (!r.ok) { banner(r.error); return; }
    banner(null);
    const d = r.data.data, body = $("tbl-preflight");
    body.textContent = "";
    for (const c of d.checks) {
      const tr = el("tr"); tr.append(el("td", "", c.name), el("td", "", c.status), el("td", "", c.detail)); tr.children[1].className = "badge status-" + c.status.toLowerCase();
      body.appendChild(tr);
    }
    U.setBadge($("pf-badge"), d.overall);
    $("pf-note").textContent = "real checks at " + U.utc(d.generated_utc);
  }
  $("btn-preflight").addEventListener("click", U.guard($("btn-preflight"), runPreflight, "CHECKING…"));

  // ------------------------------------------------------------------ stages (shared job panels)
  const P = {};
  function mkPanel(stage, badgeId) {
    P[stage] = U.jobPanel($("job-" + stage), badgeId ? $(badgeId) : null, { onError: banner, onChange: () => updateButtons(), onEnd: () => loadChoices() });
    return P[stage];
  }
  mkPanel("align", "align-badge"); mkPanel("align_plan"); mkPanel("calibrate", "calibrate-badge");
  mkPanel("reduce", "reduce-badge"); mkPanel("reduce_plan"); mkPanel("science", "science-badge"); mkPanel("science_plan");

  function reduceParams() { const p = { campaign_dir: $("re-campaign").value }; if ($("re-profile").value) p.calibration_profile = $("re-profile").value; return p; }
  function scienceParams() {
    const p = { reduce_session_dir: $("sc-session").value, beam: $("sc-beam").value };
    if (p.beam === "fwhm") p.beam_fwhm_deg = Number($("sc-fwhm").value);
    return p;
  }
  $("sc-beam").addEventListener("change", () => { $("row-sc-fwhm").hidden = $("sc-beam").value !== "fwhm"; });

  // ------------------------------------------------------------------ ALIGN: mode (HI/SOLAR), sky view, area pick, PLAN/RUN
  let alignMode = "hi", alignArea = null, alignSky = null, alignDurationModel = null;
  function alignRingConfig() { return U.ringConfig($("al-ring-radii"), $("al-ring-points")); }

  async function loadAlignDefaults() {
    const r = await U.api("/api/ops/align/defaults", { timeoutMs: 15000 });
    if (!r.ok) { banner(r.error); return; }
    const d = r.data.data;
    alignDurationModel = d.duration_model;
    $("al-ring-radii").value = d.ring_radii_deg.join(","); $("al-ring-points").value = d.ring_points;
    $("al-capture-time").value = d.capture_time_s; $("al-beam-fwhm").value = d.beam_fwhm_deg; $("al-min-elevation").value = d.min_elevation_deg;
    $("al-beam-note").textContent = `source: ${d.beam_fwhm_source}. ${d.beam_fwhm_note}`;
    updateButtons();
  }
  function setAlignMode(next) {
    alignMode = next; alignArea = null;
    $("al-mode-hi").className = "mode-btn" + (alignMode === "hi" ? " active" : "");
    $("al-mode-solar").className = "mode-btn" + (alignMode === "solar" ? " active" : "");
    $("al-track-mode-row").hidden = alignMode !== "solar";
    refreshAlignSky();
  }
  $("al-mode-hi").addEventListener("click", () => setAlignMode("hi"));
  $("al-mode-solar").addEventListener("click", () => setAlignMode("solar"));

  async function refreshAlignSky() {
    const rc = alignRingConfig(); const radii = rc.valid ? rc.radii : [5.0, 2.0, 0.6];
    const points = rc.valid ? rc.points : 16;
    // ring_points and capture_time both matter here: the candidate check now walks the WHOLE ring pattern
    // forward through the real wall-clock schedule of a run (not just an instant), so it needs to know how many
    // points per ring AND how long each capture takes to know when each point would actually be captured.
    const q = `mode=${alignMode}&ring_radii=${radii.join(",")}&ring_points=${points}&min_elevation=${$("al-min-elevation").value || 20}&beam_fwhm=${$("al-beam-fwhm").value || 20}&capture_time=${$("al-capture-time").value || 20}`;
    const r = await U.api(`/api/ops/align/sky?${q}`, { timeoutMs: 20000 });
    if (!r.ok) { $("al-sky-note").textContent = "sky view unavailable: " + U.errorText(r.error); return; }
    alignSky = r.data.data;
    if (alignArea && !(alignSky.areas || []).some((a) => a.label === alignArea)) alignArea = null;
    drawAlignSky();
  }
  function drawAlignSky() {
    if (!alignSky) return;
    U.drawSkyView($("al-sky"), alignSky, { selected: alignMode === "solar" ? "SUN" : alignArea, onPick: alignMode === "hi" ? (label) => { alignArea = label; drawAlignSky(); renderAlignAreas(); updateButtons(); } : null });
    if ($("al-sky-legend")) {
      if (alignMode === "hi" && alignSky.hi4pi_grid) { $("al-sky-legend").hidden = false; U.drawHi4piLegend($("al-sky-legend"), alignSky.hi4pi_grid.value_range_1e20cm2, "N_HI (cm⁻²)"); }
      else { $("al-sky-legend").hidden = true; }
    }
    const parts = [`sky computed ${U.utc(alignSky.computed_utc)} · Sun: az ${alignSky.sun.az_deg.toFixed(1)}° alt ${alignSky.sun.alt_deg.toFixed(1)}° ${alignSky.sun.above_horizon ? "(up)" : "(below horizon)"}`];
    if (alignMode === "hi") {
      if (alignSky.catalog_source) {
        const cs = alignSky.catalog_source;
        parts.push(`HI map: ${cs.name} - ${cs.product} - ${cs.citation} - unit ${cs.unit} - NOT an Almita measurement`);
      } else if (alignSky.hi4pi_error) {
        parts.push(`HI map unavailable: ${alignSky.hi4pi_error}`);
      }
    }
    parts.push(alignSky.ranking_note || "");
    $("al-sky-note").textContent = parts.join(" · ");
    renderAlignAreas();
  }
  function renderAlignAreas() {
    const box = $("al-hi-areas"); box.textContent = "";
    if (alignMode !== "hi" || !alignSky) return;
    if (!alignSky.ranking_defendible || !(alignSky.areas || []).length) {
      box.appendChild(document.createTextNode("No defendible HI ranking right now (" + (alignSky.ranking_note || "reason unavailable") + "). "));
      const b = el("button", "", "USE MANUAL HI ALIGN (no suggested area)"); b.type = "button";
      b.addEventListener("click", () => { alignArea = "MANUAL"; drawAlignSky(); updateButtons(); });
      box.appendChild(b); return;
    }
    for (const a of alignSky.areas) {
      const row = el("div", "obs-check-row");
      const b = el("button", alignArea === a.label ? "mode-btn active" : "mode-btn",
        `${a.label}: az ${a.az_deg.toFixed(1)}° alt ${a.alt_deg.toFixed(1)}° · score ${a.score.toFixed(2)} (contrast ${a.contrast_1e20cm2.toFixed(3)}, mean ${a.mean_1e20cm2.toFixed(3)} ×10²⁰ cm⁻²)` +
        U.temporalMarginText(a));
      b.type = "button"; b.addEventListener("click", () => { alignArea = a.label; drawAlignSky(); updateButtons(); });
      row.appendChild(b); box.appendChild(row);
    }
  }
  $("al-sky-refresh").addEventListener("click", U.guard($("al-sky-refresh"), refreshAlignSky, "..."));
  function alignSelectedCenter() {
    if (alignMode !== "hi" || alignArea === "MANUAL" || !alignSky) return null;
    const a = (alignSky.areas || []).find((x) => x.label === alignArea);
    return a ? { ra_hours: a.ra_hours, dec_deg: a.dec_deg } : null;
  }
  function estimateAlignDuration(n) {
    if (!alignDurationModel) return null;
    const ct = Number($("al-capture-time").value) || 20;
    const acquire = n * (alignDurationModel.overhead_per_point_s + ct);
    const analysis = alignMode === "hi"
      ? alignDurationModel.ensemble_fixed_s + n * (alignDurationModel.psd_per_20s_capture_s + alignDurationModel.metric_per_20s_capture_s) * (ct / 20)
      : n * alignDurationModel.solar_metric_per_20s_capture_s * (ct / 20);
    return acquire + analysis;
  }
  function alignPlanParams() {
    const rc = alignRingConfig();
    const p = { reference: alignMode === "solar" ? "sun" : "hi", ring_radii: rc.radii, ring_points: rc.points, capture_time: Number($("al-capture-time").value),
      beam_fwhm: Number($("al-beam-fwhm").value), min_elevation: Number($("al-min-elevation").value) };
    const c = alignSelectedCenter();
    if (c) { p.center_ra_hours = c.ra_hours; p.center_dec_deg = c.dec_deg; }
    return p;
  }
  U.poller(async () => {
    if (alignMode !== "solar") return true;
    const r = await U.api("/api/ops/mount", { timeoutMs: 10000 });
    if (!r.ok) return false;
    $("al-track-mode").textContent = (r.data.data.track_mode || "unknown").toUpperCase();
    return true;
  }, { intervalMs: 5000 }).start();
  $("al-ring-radii").addEventListener("input", () => { refreshAlignSky(); updateButtons(); });
  $("al-ring-points").addEventListener("input", () => { refreshAlignSky(); updateButtons(); });
  $("al-capture-time").addEventListener("input", () => { refreshAlignSky(); updateButtons(); });
  $("al-min-elevation").addEventListener("input", () => { refreshAlignSky(); updateButtons(); });
  $("al-beam-fwhm").addEventListener("input", () => { refreshAlignSky(); updateButtons(); });

  $("btn-align-plan").addEventListener("click", U.guard($("btn-align-plan"), () => {
    const rc = alignRingConfig();
    if (!rc.valid || (alignMode === "hi" && !alignArea) || (alignMode === "solar" && !(alignSky && alignSky.sun.above_horizon))) return undefined;
    return P.align_plan.start("align_plan", alignPlanParams());
  }));
  $("btn-align-run").addEventListener("click", U.guard($("btn-align-run"), () => {
    const j = P.align_plan.last, f = j && j.facts, pc = f && f.pattern_config;
    if (!j || j.state !== "EXITED" || j.verdict !== "PASS" || !U.paramsEqual(j.params, alignPlanParams())) {
      alert("PLAN is obsolete or missing (parameters or the selected area changed since PLAN ran) - PLAN again before RUN.");
      return undefined;
    }
    const ok = confirm(`REAL ALIGNMENT (${alignMode.toUpperCase()}): the mount will slew and capture MAIN across ${pc ? pc.total_positions : "?"} positions ` +
      `(center + ${pc ? pc.ring_radii_deg.length : "?"} ring(s) x ${pc ? pc.ring_points_per_ring : "?"}, max radius ${pc ? Math.max(...pc.ring_radii_deg) : "?"}°).\n` +
      (alignMode === "hi" && f && f.center ? `Approved centre: RA ${Number(f.center.ra_hours).toFixed(3)} h, Dec ${Number(f.center.dec_deg).toFixed(2)}° (exactly what RUN will use).\n`
        : alignMode === "solar" ? "The Sun's position is recomputed fresh at RUN time; the reference (Sun) and pattern are exactly what was approved.\n" : "") +
      (alignMode === "solar" ? "RUN will switch OnStep to SOLAR tracking rate and verify it before moving; it restores the previous mode when done or stopped.\n" : "") +
      "SYNC is never sent.\nConfirm physically: free travel, cables, antenna, nobody in the way.\nProceed?");
    if (!ok || !j) return undefined;
    return P.align.start("align", { ...alignPlanParams(), approved_plan_dir: j.output_dir }, $("al-confirm").value);
  }, "STARTING…"));
  $("btn-calibrate-run").addEventListener("click", U.guard($("btn-calibrate-run"), () => P.calibrate.start("calibrate", { n_captures: Number($("ca-n").value), capture_seconds: Number($("ca-s").value) }), "STARTING…"));
  $("btn-reduce-plan").addEventListener("click", U.guard($("btn-reduce-plan"), () => P.reduce_plan.start("reduce_plan", reduceParams())));
  $("btn-reduce-run").addEventListener("click", U.guard($("btn-reduce-run"), () => {
    if (!confirm("Run REDUCE V1 on this campaign? It writes a new session under data/reduced (nothing is overwritten).")) return undefined;
    return P.reduce.start("reduce", reduceParams());
  }, "STARTING…"));
  $("btn-science-plan").addEventListener("click", U.guard($("btn-science-plan"), () => P.science_plan.start("science_plan", scienceParams())));
  $("btn-science-run").addEventListener("click", U.guard($("btn-science-run"), () => {
    if (!confirm("Run SCIENCE V1 on this REDUCE session? It writes a new session under data/science.")) return undefined;
    return P.science.start("science", scienceParams());
  }, "STARTING…"));

  function updateButtons() {
    const p = P.align_plan.last, r = P.align.last;
    const exited = p && p.state === "EXITED";
    // Whether the last PLAN's own recorded request params still match what's currently configured/selected -
    // independent of PASS/FAIL (see align.js's updateReal() for the full reasoning). If they don't match, the
    // plan is OBSOLETE regardless of its verdict.
    const paramsMatch = exited && U.paramsEqual(p.params, alignPlanParams());
    const stale = exited && !paramsMatch;
    const planned = exited && p.verdict === "PASS" && paramsMatch;
    const running = r && r.state === "RUNNING";
    const rc = alignRingConfig();
    const areaOk = alignMode !== "hi" || !!alignArea;
    const sunOk = alignMode !== "solar" || (alignSky && alignSky.sun.above_horizon);
    U.setEnabled($("btn-align-run"), !!planned && $("al-confirm").value === "MOVE" && !running,
      running ? "an alignment is running" : stale ? "the PLAN is obsolete (parameters or the selected area changed) - PLAN again"
        : !planned ? "run the PLAN first (it must PASS)" : "type MOVE (uppercase) to allow movement");
    U.setEnabled($("btn-align-plan"), rc.valid && areaOk && sunOk,
      !rc.valid ? "fix the ring radii / points above" : !areaOk ? "pick area A, B, C or MANUAL above" : "the Sun is below the horizon right now");
    const staleBox = $("al-plan-stale");
    if (exited && paramsMatch) {
      if (staleBox) staleBox.hidden = true;
      U.renderAlignPreview(p.facts, $("al-preview"), $("al-preview-summary"), "#al-preview-table",
        { computedUtc: p.started_utc, areaLabel: alignMode === "hi" ? alignArea : (alignMode === "solar" ? "SUN" : null) },
        $("al-preview-temporal"));
    } else {
      $("al-preview").hidden = true;
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
    U.renderRingPatternTotal(rc, $("al-pattern-total"));
    if (rc.valid) {
      const n = 1 + rc.radii.length * rc.points, secs = estimateAlignDuration(n);
      $("al-duration-estimate").textContent = secs ? `estimated duration: ~${Math.round(secs / 60)} min (${Math.round(secs)} s) for ${n} positions (movement+settle+capture and both analysis passes)` : "";
    }
  }
  $("al-confirm").addEventListener("input", updateButtons);
  setInterval(updateButtons, 1000);
  setInterval(() => { if (!P.align_plan.last || P.align_plan.last.state !== "RUNNING") refreshAlignSky(); }, 30000);
  loadAlignDefaults().then(() => setAlignMode("hi"));

  // ------------------------------------------------------------------ choices (real directories) and reload recovery
  async function loadChoices() {
    const r = await U.api("/api/ops/campaigns", { timeoutMs: 15000 });
    if (!r.ok) return;
    const d = r.data.data;
    const fill = (sel, rows, blank) => {
      const keep = sel.value; sel.textContent = "";
      if (blank) { const o = el("option", "", blank); o.value = ""; sel.appendChild(o); }
      for (const row of rows) { const o = el("option", "", row.path); o.value = row.path; sel.appendChild(o); }
      if (keep && (rows.some((x) => x.path === keep) || keep === "")) sel.value = keep;
    };
    fill($("re-campaign"), d.campaigns); fill($("sc-session"), d.reduce_sessions); fill($("re-profile"), d.profiles || [], "(none - UNCALIBRATED)");
    for (const id of ["btn-reduce-run", "btn-reduce-plan"]) U.setEnabled($(id), d.campaigns.length > 0, "no campaign found in data/mosaic");
    for (const id of ["btn-science-run", "btn-science-plan"]) U.setEnabled($(id), d.reduce_sessions.length > 0, "no REDUCE session found: run REDUCE first");
  }
  loadChoices().then(() => { for (const stage of Object.keys(P)) P[stage].recover(stage); });

  // ------------------------------------------------------------------ observation: real orchestrator status (poll)
  U.poller(async () => {
    const r = await U.api("/api/observe/status", { timeoutMs: 10000 });
    if (!r.ok) { U.setBadge($("observe-badge"), "UNREADABLE"); return false; }
    const s = r.data, o = s.orchestrator || {}, cs = s.current_session || {};
    U.setBadge($("observe-badge"), o.orchestrator_state || "UNKNOWN");
    const lines = [`orchestrator_state: ${o.orchestrator_state || "UNKNOWN"}   session: ${o.session_id || "—"}   capture_pid: ${o.capture_pid || "—"}`];
    if (cs && cs.state) {
      lines.push(`acquisition: ${cs.state}   point ${cs.point_current ?? "—"} / ${cs.points_total ?? "—"}   current id ${cs.current_point_id || "—"}`,
        `succeeded ${cs.points_success ?? 0}   failed ${cs.points_failed ?? 0}   deferred ${cs.points_deferred ?? 0}   last capture ${U.utc(cs.last_capture_utc)}`,
        `settle ${cs.settle_seconds ?? "—"} s   capture ${cs.capture_seconds ?? "—"} s   updated ${U.utc(cs.updated_utc)}`);
    }
    $("observe-summary").textContent = lines.join("\n");
    return true;
  }, { intervalMs: 3000 }).start();

  updateButtons();
  if (location.hash) { const t = $("stage-" + location.hash.slice(1)); if (t) t.scrollIntoView(); }
})();
