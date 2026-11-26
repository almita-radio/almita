// ALMITA DEMO service worker - SIMULATED. Answers every request of the real console pages from a recording of the
// demo container (demo/record), so the static site behaves like the real web: pages, API, products, images.
//
// Replay clock: the recording is one timeline (seconds since it began). The site starts at the first mark; a POST that
// was recorded (START OBSERVATION, RUN REDUCE, RUN SCIENCE, START EXPERIMENT ...) moves the clock to the moment it was
// recorded and lets it run SPEED times faster until the next recorded action. A GET is answered with the latest
// response recorded for that URL at or before the clock (or the first one, if the clock is earlier). POSTs that were
// never recorded (SYNC, delete, mount stop, settings) answer "disabled in the SIMULATED demo".
"use strict";
const SPEED = 10;
const BANNER = `<div id="almita-demo-banner" style="position:sticky;top:0;z-index:99999;background:#e8590c;color:#fff;font:600 13px/1.4 system-ui,sans-serif;letter-spacing:.04em;padding:6px 12px;text-align:center">SIMULATED DEMO · a recording of ALMITA's real software driving a simulated telescope and SDR (sky: HI4PI, HI4PI Collaboration 2016, A&amp;A 594, A116). Nothing here is a real observation. <a href="/about-demo.html" style="color:#fff">How it works</a></div>`;

let MAN = null;            // {start_t, actions:[{t, method, url, post}], byKey:{key:[entries sorted by t]}}
let clock = null;          // {t0 (recording t), wall0 (ms), until (recording t or null)}

const norm = (u) => {
  const url = new URL(u, self.location.origin);
  const q = [...url.searchParams].filter(([k]) => !["v", "t", "_", "ts", "nocache"].includes(k)).sort(([a], [b]) => a.localeCompare(b));
  return url.pathname + (q.length ? "?" + q.map(([k, v]) => `${k}=${v}`).join("&") : "");
};

async function manifest() {
  if (MAN) return MAN;
  const m = await (await fetch("/rec/manifest.json", { cache: "no-store" })).json();
  m.byKey = {};
  m.byPath = {};
  for (const e of m.entries) {
    (m.byKey[`${e.method} ${e.key}`] ||= []).push(e);
    (m.byPath[`${e.method} ${e.key.split("?")[0]}`] ||= []).push(e);
  }
  MAN = m;
  const saved = await (await caches.open("almita-demo-state")).match("/__clock");
  clock = saved ? await saved.json() : { t0: m.start_t, wall0: Date.now(), until: m.start_t, speed: 0 };
  return m;
}

const now = () => {
  if (!clock) return 0;
  const t = clock.t0 + (Date.now() - clock.wall0) * (clock.speed || 0);
  return clock.until != null ? Math.min(t, clock.until) : t;
};

async function setClock(t0, until) {
  clock = { t0, wall0: Date.now(), until, speed: SPEED };
  await (await caches.open("almita-demo-state")).put("/__clock", new Response(JSON.stringify(clock)));
}

function pick(list, t) {
  let best = null;
  for (const e of list) { if (e.t <= t) best = e; else break; }
  return best || list[0];
}

// the pages compare times with the browser's Date: give them the recording's simulated night, at the replay clock
function dateShim() {
  const fake = (MAN.fake_epoch_ms || 0) + (now() - (MAN.clock_t || 0));
  const off = MAN.fake_epoch_ms ? Math.round(fake - Date.now()) : 0;
  return `<script>(() => { const OFF = ${off}; const D = Date;
    function F(...a) { if (!(this instanceof F)) return new D(D.now() + OFF).toString(); return a.length ? new D(...a) : new D(D.now() + OFF); }
    F.prototype = D.prototype; F.now = () => D.now() + OFF; F.parse = D.parse; F.UTC = D.UTC; window.Date = F; })();</script>`;
}

async function respond(e, extraHtml) {
  const r = await fetch(`/rec/bodies/${e.body}`);
  let buf = await r.arrayBuffer();
  const headers = { "Content-Type": e.ctype || "application/octet-stream", "Cache-Control": "no-store", "X-Almita-Demo": "SIMULATED" };
  if (extraHtml && /text\/html/.test(e.ctype || "")) {
    const html = new TextDecoder().decode(buf).replace(/<head([^>]*)>/i, (m) => m + dateShim()).replace(/<body([^>]*)>/i, (m) => m + extraHtml);
    return new Response(html, { status: e.status || 200, headers });
  }
  return new Response(buf, { status: e.status || 200, headers });
}

const json = (status, body) => new Response(JSON.stringify(body), { status, headers: { "Content-Type": "application/json", "X-Almita-Demo": "SIMULATED" } });

async function handle(req) {
  const url = new URL(req.url);
  if (url.origin !== self.location.origin) return fetch(req);
  if (url.pathname.startsWith("/rec/") || url.pathname === "/sw.js" || url.pathname === "/about-demo.html" || url.pathname === "/simulator.html"
      || url.pathname === "/sim.js" || url.pathname === "/sky.json" || url.pathname === "/home.html" || url.pathname === "/console-tour.html" || url.pathname === "/demo.html" || url.pathname.startsWith("/project/")) return fetch(req);
  // the Space root is the project page; "/" means MONITOR only when it is followed from a console page
  if (url.pathname === "/" && !url.search && req.mode === "navigate" && !(req.referrer && new URL(req.referrer).origin === self.location.origin
      && !/\/(home|console-tour|demo|about-demo|simulator)\.html$/.test(new URL(req.referrer).pathname))) return fetch(req);
  const m = await manifest();
  if (url.pathname === "/__demo_clock") return json(200, { clock, now: now(), start_t: m.start_t, end_t: m.end_t, actions: m.actions.map((a) => [a.t, a.key]) });
  // "/?go=observe.html" (links from the project page) opens that console page; "/" alone is MONITOR, as in the real console
  const go = url.searchParams.get("go");
  if (url.pathname === "/" && go && /^[a-z_]+\.html$/.test(go)) return Response.redirect(new URL("/" + go, self.location.origin).href, 302);
  let path = url.pathname === "/" ? (m.byKey["GET /"] ? "/" : "/index.html") : url.pathname;
  const key = norm(path + url.search);
  if (req.method === "POST") {
    let body = "";
    try { body = await req.clone().text(); } catch { /* none */ }
    // a recorded action: the first recorded POST of this URL (with the closest body) moves the clock there
    const cands = m.actions.filter((a) => a.key === key || a.key.split("?")[0] === key.split("?")[0]);
    if (cands.length) {
      const a = cands.find((c) => c.post === body) || cands.find((c) => c.t >= now()) || cands[0];
      const next = m.actions.find((x) => x.t > a.t && x.pausing);
      await setClock(a.t, next ? next.t : m.end_t);
      const rec = (m.byKey[`POST ${a.key}`] || []).find((x) => x.t === a.t) || (m.byKey[`POST ${a.key}`] || [])[0];
      if (rec) return respond(rec);
    }
    const plain = m.byKey[`POST ${key}`];                      // a recorded POST that is not an action (estimate, plan...)
    if (plain && plain.length) return respond(plain.find((x) => x.post === body) || pick(plain, now()));
    return json(200, { ok: false, status: "DISABLED", error: "Disabled in the SIMULATED demo: this action was not recorded.",
                       message: "Disabled in the SIMULATED demo: this action was not recorded." });
  }
  const list = m.byKey[`GET ${key}`] || m.byPath[`GET ${key.split("?")[0]}`];
  if (!list || !list.length) {
    if (/\.(png|jpg|jpeg|gif|svg)$/i.test(url.pathname)) return new Response(null, { status: 404 });
    return json(404, { ok: false, error: "not recorded in the SIMULATED demo", path: url.pathname });
  }
  const e = pick(list, now());
  return respond(e, req.mode === "navigate" ? BANNER : null);
}

self.addEventListener("install", (ev) => { ev.waitUntil(self.skipWaiting()); });
self.addEventListener("activate", (ev) => { ev.waitUntil(self.clients.claim()); });
self.addEventListener("fetch", (ev) => { ev.respondWith(handle(ev.request).catch((err) => json(500, { ok: false, error: String(err) }))); });
self.addEventListener("message", async (ev) => {
  if (ev.data === "reset") { await manifest(); await setClock(MAN.start_t, MAN.start_t); clock.speed = 0; }
});
