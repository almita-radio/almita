#!/usr/bin/env python3
"""Read-only ALMITA host telemetry producer using Python stdlib and /proc."""
from __future__ import annotations
import argparse, json, os, shutil, socket, time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SDR_SENSOR="28-082471f4e41b";LNA_SENSOR="28-2c5acd1e64ff"

def utcnow():return datetime.now(timezone.utc).isoformat()
def parse_load(text):
    parts=text.split()
    if len(parts)<3:raise ValueError("invalid loadavg")
    return tuple(float(x) for x in parts[:3])
def parse_meminfo(text):
    values={}
    for line in text.splitlines():
        if ":" in line:
            key,value=line.split(":",1);parts=value.split();values[key]=int(parts[0])*1024
    total=values.get("MemTotal");available=values.get("MemAvailable")
    if total is None or available is None:raise ValueError("MemTotal/MemAvailable unavailable")
    used=total-available
    return {"memory_total_bytes":total,"memory_used_bytes":used,"memory_available_bytes":available,
            "memory_percent":used/total*100 if total else None}
def parse_cpu_line(line):
    parts=line.split()
    if not parts or parts[0]!="cpu":raise ValueError("invalid aggregate cpu line")
    values=[int(v) for v in parts[1:]];idle=values[3]+(values[4] if len(values)>4 else 0)
    return sum(values),idle
def cpu_percent(stat_path=Path("/proc/stat"),interval=.05):
    first=parse_cpu_line(stat_path.read_text().splitlines()[0]);time.sleep(interval)
    second=parse_cpu_line(stat_path.read_text().splitlines()[0]);total=second[0]-first[0];idle=second[1]-first[1]
    return None if total<=0 else max(0.,min(100.,(total-idle)/total*100))
def parse_network(text):
    result={}
    for line in text.splitlines()[2:]:
        if ":" not in line:continue
        name,data=line.split(":",1);parts=data.split()
        if len(parts)>=16:result[name.strip()]={"rx_bytes":int(parts[0]),"tx_bytes":int(parts[8])}
    return result
def select_network(items):
    candidates=[(name,v) for name,v in items.items() if name!="lo"] or list(items.items())
    if not candidates:return {"interface":None,"rx_bytes":None,"tx_bytes":None}
    name,value=max(candidates,key=lambda x:x[1]["rx_bytes"]+x[1]["tx_bytes"])
    return {"interface":name,**value}
def read_w1(sensor,root=Path("/sys/bus/w1/devices")):
    candidates=[root/sensor/"temperature",root/sensor/"w1_slave"]
    for path in candidates:
        try:
            text=path.read_text().strip()
            raw=text if path.name=="temperature" else text.rsplit("t=",1)[1]
            return {"value_c":int(raw)/1000,"status":"OK","path":str(path)}
        except (OSError,ValueError,IndexError):continue
    return {"value_c":None,"status":"NOT_AVAILABLE","path":None}
def read_temperatures(root=Path("/sys/bus/w1/devices")):
    with ThreadPoolExecutor(max_workers=2) as pool:
        sdr_future=pool.submit(read_w1,SDR_SENSOR,root);lna_future=pool.submit(read_w1,LNA_SENSOR,root)
        return sdr_future.result(),lna_future.result()
def find_process(name="rtl_tcp",proc=Path("/proc")):
    found=[]
    for entry in proc.iterdir():
        if not entry.name.isdigit():continue
        try:cmd=(entry/"cmdline").read_bytes().replace(b"\0",b" ").decode(errors="replace").strip()
        except OSError:continue
        executable=Path(cmd.split()[0]).name if cmd else ""
        if executable==name:found.append({"pid":int(entry.name),"cmdline":cmd[:300]})
    return found
def parse_listening_port(text,port=1234,address="127.0.0.1"):
    wanted_port=f"{port:04X}";wanted_addr="0100007F" if address=="127.0.0.1" else None
    for line in text.splitlines()[1:]:
        parts=line.split()
        if len(parts)<4:continue
        try:addr,p=parts[1].split(":")
        except ValueError:continue
        if p.upper()==wanted_port and parts[3]=="0A" and (wanted_addr is None or addr.upper()==wanted_addr):return True
    return False
def atomic_json(path,value):
    path=Path(path);path.parent.mkdir(parents=True,exist_ok=True);tmp=path.with_name(path.name+".tmp")
    with tmp.open("w") as stream:json.dump(value,stream,indent=2);stream.flush();os.fsync(stream.fileno())
    os.replace(tmp,path)
def collect(workspace=Path("/home/stellarmate/almita"),proc=Path("/proc"),w1=Path("/sys/bus/w1/devices")):
    started=time.perf_counter();warnings=[]
    try:load=parse_load((proc/"loadavg").read_text())
    except Exception as e:load=(None,None,None);warnings.append(f"load unavailable: {e}")
    try:memory=parse_meminfo((proc/"meminfo").read_text())
    except Exception as e:memory={k:None for k in ("memory_total_bytes","memory_used_bytes","memory_available_bytes","memory_percent")};warnings.append(f"memory unavailable: {e}")
    try:cpu=cpu_percent(proc/"stat")
    except Exception as e:cpu=None;warnings.append(f"cpu unavailable: {e}")
    try:uptime=float((proc/"uptime").read_text().split()[0])
    except Exception as e:uptime=None;warnings.append(f"uptime unavailable: {e}")
    disk=shutil.disk_usage(workspace);network=select_network(parse_network((proc/"net/dev").read_text()))
    sdr_temp,lna_temp=read_temperatures(w1)
    processes=find_process(proc=proc)
    try:listening=parse_listening_port((proc/"net/tcp").read_text()) or parse_listening_port((proc/"net/tcp6").read_text(),address="::1")
    except OSError:listening=None;warnings.append("tcp table unavailable")
    if sdr_temp["status"]!="OK":warnings.append("SDR temperature NOT_AVAILABLE")
    if lna_temp["status"]!="OK":warnings.append("LNA temperature NOT_AVAILABLE")
    if not processes:warnings.append("rtl_tcp process not detected")
    if listening is False:warnings.append("rtl_tcp port 127.0.0.1:1234 not listening")
    value={"schema_version":"1.0","status":"DEGRADED" if warnings else "OK","created_utc":utcnow(),
      "hostname":socket.gethostname(),"uptime_seconds":uptime,
      "system":{"cpu_percent":cpu,"load_1m":load[0],"load_5m":load[1],"load_15m":load[2],**memory},
      "storage":{"filesystem":str(workspace),"total_bytes":disk.total,"used_bytes":disk.used,"available_bytes":disk.free,"percent":disk.used/disk.total*100},
      "network":network,
      "sdr":{"rtl_tcp_process_detected":bool(processes),"rtl_tcp_processes":processes,"rtl_tcp_port":1234,
        "rtl_tcp_port_listening":listening,"expected_frequency_hz":1420405752,"expected_sample_rate_hz":2400000,
        "expected_gain_db":40.2,"bias_tee_expected":True,"observed_frequency_hz":None,"observed_sample_rate_hz":None,
        "observed_gain_db":None,"observed_bias_tee":None},
      "temperatures":{"sdr_c":sdr_temp["value_c"],"sdr_status":sdr_temp["status"],"lna_c":lna_temp["value_c"],"lna_status":lna_temp["status"]},
      "mount":{"status":"NOT_EXPOSED","tracking":None,"ra":None,"dec":None},
      "limitations":["read-only local snapshot","network values are cumulative counters, not rates",
        "SDR runtime configuration is not observed because rtl_tcp stream is never connected","mount connection intentionally not opened"],
      "warnings":warnings,"generation_seconds":time.perf_counter()-started}
    return value
def main():
    p=argparse.ArgumentParser();p.add_argument("--output",default="telemetry_summary.json");p.add_argument("--once",action="store_true",default=True);p.add_argument("--watch",action="store_true");p.add_argument("--interval",type=float,default=2)
    args=p.parse_args()
    while True:
        value=collect();atomic_json(Path(args.output),value);print(json.dumps({"status":value["status"],"output":args.output,"generation_seconds":value["generation_seconds"]}))
        if not args.watch:break
        time.sleep(max(.2,args.interval))
if __name__=="__main__":main()
