#!/usr/bin/env python3
"""
Generate HI Sky Catalog with 2000 points
Estimates brightness temperature based on galactic distribution model
"""

import csv
import math

def equatorial_to_galactic(ra_deg, dec_deg):
    """Convert equatorial to galactic coordinates (simplified J2000)"""
    # NGP (North Galactic Pole): RA=12h51m26s, DEC=+27°07'42"
    # NCP in galactic: l=123.932°
    ra_ngp = 192.85948  # degrees
    dec_ngp = 27.12825
    l_ncp = 123.932
    
    ra_rad = math.radians(ra_deg)
    dec_rad = math.radians(dec_deg)
    ra_ngp_rad = math.radians(ra_ngp)
    dec_ngp_rad = math.radians(dec_ngp)
    
    # Galactic latitude
    sin_b = (math.sin(dec_rad) * math.sin(dec_ngp_rad) + 
             math.cos(dec_rad) * math.cos(dec_ngp_rad) * math.cos(ra_rad - ra_ngp_rad))
    b = math.degrees(math.asin(sin_b))
    
    # Galactic longitude
    y = math.sin(ra_rad - ra_ngp_rad)
    x = (math.cos(ra_rad - ra_ngp_rad) * math.sin(dec_ngp_rad) - 
         math.tan(dec_rad) * math.cos(dec_ngp_rad))
    l = math.degrees(math.atan2(y, x)) + l_ncp
    l = l % 360  # Normalize to 0-360
    
    return l, b

def estimate_hi_temperature(l, b, ra_deg, dec_deg):
    """Estimate HI brightness temperature based on galactic position"""
    
    # Base temperature from galactic latitude (exponential disk model)
    base_tb = 70 * math.exp(-abs(b) / 10.0)  # Disk scale height ~10°
    
    # Add minimum background (CMB + extragalactic)
    tb = max(base_tb, 3.0)
    
    # Enhancement for specific HI-rich regions
    
    # Galactic Center region (l=0±30°, |b|<5°)
    if abs(l) < 30 or abs(l - 360) < 30:
        if abs(b) < 5:
            tb += 50 * math.exp(-abs(b) / 2.0)
    
    # Cygnus region (l~80°, rich in HI)
    if 70 < l < 90 and abs(b) < 10:
        tb += 30 * math.exp(-abs(b) / 5.0)
    
    # Perseus arm (l~140°)
    if 130 < l < 150 and abs(b) < 8:
        tb += 25 * math.exp(-abs(b) / 4.0)
    
    # Outer Galaxy (l~180°, opposite to GC)
    if 160 < l < 200 and abs(b) < 5:
        tb += 15 * math.exp(-abs(b) / 3.0)
    
    # Vela region (l~260°)
    if 250 < l < 270 and abs(b) < 10:
        tb += 20
    
    # North/South Galactic Poles - very cold
    if abs(b) > 60:
        tb = 3.0 + 2.0 * math.exp(-(abs(b) - 60) / 10.0)
    
    return round(tb, 1)

def main():
    # Generate 2000-point catalog
    print("Generating HI sky catalog with 2000 points...")
    
    # Create grid: ~45x45 points
    n_ra = 48  # RA points (covers 360° / 48 = 7.5° spacing)
    n_dec = 42  # DEC points (covers -90 to +90 = ~4.3° spacing)
    
    catalog = []
    point_id = 1
    
    for i_dec in range(n_dec):
        dec = -90 + (180 / (n_dec - 1)) * i_dec
        
        for i_ra in range(n_ra):
            ra = (360 / n_ra) * i_ra
            
            # Convert to galactic
            l, b = equatorial_to_galactic(ra, dec)
            
            # Estimate HI temperature
            tb = estimate_hi_temperature(l, b, ra, dec)
            
            # Convert RA to hours
            ra_hours = ra / 15.0
            
            catalog.append({
                'point_id': point_id,
                'ra_deg': round(ra, 4),
                'ra_hours': round(ra_hours, 4),
                'dec_deg': round(dec, 4),
                'gal_lon': round(l, 4),
                'gal_lat': round(b, 4),
                'tb_kelvin': tb
            })
            
            point_id += 1
    
    # Write CSV
    filename = 'data/hi_sky_catalog_2000pts.csv'
    fieldnames = ['point_id', 'ra_deg', 'ra_hours', 'dec_deg', 'gal_lon', 'gal_lat', 'tb_kelvin']
    
    with open(filename, 'w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(catalog)
    
    print(f"✓ Catalog created: {filename}")
    print(f"  Total points: {len(catalog)}")
    print(f"  RA spacing: ~{360/n_ra:.1f}°")
    print(f"  DEC spacing: ~{180/(n_dec-1):.1f}°")
    print()
    
    # Find and report hot/cold spots
    sorted_by_tb = sorted(catalog, key=lambda x: x['tb_kelvin'])
    
    print("COLDEST 5 REGIONS (for calibration):")
    for i, point in enumerate(sorted_by_tb[:5], 1):
        print(f"  {i}. RA={point['ra_hours']:.2f}h DEC={point['dec_deg']:+.1f}° → Tb={point['tb_kelvin']}K (l={point['gal_lon']:.1f}°, b={point['gal_lat']:+.1f}°)")
    
    print()
    print("HOTTEST 5 REGIONS (for calibration):")
    for i, point in enumerate(sorted_by_tb[-5:][::-1], 1):
        print(f"  {i}. RA={point['ra_hours']:.2f}h DEC={point['dec_deg']:+.1f}° → Tb={point['tb_kelvin']}K (l={point['gal_lon']:.1f}°, b={point['gal_lat']:+.1f}°)")

if __name__ == '__main__':
    main()
