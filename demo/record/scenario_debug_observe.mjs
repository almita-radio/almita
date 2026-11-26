const set = (id, v) => `(() => { const e = document.getElementById(${JSON.stringify(id)}); if (!e) return false;
  if (e.type === "checkbox") e.checked = ${JSON.stringify(v)}; else e.value = ${JSON.stringify(v)};
  e.dispatchEvent(new Event("input", { bubbles: true })); e.dispatchEvent(new Event("change", { bubbles: true })); return true; })()`;
export default async function (a) {
  await a.goto("observe", "/observe.html"); await a.sleep(4000);
  for (const [id, v] of [["f-name", "DEMO-INNER-GALAXY"], ["f-placement", "FIXED_CENTER"], ["f-center-ra", "18.1294"],
      ["f-center-dec", "-20.29"], ["f-width", "16"], ["f-height", "16"], ["f-rows", "5"], ["f-cols", "5"], ["f-capture", "20"], ["f-settle", "2"], ["f-fswitch", true]])
    a.log(id, await a.eval("observe", set(id, v)));
  await a.sleep(5000);
  a.log("plan enabled", await a.eval("observe", `!document.getElementById("btn-plan").disabled`));
  await a.eval("observe", `document.getElementById("btn-plan").click()`);
  await a.sleep(25000);
  a.log("start", await a.eval("observe", `(() => { const b = document.getElementById("btn-start"); return [b.disabled, b.hidden, b.title, b.textContent]; })()`));
  a.log("text", await a.eval("observe", `document.body.innerText.slice(0, 3000)`));
  await a.shot("observe", "dbg.png");
}
