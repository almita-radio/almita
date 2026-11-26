"""ALMITA static demo (operator 2026-11-25: free static site, a bounded region): precompute the simulated sky.

    python demo/build_demo_data.py            ->  demo/site/sky.json  (git-ignored: HI4PI-derived)

The region is the one HI4PI tile on disk, CAR_E01 (l -1..21 deg, b -11..+11 deg, 5' pixels, 1.29 km/s). For every
pointing on a 1-deg grid (l 0..20, b -10..10) and every simulated beam (20 deg = ALMITA today, 12 deg = a 1.2 m dish,
5 deg), the HI4PI spectrum through a Gaussian beam TRUNCATED to the tile (cover = the share of the full beam inside;
the demo says so). Velocities |v| <= 150 km/s LSRK, brightness in K, quantised to uint16 per beam. Also: a moment-0
image of the tile for the click map, and the LSRK -> topocentric shift of each pointing for one demo epoch at a
city-level site (Santiago). The browser does the rest (demo/site/app.js): radiometer noise, band shape, frequency
switching, fold. Nothing here is a measurement; every product says SIMULATED and cites HI4PI.
"""
from __future__ import annotations

import base64
import json
import math
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
TILE = ROOT / "data" / "reference" / "hi" / "hi4pi" / "CAR_E01.fits"
OUT = Path(__file__).resolve().parent / "site" / "sky.json"
BEAMS_DEG = (20.0, 12.0, 5.0)
L_GRID = np.arange(0, 21, 1.0)
B_GRID = np.arange(-10, 11, 1.0)
V_MAX = 150.0
COARSE = 3                                   # 15' pixels for the beam sums (plenty for a >= 5 deg beam)
EPOCH_UTC = "2026-12-06T03:00:00"            # a Petorca-week night, 00:00 Chile
SITE = (-33.45, -70.67, 570.0)               # city level (Santiago), never the operator's exact site


def main() -> None:
    from astropy.io import fits
    with fits.open(TILE, memmap=True) as f:
        h = f[0].header
        n1, n2, n3 = h["NAXIS1"], h["NAXIS2"], h["NAXIS3"]
        l = h["CRVAL1"] + (np.arange(1, n1 + 1) - h["CRPIX1"]) * h["CDELT1"]
        b = h["CRVAL2"] + (np.arange(1, n2 + 1) - h["CRPIX2"]) * h["CDELT2"]
        v = (h["CRVAL3"] + (np.arange(1, n3 + 1) - h["CRPIX3"]) * h["CDELT3"]) / 1000.0
        sel = np.abs(v) <= V_MAX
        nb, nl = (n2 // COARSE) * COARSE, (n1 // COARSE) * COARSE
        cube = np.asarray(f[0].data[sel, :nb, :nl], dtype=np.float32)
    v = v[sel]
    dv = float(abs(h["CDELT3"]) / 1000.0)
    mom0 = np.nansum(cube, axis=0) * dv                                       # K km/s, full resolution
    c = np.nanmean(cube.reshape(cube.shape[0], nb // COARSE, COARSE, nl // COARSE, COARSE), axis=(2, 4))
    cl = l[:nl].reshape(-1, COARSE).mean(1)
    cb = b[:nb].reshape(-1, COARSE).mean(1)
    pix = math.radians(abs(h["CDELT1"]) * COARSE) * math.radians(abs(h["CDELT2"]) * COARSE)
    DL, B = np.meshgrid(cl, cb)

    beams = []
    for fwhm in BEAMS_DEG:
        sig = fwhm / 2.3548
        spec = np.zeros((B_GRID.size, L_GRID.size, v.size), np.float32)
        cover = np.zeros((B_GRID.size, L_GRID.size))
        for j, b0 in enumerate(B_GRID):
            for i, l0 in enumerate(L_GRID):
                w = np.exp(-0.5 * (((DL - l0) * np.cos(np.radians(B))) ** 2 + (B - b0) ** 2) / sig ** 2) * np.cos(np.radians(B))
                cover[j, i] = w.sum() * pix / (2 * math.pi * math.radians(sig) ** 2)
                spec[j, i] = np.nansum(c * w[None], axis=(1, 2)) / w.sum()
        top = float(spec.max())
        q = np.clip(np.round(spec / top * 65535), 0, 65535).astype("<u2")
        beams.append({"fwhm_deg": fwhm, "scale_K": top, "cover": np.round(cover, 3).tolist(),
                      "spectra_u16_b64": base64.b64encode(q.tobytes()).decode()})
        print(f"beam {fwhm:4.1f} deg: peak {top:.1f} K, cover {cover.min():.2f}-{cover.max():.2f}")

    # click map: moment 0 at 0.25 deg, l increasing to the LEFT as on the sky
    m = mom0[: (nb // 3) * 3, : (nl // 3) * 3].reshape(nb // 3, 3, nl // 3, 3).mean(axis=(1, 3))
    m_l = l[: (nl // 3) * 3].reshape(-1, 3).mean(1)
    m_b = b[: (nb // 3) * 3].reshape(-1, 3).mean(1)
    mq = np.clip(np.round(m / m.max() * 255), 0, 255).astype(np.uint8)

    # LSRK -> topocentric shift per pointing at the demo epoch (radial_velocity_correction + 20 km/s to the apex)
    from astropy import units as u
    from astropy.coordinates import FK4, EarthLocation, SkyCoord
    from astropy.time import Time
    loc = EarthLocation(lat=SITE[0] * u.deg, lon=SITE[1] * u.deg, height=SITE[2] * u.m)
    t = Time(EPOCH_UTC, scale="utc")
    apex = SkyCoord(ra=18 * u.hourangle, dec=30 * u.deg, frame=FK4(equinox="B1900")).icrs
    shift = np.zeros((B_GRID.size, L_GRID.size))
    radec = []
    for j, b0 in enumerate(B_GRID):
        row = []
        for i, l0 in enumerate(L_GRID):
            g = SkyCoord(l=l0 * u.deg, b=b0 * u.deg, frame="galactic").icrs
            bary = g.radial_velocity_correction(kind="barycentric", obstime=t, location=loc).to(u.km / u.s).value
            shift[j, i] = bary + 20.0 * float(np.dot(g.cartesian.xyz.value, apex.cartesian.xyz.value))
            row.append([round(g.ra.deg, 3), round(g.dec.deg, 3)])
        radec.append(row)

    out = {"schema": "almita.demo_sky.v1", "SIMULATED": True,
           "source": "HI4PI Collaboration 2016, A&A 594, A116 (tile CAR_E01), through a simulated Gaussian beam",
           "region": {"l_deg": [float(l.min()), float(l.max())], "b_deg": [float(b.min()), float(b.max())]},
           "l_grid": L_GRID.tolist(), "b_grid": B_GRID.tolist(),
           "v_km_s": {"start": float(v[0]), "step": float(v[1] - v[0]), "n": int(v.size)},
           "beams": beams, "lsrk_shift_km_s": np.round(shift, 3).tolist(), "radec_deg": radec,
           "epoch_utc": EPOCH_UTC, "site": "Santiago, Chile (city level)",
           "mom0": {"l": np.round(m_l, 3).tolist(), "b": np.round(m_b, 3).tolist(), "max_K_km_s": float(m.max()),
                    "u8_b64": base64.b64encode(mq.tobytes()).decode(), "shape": list(mq.shape)}}
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(out, separators=(",", ":")))
    print(OUT, OUT.stat().st_size // 1024, "KB")


if __name__ == "__main__":
    main()
