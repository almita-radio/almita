#!/usr/bin/env python3
"""
HI Spectrum Analyzer - Visualización y análisis de espectros calibrados

Muestra espectros en dB y Kelvin, con información sobre el proceso de calibración.

Usage:
    python3 analyze_spectra.py --spectra data/iq/Target-YYYYMMDD-HH:MM:SS/spectrum/ \
        --calibration data/calibration/YYYYMMDD-HH:MM:SS/
"""

import argparse
import json
import h5py
import numpy as np
from pathlib import Path
import sys


class SpectrumAnalyzer:
    def __init__(self, spectra_dir, calibration_dir):
        self.spectra_dir = Path(spectra_dir)
        self.calibration_dir = Path(calibration_dir)
        self.calibration = None
    
    def load_calibration_info(self):
        """Cargar información de calibración"""
        cal_json = self.calibration_dir / 'calibration_results.json'
        if cal_json.exists():
            with open(cal_json) as f:
                self.calibration = json.load(f)
    
    def kelvin_to_db(self, tb_kelvin, ref_temp=290.0):
        """
        Convertir temperatura de brillo a dB
        
        P_dB = 10 * log10(Tb / T_ref)
        T_ref típicamente = 290K (temperatura ambiente)
        
        Args:
            tb_kelvin: Temperatura en Kelvin
            ref_temp: Temperatura de referencia (default 290K)
        
        Returns:
            Potencia en dB relativa a T_ref
        """
        # Evitar log de valores negativos o cero
        tb_safe = np.clip(tb_kelvin, 1e-10, None)
        return 10 * np.log10(tb_safe / ref_temp)
    
    def analyze_spectrum(self, spectrum_file):
        """Analizar un archivo de espectro individual"""
        with h5py.File(spectrum_file, 'r') as f:
            freq = f['frequencies_hz'][:]
            tb = f['tb_kelvin'][:]
            metadata = {key: f.attrs[key] for key in f.attrs.keys()}
        
        # Convertir a dB
        power_db = self.kelvin_to_db(tb)
        
        # Estadísticas robustas (ignorar outliers extremos)
        # Usar percentiles para obtener rango típico
        tb_p05 = np.percentile(tb, 5)
        tb_p50 = np.percentile(tb, 50)  # Mediana
        tb_p95 = np.percentile(tb, 95)
        
        tb_typical_mask = (tb >= tb_p05) & (tb <= tb_p95)
        tb_typical = tb[tb_typical_mask]
        
        # Estadísticas en dB
        db_min = np.min(power_db[tb_typical_mask])
        db_max = np.max(power_db[tb_typical_mask])
        db_median = np.median(power_db[tb_typical_mask])
        db_std = np.std(power_db[tb_typical_mask])
        
        result = {
            'filename': spectrum_file.name,
            'target_ra': metadata.get('target_ra_hours', 'N/A'),
            'target_dec': metadata.get('target_dec_degrees', 'N/A'),
            'frequencies_mhz': freq / 1e6,
            'tb_kelvin': tb,
            'power_db': power_db,
            'metadata': metadata,
            'stats': {
                'tb_median': tb_p50,
                'tb_p05': tb_p05,
                'tb_p95': tb_p95,
                'tb_typical_mean': np.mean(tb_typical),
                'tb_typical_std': np.std(tb_typical),
                'db_median': db_median,
                'db_range': db_max - db_min,
                'db_std': db_std
            }
        }
        
        return result
    
    def print_calibration_explanation(self):
        """Explicar el método de calibración Y-factor"""
        print("=" * 80)
        print("📚 MÉTODO DE CALIBRACIÓN Y-FACTOR")
        print("=" * 80)
        print()
        print("La calibración convierte voltajes del SDR → Temperatura de Brillo (Kelvin)")
        print()
        print("🔬 Proceso de tres puntos:")
        print()
        print("  1️⃣  HOT LOAD (fuente caliente conocida)")
        if self.calibration:
            print(f"      • Coordenadas: RA={self.calibration['hot']['point']['ra_hours']:.2f}h "
                  f"DEC={self.calibration['hot']['point']['dec_deg']:.1f}°")
            print(f"      • Tb esperada: {self.calibration['hot']['point']['tb_kelvin']:.1f} K")
        print("      • Mide potencia P_hot del cielo en región brillante de HI")
        print()
        print("  2️⃣  COLD LOAD (fuente fría conocida)")
        if self.calibration:
            print(f"      • Coordenadas: RA={self.calibration['cold']['point']['ra_hours']:.2f}h "
                  f"DEC={self.calibration['cold']['point']['dec_deg']:.1f}°")
            print(f"      • Tb esperada: {self.calibration['cold']['point']['tb_kelvin']:.1f} K")
        print("      • Mide potencia P_cold del cielo en región fría (polo galáctico)")
        print()
        print("  3️⃣  LOAD (resistencia 50Ω)")
        print("      • Tb esperada: Ruido térmico del resistor + Tsys")
        print("      • Mide P_load = ruido puro del sistema")
        print()
        print("📐 Fórmula de calibración:")
        print()
        print("      Tb_obs = (P_obs - P_cold) / (P_hot - P_cold) × (Tb_hot - Tb_cold) + Tb_cold")
        print()
        print("  Donde:")
        print("    • P_obs  = Potencia medida en la observación")
        print("    • P_hot  = Potencia medida en HOT calibration")
        print("    • P_cold = Potencia medida en COLD calibration")
        print("    • Tb_hot, Tb_cold = Temperaturas conocidas del catálogo HI")
        print()
        print("🎯 Resultado: Cada observación tiene Tb en Kelvin (unidades físicas)")
        print()
        print("=" * 80)
        print()
    
    def print_system_noise_info(self):
        """Explicar el ruido del sistema sin antena"""
        print("=" * 80)
        print("📡 CONFIGURACIÓN: SDR SIN ANTENA (SMA PELADO)")
        print("=" * 80)
        print()
        print("⚠️  Sin antena conectada, solo mides:")
        print()
        print("  • Ruido térmico del receptor (Tsys)")
        print("  • Ruido térmico del cable coaxial (~290K)")
        print("  • Ruido electrónico del SDR")
        print()
        print("🌡️  Temperatura del sistema típica (Tsys):")
        print()
        print("  • RTL-SDR sin antena: ~200-400K")
        print("  • Temperatura ambiente: ~290K (17°C)")
        print("  • Ruido térmico: kT × B")
        print("     - k = 1.38×10⁻²³ J/K (Boltzmann)")
        print("     - T = temperatura (K)")
        print("     - B = ancho de banda (Hz)")
        print()
        print("📊 Valores esperados:")
        print()
        print("  • Tb mediana: 50-300K → ruido del sistema")
        print("  • Variaciones: ±5-10K → fluctuaciones estadísticas")
        print("  • Outliers extremos: artefactos de calibración con ruido puro")
        print()
        print("✅ Para mediciones reales de HI:")
        print("  → Conectar antena direccional")
        print("  → Apuntar a fuentes conocidas (Cygnus, Galactic Center)")
        print("  → Esperar Tb = 50-150K en regiones HI intensas")
        print()
        print("=" * 80)
        print()
    
    def analyze_all(self):
        """Analizar todos los espectros"""
        print("=" * 80)
        print("🔬 ANÁLISIS DE ESPECTROS CALIBRADOS")
        print("=" * 80)
        print(f"Directorio: {self.spectra_dir}")
        print("=" * 80)
        print()
        
        # Cargar info de calibración
        self.load_calibration_info()
        
        # Explicaciones
        self.print_calibration_explanation()
        self.print_system_noise_info()
        
        # Buscar archivos de espectro
        spectrum_files = sorted(self.spectra_dir.glob('*_spectrum.h5'))
        
        if not spectrum_files:
            print(f"❌ No se encontraron espectros en {self.spectra_dir}")
            return
        
        print("=" * 80)
        print(f"📊 ESPECTROS ENCONTRADOS: {len(spectrum_files)}")
        print("=" * 80)
        print()
        
        results = []
        for i, spectrum_file in enumerate(spectrum_files, 1):
            print(f"[{i}/{len(spectrum_files)}] {spectrum_file.name}")
            result = self.analyze_spectrum(spectrum_file)
            results.append(result)
            
            stats = result['stats']
            print(f"  📍 Target: RA={result['target_ra']}h DEC={result['target_dec']}°")
            print(f"  📏 Rango frecuencias: {result['frequencies_mhz'][0]:.3f} a {result['frequencies_mhz'][-1]:.3f} MHz")
            print(f"  🌡️  Temperatura de brillo (Tb):")
            print(f"     • Mediana: {stats['tb_median']:.1f} K")
            print(f"     • Rango típico (5-95%): {stats['tb_p05']:.1f} a {stats['tb_p95']:.1f} K")
            print(f"     • Media (sin outliers): {stats['tb_typical_mean']:.1f} ± {stats['tb_typical_std']:.1f} K")
            print(f"  📻 Potencia (dB relativo a 290K):")
            print(f"     • Mediana: {stats['db_median']:.2f} dB")
            print(f"     • Rango: {stats['db_range']:.2f} dB")
            print(f"     • Desviación estándar: {stats['db_std']:.2f} dB")
            print()
        
        # Estadísticas globales
        print("=" * 80)
        print("📈 ESTADÍSTICAS GLOBALES")
        print("=" * 80)
        print()
        
        all_tb_median = [r['stats']['tb_median'] for r in results]
        all_db_median = [r['stats']['db_median'] for r in results]
        
        print(f"  Espectros procesados: {len(results)}")
        print()
        print(f"  Tb mediana global: {np.mean(all_tb_median):.1f} ± {np.std(all_tb_median):.1f} K")
        print(f"  dB mediano global: {np.mean(all_db_median):.2f} ± {np.std(all_db_median):.2f} dB")
        print()
        print(f"  Tb mínima: {np.min(all_tb_median):.1f} K")
        print(f"  Tb máxima: {np.max(all_tb_median):.1f} K")
        print()
        
        # Interpretación
        print("=" * 80)
        print("💡 INTERPRETACIÓN")
        print("=" * 80)
        print()
        
        mean_tb = np.mean(all_tb_median)
        
        if mean_tb < 100:
            print("  ✅ Valores de Tb consistentes con ruido del sistema (50-100K)")
            print("     → Medición válida de Tsys sin señal astronómica")
        elif mean_tb < 300:
            print("  ⚠️  Valores de Tb moderados (100-300K)")
            print("     → Sistema con algo de ganancia o temperatura ambiente alta")
        else:
            print("  ❌ Valores de Tb muy altos (>300K)")
            print("     → Posible problema en calibración o saturación")
        
        print()
        print("  📡 RECUERDA: Sin antena conectada, solo mides ruido del receptor")
        print()
        print("  Para detectar HI real del cielo:")
        print("    1. Conectar antena direccional (Yagi, dipolo, etc.)")
        print("    2. Apuntar a regiones HI conocidas")
        print("    3. Buscar pico espectral cerca de 1420 MHz")
        print("    4. Esperar Tb = 50-150K en Cygnus, GC, etc.")
        print()
        print("=" * 80)
        
        return results


def main():
    parser = argparse.ArgumentParser(
        description='Analizar espectros calibrados (dB + Kelvin)',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('--spectra', required=True,
                       help='Directorio con espectros procesados (*_spectrum.h5)')
    parser.add_argument('--calibration', required=True,
                       help='Directorio con calibración (calibration_results.json)')
    
    args = parser.parse_args()
    
    analyzer = SpectrumAnalyzer(
        spectra_dir=args.spectra,
        calibration_dir=args.calibration
    )
    
    analyzer.analyze_all()


if __name__ == '__main__':
    main()
