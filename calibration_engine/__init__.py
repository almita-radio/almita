"""Calibration V1: OPERATIONAL / RELATIVE / REPRODUCIBILITY calibration only.

See docs/CALIBRATION_SCOPE.md before touching anything in this package -
it fixes what may and may not be claimed. Every result carries
CalibrationLevel.OPERATIONAL_RELATIVE and absolute_calibration=False; there
is no code path in this package that can produce an absolute RF quantity
(Kelvin, Jansky, dBm, noise figure). Deliberately a separate top-level
package from alignment_engine (not nested inside it) - calibration and
alignment are two different instrumental concerns that happen to share
infrastructure (session/evidence patterns, deployment-state labeling,
DS18B20 reading, PSD computation), not one owning the other.
"""
