"""SCIENCE FEATURE FORENSICS V1 - additive diagnostics on top of the frozen REDUCE V1 / SCIENCE V1 products.

Question answered: does an observed spectral structure follow the sky, or time / capture order / the receiver / RFI?
It measures; it never cleans, subtracts or corrects, never writes inside a REDUCE or SCIENCE session, and never opens RAW.
"""
FORENSICS_SCHEMA_VERSION = "1.0"
FORENSICS_PIPELINE_VERSION = "science-forensics-v1.0"
