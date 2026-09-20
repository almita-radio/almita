def test_iers_auto_download_disabled_and_real_transform():
    from astropy_offline import configure_astropy_offline
    assert configure_astropy_offline() is False
    from astropy.utils import iers
    assert iers.conf.auto_download is False
    import astropy.units as u
    from astropy.coordinates import AltAz, EarthLocation, SkyCoord
    from astropy.time import Time
    location=EarthLocation.from_geodetic(lon=-70.6483*u.deg,lat=-33.4569*u.deg,height=550*u.m)
    target=SkyCoord(ra=120*u.deg,dec=-30*u.deg,frame="icrs")
    horizontal=target.transform_to(AltAz(obstime=Time("2026-08-27T00:00:00",scale="utc"),location=location))
    assert horizontal.alt.deg==horizontal.alt.deg and horizontal.az.deg==horizontal.az.deg
    assert iers.conf.auto_download is False
