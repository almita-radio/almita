"""HI (21cm neutral hydrogen) sky-alignment subsystem.

Built entirely offline (no network access assumed at runtime, no auto-
download of survey data - see reference_trust.py) and, as of this pass,
without scipy/healpy (not installed on this Pi) - every numeric routine
here is plain numpy, mirroring the style fitting.py already established
for the solar sub-grid refinement.
"""
