// ALMITA DEMO - the full recording, in the order a visitor would see it. Drives the REAL pages of the demo container
// (SIMULATED instrument). Every step waits for the page's own state, never for a fixed time when it can avoid it.
const OBS = { name: "DEMO-INNER-GALAXY", ra: 18.1294, dec: -20.29, w: 16, h: 16, rows: 5, cols: 5, capture: 20, settle: 2 };

const set = (id, v) => `(() => { const e = document.getElementById(${JSON.stringify(id)}); if (!e) return false;
  if (e.type === "checkbox") e.checked = ${JSON.stringify(v)}; else e.value = ${JSON.stringify(v)};
  e.dispatchEvent(new Event("input", { bubbles: true })); e.dispatchEvent(new Event("change", { bubbles: true })); return true; })()`;
const click = (id) => `(() => { const e = document.getElementById(${JSON.stringify(id)}); if (!e || e.disabled) return false; e.click(); return true; })()`;
const enabled = (id) => `(() => { const e = document.getElementById(${JSON.stringify(id)}); return !!e && !e.disabled && !e.hidden; })()`;
const text = (id) => `(document.getElementById(${JSON.stringify(id)}) || {}).textContent || ""`;
const pickOption = (id, re) => `(() => { const s = document.getElementById(${JSON.stringify(id)}); if (!s) return null;
  const o = [...s.options].find((o) => new RegExp(${JSON.stringify(re)}).test(o.value + " " + o.textContent)); if (!o) return null;
  s.value = o.value; s.dispatchEvent(new Event("change", { bubbles: true })); return o.value; })()`;

export default async function (a) {
  a.mark("part2");
  await a.goto("monitor", "/index.html");
  await a.sleep(15000);
  await a.shot("monitor", "03_monitor_done.png");
  await a.goto("observe", "/observe.html"); await a.sleep(5000);
  await a.goto("status", "/status.html"); await a.sleep(6000);

  // ---- REDUCE: that 25-point grid, ESTIMATOR v2, no profile (switched captures)
  await a.goto("reduce", "/reduce.html"); await a.sleep(5000);
  await a.eval("reduce", click("mode-campaign")); await a.sleep(3000);
  const camp = await a.eval("reduce", pickOption("in-campaign", "DEMO-INNER-GALAXY"));
  a.log("REDUCE input", camp);
  await a.sleep(8000);
  await a.eval("reduce", set("in-estimator-v2", true));
  await a.eval("reduce", set("in-confirm-uncalibrated", true));
  await a.waitFor("reduce", enabled("btn-plan"), 60000);
  await a.eval("reduce", click("btn-plan"));
  await a.waitFor("reduce", enabled("btn-run"), 10 * 60000, 2000);
  a.mark("reduce_run");
  await a.eval("reduce", click("btn-run"));
  await a.waitFor("reduce", `!document.getElementById("results-panel").hidden && /\\d/.test(${text("results-summary")})`, 60 * 60000, 5000);
  await a.sleep(5000);
  await a.eval("reduce", `(() => { const r = document.querySelector("#results-points-table tbody tr"); if (r) r.click(); return !!r; })()`);
  await a.sleep(5000);
  await a.shot("reduce", "04_reduce_done.png");

  // ---- SCIENCE on that REDUCE session
  await a.goto("science", "/science.html"); await a.sleep(5000);
  const ses = await a.eval("science", pickOption("in-session", "DEMO-INNER-GALAXY"));
  a.log("SCIENCE input", ses);
  await a.sleep(8000);
  await a.eval("science", set("in-vmin", "-150")); await a.eval("science", set("in-vmax", "150"));
  await a.waitFor("science", enabled("btn-plan"), 60000);
  await a.eval("science", click("btn-plan"));
  await a.waitFor("science", enabled("btn-run"), 10 * 60000, 2000);
  a.mark("science_run");
  await a.eval("science", click("btn-run"));
  await a.waitFor("science", `!document.getElementById("results-panel").hidden && document.getElementById("img-map-b").naturalWidth > 0`, 60 * 60000, 5000);
  await a.sleep(8000);
  await a.shot("science", "05_science_done.png");

  // ---- SESSIONS: the catalog after the runs
  await a.goto("sessions", "/sessions.html"); await a.sleep(8000);
  await a.eval("sessions", `(() => { const r = document.querySelector("tbody tr"); if (r) r.click(); return !!r; })()`);
  await a.sleep(10000);
  await a.shot("sessions", "06_sessions.png");

  // ---- EXPERIMENT: the 4-minute chain check
  await a.goto("experiment", "/experiment.html"); await a.sleep(5000);
  await a.eval("experiment", set("open-sky", true));
  a.mark("experiment_start");
  await a.waitFor("experiment", `[...document.querySelectorAll(".exp-start")].some((b) => !b.disabled)`, 120000, 3000);
  const started = await a.eval("experiment", `(() => { const bs = [...document.querySelectorAll(".exp-start")];
    const b = bs.find((b) => !b.disabled && /chain check/i.test(b.parentElement.closest("div,li,article,section").textContent)) || bs.find((b) => !b.disabled);
    if (!b) return false; b.click(); return true; })()`);
  a.log("EXPERIMENT started", started);
  await a.waitFor("experiment", `fetch("/api/experiment/state").then(r=>r.json()).then(d=>!(d.data||{}).active_run_id)`, 40 * 60000, 10000).catch((e) => a.log(e.message));
  await a.sleep(10000);
  await a.goto("experiment", "/experiment.html"); await a.sleep(6000);
  await a.shot("experiment", "07_experiment.png");

  // ---- the end: every page once more, after everything ran
  for (const [tab, page] of [["status", "/status.html"], ["sessions", "/sessions.html"], ["science", "/science.html"], ["reduce", "/reduce.html"]]) {
    await a.goto(tab, page); await a.sleep(6000);
  }
  await a.shot("status", "08_status_end.png");
  a.mark("end");
}
