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
    ["waterfall","latest_waterfall.png",quicklook.waterfall_available],
    ["map","quicklook_map.png",quicklook.map_available]];
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

function drawRfiSpectrum(canvas,freqMHz,powerDbfs){
  const ctx=canvas.getContext("2d"),w=canvas.width,h=canvas.height;
  ctx.clearRect(0,0,w,h);
  if(!freqMHz.length)return;
  const pad=6;
  const minP=Math.min(...powerDbfs),maxP=Math.max(...powerDbfs),spanP=(maxP-minP)||1;
  const f0=freqMHz[0],f1=freqMHz[freqMHz.length-1],spanF=(f1-f0)||1;
  const x=v=>pad+(v-f0)/spanF*(w-2*pad),y=v=>h-pad-(v-minP)/spanP*(h-2*pad);
  ctx.strokeStyle="#65b7d8";ctx.lineWidth=1.3;ctx.beginPath();
  freqMHz.forEach((f,idx)=>{const px=x(f),py=y(powerDbfs[idx]);idx===0?ctx.moveTo(px,py):ctx.lineTo(px,py)});
  ctx.stroke();
}

async function renderRfiSpectrum(rfiRef,sessionId){
  const state=rfiRef.status||"DISABLED";
  const placeholder=$("rfi-spectrum-placeholder"),canvas=$("rfi-spectrum-canvas");
  const freqLabel=rfiRef.center_frequency_hz==null?"—":`${num(rfiRef.center_frequency_hz/1e6,3)} MHz`;
  const gainLabel=rfiRef.gain_db==null?"—":`${num(rfiRef.gain_db,1)} dB`;
  $("rfi-spectrum-caption").innerHTML=
    `<b>ANTENNA B / RFI REF</b> — RTL-SDR V3 — ${freqLabel} — gain ${gainLabel} — `+
    `updated ${safe(rfiRef.spectrum_updated_utc||rfiRef.last_update_utc)}`;
  const waiting=()=>{canvas.hidden=true;placeholder.hidden=false;placeholder.textContent="WAITING FOR SPECTRUM…"};
  if(!rfiRef.spectrum_available){
    canvas.hidden=true;placeholder.hidden=false;
    placeholder.textContent=
      state==="DISABLED"?"DISABLED":
      state==="FAILED"?`FAILED — ${safe(rfiRef.last_error,"")}`:
      state==="UNAVAILABLE"?`UNAVAILABLE — ${safe(rfiRef.last_error,"")}`:
      "WAITING FOR SPECTRUM…";
    return;
  }
  try{
    const data=await fetchJson("rfi_ref_spectrum.json");
    // Defense in depth: the watcher already rejects a session mismatch, but
    // an old spectrum file must never be drawn as if it belongs to this
    // session even if fetched a moment before the watcher's next tick.
    if(data.session_id!==sessionId||!Array.isArray(data.frequency_hz)||!data.frequency_hz.length){waiting();return}
    canvas.hidden=false;placeholder.hidden=true;
    drawRfiSpectrum(canvas,data.frequency_hz.map(f=>f/1e6),data.power_dbfs);
  }catch(error){
    waiting();
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
  renderQuicklook(status.quicklook||{state:"IDLE"});
  const rfiRef=status.rfi_ref||{status:"DISABLED"};
  renderRfiRef(rfiRef);
  renderRfiSpectrum(rfiRef,(status.acquisition||{}).session_id||null);
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
window.AlmitaConsole={renderInstrument,renderSession,renderQuicklook,renderRfiRef,renderRfiSpectrum,drawRfiSpectrum,renderLastSession,render,CONFIG};
start();
