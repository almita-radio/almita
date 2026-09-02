"use strict";
const PARAMETERS=new URLSearchParams(location.search);
const CONFIG={root:PARAMETERS.get("root")||window.ALMITA_RUNTIME_ROOT||"/runtime",pollMs:2000};
const $=id=>document.getElementById(id);
const safe=(v,fallback="—")=>v===null||v===undefined||v===""?fallback:v;
const num=(v,d=1)=>typeof v==="number"?v.toLocaleString(undefined,{maximumFractionDigits:d}):safe(v);
const pair=(label,value)=>`<dt>${label}</dt><dd>${safe(value)}</dd>`;
const badgeClass=v=>`badge status-${String(v).toLowerCase()}`;
let lastValid=null;

async function fetchJson(name){
  const response=await fetch(`${CONFIG.root}/${name}`,{cache:"no-store"});
  if(!response.ok)throw new Error(`${name}: HTTP ${response.status}`);
  return response.json();
}

function renderInstrument(instrument){
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
  ].join("");
}

function renderSession(acquisition){
  const state=acquisition.state||"IDLE";
  $("session-badge").textContent=state;$("session-badge").className=badgeClass(state);
  if(state==="IDLE"){$("session-kv").innerHTML=pair("SESSION","IDLE — NO ACTIVE SESSION");return}
  $("session-kv").innerHTML=[
    pair("SESSION ID",acquisition.session_id),
    pair("SESSION NAME",acquisition.session_name),
    pair("PROGRESS",`${safe(acquisition.point_current,"—")} / ${safe(acquisition.points_total,"—")}`),
    pair("CURRENT POINT",acquisition.current_point_id),
    pair("SUCCESS",acquisition.points_success),
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
    pair("LAST UPDATE",rfiRef.last_update_utc),
    pair("ERROR",rfiRef.last_error),
  ].join("");
}

// Axis styling shared by drawRfiSpectrum/drawRfiWaterfall, matched to
// Antenna A's matplotlib-rendered PNGs (light background, thin frame,
// small tick labels) so both antennas read the same way at a glance.
const AXIS_MARGIN={l:36,r:8,t:6,b:16};
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
function _plotArea(w,h){
  return{x0:AXIS_MARGIN.l,x1:w-AXIS_MARGIN.r,y0:AXIS_MARGIN.t,y1:h-AXIS_MARGIN.b};
}
function _drawFrameAndBg(ctx,w,h){
  ctx.clearRect(0,0,w,h);
  ctx.fillStyle=AXIS_BG;ctx.fillRect(0,0,w,h);
  const a=_plotArea(w,h);
  ctx.strokeStyle=AXIS_LINE;ctx.lineWidth=1;
  ctx.strokeRect(a.x0+.5,a.y0+.5,a.x1-a.x0,a.y1-a.y0);
  return a;
}
function _drawYAxis(ctx,a,yMin,yMax,fmt){
  ctx.font="9px ui-monospace,monospace";ctx.fillStyle=AXIS_TEXT;
  ctx.textAlign="right";ctx.textBaseline="middle";
  for(const v of _axisTicks(yMin,yMax,4)){
    const py=a.y1-(v-yMin)/(yMax-yMin)*(a.y1-a.y0);
    if(py<a.y0-1||py>a.y1+1)continue;
    ctx.strokeStyle=AXIS_GRID;ctx.beginPath();ctx.moveTo(a.x0,py);ctx.lineTo(a.x1,py);ctx.stroke();
    ctx.fillText(fmt(v),a.x0-4,py);
  }
}
function _drawXAxis(ctx,a,xMin,xMax,fmt){
  ctx.font="9px ui-monospace,monospace";ctx.fillStyle=AXIS_TEXT;
  ctx.textAlign="center";ctx.textBaseline="top";
  for(const v of _axisTicks(xMin,xMax,4)){
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

function drawRfiSpectrum(canvas,freqMHz,powerDbfs){
  const ctx=canvas.getContext("2d"),w=canvas.width,h=canvas.height;
  const a=_drawFrameAndBg(ctx,w,h);
  if(!freqMHz.length)return;
  const minP=Math.min(...powerDbfs),maxP=Math.max(...powerDbfs);
  const midP=(minP+maxP)/2,halfSpan=Math.max((maxP-minP)/2,0.5)*SPECTRUM_Y_EXPAND;
  const yMin=midP-halfSpan,yMax=midP+halfSpan;
  const f0=freqMHz[0],f1=freqMHz[freqMHz.length-1];
  _drawYAxis(ctx,a,yMin,yMax,v=>v.toFixed(0));
  _drawXAxis(ctx,a,f0,f1,v=>v.toFixed(1));
  const x=v=>a.x0+(v-f0)/(f1-f0||1)*(a.x1-a.x0),y=v=>a.y1-(v-yMin)/(yMax-yMin)*(a.y1-a.y0);
  ctx.strokeStyle="#2563a8";ctx.lineWidth=1.1;ctx.beginPath();
  freqMHz.forEach((f,idx)=>{const px=x(f),py=y(powerDbfs[idx]);idx===0?ctx.moveTo(px,py):ctx.lineTo(px,py)});
  ctx.stroke();
}

// Simple blue (low) -> yellow (mid) -> red (high) heatmap, no external
// colormap dependency. Rows are chronological oldest-first (top) to
// newest-last (bottom), matching "time flows downward".
function drawRfiWaterfall(canvas,rows,freqMHz){
  const ctx=canvas.getContext("2d"),w=canvas.width,h=canvas.height;
  const a=_drawFrameAndBg(ctx,w,h);
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
      const i=(y*pw+x)*4;
      image.data[i]=Math.round(255*Math.min(1,t*2));
      image.data[i+1]=Math.round(255*Math.min(1,Math.max(0,1-Math.abs(t-.5)*2)));
      image.data[i+2]=Math.round(255*Math.min(1,(1-t)*2));
      image.data[i+3]=255;
    }
  }
  ctx.putImageData(image,a.x0,a.y0);
  ctx.strokeStyle=AXIS_LINE;ctx.lineWidth=1;ctx.strokeRect(a.x0+.5,a.y0+.5,pw,ph);
  _drawXAxis(ctx,a,f0,f1,v=>v.toFixed(1));
  ctx.font="9px ui-monospace,monospace";ctx.fillStyle=AXIS_TEXT;
  ctx.textAlign="right";ctx.textBaseline="middle";
  ctx.fillText("new",a.x0-4,a.y0+6);
  ctx.fillText("old",a.x0-4,a.y1-6);
}

async function renderRfiProducts(rfiRef,quicklook,sessionId){
  const state=rfiRef.status||"DISABLED";
  const freqLabel=rfiRef.center_frequency_hz==null?"—":`${num(rfiRef.center_frequency_hz/1e6,3)} MHz`;
  const gainLabel=rfiRef.gain_db==null?"—":`${num(rfiRef.gain_db,1)} dB`;
  $("rfi-caption").innerHTML=
    `<b>ANTENNA B / RFI REF</b> — RTL-SDR V3 — ${freqLabel} — gain ${gainLabel} — `+
    `updated ${safe(rfiRef.spectrum_updated_utc||rfiRef.waterfall_updated_utc||rfiRef.last_update_utc)}`;

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
        drawRfiSpectrum(specCanvas,data.frequency_hz.map(f=>f/1e6),data.power_dbfs);
        $("rfi-spectrum-link").href=`${CONFIG.root}/rfi_ref_spectrum.json`;
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
        drawRfiWaterfall(wfCanvas,data.rows,data.frequency_hz);
        $("rfi-waterfall-link").href=`${CONFIG.root}/rfi_ref_session_waterfall.json`;
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

function render(status){
  lastValid=status;$("loading").hidden=true;$("app").hidden=false;$("connection").hidden=true;
  const systemState=status.system_state||"READY";
  $("system-badge").textContent=systemState;$("system-badge").className=badgeClass(systemState);
  $("updated").textContent=safe(status.updated_utc);
  renderInstrument(status.instrument||{});
  renderSession(status.acquisition||{state:"IDLE"});
  const quicklook=status.quicklook||{state:"IDLE"};
  renderQuicklook(quicklook);
  const rfiRef=status.rfi_ref||{status:"DISABLED"};
  renderRfiRef(rfiRef);
  renderRfiProducts(rfiRef,quicklook,(status.acquisition||{}).session_id||null);
  renderLastSession(status.last_session);
}

async function poll(){
  try{render(await fetchJson("almita_status.json"))}
  catch(error){
    $("connection").hidden=false;$("connection").textContent=`DATA CONNECTION DEGRADED — ${error.message}`;
    if(!lastValid)$("loading").textContent="DATA CONNECTION DEGRADED — waiting for console watcher";
  }
}

function syncJson(name){
  const request=new XMLHttpRequest();request.open("GET",`${CONFIG.root}/${name}`,false);request.send();
  return request.status===200?JSON.parse(request.responseText):null;
}

function start(){
  if(PARAMETERS.get("snapshot")==="1"){const status=syncJson("almita_status.json");if(status)render(status);return}
  poll();setInterval(poll,CONFIG.pollMs);
  setInterval(()=>{$("clock").textContent=new Date().toISOString().replace("T"," ").slice(0,19)+"Z"},1000);
}
window.AlmitaConsole={renderInstrument,renderSession,renderQuicklook,renderRfiRef,renderRfiProducts,
  drawRfiSpectrum,drawRfiWaterfall,renderLastSession,render,CONFIG};
start();
