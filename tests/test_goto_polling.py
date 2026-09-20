#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Test script for GOTO polling functionality
Tests the INDITelescopeControl class directly with multiple GOTO commands
"""

import asyncio
import sys
from indi_telescope_control import INDITelescopeControl
from datetime import datetime

async def test_goto_polling():
    """Test GOTO with state polling"""
    
    print("="*80)
    print("TEST: GOTO con Polling de Estado")
    print("="*80)
    print()
    
    # Configuración
    host = "localhost"
    port = 7624
    device = "Telescope Simulator"
    verbose = True  # Cambiar a False para salida concisa
    
    print(f"Configuración:")
    print(f"  Host: {host}")
    print(f"  Port: {port}")
    print(f"  Device: {device}")
    print(f"  Verbose: {verbose}")
    print()
    
    # Crear controlador
    telescope = INDITelescopeControl(
        host=host,
        port=port,
        device_name=device,
        verbose=verbose
    )
    
    # Conectar
    print("🔌 Conectando al servidor INDI...")
    if not await telescope.connect():
        print("❌ ERROR: No se pudo conectar al servidor INDI")
        return False
    
    print("✅ Conectado exitosamente")
    print()
    
    # Puntos de prueba (3 GOTOs diferentes)
    test_points = [
        {"name": "Punto 1", "ra": 5.9033, "dec": 7.16},    # 05:54:12, +07:09:36
        {"name": "Punto 2", "ra": 5.92, "dec": 7.41},       # 05:55:12, +07:24:36
        {"name": "Punto 3", "ra": 5.9367, "dec": 7.66},     # 05:56:12, +07:39:36
    ]
    
    total_time = 0
    
    for i, point in enumerate(test_points, 1):
        print("="*80)
        print(f"TEST {i}/3: {point['name']}")
        print(f"  Target: RA={point['ra']:.4f}h, DEC={point['dec']:.2f}°")
        print("="*80)
        
        start = datetime.now()
        
        # Ejecutar GOTO
        success = await telescope.goto(point['ra'], point['dec'])
        
        end = datetime.now()
        elapsed = (end - start).total_seconds()
        total_time += elapsed
        
        if success:
            print(f"✅ GOTO completado en {elapsed:.2f}s")
        else:
            print(f"❌ GOTO falló")
            return False
        
        print()
        
        # Pausa entre pruebas
        if i < len(test_points):
            print("⏸️  Pausa de 1s antes del siguiente GOTO...")
            await asyncio.sleep(1)
            print()
    
    # Resumen
    print("="*80)
    print("✅ RESUMEN DE PRUEBAS")
    print("="*80)
    print(f"  Total de GOTOs: {len(test_points)}")
    print(f"  Todos exitosos: ✅")
    print(f"  Tiempo total: {total_time:.2f}s")
    print(f"  Tiempo promedio por GOTO: {total_time/len(test_points):.2f}s")
    print()
    
    # Cerrar conexión
    if telescope.writer:
        telescope.writer.close()
        await telescope.writer.wait_closed()
    
    print("🔌 Conexión cerrada")
    
    return True


async def main():
    """Entry point"""
    try:
        success = await test_goto_polling()
        sys.exit(0 if success else 1)
    except KeyboardInterrupt:
        print()
        print("⚠️  Test interrumpido por el usuario")
        sys.exit(130)
    except Exception as e:
        print(f"❌ ERROR: {e}")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    asyncio.run(main())
