"use strict";
// SPECTRAL STACK 3D — ANTENNA A
//
// Renders capture.py's per-point spectrum, already reduced and accumulated
// by quicklook_session_waterfall.py into session_waterfall_state.json (one
// decimated row per grid point — the same product the WATERFALL thumbnail's
// PNG is built from). No second DSP pipeline: this only reads that existing
// file client-side and renders it as a 3D line stack in the browser, so the
// Pi never runs a renderer for this (matplotlib or otherwise) — the exact
// architecture that fixed today's earlier matplotlib OOM incident.
//
// Written as a self-contained module (own fetch/state, no dependency on
// app.js internals) so a future ANTENNA B instance can reuse it by
// constructing a second SpectralStack3D with a different canvas/data URL,
// without touching this file.
(function () {
  const PARAMETERS = new URLSearchParams(location.search);
  const RUNTIME_ROOT = PARAMETERS.get("root") || window.ALMITA_RUNTIME_ROOT || "/runtime";
  const DATA_URL = `${RUNTIME_ROOT}/quicklook_products/session_waterfall_state.json`;
  const STALE_SECONDS = 45; // looser than the 2s poll: a real point can take
  // many seconds, so this only flags a genuinely stuck/hung producer.

  const COLOR_STOPS = [
    [0.00, 0.22, 0.32, 0.95],   // brighter than the old dark blue: the lowest values must stay visible on black
    [0.25, 0.00, 0.75, 0.80],
    [0.50, 0.05, 0.75, 0.10],
    [0.75, 1.00, 0.90, 0.00],
    [1.00, 0.90, 0.10, 0.05],
  ];
  // presentation mode (BL-094) zooms the whole page; the renderer and the tooltip work in unzoomed CSS px
  function pageZoom() {
    const z = document.documentElement.hasAttribute("data-present") ? parseFloat(getComputedStyle(document.documentElement).zoom) : 1;
    return Number.isFinite(z) && z > 0 ? z : 1;
  }

  function colormap(t) {
    t = Math.min(1, Math.max(0, t));
    for (let i = 1; i < COLOR_STOPS.length; i++) {
      const [t0, r0, g0, b0] = COLOR_STOPS[i - 1];
      const [t1, r1, g1, b1] = COLOR_STOPS[i];
      if (t <= t1) {
        const f = t1 > t0 ? (t - t0) / (t1 - t0) : 0;
        return [r0 + (r1 - r0) * f, g0 + (g1 - g0) * f, b0 + (b1 - b0) * f];
      }
    }
    return COLOR_STOPS[COLOR_STOPS.length - 1].slice(1);
  }

  function robustLimits(rows) {
    const values = [];
    for (const row of rows) for (const v of row.values) if (Number.isFinite(v)) values.push(v);
    if (!values.length) return { lo: -1, hi: 1 };
    values.sort((a, b) => a - b);
    const at = (p) => values[Math.min(values.length - 1, Math.max(0, Math.round(p * (values.length - 1))))];
    const lo = at(0.02), hi = at(0.98);
    return hi > lo ? { lo, hi } : { lo: lo - 1, hi: hi + 1 };
  }

  // The vertical scale (and colour) of the stack: the FULL range of the data, exactly what the 2D session waterfall's
  // colour bar spans (quicklook_session_waterfall.py: imshow without vmin/vmax). The robust 2-98% range used before
  // made the box only ~0.35 dB tall on real data, so a +0.68 dB band-edge value rose 2.3x above it and read as a huge
  // peak the spectrum and waterfall do not show. Nothing exceeds the box now; heights stay proportional to the dB.
  function dataLimits(rows) {
    let lo = Infinity, hi = -Infinity;
    for (const row of rows) for (const v of row.values) if (Number.isFinite(v)) { if (v < lo) lo = v; if (v > hi) hi = v; }
    if (!Number.isFinite(lo)) return { lo: -1, hi: 1 };
    return hi > lo ? { lo, hi } : { lo: lo - 1, hi: hi + 1 };
  }

  // Captures that are not like the rest of the session. The scale above follows the FULL range of the data, so a
  // single anomalous capture can flatten all the others: on 2026-10-14 two captures out of 625 (+3.6 and +10.2 dB
  // over the whole band, at 6-7 deg altitude) made the other 623 - spread over +/-0.2 dB - look like one flat sheet.
  // A capture is an outlier when its median offset from the session's median spectrum is far from every other
  // capture's: more than max(1 dB, 8 robust sigmas of the per-capture offsets).
  function outlierRows(rows) {
    const out = new Map();
    if (rows.length < 5 || !rows[0].values.length) return out;
    const nBins = rows[0].values.length;
    const median = (arr) => { const a = arr.filter(Number.isFinite).sort((x, y) => x - y); if (!a.length) return NaN; const m = a.length >> 1; return a.length % 2 ? a[m] : (a[m - 1] + a[m]) / 2; };
    const sessionMedian = [];
    for (let i = 0; i < nBins; i++) sessionMedian.push(median(rows.map((r) => r.values[i])));
    const offsets = rows.map((r) => median(r.values.map((v, i) => (Number.isFinite(v) && Number.isFinite(sessionMedian[i]) ? v - sessionMedian[i] : NaN))));
    const center = median(offsets);
    const mad = median(offsets.map((o) => Math.abs(o - center)));
    const limit = Math.max(1.0, 8 * 1.4826 * (Number.isFinite(mad) ? mad : 0));
    offsets.forEach((o, index) => { if (Number.isFinite(o) && Math.abs(o - center) > limit) out.set(index, o - center); });
    return out;
  }

  function parseDocument(doc, sessionId) {
    if (!doc || doc.session_id !== sessionId) return null;
    const freq = Array.isArray(doc.frequency_mhz) ? doc.frequency_mhz : [];
    const rawRows = Array.isArray(doc.rows) ? doc.rows : [];
    const rows = [];
    for (const r of rawRows) {
      const scanOrder = parseInt(r.point_id, 10);
      if (!Number.isFinite(scanOrder)) continue;
      if (!Array.isArray(r.relative_db) || r.relative_db.length !== freq.length) continue;
      rows.push({ pointId: String(r.point_id), scanOrder, utc: r.utc, values: r.relative_db });
    }
    rows.sort((a, b) => a.scanOrder - b.scanOrder);
    return { freq, rows, updatedUtc: doc.updated_utc };
  }

  class SpectralStack3D {
    constructor({ canvas, viewport, tooltip, message, badge, resetButton, scale, rangeButton }) {
      this.canvas = canvas; this.viewport = viewport; this.tooltip = tooltip; this.scale = scale || null;
      this.rangeButton = rangeButton || null;
      this.fullRange = false;          // false: scale from the ordinary captures, outliers clipped and marked
      this.message = message; this.badge = badge; this.resetButton = resetButton;
      this.lastSessionId = null; this.lastUpdatedUtc = null; this.lastFetchFailed = false;
      this.fetchInFlight = false;
      this.webglAvailable = true;
      this.lines = []; this.surfaceMesh = null; this.freq = []; this.rows = [];
      try {
        this._initScene();
      } catch (error) {
        this.webglAvailable = false;
        this._setState("ERROR", `WebGL unavailable: ${error && error.message ? error.message : error}`);
      }
      if (this.resetButton) this.resetButton.addEventListener("click", () => this._resetCamera());
      if (this.rangeButton) this.rangeButton.addEventListener("click", () => {
        this.fullRange = !this.fullRange;
        if (this.rows.length) this._buildGeometry(this.freq, this.rows);
      });
      if (this.webglAvailable) {
        this.canvas.addEventListener("mousemove", (event) => this._onHover(event));
        this.canvas.addEventListener("mouseleave", () => this._hideTooltip());
        this._observeResize();
        this._animate();
      }
    }

    _initScene() {
      this.renderer = new THREE.WebGLRenderer({ canvas: this.canvas, antialias: true });
      this.renderer.setClearColor(0x000000, 1);   // black, like the rest of the console (operator request, BL-061)
      this.scene = new THREE.Scene();
      this.camera = new THREE.PerspectiveCamera(45, 1, 0.1, 100);
      this._defaultCamera = { position: new THREE.Vector3(0, 6, 11), target: new THREE.Vector3(0, 0.5, 0) };
      this.camera.position.copy(this._defaultCamera.position);
      this.controls = new THREE.OrbitControls(this.camera, this.renderer.domElement);
      this.controls.target.copy(this._defaultCamera.target);
      this.controls.enableDamping = true;
      this.controls.dampingFactor = 0.08;
      this.controls.update();
      this.group = new THREE.Group();
      this.scene.add(this.group);
      const box = new THREE.BoxGeometry(12, 4, 10);
      const edges = new THREE.EdgesGeometry(box);
      this.boundsHelper = new THREE.LineSegments(edges, new THREE.LineBasicMaterial({ color: 0x3a4a55 }));
      this.boundsHelper.position.set(0, 2, -5);
      this.scene.add(this.boundsHelper);
      this.raycaster = new THREE.Raycaster();
      this.raycaster.params.Line = { threshold: 0.12 };
      this._resize();
    }

    _resetCamera() {
      if (!this.webglAvailable) return;
      this.camera.position.copy(this._defaultCamera.position);
      this.controls.target.copy(this._defaultCamera.target);
      this.controls.update();
    }

    _observeResize() {
      if (typeof ResizeObserver === "undefined") { window.addEventListener("resize", () => this._resize()); return; }
      new ResizeObserver(() => this._resize()).observe(this.viewport);
    }

    _resize() {
      if (!this.webglAvailable) return;
      const w = this.viewport.clientWidth || 1, h = this.viewport.clientHeight || 1;
      this.renderer.setPixelRatio(pageZoom());    // 1 except in presentation mode (BL-094), where the page is zoomed
      this.renderer.setSize(w, h, false);
      this.camera.aspect = w / h;
      this.camera.updateProjectionMatrix();
    }

    _animate() {
      requestAnimationFrame(() => this._animate());
      this.controls.update();
      this.renderer.render(this.scene, this.camera);
    }

    _setState(state, detail) {
      if (this.badge) {
        this.badge.textContent = state;
        this.badge.className = `badge status-${state.toLowerCase()}`;
      }
      if (this.message) {
        const text = { WAITING: "WAITING FOR DATA", LOADING: "LOADING…", STALE: "STALE — NO RECENT UPDATE",
          ERROR: detail || "ERROR", COMPLETED: "", LIVE: "", STORED: "" }[state];
        this.message.textContent = text || "";
        this.message.hidden = !text;
      }
      if (this.resetButton) this.resetButton.hidden = state === "WAITING" || state === "LOADING" || state === "ERROR" || !this.webglAvailable;
    }

    _clearGeometry() {
      for (const line of this.lines) { this.group.remove(line); line.geometry.dispose(); line.material.dispose(); }
      this.lines = [];
      if (this.surfaceMesh) {
        this.group.remove(this.surfaceMesh);
        this.surfaceMesh.geometry.dispose(); this.surfaceMesh.material.dispose();
        this.surfaceMesh = null;
      }
    }

    _buildGeometry(freq, rows) {
      this._clearGeometry();
      if (!rows.length || !freq.length) return;
      const outliers = outlierRows(rows);
      const clipOutliers = !this.fullRange && outliers.size > 0;
      const { lo, hi } = clipOutliers ? dataLimits(rows.filter((_, i) => !outliers.has(i))) : dataLimits(rows);
      const span = hi - lo || 1;
      const fmt = (v) => `${v >= 0 ? "+" : ""}${v.toFixed(2)}`;
      if (this.scale) {
        this.scale.hidden = false;
        if (clipOutliers) {
          const list = [...outliers.entries()].sort((a, b) => Math.abs(b[1]) - Math.abs(a[1])).slice(0, 4)
            .map(([i, o]) => `point ${rows[i].pointId} (${fmt(o)} dB)`).join(", ");
          this.scale.textContent = `height & colour: relative PSD ${lo.toFixed(2)} … ${fmt(hi)} dB (full range of the `
            + `${rows.length - outliers.size} ordinary captures). ${outliers.size} capture(s) far off the rest, drawn `
            + `CLIPPED in white: ${list}${outliers.size > 4 ? ", …" : ""}`;
        } else {
          this.scale.textContent = `height & colour: relative PSD ${lo.toFixed(2)} … ${fmt(hi)} dB `
            + "(full data range, as in the session waterfall)";
        }
      }
      if (this.rangeButton) {
        this.rangeButton.hidden = outliers.size === 0;
        this.rangeButton.textContent = this.fullRange ? "CLIP OUTLIERS" : "FULL RANGE";
      }
      const scanOrders = rows.map((r) => r.scanOrder);
      const minScan = Math.min(...scanOrders), maxScan = Math.max(...scanOrders);
      const scanSpan = (maxScan - minScan) || 1;
      const xFor = (i) => (i / Math.max(1, freq.length - 1)) * 12 - 6;
      const yFor = (rawValue) => {
        const v = Math.min(hi, Math.max(lo, Number.isFinite(rawValue) ? rawValue : lo));   // clipped only when outliers are excluded
        return ((v - lo) / span) * 3.6;
      };
      const zFor = (scanOrder) => -((scanOrder - minScan) / scanSpan) * 10;

      // Shared per-vertex position/color, indexed [row][freqBin] - reused
      // both by the per-capture ridge lines and the filled surface below,
      // so the DSP-derived values are only walked once.
      const rowVertices = rows.map((row, rowIndex) => {
        const positions = new Float32Array(freq.length * 3);
        const colors = new Float32Array(freq.length * 3);
        const z = zFor(row.scanOrder);
        const marked = clipOutliers && outliers.has(rowIndex);
        for (let i = 0; i < freq.length; i++) {
          const raw = row.values[i];
          positions[i * 3] = xFor(i);
          positions[i * 3 + 1] = yFor(raw);
          positions[i * 3 + 2] = z;
          const [r, g, b] = marked ? [1, 1, 1] : colormap(((Number.isFinite(raw) ? Math.min(hi, Math.max(lo, raw)) : lo) - lo) / span);
          colors[i * 3] = r; colors[i * 3 + 1] = g; colors[i * 3 + 2] = b;
        }
        return { positions, colors };
      });

      for (let rowIndex = 0; rowIndex < rows.length; rowIndex++) {
        const { positions, colors } = rowVertices[rowIndex];
        const geometry = new THREE.BufferGeometry();
        geometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
        geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
        const material = new THREE.LineBasicMaterial({ vertexColors: true });
        const line = new THREE.Line(geometry, material);
        line.userData = { rowIndex };
        this.group.add(line);
        this.lines.push(line);
      }

      // Semi-transparent filled surface between consecutive captures, so
      // the gap between ridge lines doesn't read as empty space - the
      // lines drawn on top stay the crisp per-capture profile.
      if (rows.length >= 2 && freq.length >= 2) {
        const vertexCount = rows.length * freq.length;
        const positions = new Float32Array(vertexCount * 3);
        const colors = new Float32Array(vertexCount * 3);
        for (let rowIndex = 0; rowIndex < rows.length; rowIndex++) {
          positions.set(rowVertices[rowIndex].positions, rowIndex * freq.length * 3);
          colors.set(rowVertices[rowIndex].colors, rowIndex * freq.length * 3);
        }
        const indices = [];
        for (let rowIndex = 0; rowIndex < rows.length - 1; rowIndex++) {
          for (let i = 0; i < freq.length - 1; i++) {
            const a = rowIndex * freq.length + i, b = a + 1;
            const c = (rowIndex + 1) * freq.length + i, d = c + 1;
            indices.push(a, c, b, b, c, d);
          }
        }
        const surfaceGeometry = new THREE.BufferGeometry();
        surfaceGeometry.setAttribute("position", new THREE.BufferAttribute(positions, 3));
        surfaceGeometry.setAttribute("color", new THREE.BufferAttribute(colors, 3));
        surfaceGeometry.setIndex(indices);
        surfaceGeometry.computeVertexNormals();
        const surfaceMaterial = new THREE.MeshBasicMaterial({
          vertexColors: true, side: THREE.DoubleSide, transparent: true, opacity: 0.5,
        });
        this.surfaceMesh = new THREE.Mesh(surfaceGeometry, surfaceMaterial);
        this.group.add(this.surfaceMesh);
      }
      this._limits = { lo, hi, minScan, maxScan };
    }

    _onHover(event) {
      if (!this.lines.length) return;
      const rect = this.canvas.getBoundingClientRect();
      const mouse = new THREE.Vector2(
        ((event.clientX - rect.left) / rect.width) * 2 - 1,
        -((event.clientY - rect.top) / rect.height) * 2 + 1,
      );
      this.raycaster.setFromCamera(mouse, this.camera);
      const hits = this.raycaster.intersectObjects(this.lines, false);
      if (!hits.length) { this._hideTooltip(); return; }
      const hit = hits[0];
      const row = this.rows[hit.object.userData.rowIndex];
      if (!row) { this._hideTooltip(); return; }
      const positions = hit.object.geometry.attributes.position;
      let nearest = 0, best = Infinity;
      for (let i = 0; i < positions.count; i++) {
        const dx = positions.getX(i) - hit.point.x;
        const d = Math.abs(dx);
        if (d < best) { best = d; nearest = i; }
      }
      const freqMhz = this.freq[nearest];
      const power = row.values[nearest];
      this.tooltip.textContent =
        `scan_order  ${row.scanOrder}\n` +
        `point_id    ${row.pointId}\n` +
        `frequency   ${freqMhz != null ? freqMhz.toFixed(3) : "—"} MHz\n` +
        `power       ${power != null ? power.toFixed(2) : "—"} dB\n` +
        `utc         ${row.utc || "—"}`;
      this.tooltip.style.left = `${(event.clientX - rect.left) / pageZoom()}px`;
      this.tooltip.style.top = `${(event.clientY - rect.top) / pageZoom()}px`;
      this.tooltip.hidden = false;
    }

    _hideTooltip() { if (this.tooltip) this.tooltip.hidden = true; }

    async update(sessionId, acquisitionState) {
      if (!this.webglAvailable) return; // ERROR state set once in the constructor and left as-is
      if (!sessionId) {
        this.lastSessionId = null; this.lastUpdatedUtc = null;
        this._clearGeometry(); this.rows = [];
        this._setState("WAITING");
        return;
      }
      if (sessionId !== this.lastSessionId) {
        this.lastSessionId = sessionId; this.lastUpdatedUtc = null;
        this._clearGeometry(); this.rows = [];
        this._setState("LOADING");
      }
      if (this.fetchInFlight) return;
      this.fetchInFlight = true;
      try {
        const response = await fetch(DATA_URL, { cache: "no-store" });
        if (response.status === 404) {
          // Normal at the start of a session: Quicklook hasn't produced its
          // first point yet, so the symlinked artifact doesn't exist yet -
          // that's WAITING, not a broken/corrupt artifact (ERROR).
          this._setState("WAITING");
          return;
        }
        if (!response.ok) throw new Error(`HTTP ${response.status}`);
        const doc = await response.json();
        const parsed = parseDocument(doc, sessionId);
        this.lastFetchFailed = false;
        if (!parsed || !parsed.rows.length) {
          this._setState("WAITING");
          return;
        }
        const changed = parsed.updatedUtc !== this.lastUpdatedUtc;
        if (changed) {
          this.freq = parsed.freq; this.rows = parsed.rows;
          this._buildGeometry(parsed.freq, parsed.rows);
        }
        this.lastUpdatedUtc = parsed.updatedUtc;
        const ageSeconds = parsed.updatedUtc ? (Date.now() - Date.parse(parsed.updatedUtc)) / 1000 : Infinity;
        const running = acquisitionState === "RUNNING" || acquisitionState === "STARTING";
        if (running && ageSeconds > STALE_SECONDS) this._setState("STALE");
        else this._setState(running ? "LIVE" : "COMPLETED");
      } catch (error) {
        this.lastFetchFailed = true;
        this._setState("ERROR", "artifact missing or unreadable");
      } finally {
        this.fetchInFlight = false;
      }
    }
  }

  // SESSIONS (BL-085): draw a session_waterfall_state.json read from a stored campaign, handed over by the caller. It
  // never fetches the live runtime artifact and never changes what MONITOR shows (each page owns its own instance).
  SpectralStack3D.prototype.showDocument = function (doc) {
    if (!this.webglAvailable) return false;
    const parsed = doc ? parseDocument(doc, doc.session_id) : null;
    this._clearGeometry(); this.rows = [];
    if (!parsed || !parsed.rows.length) { this._setState("ERROR", "no stored rows to draw"); return false; }
    this.freq = parsed.freq; this.rows = parsed.rows;
    this._buildGeometry(parsed.freq, parsed.rows);
    this._setState("STORED");
    return true;
  };

  function syncPanelHeightToLeftColumn() {
    // Flexbox align-items:stretch alone proved unreliable here (the panel
    // ended up far taller than the antenna column at some viewport widths,
    // leaving a big empty area) - so the panel's height is pinned to the
    // antenna column's own measured height explicitly instead of hoping
    // the browser's flex layout infers the same thing.
    const left = document.querySelector(".science-row-left");
    const panel = document.getElementById("spectral-stack-panel");
    if (!left || !panel) return;
    const apply = () => { panel.style.height = `${left.getBoundingClientRect().height}px`; };
    apply();
    if (typeof ResizeObserver !== "undefined") new ResizeObserver(apply).observe(left);
    else window.addEventListener("resize", apply);
  }

  function boot() {
    syncPanelHeightToLeftColumn();
    const canvas = document.getElementById("spectral-stack-canvas");
    if (!canvas || typeof THREE === "undefined") return;
    const instance = new SpectralStack3D({
      canvas,
      viewport: document.getElementById("spectral-stack-viewport"),
      tooltip: document.getElementById("spectral-stack-tooltip"),
      message: document.getElementById("spectral-stack-message"),
      badge: document.getElementById("spectral-stack-badge"),
      resetButton: document.getElementById("spectral-stack-reset"),
      scale: document.getElementById("spectral-stack-scale"),
      rangeButton: document.getElementById("spectral-stack-range"),
    });
    window.SpectralStack3D = instance;
  }

  if (document.readyState === "loading") document.addEventListener("DOMContentLoaded", boot);
  else boot();

  window.AlmitaSpectralStack3D = { colormap, robustLimits, dataLimits, parseDocument, outlierRows, SpectralStack3D };
})();
