# RFI_REF Interface Proposal (not implemented — contract only)

## The gap (found in the first pass, confirmed again in this pass)

RFI_REF exists operationally elsewhere in ALMITA (CALIBRATE's own
workflow captures an RFI_REF spectrum alongside MAIN). But OBSERVE's real
output — audited again in this pass across three real campaigns — does
not record any association between a MAIN observation point and a
specific RFI_REF measurement. `reduce_engine/rfi_ref.py`'s
`evaluate_rfi_ref` is real, wired into `pipeline.py`, and unit-tested —
but on every real campaign it correctly reports `available=False`,
because there is nothing to associate.

**This pass does not modify OBSERVE.** This document is the minimum
contract OBSERVE would need to produce, for a future, separate,
explicitly-scoped pass to close this gap.

## Minimum fields OBSERVE would need to emit per point

```
rfi_ref_timestamp_utc        # when the associated RFI_REF measurement was taken
rfi_ref_window_start_utc     # start of the RFI_REF capture window
rfi_ref_window_end_utc       # end of the RFI_REF capture window
rfi_ref_product_id           # identifier/path of the RFI_REF product (spectrum/capture)
rfi_ref_receiver_id          # which RFI_REF receiver/antenna produced it
```

These would most naturally live as additional columns in `mosaic.csv` (or
a sibling per-point manifest), the same place `capture_status`,
`data_filename`, and pointing metadata already live — not a new,
separate lookup mechanism OBSERVE and REDUCE would need to keep in sync
by convention.

## Tentative matching criterion (not production code)

A MAIN point and an RFI_REF measurement should only be considered
associated when **all** of:

- **Temporal proximity**: `|main_timestamp - rfi_ref_timestamp| <=
  rfi_ref_max_time_delta_seconds` (REDUCE already has this as a
  registered, configurable field — `ReduceConfig.rfi_ref_max_time_delta_seconds`).
- **Frequency overlap**: the RFI_REF capture's frequency axis must
  overlap MAIN's (ideally identical, per REDUCE's existing
  `evaluate_rfi_ref` check).
- **Receiver identity**: `rfi_ref_receiver_id` must identify a real,
  known RFI_REF receiver — never assumed by absence.
- **Validity interval**: the RFI_REF product itself must be flagged
  valid/complete (mirroring `capture_status == "success"`) — a partial or
  failed RFI_REF capture must not be silently treated as "no RFI seen".

## Known risks of over-trusting RFI_REF (why V1 stays veto/flag-only)

- **Common celestial signal**: a real astrophysical or terrestrial-but-
  legitimate signal visible to both MAIN and RFI_REF antennas would be
  wrongly vetoed if the criterion were "any common signal" — this is
  exactly why `evaluate_rfi_ref` requires *coincident statistically
  significant excess in both*, not mere correlation, and never subtracts
  — only flags.
- **Broadband local interference**: a broadband source affecting both
  antennas similarly could still look "coincident" without being a true
  point-source RFI line — the current implementation's per-bin excess
  threshold reduces but does not eliminate this risk; a future pass with
  real RFI_REF data should validate the `excess_sigma_threshold` and
  `min_match_confidence` defaults against real coincident/non-coincident
  cases, not just synthetic ones.
- **Different antenna patterns**: MAIN and RFI_REF do not necessarily see
  the sky (or the interference) with the same gain pattern — a source
  strong in one and weak in the other could evade or over-trigger the
  coincidence criterion. Any future confidence-scoring refinement should
  account for this, not assume identical sensitivity.

## What REDUCE will NOT do with RFI_REF, even once associated

- Never subtract or cancel — veto/flag only (mask bins `RFI`).
- Never treat "RFI_REF unavailable" as a processing failure — REDUCE
  continues without it, exactly as it does today.
- Never invent a match when temporal/frequency/receiver conditions are
  not met — `evaluate_rfi_ref` returns `used=False` with an explicit
  `reason` in that case, not a best-effort guess.
