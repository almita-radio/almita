#!/usr/bin/env python3
"""
HI Spectrum Processor - Calibrated spectrum generation from IQ data

Uses three-point calibration (Hot, Cold, Load) to produce calibrated
brightness temperature spectra from raw SDR captures.

Usage:
    python3 process_spectra.py \
        --data data/iq/Orion-20260214-21:30:13/ \
        --calibration data/calibration/20260214-21:30:13/ \
        --output data/iq/Orion-20260214-21:30:13/spectrum/
"""

import argparse
import json
import h5py
import numpy as np
from pathlib import Path
from datetime import datetime
import sys


class SpectrumProcessor:
    def __init__(self, data_dir, calibration_dir, fft_size=8192, output_dir=None):
        """
        Initialize spectrum processor
        
        Args:
            data_dir: Directory containing observation HDF5 files
            calibration_dir: Directory containing calibration HDF5 files and JSON
            fft_size: FFT size for spectral resolution (default: 8192)
            output_dir: Output directory for processed spectra (default: data_dir/spectrum/)
        """
        self.data_dir = Path(data_dir)
        self.calibration_dir = Path(calibration_dir)
        self.fft_size = fft_size
        
        if output_dir:
            self.output_dir = Path(output_dir)
        else:
            self.output_dir = self.data_dir / 'spectrum'
        
        self.output_dir.mkdir(parents=True, exist_ok=True)
        
        self.calibration = None
        self.cal_hot = None
        self.cal_cold = None
        self.cal_load = None
    
    def load_calibration(self):
        """Load calibration data and metadata"""
        print("=" * 80)
        print("📚 LOADING CALIBRATION DATA")
        print("=" * 80)
        
        # Load calibration JSON
        cal_json = self.calibration_dir / 'calibration_results.json'
        if not cal_json.exists():
            print(f"❌ ERROR: Calibration JSON not found: {cal_json}")
            sys.exit(1)
        
        with open(cal_json) as f:
            self.calibration = json.load(f)
        
        print(f"✓ Calibration session: {self.calibration['session_id']}")
        print(f"  Timestamp: {self.calibration['timestamp']}")
        print(f"  Capture time: {self.calibration['capture_time']}s per point")
        print()
        
        # Load HOT data
        hot_file = self.calibration_dir / f"hot_calibration_{self.calibration['session_id']}.h5"
        print(f"Loading HOT calibration: {hot_file.name}")
        with h5py.File(hot_file, 'r') as f:
            i_samples = f['i_samples'][:]
            q_samples = f['q_samples'][:]
            print(f"  ✓ {len(i_samples):,} samples loaded")
            print(f"  Expected Tb: {self.calibration['hot']['point']['tb_kelvin']} K")
        
        # Convert uint8 to complex (centered and normalized)
        i_float = (i_samples.astype(np.float32) - 127.5) / 127.5
        q_float = (q_samples.astype(np.float32) - 127.5) / 127.5
        self.cal_hot = i_float + 1j * q_float
        print(f"  ✓ Converted to complex IQ")
        print()
        
        # Load COLD data
        cold_file = self.calibration_dir / f"cold_calibration_{self.calibration['session_id']}.h5"
        print(f"Loading COLD calibration: {cold_file.name}")
        with h5py.File(cold_file, 'r') as f:
            i_samples = f['i_samples'][:]
            q_samples = f['q_samples'][:]
            print(f"  ✓ {len(i_samples):,} samples loaded")
            print(f"  Expected Tb: {self.calibration['cold']['point']['tb_kelvin']} K")
        
        # Convert uint8 to complex (centered and normalized)
        i_float = (i_samples.astype(np.float32) - 127.5) / 127.5
        q_float = (q_samples.astype(np.float32) - 127.5) / 127.5
        self.cal_cold = i_float + 1j * q_float
        print(f"  ✓ Converted to complex IQ")
        print()
        
        # Load LOAD data
        load_file = self.calibration_dir / f"load_calibration_{self.calibration['session_id']}.h5"
        print(f"Loading LOAD calibration: {load_file.name}")
        with h5py.File(load_file, 'r') as f:
            i_samples = f['i_samples'][:]
            q_samples = f['q_samples'][:]
            print(f"  ✓ {len(i_samples):,} samples loaded")
        
        # Convert uint8 to complex (centered and normalized)
        i_float = (i_samples.astype(np.float32) - 127.5) / 127.5
        q_float = (q_samples.astype(np.float32) - 127.5) / 127.5
        self.cal_load = i_float + 1j * q_float
        print(f"  ✓ Converted to complex IQ")
        print()
        
        print("✅ All calibration data loaded successfully")
        print("=" * 80)
        print()
    
    def compute_spectrum(self, iq_data, normalize=True):
        """
        Compute power spectrum from IQ data using FFT
        
        Args:
            iq_data: Complex IQ samples
            normalize: Apply normalization
        
        Returns:
            frequencies, power spectrum
        """
        # Number of FFT segments
        n_segments = len(iq_data) // self.fft_size
        
        if n_segments == 0:
            print(f"⚠️  Warning: Data too short for FFT size {self.fft_size}")
            return None, None
        
        # Average FFT across segments (reduces noise)
        power_sum = np.zeros(self.fft_size)
        
        for i in range(n_segments):
            segment = iq_data[i * self.fft_size:(i + 1) * self.fft_size]
            
            # Apply window function (Hann) to reduce spectral leakage
            window = np.hanning(self.fft_size)
            segment_windowed = segment * window
            
            # Compute FFT
            fft = np.fft.fft(segment_windowed)
            
            # Power spectrum (magnitude squared)
            power = np.abs(fft) ** 2
            
            power_sum += power
        
        # Average power
        power_avg = power_sum / n_segments
        
        # Shift zero frequency to center
        power_shifted = np.fft.fftshift(power_avg)
        
        # Normalize if requested
        if normalize:
            power_shifted = power_shifted / np.max(power_shifted)
        
        # Frequency axis (relative, in bins)
        # Will be converted to actual frequencies using sample rate
        freq_bins = np.fft.fftshift(np.fft.fftfreq(self.fft_size))
        
        return freq_bins, power_shifted
    
    def calibrate_spectrum(self, obs_power, sample_rate):
        """
        Apply Y-factor calibration to convert power to brightness temperature
        
        Uses three-point calibration:
        - HOT: Known hot source (Tb_hot)
        - COLD: Known cold source (Tb_cold)
        - LOAD: 50Ω resistor (system noise Tsys)
        
        Args:
            obs_power: Observed power spectrum
            sample_rate: Sample rate in Hz
        
        Returns:
            frequency array (Hz), calibrated Tb array (K)
        """
        print("  🔬 Computing calibration spectra...")
        
        # Compute calibration spectra
        _, hot_power = self.compute_spectrum(self.cal_hot, normalize=False)
        _, cold_power = self.compute_spectrum(self.cal_cold, normalize=False)
        _, load_power = self.compute_spectrum(self.cal_load, normalize=False)
        
        # Get known temperatures
        Tb_hot = self.calibration['hot']['point']['tb_kelvin']
        Tb_cold = self.calibration['cold']['point']['tb_kelvin']
        
        print(f"  Tb_hot = {Tb_hot} K, Tb_cold = {Tb_cold} K")
        
        # Y-factor calibration
        # Tb = (P_obs - P_cold) / (P_hot - P_cold) * (Tb_hot - Tb_cold) + Tb_cold
        
        # Avoid division by zero
        denominator = hot_power - cold_power
        denominator[denominator == 0] = 1e-10
        
        Tb_spectrum = ((obs_power - cold_power) / denominator) * (Tb_hot - Tb_cold) + Tb_cold
        
        # Frequency axis in Hz
        freq_bins = np.fft.fftshift(np.fft.fftfreq(self.fft_size, d=1/sample_rate))
        
        print(f"  ✓ Calibration applied")
        print(f"  Frequency range: {freq_bins[0]/1e6:.3f} to {freq_bins[-1]/1e6:.3f} MHz")
        print(f"  Tb range: {np.min(Tb_spectrum):.1f} to {np.max(Tb_spectrum):.1f} K")
        
        return freq_bins, Tb_spectrum
    
    def process_observation(self, h5_file):
        """
        Process single observation HDF5 file
        
        Args:
            h5_file: Path to observation HDF5 file
        
        Returns:
            Dictionary with processed spectrum and metadata
        """
        print(f"📡 Processing: {h5_file.name}")
        
        # Load observation data
        with h5py.File(h5_file, 'r') as f:
            i_samples = f['i_samples'][:]
            q_samples = f['q_samples'][:]
            
            # Load metadata
            metadata = {key: f.attrs[key] for key in f.attrs.keys()}
            
            sample_rate = int(metadata['sample_rate_hz'])
            print(f"  Samples: {len(i_samples):,}")
            print(f"  Sample rate: {sample_rate/1e6:.2f} MS/s")
            print(f"  Target: RA={metadata.get('target_ra_hours', 'N/A')}h DEC={metadata.get('target_dec_degrees', 'N/A')}°")
        
        # Convert uint8 to complex (centered and normalized)
        i_float = (i_samples.astype(np.float32) - 127.5) / 127.5
        q_float = (q_samples.astype(np.float32) - 127.5) / 127.5
        iq_complex = i_float + 1j * q_float
        print(f"  ✓ Converted to complex IQ")
        
        # Compute raw power spectrum
        print(f"  📊 Computing spectrum (FFT size: {self.fft_size})...")
        _, obs_power = self.compute_spectrum(iq_complex, normalize=False)
        
        if obs_power is None:
            print(f"  ❌ Failed to compute spectrum")
            return None
        
        # Apply calibration
        freq_hz, Tb_spectrum = self.calibrate_spectrum(obs_power, sample_rate)
        
        # Prepare output
        result = {
            'filename': h5_file.name,
            'metadata': metadata,
            'frequencies_hz': freq_hz,
            'tb_spectrum_kelvin': Tb_spectrum,
            'fft_size': self.fft_size,
            'n_samples': len(i_samples)
        }
        
        print(f"  ✅ Spectrum processed successfully")
        print()
        
        return result
    
    def save_spectrum(self, result):
        """Save processed spectrum to HDF5"""
        output_file = self.output_dir / (result['filename'].replace('.h5', '_spectrum.h5'))
        
        with h5py.File(output_file, 'w') as f:
            # Save spectrum data
            f.create_dataset('frequencies_hz', data=result['frequencies_hz'], compression='gzip')
            f.create_dataset('tb_kelvin', data=result['tb_spectrum_kelvin'], compression='gzip')
            
            # Save metadata
            for key, value in result['metadata'].items():
                if isinstance(value, (int, float, str, bytes)):
                    f.attrs[key] = value
            
            # Processing metadata
            f.attrs['fft_size'] = result['fft_size']
            f.attrs['n_samples_processed'] = result['n_samples']
            f.attrs['calibration_session'] = self.calibration['session_id']
            f.attrs['processing_timestamp'] = datetime.utcnow().isoformat()
        
        print(f"  💾 Saved: {output_file.name}")
        return output_file
    
    def process_all(self):
        """Process all HDF5 files in data directory"""
        print("=" * 80)
        print("🔬 HI SPECTRUM PROCESSING")
        print("=" * 80)
        print(f"Data directory: {self.data_dir}")
        print(f"Output directory: {self.output_dir}")
        print(f"FFT size: {self.fft_size}")
        print("=" * 80)
        print()
        
        # Load calibration first
        self.load_calibration()
        
        # Find all HDF5 files
        h5_files = sorted(self.data_dir.glob('*.h5'))
        
        if not h5_files:
            print(f"❌ No HDF5 files found in {self.data_dir}")
            return
        
        print(f"Found {len(h5_files)} HDF5 files to process")
        print()
        
        results = []
        for i, h5_file in enumerate(h5_files, 1):
            print(f"[{i}/{len(h5_files)}]")
            result = self.process_observation(h5_file)
            
            if result:
                output_file = self.save_spectrum(result)
                results.append(output_file)
                print()
        
        print("=" * 80)
        print("✅ PROCESSING COMPLETE")
        print("=" * 80)
        print(f"Processed: {len(results)} / {len(h5_files)} files")
        print(f"Output directory: {self.output_dir}")
        print()
        print("Spectrum files created:")
        for output_file in results:
            print(f"  • {output_file.name}")
        print("=" * 80)


def main():
    parser = argparse.ArgumentParser(
        description='Process HI observations to calibrated spectra',
        formatter_class=argparse.RawDescriptionHelpFormatter
    )
    
    parser.add_argument('--data', required=True,
                       help='Directory containing observation HDF5 files')
    parser.add_argument('--calibration', required=True,
                       help='Directory containing calibration files (hot/cold/load HDF5 + JSON)')
    parser.add_argument('--fft-size', type=int, default=8192,
                       help='FFT size for spectral resolution (default: 8192)')
    parser.add_argument('--output', default=None,
                       help='Output directory for spectra (default: data_dir/spectrum/)')
    
    args = parser.parse_args()
    
    processor = SpectrumProcessor(
        data_dir=args.data,
        calibration_dir=args.calibration,
        fft_size=args.fft_size,
        output_dir=args.output
    )
    
    processor.process_all()


if __name__ == '__main__':
    main()
