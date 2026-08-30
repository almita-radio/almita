"""Central field-runtime policy: Astropy must never auto-download IERS data.

auto_download=False alone is not sufficient for real field operation: Astropy
still raises ValueError when a requested time falls outside the "predictive"
reach of the locally bundled/cached IERS table (default threshold: 30 days -
see astropy.utils.iers.conf.auto_max_age). A Raspberry Pi in the field has no
path to ever refresh that table, so this threshold must be disabled here,
centrally, rather than patched per-caller.
"""
def configure_astropy_offline():
    from astropy.utils import iers
    iers.conf.auto_download = False
    iers.conf.auto_max_age = None
    return iers.conf.auto_download

configure_astropy_offline()
