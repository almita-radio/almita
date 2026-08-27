# ALMITA Dashboard V1

Static, read-only presentation of Quicklook Live root derivatives. It never reads HDF5 or performs scientific processing.

Serve the repository root for local validation:

```sh
./.venv/bin/python -m http.server 8765
```

Then open:

`http://127.0.0.1:8765/dashboard/?root=/data/quicklook/QUICKLOOK-LIVE-V1-20260827T015000Z`

The datasource can also be supplied as `window.ALMITA_QUICKLOOK_ROOT` before `app.js`. Polling defaults to two seconds and product PNGs are versioned only by `updated_utc`.

`snapshot=1` is an offline Chromium evidence mode that loads the same static derivatives synchronously before first paint; normal operation remains asynchronous polling.
