"""CalibrationLevel (docs/CALIBRATION_SCOPE.md): the explicit, unavoidable
label every calibration result carries. There is no ABSOLUTE_RF_CALIBRATED
value - adding one is a deliberate future decision gated on real reference
equipment (docs/CALIBRATION_MODEL.md), never something reachable by
accident in this codebase today.
"""
from __future__ import annotations

from enum import Enum


class CalibrationLevel(str, Enum):
    OPERATIONAL_RELATIVE = "OPERATIONAL_RELATIVE"
    ABSOLUTE_RF_UNAVAILABLE = "ABSOLUTE_RF_UNAVAILABLE"


# Every calibration_result.json must carry both of these, verbatim -
# absolute_calibration is a plain bool (not the enum) so a naive
# `if result["absolute_calibration"]:` reads correctly without knowing
# about the enum at all.
CURRENT_LEVEL = CalibrationLevel.OPERATIONAL_RELATIVE
ABSOLUTE_CALIBRATION_AVAILABLE = False
