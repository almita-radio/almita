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
  a.mark("part3");
  // ---- EXPERIMENT: the 4-minute chain check
  await a.goto("experiment", "/experiment.html"); await a.sleep(5000);
  await a.eval("experiment", set("open-sky", true));
  a.mark("experiment_start");
  await a.waitFor("experiment", `[...document.querySelectorAll(".exp-start")].some((b) => !b.disabled)`, 120000, 3000);
  const started = await a.eval("experiment", `(() => { const bs = [...document.querySelectorAll(".exp-start")];
    const b = bs.find((b) => !b.disabled && /chain check/i.test(b.parentElement.closest("div,li,article,section").textContent)) || bs.find((b) => !b.disabled);
    if (!b) return false; b.click(); return true; })()`);
  a.log("EXPERIMENT started", started);
  await a.sleep(20000);
  await a.waitFor("experiment", `fetch("/api/experiment/state").then(r=>r.json()).then(d=>{const x=d.data||{};const run=(x.runs||[]).find(r=>r.run_id===x.active_run_id)||{};return !(x.service||{}).active || /DONE|COMPLETED|VERDICT|FAILED|STOPPED/.test(run.state||"")})`, 40 * 60000, 10000).catch((e) => a.log(e.message));
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
