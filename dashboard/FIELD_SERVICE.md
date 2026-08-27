# Field Service + Telemetry V1

No systemd service is installed and no auto-start is configured.

```sh
./.venv/bin/python telemetry_summary.py --once --output data/dashboard/field/telemetry_summary.json
./.venv/bin/python dashboard/build_dashboard.py
./.venv/bin/python prepare_field_dashboard.py \
  --quicklook-root data/quicklook/QUICKLOOK-LIVE-V1-20260827T015000Z \
  --telemetry data/dashboard/field/telemetry_summary.json \
  --public-root data/field_web
./.venv/bin/python serve_dashboard.py --root data/field_web --bind 127.0.0.1 --port 8088
```

Open `http://127.0.0.1:8088/`. Stop with Ctrl-C. Binding beyond localhost is an explicit later field decision.

The server accepts GET/HEAD only. It provides no controls, uploads, API, hardware access, mount access, or SDR stream connection.

## Offline operation

- Internet is not required at runtime.
- There are no CDNs, external fonts, cloud APIs, npm/pip runtime installs, or remote assets.
- Field Astropy entry points call the shared `astropy_offline.py` policy, which sets `iers.conf.auto_download = False` before coordinate work.
- Localhost/LAN static service and local instrument services remain permitted when explicitly configured.

**NO SYSTEMD INSTALLED. NO AUTO-START CONFIGURED.**
