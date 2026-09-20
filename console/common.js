// ALMITA web: shared browser helpers (no framework, no external requests, no globals except window.AlmitaUI).
// The backend is the source of truth: nothing here decides that an operation is allowed, it only makes the real state visible,
// keeps requests bounded, and makes double submits and silent failures difficult.
(function () {
  "use strict";
  const U = {};
  const DASH = "—";

  // ------------------------------------------------------------------ text safety and formatting
  const ESC = { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" };
  U.esc = (v) => String(v === null || v === undefined ? "" : v).replace(/[&<>"']/g, (c) => ESC[c]);
  U.finite = (v) => typeof v === "number" && Number.isFinite(v);
  U.num = (v, digits = 1, unit = "") => (U.finite(v) ? v.toLocaleString(undefined, { maximumFractionDigits: digits }) + (unit ? " " + unit : "") : DASH);
  U.fixed = (v, digits = 2, unit = "") => (U.finite(v) ? v.toFixed(digits) + (unit ? " " + unit : "") : DASH);
  U.mhz = (hz, digits = 6) => (U.finite(hz) ? (hz / 1e6).toFixed(digits) + " MHz" : DASH);
  U.msps = (hz, digits = 2) => (U.finite(hz) ? (hz / 1e6).toFixed(digits) + " MS/s" : DASH);
  U.bytes = (b) => {
    if (!U.finite(b)) return DASH;
    if (b >= 1024 ** 3) return (b / 1024 ** 3).toFixed(2) + " GiB";
    if (b >= 1024 ** 2) return (b / 1024 ** 2).toFixed(1) + " MiB";
    return Math.round(b / 1024) + " KiB";
  };
  U.hms = (seconds) => {
    if (!U.finite(seconds)) return "--:--:--";
    const s = Math.max(0, Math.round(seconds));
    const p = (n) => String(n).padStart(2, "0");
    return `${p(Math.floor(s / 3600))}:${p(Math.floor((s % 3600) / 60))}:${p(s % 60)}`;
  };
  // RA is always shown with its unit (hours) and Dec in degrees: never a bare number.
  U.raHours = (h, digits = 4) => (U.finite(h) ? `${h.toFixed(digits)} h` : DASH);
  U.decDeg = (d, digits = 4) => (U.finite(d) ? `${d.toFixed(digits)}°` : DASH);
  U.deg = (d, digits = 3) => (U.finite(d) ? `${d.toFixed(digits)}°` : DASH);
  // Times are always labelled UTC (backend/provenance are UTC); a bare clock time is ambiguous and never shown.
  U.utc = (iso) => {
    if (!iso) return DASH;
    const t = Date.parse(iso);
    if (!Number.isFinite(t)) return String(iso);
    return new Date(t).toISOString().replace("T", " ").slice(0, 19) + " UTC";
  };
  U.utcTime = (iso) => {
    const full = U.utc(iso);
    return full === DASH ? DASH : full.slice(11);
  };
  U.stateClass = (state) => "status-" + String(state || "unknown").toLowerCase().replace(/[^a-z0-9]+/g, "_");
  U.setBadge = (el, text) => { if (el) { el.textContent = text; el.className = "badge " + U.stateClass(text); } };

  // ------------------------------------------------------------------ bounded, explicit API calls
  const DEFAULT_TIMEOUT_MS = 15000;
  const LONG = { start: 130000, stop: 150000, preflight: 60000, plan: 120000 };  // real long operations: never a short timeout
  U.timeouts = LONG;
  // Never throws. Resolves {ok, status, data, error:{kind: timeout|network|http|parse, endpoint, message, retryable, requestId}}.
  U.api = async function (path, opts) {
    const o = opts || {};
    const method = o.method || "GET";
    const timeoutMs = o.timeoutMs || DEFAULT_TIMEOUT_MS;
    const endpoint = `${method} ${path}`;
    const ctl = typeof AbortController === "function" ? new AbortController() : null;
    const timer = ctl ? setTimeout(() => ctl.abort(), timeoutMs) : null;
    let res;
    try {
      res = await fetch((o.root || "") + path, {
        method, cache: "no-store",
        headers: o.body !== undefined ? { "Content-Type": "application/json" } : undefined,
        body: o.body !== undefined ? JSON.stringify(o.body) : undefined,
        signal: ctl ? ctl.signal : undefined,
      });
    } catch (err) {
      if (timer) clearTimeout(timer);
      const timedOut = !!(err && err.name === "AbortError");
      return { ok: false, status: 0, data: null, error: {
        kind: timedOut ? "timeout" : "network", endpoint, requestId: null, retryable: true,
        message: timedOut ? `no response within ${Math.round(timeoutMs / 1000)} s (backend slow or busy; the operation may still be in progress)`
                          : "backend not reachable (server down or network lost)" } };
    }
    if (timer) clearTimeout(timer);
    let text = "";
    try { text = await res.text(); } catch (e) { /* connection dropped mid-body: handled as a parse failure below */ }
    let data = null, parseFailed = false;
    if (text) { try { data = JSON.parse(text); } catch (e) { parseFailed = true; } }
    const requestId = (res.headers && res.headers.get("X-Request-Id")) || (data && data.request_id) || null;
    if (!res.ok) {
      const message = (data && (data.error || data.message || data.reason)) || `HTTP ${res.status}`;
      return { ok: false, status: res.status, data, error: { kind: "http", endpoint, requestId,
        message: String(message).slice(0, 300), retryable: res.status >= 500 || res.status === 408 || res.status === 429 } };
    }
    if (parseFailed || (res.status !== 204 && data === null)) {
      return { ok: false, status: res.status, data: null, error: { kind: "parse", endpoint, requestId, retryable: true,
        message: "backend answered with something that is not valid JSON" } };
    }
    return { ok: true, status: res.status, data, error: null, requestId };
  };
  U.errorText = (err) => {
    if (!err) return "";
    if (typeof err === "string") return err;
    return `${err.endpoint}: ${err.message}` + (err.requestId ? ` [request ${err.requestId}]` : "") + (err.retryable ? " — you can retry" : "");
  };
  U.showError = (el, err) => {
    if (!el) return;
    const text = U.errorText(err);
    el.textContent = text;
    el.hidden = !text;
    el.setAttribute("role", "alert");
  };
  // Envelope routes (ALIGN/CALIBRATE) answer HTTP 200 with {ok, blocked, reason, data}: a blocked outcome is a result, not a transport failure.
  U.blockedText = (body) => (body && body.blocked ? (body.reason || "blocked by the backend") : "");

  // ------------------------------------------------------------------ buttons: no double submit, reasons when disabled
  U.setEnabled = (btn, enabled, reason) => {
    if (!btn) return;
    btn.dataset.holdDisabled = enabled ? "" : "1";
    btn.disabled = !enabled || btn.dataset.busy === "1";
    btn.title = !enabled && reason ? reason : btn.dataset.baseTitle || "";
    const slot = btn.id ? document.querySelector(`.reason[data-for="${btn.id}"]`) : null;
    if (slot) { slot.textContent = !enabled && reason ? `${btn.textContent.trim()}: ${reason}` : ""; }
  };
  // Wraps an async handler: re-entry is ignored while it runs (a fast second click / Enter never becomes a second POST).
  U.guard = (btn, fn, busyText) => async function (ev) {
    if (ev && ev.type === "submit" && ev.preventDefault) ev.preventDefault();
    if (btn.dataset.busy === "1") return undefined;
    btn.dataset.busy = "1";
    const label = btn.textContent;
    btn.disabled = true;
    btn.setAttribute("aria-busy", "true");
    if (busyText) btn.textContent = busyText;
    try {
      return await fn(ev);
    } finally {
      btn.dataset.busy = "";
      btn.removeAttribute("aria-busy");
      btn.textContent = label;
      btn.disabled = btn.dataset.holdDisabled === "1";
    }
  };

  // ------------------------------------------------------------------ polling: one timer chain, no overlap, backoff, pause when hidden
  // fn() resolves false (or throws) on failure. State: CONNECTING -> LIVE -> STALE (no success for a while) -> DISCONNECTED (repeated failures).
  U.poller = function (fn, opts) {
    const o = Object.assign({ intervalMs: 2000, hiddenIntervalMs: null, staleAfterMs: null, maxBackoffMs: 30000, onState: null }, opts || {});
    const staleAfter = o.staleAfterMs || Math.max(3 * o.intervalMs, 10000);
    let timer = null, inflight = false, stopped = true, failures = 0, lastOk = 0, lastState = null, ticks = 0;
    const state = () => (failures >= 2 ? "DISCONNECTED" : !lastOk ? "CONNECTING" : (Date.now() - lastOk > staleAfter ? "STALE" : "LIVE"));
    const emit = () => { const s = state(); if (o.onState) o.onState(s, { lastOkAt: lastOk, failures }); lastState = s; };
    function schedule() {
      clearTimeout(timer);
      timer = null;
      if (stopped) return;
      if (document.hidden && o.hiddenIntervalMs === null) return;      // hidden tab: no polling; visibilitychange resumes it
      const base = document.hidden ? o.hiddenIntervalMs : o.intervalMs;
      timer = setTimeout(tick, failures ? Math.min(base * Math.pow(2, failures), o.maxBackoffMs) : base);
    }
    async function tick() {
      if (inflight || stopped) return;
      inflight = true;
      ticks += 1;
      try {
        const ok = await fn();
        if (ok === false) failures += 1; else { failures = 0; lastOk = Date.now(); }
      } catch (e) {
        failures += 1;
      } finally {
        inflight = false;
        emit();
        schedule();
      }
    }
    function onVisibility() { if (!document.hidden && !stopped) { clearTimeout(timer); tick(); } }
    return {
      start() { if (!stopped) return; stopped = false; document.addEventListener("visibilitychange", onVisibility); tick(); },
      stop() { stopped = true; clearTimeout(timer); timer = null; document.removeEventListener("visibilitychange", onVisibility); },
      now() { clearTimeout(timer); return tick(); },
      state, get lastOkAt() { return lastOk; }, get ticks() { return ticks; }, get running() { return !stopped; },
    };
  };

  // ------------------------------------------------------------------ shared page chrome (header, health strip, footer)
  const NAV = [
    { key: "OBSERVE", href: "/observe.html" }, { key: "ALIGN", href: "/align.html" }, { key: "CALIBRATE", href: "/calibrate.html" },
    { key: "STATUS", href: "/status.html" },
  ];
  function consoleBase() { return `${location.protocol}//${location.hostname}:8088`; }
  U.consoleBase = consoleBase;

  U.mountHeader = function (section, subtitle) {
    const header = document.getElementById("almita-header");
    if (!header) return null;
    header.classList.add("topbar");
    header.textContent = "";
    const brand = document.createElement("div");
    const h1 = document.createElement("h1");
    h1.textContent = "ALMITA " + section;
    const tag = document.createElement("span");
    tag.className = "readonly";
    tag.textContent = subtitle || "";
    brand.append(h1, tag);
    const nav = document.createElement("nav");
    nav.className = "obs-nav";
    nav.setAttribute("aria-label", "ALMITA sections");
    const items = NAV.concat([{ key: "MONITOR", href: consoleBase() + "/" }, { key: "LAST SESSION", href: consoleBase() + "/#last-session" }]);
    for (const it of items) {
      const a = document.createElement("a");
      a.className = "obs-nav-link" + (it.key === section ? " active" : "");
      a.href = it.href;
      a.textContent = it.key;
      if (it.key === section) a.setAttribute("aria-current", "page");
      nav.appendChild(a);
    }
    const strip = document.createElement("div");
    strip.className = "hdr-status";
    strip.id = "hdr-status";
    strip.setAttribute("aria-live", "polite");
    header.append(brand, nav, strip);
    return strip;
  };

  function chip(label, value, klass) {
    const s = document.createElement("span");
    s.className = "chip";
    const l = document.createElement("span");
    l.className = "chip-label";
    l.textContent = label;
    const v = document.createElement("span");
    v.className = "chip-value " + klass;
    v.textContent = value;
    s.append(l, v);
    return s;
  }

  U.chip = chip;

  // Compact instrument health in every 8090 page header: OBSERVATION / MAIN SDR / INDI + LIVE|STALE|DISCONNECTED of this very link.
  U.mountHealthStrip = function (strip, opts) {
    if (!strip) return null;
    const o = Object.assign({ intervalMs: 5000 }, opts || {});
    let last = null;
    const render = (linkState, meta) => {
      strip.textContent = "";
      const h = last && last.data;
      if (h) {
        strip.appendChild(chip("OBSERVATION", h.workflows.observation.state, U.stateClass(h.workflows.observation.state)));
        strip.appendChild(chip("MAIN SDR", h.dependencies.main_sdr.state, U.stateClass(h.dependencies.main_sdr.state)));
        strip.appendChild(chip("INDI", h.dependencies.indi.state, U.stateClass(h.dependencies.indi.state)));
      }
      const age = meta && meta.lastOkAt ? " " + U.utcTime(new Date(meta.lastOkAt).toISOString()) : "";
      strip.appendChild(chip("LINK", linkState + age, U.stateClass(linkState)));
    };
    const p = U.poller(async () => {
      const r = await U.api("/api/system/health", { timeoutMs: 8000 });
      if (!r.ok) return false;
      last = r.data;
      return true;
    }, { intervalMs: o.intervalMs, onState: render });
    // STALE can start without any poll finishing: re-evaluate every second
    const ticker = setInterval(() => render(p.state(), { lastOkAt: p.lastOkAt }), 1000);
    render("CONNECTING", { lastOkAt: 0 });
    p.start();
    return { poller: p, health: () => last, dispose() { p.stop(); clearInterval(ticker); } };
  };

  U.mountFooter = async function () {
    const footer = document.getElementById("almita-footer");
    if (!footer) return;
    footer.classList.add("site-footer");
    footer.textContent = "";
    const parts = ["ALMITA", "Felipe Fridman"];
    const a = document.createElement("a");
    a.href = "https://github.com/almita-radio/almita";
    a.textContent = "GitHub project";
    a.rel = "noopener";
    a.target = "_blank";
    const span = document.createElement("span");
    span.textContent = parts.join(" · ") + " · ";
    footer.append(span, a);
    const info = document.createElement("span");
    info.id = "footer-version";
    info.textContent = " · build …";
    footer.appendChild(info);
    const url = (document.body && document.body.dataset.versionUrl) || "/api/system/version";
    const r = await U.api(url, { timeoutMs: 5000 });
    const v = r.ok ? (r.data && r.data.data ? r.data.data : r.data) : null;
    info.textContent = v ? ` · build ${v.git_short_sha || "unknown"} · started ${U.utc(v.started_utc)} · ${v.transport || "HTTP LAN"}` : " · build unknown (version unavailable)";
  };

  window.AlmitaUI = U;
})();
