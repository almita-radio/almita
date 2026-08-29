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
    pair("MOUNT",safe(instrument.mount_state,"NOT_EXPOSED")),
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

function renderQuicklook(quicklook){
  const state=quicklook.state||"IDLE";
  $("quicklook-badge").textContent=state;$("quicklook-badge").className=badgeClass(state);
  if(state==="IDLE"){$("quicklook-kv").innerHTML=pair("QUICKLOOK","NO ACTIVE SESSION");return}
  $("quicklook-kv").innerHTML=[
    pair("PROCESSED",quicklook.points_processed),
    pair("SPECTRUM",quicklook.spectrum_available?"AVAILABLE":"WAITING"),
    pair("WATERFALL",quicklook.waterfall_available?"AVAILABLE":"WAITING"),
    pair("MAP",quicklook.map_available?"AVAILABLE":"WAITING"),
    pair("STALE",quicklook.quicklook_stale?"YES":"NO"),
    pair("ERROR",quicklook.error),
  ].join("");
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
window.AlmitaConsole={renderInstrument,renderSession,renderQuicklook,renderLastSession,render,CONFIG};
start();
