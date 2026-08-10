#!/usr/bin/env python3
"""Mount control helper

Script ligero para mover, sincronizar y controlar tracking de la montura vía INDI.

Este script usa la clase INDITelescopeControl (ya existente) y provee un CLI limpio
para:
  - Consultar coordenadas actuales
  - Ejecutar GOTO (movimiento)
  - Ejecutar SYNC (corregir posición sin mover)
  - Encender/apagar tracking

La idea es mantener este código separado de "capture" y de las funciones de
captura/SDR, para no mezclar responsabilidades.

USO BÁSICO:
  python mount_control.py --status
  python mount_control.py --goto 18:36:56.3 +38:47:01
  python mount_control.py --sync 18:36:56.3 +38:47:01
  python mount_control.py --track on

"""

import argparse
import re
import sys
from typing import Optional, Tuple

from indi_telescope_control import INDITelescopeControl


def parse_hms(value: str) -> Optional[float]:
    """Parse a string like HH:MM:SS(.sss) or decimal hours into float hours."""
    try:
        # Decimal hours
        return float(value)
    except ValueError:
        pass

    m = re.match(r"^([0-9]+):([0-9]+):([0-9.]+)$", value)
    if not m:
        return None
    h = float(m.group(1))
    mnt = float(m.group(2))
    s = float(m.group(3))
    return h + mnt / 60.0 + s / 3600.0


def parse_dms(value: str) -> Optional[float]:
    """Parse a string like [+/-]DD:MM:SS(.sss) or decimal degrees into float degrees."""
    try:
        # Decimal degrees
        return float(value)
    except ValueError:
        pass

    m = re.match(r"^([+-]?)([0-9]+):([0-9]+):([0-9.]+)$", value)
    if not m:
        return None

    sign = -1 if m.group(1) == "-" else 1
    deg = float(m.group(2))
    mnt = float(m.group(3))
    s = float(m.group(4))
    return sign * (deg + mnt / 60.0 + s / 3600.0)


def parse_coordinates(ra_arg: str, dec_arg: str) -> Tuple[Optional[float], Optional[float]]:
    ra = parse_hms(ra_arg)
    dec = parse_dms(dec_arg)
    return ra, dec


def main():
    parser = argparse.ArgumentParser(
        description="Control de montura INDI (movimiento, sync, tracking)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Ejemplos:
  python mount_control.py --status
  python mount_control.py --goto 18:36:56.3 +38:47:01
  python mount_control.py --sync 18:36:56.3 +38:47:01
  python mount_control.py --track on

Nota: RA puede ingresarse como decimal (18.615) o en formato HH:MM:SS.
      DEC puede ingresarse como decimal (-22.5) o en formato [+/-]DD:MM:SS.
"""
    )

    parser.add_argument("--host", default="localhost",
                        help="Servidor INDI (default: localhost)")
    parser.add_argument("--port", type=int, default=7624,
                        help="Puerto INDI (default: 7624)")
    parser.add_argument("--device", default="Telescope Simulator",
                        help="Nombre del dispositivo INDI")
    parser.add_argument("--verbose", "-v", action="store_true",
                        help="Modo verbose (muestra XML/depuración)")

    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--status", action="store_true",
                       help="Mostrar coordenadas actuales")
    group.add_argument("--goto", nargs=2, metavar=("RA", "DEC"),
                       help="GOTO a coordenadas (RA en horas, DEC en grados)")
    group.add_argument("--sync", nargs=2, metavar=("RA", "DEC"),
                       help="SYNC (actualiza posición sin mover)")
    group.add_argument("--track", choices=["on", "off"],
                       help="Encender/apagar tracking")

    args = parser.parse_args()

    controller = INDITelescopeControl(
        host=args.host,
        port=args.port,
        device_name=args.device,
        verbose=args.verbose,
    )

    try:
        if not controller.connect():
            sys.exit(1)

        if args.status:
            awaitable = controller.get_coordinates()
            # Python <3.7 can't await in non-async, so use asyncio.run
            import asyncio
            asyncio.run(awaitable)
            return

        if args.goto:
            ra, dec = parse_coordinates(args.goto[0], args.goto[1])
            if ra is None or dec is None:
                print("ERROR: Coordenadas inválidas. Usa formato HH:MM:SS o decimal.")
                sys.exit(1)
            import asyncio
            asyncio.run(controller.goto(ra, dec))
            return

        if args.sync:
            ra, dec = parse_coordinates(args.sync[0], args.sync[1])
            if ra is None or dec is None:
                print("ERROR: Coordenadas inválidas. Usa formato HH:MM:SS o decimal.")
                sys.exit(1)
            import asyncio
            asyncio.run(controller.sync(ra, dec))
            return

        if args.track:
            enable = args.track == "on"
            import asyncio
            asyncio.run(controller.set_tracking(enable))
            return

    except KeyboardInterrupt:
        print("\nOperación cancelada por el usuario.")
        sys.exit(130)


if __name__ == "__main__":
    main()
