#!/usr/bin/env python3
"""
Test script to verify grid point distribution algorithm
Shows how points are distributed for different inputs
"""

def calculate_grid_distribution(target_points: int, width_deg: float, height_deg: float):
    """Calculate optimal grid distribution"""
    aspect_ratio = width_deg / height_deg
    base = int(target_points ** 0.5)
    
    best_solution = None
    min_difference = float('inf')
    
    for dec_points in range(max(2, base - 5), base + 6):
        ra_points = int(dec_points * aspect_ratio + 0.5)
        ra_points = max(2, ra_points)
        
        actual_points = ra_points * dec_points
        difference = abs(actual_points - target_points)
        
        ra_step = width_deg / (ra_points - 1) if ra_points > 1 else width_deg
        dec_step = height_deg / (dec_points - 1) if dec_points > 1 else height_deg
        pixel_aspect = ra_step / dec_step if dec_step > 0 else 1.0
        pixel_squareness = min(pixel_aspect, 1.0 / pixel_aspect)
        
        score = difference * (2.0 - pixel_squareness)
        
        if score < min_difference:
            min_difference = score
            best_solution = (ra_points, dec_points, ra_step, dec_step, actual_points, pixel_aspect)
    
    return best_solution

# Test cases
test_cases = [
    (100, 10, 15),   # 100 points in 10x15 area
    (144, 10, 10),   # 144 points in square area
    (200, 20, 10),   # 200 points in 2:1 ratio
    (50, 5, 5),      # 50 points in small square
    (25, 2, 3),      # 25 points in rectangular area
]

print("=" * 80)
print("GRID DISTRIBUTION TEST")
print("=" * 80)
print()

for target, width, height in test_cases:
    ra_pts, dec_pts, ra_step, dec_step, actual_pts, pixel_aspect = \
        calculate_grid_distribution(target, width, height)
    
    print(f"Input: {target} points in {width}° x {height}° area")
    print(f"  → Grid: {ra_pts} x {dec_pts} = {actual_pts} points")
    print(f"  → Steps: RA={ra_step:.4f}° ({ra_step*60:.2f}'), DEC={dec_step:.4f}° ({dec_step*60:.2f}')")
    print(f"  → Pixel aspect: {pixel_aspect:.3f} (1.0 = perfect square)")
    print(f"  → Adjustment: {actual_pts - target:+d} points")
    print()

print("=" * 80)
print("VISUALIZATION EXAMPLE (10x10 area, 100 points → 10x10 grid)")
print("=" * 80)
print()

# Visual example
ra_pts, dec_pts, ra_step, dec_step, actual_pts, _ = calculate_grid_distribution(100, 10, 10)
print(f"Grid: {ra_pts} x {dec_pts} = {actual_pts} points")
print()
print("Each '█' represents a capture point:")
print()
for row in range(dec_pts):
    print("  " + "█ " * ra_pts)
print()
print(f"This creates a uniform grid of {actual_pts} square pixels")
print("Perfect for plotting radio intensity maps!")
