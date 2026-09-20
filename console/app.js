"use strict";
const PARAMETERS=new URLSearchParams(location.search);
const CONFIG={root:PARAMETERS.get("root")||window.ALMITA_RUNTIME_ROOT||"/runtime",pollMs:2000};
const $=id=>document.getElementById(id);
const safe=(v,fallback="—")=>v===null||v===undefined||v===""?fallback:v;
const num=(v,d=1)=>typeof v==="number"?v.toLocaleString(undefined,{maximumFractionDigits:d}):safe(v);
// Backend/watcher strings (errors, session names, paths) are inserted as TEXT: escape before they reach innerHTML.
const esc=v=>String(v===null||v===undefined?"":v).replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
const pair=(label,value)=>`<dt>${label}</dt><dd>${esc(safe(value))}</dd>`;
const badgeClass=v=>`badge status-${String(v).toLowerCase()}`;
let lastValid=null;

// Strips an ISO timestamp down to just its HH:MM:SS — drops the date, the
// "T" separator, sub-second decimals, and the timezone offset.
function _shortTime(iso){
  if(!iso)return"—";
  const match=/T(\d{2}:\d{2}:\d{2})/.exec(iso);
  return match?match[1]:iso;
}
function _hhmm(seconds){
  if(seconds==null||!isFinite(seconds))return"--:--";
  const total=Math.max(0,Math.round(seconds));
  const h=Math.floor(total/3600),m=Math.floor((total%3600)/60);
  return `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}`;
}
// Same defensive semantics as _hhmm (invalid -> "--:--:--", negative
// clamped to 0) plus seconds, and hours accumulate past 24 rather than
// wrapping (Math.floor(total/3600), never % 24) - a multi-hour session
// summary (e.g. GOTO total) reads as 27:15:04, not 03:15:04.
function _hhmmss(seconds){
  if(seconds==null||!isFinite(seconds))return"--:--:--";
  const total=Math.max(0,Math.round(seconds));
  const h=Math.floor(total/3600),m=Math.floor((total%3600)/60),s=total%60;
  return `${String(h).padStart(2,"0")}:${String(m).padStart(2,"0")}:${String(s).padStart(2,"0")}`;
}
// The session-summary block in capture.py's own compact console output
// (orchestrator_capture.log, tailed verbatim into activity_log.lines - see
// almita_console_watcher.py's build_activity_log) prints accumulated
// timing categories as plain text, e.g. "GOTO         866.1s". This
// augments those specific lines with an HH:MM:SS reading alongside the
// original seconds value (kept verbatim, per spec) - purely a display-side
// addition, no backend change, generic over any current or future label
// rather than hardcoded to just these four.
const _SUMMARY_TIMING_LINE_RE=/^([A-Za-z][A-Za-z ]*?)(\s+)(-?[\d.]+)s$/;
function _appendHmsToSummaryLine(line){
  const match=_SUMMARY_TIMING_LINE_RE.exec(line);
  if(!match)return line;
  const seconds=parseFloat(match[3]);
  if(!isFinite(seconds))return line;
  return `${line}   (${_hhmmss(seconds)})`;
}

// Every fetch is bounded: a stalled watcher/server must show as DISCONNECTED, not as an eternal spinner.
const FETCH_TIMEOUT_MS=8000;
async function fetchJson(name){
  const ctl=typeof AbortController==="function"?new AbortController():null;
  const timer=ctl?setTimeout(()=>ctl.abort(),FETCH_TIMEOUT_MS):null;
  try{
    const response=await fetch(`${CONFIG.root}/${name}`,{cache:"no-store",signal:ctl?ctl.signal:undefined});
    if(!response.ok)throw new Error(`${name}: HTTP ${response.status}`);
    return await response.json();
  }catch(error){
    if(error&&error.name==="AbortError")throw new Error(`${name}: no response within ${FETCH_TIMEOUT_MS/1000} s`);
    throw error;
  }finally{if(timer)clearTimeout(timer)}
}

// Collapses wifi's state + most relevant detail into one line, matching
// the other pairs' plain-text style (e.g. "DEGRADED — 12 SDIO errors",
// "FAILED — Broadcom SDIO backplane halted") rather than a second panel -
// this is deliberately the only new field this incident's monitoring adds
// to the console (see wifi_health.py for the full status this summarizes).
function _wifiValue(wifi){
  const state=wifi.state||"UNKNOWN";
  if(state==="OK")return"OK";
  if(wifi.last_error&&/backplane/i.test(wifi.last_error))return`${state} — Broadcom SDIO backplane halted`;
  const count=wifi.sdio_error_count;
  return count?`${state} — ${num(count,0)} SDIO errors`:state;
}

function renderInstrument(instrument,wifi){
  wifi=wifi||{};
  const badge=instrument.telemetry_stale?"DEGRADED":"READY";
  $("instrument-badge").textContent=badge;$("instrument-badge").className=badgeClass(badge);
  $("instrument-kv").innerHTML=[
    pair("CPU",instrument.cpu==null?"N/A":`${num(instrument.cpu)}%`),
    pair("RAM",instrument.ram==null?"N/A":`${num(instrument.ram)}%`),
    pair("DISK",instrument.disk==null?"N/A":`${num(instrument.disk)}%`),
    pair("RTL_TCP",instrument.rtl_tcp_process?"DETECTED":"NOT DETECTED"),
    pair("PORT 1234",instrument.rtl_tcp_listening===true?"LISTENING":instrument.rtl_tcp_listening===false?"NOT LISTENING":"UNKNOWN"),
    pair("SDR TEMP",instrument.sdr_temperature_c==null?"N/A":`${num(instrument.sdr_temperature_c)} °C`),
    pair("LNA TEMP",instrument.lna_temperature_c==null?"N/A":`${num(instrument.lna_temperature_c)} °C`),
    pair("MOUNT",instrument.mount_device?instrument.mount_device:safe(instrument.mount_state,"NOT_EXPOSED")),
    pair("TELEMETRY",instrument.telemetry_stale?"STALE":"LIVE"),
    pair("WI-FI",_wifiValue(wifi)),
  ].join("");
}

function renderSession(acquisition){
  const state=acquisition.state||"IDLE";
  $("session-badge").textContent=state;$("session-badge").className=badgeClass(state);
  if(state==="IDLE"){$("session-kv").innerHTML=pair("SESSION","IDLE — NO ACTIVE SESSION");return}
  // Elapsed/total/remaining: same "average time per completed point" estimator
  // capture.py's own compact console summary uses server-side, computed here
  // client-side from started_utc + progress so it updates every poll tick.
  const started=acquisition.started_utc?Date.parse(acquisition.started_utc):NaN;
  const pointCurrent=Number(acquisition.point_current),pointsTotal=Number(acquisition.points_total);
  let elapsedSec=null,totalSec=null,remainingSec=null;
  if(!isNaN(started)){
    elapsedSec=(Date.now()-started)/1000;
    if(pointCurrent>0&&pointsTotal>0){
      const perPoint=elapsedSec/pointCurrent;
      totalSec=perPoint*pointsTotal;
      remainingSec=Math.max(0,totalSec-elapsedSec);
    }
  }
  $("session-kv").innerHTML=[
    pair("SESSION ID",acquisition.session_id),
    pair("SESSION NAME",acquisition.session_name),
    pair("PROGRESS",`${safe(acquisition.point_current,"—")} / ${safe(acquisition.points_total,"—")}`),
    pair("CURRENT POINT",acquisition.current_point_id),
    pair("SETTLE / CAPTURE",
      `${acquisition.settle_seconds==null?"—":num(acquisition.settle_seconds,1)+"s"} / `+
      `${acquisition.capture_seconds==null?"—":num(acquisition.capture_seconds,1)+"s"}`),
    pair("ELAPSED / TOTAL / REMAINING",`${_hhmm(elapsedSec)} / ${_hhmm(totalSec)} / ${_hhmm(remainingSec)}`),
    pair("FAILED",acquisition.points_failed),
    pair("DEFERRED",acquisition.points_deferred),
    pair("LAST SUCCESS POINT",acquisition.last_successful_point_id),
    pair("STALE",acquisition.acquisition_stale?"YES":"NO"),
    pair("ERROR",acquisition.error),
  ].join("");
}

function renderThumbs(quicklook){
  const items=[["spectrum","latest_spectrum.png",quicklook.spectrum_available],
    // session_waterfall.png accumulates one row per point for the whole
    // session (see quicklook_session_waterfall.py); latest_waterfall.png
    // stays the per-point intra-capture product, kept for provenance but
    // no longer shown here.
    ["waterfall","session_waterfall.png",quicklook.waterfall_available],
    ["map","quicklook_map.png",quicklook.map_available],
    // OPTIONAL visual-only companion to the exact NATIVE_GRID map above -
    // never the science product itself.
    ["map-interpolated","quicklook_map_interpolated.png",quicklook.interpolated_map_available]];
  const version=encodeURIComponent(quicklook.last_product_utc||"unversioned");
  let any=false;
  for(const [id,file,available] of items){
    const img=$(`thumb-${id}`),link=$(`thumb-${id}-link`);
    if(available){
      const src=`${CONFIG.root}/quicklook_products/${file}?v=${version}`;
      img.src=src;link.href=src;img.hidden=false;any=true;
    }else{
      img.hidden=true;
    }
  }
  $("quicklook-thumbs").hidden=!any;
}

function renderQuicklook(quicklook){
  const state=quicklook.state||"IDLE";
  $("quicklook-badge").textContent=state;$("quicklook-badge").className=badgeClass(state);
  if(state==="IDLE"){$("quicklook-kv").innerHTML=pair("QUICKLOOK","NO ACTIVE SESSION");$("quicklook-thumbs").hidden=true;return}
  $("quicklook-kv").innerHTML=[
    pair("PROCESSED",quicklook.points_processed),
    pair("SPECTRUM",quicklook.spectrum_available?"AVAILABLE":"WAITING"),
    pair("WATERFALL",quicklook.waterfall_available?"AVAILABLE":"WAITING"),
    pair("MAP",quicklook.map_available?"AVAILABLE":"WAITING"),
    pair("STALE",quicklook.quicklook_stale?"YES":"NO"),
    pair("ERROR",quicklook.error),
  ].join("");
  renderThumbs(quicklook);
}

function renderRfiRef(rfiRef){
  const state=rfiRef.status||"DISABLED";
  $("rfi-ref-badge").textContent=state;$("rfi-ref-badge").className=badgeClass(state);
  if(state==="DISABLED"){$("rfi-ref-kv").innerHTML=pair("RFI REFERENCE","DISABLED");return}
  $("rfi-ref-kv").innerHTML=[
    pair("RECEIVER / SERIAL",rfiRef.device_serial?`V3 / ${rfiRef.device_serial}`:"—"),
    pair("GAIN",rfiRef.gain_db==null?"N/A":`${num(rfiRef.gain_db,1)} dB`),
    pair("FFT DUTY",rfiRef.fft_duty_fraction==null?"N/A":`${num(rfiRef.fft_duty_fraction*100,1)}%`),
    pair("OCCUPANCY",rfiRef.occupancy_fraction==null?"N/A":`${num(rfiRef.occupancy_fraction*100,2)}%`),
    pair("CLIPPING",rfiRef.clipping_fraction==null?"N/A":`${num(rfiRef.clipping_fraction*100,2)}%`),
    pair("PEAK",rfiRef.peak_dbfs==null?"N/A":`${num(rfiRef.peak_dbfs,1)} dBFS`),
    pair("PROCESSED / SKIPPED / DROPPED",`${safe(rfiRef.processed_blocks)} / ${safe(rfiRef.skipped_blocks)} / ${safe(rfiRef.dropped_blocks)}`),
    pair("LAST UPDATE",_shortTime(rfiRef.last_update_utc)),
    pair("ERROR",rfiRef.last_error),
  ].join("");
}

// Axis styling shared by drawRfiSpectrum/drawRfiWaterfall, matched to
// Antenna A's matplotlib-rendered PNGs (light background, thin frame,
// small tick labels) so both antennas read the same way at a glance.
// Two margin sets: the small THUMBNAIL (320x140, no title/axis-label text -
// there is no room) and the enlarged DETAILED view (title + "Frequency
// (MHz)"-style axis labels, same as Antenna A's matplotlib PNGs carry).
const AXIS_MARGIN={l:36,r:8,t:6,b:16};
const AXIS_MARGIN_DETAILED={l:70,r:20,t:44,b:48};
const AXIS_BG="#f7f7f7",AXIS_LINE="#94a3b8",AXIS_GRID="#d9dee3",AXIS_TEXT="#374151";

function _niceStep(span,targetCount){
  const raw=span/Math.max(1,targetCount);
  const mag=Math.pow(10,Math.floor(Math.log10(raw||1)));
  const norm=raw/mag;
  return (norm<1.5?1:norm<3?2:norm<7?5:10)*mag;
}
function _axisTicks(min,max,targetCount){
  if(!(max>min))return[min];
  const step=_niceStep(max-min,targetCount)||1;
  const start=Math.ceil(min/step)*step;
  const ticks=[];
  for(let v=start;v<=max+step*1e-6;v+=step)ticks.push(Math.abs(v)<step*1e-9?0:v);
  return ticks;
}
function _plotArea(w,h,detailed){
  const m=detailed?AXIS_MARGIN_DETAILED:AXIS_MARGIN;
  return{x0:m.l,x1:w-m.r,y0:m.t,y1:h-m.b};
}
function _drawFrameAndBg(ctx,w,h,detailed){
  ctx.clearRect(0,0,w,h);
  ctx.fillStyle=AXIS_BG;ctx.fillRect(0,0,w,h);
  const a=_plotArea(w,h,detailed);
  ctx.strokeStyle=AXIS_LINE;ctx.lineWidth=1;
  ctx.strokeRect(a.x0+.5,a.y0+.5,a.x1-a.x0,a.y1-a.y0);
  return a;
}
// title/xLabel/yLabel are only drawn when detailed=true (the enlarged
// view) - matching Antenna A's matplotlib title/xlabel/ylabel, which the
// tiny 320x140 thumbnail has no room for and never carried either.
function _drawTitleAndLabels(ctx,w,h,a,detailed,title,xLabel,yLabel){
  if(!detailed)return;
  ctx.fillStyle=AXIS_TEXT;ctx.textAlign="center";ctx.textBaseline="alphabetic";
  if(title){ctx.font="700 18px ui-monospace,monospace";ctx.fillText(title,w/2,28)}
  if(xLabel){ctx.font="13px ui-monospace,monospace";ctx.fillText(xLabel,(a.x0+a.x1)/2,h-10)}
  if(yLabel){
    ctx.save();ctx.translate(16,(a.y0+a.y1)/2);ctx.rotate(-Math.PI/2);
    ctx.font="13px ui-monospace,monospace";ctx.fillText(yLabel,0,0);ctx.restore();
  }
}
function _drawYAxis(ctx,a,yMin,yMax,fmt,detailed){
  ctx.font=detailed?"11px ui-monospace,monospace":"9px ui-monospace,monospace";
  ctx.fillStyle=AXIS_TEXT;ctx.textAlign="right";ctx.textBaseline="middle";
  for(const v of _axisTicks(yMin,yMax,detailed?6:4)){
    const py=a.y1-(v-yMin)/(yMax-yMin)*(a.y1-a.y0);
    if(py<a.y0-1||py>a.y1+1)continue;
    ctx.strokeStyle=AXIS_GRID;ctx.beginPath();ctx.moveTo(a.x0,py);ctx.lineTo(a.x1,py);ctx.stroke();
    ctx.fillText(fmt(v),a.x0-4,py);
  }
}
function _drawXAxis(ctx,a,xMin,xMax,fmt,detailed){
  ctx.font=detailed?"11px ui-monospace,monospace":"9px ui-monospace,monospace";
  ctx.fillStyle=AXIS_TEXT;ctx.textAlign="center";ctx.textBaseline="top";
  for(const v of _axisTicks(xMin,xMax,detailed?6:4)){
    const px=a.x0+(v-xMin)/(xMax-xMin)*(a.x1-a.x0);
    if(px<a.x0-1||px>a.x1+1)continue;
    ctx.strokeStyle=AXIS_GRID;ctx.beginPath();ctx.moveTo(px,a.y0);ctx.lineTo(px,a.y1);ctx.stroke();
    ctx.fillText(fmt(v),px,a.y1+2);
  }
}

// Y range is deliberately wider than the live data's own min/max: at 1x
// (auto-scaled to the exact sample range) normal receiver noise fills the
// whole plot height and looks like solid static. Expanding the range
// around the data's midpoint compresses the noise floor into a thinner
// band so a real peak — which sits outside the noise's typical spread —
// stands out by contrast instead of being lost in it.
const SPECTRUM_Y_EXPAND=5.0;

function drawRfiSpectrum(canvas,freqMHz,powerDbfs,options){
  const detailed=!!(options&&options.detailed);
  const ctx=canvas.getContext("2d"),w=canvas.width,h=canvas.height;
  const a=_drawFrameAndBg(ctx,w,h,detailed);
  _drawTitleAndLabels(ctx,w,h,a,detailed,options&&options.title,"Frequency (MHz)","Power (dBFS)");
  if(!freqMHz.length)return;
  const minP=Math.min(...powerDbfs),maxP=Math.max(...powerDbfs);
  const midP=(minP+maxP)/2,halfSpan=Math.max((maxP-minP)/2,0.5)*SPECTRUM_Y_EXPAND;
  const yMin=midP-halfSpan,yMax=midP+halfSpan;
  const f0=freqMHz[0],f1=freqMHz[freqMHz.length-1];
  _drawYAxis(ctx,a,yMin,yMax,v=>v.toFixed(0),detailed);
  _drawXAxis(ctx,a,f0,f1,v=>v.toFixed(1),detailed);
  const x=v=>a.x0+(v-f0)/(f1-f0||1)*(a.x1-a.x0),y=v=>a.y1-(v-yMin)/(yMax-yMin)*(a.y1-a.y0);
  ctx.strokeStyle="#2563a8";ctx.lineWidth=detailed?1.6:1.1;ctx.beginPath();
  freqMHz.forEach((f,idx)=>{const px=x(f),py=y(powerDbfs[idx]);idx===0?ctx.moveTo(px,py):ctx.lineTo(px,py)});
  ctx.stroke();
}

// Viridis approximation (5-anchor linear interpolation - matplotlib's own
// default colormap, which Antenna A's quicklook_waterfall.py uses via
// cmap="viridis") - no external colormap library, just the same
// low->high color story so both antennas' waterfalls read the same way.
const VIRIDIS_ANCHORS=[[68,1,84],[59,82,139],[33,145,140],[94,201,98],[253,231,37]];
function _viridis(t){
  t=Math.max(0,Math.min(1,t));
  const n=VIRIDIS_ANCHORS.length-1,pos=t*n,i=Math.min(n-1,Math.floor(pos)),frac=pos-i;
  const a=VIRIDIS_ANCHORS[i],b=VIRIDIS_ANCHORS[i+1];
  return[Math.round(a[0]+(b[0]-a[0])*frac),Math.round(a[1]+(b[1]-a[1])*frac),Math.round(a[2]+(b[2]-a[2])*frac)];
}

// Rows are chronological oldest-first (top) to newest-last (bottom),
// matching "time flows downward" - same convention regardless of size.
function drawRfiWaterfall(canvas,rows,freqMHz,options){
  const detailed=!!(options&&options.detailed);
  const ctx=canvas.getContext("2d"),w=canvas.width,h=canvas.height;
  const a=_drawFrameAndBg(ctx,w,h,detailed);
  _drawTitleAndLabels(ctx,w,h,a,detailed,options&&options.title,"Frequency (MHz)","Scan order (old → new)");
  if(!rows.length||!freqMHz.length)return;
  let minP=Infinity,maxP=-Infinity;
  for(const row of rows)for(const p of row.power_dbfs){if(p<minP)minP=p;if(p>maxP)maxP=p}
  const spanP=(maxP-minP)||1;
  const f0=freqMHz[0],f1=freqMHz[freqMHz.length-1];
  const pw=a.x1-a.x0,ph=a.y1-a.y0;
  const image=ctx.createImageData(pw,ph);
  const nRows=rows.length,nBins=freqMHz.length;
  for(let y=0;y<ph;y++){
    const power=rows[Math.min(nRows-1,Math.floor(y/ph*nRows))].power_dbfs;
    for(let x=0;x<pw;x++){
      const t=Math.max(0,Math.min(1,(power[Math.min(nBins-1,Math.floor(x/pw*nBins))]-minP)/spanP));
      const [r,g,b]=_viridis(t);
      const i=(y*pw+x)*4;
      image.data[i]=r;image.data[i+1]=g;image.data[i+2]=b;image.data[i+3]=255;
    }
  }
  ctx.putImageData(image,a.x0,a.y0);
  ctx.strokeStyle=AXIS_LINE;ctx.lineWidth=1;ctx.strokeRect(a.x0+.5,a.y0+.5,pw,ph);
  _drawXAxis(ctx,a,f0,f1,v=>v.toFixed(1),detailed);
  ctx.font=detailed?"11px ui-monospace,monospace":"9px ui-monospace,monospace";
  ctx.fillStyle=AXIS_TEXT;
  ctx.textAlign="right";ctx.textBaseline="middle";
  ctx.fillText("new",a.x0-4,a.y0+6);
  ctx.fillText("old",a.x0-4,a.y1-6);
}

// RFI SPECTRUM/WATERFALL have no server-rendered PNG at all (unlike
// Antenna A's own spectrum/waterfall, or Antenna B's own occupancy map
// below): they are drawn only client-side, on a small thumbnail canvas,
// from JSON - a deliberate choice (rfi_monitor.py's own docstring: FFT
// work stays off the asyncio loop and the Pi never runs a second renderer
// for this). So "click -> open the full-size image" for these two can't
// point at a PNG that doesn't exist; it re-renders the SAME already-drawn
// data onto a larger offscreen canvas with the exact same drawing function
// (no new chart logic, no science/data change) and exports that canvas as
// a PNG data URL via the browser's own Canvas API - never a second FFT,
// never a Pi-side image renderer, never the JSON as the click target.
//
// Dimensions measured from Antenna A's own REAL, currently-linked
// products (not guessed, and re-checked against what renderThumbs()
// above actually links to, not just any script with a similar name):
// thumb-spectrum-link -> latest_spectrum.png, a copy of
// spectrum/quicklook_spectrum.json's own PNG (quicklook_spectrum.py,
// figsize=(12,6) @ dpi=150 -> 1800x900); thumb-waterfall-link ->
// session_waterfall.png (quicklook_session_waterfall.py, figsize=(12,6)
// @ dpi=150 -> 1800x900, NOT the older/unlinked quicklook_waterfall.py's
// figsize=(12,7) product, which is not what the UI actually shows).
// Antenna B's enlarged view uses those exact sizes so both antennas' full
// -size views are the same size class, not a mismatched guess (a former
// 1000x450 constant shared by both products is gone).
const RFI_SPECTRUM_FULL_W=1800,RFI_SPECTRUM_FULL_H=900;
const RFI_WATERFALL_FULL_W=1800,RFI_WATERFALL_FULL_H=900;
function _fullSizeCanvasImageUrl(drawFn,fullW,fullH,options,...drawArgs){
  const canvas=document.createElement("canvas");
  canvas.width=fullW;canvas.height=fullH;
  drawFn(canvas,...drawArgs,options);
  return canvas.toDataURL("image/png");
}

async function renderRfiProducts(rfiRef,quicklook,sessionId){
  const state=rfiRef.status||"DISABLED";
  const freqLabel=rfiRef.center_frequency_hz==null?"—":`${num(rfiRef.center_frequency_hz/1e6,3)} MHz`;
  const gainLabel=rfiRef.gain_db==null?"—":`${num(rfiRef.gain_db,1)} dB`;
  $("rfi-caption").innerHTML=
    `<b>ANTENNA B / RFI REF</b> — RTL-SDR V3 — ${esc(freqLabel)} — gain ${esc(gainLabel)} — `+
    `updated ${esc(safe(rfiRef.spectrum_updated_utc||rfiRef.waterfall_updated_utc||rfiRef.last_update_utc))}`;

  const thumbs=$("rfi-thumbs"),placeholder=$("rfi-products-placeholder");
  const specCanvas=$("rfi-spectrum-canvas"),wfCanvas=$("rfi-waterfall-canvas"),mapImg=$("rfi-map-thumb");
  if(state==="DISABLED"||state==="FAILED"||state==="UNAVAILABLE"){
    thumbs.hidden=true;placeholder.hidden=false;
    placeholder.textContent=state==="DISABLED"?"DISABLED":`${state} — ${safe(rfiRef.last_error,"")}`;
    return;
  }
  const anyAvailable=rfiRef.spectrum_available||rfiRef.session_waterfall_available||(quicklook&&quicklook.rfi_occupancy_map_available);
  if(!anyAvailable){
    thumbs.hidden=true;placeholder.hidden=false;placeholder.textContent="WAITING FOR RFI PRODUCTS…";
    return;
  }
  thumbs.hidden=false;placeholder.hidden=true;

  if(rfiRef.spectrum_available){
    try{
      const data=await fetchJson("rfi_ref_spectrum.json");
      // Defense in depth: the watcher already rejects a session mismatch,
      // but an old product must never be drawn as if it belongs to this
      // session even if fetched a moment before the watcher's next tick.
      if(data.session_id===sessionId&&Array.isArray(data.frequency_hz)&&data.frequency_hz.length){
        const freqMHz=data.frequency_hz.map(f=>f/1e6);
        drawRfiSpectrum(specCanvas,freqMHz,data.power_dbfs);
        $("rfi-spectrum-link").href=_fullSizeCanvasImageUrl(
          drawRfiSpectrum,RFI_SPECTRUM_FULL_W,RFI_SPECTRUM_FULL_H,
          {detailed:true,title:"ALMITA — RFI REF — Spectrum"},freqMHz,data.power_dbfs);
      }else{
        specCanvas.getContext("2d").clearRect(0,0,specCanvas.width,specCanvas.height);
      }
    }catch(error){specCanvas.getContext("2d").clearRect(0,0,specCanvas.width,specCanvas.height)}
  }else{
    specCanvas.getContext("2d").clearRect(0,0,specCanvas.width,specCanvas.height);
  }

  // session_waterfall (not the live ~2s waterfall) so the panel spans the
  // whole session instead of only the last few minutes - see
  // rfi_monitor.py's _append_and_write_session_waterfall().
  if(rfiRef.session_waterfall_available){
    try{
      const data=await fetchJson("rfi_ref_session_waterfall.json");
      if(data.session_id===sessionId&&Array.isArray(data.rows)&&data.rows.length){
        // Found during this pass's own visual QA: data.frequency_hz is
        // raw Hz (the JSON's own field name says so) but was being passed
        // straight into a parameter named freqMHz and labeled "Frequency
        // (MHz)" - the small 320x140 thumbnail never had room to show the
        // resulting wrong tick values legibly, but the enlarged detailed
        // view (which finally draws the axis label text) made the
        // mismatch obvious. drawRfiSpectrum already did this conversion
        // correctly a few lines above; this brings the waterfall in line
        // with it - a display-only fix, no data/science change.
        const freqMHz=data.frequency_hz.map(f=>f/1e6);
        drawRfiWaterfall(wfCanvas,data.rows,freqMHz);
        $("rfi-waterfall-link").href=_fullSizeCanvasImageUrl(
          drawRfiWaterfall,RFI_WATERFALL_FULL_W,RFI_WATERFALL_FULL_H,
          {detailed:true,title:"ALMITA — RFI REF — Waterfall"},data.rows,freqMHz);
      }else{
        wfCanvas.getContext("2d").clearRect(0,0,wfCanvas.width,wfCanvas.height);
      }
    }catch(error){wfCanvas.getContext("2d").clearRect(0,0,wfCanvas.width,wfCanvas.height)}
  }else{
    wfCanvas.getContext("2d").clearRect(0,0,wfCanvas.width,wfCanvas.height);
  }

  // RFI OCCUPANCY MAP: server-rendered PNG in the same quicklook product
  // family as Antenna A's own map (reuses the existing runtime symlink and
  // session/staleness gate already applied to `quicklook` above) - a
  // distinct file, never confused with Antenna A's quicklook_map.png.
  if(quicklook&&quicklook.rfi_occupancy_map_available){
    const version=encodeURIComponent(quicklook.last_product_utc||"unversioned");
    const src=`${CONFIG.root}/quicklook_products/rfi_occupancy_map.png?v=${version}`;
    mapImg.src=src;$("rfi-map-link").href=src;mapImg.hidden=false;
  }else{
    mapImg.hidden=true;
  }
}

function _escapeHtml(text){
  return text.replace(/&/g,"&amp;").replace(/</g,"&lt;").replace(/>/g,"&gt;");
}
function _logLineClass(line){
  if(line.startsWith("✓"))return"log-ok";
  if(/FAIL|❌|ERROR/.test(line))return"log-error";
  if(line.startsWith("POINT "))return"log-point";
  if(line.startsWith("SESSION "))return"log-session";
  return"";
}
function renderActivityLog(activityLog){
  const panel=$("activity-log-panel"),pre=$("activity-log-lines");
  const lines=(activityLog&&activityLog.available&&activityLog.lines)||[];
  panel.hidden=lines.length===0;
  if(!lines.length)return;
  pre.innerHTML=lines.map(line=>{
    const cls=_logLineClass(line);
    const display=_appendHmsToSummaryLine(line);
    return `<span${cls?` class="${cls}"`:""}>${_escapeHtml(display)}</span>`;
  }).join("\n");
  pre.scrollTop=pre.scrollHeight;
}

function renderLastSession(lastSession){
  $("last-session").hidden=!lastSession;
  if(!lastSession)return;
  $("last-session-kv").innerHTML=[
    pair("SESSION",lastSession.session_name),
    pair("SESSION ID",lastSession.session_id),
    pair("FINAL STATE",lastSession.final_state),
    pair("COMPLETED",lastSession.completed_utc),
    pair("SUCCESS / TOTAL",`${safe(lastSession.points_success)} / ${safe(lastSession.points_total)}`),
  ].join("");
}

// LIVE / STALE / DISCONNECTED of THIS page's data link: LIVE = the last poll worked AND the status file is fresh;
// STALE = polls work but the watcher stopped updating the file; DISCONNECTED = polls fail.
const LINK={failures:0,lastOkAt:0,statusUpdatedMs:NaN,connectedOnce:false};
function linkState(now=Date.now()){
  if(LINK.failures>=2)return"DISCONNECTED";
  if(!LINK.connectedOnce)return"CONNECTING";
  const age=Number.isFinite(LINK.statusUpdatedMs)?now-LINK.statusUpdatedMs:Infinity;
  return age>3*CONFIG.pollMs+4000?"STALE":"LIVE";
}
function renderLink(){
  const el=$("link-state");if(!el)return;
  const state=linkState();
  el.textContent=state;el.className=badgeClass(state);
}

function render(status){
  lastValid=status;$("loading").hidden=true;$("app").hidden=false;$("connection").hidden=true;
  LINK.statusUpdatedMs=Date.parse(status.updated_utc);renderLink();
  const systemState=status.system_state||"READY";
  $("system-badge").textContent=systemState;$("system-badge").className=badgeClass(systemState);
  $("updated").textContent=_shortTime(status.updated_utc);
  renderInstrument(status.instrument||{},status.wifi||{});
  renderSession(status.acquisition||{state:"IDLE"});
  const quicklook=status.quicklook||{state:"IDLE"};
  renderQuicklook(quicklook);
  const rfiRef=status.rfi_ref||{status:"DISABLED"};
  renderRfiRef(rfiRef);
  renderRfiProducts(rfiRef,quicklook,(status.acquisition||{}).session_id||null);
  renderActivityLog(status.activity_log);
  if(window.SpectralStack3D)window.SpectralStack3D.update((status.acquisition||{}).session_id||null,(status.acquisition||{}).state);
  renderLastSession(status.last_session);
}

let _pollInFlight=false;
async function poll(){
  if(_pollInFlight)return;                     // never two overlapping polls (slow backend)
  _pollInFlight=true;
  try{render(await fetchJson("almita_status.json"));LINK.failures=0;LINK.lastOkAt=Date.now();LINK.connectedOnce=true;renderLink()}
  catch(error){
    LINK.failures+=1;renderLink();
    $("connection").hidden=false;$("connection").textContent=`DATA CONNECTION DEGRADED — ${error.message}`;
    if(!lastValid)$("loading").textContent="DATA CONNECTION DEGRADED — waiting for console watcher";
  }finally{_pollInFlight=false}
}

function syncJson(name){
  const request=new XMLHttpRequest();request.open("GET",`${CONFIG.root}/${name}`,false);request.send();
  return request.status===200?JSON.parse(request.responseText):null;
}

function start(){
  if(PARAMETERS.get("snapshot")==="1"){const status=syncJson("almita_status.json");if(status)render(status);return}
  poll();
  // One poll timer; no polling while the tab is hidden (it resumes immediately on return).
  setInterval(()=>{if(!document.hidden)poll()},CONFIG.pollMs);
  document.addEventListener("visibilitychange",()=>{if(!document.hidden)poll()});
  setInterval(()=>{$("clock").textContent=new Date().toISOString().replace("T"," ").slice(0,19)+"Z";renderLink()},1000);
  if(window.AlmitaUI&&AlmitaUI.mountFooter)AlmitaUI.mountFooter();
}
window.AlmitaConsole={renderInstrument,renderSession,renderQuicklook,renderRfiRef,renderRfiProducts,
  drawRfiSpectrum,drawRfiWaterfall,renderActivityLog,renderLastSession,render,CONFIG,linkState,LINK};
start();
