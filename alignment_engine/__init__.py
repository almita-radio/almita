"""ALMITA Alignment engine: console-operable today, API-consumable tomorrow.

This package does NOT replace or duplicate alignment.py ("ALMITA Alignment
V2") - that module's proven science (Sun/HI spherical template matching,
tested offset recovery, real field hardware trials on 2026-08-26) is
imported and reused here, not rewritten. What this package adds on top:

  - an explicit TrackingMode abstraction (SIDEREAL/SOLAR) - confirmed by
    direct repo audit to not exist anywhere before this package
  - a state machine with explicit transitions (see state_machine.py)
  - persistence as first-class sessions under data/alignment/<MODE>-<ts>/
  - a strict MEASURE -> FIT -> SHOW RESULT -> decide -> APPLY SYNC -> VERIFY
    split (prepare_sync/apply_sync/verify_sync as separate calls, never
    fused into one run() the way alignment.py's --apply-sync flag is)
  - a JSON-serializable progress snapshot, for a future web UI at :8090
    to consume without any of this logic being copied or reimplemented

Import boundary: this package imports pure/science functions from
alignment.py (sun_eod, offset_coordinates, estimate_template_offset,
LocalSphericalTemplate, load_hi_catalog, choose_hi_region, sync_allowed)
and from hi_spectral_metric.py, grid_generator.py, runtime_state.py,
astropy_offline.py. It never modifies those files.
"""
