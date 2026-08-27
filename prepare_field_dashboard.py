#!/usr/bin/env python3
"""Prepare an isolated static publication root without modifying sources."""
import argparse, json, os, shutil
from pathlib import Path

ROOT_FILES=("quicklook_live_status.json","latest_spectrum.json","latest_spectrum.png","latest_fractional_excess.png","latest_waterfall.json","latest_waterfall.png","quicklook_map.json","quicklook_map.png")
def prepare(dashboard,quicklook,telemetry,public):
    dashboard=Path(dashboard);quicklook=Path(quicklook);telemetry=Path(telemetry);public=Path(public)
    missing=[str(dashboard/x) for x in ("index.html","styles.css","app.js") if not (dashboard/x).is_file()]
    missing += [str(quicklook/x) for x in ROOT_FILES if not (quicklook/x).is_file()]
    if missing:raise ValueError("missing publication inputs: "+", ".join(missing))
    public.mkdir(parents=True,exist_ok=True)
    for name in ("index.html","styles.css","app.js"):
        tmp=public/(name+".tmp");shutil.copy2(dashboard/name,tmp);os.replace(tmp,public/name)
    link=public/"quicklook"
    if link.is_symlink() and link.resolve()!=quicklook.resolve():link.unlink()
    if not link.exists():link.symlink_to(quicklook.resolve(),target_is_directory=True)
    tele_dir=public/"telemetry";tele_dir.mkdir(exist_ok=True);tmp=tele_dir/"telemetry_summary.json.tmp";shutil.copy2(telemetry,tmp);os.replace(tmp,tele_dir/"telemetry_summary.json")
    config="window.ALMITA_QUICKLOOK_ROOT='/quicklook';window.ALMITA_TELEMETRY_ROOT='/telemetry';\n"
    (public/"config.js.tmp").write_text(config);os.replace(public/"config.js.tmp",public/"config.js")
    index=(public/"index.html").read_text();index=index.replace('<script src="app.js"></script>','<script src="config.js"></script><script src="app.js"></script>')
    (public/"index.html.tmp").write_text(index);os.replace(public/"index.html.tmp",public/"index.html")
    result={"status":"PASS","public_root":str(public),"quicklook_target":str(quicklook.resolve()),"telemetry_source":str(telemetry.resolve()),"root_files":list(ROOT_FILES)}
    (public/"publication_manifest.json").write_text(json.dumps(result,indent=2));return result
def main():
    p=argparse.ArgumentParser();p.add_argument("--dashboard-dist",default="dashboard/dist");p.add_argument("--quicklook-root",required=True);p.add_argument("--telemetry",required=True);p.add_argument("--public-root",default="data/field_web")
    a=p.parse_args();print(json.dumps(prepare(a.dashboard_dist,a.quicklook_root,a.telemetry,a.public_root),indent=2))
if __name__=="__main__":main()
