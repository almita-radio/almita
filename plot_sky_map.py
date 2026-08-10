#!/usr/bin/env python3
"""
HI Sky Map Plotter - Visualización de observaciones en coordenadas ecuatoriales

Genera mapas de temperatura de brillo en el cielo:
  1. Scatter plot exacto (punto por punto)
  2. Mapa interpolado suavizado (más bonito)

Usage:
    python3 plot_sky_map.py --spectra data/.../spectra/ --output sky_map.png
"""

import argparse
import h5py
import numpy as np
from pathlib import Path
import sys

try:
    import matplotlib
    matplotlib.use('Agg')  # Backend sin display para RPi
    import matplotlib.pyplot as plt
    from matplotlib.colors import LinearSegmentedColormap, Normalize
    from matplotlib.ticker import FuncFormatter
    from matplotlib.patches import Rectangle
    from scipy.interpolate import griddata
except ImportError as e:
    print(f"❌ Error: {e}")
    print("Instala dependencias: pip install matplotlib scipy")
    sys.exit(1)


class SkyMapper:
    def __init__(self, spectra_dir, output_dir=None, output_base=None):
        self.spectra_dir = Path(spectra_dir)
        
        # If output_base is provided, place plots under output_base/spectrum/
        if output_base:
            self.output_dir = Path(output_base) / 'spectrum'
        elif output_dir:
            self.output_dir = Path(output_dir)
        else:
            self.output_dir = self.spectra_dir
        
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.data_points = []
        self.observer_lat = None
        self.observer_lon = None
        self.observer_name = None
        self.capture_start = None
        self.capture_end = None
    
    def load_spectra(self):
        """Cargar todos los espectros y extraer coordenadas + Tb"""
        print("=" * 80)
        print("📡 CARGANDO DATOS DE ESPECTROS")
        print("=" * 80)
        print()
        
        spectrum_files = sorted(self.spectra_dir.glob('*_spectrum.h5'))
        
        if not spectrum_files:
            print(f"❌ No se encontraron espectros en {self.spectra_dir}")
            return False
        
        print(f"Encontrados: {len(spectrum_files)} espectros")
        print()
        
        for i, spectrum_file in enumerate(spectrum_files, 1):
            try:
                with h5py.File(spectrum_file, 'r') as f:
                    tb = f['tb_kelvin'][:]
                    
                    # Extraer coordenadas
                    ra = f.attrs.get('target_ra_hours', None)
                    dec = f.attrs.get('target_dec_degrees', None)
                    
                    # Extraer info del observatorio (primera vez)
                    if self.observer_lat is None:
                        self.observer_lat = f.attrs.get('observer_latitude_deg', None)
                        self.observer_lon = f.attrs.get('observer_longitude_deg', None)
                        self.observer_name = f.attrs.get('observer_name', 'Unknown')
                    
                    # Extraer timestamps
                    capture_time = f.attrs.get('capture_start_utc', None)
                    if capture_time:
                        if self.capture_start is None:
                            self.capture_start = capture_time
                        self.capture_end = capture_time
                    
                    if ra is None or dec is None:
                        print(f"  ⚠️  [{i}] {spectrum_file.name}: Sin coordenadas, saltando...")
                        continue
                    
                    # Estadísticas robustas (sin outliers)
                    tb_p05 = np.percentile(tb, 5)
                    tb_p95 = np.percentile(tb, 95)
                    tb_mask = (tb >= tb_p05) & (tb <= tb_p95)
                    
                    tb_median = np.median(tb[tb_mask])
                    tb_mean = np.mean(tb[tb_mask])
                    tb_std = np.std(tb[tb_mask])
                    
                    self.data_points.append({
                        'filename': spectrum_file.name,
                        'ra': ra,
                        'dec': dec,
                        'tb_median': tb_median,
                        'tb_mean': tb_mean,
                        'tb_std': tb_std
                    })
                    
                    print(f"  ✓ [{i}] RA={ra:.3f}h DEC={dec:.1f}° → Tb={tb_median:.1f}K")
            
            except Exception as e:
                print(f"  ❌ [{i}] {spectrum_file.name}: Error - {e}")
        
        print()
        print(f"✅ Cargados {len(self.data_points)} puntos válidos")
        print("=" * 80)
        print()
        
        return len(self.data_points) > 0
    
    def create_custom_colormap(self):
        """Crear colormap custom para radioastronomía (azul → rojo)"""
        colors = ['#0000CC', '#00FFFF', '#00FF00', '#FFFF00', '#FF0000']
        n_bins = 256
        cmap = LinearSegmentedColormap.from_list('radio', colors, N=n_bins)
        return cmap
    
    def kelvin_to_db(self, tb_kelvin, ref_temp=290.0):
        """Convertir temperatura de brillo a dB"""
        tb_safe = np.clip(tb_kelvin, 1e-10, None)
        return 10 * np.log10(tb_safe / ref_temp)
    
    def create_info_text(self, tb_array):
        """
        Crear texto informativo para la viñeta con coordenadas y estadísticas
        
        Args:
            tb_array: Array de temperaturas de brillo
        
        Returns:
            String formateado con toda la información
        """
        # Extraer coordenadas
        ra_vals = np.array([p['ra'] for p in self.data_points])
        dec_vals = np.array([p['dec'] for p in self.data_points])
        ra_deg = ra_vals * 15.0
        
        # Estadísticas de temperatura
        tb_min = np.nanmin(tb_array)
        tb_max = np.nanmax(tb_array)
        tb_median = np.nanmedian(tb_array)
        tb_mean = np.nanmean(tb_array)
        tb_std = np.nanstd(tb_array)
        
        # Convertir a dB
        db_min = self.kelvin_to_db(tb_min)
        db_max = self.kelvin_to_db(tb_max)
        db_median = self.kelvin_to_db(tb_median)
        
        # Construir texto
        info_lines = []
        
        # Observation time
        if self.capture_start or self.capture_end:
            info_lines.append("OBSERVATION:")
            if self.capture_start:
                # Extraer solo fecha y hora
                timestamp_str = str(self.capture_start)
                if 'T' in timestamp_str:
                    date_part, time_part = timestamp_str.split('T')
                    time_clean = time_part.split('+')[0].split('.')[0]  # Remover timezone y microsegundos
                    info_lines.append(f"  Start: {date_part} {time_clean} UTC")
                else:
                    info_lines.append(f"  Start: {self.capture_start}")
            if self.capture_end and self.capture_end != self.capture_start:
                timestamp_str = str(self.capture_end)
                if 'T' in timestamp_str:
                    date_part, time_part = timestamp_str.split('T')
                    time_clean = time_part.split('+')[0].split('.')[0]
                    info_lines.append(f"  End: {date_part} {time_clean} UTC")
            info_lines.append("")
        
        # Sky coordinates
        info_lines.append("SKY COORDINATES:")
        info_lines.append(f"  RA: {ra_deg.min():.2f}° - {ra_deg.max():.2f}°")
        info_lines.append(f"  DEC: {dec_vals.min():.1f}° - {dec_vals.max():.1f}°")
        info_lines.append("")
        
        # Observatory
        if self.observer_lat is not None and self.observer_lon is not None:
            info_lines.append("OBSERVATORY:")
            info_lines.append(f"  {self.observer_name}")
            info_lines.append(f"  Lat: {self.observer_lat:.4f}°")
            info_lines.append(f"  Lon: {self.observer_lon:.4f}°")
            info_lines.append("")
        
        # Temperature (K)
        info_lines.append("TEMPERATURE (K):")
        info_lines.append(f"  Min: {tb_min:.1f} K")
        info_lines.append(f"  Max: {tb_max:.1f} K")
        info_lines.append(f"  Median: {tb_median:.1f} K")
        info_lines.append(f"  Mean: {tb_mean:.1f} ± {tb_std:.1f} K")
        info_lines.append("")
        
        # Power (dB)
        info_lines.append("POWER (dB @ 290K):")
        info_lines.append(f"  Min: {db_min:.2f} dB")
        info_lines.append(f"  Max: {db_max:.2f} dB")
        info_lines.append(f"  Median: {db_median:.2f} dB")
        
        return "\n".join(info_lines)

    def _format_degree_ticks(self, ax, decimals=1):
        fmt = f"{{:.{decimals}f}}°"
        formatter = FuncFormatter(lambda v, pos: fmt.format(v))
        ax.xaxis.set_major_formatter(formatter)
        ax.yaxis.set_major_formatter(formatter)

    def _calc_square_marker_size(self, ax, ra_deg, dec_deg, dpi):
        ra_unique = np.unique(np.round(ra_deg, 6))
        dec_unique = np.unique(np.round(dec_deg, 6))
        if len(ra_unique) > 1:
            ra_step = np.min(np.diff(np.sort(ra_unique)))
        else:
            ra_step = 1.0
        if len(dec_unique) > 1:
            dec_step = np.min(np.diff(np.sort(dec_unique)))
        else:
            dec_step = 1.0

        mid_ra = float(np.mean(ra_unique))
        mid_dec = float(np.mean(dec_unique))
        ax.figure.canvas.draw()
        p0 = ax.transData.transform((mid_ra, mid_dec))
        p1 = ax.transData.transform((mid_ra + ra_step, mid_dec))
        p2 = ax.transData.transform((mid_ra, mid_dec + dec_step))
        dx = abs(p1[0] - p0[0])
        dy = abs(p2[1] - p0[1])
        size_pixels = min(dx, dy)
        size_points = size_pixels * 72.0 / dpi
        return size_points ** 2

    def _get_contrast_norm(self, values, low_pct=5.0, high_pct=95.0):
        vmin = float(np.nanpercentile(values, low_pct))
        vmax = float(np.nanpercentile(values, high_pct))
        if vmin == vmax:
            vmin = float(np.nanmin(values))
            vmax = float(np.nanmax(values))
        return Normalize(vmin=vmin, vmax=vmax)

    def _info_box_center_x(self, cbar):
        x_left = cbar.ax.get_position().x1
        return x_left + (1.0 - x_left) / 2.0

    def _info_box_left_center_x(self, ax):
        x_right = ax.get_position().x0
        return x_right / 2.0
    
    def plot_scatter(self, output_file='plot_scatter.png'):
        """Generar scatter plot exacto punto por punto"""
        print("=" * 80)
        print("📊 GENERANDO SCATTER PLOT (VALORES EXACTOS)")
        print("=" * 80)
        print()
        
        if not self.data_points:
            print("❌ No hay datos para plotear")
            return None
        
        # Extraer datos
        ra = np.array([p['ra'] for p in self.data_points])
        dec = np.array([p['dec'] for p in self.data_points])
        tb = np.array([p['tb_median'] for p in self.data_points])
        ra_deg = ra * 15.0
        
        # Crear figura con espacio a la derecha para info
        fig = plt.figure(figsize=(16, 8), dpi=150)
        ax = fig.add_axes([0.30, 0.1, 0.48, 0.8])  # mas margen izquierdo para viñeta
        
        # Tamaño de cuadrados para que toquen sin superposición
        marker_size = self._calc_square_marker_size(ax, ra_deg, dec, fig.dpi)
        
        # Crear normalización y colormap
        tb_min, tb_max = float(np.nanmin(tb)), float(np.nanmax(tb))
        norm = Normalize(vmin=tb_min, vmax=tb_max)
        cmap = self.create_custom_colormap()
        
        print(f"📊 DEBUG Color mapping:")
        print(f"   Tb range: {tb_min:.1f}K - {tb_max:.1f}K")
        print(f"   Colormap: {cmap.name if hasattr(cmap, 'name') else 'custom'}")
        
        # Calcular el tamaño de cada celda en unidades de datos
        ra_span = ra_deg.max() - ra_deg.min()
        dec_span = dec.max() - dec.min()
        ra_unique = np.unique(ra_deg)
        dec_unique = np.unique(dec)
        
        # Estimar el tamaño de celda basado en la separación de puntos
        if len(ra_unique) > 1:
            cell_width = np.min(np.diff(np.sort(ra_unique))) * 1.02  # Ligeramente más grande para que toquen
        else:
            cell_width = 1.0
        
        if len(dec_unique) > 1:
            cell_height = np.min(np.diff(np.sort(dec_unique))) * 1.02
        else:
            cell_height = 1.0
        
        # Dibujar rectángulos sólidos sin gaps
        for i, point in enumerate(self.data_points):
            tb_val = point['tb_median']
            color_val = norm(tb_val)
            color = cmap(color_val)
            
            # Rectángulo sólido que toca los vecinos
            rect = Rectangle((ra_deg[i] - cell_width/2, dec[i] - cell_height/2),
                           cell_width, cell_height,
                           facecolor=color, edgecolor='none', linewidth=0,
                           alpha=1.0, zorder=2)
            ax.add_patch(rect)
        
        # Crear scatter invisible solo para la colorbar
        scatter = ax.scatter(ra_deg, dec, c=tb, s=0, marker='s',
                    cmap=cmap, norm=norm, alpha=0, zorder=1)
        
        # Añadir valores de Tb (K y dB) con mejor contraste
        for i, point in enumerate(self.data_points):
            tb_k = point['tb_median']
            tb_db = self.kelvin_to_db(tb_k)
            label = f"{tb_k:.0f}K\n{tb_db:.1f}dB"
            
            # Fondo semi-transparente para legibilidad
            ax.annotate(label, 
                       (ra_deg[i], dec[i]),
                       fontsize=7, ha='center', va='center',
                       color='white', weight='bold',
                       bbox=dict(boxstyle='round,pad=0.3', facecolor='black', alpha=0.5, edgecolor='none'),
                       zorder=4)
        
        # Grid de fondo
        ax.grid(True, alpha=0.3, linestyle='--', linewidth=0.5, zorder=1)
        
        # Colorbar pegado al plot
        cbar = plt.colorbar(scatter, ax=ax, label='Brightness Temperature (K)', pad=0.01)
        cbar.ax.tick_params(labelsize=10)
        
        # Labels
        ax.set_xlabel('RA', fontsize=12, weight='bold')
        ax.set_ylabel('DEC', fontsize=12, weight='bold')
        self._format_degree_ticks(ax)
        ax.tick_params(axis='y', pad=8)
        ax.set_title('HI Map - Exact Values (Point by Point)\n' + 
                 f'N={len(self.data_points)} observations | Tb median: {np.median(tb):.1f}±{np.std(tb):.1f}K',
                     fontsize=14, weight='bold', pad=20)
        
        # Invertir eje X (RA aumenta hacia la izquierda por convención)
        ax.invert_xaxis()
        
        # Aspect ratio
        ax.set_aspect('auto')
        
        # Info box centered between left margin and plot area
        info_text = self.create_info_text(tb)
        info_x = self._info_box_left_center_x(ax)
        fig.text(info_x, 0.5, info_text, transform=fig.transFigure,
            fontsize=9.5, verticalalignment='center', horizontalalignment='center',
                family='monospace',
                bbox=dict(boxstyle='round', edgecolor='black', facecolor='white', 
                         linewidth=1, alpha=1.0, pad=0.5))
        
        # Guardar
        output_path = self.output_dir / output_file
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
        
        print(f"✅ Scatter plot guardado: {output_path}")
        print(f"   Resolución: {fig.get_size_inches()[0]*150:.0f}x{fig.get_size_inches()[1]*150:.0f} px")
        print()
        
        return output_path
    
    def plot_interpolated(self, output_file='plot_interpolated.png', 
                         grid_resolution=200, method='cubic'):
        """Generar mapa interpolado suavizado"""
        print("=" * 80)
        print("🎨 GENERANDO MAPA INTERPOLADO (SUAVIZADO)")
        print("=" * 80)
        print()
        
        if not self.data_points:
            print("❌ No hay datos para plotear")
            return None
        
        # Extraer datos
        ra = np.array([p['ra'] for p in self.data_points])
        dec = np.array([p['dec'] for p in self.data_points])
        tb = np.array([p['tb_median'] for p in self.data_points])
        ra_deg = ra * 15.0
        
        print(f"Datos: {len(ra)} puntos")
        print(f"RA range: {ra_deg.min():.2f}° - {ra_deg.max():.2f}°")
        print(f"DEC range: {dec.min():.1f}° - {dec.max():.1f}°")
        print(f"Método interpolación: {method}")
        print(f"Resolución grid: {grid_resolution}x{grid_resolution}")
        print()
        
        # Crear grid regular para interpolación
        ra_min, ra_max = ra_deg.min(), ra_deg.max()
        dec_min, dec_max = dec.min(), dec.max()
        
        # Añadir margen del 5%
        ra_margin = (ra_max - ra_min) * 0.05
        dec_margin = (dec_max - dec_min) * 0.05
        
        ra_grid = np.linspace(ra_min - ra_margin, ra_max + ra_margin, grid_resolution)
        dec_grid = np.linspace(dec_min - dec_margin, dec_max + dec_margin, grid_resolution)
        
        ra_mesh, dec_mesh = np.meshgrid(ra_grid, dec_grid)
        
        # Interpolar usando griddata
        print("Interpolando...")
        tb_interpolated = griddata((ra_deg, dec), tb, (ra_mesh, dec_mesh), 
                                   method=method, fill_value=np.nan)
        
        # Crear figura con espacio a la derecha para info
        fig = plt.figure(figsize=(16, 8), dpi=150)
        ax = fig.add_axes([0.30, 0.1, 0.48, 0.8])  # mas margen izquierdo para viñeta
        
        # Mapa interpolado con contourf suavizado - normalizacion simple min-max
        levels = 20  # Número de niveles
        norm = Normalize(vmin=float(np.nanmin(tb)), vmax=float(np.nanmax(tb)))
        level_values = np.linspace(float(np.nanmin(tb)), float(np.nanmax(tb)), levels)
        im = ax.contourf(ra_mesh, dec_mesh, tb_interpolated, 
                levels=level_values, cmap=self.create_custom_colormap(),
                norm=norm, extend='both', alpha=1.0)
        
        # Contornos con líneas
        contours = ax.contour(ra_mesh, dec_mesh, tb_interpolated, 
                             levels=10, colors='white', 
                             linewidths=0.5, alpha=0.4)
        ax.clabel(contours, inline=True, fontsize=7, fmt='%0.0fK')
        
        # Overlay: original measurements
        ax.scatter(ra_deg, dec, c='white', s=100, marker='x', 
              linewidths=2, alpha=0.8, zorder=10, label='Measurements')
        
        # Grid de fondo
        ax.grid(True, alpha=0.2, linestyle='--', linewidth=0.5, zorder=1)
        
        # Colorbar pegado al plot
        cbar = plt.colorbar(im, ax=ax, label='Brightness Temperature (K)', pad=0.01)
        cbar.ax.tick_params(labelsize=10)
        
        # Labels
        ax.set_xlabel('RA', fontsize=12, weight='bold')
        ax.set_ylabel('DEC', fontsize=12, weight='bold')
        self._format_degree_ticks(ax)
        ax.tick_params(axis='y', pad=8)
        ax.set_title('HI Map - Smoothed Interpolation\n' + 
                 f'N={len(self.data_points)} observations | Method: {method} | Grid: {grid_resolution}x{grid_resolution}',
                     fontsize=14, weight='bold', pad=20)
        
        # Invertir eje X
        ax.invert_xaxis()
        
        # Aspect ratio
        ax.set_aspect('auto')
        
        # Legend
        ax.legend(loc='upper right', fontsize=10)
        
        # Info box centered between left margin and plot area
        info_text = self.create_info_text(tb_interpolated)
        info_x = self._info_box_left_center_x(ax)
        fig.text(info_x, 0.5, info_text, transform=fig.transFigure,
            fontsize=9.5, verticalalignment='center', horizontalalignment='center',
                family='monospace',
                bbox=dict(boxstyle='round', edgecolor='black', facecolor='white', 
                         linewidth=1, alpha=1.0, pad=0.5))
        
        # Guardar
        output_path = self.output_dir / output_file
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
        
        print(f"✅ Mapa interpolado guardado: {output_path}")
        print(f"   Resolución: {fig.get_size_inches()[0]*150:.0f}x{fig.get_size_inches()[1]*150:.0f} px")
        print()
        
        return output_path
    
    def plot_combined(self, output_file='plot_both.png'):
        """Generar figura combinada con ambos plots"""
        print("=" * 80)
        print("🎨 GENERANDO VISTA COMBINADA")
        print("=" * 80)
        print()
        
        if not self.data_points:
            print("❌ No hay datos para plotear")
            return None
        
        # Extraer datos
        ra = np.array([p['ra'] for p in self.data_points])
        dec = np.array([p['dec'] for p in self.data_points])
        tb = np.array([p['tb_median'] for p in self.data_points])
        ra_deg = ra * 15.0
        
        # Crear figura con 2 subplots y espacio abajo para info
        fig = plt.figure(figsize=(20, 10), dpi=150)
        ax1 = fig.add_axes([0.05, 0.25, 0.42, 0.65])  # [left, bottom, width, height]
        ax2 = fig.add_axes([0.53, 0.25, 0.42, 0.65])
        
        # Calcular tamaño de celda para rectángulos
        ra_unique = np.unique(ra_deg)
        dec_unique = np.unique(dec)
        
        if len(ra_unique) > 1:
            cell_width = np.min(np.diff(np.sort(ra_unique))) * 1.02  # Ligeramente más grande para que toquen
        else:
            cell_width = 1.0
        
        if len(dec_unique) > 1:
            cell_height = np.min(np.diff(np.sort(dec_unique))) * 1.02
        else:
            cell_height = 1.0
        
        # === LEFT: Scatter plot con rectángulos explícitos ===
        tb_min, tb_max = float(np.nanmin(tb)), float(np.nanmax(tb))
        norm = Normalize(vmin=tb_min, vmax=tb_max)
        cmap = self.create_custom_colormap()
        
        # Rectángulos sólidos que tocan sin gaps
        for i, point in enumerate(self.data_points):
            tb_val = point['tb_median']
            color_val = norm(tb_val)
            color = cmap(color_val)
            
            rect = Rectangle((ra_deg[i] - cell_width/2, dec[i] - cell_height/2),
                           cell_width, cell_height,
                           facecolor=color, edgecolor='none', linewidth=0,
                           alpha=1.0, zorder=2)
            ax1.add_patch(rect)
        
        # Scatter invisible para colorbar
        scatter = ax1.scatter(ra_deg, dec, c=tb, s=0, marker='s',
                     cmap=cmap, norm=norm, alpha=0, zorder=1)
        
        for i, point in enumerate(self.data_points):
            tb_k = point['tb_median']
            tb_db = self.kelvin_to_db(tb_k)
            label = f"{tb_k:.0f}K\n{tb_db:.1f}dB"
            ax1.annotate(label, 
                        (ra_deg[i], dec[i]),
                        fontsize=6, ha='center', va='center',
                        color='white', weight='bold',
                        bbox=dict(boxstyle='round,pad=0.3', facecolor='black', alpha=0.5, edgecolor='none'),
                        zorder=4)
        
        ax1.grid(True, alpha=0.3, linestyle='--', linewidth=0.5)
        cbar1 = plt.colorbar(scatter, ax=ax1, label='Tb (K)', pad=0.01)
        ax1.set_xlabel('RA', fontsize=11, weight='bold')
        ax1.set_ylabel('DEC', fontsize=11, weight='bold')
        self._format_degree_ticks(ax1)
        ax1.tick_params(axis='y', pad=8)
        ax1.set_title('Exact Values\n(Point by Point)', fontsize=12, weight='bold')
        ax1.invert_xaxis()
        
        # === RIGHT: Interpolado ===
        ra_min, ra_max = ra_deg.min(), ra_deg.max()
        dec_min, dec_max = dec.min(), dec.max()
        ra_margin = (ra_max - ra_min) * 0.05
        dec_margin = (dec_max - dec_min) * 0.05
        
        ra_grid = np.linspace(ra_min - ra_margin, ra_max + ra_margin, 200)
        dec_grid = np.linspace(dec_min - dec_margin, dec_max + dec_margin, 200)
        ra_mesh, dec_mesh = np.meshgrid(ra_grid, dec_grid)
        
        tb_interpolated = griddata((ra_deg, dec), tb, (ra_mesh, dec_mesh), 
                                   method='cubic', fill_value=np.nan)
        
        level_values = np.linspace(float(np.nanmin(tb)), float(np.nanmax(tb)), 20)
        im = ax2.contourf(ra_mesh, dec_mesh, tb_interpolated, 
                 levels=level_values, cmap=self.create_custom_colormap(),
                 norm=norm, extend='both', alpha=1.0)
        
        contours = ax2.contour(ra_mesh, dec_mesh, tb_interpolated, 
                              levels=10, colors='white', 
                              linewidths=0.5, alpha=0.4)
        ax2.clabel(contours, inline=True, fontsize=7, fmt='%0.0fK')
        
        ax2.scatter(ra_deg, dec, c='white', s=100, marker='x', 
                   linewidths=2, alpha=0.8, zorder=10)
        
        ax2.grid(True, alpha=0.2, linestyle='--', linewidth=0.5)
        cbar2 = plt.colorbar(im, ax=ax2, label='Tb (K)', pad=0.01)
        ax2.set_xlabel('RA', fontsize=11, weight='bold')
        ax2.set_ylabel('DEC', fontsize=11, weight='bold')
        self._format_degree_ticks(ax2)
        ax2.tick_params(axis='y', pad=8)
        ax2.set_title('Smoothed Interpolation\n(Cubic Interpolation)', fontsize=12, weight='bold')
        ax2.invert_xaxis()
        
        # Info box in lower panel
        info_text = self.create_info_text(tb_interpolated)
        fig.text(0.5, 0.08, info_text, transform=fig.transFigure,
                fontsize=9.5, verticalalignment='top', horizontalalignment='center',
                family='monospace',
                bbox=dict(boxstyle='round', edgecolor='black', facecolor='white', 
                         linewidth=1, alpha=1.0, pad=1.2))
        
        # Main title
        fig.suptitle(f'HI Sky Map in Equatorial Coordinates\n' +
                f'N={len(self.data_points)} observations | Tb median: {np.median(tb):.1f}±{np.std(tb):.1f}K',
                    fontsize=16, weight='bold', y=0.96)
        
        # Guardar
        output_path = self.output_dir / output_file
        plt.savefig(output_path, dpi=150, bbox_inches='tight')
        plt.close()
        
        print(f"✅ Vista combinada guardada: {output_path}")
        print(f"   Resolución: {fig.get_size_inches()[0]*150:.0f}x{fig.get_size_inches()[1]*150:.0f} px")
        print()
        
        return output_path
    
    def generate_all_plots(self):
        """Generar todos los plots"""
        if not self.load_spectra():
            return
        
        # Generar plots
        scatter_file = self.plot_scatter()
        interp_file = self.plot_interpolated()
        combined_file = self.plot_combined()
        
        print("=" * 80)
        print("✅ GENERACIÓN COMPLETADA")
        print("=" * 80)
        print()
        print("Archivos generados:")
        if scatter_file:
            print(f"  • {scatter_file.name}")
        if interp_file:
            print(f"  • {interp_file.name}")
        if combined_file:
            print(f"  • {combined_file.name}")
        print()
        print(f"Directorio: {self.output_dir}")
        print("=" * 80)


def main():
    parser = argparse.ArgumentParser(
        description='Generar mapas del cielo en coordenadas ecuatoriales',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('--spectra', required=True,
                       help='Directorio con espectros procesados (*_spectrum.h5)')
    parser.add_argument('--output', default=None,
                       help='Directorio de salida para plots (default: mismo que spectra)')
    parser.add_argument('--output-base', default=None,
                       help='Base de salida para data/iq/...; los plots se guardan en <output_base>/spectrum/')
    parser.add_argument('--scatter-only', action='store_true',
                       help='Solo generar scatter plot')
    parser.add_argument('--interp-only', action='store_true',
                       help='Solo generar mapa interpolado')
    parser.add_argument('--grid-res', type=int, default=200,
                       help='Resolución del grid de interpolación (default: 200)')
    parser.add_argument('--method', default='cubic', 
                       choices=['linear', 'cubic', 'nearest'],
                       help='Método de interpolación (default: cubic)')
    
    args = parser.parse_args()
    
    mapper = SkyMapper(spectra_dir=args.spectra, output_dir=args.output, output_base=args.output_base)
    
    if not mapper.load_spectra():
        sys.exit(1)
    
    if args.scatter_only:
        mapper.plot_scatter()
    elif args.interp_only:
        mapper.plot_interpolated(grid_resolution=args.grid_res, method=args.method)
    else:
        # Generar todos
        mapper.plot_scatter()
        mapper.plot_interpolated(grid_resolution=args.grid_res, method=args.method)
        mapper.plot_combined()


if __name__ == '__main__':
    main()
