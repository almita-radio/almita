#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Grid Generator for Radio Astronomy Sky Surveys
Generates CSV with planned observation points (NO INDI CONNECTION REQUIRED)

This script ONLY calculates grid points and creates a CSV plan.
Use this CSV later for actual observations with your capture script.

Output: ./data/mosaic/{session_name}-{timestamp}/mosaic.csv
Output: ./data/mosaic/{session_name}-{timestamp}/mosaic.png (if matplotlib available)
"""

import sys
import os
import csv
import argparse
from datetime import datetime, timezone
from pathlib import Path
from typing import Tuple


class GridGenerator:
    """
    Generates grid observation plans (coordinates only, no telescope control)
    """
    
    def __init__(self, session_name: str, base_dir: str = "./data/mosaic"):
        """
        Initialize grid generator
        
        Args:
            session_name: Name for this observation session
            base_dir: Base directory for data storage (default: ./data)
        """
        self.session_name = session_name
        self.base_dir = base_dir
        
        # Create timestamp for this session
        self.session_timestamp = datetime.now(timezone.utc)
        # Use formato objeto-YYYYMMDD-HH:MM:SS para las carpetas
        self.session_id = self.session_timestamp.strftime("%Y%m%d-%H:%M:%S")
        
        # Create output directory: ./data/mosaic/{session_name}-{timestamp}/
        self.output_dir = Path(base_dir) / f"{session_name}-{self.session_id}"
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        # CSV file path: mosaic.csv
        self.csv_filepath = self.output_dir / "mosaic.csv"
        
        self.log(f"Grid Generator initialized")
        self.log(f"Session: {session_name}")
        self.log(f"Session ID: {self.session_id}")
        self.log(f"Output directory: {self.output_dir}")
        self.log(f"CSV file: {self.csv_filepath}")
        
        # CSV fieldnames (simplified for planning)
        self.csv_fieldnames = [
            # Identification
            'point_number',
            'grid_row',
            'grid_col',

            # Target coordinates
            'target_ra_hours',
            'target_dec_degrees',
            'target_ra_hms',
            'target_dec_dms',

            # Status and timing (updated by capture.py)
            'capture_status',
            'start_time',
            'end_time',
            'duration',
            'error_message',

            # Files
            'data_filename',

            # Session info
            'session_name',
            'session_id'
        ]
        
        # Initialize CSV file with headers
        self._initialize_csv()
    
    def log(self, message: str, level: str = "INFO"):
        """Print timestamped log message"""
        timestamp = datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
        print(f"[{timestamp}] [{level}] {message}")
        sys.stdout.flush()
    
    def _initialize_csv(self):
        """Create CSV file with headers"""
        try:
            with open(self.csv_filepath, 'w', newline='') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=self.csv_fieldnames)
                writer.writeheader()
            self.log(f"CSV file created with {len(self.csv_fieldnames)} fields")
        except Exception as e:
            self.log(f"Error creating CSV file: {e}", "ERROR")
            raise
    
    def _ra_to_hms(self, ra_hours: float) -> str:
        """Convert RA from decimal hours to HH:MM:SS.SSS format"""
        hours = int(ra_hours)
        minutes_decimal = (ra_hours - hours) * 60
        minutes = int(minutes_decimal)
        seconds = (minutes_decimal - minutes) * 60
        return f"{hours:02d}:{minutes:02d}:{seconds:06.3f}"
    
    def _dec_to_dms(self, dec_degrees: float) -> str:
        """Convert DEC from decimal degrees to +/-DD:MM:SS.SS format"""
        sign = '+' if dec_degrees >= 0 else '-'
        dec_abs = abs(dec_degrees)
        degrees = int(dec_abs)
        arcmin_decimal = (dec_abs - degrees) * 60
        arcmin = int(arcmin_decimal)
        arcsec = (arcmin_decimal - arcmin) * 60
        return f"{sign}{degrees:02d}:{arcmin:02d}:{arcsec:05.2f}"
    
    def append_point(self, point_data: dict):
        """Append a point to the CSV file"""
        try:
            # Ensure all fields are present
            row = {field: point_data.get(field, '') for field in self.csv_fieldnames}
            
            # Append to CSV
            with open(self.csv_filepath, 'a', newline='') as csvfile:
                writer = csv.DictWriter(csvfile, fieldnames=self.csv_fieldnames)
                writer.writerow(row)
            
        except Exception as e:
            self.log(f"Error appending to CSV: {e}", "ERROR")
            raise
    
    def _calculate_grid_distribution(self, target_points: int, width_deg: float, height_deg: float) -> Tuple:
        """
        Calculate optimal grid distribution for uniform pixels
        
        Args:
            target_points: Desired number of points
            width_deg: Grid width in degrees
            height_deg: Grid height in degrees
            
        Returns:
            (num_ra_points, num_dec_points, ra_step_deg, dec_step_deg, actual_points)
        """
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
                best_solution = (ra_points, dec_points, ra_step, dec_step, actual_points)
        
        return best_solution

    def _generate_plot(self, points_ra, points_dec, points_numbers,
                      center_ra, center_dec, width_deg, height_deg):
        """
        Generate PNG plot of grid points

        Args:
            points_ra: List of RA coordinates (hours)
            points_dec: List of DEC coordinates (degrees)
            points_numbers: List of point numbers
            center_ra: Grid center RA
            center_dec: Grid center DEC
            width_deg: Grid width
            height_deg: Grid height
        """
        try:
            # Import matplotlib only when needed
            import matplotlib
            matplotlib.use('Agg')  # Non-interactive backend
            import matplotlib.pyplot as plt

            self.log("Generating grid plot...")

            # Convert RA from hours to degrees for proper aspect ratio
            points_ra_deg = [ra * 15.0 for ra in points_ra]
            center_ra_deg = center_ra * 15.0

            # Create figure
            fig, ax = plt.subplots(figsize=(12, 10))

            # Plot grid points
            ax.scatter(points_ra_deg, points_dec, c='blue', s=100, alpha=0.6, 
                      edgecolors='darkblue', linewidth=1.5, label='Observation points')

            # Add point numbers (only if not too many)
            if len(points_numbers) <= 100:
                for ra_deg, dec, num in zip(points_ra_deg, points_dec, points_numbers):
                    ax.annotate(str(num), (ra_deg, dec), fontsize=8, ha='center', va='center',
                               color='white', weight='bold',
                               bbox=dict(boxstyle='circle,pad=0.1', facecolor='darkblue', alpha=0.7))
            else:
                self.log(f"  Too many points ({len(points_numbers)}), skipping labels")

            # Mark center
            ax.plot(center_ra_deg, center_dec, 'r*', markersize=20, label='Grid center', 
                   markeredgecolor='darkred', markeredgewidth=1.5)

            # Draw grid boundary
            ra_margin_deg = width_deg / 2
            dec_margin = height_deg / 2
            boundary_ra_deg = [
                center_ra_deg - ra_margin_deg, center_ra_deg + ra_margin_deg,
                center_ra_deg + ra_margin_deg, center_ra_deg - ra_margin_deg,
                center_ra_deg - ra_margin_deg
            ]
            boundary_dec = [
                center_dec - dec_margin, center_dec - dec_margin,
                center_dec + dec_margin, center_dec + dec_margin,
                center_dec - dec_margin
            ]
            ax.plot(boundary_ra_deg, boundary_dec, 'r--', linewidth=2, alpha=0.5, label='Grid boundary')

            # Labels and title
            ax.set_xlabel('Right Ascension (degrees)', fontsize=12, fontweight='bold')
            ax.set_ylabel('Declination (degrees)', fontsize=12, fontweight='bold')
            ax.set_title(f'Observation Grid Plan: {self.session_name}\n'
                        f'{len(points_numbers)} points | Area: {width_deg} deg x {height_deg} deg',
                        fontsize=14, fontweight='bold', pad=20)

            # Grid
            ax.grid(True, alpha=0.3, linestyle='--', linewidth=0.5)

            # Legend
            ax.legend(loc='upper right', fontsize=10)

            # Add info text box
            info_text = (f"Session: {self.session_name}\n"
                        f"Center: RA={center_ra:.4f}h ({center_ra_deg:.2f} deg), DEC={center_dec:.4f} deg\n"
                        f"Total points: {len(points_numbers)}\n"
                        f"Session ID: {self.session_id}")
            ax.text(0.02, 0.98, info_text, transform=ax.transAxes,
                   fontsize=9, verticalalignment='top',
                   bbox=dict(boxstyle='round', facecolor='wheat', alpha=0.8))

            # Equal aspect ratio for square pixels (NOW both axes in degrees)
            ax.set_aspect('equal', adjustable='box')

            # Tight layout
            plt.tight_layout()

            # Save plot
            plot_path = self.output_dir / 'mosaic.png'
            plt.savefig(plot_path, dpi=150, bbox_inches='tight')
            plt.close()

            self.log(f"  Plot saved: {plot_path}")

        except ImportError:
            self.log("matplotlib not installed - skipping plot generation", "WARNING")
            self.log("  Install with: pip install matplotlib", "INFO")
        except Exception as e:
            self.log(f"Error generating plot: {e}", "WARNING")
            self.log("  (Continuing without plot)")

    def generate_grid_plan(self,
                          center_ra: float,
                          center_dec: float,
                          width_deg: float,
                          height_deg: float,
                          num_points: int) -> bool:
        """
        Generate grid observation plan (no INDI connection needed)

        Args:
            center_ra: Grid center RA in hours
            center_dec: Grid center DEC in degrees
            width_deg: Grid width in degrees
            height_deg: Grid height in degrees
            num_points: Target number of points

        Returns:
            True if grid generated successfully
        """
        self.log("=" * 80)
        self.log("GENERATING GRID OBSERVATION PLAN")
        self.log("=" * 80)
        self.log(f"Center: RA={center_ra}h, DEC={center_dec} deg")
        self.log(f"Grid area: {width_deg} deg x {height_deg} deg")
        self.log(f"Target points: {num_points}")
        self.log(f"Press Ctrl+C to cancel generation")
        self.log("")

        points_generated = 0

        try:
            # Calculate optimal grid distribution
            num_ra_points, num_dec_points, ra_step_deg, dec_step_deg, actual_points = \
                self._calculate_grid_distribution(num_points, width_deg, height_deg)
            
            self.log(f"Calculated distribution:")
            self.log(f"  Grid: {num_ra_points} x {num_dec_points} = {actual_points} points")
            self.log(f"  RA step: {ra_step_deg:.4f} deg ({ra_step_deg*60:.2f} arcmin)")
            self.log(f"  DEC step: {dec_step_deg:.4f} deg ({dec_step_deg*60:.2f} arcmin)")
            self.log(f"  Pixel aspect ratio: {ra_step_deg/dec_step_deg:.3f} (1.0 = perfect square)")
            
            if actual_points != num_points:
                self.log(f"  Adjusted from {num_points} to {actual_points} points for uniform distribution")
            self.log("")
            
            # Convert to hours for RA
            ra_step_hours = ra_step_deg / 15.0
            
            # Calculate grid bounds
            ra_min = center_ra - (width_deg / 15.0) / 2
            ra_max = center_ra + (width_deg / 15.0) / 2
            dec_min = center_dec - height_deg / 2
            dec_max = center_dec + height_deg / 2
            
            self.log(f"Grid bounds:")
            self.log(f"  RA: {ra_min:.4f}h to {ra_max:.4f}h")
            self.log(f"  DEC: {dec_min:.4f} deg to {dec_max:.4f} deg")
            self.log("")

            point_number = 0
            points_ra = []
            points_dec = []
            points_numbers = []

            # Generate grid points
            for row_idx in range(num_dec_points):
                if num_dec_points > 1:
                    current_dec = dec_min + row_idx * dec_step_deg
                else:
                    current_dec = center_dec

                for col_idx in range(num_ra_points):
                    if num_ra_points > 1:
                        current_ra = ra_min + col_idx * ra_step_hours
                    else:
                        current_ra = center_ra

                    point_number += 1

                    # Store for plotting
                    points_ra.append(current_ra)
                    points_dec.append(current_dec)
                    points_numbers.append(point_number)

                    # Prepare point data
                    point_data = {
                        'point_number': point_number,
                        'grid_row': row_idx,
                        'grid_col': col_idx,
                        'target_ra_hours': f"{current_ra:.6f}",
                        'target_dec_degrees': f"{current_dec:.6f}",
                        'target_ra_hms': self._ra_to_hms(current_ra),
                        'target_dec_dms': self._dec_to_dms(current_dec),
                        'capture_status': 'planned',
                        'start_time': '',
                        'end_time': '',
                        'duration': '',
                        'error_message': '',
                        'data_filename': f"{self.session_name}_{point_number:04d}.dat",
                        'session_name': self.session_name,
                        'session_id': self.session_id
                    }

                    # Append to CSV
                    self.append_point(point_data)
                    points_generated += 1

            # Generate plot
            self._generate_plot(points_ra, points_dec, points_numbers, 
                              center_ra, center_dec, width_deg, height_deg)

            # Summary
            self.log("=" * 80)
            self.log("GRID PLAN GENERATED")
            self.log("=" * 80)
            self.log(f"Total points: {actual_points}")
            self.log(f"CSV file: {self.csv_filepath}")
            self.log(f"Plot file: {self.csv_filepath.with_suffix('.png')}")
            self.log("")
            self.log("Next steps:")
            self.log("  1. Review the CSV file")
            self.log("  2. Check the PNG plot for visualization")
            self.log("  3. Use this CSV with your observation/capture script")
            self.log("")

            return True

        except KeyboardInterrupt:
            # User pressed Ctrl+C
            self.log("", "WARNING")
            self.log("=" * 80, "WARNING")
            self.log("GRID GENERATION INTERRUPTED BY USER (Ctrl+C)", "WARNING")
            self.log("=" * 80, "WARNING")
            self.log(f"Points generated before interruption: {points_generated}/{actual_points if 'actual_points' in locals() else num_points}", "WARNING")
            self.log(f"Partial CSV file saved: {self.csv_filepath}", "WARNING")
            self.log("")
            self.log("NOTE: The CSV file contains only the points generated before interruption.", "INFO")
            self.log("      You can delete it or continue with the partial grid.", "INFO")
            self.log("")
            raise  # Re-raise to propagate to main()

        except Exception as e:
            self.log(f"Grid generation error: {e}", "ERROR")
            import traceback
            self.log(traceback.format_exc(), "ERROR")
            return False


def main():
    """Main entry point"""
    parser = argparse.ArgumentParser(
        description='Grid Generator for Radio Astronomy - Generates observation plan (NO INDI needed)',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
  %(prog)s --session "cygnus_a" --center-ra 19.99 --center-dec 40.73 --points 100
  %(prog)s --session "m51" --center-ra 13.5 --center-dec 47.2 --width 2.0 --height 3.0 --points 150
  %(prog)s --session "test" --center-ra 0.0 --center-dec 0.0 --width 0.5 --height 0.5 --points 25

This script ONLY generates the observation plan (CSV with coordinates).
NO INDI connection required. NO telescope movement.
Use the CSV later for actual observations with your capture script.

Output structure:
  ./data/20260214_020754/test.csv (grid coordinates)
        """
    )
    
    # Required arguments
    parser.add_argument('--session', required=True,
                        help='Session name (will be used as CSV filename)')
    parser.add_argument('--center-ra', type=float, required=True,
                        help='Grid center RA in hours (0-24)')
    parser.add_argument('--center-dec', type=float, required=True,
                        help='Grid center DEC in degrees (-90 to +90)')
    parser.add_argument('--points', type=int, required=True,
                        help='Target number of points (will be adjusted for uniform distribution)')
    
    # Grid parameters
    parser.add_argument('--width', type=float, default=1.0,
                        help='Grid width in degrees (default: 1.0)')
    parser.add_argument('--height', type=float, default=1.0,
                        help='Grid height in degrees (default: 1.0)')
    
    # Output directory
    parser.add_argument('--data-dir', default='./data',
                        help='Base directory for data storage (default: ./data)')
    
    args = parser.parse_args()
    
    # Create grid generator
    generator = GridGenerator(
        session_name=args.session,
        base_dir=args.data_dir
    )
    
    # Generate grid plan
    try:
        success = generator.generate_grid_plan(
            center_ra=args.center_ra,
            center_dec=args.center_dec,
            width_deg=args.width,
            height_deg=args.height,
            num_points=args.points
        )

        if success:
            generator.log("Grid plan generated successfully", "INFO")
            sys.exit(0)
        else:
            generator.log("Grid plan generation failed", "ERROR")
            sys.exit(1)

    except KeyboardInterrupt:
        generator.log("", "WARNING")
        generator.log("Operation cancelled by user", "WARNING")
        generator.log(f"Partial output directory: {generator.output_dir}", "INFO")
        generator.log("", "WARNING")
        sys.exit(130)  # Standard exit code for Ctrl+C
    except Exception as e:
        generator.log(f"Unexpected error: {e}", "ERROR")
        import traceback
        traceback.print_exc()
        sys.exit(1)


if __name__ == "__main__":
    main()
