"""mount_idle_problems() (almita_web_ops.py) real preflight gate.

Two real, reported failures fixed here:
  1. It used to require mount_state == "Idle" exactly, blocking ALIGN from starting whenever the mount was
     simply in its normal "Tracking" state (tracking on, stationary, following the sky - the mount's ordinary
     resting state after any prior alignment/observation).
  2. The first fix (a movement-keyword BLOCKLIST) still silently PASSED any mount_state it didn't recognise as
     a movement word, including an absent one (None) - never having a full enumeration of OnStep's own status
     vocabulary in this repo made that unsafe. The current version is a WHITELIST of the only two states this
     repo has ever seen documented or exercised ("Idle", "Tracking"): anything else - real movement under
     whatever name OnStep gives it, an unrecognised status string, or none reported at all - blocks, fail-closed.

mount_state here is OnStep's own "OnStep Status" > "Tracking" text field - an ACTIVITY string, not the on/off
switch (that's m["tracking"], TELESCOPE_TRACK_STATE, and must never affect this function either way) and not
the tracking RATE/MODE (sidereal/solar - alignment_engine.tracking.TrackingMode, unrelated and untouched here).

These tests are read-only: they call the pure function directly with fixture dicts, never touch a real mount,
INDI server or almita_web_ops.py's job/subprocess machinery."""
import almita_web_ops as ops


def _mount(**over):
    """A real-shaped read_mount() result for a connected, unparked, error-free mount - only the field(s)
    under test vary between cases."""
    base = {"error": None, "connected": True, "ra_h": 6.0, "dec_deg": -30.0, "eod_state": "Ok",
            "tracking": "on", "track_mode": "sidereal", "parked": False, "pier": ["EAST"],
            "mount_state": "Idle", "onstep_error": "None", "onstep_time_utc": None}
    base.update(over)
    return base


def test_both_known_rest_states_pass():
    """The actual reported bug: mount_state == "Tracking" (tracking on, stationary) must PASS, exactly like
    "Idle" (tracking off, stationary) already did - both are explicitly whitelisted, safe, at-rest states."""
    assert ops.mount_idle_problems(_mount(mount_state="Idle")) == []
    assert ops.mount_idle_problems(_mount(mount_state="Tracking", tracking="on")) == []


def test_tracking_on_or_off_never_matters_on_its_own():
    """Explicit requirement: allow starting with tracking ON or OFF - this field (TELESCOPE_TRACK_STATE, read
    into m["tracking"]) must never by itself cause a block; only mount_state/park/error/connection do."""
    for tracking in ("on", "off", None):
        assert ops.mount_idle_problems(_mount(mount_state="Idle", tracking=tracking)) == []
        assert ops.mount_idle_problems(_mount(mount_state="Tracking", tracking=tracking)) == []


def test_real_movement_blocks_whatever_its_exact_wording():
    """Real movement must still block - now simply because it's not one of the two whitelisted rest states,
    not because it matches a specific keyword (so an OnStep wording this repo has never seen still blocks)."""
    for word in ("Slewing", "GOTO in progress", "moving to target", "Busy", "Homing"):
        probs = ops.mount_idle_problems(_mount(mount_state=word))
        assert len(probs) == 1 and word in probs[0] and "not a known rest state" in probs[0], (word, probs)


def test_unknown_mount_state_blocks():
    """The gap the blocklist version left open: an OnStep status this repo doesn't recognise as EITHER a rest
    state or a movement word must still block, fail-closed - never silently pass through."""
    probs = ops.mount_idle_problems(_mount(mount_state="SomeNewFirmwareStatus"))
    assert len(probs) == 1 and "SomeNewFirmwareStatus" in probs[0] and "not a known rest state" in probs[0]


def test_absent_mount_state_blocks():
    """No OnStep status reported at all (mount_state is None) used to silently pass (neither "Idle" nor a
    movement word) - must now block too, fail-closed, same as any other unrecognised value."""
    probs = ops.mount_idle_problems(_mount(mount_state=None))
    assert len(probs) == 1 and "not a known rest state" in probs[0]


def test_park_still_blocks_regardless_of_mount_state_text():
    assert ops.mount_idle_problems(_mount(parked=True)) == ["mount is parked"]
    # even with a whitelisted rest state - the dedicated `parked` flag (TELESCOPE_PARK) is authoritative and
    # independent of this text field.
    assert ops.mount_idle_problems(_mount(mount_state="Tracking", parked=True)) == ["mount is parked"]


def test_onstep_error_still_blocks():
    probs = ops.mount_idle_problems(_mount(onstep_error="Motor Fault"))
    assert probs == ["OnStep error Motor Fault"]


def test_not_connected_blocks():
    """connected is parsed by read_mount() but was never checked here at all - one of the operator's four
    explicit preconditions (connected, unparked, no error, no incompatible movement)."""
    assert ops.mount_idle_problems(_mount(connected=False)) == ["mount not connected"]


def test_bad_eod_coordinate_state_still_blocks():
    """EQUATORIAL_EOD_COORD's own INDI vector state (Idle/Ok = at rest, Busy = moving, Alert = error) is
    untouched by this fix - Busy/Alert must still block."""
    assert ops.mount_idle_problems(_mount(eod_state="Busy")) == ["mount coordinates state Busy"]
    assert ops.mount_idle_problems(_mount(eod_state="Alert")) == ["mount coordinates state Alert"]


def test_read_mount_error_short_circuits_everything_else():
    assert ops.mount_idle_problems(_mount(error="mount unreachable: timed out")) == ["mount unreachable: timed out"]


def test_multiple_real_problems_are_all_reported_together():
    probs = ops.mount_idle_problems(_mount(parked=True, onstep_error="Motor Fault", eod_state="Alert"))
    assert probs == ["mount coordinates state Alert", "mount is parked", "OnStep error Motor Fault"]
