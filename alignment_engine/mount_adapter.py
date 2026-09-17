"""Mount adapter (Fase 0/9 evidence-driven).

RealMountAdapter wraps indi_telescope_control.INDITelescopeControl - the
same class capture.py/alignment.py already use - rather than adding a
fourth hand-rolled INDI client (the repo audit found three already:
INDITelescopeControl, simple_indi_client.py, explore_telescope_properties.py
- duplication this package must not add to).

Coordinate frame: confirmed by direct audit that EQUATORIAL_EOD_COORD
expects RA/Dec of-date (JNow/EOD), not J2000/ICRS - and that capture.py
sends ICRS values directly without converting, which alignment.py does not
do (it explicitly converts via sun_eod()/CIRS). RealMountAdapter always
converts to CIRS(obstime=now) before every goto(), matching alignment.py's
correct pattern, not capture.py's.

RealMountAdapter is written for completeness (it documents exactly what a
future real run would do) but is not exercised by any test and is not
wired into the CLI in this version - real GOTO/SYNC stays behind a second,
explicit hardware authorization. SimulatedMountAdapter is what every
current test and CLI --simulate/--dry-run path actually uses.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Protocol

from astropy.coordinates import CIRS, SkyCoord
from astropy.time import Time
import astropy.units as u


class MountAdapter(Protocol):
    async def connect(self) -> bool: ...
    async def disconnect(self) -> None: ...
    async def goto(self, target: SkyCoord) -> bool: ...
    async def get_position(self) -> Optional[SkyCoord]: ...
    async def sync(self, reference: SkyCoord) -> bool: ...


@dataclass
class SimulatedMountAdapter:
    """In-memory mount: goto always lands exactly on the commanded ICRS
    coordinate (the mount itself is never the thing under test here - the
    beam/template simulation in simulation.py is where a pointing offset is
    injected, exactly mirroring alignment.py's own simulate_offset_case
    approach). Optional per-index failure injection lets tests exercise a
    "GOTO failed at point N" path without needing real hardware."""

    fail_at_indices: frozenset = field(default_factory=frozenset)
    current: Optional[SkyCoord] = None
    goto_log: List[Dict] = field(default_factory=list)
    connected: bool = False
    synced_to: Optional[SkyCoord] = None

    async def connect(self) -> bool:
        self.connected = True
        return True

    async def disconnect(self) -> None:
        self.connected = False

    async def goto(self, target: SkyCoord, point_index: Optional[int] = None) -> bool:
        self.goto_log.append({"point_index": point_index, "ra_hours": target.ra.hour, "dec_deg": target.dec.deg})
        if point_index is not None and point_index in self.fail_at_indices:
            return False
        self.current = target
        return True

    async def get_position(self) -> Optional[SkyCoord]:
        return self.current

    async def sync(self, reference: SkyCoord) -> bool:
        self.synced_to = reference
        self.current = reference
        return True


class RealMountAdapter:
    """Wraps indi_telescope_control.INDITelescopeControl. Not exercised by
    any test in this package - see module docstring. Kept here so the
    interface (and the CIRS/EOD conversion it must perform) is designed
    and documented now, rather than invented later under time pressure."""

    def __init__(self, host: str, port: int, device: str, verbose: bool = False):
        from indi_telescope_control import INDITelescopeControl
        self._telescope = INDITelescopeControl(host, port, device, verbose)

    async def connect(self) -> bool:
        return await self._telescope.connect()

    async def disconnect(self) -> None:
        await self._telescope.disconnect()

    async def goto(self, target: SkyCoord, point_index: Optional[int] = None) -> bool:
        eod = target.transform_to(CIRS(obstime=Time.now()))
        return await self._telescope.goto(eod.ra.hour, eod.dec.deg)

    async def get_position(self) -> Optional[SkyCoord]:
        ra, dec = await self._telescope.get_coordinates(force_refresh=True)
        if ra is None or dec is None:
            return None
        # EQUATORIAL_EOD_COORD is of-date; report it back as an
        # equinox-of-date CIRS coordinate rather than silently mislabeling
        # it ICRS - callers that need ICRS must transform explicitly.
        return SkyCoord(ra=ra * u.hourangle, dec=dec * u.deg, frame=CIRS(obstime=Time.now()))

    async def sync(self, reference: SkyCoord) -> bool:
        eod = reference.transform_to(CIRS(obstime=Time.now()))
        return bool(await self._telescope.sync(eod.ra.hour, eod.dec.deg))
