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
  // Shared "pattern min alt ... (margin ...)" phrase for an ALIGN sky area (HI candidate or the SOLAR area) -
  // the temporal check across the WHOLE run, not just the instant PLAN/REFRESH was clicked: which point, when,
  // and by how much. Used by both align.js and pipeline.js so the wording never drifts between the two pages.
  U.temporalMarginText = (a) => {
    if (!U.finite(a.pattern_min_altitude_deg)) return "";
    const sign = a.pattern_margin_deg >= 0 ? "+" : "";
    return ` · pattern min alt ${a.pattern_min_altitude_deg.toFixed(2)}° at point ${a.pattern_min_altitude_point_index} ` +
      `(${U.utcTime(a.pattern_min_altitude_utc)}) — margin ${sign}${a.pattern_margin_deg.toFixed(2)}° vs MIN ELEVATION` +
      (U.finite(a.run_duration_s) ? ` over a ${Math.round(a.run_duration_s / 60)} min run` : "");
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
    { key: "PIPELINE", href: "/pipeline.html" }, { key: "OBSERVE", href: "/observe.html" }, { key: "ALIGN", href: "/align.html" }, { key: "CALIBRATE", href: "/calibrate.html" },
    { key: "REDUCE", href: "/reduce.html" }, { key: "SCIENCE", href: "/science.html" }, { key: "STATUS", href: "/status.html" },
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

  // ------------------------------------------------------------------ REAL operations (shared by PIPELINE, ALIGN and CALIBRATE)
  // Every job is a real ALMITA command started by /api/ops/*; the backend decides PASS / PARTIAL / FAIL from its exit code and result files.
  function mk(tag, cls, text) { const e = document.createElement(tag); if (cls) e.className = cls; if (text !== undefined) e.textContent = text; return e; }
  U.mountReadout = function (kv, badge, intervalMs) {
    const p = U.poller(async () => {
      const r = await U.api("/api/ops/mount", { timeoutMs: 12000 });
      if (!r.ok) { U.setBadge(badge, "UNREADABLE"); return false; }
      const m = r.data.data;
      kv.textContent = "";
      const add = (k, v) => { kv.append(mk("dt", "", k), mk("dd", "", v)); };
      if (m.error) { U.setBadge(badge, "ERROR"); add("MOUNT", m.error); return true; }
      const idle = (m.eod_state === "Idle" || m.eod_state === "Ok") && !m.parked && (m.mount_state === "Idle" || m.mount_state === null);
      U.setBadge(badge, idle ? "IDLE" : String(m.mount_state || m.eod_state).toUpperCase());
      add("RA", U.raHours(m.ra_h)); add("DEC", U.decDeg(m.dec_deg)); add("TRACKING", String(m.tracking).toUpperCase());
      add("PARK", m.parked ? "PARKED" : "UNPARKED"); add("PIER", (m.pier || []).join(", ") || "—"); add("ONSTEP ERROR", String(m.onstep_error)); add("READ", U.utc(m.read_utc));
      return true;
    }, { intervalMs: intervalMs || 5000 });
    p.start();
    return p;
  };
  U.jobPanel = function (container, badge, opts) {
    const o = Object.assign({ onEnd: null, onChange: null, onError: null }, opts || {});
    const st = { jobId: null, poller: null, last: null };
    const fail = (err) => { if (o.onError) o.onError(err); };
    const NEAR_BOTTOM_PX = 24; // slack (~1-2 lines) so "at the bottom" survives sub-pixel rounding
    function render(j) {
      st.last = j;
      // The whole panel is rebuilt from scratch on every poll (state/verdict/exit/artifacts can all change), which
      // used to also recreate the log <pre> as a brand-new node every time - a fresh node always starts at
      // scrollTop 0, so the log silently jumped to the top on every poll regardless of where the operator had
      // scrolled to. Capture the outgoing log element's scroll state (as plain values, before it's discarded)
      // so the new one can be given equivalent behavior: follow the tail while the operator is near the bottom,
      // hold still if nothing changed, and never drag them away from wherever they scrolled up to read.
      const oldLogPre = container.querySelector(".job-log");
      const sameJobLog = !!oldLogPre && oldLogPre.dataset.jobId === j.job_id;
      const newLogText = j.log_tail || "";
      const logTextChanged = !sameJobLog || oldLogPre.textContent !== newLogText;
      const logWasNearBottom = sameJobLog && oldLogPre.scrollTop + oldLogPre.clientHeight >= oldLogPre.scrollHeight - NEAR_BOTTOM_PX;
      const logOldScrollTop = sameJobLog ? oldLogPre.scrollTop : 0;
      container.textContent = "";
      if (badge) U.setBadge(badge, j.state === "RUNNING" ? "RUNNING" : j.verdict);
      const head = mk("div", "obs-summary");
      head.append(mk("div", "", `job ${j.job_id} · ${j.state} · ${j.verdict} · exit ${j.exit_code === null ? "—" : j.exit_code} · ${U.hms(j.elapsed_s)}`));
      if (j.detail) head.append(mk("div", "", j.detail));
      if (j.progress) head.append(mk("div", "", `real samples written so far: ${j.progress.samples_written}`));
      const f = j.facts || {};
      for (const k of Object.keys(f)) {
        const v = f[k];
        if (v === null || v === undefined || v === "") continue;
        head.append(mk("div", "muted", `${k}: ${typeof v === "object" ? JSON.stringify(v) : v}`));
      }
      container.appendChild(head);
      if (j.state === "RUNNING") {
        const b = mk("button", "", "STOP (SIGINT)");
        b.type = "button";
        b.addEventListener("click", U.guard(b, async () => {
          if (!confirm("Send a clean stop (SIGINT) to this job? A physical step may finish its current move first.")) return;
          const r = await U.api(`/api/ops/stop/${j.job_id}`, { method: "POST", body: { confirm: true }, timeoutMs: 20000 });
          fail(r.ok ? null : r.error);
        }));
        container.appendChild(b);
      }
      const logPre = mk("pre", "job-log", newLogText);
      logPre.dataset.jobId = j.job_id;
      container.appendChild(logPre);
      if (!logTextChanged) {
        logPre.scrollTop = logOldScrollTop;              // content identical: never move the scroll
      } else if (!sameJobLog || logWasNearBottom) {
        logPre.scrollTop = logPre.scrollHeight;           // new job, or operator was following the tail: keep following
      } else {
        logPre.scrollTop = logOldScrollTop;               // operator scrolled up to read history: leave them exactly there
      }
      if (j.artifacts && j.artifacts.length) {
        const list = mk("div", "job-artifacts"), imgs = mk("div", "thumbs");
        list.append(mk("div", "muted", `artifacts in ${j.output_dir} (${j.artifacts.length})`));
        for (const a of j.artifacts) {
          const label = `${a.path.split("/").slice(-2).join("/")} · ${U.bytes(a.size)}`;
          if (!a.viewable) { list.append(mk("span", "muted", label + "  ")); continue; }
          const link = mk("a", "", label);
          link.href = "/api/ops/file?path=" + encodeURIComponent(a.path); link.target = "_blank"; link.rel = "noopener";
          list.append(link, document.createTextNode("  "));
          if (a.path.toLowerCase().endsWith(".png") && imgs.children.length < 8) {
            const im = mk("img"); im.src = link.href; im.alt = a.path; im.loading = "lazy"; im.style.maxWidth = "320px";
            const wrap = mk("a"); wrap.href = link.href; wrap.target = "_blank"; wrap.rel = "noopener"; wrap.appendChild(im); imgs.appendChild(wrap);
          }
        }
        container.append(list, imgs);
      }
      if (o.onChange) o.onChange(j);
    }
    st.attach = function (jobId) {
      if (st.poller) st.poller.stop();
      st.jobId = jobId;
      st.poller = U.poller(async () => {
        const r = await U.api(`/api/ops/job/${jobId}?tail=200`, { timeoutMs: 15000 });
        if (!r.ok) return false;
        render(r.data.data);
        if (r.data.data.state !== "RUNNING") { st.poller.stop(); if (o.onEnd) o.onEnd(r.data.data); }
        return true;
      }, { intervalMs: 2000 });
      st.poller.start();
    };
    st.start = async function (stage, params, confirmText) {
      const r = await U.api(`/api/ops/start/${stage}`, { method: "POST", body: { params, confirm: confirmText === undefined ? null : confirmText }, timeoutMs: 60000 });
      if (!r.ok) { fail(r.error); return false; }
      fail(null);
      st.attach(r.data.data.job_id);
      return true;
    };
    st.recover = async function (stage) {           // after a reload: the backend, not this page, remembers the latest job of this stage
      const r = await U.api("/api/ops/jobs", { timeoutMs: 15000 });
      if (!r.ok) return;
      const row = (r.data.data || []).find((x) => x.stage === stage);
      if (row) st.attach(row.job_id);
    };
    return st;
  };

  // ------------------------------------------------------------------ ALIGN ring pattern (shared by align.html and pipeline.html)
  U.ringConfig = function (radiiEl, pointsEl) {
    const radii = radiiEl.value.split(",").map((s) => s.trim()).filter((s) => s !== "").map(Number);
    const points = Number(pointsEl.value);
    const valid = radii.length >= 1 && radii.length <= 8 && radii.every((r) => Number.isFinite(r) && r > 0 && r <= 45) &&
      new Set(radii.map((r) => r.toFixed(9))).size === radii.length && Number.isInteger(points) && points >= 3 && points <= 64;
    return { radii, points, valid };
  };
  U.renderRingPatternTotal = function (rc, totalEl) {
    if (!rc.valid) { totalEl.textContent = "pattern: invalid ring radii / points (each radius 0-45° and unique, 1-8 rings, 3-64 points/ring)"; return; }
    totalEl.textContent = `pattern: center + ${rc.radii.length} ring(s) x ${rc.points} = ${1 + rc.radii.length * rc.points} positions (radii ${rc.radii.join(", ")}°)`;
  };
  // Exact-match check between a PLAN job's recorded request params (job.params, as the server stored it verbatim)
  // and the params the web would submit right now - used to detect an OBSOLETE PLAN (parameters or the selected
  // A/B/C centre changed since PLAN ran) so a stale preview/table is never presented as if it were current.
  U.paramsEqual = function (a, b) {
    if (!a || !b) return false;
    const norm = (o) => JSON.stringify({
      reference: o.reference, ring_radii: (o.ring_radii || []).map(Number), ring_points: Number(o.ring_points),
      capture_time: Number(o.capture_time), beam_fwhm: Number(o.beam_fwhm), min_elevation: Number(o.min_elevation),
      center_ra_hours: o.center_ra_hours === undefined ? null : Number(o.center_ra_hours),
      center_dec_deg: o.center_dec_deg === undefined ? null : Number(o.center_dec_deg),
    });
    return norm(a) === norm(b);
  };
  U.renderAlignPreview = function (facts, wrapEl, summaryEl, tableSelector, identity, temporalEl) {
    const tbody = document.querySelector(tableSelector + " tbody");
    if (!facts || !facts.planned_positions || !facts.pattern_config) { wrapEl.hidden = true; if (temporalEl) temporalEl.hidden = true; return; }
    wrapEl.hidden = false;
    const pc = facts.pattern_config;
    const idParts = [];
    if (identity) {
      if (identity.computedUtc) idParts.push(`PLAN computed ${U.utc(identity.computedUtc)}`);
      if (identity.areaLabel) idParts.push(`candidate ${identity.areaLabel}`);
      if (facts.center) idParts.push(`centre RA ${Number(facts.center.ra_hours).toFixed(4)} h / Dec ${Number(facts.center.dec_deg).toFixed(3)}°`);
    }
    summaryEl.textContent = (idParts.length ? idParts.join(" · ") + " · " : "") +
      `${pc.total_positions} positions (center + ${pc.ring_radii_deg.length} ring(s) x ${pc.ring_points_per_ring}, radii ${pc.ring_radii_deg.join(", ")}°) · ` +
      `max separation from center: ${U.finite(facts.max_separation_from_center_deg) ? facts.max_separation_from_center_deg.toFixed(4) : "?"}°`;
    // Temporal check across the WHOLE run (alignment.pattern_temporal_altitudes(), the SAME function RUN itself
    // re-runs before its first movement) - which point falls lowest, when, and by how much, not just an instant.
    if (temporalEl) {
      const tc = facts.temporal_check;
      if (tc && U.finite(tc.min_altitude_deg)) {
        temporalEl.hidden = false;
        temporalEl.textContent = (tc.ok === false ? "⚠ TEMPORAL CHECK FAILED — " : "✓ temporal check OK — ") +
          `minimum altitude over the ${Math.round((tc.run_duration_s || 0) / 60)} min run: ${tc.min_altitude_deg.toFixed(2)}° ` +
          `at point ${tc.min_altitude_point_index} (${U.utc(tc.min_altitude_utc)}) — margin ${tc.margin_deg >= 0 ? "+" : ""}${tc.margin_deg.toFixed(2)}° ` +
          `vs MIN ELEVATION ${tc.min_elevation_deg}°`;
        temporalEl.style.color = tc.ok === false ? "var(--warn,#e6b85c)" : "";
      } else {
        temporalEl.hidden = true;
      }
    }
    tbody.textContent = "";
    let ring = 0, inRing = 0;
    for (const row of facts.planned_positions) {
      if (row.index > 0) { inRing++; if (inRing > pc.ring_points_per_ring) { inRing = 1; ring++; } }
      const tr = document.createElement("tr");
      const cells = [row.index, row.index === 0 ? "center" : `ring ${ring + 1} (r=${pc.ring_radii_deg[ring]}°)`,
        U.raHours(row.ra_hours, 6), U.decDeg(row.dec_deg, 5), row.separation_from_center_deg.toFixed(4) + "°",
        U.finite(row.elapsed_offset_s) ? U.utcTime(row.capture_start_utc) + ` (t+${Math.round(row.elapsed_offset_s)}s)` : "—",
        U.finite(row.alt_min_deg) ? row.alt_min_deg.toFixed(2) + "°" : "—"];
      for (const c of cells) { const td = document.createElement("td"); td.textContent = c; tr.appendChild(td); }
      tbody.appendChild(tr);
    }
  };

  // ------------------------------------------------------------------ ALIGN Alt/Az sky view (zenith at centre, horizon at the edge)
  // Draws: altitude rings (30/60 deg) + horizon, N/E/S/O, an optional faint HI intensity overlay (a real but SYNTHETIC model, never
  // Almita data), and each area as an OUTLINE circle only (never individual capture points, per the design brief). The operator judges
  // physical obstructions (hills, buildings) visually - this view carries no obstacle model.
  // Perceptually ordered dark-blue -> teal -> yellow -> red ramp (t in [0,1]), used only for the HI4PI overlay.
  U.hi4piColormap = function (t) {
    t = Math.max(0, Math.min(1, t));
    const stops = [[6, 12, 40], [20, 80, 130], [40, 170, 150], [210, 210, 70], [230, 90, 60]];
    const seg = Math.min(stops.length - 2, Math.floor(t * (stops.length - 1)));
    const localT = t * (stops.length - 1) - seg;
    const a = stops[seg], b = stops[seg + 1];
    return [Math.round(a[0] + (b[0] - a[0]) * localT), Math.round(a[1] + (b[1] - a[1]) * localT), Math.round(a[2] + (b[2] - a[2]) * localT)];
  };

  U.drawSkyView = function (canvas, data, opts) {
    const o = Object.assign({ selected: null, onPick: null }, opts || {});
    const ctx = canvas.getContext("2d");
    const cssW = canvas.clientWidth || 320, cssH = canvas.clientHeight || 320;
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) { canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(cssH * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    const w = cssW, h = cssH, cx = w / 2, cy = h / 2, R = Math.min(w, h) / 2 - 22;
    // this app is fixed dark (console/styles.css sets color-scheme:dark with no light variant): match its own palette, not the OS theme.
    const fg = "#e6edf1", muted = "rgba(145,162,173,0.75)", ringBg = "#121a20";
    ctx.clearRect(0, 0, w, h);
    ctx.fillStyle = ringBg; ctx.beginPath(); ctx.arc(cx, cy, R + 14, 0, 2 * Math.PI); ctx.fill();
    const rFor = (altDeg) => R * (1 - Math.max(0, Math.min(90, altDeg)) / 90);
    const toXY = (azDeg, altDeg) => { const r = rFor(altDeg), t = (azDeg * Math.PI) / 180; return [cx + r * Math.sin(t), cy - r * Math.cos(t)]; };

    // Continuous HI4PI overlay (real map, beam-smoothed server-side): a small raster upscaled with smoothing for
    // a continuous, faint layer instead of discrete dots. Grid row 0 = North (top), col 0 = West (left) - the
    // server samples it in exactly this canvas orientation, so it blits directly onto the disk with no flipping.
    if (data.hi4pi_grid && data.hi4pi_grid.values_1e20cm2 && data.hi4pi_grid.values_1e20cm2.length) {
      const g = data.hi4pi_grid, n = g.n, vals = g.values_1e20cm2;
      const [vlo, vhi] = g.value_range_1e20cm2 && g.value_range_1e20cm2[1] > g.value_range_1e20cm2[0] ? g.value_range_1e20cm2 : [0, 1];
      let off = canvas.__hiOff;
      if (!off || off.width !== n) { off = document.createElement("canvas"); off.width = n; off.height = n; canvas.__hiOff = off; }
      const octx = off.getContext("2d");
      const img = octx.createImageData(n, n);
      for (let i = 0; i < n * n; i++) {
        const v = vals[i], px4 = i * 4;
        if (v === null || v === undefined) { img.data[px4 + 3] = 0; continue; }
        const t = Math.max(0, Math.min(1, (v - vlo) / Math.max(1e-9, vhi - vlo)));
        const [rr, gg, bb] = U.hi4piColormap(t);
        img.data[px4] = rr; img.data[px4 + 1] = gg; img.data[px4 + 2] = bb; img.data[px4 + 3] = 235;   // faint overall via ctx.globalAlpha below
      }
      octx.putImageData(img, 0, 0);
      ctx.save(); ctx.globalAlpha = 0.55; ctx.imageSmoothingEnabled = true; ctx.imageSmoothingQuality = "high";
      ctx.beginPath(); ctx.arc(cx, cy, R, 0, 2 * Math.PI); ctx.clip();
      ctx.drawImage(off, cx - R, cy - R, 2 * R, 2 * R);
      ctx.restore();
    }

    // altitude rings + horizon (drawn OVER the overlay so they stay legible)
    ctx.strokeStyle = muted; ctx.lineWidth = 1; ctx.font = "9px ui-monospace,monospace";
    for (const alt of [0, 30, 60]) {
      ctx.beginPath(); ctx.arc(cx, cy, rFor(alt), 0, 2 * Math.PI); ctx.stroke();
      const ty = cy - rFor(alt) - 2;
      ctx.fillStyle = ringBg; ctx.fillText(alt + "°", cx + 3, ty);
      ctx.fillStyle = muted; ctx.fillText(alt + "°", cx + 3, ty);
    }
    ctx.fillStyle = fg; ctx.beginPath(); ctx.arc(cx, cy, 2, 0, 2 * Math.PI); ctx.fill();          // zenith
    ctx.font = "bold 12px ui-monospace,monospace"; ctx.textAlign = "center"; ctx.textBaseline = "middle";
    for (const [label, az] of [["N", 0], ["E", 90], ["S", 180], ["O", 270]]) {
      const [x, y] = toXY(az, -8);
      ctx.lineWidth = 3; ctx.strokeStyle = ringBg; ctx.strokeText(label, x, y);                    // halo so the label reads over any colour
      ctx.fillStyle = fg; ctx.fillText(label, x, y);
    }
    // Sun (context in both modes, small marker)
    if (data.sun) {
      const [x, y] = toXY(data.sun.az_deg, data.sun.alt_deg);
      ctx.fillStyle = data.sun.above_horizon ? "#e6b85c" : "rgba(230,184,92,0.35)";
      ctx.beginPath(); ctx.arc(x, y, 5, 0, 2 * Math.PI); ctx.fill();
      ctx.strokeStyle = ringBg; ctx.lineWidth = 1; ctx.stroke();
    }
    // areas: outline circles only, clickable when onPick is set (HI A/B/C)
    canvas.__hitAreas = [];
    const colors = { A: "#5cc8e6", B: "#9b8cf2", C: "#5ec98d", SUN: "#e6b85c" };
    for (const area of data.areas || []) {
      const [x, y] = toXY(area.az_deg, area.alt_deg);
      const edge = toXY(area.az_deg, Math.max(-90, area.alt_deg - area.radius_deg));           // toward the horizon = a real angular radius on this projection
      const rPix = Math.max(4, Math.hypot(edge[0] - x, edge[1] - y));
      const isSel = o.selected === area.label;
      ctx.strokeStyle = ringBg; ctx.lineWidth = isSel ? 5 : 3.5; ctx.setLineDash(area.alt_deg < 0 ? [4, 3] : []);
      ctx.beginPath(); ctx.arc(x, y, rPix, 0, 2 * Math.PI); ctx.stroke();                          // dark halo first so the circle reads over any colour
      ctx.strokeStyle = colors[area.label] || fg; ctx.lineWidth = isSel ? 3 : 1.5;
      ctx.beginPath(); ctx.arc(x, y, rPix, 0, 2 * Math.PI); ctx.stroke(); ctx.setLineDash([]);
      if (isSel) { ctx.fillStyle = (colors[area.label] || fg) + "22"; ctx.beginPath(); ctx.arc(x, y, rPix, 0, 2 * Math.PI); ctx.fill(); }
      const ly = y - rPix - 8 < -h / 2 ? y + rPix + 12 : y - rPix - 8;
      ctx.font = "bold 11px ui-monospace,monospace"; ctx.lineWidth = 3; ctx.strokeStyle = ringBg; ctx.strokeText(area.label, x, ly);
      ctx.fillStyle = colors[area.label] || fg; ctx.fillText(area.label, x, ly);
      canvas.__hitAreas.push({ x, y, r: rPix, label: area.label, alt_deg: area.alt_deg });
    }
    if (!canvas.__skyClickBound) {
      canvas.__skyClickBound = true;
      canvas.addEventListener("click", (ev) => {
        if (!canvas.__onPick) return;
        const rect = canvas.getBoundingClientRect();
        const mx = ev.clientX - rect.left, my = ev.clientY - rect.top;
        for (const a of canvas.__hitAreas || []) { if (Math.hypot(mx - a.x, my - a.y) <= Math.max(a.r, 14)) { canvas.__onPick(a.label); return; } }
      });
    }
    canvas.__onPick = o.onPick || null;
  };

  // Horizontal colour-scale legend for the HI4PI overlay: real cm^-2 values at both ends, drawn on its own canvas.
  U.drawHi4piLegend = function (canvas, valueRange, unit) {
    const ctx = canvas.getContext("2d");
    const cssW = canvas.clientWidth || 300, cssH = canvas.clientHeight || 34;
    const dpr = window.devicePixelRatio || 1;
    if (canvas.width !== Math.round(cssW * dpr) || canvas.height !== Math.round(cssH * dpr)) { canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(cssH * dpr); }
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, cssW, cssH);
    if (!valueRange) { return; }
    const barH = 10, barY = 4, barX = 2, barW = cssW - 4;
    for (let x = 0; x < barW; x++) {
      const [r, g, b] = U.hi4piColormap(x / (barW - 1));
      ctx.fillStyle = `rgb(${r},${g},${b})`;
      ctx.fillRect(barX + x, barY, 1, barH);
    }
    ctx.strokeStyle = "rgba(145,162,173,0.6)"; ctx.strokeRect(barX, barY, barW, barH);
    ctx.font = "10px ui-monospace,monospace"; ctx.fillStyle = "#e6edf1"; ctx.textBaseline = "top";
    const fmt = (v) => (v * 1e20).toExponential(1).replace("e+", "e");
    ctx.textAlign = "left"; ctx.fillText(fmt(valueRange[0]), barX, barY + barH + 3);
    ctx.textAlign = "right"; ctx.fillText(fmt(valueRange[1]), barX + barW, barY + barH + 3);
    ctx.textAlign = "center"; ctx.fillText(unit || "N_HI (cm⁻²)", barX + barW / 2, barY + barH + 3);
  };

  window.AlmitaUI = U;
})();
