"""REDUCE: transforms LEVEL 0 (RAW capture, evidence) into LEVEL 1 (REDUCED
SCIENTIFIC MEASUREMENT). Sits between OBSERVE and the future SCIENCE module.

RAW ES EVIDENCIA. This package never modifies, overwrites, or "cleans"
original capture files. Invalid data is masked or quality-flagged, never
zeroed or silently dropped. See docs/REDUCE_SCOPE.md.
"""
from reduce_engine.config import ReduceConfig
from reduce_engine.models import CaptureRef, MaskFlag, MasterSpectrum, QualityState

__all__ = ["ReduceConfig", "CaptureRef", "MaskFlag", "MasterSpectrum", "QualityState"]
