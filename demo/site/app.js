// ALMITA static demo - SIMULATED. Sky: HI4PI (CAR_E01) through a beam, precomputed in demo/build_demo_data.py.
// Everything else (receiver, noise, frequency switching, fold, baseline) is computed here, in the browser.
"use strict";

const C = 299792.458, F0 = 1420405752, TSYS = 120, ETA = 0.5;
const DNU = 6110;                    // channel width, Hz (~1.29 km/s)
const BB_HALF = 1.0e6;               // usable baseband, +/- Hz
const $ = (id) => document.getElementById(id);
let SKY = null, specs = [], seed = 1;
const state = { l: 10, b: 0, beam: 0, t: 120, delta: 600000, ripple: 0.005 };

// ---------------------------------------------------------------- data
function b64u16(s) { const bin = atob(s), u = new Uint16Array(bin.length / 2);
  for (let i = 0; i < u.length; i++) u[i] = bin.charCodeAt(2 * i) | (bin.charCodeAt(2 * i + 1) << 8); return u; }
function b64u8(s) { const bin = atob(s), u = new Uint8Array(bin.length); for (let i = 0; i < u.length; i++) u[i] = bin.charCodeAt(i); return u; }

async function load() {
  SKY = await (await fetch("sky.json")).json();
  specs = SKY.beams.map((bm) => { const q = b64u16(bm.spectra_u16_b64), f = new Float32Array(q.length);
    for (let i = 0; i < q.length; i++) f[i] = q[i] / 65535 * bm.scale_K; return f; });
  drawSky(); observe();
}

const NV = () => SKY.v_km_s.n;
const vAt = (k) => SKY.v_km_s.start + k * SKY.v_km_s.step;

// bilinear interpolation on the 1-deg grid (l 0..20, b -10..10)
function gridInterp(l, b, fn) {
  const L = SKY.l_grid, B = SKY.b_grid;
  const x = Math.min(Math.max(l - L[0], 0), L.length - 1.0001), y = Math.min(Math.max(b - B[0], 0), B.length - 1.0001);
  const i = Math.floor(x), j = Math.floor(y), fx = x - i, fy = y - j;
  return fn(j, i) * (1 - fx) * (1 - fy) + fn(j, i + 1) * fx * (1 - fy) + fn(j + 1, i) * (1 - fx) * fy + fn(j + 1, i + 1) * fx * fy;
}
function hi4piSpectrum(l, b, beam) {
  const n = NV(), nl = SKY.l_grid.length, s = specs[beam], out = new Float32Array(n);
  for (let k = 0; k < n; k++) out[k] = gridInterp(l, b, (j, i) => s[(j * nl + i) * n + k]);
  return out;
}
const cover = (l, b, beam) => gridInterp(l, b, (j, i) => SKY.beams[beam].cover[j][i]);
const shiftAt = (l, b) => gridInterp(l, b, (j, i) => SKY.lsrk_shift_km_s[j][i]);
function tAtV(spec, v) {                       // K at LSRK velocity v (0 outside the cube)
  const x = (v - SKY.v_km_s.start) / SKY.v_km_s.step;
  if (x < 0 || x > NV() - 1) return 0;
  const i = Math.floor(x), f = x - i; return spec[i] * (1 - f) + spec[Math.min(i + 1, NV() - 1)] * f;
}

// ---------------------------------------------------------------- noise
function rng(s) { return () => { s |= 0; s = (s + 0x6D2B79F5) | 0; let t = Math.imul(s ^ (s >>> 15), 1 | s);
  t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t; return ((t ^ (t >>> 14)) >>> 0) / 4294967296; }; }
function gauss(r) { let u = 0; while (u === 0) u = r(); return Math.sqrt(-2 * Math.log(u)) * Math.cos(2 * Math.PI * r()); }

// ---------------------------------------------------------------- the instrument
const band = (bb) => (1 + 0.12 * Math.sin(2 * Math.PI * bb / 9e5 + 0.7)) * Math.exp(-0.5 * Math.pow(bb / 1.1e6, 4));
const freqToVlsrk = (f, shift) => C * (1 - f / F0) + shift;

function simulate() {
  const { l, b, beam, t, delta, ripple } = state;
  const spec = hi4piSpectrum(l, b, beam), shift = shiftAt(l, b), r = rng(seed * 7919 + 13);
  const lineFrac = (f) => ETA * tAtV(spec, freqToVlsrk(f, shift)) / TSYS;
  const nbb = Math.round(2 * BB_HALF / DNU), bb = new Float64Array(nbb);
  for (let i = 0; i < nbb; i++) bb[i] = -BB_HALF + (i + 0.5) * DNU;
  const res = { spec, shift, bb };
  if (delta === 0) {                                   // no switching: the SDR's raw band, the whole time on f0
    const sig = 1 / Math.sqrt(DNU * t);
    res.raw = Float64Array.from(bb, (x) => band(x) * (1 + lineFrac(F0 + x)) * (1 + sig * gauss(r)) * (Math.abs(x) < 4e3 ? 1.6 : 1));
    res.rawLO = F0; return res;
  }
  const fa = F0 - delta, fb = F0 + delta, sig = 1 / Math.sqrt(DNU * t / 2);
  const A = new Float64Array(nbb), B = new Float64Array(nbb), S = new Float64Array(nbb);
  const phase = (l * 0.37 + b * 0.61) % (2 * Math.PI);  // the leftover ripple depends on where the dish points
  for (let i = 0; i < nbb; i++) {
    A[i] = band(bb[i]) * (1 + lineFrac(fa + bb[i])) * (1 + sig * gauss(r));
    B[i] = band(bb[i]) * (1 + lineFrac(fb + bb[i])) * (1 + sig * gauss(r));
    S[i] = (A[i] - B[i]) / B[i] + ripple * Math.sin(2 * Math.PI * bb[i] / 1.7e6 + phase);
  }
  res.raw = Float64Array.from(A, (a, i) => a * (Math.abs(bb[i]) < 4e3 ? 1.6 : 1)); res.rawLO = fa;
  // fold onto the RF axis: T(f) = [S(f - fa) - S(f - fb)] / 2, only where both images exist
  const half = BB_HALF - delta, fold = [], interpS = (x) => { const k = (x + BB_HALF) / DNU - 0.5;
    if (k < 0 || k > nbb - 1) return NaN; const i = Math.floor(k), f = k - i; return S[i] * (1 - f) + S[Math.min(i + 1, nbb - 1)] * f; };
  for (let x = -half; x <= half; x += DNU) {
    const f = F0 + x, dcA = Math.abs(f - fa) < 1.5e4, dcB = Math.abs(f - fb) < 1.5e4;
    const val = dcA || dcB ? NaN : 0.5 * (interpS(f - fa) - interpS(f - fb));
    fold.push({ v: freqToVlsrk(f, shift), y: val });
  }
  // baseline: linear, outside where a flat rotation curve (+/- 25 km/s) puts the line for this l
  const win = modelWindow(l);
  const pts = fold.filter((p) => isFinite(p.y) && (p.v < win[0] || p.v > win[1]));
  if (pts.length > 20) {
    const n = pts.length, sx = pts.reduce((a, p) => a + p.v, 0), sy = pts.reduce((a, p) => a + p.y, 0);
    const sxx = pts.reduce((a, p) => a + p.v * p.v, 0), sxy = pts.reduce((a, p) => a + p.v * p.y, 0);
    const m = (n * sxy - sx * sy) / (n * sxx - sx * sx || 1), c0 = (sy - m * sx) / n;
    fold.forEach((p) => { p.y -= m * p.v + c0; });
    res.baseline = { bins: n, window: win };
  } else res.baseline = { bins: pts.length, window: win, none: true };
  res.fold = fold; res.ghostKms = 2 * delta / F0 * C;
  return res;
}

function modelWindow(l) {                              // flat rotation curve: v from 0 to V0(1 - sin l) at the tangent
  const s = Math.sin(l * Math.PI / 180), vt = 220 * (1 - s);
  return [-35, Math.max(35, vt + 25)];
}

// ---------------------------------------------------------------- statistics
function corr(a, b) { const m = a.map((x, i) => [x, b[i]]).filter(([x, y]) => isFinite(x) && isFinite(y));
  if (m.length < 20) return NaN; const n = m.length, ma = m.reduce((s, p) => s + p[0], 0) / n, mb = m.reduce((s, p) => s + p[1], 0) / n;
  let sab = 0, saa = 0, sbb = 0; for (const [x, y] of m) { sab += (x - ma) * (y - mb); saa += (x - ma) ** 2; sbb += (y - mb) ** 2; }
  return sab / Math.sqrt(saa * sbb); }

// ---------------------------------------------------------------- drawing
const css = (v) => getComputedStyle(document.documentElement).getPropertyValue(v).trim();
function viridis(t) { const c = [[68,1,84],[59,82,139],[33,145,140],[94,201,98],[253,231,37]];
  const x = Math.min(Math.max(t, 0), 1) * 4, i = Math.min(Math.floor(x), 3), f = x - i;
  return c[i].map((v, k) => Math.round(v + (c[i + 1][k] - v) * f)); }

function skyGeom(cv) { const m = SKY.mom0, pad = 64, W = cv.width - 2 * pad, H = cv.height - 2 * pad;
  const l0 = Math.max(...m.l), l1 = Math.min(...m.l), b0 = Math.min(...m.b), b1 = Math.max(...m.b);
  return { pad, W, H, x: (l) => pad + (l0 - l) / (l0 - l1) * W, y: (b) => pad + (b1 - b) / (b1 - b0) * H,
    l: (x) => l0 - (x - pad) / W * (l0 - l1), b: (y) => b1 - (y - pad) / H * (b1 - b0), degPx: W / (l0 - l1) }; }

function drawSky() {
  const cv = $("sky"), g = cv.getContext("2d"), m = SKY.mom0, q = b64u8(m.u8_b64), [ny, nx] = m.shape, G = skyGeom(cv);
  g.fillStyle = css("--panel"); g.fillRect(0, 0, cv.width, cv.height);
  const img = g.createImageData(nx, ny);
  for (let j = 0; j < ny; j++) for (let i = 0; i < nx; i++) {
    const src = q[j * nx + i], col = viridis(Math.sqrt(src / 255)), jj = m.b[0] < m.b[ny - 1] ? ny - 1 - j : j;
    const ii = m.l[0] > m.l[nx - 1] ? i : nx - 1 - i, o = 4 * (jj * nx + ii);
    img.data[o] = col[0]; img.data[o + 1] = col[1]; img.data[o + 2] = col[2]; img.data[o + 3] = 255; }
  const tmp = document.createElement("canvas"); tmp.width = nx; tmp.height = ny; tmp.getContext("2d").putImageData(img, 0, 0);
  g.imageSmoothingEnabled = true; g.drawImage(tmp, G.pad, G.pad, G.W, G.H);
  // the pointable area and the axes
  g.strokeStyle = "rgba(255,255,255,.35)"; g.setLineDash([6, 6]); g.lineWidth = 2;
  g.strokeRect(G.x(20), G.y(10), G.x(0) - G.x(20), G.y(-10) - G.y(10)); g.setLineDash([]);
  g.fillStyle = css("--dim"); g.font = "22px IBM Plex Mono, monospace"; g.textAlign = "center";
  for (const l of [0, 5, 10, 15, 20]) g.fillText(l + "°", G.x(l), cv.height - 14);
  g.textAlign = "right"; for (const b of [-10, 0, 10]) g.fillText(b + "°", G.pad - 8, G.y(b) + 7);
  g.textAlign = "left"; g.fillText("galactic longitude l (east is left), latitude b", G.pad, 32);
  // the beam
  const fw = SKY.beams[state.beam].fwhm_deg, cx = G.x(state.l), cy = G.y(state.b);
  g.strokeStyle = css("--warn"); g.lineWidth = 3; g.beginPath(); g.arc(cx, cy, fw / 2 * G.degPx, 0, 2 * Math.PI); g.stroke();
  g.beginPath(); g.moveTo(cx - 12, cy); g.lineTo(cx + 12, cy); g.moveTo(cx, cy - 12); g.lineTo(cx, cy + 12); g.stroke();
  const c = cover(state.l, state.b, state.beam);
  $("pointing").innerHTML = `<span>l <b>${state.l.toFixed(1)}°</b></span><span>b <b>${state.b.toFixed(1)}°</b></span>` +
    `<span>beam <b>${fw}°</b> (half power circle)</span><span>beam inside the HI4PI tile <b>${Math.round(100 * c)} %</b></span>` +
    `<span>LSRK − topo <b>${shiftAt(state.l, state.b).toFixed(1)} km/s</b></span>`;
}

function axes(cv, xr, yr, xlab, ylab, ticksX) {
  const k = Math.min(3, Math.max(1, cv.width / Math.max(1, cv.clientWidth)));   // canvas px per CSS px
  const g = cv.getContext("2d"), p = { l: 62 * k, r: 14 * k, t: 12 * k, b: 40 * k }, W = cv.width - p.l - p.r, H = cv.height - p.t - p.b;
  const fpx = Math.round(12 * k);
  g.fillStyle = css("--panel"); g.fillRect(0, 0, cv.width, cv.height);
  const X = (x) => p.l + (x - xr[0]) / (xr[1] - xr[0]) * W, Y = (y) => p.t + (1 - (y - yr[0]) / (yr[1] - yr[0])) * H;
  g.strokeStyle = css("--line"); g.lineWidth = 1; g.fillStyle = css("--dim"); g.font = `${fpx}px IBM Plex Mono, monospace`;
  g.textAlign = "center";
  for (const x of ticksX) { g.beginPath(); g.moveTo(X(x), p.t); g.lineTo(X(x), p.t + H); g.stroke(); g.fillText(String(x), X(x), p.t + H + 1.4 * fpx); }
  const ny = 5; g.textAlign = "right";
  for (let k = 0; k <= ny; k++) { const y = yr[0] + k * (yr[1] - yr[0]) / ny; g.beginPath(); g.moveTo(p.l, Y(y)); g.lineTo(p.l + W, Y(y)); g.stroke();
    g.fillText(fmt(y), p.l - 0.5 * fpx, Y(y) + 0.35 * fpx); }
  g.textAlign = "center"; g.fillText(xlab, p.l + W / 2, cv.height - 0.5 * fpx);
  g.save(); g.translate(1.1 * fpx, p.t + H / 2); g.rotate(-Math.PI / 2); g.fillText(ylab, 0, 0); g.restore();
  g.lineWidthScale = k;
  return { g, X, Y, k };
}
const fmt = (y) => Math.abs(y) >= 10 ? y.toFixed(0) : Math.abs(y) >= 1 ? y.toFixed(1) : y.toFixed(2);
function line(g, xs, ys, X, Y, color, width, dash) {
  g.strokeStyle = color; g.lineWidth = width; g.setLineDash(dash || []); g.beginPath(); let pen = false;
  for (let i = 0; i < xs.length; i++) { if (!isFinite(ys[i])) { pen = false; continue; }
    if (!pen) { g.moveTo(X(xs[i]), Y(ys[i])); pen = true; } else g.lineTo(X(xs[i]), Y(ys[i])); }
  g.stroke(); g.setLineDash([]);
}

function drawRaw(res) {
  const mhz = Array.from(res.bb, (x) => (res.rawLO + x) / 1e6), y = Array.from(res.raw);
  const lo = Math.min(...y), hi = Math.max(...y), m0 = mhz[0], m1 = mhz[mhz.length - 1], ticks = [];
  for (let t = Math.ceil(m0 * 2) / 2; t <= m1; t += 0.5) ticks.push(+t.toFixed(1));
  const A = axes($("raw"), [m0, m1], [lo - 0.05 * (hi - lo), hi + 0.05 * (hi - lo)], "frequency [MHz]", "power", ticks);
  line(A.g, mhz, y, A.X, A.Y, css("--accent"), 1.2 * A.k);
  A.g.strokeStyle = css("--warn"); A.g.setLineDash([5, 5]); A.g.beginPath(); A.g.moveTo(A.X(F0 / 1e6), A.Y(lo)); A.g.lineTo(A.X(F0 / 1e6), A.Y(hi)); A.g.stroke(); A.g.setLineDash([]);
  $("rawNote").textContent = state.delta ? `LO A = ${(res.rawLO / 1e6).toFixed(3)} MHz; dashed: 1420.406 MHz (HI at rest). The band shape hides the line here: the switching removes it.`
    : "Without switching the line hides in the band shape: real ALMITA needs a calibration profile for this, and the demo does not reduce it.";
}

function drawReduced(res) {
  if (!res.fold) { const A = axes($("red"), [-150, 150], [-0.05, 0.25], "LSRK velocity [km/s]", "fraction of Tsys", [-150, -100, -50, 0, 50, 100, 150]);
    A.g.fillStyle = css("--dim"); A.g.textAlign = "center"; A.g.fillText("Choose frequency switching to get a folded spectrum.", A.X(0), A.Y(0.1));
    $("verdict").textContent = "No switching: nothing to fold."; return; }
  const v = res.fold.map((p) => p.v), y = res.fold.map((p) => p.y);
  const truth = v.map((x) => ETA * tAtV(res.spec, x) / TSYS);
  // the null: HI4PI 8 deg away (inside the region), same beam
  const l2 = state.l > 10 ? state.l - 8 : state.l + 8, nullSpec = hi4piSpectrum(l2, state.b, state.beam);
  const nul = v.map((x) => ETA * tAtV(nullSpec, x) / TSYS);
  const all = y.concat(truth).filter(isFinite), lo = Math.min(...all), hi = Math.max(...all);
  const vmin = Math.min(...v), vmax = Math.max(...v), ticks = [];
  for (let t = Math.ceil(vmin / 25) * 25; t <= vmax; t += 25) ticks.push(t);
  const A = axes($("red"), [vmin, vmax], [lo - 0.08 * (hi - lo), hi + 0.08 * (hi - lo)], "LSRK velocity [km/s]", "fraction of Tsys", ticks);
  A.g.fillStyle = "rgba(76,201,240,.07)"; A.g.fillRect(A.X(Math.max(res.baseline.window[0], vmin)), A.Y(hi + 0.08 * (hi - lo)),
    A.X(Math.min(res.baseline.window[1], vmax)) - A.X(Math.max(res.baseline.window[0], vmin)), A.Y(lo - 0.08 * (hi - lo)) - A.Y(hi + 0.08 * (hi - lo)));
  line(A.g, v, y, A.X, A.Y, css("--accent"), 1.2 * A.k);
  line(A.g, v, nul, A.X, A.Y, css("--null"), 1.4 * A.k, [5 * A.k, 4 * A.k]);
  line(A.g, v, truth, A.X, A.Y, css("--truth"), 1.4 * A.k);
  const rSame = corr(y, truth), rOther = corr(y, nul), sigma = Math.SQRT2 / Math.sqrt(DNU * state.t);
  const peak = Math.max(...truth), snr = peak / sigma;
  let msg = `Line peak ${(100 * peak).toFixed(1)} % of Tsys; noise per channel ${(100 * sigma).toFixed(2)} % (S/N ≈ ${snr.toFixed(0)}). ` +
    `Shape correlation with HI4PI here r = ${rSame.toFixed(2)}; with HI4PI 8° away r = ${rOther.toFixed(2)}. `;
  if (!isFinite(rSame) || rSame < 0.5) msg += "Not enough signal yet: integrate longer.";
  else if (rSame - rOther < 0.03) msg += "You see hydrogen at the right velocities, but this beam cannot tell this direction from its neighbour.";
  else msg += "The spectrum is specific to this direction: it beats the neighbour.";
  if (state.delta === 300000) msg += ` Throw ±300 kHz: a negative "ghost" of the line sits ${res.ghostKms.toFixed(0)} km/s from it.`;
  $("verdict").textContent = msg;
}

function observe() { if (!SKY) return; const res = simulate(); drawSky(); drawRaw(res); drawReduced(res); }

// ---------------------------------------------------------------- controls
const T_MIN = 5, T_MAX = 1800;
function timeFromSlider() { const s = +$("time").value / 100; state.t = Math.round(T_MIN * Math.pow(T_MAX / T_MIN, s));
  $("timeLabel").textContent = state.t < 120 ? `${state.t} s` : `${(state.t / 60).toFixed(state.t < 600 ? 1 : 0)} min`; }
$("time").addEventListener("input", timeFromSlider);
$("time").addEventListener("change", observe);
$("beam").addEventListener("change", (e) => { state.beam = +e.target.value; observe(); });
$("mode").addEventListener("change", (e) => { state.delta = +e.target.value; observe(); });
$("ripple").addEventListener("change", (e) => { state.ripple = +e.target.value; observe(); });
$("observe").addEventListener("click", observe);
$("again").addEventListener("click", () => { seed++; observe(); });
$("sky").addEventListener("pointerdown", (e) => {
  const cv = $("sky"), rect = cv.getBoundingClientRect(), G = skyGeom(cv);
  const x = (e.clientX - rect.left) * cv.width / rect.width, y = (e.clientY - rect.top) * cv.height / rect.height;
  state.l = Math.min(20, Math.max(0, G.l(x))); state.b = Math.min(10, Math.max(-10, G.b(y))); seed++; observe();
});
let resizeTimer = null;
window.addEventListener("resize", () => { clearTimeout(resizeTimer); resizeTimer = setTimeout(observe, 200); });
timeFromSlider();
load().catch((err) => { $("verdict").textContent = "Could not load the simulated sky: " + err; });
