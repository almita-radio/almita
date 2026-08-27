"""Central field-runtime policy: Astropy must never auto-download IERS data."""
def configure_astropy_offline():
    from astropy.utils import iers
    iers.conf.auto_download = False
    return iers.conf.auto_download

configure_astropy_offline()
