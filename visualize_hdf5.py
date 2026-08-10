#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Visualizador de capturas HDF5 - Análisis espectral
Muestra espectro de potencia, forma temporal y estadísticas
"""

import numpy as np
import h5py
import matplotlib.pyplot as plt
from datetime import datetime
import sys
import os

def visualize_hdf5(filename, display_length=None, save_plot=False):
    """
    Visualiza datos de captura HDF5
    
    Args:
        filename: Ruta al archivo HDF5
        display_length: Número de muestras a mostrar (None = todos)
        save_plot: Si True, guarda el plot como PNG
    """
    
    print("="*80)
    print("📊 VISUALIZADOR DE CAPTURAS HDF5")
    print("="*80)
    print()
    
    if not os.path.exists(filename):
        print(f"❌ ERROR: Archivo no encontrado: {filename}")
        return
    
    # Leer HDF5
    with h5py.File(filename, 'r') as f:
        # Metadata
        print("📍 METADATA:")
        print(f"  Archivo: {os.path.basename(filename)}")
        print(f"  Target: RA={f.attrs.get('target_ra_hms', 'N/A')} DEC={f.attrs.get('target_dec_dms', 'N/A')}")
        print(f"  Frecuencia: {f.attrs['center_frequency_hz']/1e6:.6f} MHz")
        print(f"  Sample Rate: {f.attrs['sample_rate_hz']/1e6:.2f} MS/s")
        print(f"  Muestras: {f.attrs['num_samples']:,}")
        print(f"  Duración: {f.attrs['duration_seconds']:.1f} s")
        print(f"  Tiempo UTC: {f.attrs['capture_start_utc']}")
        print(f"  Observador: {f.attrs.get('observer_name', 'N/A')}")
        print()
        
        # Leer datos I/Q
        i_samples = f['i_samples'][:]
        q_samples = f['q_samples'][:]
        
        sample_rate = f.attrs['sample_rate_hz']
        center_freq = f.attrs['center_frequency_hz']
        num_samples = len(i_samples)
        
    # Limitar cantidad de muestras si se especifica
    if display_length and display_length < num_samples:
        i_samples = i_samples[:display_length]
        q_samples = q_samples[:display_length]
        num_samples = display_length
    
    # Convertir a valores centrados en 0 (uint8 127.5 es el centro)
    i_centered = i_samples.astype(np.float32) - 127.5
    q_centered = q_samples.astype(np.float32) - 127.5
    
    # Crear señal compleja
    iq_complex = i_centered + 1j * q_centered
    
    # Estadísticas
    print("📈 ESTADÍSTICAS:")
    print(f"  I samples - Media: {np.mean(i_samples):.2f}, Std: {np.std(i_samples):.2f}, Min: {np.min(i_samples)}, Max: {np.max(i_samples)}")
    print(f"  Q samples - Media: {np.mean(q_samples):.2f}, Std: {np.std(q_samples):.2f}, Min: {np.min(q_samples)}, Max: {np.max(q_samples)}")
    print(f"  Potencia promedio: {np.mean(np.abs(iq_complex)**2):.2f}")
    print()
    
    # Calcular FFT
    print("🔄 Calculando espectro de potencia...")
    fft_size = min(num_samples, 2**20)  # Max 1M puntos para FFT
    iq_fft = iq_complex[:fft_size]
    
    # FFT
    spectrum = np.fft.fftshift(np.fft.fft(iq_fft))
    power_spectrum = 10 * np.log10(np.abs(spectrum)**2 + 1e-10)  # dB scale
    
    # Eje de frecuencias
    freqs = np.fft.fftshift(np.fft.fftfreq(fft_size, 1/sample_rate))
    freqs_mhz = (center_freq + freqs) / 1e6
    
    print(f"  FFT size: {fft_size:,} puntos")
    print(f"  Resolución: {sample_rate/fft_size:.2f} Hz/bin")
    print()
    
    # Crear figura
    fig = plt.figure(figsize=(16, 10))
    fig.suptitle(f'Análisis de Captura HDF5 - {os.path.basename(filename)}', fontsize=14, fontweight='bold')
    
    # 1. Espectro de potencia
    ax1 = plt.subplot(2, 2, 1)
    ax1.plot(freqs_mhz, power_spectrum, linewidth=0.5)
    ax1.set_xlabel('Frecuencia (MHz)')
    ax1.set_ylabel('Potencia (dB)')
    ax1.set_title('Espectro de Potencia (FFT)')
    ax1.grid(True, alpha=0.3)
    ax1.axvline(center_freq/1e6, color='r', linestyle='--', linewidth=1, label=f'Centro: {center_freq/1e6:.6f} MHz')
    ax1.legend()
    
    # 2. Forma de onda temporal (primeros 10000 samples)
    ax2 = plt.subplot(2, 2, 2)
    time_samples = min(10000, num_samples)
    time_axis = np.arange(time_samples) / sample_rate * 1000  # ms
    ax2.plot(time_axis, i_samples[:time_samples], label='I', alpha=0.7, linewidth=0.5)
    ax2.plot(time_axis, q_samples[:time_samples], label='Q', alpha=0.7, linewidth=0.5)
    ax2.set_xlabel('Tiempo (ms)')
    ax2.set_ylabel('Amplitud (uint8)')
    ax2.set_title(f'Forma de Onda Temporal (primeros {time_samples} samples)')
    ax2.grid(True, alpha=0.3)
    ax2.legend()
    
    # 3. Histograma I/Q
    ax3 = plt.subplot(2, 2, 3)
    ax3.hist(i_samples, bins=50, alpha=0.5, label='I', color='blue')
    ax3.hist(q_samples, bins=50, alpha=0.5, label='Q', color='orange')
    ax3.set_xlabel('Amplitud (uint8)')
    ax3.set_ylabel('Frecuencia')
    ax3.set_title('Distribución de Amplitudes I/Q')
    ax3.grid(True, alpha=0.3)
    ax3.legend()
    ax3.axvline(127.5, color='r', linestyle='--', linewidth=1, label='Centro teórico')
    
    # 4. Zoom del espectro alrededor del centro (±1 MHz)
    ax4 = plt.subplot(2, 2, 4)
    center_idx = len(freqs_mhz) // 2
    zoom_range = int(1e6 / (sample_rate / fft_size))  # ±1 MHz
    zoom_start = max(0, center_idx - zoom_range)
    zoom_end = min(len(freqs_mhz), center_idx + zoom_range)
    
    ax4.plot(freqs_mhz[zoom_start:zoom_end], power_spectrum[zoom_start:zoom_end], linewidth=1)
    ax4.set_xlabel('Frecuencia (MHz)')
    ax4.set_ylabel('Potencia (dB)')
    ax4.set_title('Espectro Zoom (±1 MHz del centro)')
    ax4.grid(True, alpha=0.3)
    ax4.axvline(center_freq/1e6, color='r', linestyle='--', linewidth=1, label='HI line')
    ax4.legend()
    
    plt.tight_layout()
    
    # Guardar si se solicita
    if save_plot:
        output_name = filename.replace('.h5', '_spectrum.png')
        plt.savefig(output_name, dpi=150, bbox_inches='tight')
        print(f"💾 Plot guardado: {output_name}")
    
    print("✅ Visualización lista")
    print("   Cierra la ventana para continuar...")
    plt.show()


if __name__ == '__main__':
    if len(sys.argv) < 2:
        print("Uso: python3 visualize_hdf5.py <archivo.h5> [--save]")
        print()
        print("Opciones:")
        print("  --save    Guarda el plot como PNG")
        print()
        print("Ejemplo:")
        print("  python3 visualize_hdf5.py data/visual_test_0001.h5")
        print("  python3 visualize_hdf5.py data/visual_test_0001.h5 --save")
        sys.exit(1)
    
    filename = sys.argv[1]
    save_plot = '--save' in sys.argv
    
    visualize_hdf5(filename, save_plot=save_plot)
