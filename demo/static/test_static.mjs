// Visitor test of the static demo: landing -> service worker -> pages; PLAN/START replays; screenshots.
import { spawn } from "node:child_process";
import { writeFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join } from "node:path";
const [base, out] = process.argv.slice(2);
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));
const chrome = spawn("google-chrome", ["--headless=new", "--disable-gpu", "--remote-debugging-port=9334",
  `--user-data-dir=${mkdtempSync(join(tmpdir(), "almita-vis-"))}`, "--window-size=1440,1000", "about:blank"], { stdio: "ignore" });
let wsUrl; for (let i = 0; i < 50 && !wsUrl; i++) { try { wsUrl = (await (await fetch("http://127.0.0.1:9334/json/version")).json()).webSocketDebuggerUrl; } catch { await sleep(200); } }
const ws = new WebSocket(wsUrl); await new Promise((r) => ws.addEventListener("open", r, { once: true }));
let id = 1; const pend = new Map();
ws.addEventListener("message", (e) => { const m = JSON.parse(e.data); if (m.id && pend.has(m.id)) { pend.get(m.id)(m); pend.delete(m.id); } });
const send = (method, params = {}, sessionId) => new Promise((r) => { const i = id++; pend.set(i, r); ws.send(JSON.stringify({ id: i, method, params, ...(sessionId ? { sessionId } : {}) })); });
const { result: { targetId } } = await send("Target.createTarget", { url: "about:blank" });
const { result: { sessionId: S } } = await send("Target.attachToTarget", { targetId, flatten: true });
await send("Page.enable", {}, S); await send("Runtime.enable", {}, S);
await send("Page.addScriptToEvaluateOnNewDocument", { source: "window.confirm=()=>true;" }, S);
const ev = async (x) => (await send("Runtime.evaluate", { expression: x, awaitPromise: true, returnByValue: true }, S)).result?.result?.value;
const go = async (p, w = 6000) => { await send("Page.navigate", { url: base + p }, S); await sleep(w); };
const shot = async (f) => { const r = await send("Page.captureScreenshot", { format: "png" }, S); writeFileSync(join(out, f), Buffer.from(r.result.data, "base64")); };
try {
  await go("/", 8000);
  console.log("after landing:", await ev("location.pathname"), await ev("!!navigator.serviceWorker.controller"));
  await shot("v1_status.png");
  await go("/observe.html", 6000);
  await ev(`document.getElementById("btn-plan").click()`); await sleep(6000);
  console.log("start enabled:", await ev(`!document.getElementById("btn-start").disabled`), await ev(`document.getElementById("btn-start").title`));
  await ev(`document.getElementById("btn-start").click()`); await sleep(5000);
  await go("/index.html", 40000); await shot("v2_monitor.png");
  await go("/reduce.html", 8000); await shot("v3_reduce.png");
  await go("/science.html", 8000); await shot("v4_science.png");
  await go("/experiment.html", 6000); await shot("v5_experiment.png");
} finally { chrome.kill("SIGKILL"); process.exit(); }
