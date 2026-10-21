// ALMITA SESSIONS (BL-079..BL-090): catalog and viewer of stored observations.
// Read-only over the data: every request here is a GET of stored files, except SAVE (annotations), REBUILD INDEX and
// EXPORT REPORT, which the server writes only under data/sessions/. Opening a session never starts REDUCE, SCIENCE,
// QUICKLOOK or anything physical, and never touches what MONITOR shows (the 3D stack here is its own instance).
(function () {
  "use strict";
  const U = window.AlmitaUI;
  const $ = (id) => document.getElementById(id);
  const DASH = "—";
  const strip = U.mountHeader("SESSIONS", "STORED OBSERVATIONS");
  U.mountFooter();
  U.mountHealthStrip(strip);

  // ------------------------------------------------------------------ small DOM helpers (text only, never markup)
  function el(tag, attrs, ...children) {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs || {})) {
      if (v === null || v === undefined || v === false) continue;
      if (k === "class") e.className = v;
      else if (k === "text") e.textContent = v;
      else if (k === "on") for (const [ev, fn] of Object.entries(v)) e.addEventListener(ev, fn);
      else e.setAttribute(k, v === true ? "" : String(v));
    }
    for (const c of children) if (c !== null && c !== undefined) e.append(c instanceof Node ? c : String(c));
    return e;
  }
  const clear = (node) => { node.textContent = ""; return node; };
  const badge = (state) => el("span", { class: "badge " + U.stateClass(state), text: state || "UNKNOWN" });
  const fmt = (v) => (v === null || v === undefined || v === "" ? DASH : String(v));
  const fileUrl = (rel) => "/api/ops/file?path=" + encodeURIComponent(rel);
  const SERVED = /\.(png|json|csv|txt|log|md|svg|pdf)$/i;          // almita_web_ops.SERVE_SUFFIXES: what /api/ops/file hands out
  function emptyRow(tbody, cols, text) { tbody.append(el("tr", {}, el("td", { colspan: cols, class: "muted", text }))); }
  function kv(dl, pairs) {
    clear(dl);
    for (const [k, v] of pairs) dl.append(el("dt", { text: k }), v instanceof Node ? el("dd", {}, v) : el("dd", { text: fmt(v) }));
  }
  function card(label, value, state) {
    return el("div", { class: "card" }, el("dt", { text: label }), el("dd", { class: state ? U.stateClass(state) : null, text: fmt(value) }));
  }
  function showError(err) { U.showError($("error-banner"), err); }
  async function get(path, timeoutMs) {
    const r = await U.api(path, { timeoutMs: timeoutMs || 30000 });
    if (!r.ok) throw r.error;
    return r.data.data;
  }
  async function post(path, body, timeoutMs) {
    const r = await U.api(path, { method: "POST", body: body || {}, timeoutMs: timeoutMs || 60000 });
    if (!r.ok) throw r.error;
    return r.data.data;
  }
  const chileShort = (s) => (s ? s.replace(/:\d\d \(UTC/, " (UTC") : DASH);

  // ------------------------------------------------------------------ CATALOG
  const FILTERS = ["q", "state", "condition", "kind", "reduce", "science", "tag", "archived", "discarded", "sort", "order"];
  let page = 1, pages = 1, current = null, allSessions = [];
  // ------------------------------------------------------------------ SELECTION + DELETION (BL-093)
  // Ticking boxes never deletes anything: DELETE … opens a preview computed by the server, and only its red button
  // (labelled by the server, e.g. "DELETE 4 SESSIONS — IRREVERSIBLE") sends the batch, with the preview's token.
  const selected = new Map();           // campaign_id -> listed size (campaign + its runs), kept across pages and filters
  let pageIds = [], lastPreview = null;

  function filterQuery() {
    const p = new URLSearchParams();
    for (const f of FILTERS) { const v = $("f-" + f).value; if (v) p.set(f, v); }
    if ($("f-favorite").checked) p.set("favorite", "1");
    p.set("page", String(page)); p.set("per_page", "25");
    return p.toString();
  }

  function receiverText(r) {
    if (!r) return DASH;
    if (r.value) return r.value + (r.documented_by === "operator" ? " (operator)" : "");
    return "not documented";
  }

  function renderCatalog(data) {
    pages = data.pages; page = data.page;
    U.setBadge($("catalog-badge"), `${data.total} / ${data.all_sessions}`);
    $("catalog-badge").className = "badge";
    $("catalog-info").textContent = `index built ${U.utc(data.generated_utc)} in ${fmt(data.build_seconds)} s · ${data.pending_associations} pending association(s)` +
      (data.orphan_annotations.length ? ` · ${data.orphan_annotations.length} annotation(s) of sessions no longer on disk: ${data.orphan_annotations.join(", ")}` : "");
    $("size-method").textContent = "SIZE: " + data.size_method;
    $("pager").textContent = `page ${data.page} of ${data.pages}`;
    U.setEnabled($("btn-prev"), data.page > 1, "first page");
    U.setEnabled($("btn-next"), data.page < data.pages, "last page");
    const tagSel = $("f-tag"), keep = tagSel.value;
    clear(tagSel).append(el("option", { value: "", text: "any" }));
    for (const t of data.tags) tagSel.append(el("option", { value: t, text: t }));
    tagSel.value = data.tags.includes(keep) ? keep : "";
    const tb = clear($("tbl-sessions"));
    pageIds = data.sessions.map((s) => s.campaign_id);
    renderSelection();
    if (!data.sessions.length) { emptyRow(tb, 12, "No session matches these filters."); return; }
    for (const s of data.sessions) {
      const a = s.annotation || {};
      const marks = [a.favorite ? "★" : "", a.reference ? "REF" : "", a.archived ? "ARCHIVED" : "", a.discarded ? "DISCARDED" : ""].filter(Boolean).join(" ");
      const open = el("button", { type: "button", class: "mode-btn ses-open", text: "OPEN", "aria-label": "open " + s.campaign_id,
        on: { click: () => openSession(s.campaign_id) } });
      const size = s.size || {}, campBytes = (size.campaign || {}).bytes_on_disk || 0, prodBytes = (size.products || {}).bytes_on_disk || 0;
      const box = el("input", { type: "checkbox", "aria-label": "select " + s.campaign_id });
      box.checked = selected.has(s.campaign_id);
      box.addEventListener("change", () => { if (box.checked) selected.set(s.campaign_id, campBytes + prodBytes); else selected.delete(s.campaign_id); renderSelection(); });
      const tr = el("tr", { class: current === s.campaign_id ? "ses-current" : null },
        el("td", {}, el("div", { class: "ses-selcell" }, box, open)),
        el("td", {}, el("div", { class: "ses-name", text: s.display_name || s.campaign_id }),
          el("div", { class: "muted ses-id", text: s.campaign_id + (marks ? " · " + marks : "") })),
        el("td", { text: chileShort(s.started_chile) }),
        el("td", {}, badge((s.capture || {}).state)),
        el("td", { text: `${fmt(s.points_captured)} / ${fmt(s.points_planned)}` }),
        el("td", {}, badge((s.calibration || {}).state)),
        el("td", {}, badge((s.reduce || {}).state), el("span", { class: "muted", text: (s.reduce || {}).runs && s.reduce.runs.length ? ` ×${s.reduce.runs.length}` : "" })),
        el("td", {}, badge((s.science || {}).state), el("span", { class: "muted", text: (s.science || {}).runs && s.science.runs.length ? ` ×${s.science.runs.length}` : "" })),
        el("td", { text: receiverText(s.receiver) }),
        el("td", {}, badge((s.condition || {}).value)),
        el("td", { text: U.bytes(campBytes) + (prodBytes ? ` + ${U.bytes(prodBytes)} runs` : "") }),
        el("td", { text: (a.tags || []).join(", ") || DASH }));
      tb.append(tr);
    }
  }

  async function loadCatalog() {
    try {
      renderCatalog(await get("/api/sessions/list?" + filterQuery(), 60000));
      showError("");
    } catch (err) { showError(err); }
  }

  async function loadAllForCompare() {
    try {
      const d = await get("/api/sessions/list?archived=all&per_page=100&page=1&sort=started", 60000);
      allSessions = d.sessions;
      for (let p = 2; p <= d.pages; p++) allSessions = allSessions.concat((await get(`/api/sessions/list?archived=all&per_page=100&page=${p}&sort=started`, 60000)).sessions);
    } catch (err) { allSessions = []; }
  }

  let debounce = null;
  for (const f of FILTERS) $("f-" + f).addEventListener(f === "q" ? "input" : "change", () => {
    clearTimeout(debounce); debounce = setTimeout(() => { page = 1; loadCatalog(); }, f === "q" ? 300 : 0);
  });
  $("f-favorite").addEventListener("change", () => { page = 1; loadCatalog(); });
  $("btn-refresh").addEventListener("click", U.guard($("btn-refresh"), loadCatalog, "LOADING…"));
  $("btn-prev").addEventListener("click", () => { if (page > 1) { page--; loadCatalog(); } });
  $("btn-next").addEventListener("click", () => { if (page < pages) { page++; loadCatalog(); } });
  $("btn-reindex").addEventListener("click", U.guard($("btn-reindex"), async () => {
    try {
      const r = await post("/api/sessions/reindex", {}, 180000);
      $("catalog-info").textContent = `rebuilt: ${r.sessions} sessions in ${r.build_seconds} s`;
      await loadCatalog();
      if (current) await openSession(current, currentTab);
    } catch (err) { showError(err); }
  }, "REBUILDING…"));

  function renderSelection() {
    const n = selected.size, off = [...selected.keys()].filter((id) => !pageIds.includes(id)).length;
    const listed = [...selected.values()].reduce((a, b) => a + b, 0);
    $("sel-count").textContent = n ? `${n} selected${off ? ` · ${off} of them NOT on this page` : ""} · listed size ${U.bytes(listed)} ` +
      "(what can really be recovered is computed in the preview)" : "0 selected";
    const onPage = pageIds.filter((id) => selected.has(id)).length;
    $("sel-page").checked = pageIds.length > 0 && onPage === pageIds.length;
    $("sel-page").indeterminate = onPage > 0 && onPage < pageIds.length;
    U.setEnabled($("btn-del-products"), n > 0, "select sessions first");
    U.setEnabled($("btn-del-sessions"), n > 0, "select sessions first");
    U.setEnabled($("btn-sel-clear"), n > 0, "nothing selected");
  }
  $("sel-page").addEventListener("change", () => {
    for (const box of document.querySelectorAll('#tbl-sessions input[type="checkbox"]')) if (box.checked !== $("sel-page").checked) { box.checked = $("sel-page").checked; box.dispatchEvent(new Event("change")); }
    renderSelection();
  });
  $("btn-sel-all").addEventListener("click", U.guard($("btn-sel-all"), async () => {
    try {
      const q = new URLSearchParams(filterQuery()); q.set("ids", "1"); q.delete("page");
      const d = await get("/api/sessions/list?" + q.toString(), 60000);
      for (const s of d.selection) selected.set(s.campaign_id, s.bytes_on_disk);
      renderSelection();
      $("sel-count").textContent += ` · added all ${d.selection.length} filtered (${Math.max(0, d.selection.length - pageIds.length)} beyond this page)`;
    } catch (err) { showError(err); }
  }, "SELECTING…"));
  $("btn-sel-clear").addEventListener("click", () => {
    selected.clear();
    for (const box of document.querySelectorAll('#tbl-sessions input[type="checkbox"]')) box.checked = false;
    renderSelection();
  });

  function categories() { return [["cat-quicklook", "QUICKLOOK"], ["cat-reduce", "REDUCE"], ["cat-science", "SCIENCE"]].filter(([id]) => $(id).checked).map(([, c]) => c); }
  async function preview(mode) {
    const body = { ids: [...selected.keys()], mode, categories: mode === "products" ? categories() : null };
    if (mode === "products" && !body.categories.length) { showError("tick at least one product category"); return; }
    try { lastPreview = await post("/api/sessions/delete/preview", body, 120000); showError(""); }
    catch (err) { showError(err); return; }
    renderPreview(lastPreview);
  }
  $("btn-del-products").addEventListener("click", U.guard($("btn-del-products"), () => preview("products"), "PREVIEWING…"));
  $("btn-del-sessions").addEventListener("click", U.guard($("btn-del-sessions"), () => preview("sessions"), "PREVIEWING…"));

  function renderPreview(p) {
    $("delete-panel").hidden = false;
    clear($("delete-result"));
    U.setBadge($("delete-badge"), p.deletable ? "PREVIEW" : "NOTHING TO DELETE");
    const cards = clear($("delete-cards"));
    cards.append(card("MODE", p.mode === "sessions" ? "WHOLE SESSIONS" : "PRODUCTS: " + p.categories.join(" + ")),
      card("SESSIONS THAT WILL CHANGE", `${p.deletable} of ${p.ids.length}`), card("BLOCKED", p.blocked.length, p.blocked.length ? "status-warning" : null),
      card("FILES TO REMOVE", p.files), card("RECOVERABLE (ESTIMATE)", U.bytes(p.space.recoverable_bytes)),
      card("KEPT ALIVE BY HARD LINKS OUTSIDE", `${p.space.kept_alive_by_outside_hard_links} file(s), ${U.bytes(p.space.kept_alive_bytes)}`));
    $("delete-note").textContent = [p.busy.length ? "PIPELINE ACTIVE — every session is blocked: " + p.busy.join("; ") : "",
      p.mode === "products" ? "Captures, plans, logs, notes and manual analyses stay. A product goes only when it can be regenerated now (its profile loads and the captures are there); its parameters (config/provenance/manifest) are kept in the audit record." :
        "Each session's campaign directory and the REDUCE/SCIENCE runs that belong only to it go. Calibration profiles, manual analyses, ambiguous runs, exports and the temperature log stay.",
      "Space: " + p.space.estimate_note + ".", "Nothing goes to a trash: the space is freed for real."].filter(Boolean).join("\n");
    const tb = clear($("tbl-delete"));
    for (const r of p.sessions) {
      const goes = r.items.map((i) => `${i.kind} ${i.path} (${i.files} files, ${U.bytes(i.bytes_on_disk)})`).join("\n") || "—";
      const stays = [r.blocked ? "BLOCKED: " + r.blocked : "", ...r.kept.map((k) => `${k.kind} ${k.path}: ${k.why}`), ...r.warnings.map((w) => "WARNING: " + w)].filter(Boolean).join("\n") || "—";
      tb.append(el("tr", { class: r.blocked ? "row-warn" : null },
        el("td", {}, el("div", { class: "ses-name", text: r.name || r.campaign_id }), el("div", { class: "muted ses-id", text: r.campaign_id })),
        el("td", { text: chileShort(r.started_chile) }), el("td", {}, badge(r.capture_state)), el("td", { text: String(r.files) }),
        el("td", { text: U.bytes(r.space.recoverable_bytes) }), el("td", { class: "ses-why", text: goes }), el("td", { class: "ses-why", text: stays })));
    }
    const tk = clear($("tbl-delete-kept"));
    if (!p.kept_resources.length) emptyRow(tk, 2, "None.");
    for (const k of p.kept_resources) tk.append(el("tr", {}, el("td", { class: "ses-id", text: k.path }), el("td", { class: "ses-why", text: k.why })));
    const btn = $("btn-del-confirm");
    btn.textContent = p.confirm_label;
    U.setEnabled(btn, p.deletable > 0 && !p.busy.length, p.busy.length ? "the pipeline is active" : "nothing in this selection can be deleted");
    $("delete-panel").scrollIntoView({ block: "start" });
  }
  $("btn-del-cancel").addEventListener("click", () => { lastPreview = null; $("delete-panel").hidden = true; clear($("tbl-delete")); });
  $("btn-del-confirm").addEventListener("click", U.guard($("btn-del-confirm"), async () => {
    const p = lastPreview;
    if (!p) return;
    lastPreview = null;
    U.setEnabled($("btn-del-confirm"), false, "sent: preview again for another batch");
    const out = clear($("delete-result"));
    let r;
    try { r = await post("/api/sessions/delete/execute", { ids: p.ids, mode: p.mode, categories: p.mode === "products" ? p.categories : null, token: p.token, confirm: "DELETE" }, 600000); }
    catch (err) { out.append(el("div", { class: "obs-summary status-error", text: "NOT DELETED: " + U.errorText(err) })); return; }
    U.setBadge($("delete-badge"), r.sessions.some((s) => s.status === "PARTIAL") ? "PARTIAL" : "DONE");
    out.append(el("h3", { class: "ses-h3", text: `RESULT · batch ${r.batch_id} · audit ${r.audit_dir}` }),
      el("div", { class: "obs-summary", text: `Removed (estimate from the preview's blocks): ${U.bytes(r.bytes_removed_estimate)} · free space change observed on the card: ` +
        (r.free_bytes_change_observed === null ? "unknown" : U.bytes(r.free_bytes_change_observed)) + ` · catalog now: ${r.catalog_sessions_after} sessions` }));
    const body = el("tbody");
    for (const s of r.sessions) body.append(el("tr", { class: s.status === "PARTIAL" ? "row-warn" : null }, el("td", { text: s.campaign_id }), el("td", {}, badge(s.status)),
      el("td", { text: String(s.files_removed) }), el("td", { text: U.bytes(s.bytes_removed_estimate) }),
      el("td", { class: "ses-why", text: s.blocked || (s.error_count ? `${s.error_count} error(s), these stayed:\n` + s.errors.map((e) => `${e.path}: ${e.error}`).join("\n") : "—") })));
    out.append(el("div", { class: "table-wrap" }, el("table", { class: "status-table" }, el("thead", {}, el("tr", {}, el("th", { text: "SESSION" }), el("th", { text: "RESULT" }),
      el("th", { text: "FILES REMOVED" }), el("th", { text: "REMOVED (ESTIMATE)" }), el("th", { text: "WHAT STAYED AND WHY" }))), body)));
    for (const s of r.sessions) if (p.mode === "sessions" && s.status === "DELETED") selected.delete(s.campaign_id);
    if (current && p.mode === "sessions" && r.sessions.some((s) => s.campaign_id === current && s.status === "DELETED")) { current = null; detail = null; $("session-panel").hidden = true; history.replaceState(null, "", "#"); }
    else if (current) await openSession(current, currentTab);
    await loadCatalog();
    await loadHistory();
  }, "DELETING…"));

  async function loadHistory() {
    const tb = clear($("tbl-del-history"));
    let d;
    try { d = await get("/api/sessions/deletions"); } catch (err) { emptyRow(tb, 6, "history unavailable"); return; }
    if (!d.batches.length) { emptyRow(tb, 6, "No deletion yet."); return; }
    for (const b of d.batches) tb.append(el("tr", {}, el("td", { class: "ses-id", text: b.batch_id }), el("td", { text: U.utc(b.utc) }),
      el("td", { text: b.mode + (b.categories && b.categories.length ? " " + b.categories.join("+") : "") }),
      el("td", { class: "ses-why", text: b.sessions.map((s) => `${s.campaign_id} ${s.status}${s.error_count ? ` (${s.error_count} errors)` : ""}`).join("\n") }),
      el("td", { text: U.bytes(b.bytes_removed_estimate) }), el("td", { text: b.free_bytes_change_observed === null ? "unknown" : U.bytes(b.free_bytes_change_observed) })));
  }

  // ------------------------------------------------------------------ SESSION VIEWER
  const TABS = ["SUMMARY", "OBSERVATION", "CALIBRATION", "CAPTURES / QUICKLOOK", "REDUCE", "SCIENCE", "INSTRUMENT", "FILES", "ORGANIZE", "EXPORT", "COMPARE"];
  let currentTab = "SUMMARY", detail = null, points = [], loaded = {};
  const tabBar = $("session-tabs");
  for (const t of TABS) tabBar.append(el("button", { type: "button", class: "mode-btn", role: "tab", "data-tab": t, text: t, on: { click: () => selectTab(t) } }));

  function selectTab(t) {
    currentTab = t;
    for (const b of tabBar.querySelectorAll("button")) { const on = b.dataset.tab === t; b.classList.toggle("active", on); b.setAttribute("aria-selected", on ? "true" : "false"); }
    for (const s of document.querySelectorAll(".ses-section")) s.hidden = s.dataset.tab !== t;
    writeHash();
    if (detail) loadTab(t).catch(showError);
  }

  function writeHash() {
    if (!current) return;
    const h = "#id=" + encodeURIComponent(current) + "&tab=" + encodeURIComponent(currentTab);
    if (location.hash !== h) history.replaceState(null, "", h);
  }

  async function openSession(id, tab) {
    current = id; loaded = {}; points = [];
    $("session-panel").hidden = false;
    $("h-session").textContent = "SESSION " + id;
    U.setBadge($("session-badge"), "LOADING");
    try {
      detail = await get("/api/sessions/detail?id=" + encodeURIComponent(id), 60000);
      showError("");
    } catch (err) { showError(err); U.setBadge($("session-badge"), "ERROR"); return; }
    $("h-session").textContent = detail.display_name || id;
    U.setBadge($("session-badge"), (detail.capture || {}).state);
    const a = detail.annotation || {};
    $("session-ident").textContent = [
      `CAMPAIGN ${detail.campaign_id} · ${detail.dir}`,
      `capture session ${fmt((detail.capture_session_ids || []).join(", "))} · IQ ${fmt((detail.iq_session_dirs || []).join(", "))}`,
      a.discarded ? `DISCARDED FOR ANALYSIS: ${a.discarded.reason} (${U.utc(a.discarded.utc)})` : "",
      a.archived ? "ARCHIVED (catalog only)" : "",
    ].filter(Boolean).join("\n");
    for (const r of $("tbl-sessions").rows) r.classList.toggle("ses-current", r.textContent.includes(id));
    renderSummary();
    selectTab(tab && TABS.includes(tab) ? tab : currentTab);
    $("session-panel").scrollIntoView({ block: "start" });
  }

  async function loadTab(t) {
    if (t === "OBSERVATION") renderObservation();
    else if (t === "CALIBRATION") renderCalibration();
    else if (t === "CAPTURES / QUICKLOOK") await renderQuicklook();
    else if (t === "REDUCE") renderReduce();
    else if (t === "SCIENCE") renderScience();
    else if (t === "INSTRUMENT") await renderInstrument();
    else if (t === "FILES") await renderFiles("");
    else if (t === "ORGANIZE") renderOrganize();
    else if (t === "EXPORT") await renderExports();
    else if (t === "COMPARE") await renderCompareChoices();
  }

  // ---- SUMMARY
  function renderSummary() {
    const d = detail, s = d.sections.summary, cond = d.condition || {};
    const cards = clear($("summary-cards"));
    cards.append(card("CAPTURE", (d.capture || {}).state, (d.capture || {}).state),
      card("POINTS CAPTURED / PLANNED", `${fmt(d.points_captured)} / ${fmt(d.points_planned)}`),
      card("DURATION (first → last capture)", d.duration_s ? U.hms(d.duration_s) : DASH),
      card("STARTED (CHILE)", chileShort(d.started_chile)), card("STARTED (UTC)", U.utc(d.started_utc)),
      card("CONDITION", cond.value, cond.value), card("RECEIVER", receiverText(d.receiver)), card("MOUNT (log)", d.mount),
      card("OBJECTIVE", (s.objective || {}).name || d.observation_name));
    const tb = clear($("tbl-stages"));
    const st = s.stages;
    tb.append(el("tr", {}, el("td", { text: "CAPTURE" }), el("td", {}, badge(st.capture.state)), el("td", { text: `${fmt(st.capture.detail)} · evidence: ${(st.capture.evidence || []).join(", ")}` })));
    tb.append(el("tr", {}, el("td", { text: "CALIBRATION" }), el("td", {}, badge(st.calibration.state)), el("td", { text: fmt(st.calibration.profile_path) })));
    tb.append(el("tr", {}, el("td", { text: "REDUCE" }), el("td", {}, badge(st.reduce.state)), el("td", { text: (st.reduce.runs || []).join(", ") || "no REDUCE run of this campaign" })));
    tb.append(el("tr", {}, el("td", { text: "SCIENCE" }), el("td", {}, badge(st.science.state)), el("td", { text: (st.science.runs || []).join(", ") || "no SCIENCE run on its REDUCE runs" })));
    tb.append(el("tr", {}, el("td", { text: "CONDITION" }), el("td", {}, badge(cond.value)), el("td", { text: fmt(cond.source) })));
    $("summary-notes").textContent = s.notes || "No notes yet (ORGANIZE).";
    const ti = clear($("tbl-incidents"));
    if (!s.incidents.length) emptyRow(ti, 4, "No incident recorded.");
    for (const i of s.incidents) ti.append(el("tr", {}, el("td", { text: fmt(i.utc) }), el("td", { text: i.kind }), el("td", { text: i.source }), el("td", { text: i.detail })));
  }

  // ---- OBSERVATION
  function renderObservation() {
    const o = detail.sections.observation, req = o.requested || {}, eff = o.effective || {};
    const rq = req.main || {}, em = eff.main || {}, rc = req.capture || {}, rg = req.grid || {}, res = eff.resolved || {};
    const site = o.site || {};
    const rows = [
      ["CENTRE FREQUENCY", U.mhz(rq.center_frequency_hz), U.mhz(em.center_frequency_hz)],
      ["SAMPLE RATE", U.msps(rq.sample_rate), U.msps(em.sample_rate)],
      ["GAIN", U.num(rq.gain_db, 1, "dB"), U.num(em.gain_db, 1, "dB")],
      ["MAIN BIAS-T", fmt(rq.bias_tee), fmt(em.bias_tee)],
      ["INTEGRATION PER POINT", U.num(rc.seconds, 1, "s"), U.num(rc.seconds, 1, "s") + " (plan does not change it)"],
      ["SETTLE", U.num(rc.settle_seconds, 1, "s"), DASH],
      ["GRID", rg.rows ? `${rg.rows} × ${rg.cols}` : fmt(rg.mode), res.rows ? `${res.rows} × ${res.cols} = ${res.point_count} points` : DASH],
      ["SPACING", U.num(rg.spacing_deg, 3, "°"), U.num(res.spacing_deg, 3, "°")],
      ["ROTATION (from North)", U.num(rg.rotation_deg, 1, "°"), U.num(res.rotation_deg, 1, "°")],
      ["CENTRE RA / DEC", rg.center_ra_hours !== undefined ? `${U.raHours(rg.center_ra_hours)} / ${U.decDeg(rg.center_dec_deg)}` : fmt(rg.placement),
        `${U.raHours(res.center_ra_hours)} / ${U.decDeg(res.center_dec_deg)} (${fmt(res.frame)})`],
      ["TRAVERSAL", fmt(rg.traversal), fmt(res.traversal)],
      ["RFI REF", fmt((req.rfi_ref || {}).enabled), fmt((eff.rfi_ref || {}).enabled) + ((o.rfi_ref_dir || {}).state === "OK" ? " · rfi_ref/ present" : "")],
      ["SITE", DASH, (() => {
        const lat = site.latitude_deg !== undefined ? site.latitude_deg : site.latitude, lon = site.longitude_deg !== undefined ? site.longitude_deg : site.longitude;
        return lat !== undefined ? `${fmt(site.name)} · lat ${lat}° · lon ${lon}° · ${fmt(site.elevation_m)} m` : DASH;
      })()],
      ["MOUNT (orchestrator log)", DASH, fmt(o.mount)],
      ["VISIBILITY AT PLAN", DASH, fmt(eff.visibility)],
      ["CONFIG SHA-256", DASH, fmt(eff.observation_config_sha256)],
    ];
    const tb = clear($("tbl-observation"));
    if (o.plan_state !== "OK") emptyRow(tb, 3, `observation_resolved.json ${o.plan_state}: parameters not recorded`);
    for (const [k, a, b] of rows) tb.append(el("tr", {}, el("td", { text: k }), el("td", { text: a }), el("td", { text: b })));
    $("observation-note").textContent = o.note;
    $("observation-requested-raw").textContent = JSON.stringify(req, null, 1);
    $("observation-effective-raw").textContent = JSON.stringify(eff, null, 1);
  }

  // ---- CALIBRATION
  function renderCalibration() {
    const c = detail.sections.calibration, p = c.profile, m = c.profile_metadata || {}, comp = m.compatibility || {};
    const yes = (v) => (v === true ? "MATCH" : v === false ? "DIFFERENT" : "UNKNOWN");
    kv($("calibration-kv"), [["PROFILE", p.path], ["STATE", badge(p.state)], ["DETAIL", p.detail],
      ["INVALIDATED", p.invalidated ? `${fmt(p.invalidated.invalidated_utc)} by ${fmt(p.invalidated.by)}: ${fmt(p.invalidated.reason)}` : "no"],
      ["PROFILE CREATED", m.created_utc ? U.utc(m.created_utc) : DASH], ["PROFILE FREQUENCY", U.mhz(m.center_frequency_hz)],
      ["PROFILE SAMPLE RATE", U.msps(m.sample_rate_hz)], ["PROFILE GAIN", U.num(m.gain_db, 1, "dB")],
      ["FREQUENCY vs SESSION", yes(comp.center_frequency)], ["SAMPLE RATE vs SESSION", yes(comp.sample_rate)], ["GAIN vs SESSION", yes(comp.gain)],
      ["CALIBRATION LEVEL", m.calibration_level], ["ABSOLUTE CALIBRATION", fmt(m.absolute_calibration)]]);
    $("calibration-note").textContent = "Compatibility is the recorded settings side by side (profile JSON vs this session's resolved plan); REDUCE runs keep their own check in calibration_compatibility_record.json.";
  }

  // ---- CAPTURES / QUICKLOOK
  let stack = null;
  async function renderQuicklook() {
    const q = detail.sections.quicklook;
    $("quicklook-note").textContent = q.state === "OK" ? q.note : "QUICKLOOK did not run for this session (no quicklook/ directory).";
    const th = clear($("quicklook-thumbs"));
    for (const p of q.products || []) {
      if (p.state !== "OK") { th.append(el("div", { class: "ses-thumb ses-missing", text: `${p.label}: MISSING` })); continue; }
      th.append(el("a", { class: "ses-thumb", href: fileUrl(p.path), target: "_blank", rel: "noopener" },
        el("img", { src: fileUrl(p.path), alt: p.label, loading: "lazy" }), el("span", { text: p.label.toUpperCase() })));
    }
    const sb = $("ses-stack-badge");
    U.setEnabled($("btn-stack"), !!(q.stack3d && q.stack3d.state === "OK"), "no stored session_waterfall_state.json");
    if (!(q.stack3d && q.stack3d.state === "OK")) { sb.textContent = "MISSING"; sb.className = "badge status-missing"; }
    else if (!loaded.stack) { sb.textContent = "NOT LOADED"; sb.className = "badge status-not_loaded"; $("ses-stack-message").textContent = "PRESS LOAD 3D STACK"; $("ses-stack-message").hidden = false; if (stack) stack._clearGeometry(); }
    if (!loaded.points) {
      try { points = (await get("/api/sessions/points?id=" + encodeURIComponent(current))).points; loaded.points = true; }
      catch (err) { points = []; showError(err); }
      renderPoints();
    }
  }

  $("btn-stack").addEventListener("click", U.guard($("btn-stack"), async () => {
    const q = detail.sections.quicklook;
    const r = await U.api(fileUrl(q.stack3d.path), { timeoutMs: 60000 });
    if (!r.ok) { showError(r.error); return; }
    const S = window.AlmitaSpectralStack3D;
    if (!S || !S.SpectralStack3D || typeof THREE === "undefined") { $("ses-stack-message").textContent = "3D view unavailable in this browser"; return; }
    if (!stack) stack = new S.SpectralStack3D({ canvas: $("ses-stack-canvas"), viewport: $("ses-stack-viewport"), tooltip: $("ses-stack-tooltip"),
      message: $("ses-stack-message"), badge: $("ses-stack-badge"), resetButton: $("ses-stack-reset"), scale: $("ses-stack-scale"), rangeButton: $("ses-stack-range") });
    loaded.stack = stack.showDocument(r.data);
  }, "LOADING…"));

  const STATUS_COLOR = { success: "--ok", failed: "--error", error: "--error", deferred: "--muted", planned: "--line", capturing: "--warn" };
  let gridGeom = null;
  function renderPoints() {
    const tb = clear($("tbl-points"));
    if (!points.length) emptyRow(tb, 5, "mosaic.csv has no points (or is missing).");
    for (const p of points) {
      const tr = el("tr", { class: "ses-click", tabindex: "0", on: { click: () => openPoint(p.point_id), keydown: (e) => { if (e.key === "Enter") openPoint(p.point_id); } } },
        el("td", { text: p.point_id }), el("td", { text: `${fmt(p.grid_row)}·${fmt(p.grid_col)}` }), el("td", {}, badge(p.capture_status)),
        el("td", { text: p.start_time ? U.utc(p.start_time) : DASH }), el("td", { text: p.altitude_deg_at_goto ? Number(p.altitude_deg_at_goto).toFixed(1) + "°" : DASH }));
      tr.dataset.point = p.point_id;
      tb.append(tr);
    }
    drawGrid();
  }

  function drawGrid(selected) {
    const c = $("points-grid");
    const rows = Math.max(1, ...points.map((p) => Number(p.grid_row) + 1 || 1)), cols = Math.max(1, ...points.map((p) => Number(p.grid_col) + 1 || 1));
    const dpr = window.devicePixelRatio || 1, w = Math.min(320, c.parentElement.clientWidth || 320);
    const cell = Math.max(6, Math.floor(Math.min(w / cols, 360 / rows)));
    const cw = cols * cell, ch = rows * cell;
    c.style.width = cw + "px"; c.style.height = ch + "px";
    c.width = Math.round(cw * dpr); c.height = Math.round(ch * dpr);
    const ctx = c.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.fillStyle = U.cssVar("--inset", "#111"); ctx.fillRect(0, 0, cw, ch);
    for (const p of points) {
      const r = Number(p.grid_row), k = Number(p.grid_col);
      if (!Number.isFinite(r) || !Number.isFinite(k)) continue;
      ctx.fillStyle = U.cssVar(STATUS_COLOR[p.capture_status] || "--warn", "#888");
      ctx.fillRect(k * cell + 1, r * cell + 1, cell - 2, cell - 2);
      if (String(p.point_id) === String(selected)) { ctx.strokeStyle = U.cssVar("--accent", "#65b7d8"); ctx.lineWidth = 2; ctx.strokeRect(k * cell + 1, r * cell + 1, cell - 2, cell - 2); }
    }
    gridGeom = { cell };
  }
  $("points-grid").addEventListener("click", (e) => {
    if (!gridGeom) return;
    const rect = e.currentTarget.getBoundingClientRect();
    const k = Math.floor((e.clientX - rect.left) / gridGeom.cell), r = Math.floor((e.clientY - rect.top) / gridGeom.cell);
    const p = points.find((q) => Number(q.grid_row) === r && Number(q.grid_col) === k);
    if (p) openPoint(p.point_id);
  });

  let currentPoint = null;
  async function openPoint(pid) {
    currentPoint = pid;
    drawGrid(pid);
    for (const r of $("tbl-points").rows) r.classList.toggle("ses-current", r.dataset.point === String(pid));
    $("point-detail").hidden = false; $("point-attrs").hidden = true; $("point-spectrum").hidden = true; $("point-spectrum-note").textContent = "";
    $("point-title").textContent = "POINT " + pid;
    let d;
    try { d = await get(`/api/sessions/point?id=${encodeURIComponent(current)}&point=${encodeURIComponent(pid)}`); } catch (err) { showError(err); return; }
    const r = d.row;
    kv($("point-kv"), [["STATUS", badge(r.capture_status)], ["GRID ROW · COL", `${fmt(r.grid_row)} · ${fmt(r.grid_col)}`], ["SCAN ORDER", r.scan_order],
      ["RA / DEC (target)", `${fmt(r.target_ra_hms || r.ra)} / ${fmt(r.target_dec_dms || r.dec)}`],
      ["ALT / AZ AT GOTO", `${U.fixed(Number(r.altitude_deg_at_goto), 2, "°")} / ${U.fixed(Number(r.azimuth_deg_at_goto), 2, "°")}`],
      ["START → END (UTC)", `${fmt(r.start_time)} → ${fmt(r.end_time)}`], ["TRACKING CONFIRMED", r.tracking_confirmed], ["ERROR", r.error_message || r.error_detail || DASH],
      ["CAPTURE FILE", `${fmt(d.capture.path)} (${d.capture.state}${d.capture.bytes ? ", " + U.bytes(d.capture.bytes) : ""})`]]);
    U.setEnabled($("btn-attrs"), d.capture.state === "OK", "no capture file for this point");
    const pp = clear($("point-products"));
    pp.append(el("div", { class: "muted", text: d.quicklook_products.length ? "QUICKLOOK products of this point:" : "No QUICKLOOK product stored for this point." }));
    for (const f of d.quicklook_products) pp.append(el("div", {}, SERVED.test(f) ? el("a", { href: fileUrl(f), target: "_blank", rel: "noopener", text: f })
      : el("span", { class: "muted", text: f + " (not served by the web)" })));
    const pr = clear($("point-reduce"));
    if (!d.reduce_points.length) pr.append(el("span", { class: "muted", text: "No REDUCE run of this session." }));
    for (const rp of d.reduce_points) {
      const b = el("button", { type: "button", class: "mode-btn", id: null, text: `SPECTRUM · ${rp.run_id}` });
      U.setEnabled(b, rp.state === "OK", "this REDUCE run has no result for this point");
      b.addEventListener("click", U.guard(b, () => showSpectrum(rp, pid), "LOADING…"));
      pr.append(b);
    }
  }
  $("btn-attrs").addEventListener("click", U.guard($("btn-attrs"), async () => {
    try {
      const d = await get(`/api/sessions/capture_attributes?id=${encodeURIComponent(current)}&point=${encodeURIComponent(currentPoint)}`, 30000);
      $("point-attrs").hidden = false; $("point-attrs").textContent = JSON.stringify(d.attributes, null, 1);
    } catch (err) { showError(err); }
  }, "READING…"));

  let lastSpectrum = null;
  async function showSpectrum(rp, pid) {
    const d = await get(`/api/ops/reduce/point?session_dir=${encodeURIComponent(rp.dir)}&point_index=${encodeURIComponent(pid)}`, 30000);
    lastSpectrum = d;
    $("point-spectrum").hidden = false;
    drawSpectrum(d.arrays);
    const m = d.metadata || {}, q = m.quality;
    const quality = q && typeof q === "object" ? (q.flag || q.status || q.label || JSON.stringify(q)) : q;
    $("point-spectrum-note").textContent = `${rp.run_id} · point ${pid} · stored master spectrum (${d.n_bins_full} bins${d.decimation_step > 1 ? ", shown 1 in " + d.decimation_step : ""}) · frame ${fmt(m.velocity_frame)} · quality ${fmt(quality)} · ${fmt(m.calibration_level)} · shaded = masked`;
  }
  U.onThemeChange(() => { if (lastSpectrum && !$("point-spectrum").hidden) drawSpectrum(lastSpectrum.arrays); if (points.length) drawGrid(currentPoint); if (tempData) drawTemps(tempData); });

  function drawSpectrum(arrays) {
    const canvas = $("point-spectrum");
    const dpr = window.devicePixelRatio || 1, cssW = canvas.clientWidth || 900, cssH = 260;
    canvas.width = Math.round(cssW * dpr); canvas.height = Math.round(cssH * dpr);
    const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, cssW, cssH);
    const freq = arrays.frequency_hz || [], val = arrays.relative_intensity || [], mask = arrays.mask;
    const fin = val.filter((v) => Number.isFinite(v));
    ctx.font = "12px ui-monospace,monospace"; ctx.fillStyle = U.cssVar("--muted", "#91a2ad");
    if (!freq.length || !fin.length) { ctx.fillText("no stored relative intensity to plot for this point", 12, cssH / 2); return; }
    const lo = Math.min(...fin), hi = Math.max(...fin), pad = 24;
    const x = (i) => pad + (i / Math.max(1, freq.length - 1)) * (cssW - 2 * pad);
    const y = (v) => cssH - pad - ((v - lo) / Math.max(hi - lo, 1e-30)) * (cssH - 2 * pad);
    ctx.strokeStyle = U.cssVar("--line", "#2b3942"); ctx.strokeRect(pad, pad, cssW - 2 * pad, cssH - 2 * pad);
    if (mask) { ctx.fillStyle = U.cssRgba("--error", 0.15); for (let i = 0; i < mask.length; i++) if (mask[i]) ctx.fillRect(x(i) - 1, pad, 2, cssH - 2 * pad); }
    ctx.beginPath(); ctx.strokeStyle = U.cssVar("--accent", "#65b7d8"); ctx.lineWidth = 1;
    let pen = false;
    for (let i = 0; i < freq.length; i++) {
      if (!Number.isFinite(val[i])) { pen = false; continue; }          // a gap stays a gap: nothing drawn through nulls
      if (!pen) { ctx.moveTo(x(i), y(val[i])); pen = true; } else ctx.lineTo(x(i), y(val[i]));
    }
    ctx.stroke();
    ctx.fillStyle = U.cssVar("--muted", "#91a2ad"); ctx.font = "10px ui-monospace,monospace";
    ctx.fillText(U.mhz(freq[0]), pad, cssH - 6);
    ctx.textAlign = "right"; ctx.fillText(U.mhz(freq[freq.length - 1]), cssW - pad, cssH - 6); ctx.textAlign = "left";
    ctx.fillText(hi.toFixed(4), pad + 2, pad + 10); ctx.fillText(lo.toFixed(4), pad + 2, cssH - pad - 2);
  }

  // ---- REDUCE
  function renderReduce() {
    const runs = detail.sections.reduce.runs, tb = clear($("tbl-reduce"));
    if (!runs.length) emptyRow(tb, 10, "NOT_RUN: no REDUCE run of this campaign.");
    for (const r of runs) {
      const view = el("button", { type: "button", class: "mode-btn", text: "VIEW", on: { click: () => viewReduce(r) } });
      const pref = el("button", { type: "button", class: "mode-btn" + (r.preferred ? " active" : ""), text: r.preferred ? "PREFERRED" : "MAKE PREFERRED" });
      pref.addEventListener("click", U.guard(pref, () => saveAnnotation({ preferred_reduce: r.preferred ? null : r.run_id }), "SAVING…"));
      const q = r.quality_counts ? Object.entries(r.quality_counts).map(([k, v]) => `${k} ${v}`).join(", ") : DASH;
      tb.append(el("tr", {}, el("td", {}, el("div", { text: r.run_id }), el("div", { class: "muted ses-id", text: r.dir })), el("td", {}, badge(r.status)),
        el("td", { text: r.started_utc ? U.utc(r.started_utc) : DASH }), el("td", { text: fmt(r.velocity_frame) }),
        el("td", { class: "ses-id", text: fmt(r.calibration_profile_path) }),
        el("td", { text: r.counts ? `${fmt(r.counts.points_completed)} / ${fmt(r.counts.points_discovered)}` : `${r.points_written} written` }),
        el("td", { text: q }), el("td", { text: fmt(r.git_commit) }), el("td", { text: (r.science_runs || []).length ? String(r.science_runs.length) : "0" }),
        el("td", {}, view, " ", pref)));
    }
    fillRunChoices("REDUCE", runs, "cr-a", "cr-b", "btn-compare-runs");
  }
  function viewReduce(r) {
    $("reduce-detail").hidden = false; $("reduce-title").textContent = "REDUCE " + r.run_id;
    kv($("reduce-kv"), [["STATUS", badge(r.status)], ["CAMPAIGN LINK", `${fmt(r.campaign_dir)} · ${fmt(r.link_evidence)}`],
      ["STARTED → ENDED (UTC)", `${fmt(r.started_utc)} → ${fmt(r.ended_utc)}`], ["PIPELINE / CODE", `${fmt(r.pipeline_version)} · git ${fmt(r.git_commit)}`],
      ["CONFIG HASH", r.config_hash], ["PROFILE", r.calibration_profile_path], ["PROFILE HASH", r.calibration_profile_hash],
      ["VELOCITY FRAME", r.velocity_frame], ["COUNTS", r.counts ? JSON.stringify(r.counts) : `manifest ${r.manifest}`],
      ["CALIBRATION LEVELS", JSON.stringify(r.calibration_level_counts || {})], ["MEDIAN USABLE FRACTION", U.fixed(r.median_usable_fraction, 3)],
      ["MEDIAN BASELINE RMS FRACTION", U.fixed(r.median_baseline_rms_fraction, 4)], ["COMPATIBILITY RECORD", `${r.compatibility_record.state} · ${r.compatibility_record.path}`],
      ["FILES", `manifest ${r.manifest} · provenance ${r.provenance} · config ${r.config}`]]);
    $("reduce-qc").textContent = "qc/ (as stored):\n" + JSON.stringify(r.qc, null, 1);
  }

  // Two runs of the SAME kind only (REDUCE with REDUCE, SCIENCE with SCIENCE): the server refuses a mixed pair anyway.
  function fillRunChoices(kind, runs, aId, bId, btnId) {
    for (const id of [aId, bId]) {
      const s = clear($(id));
      for (const r of runs) s.append(el("option", { value: r.run_id, text: `${kind} · ${r.run_id}` }));
    }
    if (runs.length > 1) $(bId).selectedIndex = 1;
    U.setEnabled($(btnId), runs.length > 1, `this session has fewer than two ${kind} runs`);
  }
  function compareRuns(aId, bId, outId) {
    return async () => {
    const out = clear($(outId));
    try {
      const d = await get(`/api/sessions/compare_runs?id=${encodeURIComponent(current)}&a=${encodeURIComponent($(aId).value)}&b=${encodeURIComponent($(bId).value)}`);
      out.append(compareTable(d.fields, d.a, d.b));
      if (d.blockers.length) out.append(el("div", { class: "obs-summary status-warning", text: "Not directly comparable: " + d.blockers.join(", ") }));
      if (d.config_diff.length) out.append(el("h3", { class: "ses-h3", text: "config.json DIFFERENCES" }), compareTable(d.config_diff.map((c) => ({ field: c.key, a: c.a, b: c.b, same: false })), d.a, d.b));
      else out.append(el("div", { class: "obs-summary muted", text: "config.json: identical (or missing in one of them)." }));
      if (d.kind === "REDUCE") out.append(spectrumOverlay(d));
      if (d.kind === "SCIENCE") out.append(mapsSideBySide([{ label: "A · " + d.a, dir: d.dirs.a, maps: d.maps.a }, { label: "B · " + d.b, dir: d.dirs.b, maps: d.maps.b }]));
    } catch (err) { out.append(el("div", { class: "obs-summary status-error", text: U.errorText(err) })); }
    };
  }
  $("btn-compare-runs").addEventListener("click", U.guard($("btn-compare-runs"), compareRuns("cr-a", "cr-b", "compare-runs-out"), "COMPARING…"));
  $("btn-compare-sci").addEventListener("click", U.guard($("btn-compare-sci"), compareRuns("cs-a", "cs-b", "compare-sci-out"), "COMPARING…"));

  // Two stored SCIENCE maps next to each other. Each PNG keeps its own stored colour scale: shapes compare, colours do not.
  function mapsSideBySide(sides) {
    const box = el("div", {}, el("h3", { class: "ses-h3", text: "STORED A/B/C MAPS SIDE BY SIDE" }));
    const row = el("div", { class: "ses-thumbs ses-pair" });
    for (const s of sides) {
      const name = (s.maps || []).find((m) => m === "map_abc_combined.png") || (s.maps || [])[0];
      row.append(name ? el("a", { class: "ses-thumb", href: fileUrl(`${s.dir}/maps/${name}`), target: "_blank", rel: "noopener" },
        el("img", { src: fileUrl(`${s.dir}/maps/${name}`), alt: s.label, loading: "lazy" }), el("span", { text: `${s.label} · ${name}` }))
        : el("div", { class: "ses-thumb ses-missing", text: `${s.label}: no stored map` }));
    }
    box.append(row, el("div", { class: "obs-summary muted", text: "Each map was drawn with its own stored colour scale: compare shapes and positions, not colours. Nothing is re-rendered here." }));
    return box;
  }

  // The same point of two REDUCE runs on one frequency axis and one shared intensity range (no per-run rescaling).
  function spectrumOverlay(d) {
    const box = el("div", {}, el("h3", { class: "ses-h3", text: "SPECTRA OF ONE POINT, BOTH RUNS" }));
    if (!(d.common_points || []).length) { box.append(el("div", { class: "obs-summary muted", text: "The two runs have no point in common." })); return box; }
    const sel = el("select", { id: "ov-point", "aria-label": "point to overlay" });
    for (const p of d.common_points) sel.append(el("option", { value: String(p), text: "point " + p }));
    const canvas = el("canvas", { class: "ses-plot", "aria-label": "two stored spectra of the same point" });
    const note = el("div", { class: "obs-summary muted" });
    const btn = el("button", { type: "button", text: "OVERLAY" });
    btn.addEventListener("click", U.guard(btn, async () => {
      const q = (dir) => get(`/api/ops/reduce/point?session_dir=${encodeURIComponent(dir)}&point_index=${encodeURIComponent(sel.value)}`, 30000);
      const [ra, rb] = await Promise.all([q(d.dirs.a), q(d.dirs.b)]);
      drawOverlay(canvas, ra.arrays, rb.arrays);
      const fa = ra.arrays.frequency_hz || [], fb = rb.arrays.frequency_hz || [];
      const sameAxis = fa.length === fb.length && fa[0] === fb[0] && fa[fa.length - 1] === fb[fb.length - 1];
      const ia = ra.arrays.relative_intensity || [], ib = rb.arrays.relative_intensity || [];
      const identical = sameAxis && ia.length === ib.length && ia.every((v, i) => v === ib[i]);
      note.textContent = `A ${d.a} (${fmt((ra.metadata || {}).velocity_frame)}) in accent · B ${d.b} (${fmt((rb.metadata || {}).velocity_frame)}) in orange · ` +
        (sameAxis ? "same frequency axis" : "DIFFERENT frequency axes: drawn on the union of both, not interpolated") +
        (identical ? " · the two stored spectra are IDENTICAL bin by bin (B covers A)" : "") + (d.blockers.length ? " · not directly comparable: " + d.blockers.join(", ") : "");
    }, "LOADING…"));
    box.append(el("div", { class: "obs-actions" }, sel, btn), canvas, note);
    return box;
  }
  function drawOverlay(canvas, A, B) {
    const dpr = window.devicePixelRatio || 1, w = canvas.clientWidth || 900, h = 260;
    canvas.width = Math.round(w * dpr); canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    const fin = (arr) => (arr || []).filter((v) => Number.isFinite(v));
    const freqs = fin(A.frequency_hz).concat(fin(B.frequency_hz)), vals = fin(A.relative_intensity).concat(fin(B.relative_intensity));
    if (!freqs.length || !vals.length) { ctx.fillStyle = U.cssVar("--muted", "#91a2ad"); ctx.fillText("nothing stored to plot", 12, h / 2); return; }
    const f0 = Math.min(...freqs), f1 = Math.max(...freqs), lo = Math.min(...vals), hi = Math.max(...vals), pad = 24;
    const x = (f) => pad + ((f - f0) / Math.max(1, f1 - f0)) * (w - 2 * pad), y = (v) => h - pad - ((v - lo) / Math.max(hi - lo, 1e-30)) * (h - 2 * pad);
    ctx.strokeStyle = U.cssVar("--line", "#2b3942"); ctx.strokeRect(pad, pad, w - 2 * pad, h - 2 * pad);
    for (const [arr, col] of [[A, U.cssVar("--accent", "#65b7d8")], [B, U.dataColor("#d95926")]]) {
      ctx.beginPath(); ctx.strokeStyle = col; ctx.lineWidth = 1; let pen = false;
      const fq = arr.frequency_hz || [], v = arr.relative_intensity || [];
      for (let i = 0; i < fq.length; i++) {
        if (!Number.isFinite(v[i])) { pen = false; continue; }
        if (!pen) { ctx.moveTo(x(fq[i]), y(v[i])); pen = true; } else ctx.lineTo(x(fq[i]), y(v[i]));
      }
      ctx.stroke();
    }
    ctx.fillStyle = U.cssVar("--muted", "#91a2ad"); ctx.font = "10px ui-monospace,monospace";
    ctx.fillText(U.mhz(f0), pad, h - 6); ctx.textAlign = "right"; ctx.fillText(U.mhz(f1), w - pad, h - 6); ctx.textAlign = "left";
    ctx.fillText(hi.toFixed(4), pad + 2, pad + 10); ctx.fillText(lo.toFixed(4), pad + 2, h - pad - 2);
  }

  function compareTable(fields, a, b) {
    const body = el("tbody");
    for (const f of fields) body.append(el("tr", { class: f.blocking ? "row-warn" : null }, el("td", { text: f.field }),
      el("td", { text: typeof f.a === "object" && f.a !== null ? JSON.stringify(f.a) : fmt(f.a) }),
      el("td", { text: typeof f.b === "object" && f.b !== null ? JSON.stringify(f.b) : fmt(f.b) }),
      el("td", { class: f.same === null ? "status-unknown" : null, text: f.same === null ? "NOT RECORDED" : f.same ? "same" : "DIFFERENT" })));
    return el("div", { class: "table-wrap" }, el("table", { class: "status-table" }, el("thead", {}, el("tr", {}, el("th", { text: "FIELD" }), el("th", { text: "A · " + a }),
      el("th", { text: "B · " + b }), el("th", { text: "" }))), body));
  }

  // ---- SCIENCE
  function renderScience() {
    const runs = detail.sections.science.runs, tb = clear($("tbl-science"));
    if (!runs.length) emptyRow(tb, 7, "NOT_RUN: no SCIENCE run on this session's REDUCE runs.");
    for (const s of runs) {
      const view = el("button", { type: "button", class: "mode-btn", text: "VIEW", on: { click: () => viewScience(s) } });
      const pref = el("button", { type: "button", class: "mode-btn" + (s.preferred ? " active" : ""), text: s.preferred ? "PREFERRED" : "MAKE PREFERRED" });
      pref.addEventListener("click", U.guard(pref, () => saveAnnotation({ preferred_science: { [s.reduce_run_id]: s.preferred ? null : s.run_id } }), "SAVING…"));
      tb.append(el("tr", { class: s.link_state === "INCONSISTENT" ? "row-warn" : null }, el("td", {}, el("div", { text: s.run_id }), el("div", { class: "muted ses-id", text: s.dir })),
        el("td", { text: fmt(s.reduce_run_id) }), el("td", {}, badge(s.status)), el("td", { text: fmt(s.data_completeness) }),
        el("td", { title: s.link_detail }, badge(s.link_state)), el("td", { text: fmt(s.git_commit) }), el("td", {}, view, " ", pref)));
    }
    fillRunChoices("SCIENCE", runs, "cs-a", "cs-b", "btn-compare-sci");
    const tp = clear($("tbl-pending")), pend = detail.sections.science.pending_associations;
    if (!pend.length) emptyRow(tp, 4, "None.");
    const confirmed = ((detail.annotation || {}).confirmed_links || []);
    for (const p of pend) {
      const b = el("button", { type: "button", class: "mode-btn", text: p.confirmed ? "UNCONFIRM" : "CONFIRM IT BELONGS HERE" });
      b.addEventListener("click", U.guard(b, () => {
        const links = p.confirmed ? confirmed.filter((c) => c.dir !== p.dir) : confirmed.concat([{ dir: p.dir, note: "confirmed in SESSIONS" }]);
        return saveAnnotation({ confirmed_links: links });
      }, "SAVING…"));
      tp.append(el("tr", {}, el("td", {}, el("div", { text: p.run_id }), el("div", { class: "muted ses-id", text: p.dir })),
        el("td", { text: `${p.reason}${p.candidates && p.candidates.length ? " · candidate because its name contains this session's capture id" : ""}` }),
        el("td", { text: p.confirmed ? "YES (operator)" : "no" }), el("td", {}, b)));
    }
  }
  function viewScience(s) {
    $("science-detail").hidden = false; $("science-title").textContent = `SCIENCE ${s.run_id} · ${s.link_detail || ""}`;
    const m = clear($("science-maps"));
    if (!s.maps.length) m.append(el("div", { class: "ses-thumb ses-missing", text: "no stored map (maps/*.png)" }));
    for (const name of s.maps) {
      const rel = `${s.dir}/maps/${name}`;
      m.append(el("a", { class: "ses-thumb", href: fileUrl(rel), target: "_blank", rel: "noopener" }, el("img", { src: fileUrl(rel), alt: name, loading: "lazy" }), el("span", { text: name })));
    }
    const n = clear($("science-notes"));
    if (s.notes_file && s.notes_file.state === "OK") n.append(el("a", { href: fileUrl(s.notes_file.path), target: "_blank", rel: "noopener", text: "maps/NOTES.md (as stored)" }));
    n.append(el("div", { class: "muted", text: "Each map keeps its own stored colour scale. Exports: " + s.dir + "/maps/" }));
  }

  // ---- INSTRUMENT
  const ROLE_COLORS = { sdr: "#3987e5", lna: "#d95926", load: "#199e70", cable: "#c98500" };
  let tempData = null;
  async function renderInstrument() {
    const r = detail.receiver || {};
    kv($("instrument-kv"), [["RECEIVER", receiverText(r)], ["RECEIVER NOTE", r.note], ["CONFIG LABEL (observer_config sdr_device)", r.config_label],
      ["ABOUT THAT LABEL", r.config_label_note], ["MOUNT (log)", detail.mount], ["TEMPERATURE SOURCE", detail.sections.instrument.temperature_source]]);
    if (loaded.temps) return;
    try { tempData = await get("/api/sessions/temperatures?id=" + encodeURIComponent(current), 60000); loaded.temps = true; } catch (err) { showError(err); return; }
    const tb = clear($("tbl-temps")), st = tempData.stats || {};
    if (!Object.keys(st).length) emptyRow(tb, 6, tempData.detail || "No temperature samples.");
    for (const [role, s] of Object.entries(st)) tb.append(el("tr", {}, el("td", { text: role.toUpperCase() }), el("td", { text: U.fixed(s.first, 2, "°C") }),
      el("td", { text: U.fixed(s.last, 2, "°C") }), el("td", { text: U.fixed(s.min, 2, "°C") }), el("td", { text: U.fixed(s.max, 2, "°C") }), el("td", { text: `${s.valid} / ${s.samples}` })));
    const cov = tempData.coverage;
    $("temp-note").textContent = `${tempData.state} · source ${tempData.source}` + (tempData.window ? ` · session ${U.utc(tempData.window.session_start_utc)} → ${U.utc(tempData.window.session_end_utc)} (±${tempData.window.pad_minutes} min shown)` : "") +
      (cov ? ` · coverage ${cov.samples_in_session} of ~${cov.expected_one_per_minute} expected one-a-minute samples (${Math.round(cov.fraction * 100)} %)` : "") +
      " · external log, not read by the captures; no correction is applied to any capture";
    drawTemps(tempData);
  }

  function drawTemps(d) {
    const c = $("ses-temp-chart"), series = d.series || {};
    const dpr = window.devicePixelRatio || 1, w = c.clientWidth || 900, h = 260;
    c.width = Math.round(w * dpr); c.height = Math.round(h * dpr);
    const ctx = c.getContext("2d"); ctx.setTransform(dpr, 0, 0, dpr, 0, 0); ctx.clearRect(0, 0, w, h);
    const all = [];
    for (const pts of Object.values(series)) for (const [t, v] of pts) if (Number.isFinite(v)) all.push([Date.parse(t), v]);
    ctx.font = "11px ui-monospace,monospace"; ctx.fillStyle = U.cssVar("--muted", "#91a2ad");
    if (!all.length) { ctx.fillText(d.detail || "no temperature samples for this session", 14, h / 2); return; }
    const t0 = Math.min(...all.map((p) => p[0])), t1 = Math.max(...all.map((p) => p[0]));
    let lo = Math.min(...all.map((p) => p[1])), hi = Math.max(...all.map((p) => p[1]));
    if (hi - lo < 0.5) { lo -= 0.25; hi += 0.25; }
    const L = 48, R = 70, T = 14, B = 26;
    const x = (t) => L + ((t - t0) / Math.max(1, t1 - t0)) * (w - L - R), y = (v) => h - B - ((v - lo) / (hi - lo)) * (h - T - B);
    ctx.strokeStyle = U.cssVar("--line", "#2b3942"); ctx.strokeRect(L, T, w - L - R, h - T - B);
    if (d.window) {      // the session itself, shaded: outside it is context only
      const s0 = Date.parse(d.window.session_start_utc), s1 = Date.parse(d.window.session_end_utc);
      ctx.fillStyle = U.cssRgba("--accent", 0.08); ctx.fillRect(x(s0), T, Math.max(1, x(s1) - x(s0)), h - T - B);
    }
    for (const [role, pts] of Object.entries(series)) {
      const col = U.dataColor(ROLE_COLORS[role] || "#9085e9");
      ctx.strokeStyle = col; ctx.lineWidth = 1.5; ctx.beginPath();
      let pen = false, last = null;
      for (const [t, v] of pts) {
        if (!Number.isFinite(v)) { pen = false; continue; }
        const px = x(Date.parse(t)), py = y(v);
        if (!pen) { ctx.moveTo(px, py); pen = true; } else ctx.lineTo(px, py);
        last = [px, py];
      }
      ctx.stroke();
      if (last) { ctx.fillStyle = col; ctx.fillText(role.toUpperCase(), last[0] + 4, last[1] + 4); }
    }
    ctx.fillStyle = U.cssVar("--muted", "#91a2ad");
    ctx.fillText(hi.toFixed(1) + " °C", 4, T + 8); ctx.fillText(lo.toFixed(1) + " °C", 4, h - B);
    ctx.fillText(U.utcTime(new Date(t0).toISOString()), L, h - 8);
    ctx.textAlign = "right"; ctx.fillText(U.utcTime(new Date(t1).toISOString()), w - R, h - 8); ctx.textAlign = "left";
  }

  // ---- FILES
  async function renderFiles(dir) {
    let d;
    try { d = await get(`/api/sessions/files?id=${encodeURIComponent(current)}&dir=${encodeURIComponent(dir)}`, 60000); } catch (err) { showError(err); return; }
    const size = detail.sections.files.size || {}, camp = size.campaign || {}, prod = size.products || {};
    const cards = clear($("files-cards"));
    cards.append(card("CAMPAIGN ON DISK", U.bytes(camp.bytes_on_disk)), card("CAMPAIGN FILES", camp.files), card("APPARENT SIZE", U.bytes(camp.apparent_bytes)),
      card("REDUCE + SCIENCE RUNS", U.bytes(prod.bytes_on_disk)), card("HARD LINKS COUNTED ONCE", camp.hardlinks_counted_once), card("SYMLINKS (not followed)", camp.symlinks));
    $("files-method").textContent = "HOW: " + detail.sections.files.size_method + ". HDF5 (IQ) files are listed but never offered for download here.";
    const cr = clear($("files-crumbs"));
    const parts = d.dir ? d.dir.split("/") : [];
    cr.append(el("button", { type: "button", class: "mode-btn", text: detail.campaign_id, on: { click: () => renderFiles("") } }));
    parts.forEach((p, i) => cr.append(" / ", el("button", { type: "button", class: "mode-btn", text: p, on: { click: () => renderFiles(parts.slice(0, i + 1).join("/")) } })));
    const tb = clear($("tbl-files"));
    for (const sub of d.dirs) tb.append(el("tr", { class: "ses-click", tabindex: "0", on: { click: () => renderFiles((d.dir ? d.dir + "/" : "") + sub.name) } },
      el("td", { text: sub.name + "/" }), el("td", { text: `${U.bytes(sub.bytes_on_disk)} · ${sub.files} files` }), el("td", { text: DASH }), el("td", { class: "muted", text: "open" })));
    for (const f of d.files) tb.append(el("tr", {}, el("td", { text: f.name }), el("td", { text: U.bytes(f.bytes) }), el("td", { text: U.utc(f.mtime_utc) }),
      el("td", {}, f.downloadable ? el("a", { href: fileUrl(f.path), target: "_blank", rel: "noopener", text: "download" }) : el("span", { class: "muted", text: /\.h5$/.test(f.name) ? "IQ: not served" : "not served" }))));
    if (!d.dirs.length && !d.files.length) emptyRow(tb, 4, "Empty directory.");
    if (d.truncated) emptyRow(tb, 4, "Listing truncated (first 500 files).");
    if (!dir) {
      const tp = clear($("tbl-products"));
      if (!d.products.length) emptyRow(tp, 4, "No REDUCE or SCIENCE run.");
      for (const p of d.products) tp.append(el("tr", {}, el("td", { text: `${p.kind} ${p.run_id}` }), el("td", { class: "ses-id", text: p.dir }), el("td", { text: p.files }), el("td", { text: U.bytes(p.bytes_on_disk) })));
    }
  }

  // ---- ORGANIZE
  function renderOrganize() {
    const a = detail.annotation || {};
    $("a-name").value = a.name || ""; $("a-tags").value = (a.tags || []).join(", "); $("a-notes").value = a.notes || "";
    $("a-favorite").checked = !!a.favorite; $("a-reference").checked = !!a.reference; $("a-archived").checked = !!a.archived;
    $("a-condition").value = (a.condition || {}).value || ""; $("a-condition-note").value = (a.condition || {}).note || "";
    $("a-receiver").value = (a.receiver || {}).value || ""; $("a-receiver-note").value = (a.receiver || {}).note || "";
    $("discard-state").textContent = a.discarded ? `DISCARDED FOR ANALYSIS since ${U.utc(a.discarded.utc)}: ${a.discarded.reason}` : "Not discarded.";
    $("a-discard-reason").value = "";
    U.setEnabled($("btn-undiscard"), !!a.discarded, "not discarded");
    $("save-out").textContent = a.updated_utc ? "last saved " + U.utc(a.updated_utc) : "";
  }

  async function saveAnnotation(patch) {
    try {
      await post("/api/sessions/annotate", { id: current, patch });
      const keep = currentTab;
      await openSession(current, keep);
      loadCatalog();
      return true;
    } catch (err) { showError(err); $("save-out").textContent = "NOT SAVED: " + U.errorText(err); return false; }
  }
  $("btn-save").addEventListener("click", U.guard($("btn-save"), () => {
    const cond = $("a-condition").value, rec = $("a-receiver").value.trim();
    return saveAnnotation({
      name: $("a-name").value, notes: $("a-notes").value,
      tags: $("a-tags").value.split(",").map((t) => t.trim()).filter(Boolean),
      favorite: $("a-favorite").checked, reference: $("a-reference").checked, archived: $("a-archived").checked,
      condition: cond ? { value: cond, note: $("a-condition-note").value } : null,
      receiver: rec ? { value: rec, note: $("a-receiver-note").value } : null,
    });
  }, "SAVING…"));
  $("btn-discard").addEventListener("click", U.guard($("btn-discard"), () => {
    const reason = $("a-discard-reason").value.trim();
    if (!reason) { $("save-out").textContent = "A reason is required to discard a session for analysis."; return false; }
    return saveAnnotation({ discarded: { reason } });
  }, "SAVING…"));
  $("btn-undiscard").addEventListener("click", U.guard($("btn-undiscard"), () => saveAnnotation({ discarded: null }), "SAVING…"));

  // ---- EXPORT
  async function renderExports() {
    const tb = clear($("tbl-exports"));
    let d;
    try { d = await get("/api/sessions/exports?id=" + encodeURIComponent(current)); } catch (err) { showError(err); return; }
    if (!d.exports.length) emptyRow(tb, 3, "No export yet.");
    for (const x of d.exports) tb.append(el("tr", {}, el("td", { text: x.name }), el("td", { text: U.bytes(x.bytes) }),
      el("td", {}, el("a", { href: "/api/sessions/export_file?path=" + encodeURIComponent(x.zip), text: "download .zip" }))));
  }
  $("btn-export").addEventListener("click", U.guard($("btn-export"), async () => {
    try {
      const r = await post("/api/sessions/export", { id: current }, 180000);
      $("export-out").textContent = `written ${r.zip} (${U.bytes(r.bytes)}, ${r.figures} figures)` + (r.warnings.length ? ` · ${r.warnings.length} warning(s) inside` : "");
      await renderExports();
    } catch (err) { showError(err); $("export-out").textContent = "NOT EXPORTED: " + U.errorText(err); }
  }, "EXPORTING…"));

  // ---- COMPARE
  async function renderCompareChoices() {
    if (!allSessions.length) await loadAllForCompare();
    const s = clear($("cmp-b"));
    for (const x of allSessions) if (x.campaign_id !== current) s.append(el("option", { value: x.campaign_id, text: `${x.display_name} · ${x.campaign_id}` }));
    U.setEnabled($("btn-compare"), s.options.length > 0, "no other session");
  }
  $("btn-compare").addEventListener("click", U.guard($("btn-compare"), async () => {
    const out = clear($("compare-out"));
    try {
      const d = await get(`/api/sessions/compare?a=${encodeURIComponent(current)}&b=${encodeURIComponent($("cmp-b").value)}`, 60000);
      out.append(el("div", { class: "obs-summary " + (d.direct_comparison ? "status-ok" : "status-warning"),
        text: d.direct_comparison ? "No recorded difference blocks a direct comparison." : "NOT DIRECTLY COMPARABLE — " + d.blockers.join(" · ") }));
      if ((d.unknown || []).length) out.append(el("div", { class: "obs-summary status-warning",
        text: "CANNOT BE CHECKED (not recorded in one or both sessions): " + d.unknown.join(" · ") + ". Unknown is not the same: document it in ORGANIZE." }));
      out.append(compareTable(d.fields, d.a.campaign_id, d.b.campaign_id));
      if (d.a.science || d.b.science) out.append(mapsSideBySide([d.a, d.b].map((x, i) => ({ label: `${i ? "B" : "A"} · ${x.campaign_id}` + (x.science ? ` · ${x.science.run_id}` : ""),
        dir: x.science ? x.science.dir : "", maps: x.science ? x.science.maps : [] }))));
      const body = el("tbody");
      for (const [role, v] of Object.entries(d.temperatures)) body.append(el("tr", {}, el("td", { text: role.toUpperCase() }),
        el("td", { text: v.a ? `${U.fixed(v.a.min, 2)} … ${U.fixed(v.a.max, 2)} °C` : DASH }), el("td", { text: v.b ? `${U.fixed(v.b.min, 2)} … ${U.fixed(v.b.max, 2)} °C` : DASH })));
      out.append(el("h3", { class: "ses-h3", text: "TEMPERATURES DURING EACH SESSION (min … max, external log)" }),
        el("div", { class: "table-wrap" }, el("table", { class: "status-table" }, el("thead", {}, el("tr", {}, el("th", { text: "SENSOR" }), el("th", { text: "A" }), el("th", { text: "B" }))), body)),
        el("div", { class: "obs-summary muted", text: d.note }));
    } catch (err) { out.append(el("div", { class: "obs-summary status-error", text: U.errorText(err) })); }
  }, "COMPARING…"));

  // ------------------------------------------------------------------ start
  window.addEventListener("resize", () => { if (points.length && !$("sec-quicklook").hidden) drawGrid(currentPoint); });
  (async () => {
    await loadCatalog();
    loadHistory();
    const h = new URLSearchParams(location.hash.slice(1));
    if (h.get("id")) await openSession(h.get("id"), h.get("tab"));
  })();
})();
