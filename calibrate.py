#!/usr/bin/env python3
"""
HI Calibration Script - Three-point calibration (Hot, Cold, Load)

Performs automated calibration measurements:
1. HOT: Points to strongest HI region visible
2. COLD: Points to weakest HI region visible  
3. LOAD: Measures 50Ω resistor noise (manual connection required)

Usage:
    python3 calibrate.py --capture 30 --min-elevation 15
"""

import argparse
import asyncio
import json
import csv
import sys
from pathlib import Path
from datetime import datetime, timezone
import math

# Import existing modules
from indi_telescope_control import INDITelescopeControl
from sdr_capture import SDRCapture


class HICalibratorScript:
    def __init__(self, capture_time=30, min_elevation=15, sdr_host='localhost', sdr_port=1234, output_base: Path | None = None):
        self.capture_time = capture_time
        self.min_elevation = min_elevation
        self.sdr_host = sdr_host
        self.sdr_port = sdr_port
        
        self.observer = None
        self.catalog = []
        self.session_id = datetime.now(timezone.utc).strftime('%Y%m%d-%H:%M:%S')
        if output_base:
            self.output_dir = Path(output_base)
        else:
            self.output_dir = Path('data') / 'calibration' / self.session_id
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.results = {
            'session_id': self.session_id,
            'timestamp': datetime.now(timezone.utc).isoformat(),
            'capture_time': capture_time,
            'min_elevation': min_elevation,
            'hot': None,
            'cold': None,
            'load': None
        }
    
    def load_observer_config(self):
        """Load observer location from config"""
        config_file = Path('data') / 'observer_config.json'
        
        if not config_file.exists():
            print(f"❌ ERROR: Observer config not found at {config_file}")
            print("   Using default: Santiago, Chile")
            self.observer = {
                'name': 'Santiago Observatory',
                'latitude': -33.4489,
                'longitude': -70.6693,
                'elevation': 570
            }
        else:
            with open(config_file) as f:
                config = json.load(f)
                obs = config['observer']
                self.observer = {
                    'name': obs['name'],
                    'latitude': obs['latitude_deg'],
                    'longitude': obs['longitude_deg'],
                    'elevation': obs['elevation_m']
                }
        
        print(f"📍 Observer location:")
        print(f"   {self.observer['name']}")
        print(f"   Lat: {self.observer['latitude']:.4f}° Lon: {self.observer['longitude']:.4f}° Elev: {self.observer['elevation']}m")
        print()
    
    def load_hi_catalog(self):
        """Load HI sky catalog"""
        catalog_file = Path('data') / 'hi_sky_catalog_2000pts.csv'
        
        if not catalog_file.exists():
            print(f"❌ ERROR: HI catalog not found at {catalog_file}")
            print("   Run: python3 generate_hi_catalog.py")
            sys.exit(1)
        
        with open(catalog_file) as f:
            reader = csv.DictReader(f)
            for row in reader:
                self.catalog.append({
                    'point_id': int(row['point_id']),
                    'ra_hours': float(row['ra_hours']),
                    'dec_deg': float(row['dec_deg']),
                    'gal_lon': float(row['gal_lon']),
                    'gal_lat': float(row['gal_lat']),
                    'tb_kelvin': float(row['tb_kelvin'])
                })
        
        print(f"✓ Loaded HI catalog: {len(self.catalog)} points")
        print()
    
    def equatorial_to_horizontal(self, ra_hours, dec_deg, lat_deg, lon_deg):
        """Convert equatorial to horizontal coordinates (alt/az)"""
        # Get current LST
        now = datetime.now(timezone.utc)
        jd = 2451545.0 + (now - datetime(2000, 1, 1, 12, 0, 0, tzinfo=timezone.utc)).total_seconds() / 86400.0
        gmst = (18.697374558 + 24.06570982441908 * (jd - 2451545.0)) % 24
        lst = (gmst + lon_deg / 15.0) % 24
        
        # Hour angle
        ha = (lst - ra_hours) * 15.0  # degrees
        
        # Convert to radians
        ha_rad = math.radians(ha)
        dec_rad = math.radians(dec_deg)
        lat_rad = math.radians(lat_deg)
        
        # Calculate altitude
        sin_alt = (math.sin(dec_rad) * math.sin(lat_rad) + 
                   math.cos(dec_rad) * math.cos(lat_rad) * math.cos(ha_rad))
        alt = math.degrees(math.asin(sin_alt))
        alt_rad = math.radians(alt)
        
        # Calculate azimuth
        cos_az = ((math.sin(dec_rad) - math.sin(alt_rad) * math.sin(lat_rad)) / 
                  (math.cos(alt_rad) * math.cos(lat_rad)))
        cos_az = max(-1, min(1, cos_az))  # Clamp to [-1, 1]
        az = math.degrees(math.acos(cos_az))
        
        if math.sin(ha_rad) > 0:
            az = 360 - az
        
        return alt, az
    
    def find_visible_points(self):
        """Find hot and cold points visible above minimum elevation"""
        visible = []
        
        lat = self.observer['latitude']
        lon = self.observer['longitude']
        
        for point in self.catalog:
            alt, az = self.equatorial_to_horizontal(
                point['ra_hours'], point['dec_deg'], lat, lon
            )
            
            if alt >= self.min_elevation:
                point_with_alt = point.copy()
                point_with_alt['altitude'] = alt
                point_with_alt['azimuth'] = az
                visible.append(point_with_alt)
        
        if not visible:
            print(f"❌ ERROR: No points visible above {self.min_elevation}° elevation!")
            sys.exit(1)
        
        # Sort by temperature
        visible_sorted = sorted(visible, key=lambda x: x['tb_kelvin'])
        
        cold_points = visible_sorted[:3]  # 3 coldest
        hot_points = visible_sorted[-3:][::-1]  # 3 hottest (reversed)
        
        return hot_points, cold_points
    
    def display_calibration_targets(self, hot_points, cold_points):
        """Display available calibration targets"""
        print("=" * 80)
        print("🔥 HOTTEST 3 VISIBLE REGIONS (for HOT calibration):")
        print("=" * 80)
        for i, point in enumerate(hot_points, 1):
            print(f"  {i}. RA={point['ra_hours']:6.2f}h  DEC={point['dec_deg']:+6.1f}°  →  Tb={point['tb_kelvin']:5.1f}K")
            print(f"     Alt={point['altitude']:5.1f}°  Az={point['azimuth']:6.1f}°  (l={point['gal_lon']:6.1f}°, b={point['gal_lat']:+6.1f}°)")
            print()
        
        print("=" * 80)
        print("❄️  COLDEST 3 VISIBLE REGIONS (for COLD calibration):")
        print("=" * 80)
        for i, point in enumerate(cold_points, 1):
            print(f"  {i}. RA={point['ra_hours']:6.2f}h  DEC={point['dec_deg']:+6.1f}°  →  Tb={point['tb_kelvin']:5.1f}K")
            print(f"     Alt={point['altitude']:5.1f}°  Az={point['azimuth']:6.1f}°  (l={point['gal_lon']:6.1f}°, b={point['gal_lat']:+6.1f}°)")
            print()
        
        print("=" * 80)
        print()
    
    async def perform_calibration_capture(self, point, cal_type, telescope, sdr):
        """Perform single calibration capture"""
        print("=" * 80)
        print(f"📡 {cal_type.upper()} CALIBRATION")
        print("=" * 80)
        
        if cal_type != 'load':
            print(f"   Target: RA={point['ra_hours']:.2f}h  DEC={point['dec_deg']:+.1f}°")
            print(f"   Alt={point['altitude']:.1f}°  Az={point['azimuth']:.1f}°")
            print(f"   Expected Tb: {point['tb_kelvin']:.1f} K")
            print()
            
            # SLEW to target
            print("🔭 Step 1/3: SLEWING to coordinates...")
            success = await telescope.goto(point['ra_hours'], point['dec_deg'])
            if not success:
                print(f"   ❌ SLEW failed!")
                return None
            print("   ✓ SLEW completed")
            print()
            
            # Enable tracking
            await telescope.set_tracking(True)
            
            # Settle
            print("⏱️  Step 2/3: SETTLING (1 second)...")
            await asyncio.sleep(1.0)
            print("   ✓ Settling completed")
            print()
        else:
            print("⚠️  LOAD CALIBRATION - 50Ω resistor must be connected!")
            print()
        
        # Flush SDR buffer
        print("🗑️  Flushing SDR buffer...")
        flushed = await sdr.flush_buffer()
        print(f"   ✓ Buffer flushed: {flushed:,} samples")
        print()
        
        # Capture
        print(f"📡 Step 3/3: CAPTURING {self.capture_time}s of data...")
        filename = f"{cal_type}_calibration_{self.session_id}.h5"
        output_file = str(self.output_dir / filename)
        
        capture_result = await sdr.capture(
            duration=self.capture_time,
            output_file=output_file,
            sample_rate=2400000,
            metadata={
                'calibration_type': cal_type,
                'session_id': self.session_id,
                'target_ra_hours': point['ra_hours'] if cal_type != 'load' else 0,
                'target_dec_degrees': point['dec_deg'] if cal_type != 'load' else 0,
                'target_ra_hms': f"{int(point['ra_hours']):02d}:{int((point['ra_hours']%1)*60):02d}:00" if cal_type != 'load' else "N/A",
                'target_dec_dms': f"{point['dec_deg']:+.1f}" if cal_type != 'load' else "N/A",
                'altitude_degrees': point['altitude'] if cal_type != 'load' else 0,
                'azimuth_degrees': point['azimuth'] if cal_type != 'load' else 0,
                'expected_tb_kelvin': point['tb_kelvin'] if cal_type != 'load' else 0,
                'capture_time_seconds': self.capture_time
            }
        )
        
        print(f"   ✓ Capture completed: {capture_result.total_samples:,} samples")
        print(f"   ✓ File: {output_file}")
        print("=" * 80)
        print()
        
        return {
            'point': point,
            'hdf5_file': output_file,
            'samples': capture_result.total_samples,
            'capture_time': capture_result.capture_time
        }
    
    async def run(self):
        """Run complete calibration sequence"""
        print()
        print("=" * 80)
        print("🔬 HI THREE-POINT CALIBRATION")
        print("=" * 80)
        print(f"Session ID: {self.session_id}")
        print(f"Capture time: {self.capture_time}s per point")
        print(f"Min elevation: {self.min_elevation}°")
        print(f"Output: {self.output_dir}")
        print("=" * 80)
        print()
        
        # Load configuration
        self.load_observer_config()
        self.load_hi_catalog()
        
        # Find visible points
        print("🔍 Finding visible calibration targets...")
        hot_points, cold_points = self.find_visible_points()
        print(f"   ✓ Found {len(hot_points)} hot targets, {len(cold_points)} cold targets")
        print()
        
        # Display options
        self.display_calibration_targets(hot_points, cold_points)
        
        # Use strongest hot and coldest cold by default
        hot_target = hot_points[0]
        cold_target = cold_points[0]
        
        print("✓ Selected targets:")
        print(f"  HOT:  RA={hot_target['ra_hours']:.2f}h DEC={hot_target['dec_deg']:+.1f}° (Tb={hot_target['tb_kelvin']:.1f}K)")
        print(f"  COLD: RA={cold_target['ra_hours']:.2f}h DEC={cold_target['dec_deg']:+.1f}° (Tb={cold_target['tb_kelvin']:.1f}K)")
        print()
        
        # Connect to telescope and SDR
        print("🔌 Connecting to INDI server...")
        telescope = INDITelescopeControl(host='localhost', port=7624, verbose=False)
        await telescope.connect()
        print("   ✓ Connected to telescope")
        print()
        
        print("📻 Connecting to SDR...")
        sdr = SDRCapture(
            mode='network',
            host=self.sdr_host,
            port=self.sdr_port,
            verbose=False
        )
        await sdr.connect()
        await sdr.configure(
            center_freq=1420405752,
            sample_rate=2400000,
            gain='auto'
        )
        print("   ✓ SDR ready")
        print()
        
        try:
            # 1. HOT calibration
            self.results['hot'] = await self.perform_calibration_capture(hot_target, 'hot', telescope, sdr)
            
            # 2. COLD calibration
            self.results['cold'] = await self.perform_calibration_capture(cold_target, 'cold', telescope, sdr)
            
            # 3. LOAD calibration - PAUSE for manual intervention
            print("=" * 80)
            print("⚠️  LOAD CALIBRATION PREPARATION")
            print("=" * 80)
            print()
            print("Please perform the following steps:")
            print("  1. DISCONNECT the antenna from the SDR")
            print("  2. CONNECT a 50Ω resistor to the SDR input")
            print("  3. Press ENTER when ready to continue...")
            print()
            input(">>> Waiting for user... ")
            print()
            
            # Create dummy point for load
            load_point = {
                'ra_hours': 0, 'dec_deg': 0,
                'altitude': 0, 'azimuth': 0,
                'tb_kelvin': 0, 'gal_lon': 0, 'gal_lat': 0
            }
            self.results['load'] = await self.perform_calibration_capture(load_point, 'load', telescope, sdr)
            
            # Save results
            results_file = self.output_dir / 'calibration_results.json'
            with open(results_file, 'w') as f:
                json.dump(self.results, f, indent=2)
            
            print("=" * 80)
            print("✅ CALIBRATION COMPLETED SUCCESSFULLY")
            print("=" * 80)
            print()
            print(f"Results saved to: {results_file}")
            print()
            print("Files created:")
            print(f"  HOT:  {self.results['hot']['hdf5_file']}")
            print(f"  COLD: {self.results['cold']['hdf5_file']}")
            print(f"  LOAD: {self.results['load']['hdf5_file']}")
            print()
            print("Next steps:")
            print("  1. Reconnect the antenna")
            print("  2. Use these calibration files for spectrum processing")
            print("=" * 80)
            
        finally:
            await telescope.disconnect()


def main():
    parser = argparse.ArgumentParser(
        description='HI Three-Point Calibration (Hot, Cold, Load)',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('--capture', type=float, default=30,
                       help='Capture time in seconds per calibration point (default: 30)')
    parser.add_argument('--min-elevation', type=float, default=15,
                       help='Minimum elevation in degrees for valid targets (default: 15)')
    parser.add_argument('--sdr-host', default='localhost',
                       help='SDR server host (default: localhost)')
    parser.add_argument('--sdr-port', type=int, default=1234,
                       help='SDR server port (default: 1234)')
    parser.add_argument('--output-base', default=None,
                       help='Carpeta base para guardar resultados (default: data/calibration/<session_id>)')
    
    args = parser.parse_args()
    
    calibrator = HICalibratorScript(
        capture_time=args.capture,
        min_elevation=args.min_elevation,
        sdr_host=args.sdr_host,
        sdr_port=args.sdr_port,
        output_base=Path(args.output_base) if args.output_base else None,
    )
    
    asyncio.run(calibrator.run())


if __name__ == '__main__':
    main()
