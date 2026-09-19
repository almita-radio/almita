"""MASK CONTRACT validation (2nd-pass section 6): MaskFlag combinations
that arise in real operation, and confirming GOOD == no flags (not an
independent bit that could coexist with a BAD flag)."""
import numpy as np

from reduce_engine.models import MaskFlag


def test_good_is_the_zero_value_not_an_independent_bit():
    """GOOD occupies no bit position at all (value 0) - it is the ABSENCE
    of every other reason, not a settable flag that could coexist
    alongside a real exclusion reason. (Note: Python's Flag.__contains__
    trivially reports the zero member as "in" every combination - that is
    a quirk of the stdlib operator for zero-valued flags, not evidence
    GOOD "coexists" with anything; the real, meaningful invariants are
    checked below: GOOD never appears in .reasons(), and any non-zero
    combination is never .usable().)"""
    assert MaskFlag.GOOD.value == 0
    combined = MaskFlag.combine(MaskFlag.RFI, MaskFlag.EDGE)
    assert "GOOD" not in combined.reasons()
    assert not combined.usable()
    assert combined.value != MaskFlag.GOOD.value


def test_dc_plus_rfi_combination_reports_both_reasons():
    combined = MaskFlag.combine(MaskFlag.DC, MaskFlag.RFI)
    assert set(combined.reasons()) == {"DC", "RFI"}
    assert not combined.usable()


def test_edge_plus_invalid_combination_reports_both_reasons():
    combined = MaskFlag.combine(MaskFlag.EDGE, MaskFlag.INVALID)
    assert set(combined.reasons()) == {"EDGE", "INVALID"}


def test_rfi_plus_saturated_combination_reports_both_reasons():
    combined = MaskFlag.combine(MaskFlag.RFI, MaskFlag.SATURATED)
    assert set(combined.reasons()) == {"RFI", "SATURATED"}


def test_missing_plus_user_excluded_combination_reports_both_reasons():
    combined = MaskFlag.combine(MaskFlag.MISSING, MaskFlag.USER_EXCLUDED)
    assert set(combined.reasons()) == {"MISSING", "USER_EXCLUDED"}


def test_all_nine_flags_are_distinct_bit_positions():
    values = [flag.value for flag in MaskFlag if flag != MaskFlag.GOOD]
    assert len(values) == len(set(values)) == 8  # DC, KNOWN_SPUR, RFI, EDGE, INVALID, MISSING, SATURATED, USER_EXCLUDED
    # each is a distinct power of two - no accidental overlap
    for v in values:
        assert v & (v - 1) == 0


def test_combining_every_flag_still_decodes_every_reason():
    every_flag = MaskFlag.combine(*[f for f in MaskFlag if f != MaskFlag.GOOD])
    assert set(every_flag.reasons()) == {"DC", "KNOWN_SPUR", "RFI", "EDGE", "INVALID", "MISSING",
                                         "SATURATED", "USER_EXCLUDED"}
    assert not every_flag.usable()


def test_mask_array_bitwise_or_composes_like_combine():
    array_mask = np.array([MaskFlag.DC.value | MaskFlag.RFI.value], dtype=np.int64)
    decoded = MaskFlag(int(array_mask[0]))
    assert set(decoded.reasons()) == {"DC", "RFI"}


def test_usable_bin_mask_treats_any_single_flag_as_unusable():
    from reduce_engine.masks import usable_bin_mask
    for flag in MaskFlag:
        if flag == MaskFlag.GOOD:
            continue
        mask = np.array([flag.value], dtype=np.int64)
        assert usable_bin_mask(mask)[0] is np.False_ or not usable_bin_mask(mask)[0]
