// ALMITA STATUS: one compact, read-only view of services, dependencies, workflows and operational readiness.
// Service UP is not operational READY: the dependencies decide (the backend computes it; this page only shows it).
(function () {
  "use strict";
  const U = window.AlmitaUI;
  const $ = (id) => document.getElementById(id);
  const strip = U.mountHeader("STATUS", "SYSTEM HEALTH");
  U.mountFooter();

  function fill(tbodyId, rows) {
    const tb = $(tbodyId);
    tb.textContent = "";
    if (!rows.length) {
      const tr = tb.insertRow();
      const td = tr.insertCell();
      td.colSpan = 3;
      td.textContent = "No data yet";
      return;
    }
    for (const [name, item] of rows) {
      const tr = tb.insertRow();
      tr.insertCell().textContent = name.replace(/_/g, " ").toUpperCase();
      const st = tr.insertCell();
      const badge = document.createElement("span");
      badge.className = "badge " + U.stateClass(item.state);
      badge.textContent = item.state;
      st.appendChild(badge);
      tr.insertCell().textContent = item.detail || "—";
    }
  }

  function render(h) {
    const op = h.operational || {};
    U.setBadge($("op-badge"), op.level || "UNKNOWN");
    const lines = op.reasons && op.reasons.length ? op.reasons.map((r) => "• " + r) : ["• all required services and dependencies are up"];
    if (op.degraded_by && op.degraded_by.length) lines.push("", "Degraded by: " + op.degraded_by.join(", "));
    lines.push("", op.note || "");
    $("op-reasons").textContent = lines.join("\n");
    fill("tbl-services", Object.entries(h.services || {}));
    fill("tbl-deps", Object.entries(h.dependencies || {}));
    fill("tbl-workflows", Object.entries(h.workflows || {}));
    $("updated").textContent = "generated " + U.utc(h.generated_utc);
    const v = h.version || {};
    const kv = $("version-kv");
    kv.textContent = "";
    for (const [k, val] of [["BUILD (git)", v.git_short_sha], ["STARTED", U.utc(v.started_utc)], ["HOSTNAME", v.hostname], ["TRANSPORT", v.transport], ["PROJECT", v.project_url]]) {
      const dt = document.createElement("dt"), dd = document.createElement("dd");
      dt.textContent = k;
      dd.textContent = val || "—";
      kv.append(dt, dd);
    }
  }

  const poller = U.poller(async () => {
    const r = await U.api("/api/system/health", { timeoutMs: 8000 });
    if (!r.ok) { U.showError($("error-banner"), r.error); return false; }
    U.showError($("error-banner"), "");
    render(r.data.data);
    return true;
  }, { intervalMs: 5000, onState: (state) => {
    strip.textContent = "";
    strip.appendChild(U.chip("LINK", state, U.stateClass(state)));
  } });
  $("btn-refresh").addEventListener("click", U.guard($("btn-refresh"), () => poller.now(), "REFRESHING…"));
  poller.start();
})();
