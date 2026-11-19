// STATUS > ALMITA'S SKY > SURVEY (operator 2026-11-19: "yo fuera mirando en status cómo este lindo survey va creciendo").
// Polls /api/survey/state; shows progress, the pilot and its last batch; START/STOP the pilot's user unit; hands the
// tiles to sky_tiles.js (window.AlmitaSurvey + an "almita-survey" event) so the map draws them as rings.
(() => {
  "use strict";
  const U = window.AlmitaUI;
  const $ = (id) => document.getElementById(id);
  const panel = $("survey-panel");
  if (!panel || !U) return;
  let busy = false, svc = null;

  const STATE_CLASS = { RUNNING: "status-ok", CAPTURING: "status-ok", REDUCING: "status-ok", COMPLETE: "status-ok",
    WAITING: "status-unknown", IDLE: "status-unknown", STOPPED: "status-unknown", HALTED: "status-bad", CRASHED: "status-bad" };
  const chile = (utc) => {
    if (!utc) return "–";
    const d = new Date(utc);
    return isNaN(d) ? utc : d.toLocaleString("es-CL", { timeZone: "America/Santiago", hour12: false }).replace(",", "");
  };
  const esc = (s) => String(s == null ? "" : s).replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));

  function render(d) {
    if (!d || !d.active_id || !d.tiles) {
      panel.hidden = true;
      window.AlmitaSurvey = null;
      window.dispatchEvent(new Event("almita-survey"));
      return;
    }
    panel.hidden = false;
    svc = d.service;
    const p = d.progress, pil = d.pilot || {}, def = d.definition;
    const running = svc && svc.active;
    const st = running ? (pil.state || "RUNNING") : (pil.state === "HALTED" || pil.state === "COMPLETE" || pil.state === "CRASHED" ? pil.state : "STOPPED");
    $("survey-name").textContent = `SURVEY · ${d.name} (${d.survey_id})`;
    $("survey-badge").textContent = st;
    $("survey-badge").className = "badge " + (STATE_CLASS[st] || "status-unknown");
    $("survey-fill").style.width = (p.fraction * 100).toFixed(1) + "%";
    panel.querySelector(".survey-bar").setAttribute("aria-valuenow", (p.fraction * 100).toFixed(0));
    $("survey-stats").innerHTML = [
      [(p.fraction * 100).toFixed(1) + " %", "done"], [`${p.passes_done}/${p.passes_total}`, "passes"],
      [`${p.tiles_complete}/${p.tiles}`, `tiles with all ${p.passes_per_tile} passes`], [`${p.tiles_started}`, "tiles measured at least once"],
      [p.batches, "batches"], [`${def.spacing_deg}° · r ${def.radius_deg}°`, "spacing · radius"], [`≥ ${def.min_altitude_deg}°`, "altitude floor"],
    ].map(([v, k]) => `<span><b>${esc(v)}</b>${esc(k)}</span>`).join("");
    $("survey-pilot").innerHTML = `pilot: <b>${esc(st)}</b>${running ? "" : " (unit " + esc(svc ? svc.state : "?") + ")"} · ${esc(pil.note || "")} · ${chile(pil.updated_utc)}`;
    const lb = d.last_batch;
    $("survey-last").textContent = lb
      ? `last batch ${lb.batch_id}: ${lb.status} · ${chile(lb.ended_utc || lb.planned_utc)} · credited ${(lb.credited || []).length}, missed ${(lb.missed || []).length}`
        + (lb.error ? ` · ${lb.error}` : "") + (lb.mount ? ` · mount ${lb.mount}` : "")
      : "no batch yet";
    $("survey-start").disabled = busy || !!running || p.complete;
    $("survey-stop").disabled = busy || !running;
    window.AlmitaSurvey = { tiles: d.tiles, passes: def.passes, spacing: def.spacing_deg, name: d.name };
    window.dispatchEvent(new Event("almita-survey"));
  }

  async function tick() {
    const r = await U.api("/api/survey/state", { timeoutMs: 15000 });
    if (r && r.ok && r.data && r.data.ok) render(r.data.data);
  }

  async function control(action) {
    const ask = action === "start"
      ? "START the survey pilot? It moves the mount and uses the SDR by itself, batch after batch, until you STOP it."
      : "STOP the survey pilot? The point being captured ends cleanly (SIGINT) and the batch is reduced on the next START.";
    if (!window.confirm(ask)) return;
    busy = true;
    $("survey-ctl-note").textContent = action === "start" ? "starting the pilot…" : "stopping the pilot (the current point ends first, up to ~3 min)…";
    const r = await U.api(`/api/survey/${action}`, { method: "POST", body: { confirm: true }, timeoutMs: 240000 });
    busy = false;
    $("survey-ctl-note").textContent = r.ok ? `pilot ${r.data.data.active ? "running" : "stopped"} (${r.data.data.state})`
      : `${action.toUpperCase()} failed: ${U.errorText(r.error)}`;
    tick();
  }
  $("survey-start").addEventListener("click", () => control("start"));
  $("survey-stop").addEventListener("click", () => control("stop"));
  tick();
  setInterval(tick, 30000);
})();
