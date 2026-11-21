// ALMITA EXPERIMENT (operator 2026-11-21: ready-made pilots that can be started at any moment, on their own page).
// Polls /api/experiment/state: the recipes and when each can run, the runner (its unit + the active run), the runs and
// their verdicts. START needs the open-sky box ticked and a READY recipe; STOP is the unit's SIGINT (capture.py ends
// its point cleanly). Every number shown is what experiment.py computed - the page only draws it.
(function () {
  "use strict";
  const U = window.AlmitaUI;
  const $ = (id) => document.getElementById(id);
  const strip = U.mountHeader("EXPERIMENT", "READY-MADE RECIPES · VERDICT WRITTEN BEFORE THE DATA");
  U.mountFooter();
  U.mountHealthStrip(strip);

  let last = null, busy = false, shown = null, shownSig = "";

  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else if (k === "on") for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn);
      else e.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of children) if (c !== null && c !== undefined) e.append(c instanceof Node ? c : String(c));
    return e;
  }
  const clear = (n) => { n.textContent = ""; return n; };
  const chile = (utc) => (utc ? U.chileStamp(utc) : "—");
  const mins = (s) => `${Math.round((s || 0) / 60)} min`;
  const badge = (text, cls) => el("span", { class: "badge " + (cls || U.stateClass(text)), text });
  const READY_CLASS = { READY: "status-ok", LATER: "status-unknown", NOT_TODAY: "status-bad", ERROR: "status-bad" };
  const VERDICT_CLASS = { DETECTED: "status-ok", HI_OK: "status-ok", MEASURED: "status-ok", MODES_AGREE: "status-ok", MODE_SHIFT: "status-unknown",
    NOT_DETECTED: "status-bad", BASELINE: "status-bad", NO_LINE: "status-bad", NO_DATA: "status-bad", HI_WEAK: "status-unknown", INCONCLUSIVE: "status-unknown" };
  const RUN_CLASS = { RUNNING: "status-ok", CAPTURING: "status-ok", REDUCING: "status-ok", DONE: "status-ok", REPLAY: "status-unknown",
    WAITING: "status-unknown", STOPPED: "status-unknown", PENDING: "status-unknown", IDLE: "status-unknown", HALTED: "status-bad", CRASHED: "status-bad" };

  // ------------------------------------------------------------------ what each kind counts as a result (from the recipe's numbers)
  function ruleText(r) {
    const a = r.analysis || {};
    if (r.kind === "ONOFF") return `DETECTED only if ON − OFF in ${a.window_km_s[0]}…${a.window_km_s[1]} km/s is ≥ 3σ, ≥ half the expected ${a.expected_pct[0]}–${a.expected_pct[1]} %, positive in ≥ 75 % of ≥ ${a.min_pairs} pairs, with the OFFs within ±${a.max_off_abs_pct} %. ON below its OFFs at ≥ 3σ = BASELINE (not gas).`;
    if (r.kind === "AB_VELOCITY") return `Whole-profile fit (scale + linear baseline + shift) of each mode against ${a.reference_mode}: SHIFTED if |shift| > ${a.tolerance_km_s} km/s and > 3σ, SAME if within ±${a.tolerance_km_s} km/s with 2σ ≤ ${a.tolerance_km_s}; ≥ ${a.min_passes} passes per mode. Never the position of the maximum.`;
    if (r.kind === "TANGENT") return `Terminal edge per longitude (${a.edge_fraction} of the spectrum's own peak, ≥ ${a.nsigma}σ); MEASURED with ≥ ${a.min_points} tangent points. V = |v_t| + 220·|sin l|, R = 8.2·|sin l| kpc.`;
    if (r.kind === "LINE_CHECK") return `HI_OK if the line's S/N ≥ ${a.snr_ok}; weak ${a.snr_weak}–${a.snr_ok}; below that NO_LINE (stop: SDR mute, LNA off or feed).`;
    return "";
  }

  // ------------------------------------------------------------------ runner
  function renderRunner(d) {
    const svc = d.service || {}, run = d.active_run;
    const active = !!svc.active, stopping = svc.state === "deactivating";
    let st = run ? (active ? ((run.pilot || {}).state || "RUNNING") : run.status) : (active ? "RUNNING" : "IDLE");
    if (stopping) st = "STOPPING";
    U.setBadge($("run-badge"), st);
    $("run-badge").className = "badge " + (RUN_CLASS[st] || "status-unknown");
    if (!run) {
      $("run-name").textContent = "No experiment run yet.";
      $("run-fill").style.width = "0%";
      clear($("run-steps"));
      $("run-pilot").textContent = d.survey_service && d.survey_service.active ? "The SURVEY pilot is running: EXPERIMENT START waits until it stops." : "";
    } else {
      $("run-name").textContent = `${run.name} · ${run.run_id}${run.replay_of ? " · REPLAY of " + run.replay_of : ""}`;
      const n = run.steps.length || 1;
      $("run-fill").style.width = ((run.steps_done / n) * 100).toFixed(1) + "%";
      clear($("run-steps"));
      for (const s of run.steps) {
        const tip = `batch ${s.index + 1} · mode ${s.mode} · ${s.status}` + (s.targets && s.targets.length ? ` · ${s.targets.join(", ")}` : ` · planned ${(s.planned_targets || []).join(", ")}`)
          + (s.accepted ? ` · ${s.accepted} accepted` : "") + (s.skipped_reason ? ` · ${s.skipped_reason}` : "") + (s.error ? ` · ${s.error}` : "");
        $("run-steps").append(el("span", { class: "exp-step exp-step-" + String(s.status || "PENDING").toLowerCase() + (run.current_step === s.index ? " current" : ""), title: tip, text: s.mode }));
      }
      const p = run.pilot || {};
      $("run-pilot").textContent = `${run.steps_done}/${run.steps.length} batches · ${p.state || ""} · ${p.note || ""} · ${chile(p.updated_utc)}`;
    }
    $("run-stop").disabled = busy || !active || stopping;
    const resumable = run && !active && ["STOPPED", "HALTED", "CRASHED"].includes(run.status);
    $("run-resume").hidden = !resumable;
    $("run-resume").disabled = busy || !resumable || !(d.survey_service && !d.survey_service.active);
  }

  // ------------------------------------------------------------------ recipes
  function renderRecipes(d) {
    const box = clear($("recipes"));
    const recipes = d.recipes || [];
    if (d.recipes_error) box.append(el("p", { class: "status-bad", text: "recipes: " + d.recipes_error }));
    const nReady = recipes.filter((r) => r.readiness && r.readiness.state === "READY").length;
    U.setBadge($("recipes-badge"), `${nReady}/${recipes.length} READY`);
    $("recipes-badge").className = "badge " + (nReady ? "status-ok" : "status-unknown");
    const sky = $("open-sky").checked;
    const blocked = (d.service && d.service.active) ? "an experiment is running" : (d.survey_service && d.survey_service.active) ? "the SURVEY pilot is running" : null;
    for (const r of recipes) {
      const rd = r.readiness || {};
      let when;
      if (rd.state === "READY") when = `READY NOW · startable until ${chile(rd.until_utc)}`;
      else if (rd.state === "LATER") when = `FROM ${chile(rd.start_utc)} · until ${chile(rd.until_utc)}`;
      else if (rd.state === "NOT_TODAY") when = `NOT IN THE NEXT 24 h · best ${Math.round((rd.best_coverage || 0) * 100)} % at ${chile(rd.best_start_utc)}`;
      else when = rd.reason || rd.state || "—";
      const cov = rd.coverage !== undefined ? ` · sees ${Math.round(rd.coverage * 100)} % of the points` : "";
      const alts = Object.entries(rd.alt_now || {}).map(([k, v]) => el("span", { class: "exp-chip" + (v < r.min_altitude_deg ? " low" : ""), text: `${k} ${v.toFixed(0)}°` }));
      const lastRun = r.last_run;
      const start = el("button", { type: "button", class: "exp-start", text: "START",
        on: { click: () => startRecipe(r) } });
      const why = [];
      if (!sky) why.push("tick the open-sky box");
      if (rd.state !== "READY") why.push("not READY now");
      if (blocked) why.push(blocked);
      start.disabled = busy || why.length > 0;
      const card = el("article", { class: "exp-card" },
        el("div", { class: "exp-card-head" }, el("h3", { text: r.name }), badge(rd.state || "—", READY_CLASS[rd.state])),
        el("p", { class: "exp-q", text: r.question }),
        el("p", { class: "exp-when", text: `${when} · ~${mins(rd.duration_s)} · ${r.steps} batch(es) of ${r.capture_seconds} s per point${cov}` }),
        el("div", { class: "exp-chips" }, el("span", { class: "muted", text: "altitude now:" }), ...alts),
        el("p", { class: "exp-modes muted", text: "modes: " + Object.entries(r.modes).map(([k, v]) => `${k} = ${v}`).join(" · ") }),
        r.pending_after && r.pending_after.length ? el("p", { class: "exp-after status-unknown", text: `Run ${r.pending_after.join(", ")} first: its verdict decides how to read this one.` }) : null,
        el("details", {}, el("summary", { text: "why this design · what counts as a result" }), el("p", { text: r.why }), el("p", { class: "exp-rule", text: ruleText(r) })),
        lastRun ? el("p", { class: "exp-last" }, el("span", { class: "muted", text: `last run ${chile(lastRun.created_utc)} · ${lastRun.status} · ` }),
          lastRun.verdict && lastRun.verdict.code ? badge(lastRun.verdict.code, VERDICT_CLASS[lastRun.verdict.code]) : el("span", { class: "muted", text: "no verdict yet" })) : null,
        el("div", { class: "exp-card-ctl" }, start, el("span", { class: "muted", text: why.length ? "(" + why.join(" · ") + ")" : "" })));
      box.append(card);
    }
    if (!recipes.length && !d.recipes_error) box.append(el("p", { class: "muted", text: "No recipes in experiments/." }));
  }

  async function startRecipe(r) {
    const rd = r.readiness || {};
    if (!window.confirm(`START "${r.name}"?\n\n~${mins(rd.duration_s)}, ${r.steps} batch(es). It moves the mount and uses the SDR by itself until it ends or you STOP it.\nThe antenna is under open sky: confirmed.`)) return;
    busy = true;
    $("run-note").textContent = `starting ${r.id}…`;
    const res = await U.api("/api/experiment/start", { method: "POST", body: { confirm: true, recipe_id: r.id, open_sky: $("open-sky").checked }, timeoutMs: 90000 });
    busy = false;
    $("run-note").textContent = res.ok ? `started ${res.data.data.run_id} (${res.data.data.state})` : `START failed: ${U.errorText(res.error)}`;
    if (res.ok) shown = res.data.data.run_id;
    tick();
  }

  async function control(action) {
    const run = last && last.active_run;
    const ask = action === "stop" ? "STOP the experiment? The point being captured ends cleanly (SIGINT); RESUME continues where it stopped."
      : `RESUME ${run ? run.run_id : ""}? It continues with the batches still pending.`;
    if (!window.confirm(ask)) return;
    busy = true;
    $("run-note").textContent = action === "stop" ? "stopping (the current point ends first, up to ~3 min)…" : "resuming…";
    const res = await U.api(`/api/experiment/${action}`, { method: "POST", body: { confirm: true, run_id: run ? run.run_id : undefined }, timeoutMs: 90000 });
    busy = false;
    $("run-note").textContent = res.ok ? `${action} requested (${res.data.data.state})` : `${action.toUpperCase()} failed: ${U.errorText(res.error)}`;
    tick();
  }
  $("run-stop").addEventListener("click", () => control("stop"));
  $("run-resume").addEventListener("click", () => control("resume"));
  $("open-sky").addEventListener("change", () => last && renderRecipes(last));

  // ------------------------------------------------------------------ results
  function renderRuns(d) {
    const tb = clear($("runs-table").querySelector("tbody"));
    const runs = d.runs || [];
    U.setBadge($("results-badge"), `${runs.length} RUN(S)`);
    $("results-badge").className = "badge status-unknown";
    for (const r of runs) {
      const v = r.verdict || {};
      const tr = el("tr", { class: "exp-row" + (r.run_id === shown ? " selected" : ""), "data-run": r.run_id, tabindex: "0", on: { click: () => openRun(r.run_id), keydown: (e) => { if (e.key === "Enter") openRun(r.run_id); } } },
        el("td", { text: chile(r.created_utc) }), el("td", { text: r.name + (r.replay_of ? ` (replay of ${r.replay_of})` : "") }),
        el("td", {}, badge(r.status, RUN_CLASS[r.status])), el("td", { text: `${r.steps_done}/${r.steps}` }),
        el("td", {}, v.code ? badge(v.code, VERDICT_CLASS[v.code]) : el("span", { class: "muted", text: "—" }), " ", v.title || ""));
      tb.append(tr);
    }
    if (!runs.length) tb.append(el("tr", {}, el("td", { colspan: "5", class: "muted", text: "No runs yet." })));
    if (!shown && runs.length) shown = (d.active_run && d.active_run.run_id) || runs[0].run_id;
    if (shown) {
      const sig = JSON.stringify(runs.find((r) => r.run_id === shown) || {});
      if (sig !== shownSig) { shownSig = sig; openRun(shown, true); }
    }
  }

  async function openRun(id, quiet) {
    shown = id;
    const res = await U.api(`/api/experiment/run/${encodeURIComponent(id)}`, { timeoutMs: 20000 });
    if (!res.ok) { if (!quiet) $("run-note").textContent = `cannot open ${id}: ${U.errorText(res.error)}`; return; }
    renderDetail(res.data.data);
    for (const tr of $("runs-table").querySelectorAll("tr.exp-row")) tr.classList.toggle("selected", tr.dataset.run === id);
  }

  function table(head, rows) {
    return el("div", { class: "exp-table-wrap" }, el("table", { class: "exp-table" },
      el("thead", {}, el("tr", {}, ...head.map((h) => el("th", { text: h })))),
      el("tbody", {}, ...rows.map((r) => el("tr", {}, ...r.map((c) => el("td", { text: c === null || c === undefined ? "—" : String(c) })))))));
  }
  const f = (x, nd = 2, sign = false) => (x === null || x === undefined ? "—" : (sign && x > 0 ? "+" : "") + Number(x).toFixed(nd));

  function renderDetail(run) {
    const box = clear($("run-detail"));
    box.hidden = false;
    const a = run.analysis;
    box.append(el("h3", { text: `${run.name} · ${run.run_id}` }));
    if (run.note) box.append(el("p", { class: "muted", text: run.note }));
    if (!a) { box.append(el("p", { class: "muted", text: "No verdict yet: it is written when the last batch is reduced (a stopped or halted run gets a partial one)." })); }
    else if (a.error) box.append(el("p", { class: "status-bad", text: "analysis failed: " + a.error }));
    else {
      const v = a.verdict || {};
      const vb = el("div", { class: "exp-verdict " + (VERDICT_CLASS[v.code] || "status-unknown") },
        el("div", { class: "exp-verdict-head" }, badge(v.code || "—", VERDICT_CLASS[v.code]), el("b", { text: v.title || "" })),
        el("p", { text: v.text || "" }));
      for (const w of v.warnings || []) vb.append(el("p", { class: "status-unknown", text: "⚠ " + w }));
      if (a.partial) vb.append(el("p", { class: "status-unknown", text: "⚠ Partial: not every batch was done." }));
      if (a.caveat) vb.append(el("p", { class: "status-unknown", text: "⚠ " + a.caveat }));
      vb.append(el("p", { class: "muted", text: `${a.spectra_used} reduced spectra · analysed ${chile(a.analysed_utc)}` }));
      box.append(vb);
      for (const p of a.plots || []) box.append(plotBlock(p));
      if (a.kind === "ONOFF" && a.pairs) box.append(table(["pair", "ON %", "OFF %", "ON − OFF %", "OFFs"], a.pairs.map((p, i) => [i + 1, f(p.on_pct), f(p.off_pct), f(p.diff_pct, 2, true), (p.off_ids || []).join(", ")])));
      if (a.kind === "AB_VELOCITY") {
        for (const t of a.targets || []) {
          box.append(table(["target · mode", "passes", "shift vs " + a.reference_mode + " (km/s)", "± sem", "per pass", "fitted over (km/s)"],
            Object.entries(t.modes).map(([m, mv]) => [`${t.target_id} · ${m}`, t.passes[m], f(mv.lag_km_s, 2, true), f(mv.sem_km_s), (mv.pass_lags || []).map((x) => f(x, 1, true)).join(" "),
              mv.fit_from_km_s !== undefined && mv.fit_from_km_s !== null ? `${mv.fit_from_km_s}…${mv.fit_to_km_s}` : "—"])));
          if (t.external) box.append(el("p", { class: "muted", text: `against ${t.external.label}: ` + Object.entries(t.external.lags_km_s).map(([m, x]) => `${m} ${f(x, 2, true)} km/s`).join(" · ") }));
        }
      }
      if (a.kind === "TANGENT") box.append(table(["target", "l", "b", "passes", "v_t (km/s)", "R (kpc)", "V (km/s)", "note"],
        (a.points || []).map((p) => [p.target_id, p.l_deg, p.b_deg, p.passes, f(p.terminal_km_s, 0, true), f(p.R_kpc), f(p.V_km_s, 0), p.used ? "" : p.reason])));
      if (a.kind === "LINE_CHECK") box.append(table(["peak %", "at km/s", "noise %", "S/N"], [[f(a.peak_pct), f(a.peak_km_s, 0, true), f(a.noise_pct, 3), f(a.snr, 0)]]));
    }
    const steps = run.steps || [];
    if (steps.length) {
      const det = el("details", {}, el("summary", { text: `batches (${steps.length})` }),
        table(["#", "cycle", "mode", "status", "targets", "accepted", "ended (Chile)", "note"],
          steps.map((s) => [s.index + 1, (s.cycle || 0) + 1, s.mode, s.status, (s.targets && s.targets.length ? s.targets : s.planned_targets || []).join(", "), s.accepted, chile(s.ended_utc), s.skipped_reason || s.error || ""])));
      box.append(det);
    }
  }

  // ------------------------------------------------------------------ plots (canvas, theme tokens, legend in text)
  const TOKENS = ["--warn", "--accent", "--ok", "--error", "--heading"];
  const css = (name) => getComputedStyle(document.documentElement).getPropertyValue(name).trim() || "#888";

  function plotBlock(p) {
    const wrap = el("figure", { class: "exp-plot" });
    wrap.append(el("figcaption", { text: p.title }));
    const cv = el("canvas", { width: "900", height: "320", role: "img", "aria-label": p.title });
    const read = el("div", { class: "exp-readout muted", text: "move over the plot to read the values" });
    const legend = el("div", { class: "exp-legend" });
    (p.series || []).forEach((s, i) => {
      const ref = /^REF/.test(s.name);
      legend.append(el("span", {}, el("i", { class: "exp-swatch" + (ref ? " dashed" : ""), style: `border-color:var(${ref ? "--muted" : TOKENS[i % TOKENS.length]})` }), s.name));
    });
    wrap.append(cv, legend, read);
    requestAnimationFrame(() => drawPlot(cv, p, read));
    return wrap;
  }

  function drawPlot(cv, p, read) {
    const dpr = window.devicePixelRatio || 1;
    const W = cv.clientWidth || 900, H = Math.round(W * 0.36) || 320;
    cv.width = W * dpr; cv.height = H * dpr; cv.style.height = H + "px";
    const g = cv.getContext("2d");
    g.scale(dpr, dpr);
    const m = { l: 52, r: 12, t: 10, b: 34 };
    const series = (p.series || []).filter((s) => s.x && s.x.length);
    let xs = [], ys = [];
    for (const s of series) s.x.forEach((x, i) => { if (s.y[i] !== null && (!p.xlim || (x >= p.xlim[0] && x <= p.xlim[1]))) { xs.push(x); ys.push(s.y[i]); } });
    if (!xs.length) { g.fillStyle = css("--muted"); g.fillText("no data", W / 2 - 20, H / 2); return; }
    let x0 = p.xlim ? p.xlim[0] : Math.min(...xs), x1 = p.xlim ? p.xlim[1] : Math.max(...xs);
    if (p.kind === "bars" || p.kind === "points") { x0 = Math.min(...xs) - (p.kind === "bars" ? 0.6 : 0.5); x1 = Math.max(...xs) + (p.kind === "bars" ? 0.6 : 0.5); }
    let y0 = Math.min(...ys, 0), y1 = Math.max(...ys, 0);
    if (p.band_y) { y0 = Math.min(y0, p.band_y[0]); y1 = Math.max(y1, p.band_y[1]); }
    if (p.hline !== undefined) { y0 = Math.min(y0, p.hline); y1 = Math.max(y1, p.hline); }
    const pad = (y1 - y0) * 0.08 || 1; y0 -= pad; y1 += pad;
    if (p.kind === "points") y0 = Math.min(y0, 0);
    const X = (x) => m.l + ((x - x0) / (x1 - x0)) * (W - m.l - m.r);
    const Y = (y) => H - m.b - ((y - y0) / (y1 - y0)) * (H - m.t - m.b);
    g.font = "11px ui-monospace,monospace";
    // grid + axes
    g.strokeStyle = css("--row-line"); g.fillStyle = css("--muted"); g.lineWidth = 1;
    const ticks = (a, b, n) => { const st = Math.pow(10, Math.floor(Math.log10((b - a) / n))); const k = [1, 2, 5, 10].find((q) => (b - a) / (st * q) <= n) * st; const out = []; for (let t = Math.ceil(a / k) * k; t <= b + 1e-9; t += k) out.push(+t.toFixed(6)); return out; };
    for (const t of ticks(y0, y1, 6)) { g.beginPath(); g.moveTo(m.l, Y(t)); g.lineTo(W - m.r, Y(t)); g.stroke(); g.fillText(String(t), 4, Y(t) + 4); }
    for (const t of ticks(x0, x1, 10)) { g.fillText(String(t), X(t) - 10, H - m.b + 14); }
    g.fillText(p.x_label || "", W / 2 - 50, H - 4);
    g.fillText(p.y_label || "", m.l + 4, m.t + 10);
    // zero
    g.strokeStyle = css("--line"); g.beginPath(); g.moveTo(m.l, Y(0)); g.lineTo(W - m.r, Y(0)); g.stroke();
    // bands
    g.fillStyle = css("--info-bg");
    if (p.band) g.fillRect(X(p.band[0]), m.t, X(p.band[1]) - X(p.band[0]), H - m.t - m.b);
    if (p.band_y) { g.fillStyle = css("--ok-bg"); g.fillRect(m.l, Y(p.band_y[1]), W - m.l - m.r, Y(p.band_y[0]) - Y(p.band_y[1])); g.fillStyle = css("--muted"); g.fillText("expected", W - m.r - 60, Y(p.band_y[1]) - 3); }
    if (p.hline !== undefined) { g.setLineDash([6, 4]); g.strokeStyle = css("--muted"); g.beginPath(); g.moveTo(m.l, Y(p.hline)); g.lineTo(W - m.r, Y(p.hline)); g.stroke(); g.setLineDash([]); }
    // series
    series.forEach((s, i) => {
      const ref = /^REF/.test(s.name);
      const col = css(ref ? "--muted" : TOKENS[i % TOKENS.length]);
      g.strokeStyle = col; g.fillStyle = col; g.lineWidth = 2;
      if (p.kind === "bars") {
        const w = Math.max(4, (W - m.l - m.r) / (s.x.length + 1) * 0.6);
        s.x.forEach((x, k) => { const y = s.y[k]; if (y === null) return; g.fillRect(X(x) - w / 2, Math.min(Y(0), Y(y)), w, Math.abs(Y(y) - Y(0)) || 1); });
        return;
      }
      if (p.kind === "points") { s.x.forEach((x, k) => { g.beginPath(); g.arc(X(x), Y(s.y[k]), 5, 0, 2 * Math.PI); g.fill(); }); return; }
      if (ref) g.setLineDash([6, 4]);
      g.beginPath();
      let pen = false;
      s.x.forEach((x, k) => {
        const y = s.y[k];
        if (y === null || x < x0 || x > x1) { pen = false; return; }
        if (!pen) { g.moveTo(X(x), Y(y)); pen = true; } else g.lineTo(X(x), Y(y));
      });
      g.stroke(); g.setLineDash([]);
    });
    cv.onmousemove = (e) => {
      const r = cv.getBoundingClientRect();
      const x = x0 + ((e.clientX - r.left - m.l) / (W - m.l - m.r)) * (x1 - x0);
      read.textContent = `${p.x_label}: ${x.toFixed(1)} · ` + series.map((s) => {
        let best = null, bd = Infinity;
        s.x.forEach((xx, k) => { const dd = Math.abs(xx - x); if (dd < bd && s.y[k] !== null) { bd = dd; best = s.y[k]; } });
        return `${s.name.split(" (")[0]}: ${best === null ? "—" : Number(best).toFixed(2)}`;
      }).join(" · ");
    };
  }

  // ------------------------------------------------------------------ poll
  async function tick() {
    const r = await U.api("/api/experiment/state", { timeoutMs: 30000 });
    if (!(r && r.ok && r.data && r.data.ok)) { $("recipes").textContent = r && r.error ? "state unavailable: " + U.errorText(r.error) : "state unavailable"; return; }
    last = r.data.data;
    renderRunner(last);
    renderRecipes(last);
    renderRuns(last);
  }
  tick();
  setInterval(tick, 15000);
})();
