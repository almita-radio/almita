// AUDIO (operator 2026-11-17: "una función no científica ... poder escuchar la subida de volumen cuando estemos transitando por
// un área que tenga más hidrógeno"). NOT a measurement. Loaded by the header's AUDIO button (common.js) on its first click.
//
// Spectra: during a capture, the capture's own clean spectrum (runtime/main_rfi_mask_latest.json, written by
// rfi_mask_live.py - costs the capture nothing); otherwise the idle reader's (runtime/sdr_audio_latest.json, sdr_audio.py,
// a 0.5 s sample every 2 s only while nothing else uses the SDR). All the sound is made HERE, in the browser.
//
// What you hear, from each spectrum (2048 bins over the whole band):
//   1. the spectrum is divided by its own level at the band's outer parts (gain drifts cancel) and narrow lines are
//      shaved off (running median);
//   2. EXCESS = how far each bin is above the LOWEST it has been since AUDIO was switched on (a floor that very slowly
//      forgets): more hydrogen than the least you have pointed at - that is what sounds. Pointing at one place only, it
//      fades to the quiet bed: it is the CHANGE that you hear;
//   3. a window FOLLOWS the hydrogen: centred on the excess, as wide as it is (200 kHz - 1 MHz), gliding;
//   4. HISS: the window cut into 16 slices, each slice a band of noise between 300 Hz and 3 kHz (low radio frequency =
//      receding gas = low sound) as loud as its excess - more hydrogen, more hiss;
//   5. TONE: one soft note at the excess's mean velocity (receding = lower; 440 Hz at 0 km/s, an octave per 150 km/s)
//      as loud as its peak.
(function () {
  "use strict";
  const C_KM_S = 299792.458, HI_REST_HZ = 1420405751.77;
  const N_BANDS = 16, AUDIO_LO = 300, AUDIO_HI = 3000;
  const MIN_WIDTH_HZ = 200e3, MAX_WIDTH_HZ = 1.0e6, ZONE_HALF_HZ = 0.95e6, OUTER_FROM_HZ = 0.7e6;
  const FLOOR_RISE = 0.0005;                    // per spectrum: the floor forgets ~1.5 %/min at one spectrum every 2 s
  const POLL_MS = 2000, HEARTBEAT_MS = 15000, IDLE_FRESH_S = 15, CAPTURE_FRESH_S = 600;
  const RUNTIME = (new URLSearchParams(location.search)).get("root") || window.ALMITA_RUNTIME_ROOT || "/runtime";

  // ---------------------------------------------------------------- analysis (pure: tests call it without sound)
  function runningMedian(a, half) {
    const out = new Float64Array(a.length);
    for (let i = 0; i < a.length; i++) {
      const w = [];
      for (let j = Math.max(0, i - half); j <= Math.min(a.length - 1, i + half); j++) if (Number.isFinite(a[j])) w.push(a[j]);
      if (!w.length) { out[i] = NaN; continue; }
      w.sort((x, y) => x - y); out[i] = w[w.length >> 1];
    }
    return out;
  }
  function boxcar(a, half) {
    const out = new Float64Array(a.length);
    for (let i = 0; i < a.length; i++) {
      let s = 0, k = 0;
      for (let j = Math.max(0, i - half); j <= Math.min(a.length - 1, i + half); j++) if (Number.isFinite(a[j])) { s += a[j]; k++; }
      out[i] = k ? s / k : NaN;
    }
    return out;
  }
  function median(v) { const w = v.filter(Number.isFinite).sort((x, y) => x - y); return w.length ? w[w.length >> 1] : NaN; }

  // spectrum = {frequency_hz:[], power_db:[] (null = no data), inner:[] (optional)}; state = {} kept between calls
  function analyse(spectrum, state, centerHz) {
    const f = spectrum.frequency_hz, db = spectrum.power_db, n = f.length;
    const inner = Array.isArray(spectrum.inner) && spectrum.inner.length === n ? spectrum.inner : null;
    const c0 = Number.isFinite(centerHz) ? centerHz : (f[0] + f[n - 1]) / 2;
    const r = new Float64Array(n);
    for (let i = 0; i < n; i++) r[i] = (db[i] === null || db[i] === undefined || (inner && !inner[i])) ? NaN : Math.pow(10, db[i] / 10);
    const outer = []; for (let i = 0; i < n; i++) if (Math.abs(f[i] - c0) >= OUTER_FROM_HZ && Number.isFinite(r[i])) outer.push(r[i]);
    const g = median(outer.length > 20 ? outer : Array.from(r));
    if (!Number.isFinite(g) || g <= 0) return { found: false, reason: "no data" };
    const x = runningMedian(Array.from(r, (v) => v / g), 4);                   // narrow lines (<~10 kHz) shaved off
    if (!state.floor || state.floor.length !== n) { state.floor = Float64Array.from(x); state.spectra = 0; }
    const fl = state.floor;
    for (let i = 0; i < n; i++) if (Number.isFinite(x[i])) fl[i] = Number.isFinite(fl[i]) ? Math.min(fl[i] * (1 + FLOOR_RISE), x[i]) : x[i];
    state.spectra = (state.spectra || 0) + 1;
    const e = boxcar(Array.from(x, (v, i) => v / fl[i] - 1), 7);              // ~17 kHz smoothing
    const outerE = []; for (let i = 0; i < n; i++) if (Math.abs(f[i] - c0) >= OUTER_FROM_HZ) outerE.push(e[i]);
    const mOut = median(outerE);
    const sigma = 1.4826 * median(outerE.map((v) => Math.abs(v - mOut))) || 0.002;
    const thr = Math.max(3 * sigma, 0.003);
    let sw = 0, swf = 0, peak = 0; const pos = new Float64Array(n);
    for (let i = 0; i < n; i++) {
      if (Math.abs(f[i] - c0) > ZONE_HALF_HZ || !Number.isFinite(e[i])) continue;
      pos[i] = Math.max(0, e[i] - thr); sw += pos[i]; swf += pos[i] * f[i]; if (e[i] > peak) peak = e[i];
    }
    const out = { spectra: state.spectra, threshold: thr, peak: 0, meanExcess: 0, bands: new Array(N_BANDS).fill(0) };
    if (sw <= 0) return { ...out, found: false, reason: "no excess above the floor" };
    const fc = swf / sw;
    let acc = 0, lo = fc, hi = fc;
    for (let i = 0; i < n; i++) { const a0 = acc; acc += pos[i]; if (a0 < 0.05 * sw && acc >= 0.05 * sw) lo = f[i]; if (a0 < 0.95 * sw && acc >= 0.95 * sw) hi = f[i]; }
    const width = Math.min(MAX_WIDTH_HZ, Math.max(MIN_WIDTH_HZ, hi - lo));
    const w0 = fc - width / 2, w1 = fc + width / 2;
    const sums = new Array(N_BANDS).fill(0), counts = new Array(N_BANDS).fill(0);
    let sWin = 0, kWin = 0, pWin = 0;
    for (let i = 0; i < n; i++) {
      if (f[i] < w0 || f[i] >= w1 || !Number.isFinite(e[i])) continue;
      const b = Math.min(N_BANDS - 1, Math.floor((f[i] - w0) / width * N_BANDS));
      const v = Math.max(0, e[i]); sums[b] += v; counts[b]++; sWin += v; kWin++; if (e[i] > pWin) pWin = e[i];
    }
    return { ...out, found: true, centerHz: fc, widthHz: width, lowHz: w0, highHz: w1,
             velocityKmS: (1 - fc / HI_REST_HZ) * C_KM_S, peak: pWin, meanExcess: kWin ? sWin / kWin : 0,
             bands: sums.map((s, b) => (counts[b] ? s / counts[b] : 0)) };
  }

  // ---------------------------------------------------------------- sound
  let ctx = null, master = null, bandGains = [], tone = null, toneGain = null, noiseSrc = null;
  let timer = null, lastBeat = 0, lastStamp = null, state = {}, stateSource = null, onStatus = () => {};
  const bandFreq = (b) => AUDIO_LO * Math.pow(AUDIO_HI / AUDIO_LO, (b + 0.5) / N_BANDS);
  const BED = 0.012;                                                          // the quiet hiss that says AUDIO is on

  function build() {
    const AC = window.AudioContext || window.webkitAudioContext;
    if (!AC) throw new Error("this browser has no Web Audio");
    ctx = new AC();
    master = ctx.createGain(); master.gain.value = 0; master.connect(ctx.destination);
    const buf = ctx.createBuffer(1, ctx.sampleRate * 2, ctx.sampleRate), d = buf.getChannelData(0);
    for (let i = 0; i < d.length; i++) d[i] = Math.random() * 2 - 1;
    noiseSrc = ctx.createBufferSource(); noiseSrc.buffer = buf; noiseSrc.loop = true;
    bandGains = [];
    for (let b = 0; b < N_BANDS; b++) {
      const bp = ctx.createBiquadFilter(); bp.type = "bandpass"; bp.frequency.value = bandFreq(b); bp.Q.value = 6;
      const gn = ctx.createGain(); gn.gain.value = BED;
      noiseSrc.connect(bp); bp.connect(gn); gn.connect(master); bandGains.push(gn);
    }
    tone = ctx.createOscillator(); tone.type = "triangle"; tone.frequency.value = 440;
    toneGain = ctx.createGain(); toneGain.gain.value = 0; tone.connect(toneGain); toneGain.connect(master);
    noiseSrc.start(); tone.start();
    master.gain.setTargetAtTime(0.5, ctx.currentTime, 0.4);
  }

  function play(a) {
    if (!ctx) return;
    const t = ctx.currentTime, tau = 1.2;
    for (let b = 0; b < N_BANDS; b++) {
      const lvl = a.found ? Math.min(1, Math.sqrt(a.bands[b] / 0.05)) * 0.5 : 0;   // 5 % excess in a slice = full hiss
      bandGains[b].gain.setTargetAtTime(BED + lvl, t, tau);
    }
    if (a.found) {
      const hz = Math.min(1000, Math.max(180, 440 * Math.pow(2, -a.velocityKmS / 150)));
      tone.frequency.setTargetAtTime(hz, t, tau);
      toneGain.gain.setTargetAtTime(Math.min(0.18, a.peak / 0.2 * 0.18), t, tau);
    } else toneGain.gain.setTargetAtTime(0, t, tau);
  }

  async function getJson(url) {
    try { const r = await fetch(url, { cache: "no-store" }); return r.ok ? await r.json() : null; } catch (e) { return null; }
  }
  const ageS = (utc) => { const t = Date.parse(utc || ""); return Number.isFinite(t) ? (Date.now() - t) / 1000 : Infinity; };

  async function tick() {
    if (Date.now() - lastBeat > HEARTBEAT_MS) {
      lastBeat = Date.now();
      try { await window.AlmitaUI.api("/api/audio/state?listening=1"); } catch (e) { /* the next beat tries again */ }
    }
    let doc = null, source = null, note = "";
    const idle = await getJson(`${RUNTIME}/sdr_audio_latest.json`);
    if (idle && idle.status === "ok" && ageS(idle.updated_utc) < IDLE_FRESH_S && idle.spectrum) { doc = idle; source = "idle"; }
    else {
      const cap = await getJson(`${RUNTIME}/main_rfi_mask_latest.json`);
      if (cap && cap.spectrum && ageS(cap.updated_utc) < CAPTURE_FRESH_S && (!idle || idle.status === "sdr_busy" || ageS(idle.updated_utc) >= IDLE_FRESH_S)) {
        doc = cap; source = "capture";
      } else note = idle ? (idle.status === "sdr_busy" ? `SDR busy (${idle.busy}), no capture spectra` : idle.status === "failed" ? `idle reader: ${idle.error}` : "waiting for spectra") : "waiting for the idle reader";
    }
    if (!doc) { play({ found: false }); onStatus({ source: null, note }); return; }
    if (source !== stateSource) { state = {}; stateSource = source; lastStamp = null; }   // two pipelines, two floors
    const stamp = `${source}|${doc.updated_utc}|${doc.point_id || ""}`;
    if (stamp === lastStamp) return;
    lastStamp = stamp;
    const a = analyse(doc.spectrum, state, doc.center_frequency_hz);
    play(a);
    onStatus({ source, analysis: a });
  }

  async function start(statusCb) {
    if (ctx) return;
    onStatus = statusCb || (() => {});
    build();                                                                  // inside the click: browsers allow sound
    lastBeat = Date.now();
    const r = await window.AlmitaUI.api("/api/audio/start", { method: "POST", body: { confirm: true } });
    if (!r.ok) onStatus({ source: null, note: "idle reader not started: " + window.AlmitaUI.errorText(r.error) + " - capture spectra only" });
    await tick();
    timer = window.setInterval(() => { tick().catch((e) => console.error("AUDIO tick failed (isolated):", e)); }, POLL_MS);
  }

  async function stop() {
    if (timer) window.clearInterval(timer);
    timer = null;
    if (ctx) {
      const c = ctx; ctx = null;
      try { master.gain.setTargetAtTime(0, c.currentTime, 0.15); } catch (e) { /* closing anyway */ }
      window.setTimeout(() => { c.close().catch(() => {}); }, 600);
    }
    state = {}; stateSource = null; lastStamp = null;
    try { await window.AlmitaUI.api("/api/audio/stop", { method: "POST", body: { confirm: true } }); } catch (e) { /* the reader stops by itself within a minute */ }
  }

  window.AlmitaAudio = { start, stop, analyse, get running() { return !!ctx; }, N_BANDS };
})();
