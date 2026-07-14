# Grid Scan Quick Reference Guide

## Overview

The grid scan feature performs systematic sky surveys by moving the telescope mount through a grid pattern and capturing data at each position. Each capture is automatically numbered sequentially.

---

## Quick Start

```bash
# Basic grid scan (1° x 1°, 0.1° steps)
python Testing_INDIpy.py --grid \
  --center-ra 19.99 \
  --center-dec 40.73 \
  --name cygnus_a

# Custom grid with longer captures
python Testing_INDIpy.py --grid \
  --center-ra 12.5 \
  --center-dec 45.0 \
  --width 2.0 \
  --height 2.0 \
  --step 0.2 \
  --name m51_survey \
  --capture-time 30.0
```

---

## Parameters

| Parameter | Type | Default | Description |
|-----------|------|---------|-------------|
| `--grid` | flag | - | Enable grid scan mode (required) |
| `--center-ra` | float | - | Grid center RA in hours (0-24) |
| `--center-dec` | float | - | Grid center DEC in degrees (-90 to +90) |
| `--width` | float | 1.0 | Grid width in degrees |
| `--height` | float | 1.0 | Grid height in degrees |
| `--step` | float | 0.1 | Step size between points (degrees) |
| `--name` | string | grid_scan | Base name for captures |
| `--capture-time` | float | 5.0 | Duration per capture point (seconds) |

---

## Output Structure

```
grid_scans/
└── {capture_name}/
    ├── {capture_name}_0001.dat
    ├── {capture_name}_0001.dat.meta
    ├── {capture_name}_0002.dat
    ├── {capture_name}_0002.dat.meta
    └── ...
```

### File Naming Convention

- **Format**: `{capture_name}_{NNNN}.dat`
- **Numbering**: 4-digit zero-padded (0001, 0002, ..., 9999)
- **Metadata**: Same name with `.meta` extension

---

## Examples by Use Case

### 1. Quick Test Grid
```bash
python Testing_INDIpy.py --grid \
  --center-ra 0.0 --center-dec 0.0 \
  --width 0.2 --height 0.2 \
  --step 0.1 \
  --name test_grid \
  --capture-time 2.0
```
- **Grid points**: 3 x 3 = 9 positions
- **Total time**: ~1 minute

### 2. Detailed Source Study
```bash
python Testing_INDIpy.py --grid \
  --center-ra 19.99 --center-dec 40.73 \
  --width 0.5 --height 0.5 \
  --step 0.05 \
  --name cygnus_a_detail \
  --capture-time 60.0
```
- **Grid points**: 11 x 11 = 121 positions
- **Total time**: ~2.5 hours

### 3. Large Sky Survey
```bash
python Testing_INDIpy.py --grid \
  --center-ra 18.0 --center-dec 30.0 \
  --width 10.0 --height 10.0 \
  --step 1.0 \
  --name galactic_survey \
  --capture-time 120.0
```
- **Grid points**: 11 x 11 = 121 positions
- **Total time**: ~5 hours

### 4. Radio Spectral Line Survey (H-I)
```bash
python Testing_INDIpy.py --grid \
  --center-ra 20.0 --center-dec 40.0 \
  --width 5.0 --height 5.0 \
  --step 0.25 \
  --name hi_line_survey \
  --capture-time 300.0
```
- **Grid points**: 21 x 21 = 441 positions
- **Total time**: ~40 hours (multi-night observation)

---

## Calculating Grid Parameters

### Grid Points
```
num_points = ((width / step) + 1) × ((height / step) + 1)
```

### Total Time (approximate)
```
total_time = num_points × (capture_time + 10s)
```
*The +10s accounts for mount movement and settling time*

### Optimal Step Size

For radio astronomy, choose step size based on your beam width:

```
step_size = beam_width / 2  (Nyquist sampling)
step_size = beam_width / 3  (oversampling, recommended)
```

**Example**: 
- Beam width: 0.5°
- Recommended step: 0.15° to 0.17°

---

## Metadata File Contents

Each `.meta` file contains:

```
Capture: cygnus_a_survey_0042.dat
Point: 42/121
Grid position: (3, 6)
RA: 19.990000h
DEC: 40.730000°
Start time: 2025-01-15T23:45:12.123456
End time: 2025-01-15T23:45:17.123456
Duration: 5.0s
```

### Using Metadata in Data Processing

```python
import os

def read_capture_metadata(meta_file):
    """Parse metadata file"""
    metadata = {}
    with open(meta_file, 'r') as f:
        for line in f:
            if ':' in line:
                key, value = line.split(':', 1)
                metadata[key.strip()] = value.strip()
    return metadata

# Process all captures in a survey
scan_dir = "grid_scans/cygnus_a_survey"
for file in sorted(os.listdir(scan_dir)):
    if file.endswith('.dat'):
        meta_file = f"{scan_dir}/{file}.meta"
        metadata = read_capture_metadata(meta_file)
        
        # Your processing here
        process_radio_data(
            data_file=f"{scan_dir}/{file}",
            ra=float(metadata['RA'].rstrip('h')),
            dec=float(metadata['DEC'].rstrip('°')),
            time=metadata['Start time']
        )
```

---

## Integration with Your Radio Receiver

### Step 1: Locate the Capture Point

In `Testing_INDIpy.py`, find the `grid_scan()` method:

```python
# Around line 280-290
# Here you would integrate your radio astronomy data acquisition
# For now, we'll create a metadata file
start_time = datetime.now()
await asyncio.sleep(capture_duration)  # ← REPLACE THIS LINE
end_time = datetime.now()
```

### Step 2: Replace with Your Capture Code

```python
# Example for SDR-based receiver
start_time = datetime.now()

# Your actual capture code
await capture_spectrum(
    filename=capture_path,
    duration=capture_duration,
    center_freq=1420.405,  # MHz (H-I line)
    bandwidth=2.0,          # MHz
    gain=40                 # dB
)

end_time = datetime.now()
```

### Step 3: Test with Short Capture

```bash
# Test with 2-second captures first
python Testing_INDIpy.py --grid \
  --center-ra 0.0 --center-dec 0.0 \
  --width 0.1 --height 0.1 \
  --step 0.05 \
  --name integration_test \
  --capture-time 2.0
```

---

## Troubleshooting

### Grid Scan Doesn't Start

**Check:**
1. INDI server is running: `pgrep indiserver`
2. Mount is connected in KStars/Ekos
3. Both `--center-ra` and `--center-dec` are specified

```bash
# Correct
python Testing_INDIpy.py --grid --center-ra 12.0 --center-dec 45.0 --name test

# Wrong (missing parameters)
python Testing_INDIpy.py --grid --name test
```

### Mount Doesn't Move

**Check:**
1. Tracking is enabled
2. Coordinates are within mount limits
3. Mount is not parked

**Fix:**
```python
# In your script, ensure tracking is on
await controller.set_tracking(True)
```

### Captures Are Too Slow

**Reduce overhead:**
- Decrease `capture_duration`
- Increase `step_size` (fewer points)
- Reduce grid size (`width` and `height`)

### Files Not Created

**Check:**
1. Write permissions in current directory
2. Disk space available
3. No special characters in `--name` parameter

---

## Advanced Usage

### Resume Interrupted Grid Scan

Currently not implemented. To add resume capability:

1. Check existing captures before starting
2. Skip grid positions that already have data files
3. Continue from last position

### Parallel Processing

For faster surveys with multiple receivers:

```python
# Theoretical example (not implemented)
await controller.grid_scan_parallel(
    center_ra=19.99,
    center_dec=40.73,
    width_deg=2.0,
    height_deg=2.0,
    step_size_deg=0.2,
    capture_name="parallel_survey",
    num_receivers=4  # Capture 4 points simultaneously
)
```

### Custom Grid Patterns

Currently implements raster scan (left-to-right, bottom-to-top). To implement spiral pattern:

1. Modify loop order in `grid_scan()` method
2. Calculate spiral coordinates instead of linear grid
3. Maintain sequential numbering

---

## Best Practices

### 1. Test First
Always test with a small grid before starting long observations:
```bash
python Testing_INDIpy.py --grid \
  --center-ra 0.0 --center-dec 0.0 \
  --width 0.1 --height 0.1 --step 0.05 \
  --name test --capture-time 2.0
```

### 2. Estimate Time
Calculate total observation time before starting:
- Grid points × (capture_time + 10s)
- Add 20% buffer for unexpected delays

### 3. Descriptive Names
Use descriptive capture names with dates:
```bash
--name cygnus_a_1420mhz_2025_01_15
--name m51_continuum_survey_jan
--name galactic_plane_10cm_night1
```

### 4. Monitor Progress
The script logs progress in real-time:
```
--- Point 42/121 ---
Target: RA=19.990000h, DEC=40.730000°
✓ Capture completed: cygnus_a_survey_0042.dat
```

### 5. Data Backup
After completing a grid scan, immediately backup:
```bash
tar -czf cygnus_a_survey_backup.tar.gz grid_scans/cygnus_a_survey/
```

---

## Performance Tips

### Faster Surveys
- Increase step size (fewer points)
- Reduce capture time
- Use SSD for data storage
- Minimize other system processes

### More Accurate Surveys
- Decrease step size (more points)
- Increase capture time
- Add plate solving between captures (not yet implemented)
- Use lower mount slew speeds for better settling

---

## Example Workflow

```bash
# 1. Plan your grid
# Target: Cygnus A
# Beam width: 0.5°
# Desired coverage: 2° × 2°
# Step: beam_width / 3 = 0.17°

# 2. Calculate
# Points: (2.0/0.17 + 1)² ≈ 144 points
# Time: 144 × (60s + 10s) = 2.8 hours

# 3. Test
python Testing_INDIpy.py --grid \
  --center-ra 19.99 --center-dec 40.73 \
  --width 0.2 --height 0.2 --step 0.17 \
  --name cygnus_test --capture-time 5.0

# 4. Full survey
python Testing_INDIpy.py --grid \
  --center-ra 19.99 --center-dec 40.73 \
  --width 2.0 --height 2.0 --step 0.17 \
  --name cygnus_a_survey_jan_2025 \
  --capture-time 60.0

# 5. Process data
python process_grid_data.py grid_scans/cygnus_a_survey_jan_2025/
```

---

## Summary

✅ **Automatic sequential numbering**: 0001, 0002, 0003, ...  
✅ **Organized output**: Separate directory per survey  
✅ **Rich metadata**: Position and timing for each capture  
✅ **Flexible parameters**: Custom grid size, step, and timing  
✅ **Progress tracking**: Real-time status updates  
✅ **Radio astronomy ready**: Easy integration with your receiver  

**Start small, test thoroughly, then scale up!**
