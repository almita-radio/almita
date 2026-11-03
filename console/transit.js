// ALMITA TRANSIT (operator 2026-11-02): the Sun drifting through the still antenna's beam. Reads only what
// transit_logger.py writes (data/runtime/transit_latest.json: the last 180 min) and draws the band power of the last
// 30, 60 and 180 minutes, with the capture Pi's temperature dashed on the right axis. START / STOP switch the logger's user unit (it never moves the mount).
(function () {
  "use strict";
  const U = window.AlmitaUI;
  const $ = (id) => document.getElementById(id);
  U.mountHealthStrip(U.mountHeader("TRANSIT", "SUN THROUGH THE STILL BEAM"));
  U.mountFooter();
  const ROOT = window.ALMITA_RUNTIME_ROOT || "/runtime";
  const POLL_MS = 10000;
  const WINDOWS = [30, 60, 180];
  const css = (name, fallback) => (getComputedStyle(document.documentElement).getPropertyValue(name) || "").trim() || fallback;
  const chile = (ms) => new Date(ms - 3 * 3600e3).toISOString().slice(11, 16);          // Chile, UTC-3
  const kv = (dl, rows) => { dl.textContent = ""; for (const [k, v] of rows) { const dt = document.createElement("dt"), dd = document.createElement("dd"); dt.textContent = k; dd.textContent = v; dl.append(dt, dd); } };
  let doc = null, svc = null, busy = false;
  // dB axis: fixed 0-100 (operator 2026-11-03) or AUTO (the window's own range); remembered in this browser
  let scaleFixed = true;
  try { scaleFixed = localStorage.getItem("almita-transit-scale") !== "auto"; } catch (e) { /* storage blocked: 0-100 */ }

  function setup(canvas) {
    const dpr = Math.max(2, window.devicePixelRatio || 1), box = canvas.getBoundingClientRect();
    const w = box.width || 600, h = box.height || 260;
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    return { ctx, w, h };
  }

  function draw(canvas, rows, minutes, everyS) {
    const { ctx, w, h } = setup(canvas);
    const C = { bg: css("--inset", "#0d1317"), grid: css("--line", "#2b3942"), text: css("--muted", "#91a2ad"),
                power: css("--accent", "#65b7d8"), sun: css("--warn", "#e6b85c"), bad: css("--error", "#e36b6b") };
    ctx.fillStyle = C.bg; ctx.fillRect(0, 0, w, h);
    const padL = 56, padR = 60, padT = 12, padB = 26;
    const t1 = Math.max(doc && doc.updated_utc ? Date.parse(doc.updated_utc) : 0, doc && doc.cleared_utc ? Date.parse(doc.cleared_utc) : 0) || Date.now();
    const t0 = t1 - minutes * 60e3;
    const pts = rows.map((r) => ({ t: Date.parse(r.utc), ...r })).filter((r) => r.t >= t0 && r.t <= t1);
    const good = pts.filter((r) => r.power_db !== null && (r.status === "VALID" || r.status === "CLIPPED"));
    const X = (t) => padL + (w - padL - padR) * (t - t0) / (t1 - t0);
    ctx.font = "11px ui-monospace,monospace"; ctx.fillStyle = C.text;
    // time axis: a tick every 5 / 10 / 30 min
    const stepMin = minutes <= 30 ? 5 : minutes <= 60 ? 10 : 30;
    ctx.strokeStyle = C.grid; ctx.lineWidth = 1; ctx.textAlign = "center";
    for (let t = Math.ceil(t0 / (stepMin * 60e3)) * stepMin * 60e3; t <= t1; t += stepMin * 60e3) {
      ctx.beginPath(); ctx.moveTo(X(t), padT); ctx.lineTo(X(t), h - padB); ctx.stroke();
      ctx.fillText(chile(t), X(t), h - 8);
    }
    if (!good.length) {
      ctx.textAlign = "center"; ctx.fillText("no samples in this window", w / 2, h / 2);
      return;
    }
    let lo = 0, hi = 100;
    if (!scaleFixed) {
      lo = Math.min(...good.map((r) => r.power_db)); hi = Math.max(...good.map((r) => r.power_db));
      const pad = Math.max((hi - lo) * 0.1, 0.05); lo -= pad; hi += pad;
    }
    const Y = (v) => padT + (h - padT - padB) * (hi - v) / (hi - lo);
    ctx.textAlign = "right";
    for (let k = 0; k <= 4; k++) {
      const v = lo + (hi - lo) * k / 4;
      ctx.strokeStyle = C.grid; ctx.beginPath(); ctx.moveTo(padL, Y(v)); ctx.lineTo(w - padR, Y(v)); ctx.stroke();
      ctx.fillText(scaleFixed ? v.toFixed(0) : v.toFixed(2), padL - 6, Y(v) + 4);
    }
    // the capture Pi's temperature (the SDR hangs from it), dashed, right axis in °C, its own range
    const tp = pts.filter((r) => r.pi_temp_c !== null && r.pi_temp_c !== undefined);
    if (tp.length) {
      let tlo = Math.min(...tp.map((r) => r.pi_temp_c)), thi = Math.max(...tp.map((r) => r.pi_temp_c));
      const tpad = Math.max((thi - tlo) * 0.1, 1); tlo = Math.floor(tlo - tpad); thi = Math.ceil(thi + tpad);
      const YT = (c) => padT + (h - padT - padB) * (thi - c) / (thi - tlo);
      ctx.textAlign = "left"; ctx.fillStyle = C.sun;
      for (let k = 0; k <= 4; k++) { const c = tlo + (thi - tlo) * k / 4; ctx.fillText(`${c.toFixed(1)}°C`, w - padR + 4, YT(c) + 4); }
      ctx.strokeStyle = C.sun; ctx.setLineDash([5, 4]); ctx.lineWidth = 1.2; ctx.beginPath();
      tp.forEach((r, i) => (i && r.t - tp[i - 1].t <= 3 * (everyS || 10) * 1e3 ? ctx.lineTo(X(r.t), YT(r.pi_temp_c)) : ctx.moveTo(X(r.t), YT(r.pi_temp_c))));
      ctx.stroke(); ctx.setLineDash([]);
    }
    // band power: a line, broken where samples are missing (> 3 sampling intervals apart); clipped samples in red
    const gapMs = 3 * (everyS || 10) * 1e3;
    ctx.strokeStyle = C.power; ctx.lineWidth = 1.6; ctx.lineJoin = "round"; ctx.beginPath();
    good.forEach((r, i) => {
      if (i && r.t - good[i - 1].t <= gapMs) ctx.lineTo(X(r.t), Y(r.power_db)); else ctx.moveTo(X(r.t), Y(r.power_db));
    });
    ctx.stroke();
    const dot = minutes <= 30 ? 2.2 : minutes <= 60 ? 1.6 : 1.1;
    for (const r of good) {
      ctx.fillStyle = r.status === "CLIPPED" ? C.bad : C.power;
      ctx.beginPath(); ctx.arc(X(r.t), Y(r.power_db), dot, 0, 2 * Math.PI); ctx.fill();
    }
    // the peak of this window
    const top = good.reduce((a, b) => (b.power_db > a.power_db ? b : a));
    ctx.fillStyle = C.text; ctx.textAlign = X(top.t) > w / 2 ? "right" : "left";
    ctx.fillText(`max ${top.power_db.toFixed(2)} dB at ${chile(top.t)}`, X(top.t) + (X(top.t) > w / 2 ? -6 : 6), Math.max(padT + 10, Y(top.power_db) - 6));
  }

  function state() {
    const rows = (doc && doc.rows) || [];
    const last = rows[rows.length - 1];
    const age = doc && doc.updated_utc ? (Date.now() - Date.parse(doc.updated_utc)) / 1000 : Infinity;
    // the unit's own state when the server could read it; otherwise how fresh the last sample is
    const running = svc && svc.active !== null && svc.active !== undefined ? svc.active : age < 3 * ((doc && doc.every_s) || 10) + 15;
    U.setBadge($("state-badge"), running ? "RUNNING" : "STOPPED");
    $("btn-start").disabled = busy || running;
    $("btn-stop").disabled = busy || !running;
    const count = (s) => rows.filter((r) => r.status === s).length;
    kv($("state-kv"), [
      ["LOGGER", running ? `running · last sample ${Number.isFinite(age) ? Math.round(age) + " s ago" : "—"}` : `stopped (the SDR is not being used by TRANSIT)${doc ? ` · last sample ${Math.round(age / 60)} min ago` : ""}`],
      ["LAST SAMPLE", last ? `${chile(Date.parse(last.utc))} Chile · ${last.status}${last.power_db !== null ? ` · ${last.power_db.toFixed(3)} dB` : ""}` : "—"],
      ["PI TEMPERATURE", last && last.pi_temp_c !== null && last.pi_temp_c !== undefined ? `${last.pi_temp_c.toFixed(1)} °C (the Pi the SDR hangs from)` : "—"],
      ["SUN NOW", last && last.sun_alt_deg !== null ? `alt ${last.sun_alt_deg.toFixed(1)}° · az ${last.sun_az_deg.toFixed(1)}°` : "—"],
      ["GAIN", doc ? `${doc.gain_db} dB fixed · ${doc.seconds} s of IQ every ${doc.every_s} s` : "—"],
      ["LAST 180 MIN", `${rows.length} samples · ${count("VALID")} valid · ${count("CLIPPED")} clipped · ${count("SDR_BUSY")} skipped (SDR busy) · ${count("FAILED")} failed`],
    ]);
  }

  function render() {
    state();
    const rows = (doc && doc.rows) || [];
    for (const m of WINDOWS) draw($(`chart-${m}`), rows, m, doc && doc.every_s);
  }

  async function control(action) {
    busy = true; state();
    $("ctl-note").textContent = action === "start" ? "starting the logger…" : "stopping the logger (the sample in progress ends first)…";
    const r = await U.api(`/api/transit/${action}`, { method: "POST", body: { confirm: true }, timeoutMs: 70000 });
    busy = false;
    if (r.ok) { svc = r.data.data; $("ctl-note").textContent = `logger ${svc.active ? "started" : "stopped"} (${svc.state})`; }
    else $("ctl-note").textContent = `${action.toUpperCase()} failed: ${U.errorText(r.error)}`;
    tick();
  }
  const scaleLabel = () => { $("btn-scale").textContent = scaleFixed ? "dB AXIS: 0–100 · show AUTO" : "dB AXIS: AUTO · show 0–100"; };
  scaleLabel();
  $("btn-scale").addEventListener("click", () => {
    scaleFixed = !scaleFixed;
    try { localStorage.setItem("almita-transit-scale", scaleFixed ? "fixed" : "auto"); } catch (e) { /* not remembered */ }
    scaleLabel(); render();
  });
  $("btn-start").addEventListener("click", () => control("start"));
  $("btn-stop").addEventListener("click", () => control("stop"));
  $("btn-clear").addEventListener("click", async () => {
    if (!window.confirm("Clear the graphs and start from zero? (the samples stay saved in data/transit/)")) return;
    const r = await U.api("/api/transit/clear", { method: "POST", body: { confirm: true }, timeoutMs: 15000 });
    $("ctl-note").textContent = r.ok ? `graphs cleared at ${chile(Date.parse(r.data.data.cleared_utc))} Chile - the samples stay saved in data/transit/`
                                     : `CLEAR failed: ${U.errorText(r.error)}`;
    tick();
  });

  async function tick() {
    if (document.hidden) return;
    const s = await U.api("/api/transit/state", { timeoutMs: 15000 });
    svc = s.ok ? s.data.data : null;
    try {
      const r = await fetch(`${ROOT}/${"transit_latest.json"}`, { cache: "no-store" });
      doc = r.ok ? await r.json() : null;
      $("error-banner").hidden = true;
    } catch (e) {
      U.showError($("error-banner"), `could not read ${ROOT}/transit_latest.json: ${e}`);
    }
    render();
  }

  window.addEventListener("resize", render);
  document.addEventListener("visibilitychange", tick);
  tick(); setInterval(tick, POLL_MS);
})();
