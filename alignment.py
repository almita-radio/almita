#!/usr/bin/env python3
"""
Antenna alignment helper using HI brightest target
- Picks the strongest visible HI point from offline catalog
- Performs iterative peaking with an 8-point circular pattern
- Optionally syncs the mount once the peak is found
"""

import argparse
import asyncio
import json
import math
from pathlib import Path
from typing import List, Tuple, Optional

import numpy as np

from indi_telescope_control import INDITelescopeControl
from sdr_capture import SDRCapture


class AlignmentRunner:
    def __init__(
        self,
        radius_deg: float,
        capture_time: float,
        iterations: int,
        min_elevation: float,
        center_freq: int,
        sample_rate: int,
        settle_time: float,
        no_sync: bool,
        no_calib: bool,
        host: str,
        port: int,
        device: str,
        sdr_host: str,
        sdr_port: int,
        observer_config: str,
        verbose: bool,
        output_base: Optional[str] = None,
    ):
        self.radius_deg = radius_deg
        self.capture_time = capture_time
        self.iterations = iterations
        self.min_elevation = min_elevation
        self.center_freq = center_freq
        self.sample_rate = sample_rate
        self.settle_time = settle_time
        self.no_sync = no_sync
        self.no_calib = no_calib
        self.host = host
        self.port = port
        self.device = device
        self.sdr_host = sdr_host
        self.sdr_port = sdr_port
        self.observer_config = observer_config
        self.verbose = verbose
        self.session_id = datetime.now(timezone.utc).strftime('%Y%m%d-%H:%M:%S')
        # Default output under data/alignment/<session_id>/
        if output_base:
            self.output_dir = Path(output_base)
        else:
            self.output_dir = Path(__file__).parent / 'data' / 'alignment' / self.session_id
        self.output_dir.mkdir(parents=True, exist_ok=True)

        self.observer = None
        self.catalog = []

        self.telescope: Optional[INDITelescopeControl] = None
        self.sdr: Optional[SDRCapture] = None

    async def run(self):
        self._load_observer_config()
        self._load_hi_catalog()

        target = self._pick_strongest_visible()
        if not target:
            print(f"No hay blancos visibles sobre {self.min_elevation}°")
            return 1

        if self.no_calib:
            print("Modo sin calibración: se usa potencia relativa (sin K absoluta)")

        print("Objetivo inicial (HI más fuerte visible):")
        print(f"  RA={target['ra_hours']:.2f}h  DEC={target['dec_deg']:+.1f}°  Tb={target['tb_kelvin']:.1f}K  Alt={target['altitude']:.1f}°")

        self.telescope = INDITelescopeControl(
            host=self.host,
            port=self.port,
            device_name=self.device,
            verbose=self.verbose,
        )

        self.sdr = SDRCapture(mode="network", host=self.sdr_host, port=self.sdr_port, verbose=self.verbose)

        try:
            # Connect both subsystems
            if not await self.telescope.connect():
                return 1

            await self.sdr.connect()
            await self.sdr.configure(center_freq=self.center_freq, sample_rate=self.sample_rate, gain='auto')

            # Go to initial target
            await self.telescope.goto(target['ra_hours'], target['dec_deg'])
            await asyncio.sleep(self.settle_time)

            best_ra_deg = target['ra_hours'] * 15.0
            best_dec_deg = target['dec_deg']
            best_power = await self._measure_power()
            print(f"Medición centro inicial: power={best_power:.4f}")

            radius = self.radius_deg

            for step in range(1, self.iterations + 1):
                print(f"\nIteración {step}/{self.iterations} - radio {radius:.3f}°")
                ring = self._build_ring(best_ra_deg, best_dec_deg, radius)

                measurements = []
                for idx, (ra_deg, dec_deg) in enumerate(ring, 1):
                    alt = self._equatorial_to_alt(ra_deg / 15.0, dec_deg)
                    if alt < self.min_elevation:
                        print(f"  P{idx}: skip alt {alt:.1f}° < {self.min_elevation}°")
                        continue

                    print(f"  P{idx}: RA={ra_deg/15.0:.4f}h DEC={dec_deg:+.3f}° Alt={alt:.1f}° -> goto")
                    await self.telescope.goto(ra_deg / 15.0, dec_deg)
                    await asyncio.sleep(self.settle_time)

                    power = await self._measure_power()
                    print(f"  P{idx}: power={power:.4f}")
                    measurements.append((power, ra_deg, dec_deg))

                if not measurements:
                    print("Sin medidas válidas en esta iteración; deteniendo.")
                    break

                best_power, best_ra_deg, best_dec_deg = max(measurements, key=lambda x: x[0])
                print(f"  Mejor punto: RA={best_ra_deg/15.0:.4f}h DEC={best_dec_deg:+.3f}° power={best_power:.4f}")

                # Recentrar en el mejor punto antes de reducir radio
                await self.telescope.goto(best_ra_deg / 15.0, best_dec_deg)
                await asyncio.sleep(self.settle_time)

                radius *= 0.5

            # Final sync (optional)
            if not self.no_sync:
                print("Aplicando SYNC con el mejor punto encontrado...")
                await self.telescope.sync(best_ra_deg / 15.0, best_dec_deg)
            else:
                print("--no_sync activo: no se aplica SYNC final")

            print("\nResultado final:")
            print(f"  RA={best_ra_deg/15.0:.4f}h DEC={best_dec_deg:+.3f}° power={best_power:.4f}")
            # Persist results
            results = {
                'session_id': self.session_id,
                'initial_target': target,
                'best_point': {
                    'ra_hours': best_ra_deg / 15.0,
                    'dec_deg': best_dec_deg,
                    'power': best_power,
                },
                'radius_initial_deg': self.radius_deg,
                'iterations': self.iterations,
                'capture_time_s': self.capture_time,
                'min_elevation_deg': self.min_elevation,
                'no_calib': self.no_calib,
                'no_sync': self.no_sync,
                'timestamp_utc': datetime.now(timezone.utc).isoformat(),
                'output_dir': str(self.output_dir),
            }
            results_file = self.output_dir / 'alignment_results.json'
            with open(results_file, 'w') as f:
                json.dump(results, f, indent=2)
            print(f"Resultados guardados en: {results_file}")

            await self._safe_disconnect()
            return 0
        finally:
            await self._safe_disconnect()

    def _load_observer_config(self):
        cfg_path = Path(self.observer_config)
        if not cfg_path.is_absolute():
            cfg_path = Path(__file__).parent / cfg_path

        if not cfg_path.exists():
            raise FileNotFoundError(f"Observer config no encontrado: {cfg_path}")

        with open(cfg_path, 'r') as f:
            data = json.load(f)
            obs = data.get('observer', {})
            self.observer = {
                'name': obs.get('name', 'Unknown'),
                'latitude': obs.get('latitude_deg', -33.4489),
                'longitude': obs.get('longitude_deg', -70.6693),
                'elevation': obs.get('elevation_m', 570),
            }

    def _load_hi_catalog(self):
        catalog_file = Path(__file__).parent / 'data' / 'hi_sky_catalog_2000pts.csv'
        if not catalog_file.exists():
            raise FileNotFoundError(f"HI catalog no encontrado: {catalog_file}")

        import csv
        with open(catalog_file) as f:
            reader = csv.DictReader(f)
            for row in reader:
                self.catalog.append({
                    'point_id': int(row['point_id']),
                    'ra_hours': float(row['ra_hours']),
                    'dec_deg': float(row['dec_deg']),
                    'tb_kelvin': float(row['tb_kelvin']),
                })

    def _pick_strongest_visible(self):
        lat = self.observer['latitude']
        lon = self.observer['longitude']

        visible = []
        for point in self.catalog:
            alt = self._equatorial_to_alt(point['ra_hours'], point['dec_deg'], lat, lon)
            if alt >= self.min_elevation:
                p = dict(point)
                p['altitude'] = alt
                visible.append(p)

        if not visible:
            return None

        visible_sorted = sorted(visible, key=lambda p: p['tb_kelvin'], reverse=True)
        return visible_sorted[0]

    def _equatorial_to_alt(self, ra_hours: float, dec_deg: float, lat_deg: Optional[float] = None, lon_deg: Optional[float] = None) -> float:
        lat = self.observer['latitude'] if lat_deg is None else lat_deg
        lon = self.observer['longitude'] if lon_deg is None else lon_deg

        from datetime import datetime, timezone

        now = datetime.now(timezone.utc)
        jd = 2451545.0 + (now - datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)).total_seconds() / 86400.0
        gmst = (18.697374558 + 24.06570982441908 * (jd - 2451545.0)) % 24
        lst = (gmst + lon / 15.0) % 24
        ha = (lst - ra_hours) * 15.0

        ha_rad = math.radians(ha)
        dec_rad = math.radians(dec_deg)
        lat_rad = math.radians(lat)

        sin_alt = math.sin(dec_rad) * math.sin(lat_rad) + math.cos(dec_rad) * math.cos(lat_rad) * math.cos(ha_rad)
        alt = math.degrees(math.asin(max(-1.0, min(1.0, sin_alt))))
        return alt

    def _build_ring(self, ra_deg: float, dec_deg: float, radius_deg: float) -> List[Tuple[float, float]]:
        points = []
        dec_rad = math.radians(dec_deg)
        cos_dec = max(0.1, abs(math.cos(dec_rad)))  # evitar división por cero

        for k in range(8):
            theta = 2 * math.pi * k / 8.0
            d_dec = radius_deg * math.sin(theta)
            d_ra = (radius_deg * math.cos(theta)) / cos_dec
            points.append((ra_deg + d_ra, dec_deg + d_dec))
        return points

    async def _measure_power(self) -> float:
        if not self.sdr or not self.sdr.socket:
            raise RuntimeError("SDR no conectado")

        expected_bytes = int(self.capture_time * self.sample_rate * 2)
        buf = bytearray()
        loop = asyncio.get_event_loop()

        while len(buf) < expected_bytes:
            remaining = expected_bytes - len(buf)
            read_size = min(16384, remaining)
            self.sdr.socket.settimeout(5.0)
            chunk = await loop.run_in_executor(None, self.sdr.socket.recv, read_size)
            if not chunk:
                raise ConnectionError("rtl_tcp cerró la conexión")
            buf.extend(chunk)

        iq = np.frombuffer(buf, dtype=np.uint8)
        i = (iq[0::2].astype(np.float32) - 127.5) / 127.5
        q = (iq[1::2].astype(np.float32) - 127.5) / 127.5
        power = float(np.mean(i * i + q * q))
        return power

    async def _safe_disconnect(self):
        try:
            if self.telescope:
                await self.telescope.disconnect()
        finally:
            if self.sdr and self.sdr.socket:
                self.sdr.socket.close()


def parse_args():
    parser = argparse.ArgumentParser(description="Alineación de antena usando catálogo HI")
    parser.add_argument('--radius', type=float, default=0.5, help='Radio inicial de búsqueda (grados)')
    parser.add_argument('--capture-time', type=float, default=1.5, help='Tiempo de captura por punto (s)')
    parser.add_argument('--iterations', type=int, default=3, help='Iteraciones de refinamiento')
    parser.add_argument('--min-elev', type=float, default=20.0, help='Elevación mínima (grados)')
    parser.add_argument('--center-freq', type=int, default=1420405752, help='Frecuencia central (Hz)')
    parser.add_argument('--sample-rate', type=int, default=2400000, help='Tasa de muestreo (Hz)')
    parser.add_argument('--settle', type=float, default=2.0, help='Tiempo de asentamiento tras GOTO (s)')
    parser.add_argument('--host', default='localhost', help='Servidor INDI')
    parser.add_argument('--port', type=int, default=7624, help='Puerto INDI')
    parser.add_argument('--device', default='Telescope Simulator', help='Dispositivo INDI')
    parser.add_argument('--sdr-host', default='localhost', help='Host rtl_tcp')
    parser.add_argument('--sdr-port', type=int, default=1234, help='Puerto rtl_tcp')
    parser.add_argument('--observer-config', default='observer_config.json', help='Archivo de config del observador')
    parser.add_argument('--no_sync', action='store_true', help='No aplicar SYNC al finalizar')
    parser.add_argument('--no_calib', action='store_true', help='Usar potencia relativa sin aplicar calibración (inicio de sesión)')
    parser.add_argument('--output-base', default=None, help='Carpeta base para guardar resultados de alineación (default: data/alignment/<session>)')
    parser.add_argument('--verbose', action='store_true', help='Logs detallados')
    return parser.parse_args()


def main():
    args = parse_args()

    runner = AlignmentRunner(
        radius_deg=args.radius,
        capture_time=args.capture_time,
        iterations=args.iterations,
        min_elevation=args.min_elev,
        center_freq=args.center_freq,
        sample_rate=args.sample_rate,
        settle_time=args.settle,
        no_sync=args.no_sync,
        no_calib=args.no_calib,
        host=args.host,
        port=args.port,
        device=args.device,
        sdr_host=args.sdr_host,
        sdr_port=args.sdr_port,
        observer_config=args.observer_config,
        verbose=args.verbose,
        output_base=args.output_base,
    )

    try:
        exit_code = asyncio.run(runner.run())
    except KeyboardInterrupt:
        print("Cancelado por usuario")
        exit_code = 130
    except Exception as e:
        print(f"Error inesperado: {e}")
        exit_code = 1

    return exit_code


if __name__ == '__main__':
    raise SystemExit(main())
