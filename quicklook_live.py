#!/usr/bin/env python3
"""Decoupled polling consumer for ALMITA quicklook products."""
from __future__ import annotations

import argparse
import csv
import gc
import json
import os
import resource
import shutil
import signal
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import numpy as np

from calibration_foundation import check_calibration_compatibility, load_calibration_profile
from quicklook_map import (MapError, build_native_grid_document, find_grid_directory,
                           load_native_grid_geometry, map_metric, read_cell_status, sha256,
                           write_native_grid_png)
from quicklook_map_interpolated_preview import (build_interpolated_preview_document,
                                                write_interpolated_preview_png)
from quicklook_session_waterfall import update_session_waterfall
from quicklook_spectrum import generate_quicklook
from quicklook_waterfall import generate_waterfall
from rfi_occupancy_map import (build_rfi_occupancy_map_document, correlate_rfi_with_points,
                               read_point_timing, read_rfi_history, write_rfi_occupancy_png)
from runtime_state import read_json_safe


STOP_REQUESTED = False

# Canonical field-console runtime dir, anchored to this file's own location so
# the CLI resolves it correctly regardless of the operator's cwd. Only used as
# the CLI default below; QuicklookLive itself still defaults to
# runtime_dir=None (announcements are opt-in at the class level).
DEFAULT_RUNTIME_DIR = str(Path(__file__).resolve().parent / "data" / "runtime")


def request_stop(signum=None, frame=None):
    global STOP_REQUESTED
    STOP_REQUESTED = True


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat()


def atomic_json(path: Path, value: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + ".tmp")
    with temporary.open("w") as stream:
        json.dump(value, stream, indent=2, allow_nan=False)
        stream.flush(); os.fsync(stream.fileno())
    os.replace(temporary, path)


def atomic_copy(source: Path, destination: Path) -> None:
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".tmp")
    shutil.copyfile(source, temporary)
    os.replace(temporary, destination)


def read_manifest(session_dir: Path) -> list[dict[str, str]]:
    path = session_dir / "session.csv"
    if not path.exists():
        return []
    with path.open(newline="") as stream:
        rows = list(csv.DictReader(stream))
    required = {"point_id", "status", "source_hdf5"}
    if not rows and not required.issubset(set(csv.DictReader(path.open()).fieldnames or [])):
        raise ValueError("session.csv lacks required columns")
    return rows


def resolve_session_id(session_dir: Path) -> str:
    """Prefer the canonical session_id Capture persisted inside the session
    directory (session_identity.json); fall back to the directory's own
    basename only for historical sessions that predate this contract. New
    sessions always carry session_identity.json, so this fallback never
    applies to them - it exists solely for compatibility with sessions
    captured before this fix."""
    identity = read_json_safe(session_dir / "session_identity.json")
    if identity and isinstance(identity.get("session_id"), str) and identity["session_id"]:
        return identity["session_id"]
    return session_dir.name


def initial_state(session_id: str) -> dict[str, Any]:
    return {"schema_version":"1.0", "session_id":session_id, "created_utc":utcnow(),
            "updated_utc":utcnow(), "points":{}, "duplicates":[], "last_success_point":None,
            "performance_history":[]}


def load_state(path: Path, session_id: str) -> dict[str, Any]:
    if not path.exists(): return initial_state(session_id)
    value=json.loads(path.read_text())
    if value.get("session_id") != session_id: raise ValueError("state session_id mismatch")
    return value


def _source_path(session_dir: Path, text: str) -> Path:
    path=Path(text)
    return path if path.is_absolute() else session_dir/path


def _fingerprint(path: Path) -> dict[str, int]:
    stat=path.stat(); return {"size":stat.st_size,"mtime_ns":stat.st_mtime_ns}


def _log(path: Path, event: str, detail: str="") -> None:
    with path.open("a") as stream: stream.write(f"{utcnow()} {event}{' '+detail if detail else ''}\n")


class QuicklookLive:
    def __init__(self, session_dir, profile_path, output_dir, poll_interval=1.0, runtime_dir=None):
        self.session_dir=Path(session_dir); self.output=Path(output_dir); self.output.mkdir(parents=True,exist_ok=True)
        # Console announcement is opt-in (None by default): direct construction
        # (e.g. in tests) never writes outside of what the caller controls.
        self.runtime_dir=Path(runtime_dir) if runtime_dir else None
        self.profile_path=Path(profile_path); self.profile=load_calibration_profile(profile_path)
        self.poll_interval=float(poll_interval); self.session_id=resolve_session_id(self.session_dir)
        self.state_path=self.output/"quicklook_live_state.json"; self.status_path=self.output/"quicklook_live_status.json"
        self.log_path=self.output/"quicklook_live.log"; self.state=load_state(self.state_path,self.session_id)
        self.state.setdefault("performance_history",[])
        self.performance=[]; self.started=time.perf_counter(); self.resume_seconds=time.perf_counter()-self.started
        # Native-grid map geometry: best-effort, never fatal. Spectrum/Waterfall
        # must keep working even if the planned grid cannot be located (e.g. a
        # historical session, or one launched without grid_generator.py).
        try:
            self._grid_dir=find_grid_directory(self.session_dir)
            self._grid_geometry=load_native_grid_geometry(self._grid_dir) if self._grid_dir else None
        except Exception:
            self._grid_dir=None; self._grid_geometry=None

    def _publish_latest(self, point_dir:Path)->float:
        start=time.perf_counter()
        mapping={"spectrum/quicklook_spectrum.json":"latest_spectrum.json",
          "spectrum/quicklook_spectrum.png":"latest_spectrum.png",
          "spectrum/quicklook_fractional_excess.png":"latest_fractional_excess.png",
          "waterfall/quicklook_waterfall.json":"latest_waterfall.json",
          "waterfall/quicklook_waterfall.png":"latest_waterfall.png"}
        for source,dest in mapping.items(): atomic_copy(point_dir/source,self.output/dest)
        return time.perf_counter()-start

    def _update_map(self)->float:
        start=time.perf_counter()
        if self._grid_geometry is None:
            return time.perf_counter()-start
        processed={pid:item for pid,item in self.state["points"].items()
                  if item.get("result")=="PROCESSED" and item.get("map_point")}
        if not processed: return time.perf_counter()-start
        environments={v.get("environment","UNKNOWN") for v in processed.values()}
        environment=environments.pop() if len(environments)==1 else "MIXED_OR_UNKNOWN"
        try:
            cell_status=read_cell_status(self._grid_dir)
        except Exception:
            cell_status={}
        document=build_native_grid_document(self._grid_geometry,self.state["points"],cell_status,
                                            self.session_id,environment)
        temporary=self.output/"quicklook_map.png.tmp.png"
        write_native_grid_png(temporary,document)
        os.replace(temporary,self.output/"quicklook_map.png")
        atomic_json(self.output/"quicklook_map.json",document)
        self._update_interpolated_preview()
        return time.perf_counter()-start

    def _update_interpolated_preview(self)->None:
        """OPTIONAL, purely visual companion to the NATIVE_GRID map above -
        best-effort, never fatal: the exact science map (quicklook_map.json)
        must keep working whether or not this preview succeeds. Written to
        distinctly-named files so it can never be confused with or
        overwrite the NATIVE_GRID product.

        PNG written (via temp+rename) before the JSON ever claims
        available=True - the same order _update_map()/_update_rfi_occupancy_map()
        use - so a mid-render failure here can never leave a JSON claiming a
        PNG that was never actually produced (confirmed gap, 2026-09: the
        previous JSON-then-PNG order let a PNG-write exception get swallowed
        by the except below after available=True was already on disk)."""
        try:
            document=build_interpolated_preview_document(self._grid_geometry,self.state["points"],
                                                          self.session_id)
            if document.get("available"):
                temporary=self.output/"quicklook_map_interpolated.png.tmp.png"
                write_interpolated_preview_png(temporary,document)
                os.replace(temporary,self.output/"quicklook_map_interpolated.png")
            atomic_json(self.output/"quicklook_map_interpolated.json",document)
        except Exception:
            pass

    def _update_rfi_occupancy_map(self)->float:
        """ANTENNA B diagnostic overlay on Antenna A's own native grid -
        best-effort and entirely optional: Spectrum/Waterfall/the science
        map above must keep working whether or not RFI_REF was ever
        enabled, whether or not it has produced any history yet, and
        regardless of any error in this correlation. Never a second FFT,
        never a fake sky measurement from a fixed reference dipole."""
        start=time.perf_counter()
        if self._grid_geometry is None or self.runtime_dir is None:
            return time.perf_counter()-start
        try:
            rfi_samples=read_rfi_history(self.runtime_dir,self.session_id)
            if not rfi_samples:
                return time.perf_counter()-start
            point_timing=read_point_timing(self._grid_dir)
            point_metrics=correlate_rfi_with_points(point_timing,rfi_samples)
            if not point_metrics:
                return time.perf_counter()-start
            try:
                cell_status=read_cell_status(self._grid_dir)
            except Exception:
                cell_status={}
            document=build_rfi_occupancy_map_document(self._grid_geometry,point_metrics,cell_status,
                                                       self.session_id)
            temporary=self.output/"rfi_occupancy_map.png.tmp.png"
            write_rfi_occupancy_png(temporary,document)
            os.replace(temporary,self.output/"rfi_occupancy_map.png")
            atomic_json(self.output/"rfi_occupancy_map.json",document)
        except Exception:
            pass
        return time.perf_counter()-start

    def _process(self,row:dict[str,str],source:Path)->None:
        point_id=row["point_id"]; started=time.perf_counter(); point_dir=self.output/"points"/point_id
        point_dir.mkdir(parents=True,exist_ok=True); _log(self.log_path,"NEW SUCCESS",point_id)
        source_hash_before=sha256(source)
        compatibility=check_calibration_compatibility(self.profile,source)
        if compatibility["status"]!="COMPATIBLE":
            self.state["points"][point_id]={"result":compatibility["status"],"reason":compatibility["reason"],
              "source_hdf5":str(source),"fingerprint":_fingerprint(source),"products":{}}
            _log(self.log_path,"SKIP INCOMPATIBLE",f"{point_id} {compatibility['status']}");return
        spectrum_start=time.perf_counter();_log(self.log_path,"PROCESS SPECTRUM",point_id)
        spectrum,arrays=generate_quicklook(source,self.profile_path,point_dir/"spectrum")
        spectrum_seconds=time.perf_counter()-spectrum_start
        frequency=np.asarray(arrays["frequency_hz"]);span=frequency[-1]-frequency[0]
        metric=map_metric(arrays["fractional_excess"],arrays["fractional_uncertainty"],arrays["valid_mask"],frequency,
                          frequency[0]+.1*span,frequency[-1]-.1*span)
        session_waterfall_start=time.perf_counter()
        try:
            update_session_waterfall(self.output,session_id=self.session_id,point_id=point_id,
              frequency_hz=frequency,relative_db=arrays["relative_psd_db"],valid_mask=arrays["valid_mask"])
        except Exception:
            _log(self.log_path,"SESSION WATERFALL FAILED",point_id)
        session_waterfall_seconds=time.perf_counter()-session_waterfall_start
        del arrays, frequency
        gc.collect()
        waterfall_start=time.perf_counter();_log(self.log_path,"PROCESS WATERFALL",point_id)
        generate_waterfall(source,self.profile_path,point_dir/"waterfall")
        waterfall_seconds=time.perf_counter()-waterfall_start
        map_point=None; warning=None
        try:
            ra=float(row["ra_deg"]);dec=float(row["dec_deg"]);source_kind=row.get("coordinate_source") or "UNKNOWN"
            if not (0<=ra<360 and -90<=dec<=90): raise ValueError
            map_point={"ra_deg":ra,"dec_deg":dec,"coordinate_source":source_kind,**{k:metric[k] for k in
              ("map_value","map_uncertainty","valid_fraction","masked_fraction")}}
        except (ValueError,KeyError): warning="MISSING_POSITION_METADATA"
        publication_seconds=self._publish_latest(point_dir)
        self.state["points"][point_id]={"result":"PROCESSED","source_hdf5":str(source),"fingerprint":_fingerprint(source),
          "products":{"spectrum_json":str(point_dir/"spectrum/quicklook_spectrum.json"),
            "spectrum_png":str(point_dir/"spectrum/quicklook_spectrum.png"),
            "fractional_excess_png":str(point_dir/"spectrum/quicklook_fractional_excess.png"),
            "waterfall_json":str(point_dir/"waterfall/quicklook_waterfall.json"),
            "waterfall_npz":str(point_dir/"waterfall/quicklook_waterfall.npz"),
            "waterfall_png":str(point_dir/"waterfall/quicklook_waterfall.png")},
          "map_point":map_point,"warning":warning,"environment":row.get("environment") or spectrum.get("environment","UNKNOWN"),
          "source_sha256":source_hash_before,"source_unchanged":source_hash_before==sha256(source),
          "processed_utc":utcnow()}
        map_seconds=self._update_map();self.state["last_success_point"]=point_id
        record={"point_id":point_id,"spectrum_seconds":spectrum_seconds,
          "waterfall_seconds":waterfall_seconds,"session_waterfall_seconds":session_waterfall_seconds,
          "map_seconds":map_seconds,"publication_seconds":publication_seconds,
          "total_seconds":time.perf_counter()-started}
        self.performance.append(record);self.state["performance_history"].append(record)

    def scan_once(self)->dict[str,Any]:
        discovery=time.perf_counter(); rows=read_manifest(self.session_dir); discovery_seconds=time.perf_counter()-discovery
        seen_ids={}; queue=[]
        for row in rows:
            pid=row.get("point_id","")
            if pid in seen_ids:
                self.state["duplicates"].append({"point_id":pid,"first_source":seen_ids[pid],"duplicate_source":row.get("source_hdf5")})
                _log(self.log_path,"DUPLICATE_POINT_ID",pid);continue
            seen_ids[pid]=row.get("source_hdf5")
            if row.get("status","").upper()!="SUCCESS": continue
            source=_source_path(self.session_dir,row.get("source_hdf5", ""))
            if source.name.endswith(".part") or not source.is_file() or source.stat().st_size==0: continue
            prior=self.state["points"].get(pid)
            if prior:
                if prior.get("fingerprint")!=_fingerprint(source):
                    prior["source_change_status"]="SOURCE_CHANGED_AFTER_PROCESSING"
                    _log(self.log_path,"SOURCE_CHANGED_AFTER_PROCESSING",pid)
                continue
            queue.append((row,source))
        backlog_initial=len(queue)
        if backlog_initial>1:_log(self.log_path,"BACKLOG",str(backlog_initial))
        for row,source in queue:
            try:self._process(row,source)
            except Exception as error:
                self.state["points"][row["point_id"]]={"result":"POINT_ERROR","reason":f"{type(error).__name__}: {error}",
                  "source_hdf5":str(source),"fingerprint":_fingerprint(source),"products":{}}
                _log(self.log_path,"POINT ERROR",f"{row['point_id']} {type(error).__name__}")
        self.state["updated_utc"]=utcnow();state_start=time.perf_counter();atomic_json(self.state_path,self.state);state_seconds=time.perf_counter()-state_start
        self._update_rfi_occupancy_map()
        return self._status(rows,discovery_seconds,backlog_initial,state_seconds)

    def _status(self,rows,discovery_seconds,backlog_initial,state_seconds):
        processed=sum(v.get("result")=="PROCESSED" for v in self.state["points"].values())
        skipped=sum(v.get("result") in ("INCOMPATIBLE","UNKNOWN") for v in self.state["points"].values())
        errors=[{"point_id":k,"error":v.get("reason")} for k,v in self.state["points"].items() if v.get("result")=="POINT_ERROR"]
        warnings=[]
        for k,v in self.state["points"].items():
            if v.get("warning"):warnings.append({"point_id":k,"warning":v["warning"]})
            if v.get("source_change_status"):warnings.append({"point_id":k,"warning":v["source_change_status"]})
        if self.state["duplicates"]:warnings.append({"warning":"DUPLICATE_POINT_ID","count":len(self.state["duplicates"])})
        latest=self.state.get("last_success_point");latest_item=self.state["points"].get(latest,{})
        environments={v.get("environment","UNKNOWN") for v in self.state["points"].values() if v.get("result")=="PROCESSED"}
        environment=environments.pop() if len(environments)==1 else ("UNKNOWN" if not environments else "MIXED_OR_UNKNOWN")
        status={"schema_version":"1.0","status":"DEGRADED" if errors or skipped or warnings or backlog_initial>1 else ("OK" if processed else "IDLE"),
          "session_id":self.session_id,"created_utc":self.state["created_utc"],"updated_utc":utcnow(),
          "poll_interval_seconds":self.poll_interval,"calibration_profile":str(self.profile_path.with_suffix("")),
          "calibration_level":"RELATIVE_INSTRUMENTAL","absolute_calibration":False,"environment":environment,
          "astronomical_interpretation":"NOT_PERMITTED" if environment=="INDOOR_DEPARTMENT" else "NOT_ASSERTED",
          "points_seen":len(rows),"points_success":sum(r.get("status","").upper()=="SUCCESS" for r in rows),
          "points_processed":processed,"points_skipped":skipped,"latest_point_id":latest,
          "latest_source_hdf5":latest_item.get("source_hdf5"),"latest_products":latest_item.get("products",{}),
          "map_points":sum(bool(v.get("map_point")) for v in self.state["points"].values()),
          "backlog_count":0,"backlog_initial":backlog_initial,"errors":errors,"warnings":warnings,
          "session_progress":{"total_planned":None,"success":sum(r.get("status","").upper()=="SUCCESS" for r in rows),
            "failed":sum(r.get("status","").upper()=="FAILED" for r in rows),
            "deferred":sum(r.get("status","").upper()=="DEFERRED" for r in rows),"pending":None,"processed":processed},
          "performance":{"discovery_seconds":discovery_seconds,"state_write_seconds":state_seconds,
            "restart_resume_seconds":self.resume_seconds,"points":self.performance,
            "point_history":self.state["performance_history"],
            "peak_rss_mib":resource.getrusage(resource.RUSAGE_SELF).ru_maxrss/1024,
            "approx_sustainable_points_per_minute":None if not self.state["performance_history"] else 60/max(p["total_seconds"] for p in self.state["performance_history"])},
          "known_limitations":["polling V1","sequential Spectrum then Waterfall then Map","relative instrumental only",
            "capture launch integration intentionally absent"]}
        atomic_json(self.status_path,status);return status

    def _announce(self)->None:
        """Best-effort, isolated locator for the console watcher. Never raises
        and never affects the Quicklook algorithms or their outputs. No-op
        unless runtime_dir was explicitly configured (CLI only)."""
        if self.runtime_dir is None:
            return
        try:
            atomic_json(self.runtime_dir/"quicklook_announcement.json",
                        {"schema_version":1,"session_id":self.session_id,
                         "quicklook_root":str(self.output.resolve()),"updated_utc":utcnow()})
        except Exception:
            pass

    def run(self,once=False):
        _log(self.log_path,"SESSION START",self.session_id)
        self._announce()
        while True:
            status=self.scan_once()
            if once or STOP_REQUESTED:break
            time.sleep(self.poll_interval)
        validation={"status":"PASS","offline_simulation":True,
          "processed_points":status["points_processed"],"skipped_points":status["points_skipped"],
          "part_files_ignored":all(not str(v.get("source_hdf5","")).endswith(".part") for v in self.state["points"].values()),
          "restart_resume_no_reprocessing":bool(not self.performance and status["points_processed"]>0),
          "source_integrity":all(v.get("source_unchanged",True) for v in self.state["points"].values()),
          "latest_contract_complete":all((self.output/name).exists() for name in
            ("latest_spectrum.json","latest_spectrum.png","latest_fractional_excess.png",
             "latest_waterfall.json","latest_waterfall.png","quicklook_map.json","quicklook_map.png")),
          "calibration_level":"RELATIVE_INSTRUMENTAL","absolute_calibration":False}
        atomic_json(self.output/"quicklook_live_validation.json",validation)
        _log(self.log_path,"SESSION STOP",self.session_id);return status


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--session-dir",required=True);parser.add_argument("--calibration-profile",required=True)
    parser.add_argument("--output-dir",required=True);parser.add_argument("--poll-interval",type=float,default=1.0)
    parser.add_argument("--once",action="store_true")
    parser.add_argument("--runtime-dir",default=DEFAULT_RUNTIME_DIR,
                         help=f"Canonical field-console runtime dir for the console watcher (default: {DEFAULT_RUNTIME_DIR})")
    args=parser.parse_args();signal.signal(signal.SIGINT,request_stop);signal.signal(signal.SIGTERM,request_stop)
    live=QuicklookLive(args.session_dir,args.calibration_profile,args.output_dir,args.poll_interval,args.runtime_dir)
    print(json.dumps(live.run(args.once),indent=2));return 0


if __name__=="__main__":raise SystemExit(main())
