// ALMITA DEMO recorder (SIMULATED instrument): drives headless Chrome through the REAL console pages of the demo
// container and records every request and response with its time, for the static demo's service worker to replay.
//
//   node demo/record/record.mjs <scenario.mjs> <out dir> [base url]
//
// Chrome DevTools Protocol over Node's built-in WebSocket; no npm packages. confirm()/alert() are answered "yes"
// inside the recorded pages (the recording session is the operator pressing the buttons). Only ever pointed at the
// demo container (127.0.0.1:18088), never at the real ALMITA.
import { spawn } from "node:child_process";
import { mkdirSync, writeFileSync, appendFileSync, mkdtempSync } from "node:fs";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { createHash } from "node:crypto";

const [scenarioPath, outDir, base = "http://127.0.0.1:18088"] = process.argv.slice(2);
if (!scenarioPath || !outDir) { console.error("usage: record.mjs <scenario.mjs> <out dir> [base]"); process.exit(2); }
if (!/^http:\/\/127\.0\.0\.1:18088/.test(base)) { console.error("refusing: the recorder only talks to the demo container"); process.exit(2); }
const AUTH = "Basic " + Buffer.from(`demo:${process.env.ALMITA_DEMO_PASSWORD || "almita-demo"}`).toString("base64");
mkdirSync(join(outDir, "bodies"), { recursive: true });
const INDEX = join(outDir, "index.jsonl");
const T0 = Date.now();
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// ---------------------------------------------------------------- the container's simulated clock
// The demo container runs on a simulated night (libfaketime); the pages compare plan times with the browser's Date,
// so the recorded browser gets the same clock: Date shifted by (container now - real now).
const health = await (await fetch(base + "/api/system/health", { headers: { Authorization: AUTH } })).json();
const FAKE_NOW = Date.parse(health.data.generated_utc);
const CLOCK_OFFSET = Math.round((FAKE_NOW - Date.now()) / 1000) * 1000;
const DATE_SHIM = `(() => { const OFF = ${CLOCK_OFFSET}; const D = Date;
  function F(...a) { if (!(this instanceof F)) return new D(D.now() + OFF).toString(); return a.length ? new D(...a) : new D(D.now() + OFF); }
  F.prototype = D.prototype; F.now = () => D.now() + OFF; F.parse = D.parse; F.UTC = D.UTC; window.Date = F; })();`;
appendFileSync(join(outDir, "marks.jsonl"), JSON.stringify({ t: 0, label: "clock", clock_offset_ms: CLOCK_OFFSET, fake_epoch_ms: FAKE_NOW }) + "\n");

// ---------------------------------------------------------------- Chrome
const port = 9333;
const chrome = spawn("google-chrome", ["--headless=new", "--disable-gpu", "--no-first-run", "--no-default-browser-check",
  `--remote-debugging-port=${port}`, `--user-data-dir=${mkdtempSync(join(tmpdir(), "almita-rec-"))}`,
  "--window-size=1440,1000", "--disable-background-timer-throttling", "--disable-renderer-backgrounding",
  "--disable-backgrounding-occluded-windows", "--disable-features=IntensiveWakeUpThrottling,CalculateNativeWinOcclusion", "about:blank"], { stdio: "ignore" });
process.on("exit", () => chrome.kill("SIGKILL"));
let wsUrl = null;
for (let i = 0; i < 50 && !wsUrl; i++) {
  try { wsUrl = (await (await fetch(`http://127.0.0.1:${port}/json/version`)).json()).webSocketDebuggerUrl; } catch { await sleep(200); }
}
const ws = new WebSocket(wsUrl);
await new Promise((r) => ws.addEventListener("open", r, { once: true }));
let nextId = 1;
const pending = new Map(), listeners = [];
ws.addEventListener("message", (ev) => {
  const m = JSON.parse(ev.data);
  if (m.id && pending.has(m.id)) { const { res, rej } = pending.get(m.id); pending.delete(m.id); m.error ? rej(new Error(m.error.message)) : res(m.result); }
  else listeners.forEach((fn) => fn(m));
});
const send = (method, params = {}, sessionId) => new Promise((res, rej) => {
  const id = nextId++; pending.set(id, { res, rej }); ws.send(JSON.stringify({ id, method, params, ...(sessionId ? { sessionId } : {}) }));
});

// ---------------------------------------------------------------- recording
let seq = 0;
const reqs = new Map();          // sessionId:requestId -> request info
listeners.push(async (m) => {
  const sid = m.sessionId, p = m.params || {};
  const key = `${sid}:${p.requestId}`;
  if (m.method === "Network.requestWillBeSent") {
    reqs.set(key, { t: Date.now() - T0, method: p.request.method, url: p.request.url, post: p.request.postData || null, tab: tabName(sid) });
  } else if (m.method === "Network.responseReceived") {
    const r = reqs.get(key); if (r) Object.assign(r, { status: p.response.status, mime: p.response.mimeType, headers: p.response.headers });
  } else if (m.method === "Network.loadingFinished") {
    const r = reqs.get(key); reqs.delete(key);
    if (!r || !r.url.startsWith(base)) return;
    let body = Buffer.alloc(0);
    try { const b = await send("Network.getResponseBody", { requestId: p.requestId }, sid); body = Buffer.from(b.body, b.base64Encoded ? "base64" : "utf8"); } catch { /* no body */ }
    const h = createHash("sha256").update(body).digest("hex").slice(0, 24);
    writeFileSync(join(outDir, "bodies", h), body);
    const url = r.url.slice(base.length);
    appendFileSync(INDEX, JSON.stringify({ seq: seq++, t: r.t, tab: r.tab, method: r.method, url, post: r.post, status: r.status,
      mime: r.mime, ctype: (r.headers || {})["content-type"] || (r.headers || {})["Content-Type"] || r.mime, body: h, size: body.length }) + "\n");
  } else if (m.method === "Network.loadingFailed") {
    reqs.delete(key);
  }
});

// ---------------------------------------------------------------- tabs
const tabs = new Map();          // name -> sessionId
const tabName = (sid) => [...tabs].find(([, s]) => s === sid)?.[0] || "?";
async function tab(name) {
  if (tabs.has(name)) return tabs.get(name);
  const { targetId } = await send("Target.createTarget", { url: "about:blank", newWindow: true });
  const { sessionId } = await send("Target.attachToTarget", { targetId, flatten: true });
  tabs.set(name, sessionId);
  await send("Network.enable", { maxResourceBufferSize: 64e6, maxTotalBufferSize: 512e6 }, sessionId);
  await send("Network.setCacheDisabled", { cacheDisabled: true }, sessionId);
  await send("Network.setExtraHTTPHeaders", { headers: { Authorization: AUTH } }, sessionId);
  await send("Page.enable", {}, sessionId);
  await send("Runtime.enable", {}, sessionId);
  await send("Emulation.setFocusEmulationEnabled", { enabled: true }, sessionId);
  await send("Page.setWebLifecycleState", { state: "active" }, sessionId).catch(() => {});
  await send("Page.addScriptToEvaluateOnNewDocument", { source: DATE_SHIM + "window.confirm=()=>true;window.alert=()=>{};window.prompt=()=>null;" }, sessionId);
  return sessionId;
}

const api = {
  base, sleep,
  log: (...a) => console.log(`[${((Date.now() - T0) / 1000).toFixed(0)}s]`, ...a),
  async goto(name, path) { const s = await tab(name); await send("Page.navigate", { url: base + path }, s); await sleep(1500); return s; },
  async eval(name, expr) {
    const r = await send("Runtime.evaluate", { expression: expr, awaitPromise: true, returnByValue: true }, await tab(name));
    if (r.exceptionDetails) throw new Error(`${name}: ${r.exceptionDetails.text} ${r.exceptionDetails.exception?.description || ""}`);
    return r.result.value;
  },
  async waitFor(name, expr, timeoutMs = 60000, everyMs = 1000) {
    const end = Date.now() + timeoutMs;
    while (Date.now() < end) { try { if (await api.eval(name, expr)) return true; } catch { /* page loading */ } await sleep(everyMs); }
    throw new Error(`timeout waiting for ${expr} on ${name}`);
  },
  async shot(name, file) { const r = await send("Page.captureScreenshot", { format: "png" }, await tab(name)); writeFileSync(join(outDir, file), Buffer.from(r.data, "base64")); },
  mark(label) { appendFileSync(join(outDir, "marks.jsonl"), JSON.stringify({ t: Date.now() - T0, label }) + "\n"); api.log("MARK", label); },
};

try {
  const scenario = await import(resolve(scenarioPath));
  await scenario.default(api);
  api.log("scenario done");
} catch (e) {
  console.error("SCENARIO FAILED:", e.message);
  process.exitCode = 1;
} finally {
  await sleep(1500);
  chrome.kill("SIGKILL");
  process.exit();
}
