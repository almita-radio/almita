# Grid Generator - Usage Guide and CSV Structure

## Summary

`grid_generator.py` runs grid scans by driving the mount, and saves everything in a CSV file with **26 data fields**.

**POINT DISTRIBUTION:** instead of giving an angular step, you give the **total number of points** and the script works out
the best distribution to get uniform **square pixels**.

**NOTE:** the mount's real coordinates are not read back after the GOTO, to keep the scan fast.

---

## Output structure

```
./data/
└── 20250115_143022/              ← session timestamp
    ├── cygnus_a_survey.csv       ← main CSV file
    └── captures/                  ← data files
        ├── cygnus_a_survey_0001.dat
        ├── cygnus_a_survey_0001.meta
        ├── cygnus_a_survey_0002.dat
        ├── cygnus_a_survey_0002.meta
        └── ...
```

---

## Basic use

### Example 1: simple scan (100 points)

```bash
python grid_generator.py \
  --session "cygnus_a_survey" \
  --center-ra 19.99 \
  --center-dec 40.73 \
  --points 100
```

**Result:**
- CSV: `./data/20250115_143022/cygnus_a_survey.csv`
- Area: 1° x 1° (default)
- Points: ~100 (adjusted to 100 = a 10x10 grid for square pixels)

### Example 2: rectangular scan (150 points over 2°x3°)

```bash
python grid_generator.py \
  --session "m51_detailed" \
  --center-ra 13.5 \
  --center-dec 47.2 \
  --width 2.0 \
  --height 3.0 \
  --points 150
```

**Result:**
- Area: 2° x 3°
- Area ratio: 2:3 = 0.667
- Points requested: 150
- **Computed distribution:** 10x15 = 150 points ✅ (ratio = 0.667, perfect!)
- RA step: 0.222°, DEC step: 0.214° → nearly square pixels

### Example 3: quick test (25 points)

```bash
python grid_generator.py \
  --session "test_grid" \
  --center-ra 0.0 \
  --center-dec 0.0 \
  --width 0.5 \
  --height 0.5 \
  --points 25
```

**Result:**
- Area: 0.5° x 0.5° (square)
- Points requested: 25
- **Computed distribution:** 5x5 = 25 points ✅ (perfectly square pixels)

---

## How the automatic distribution works

### Goal
Build a grid where each "pixel" is as **square** as possible, so that intensity maps look right.

### Example: 100 points over 10° x 15°

```
Area: 10° x 15°
Ratio: 10/15 = 0.667

Possible options:
1. 10x10 = 100 points → grid ratio = 1.0   ❌ (ignores the shape of the area)
2. 8x12 = 96 points   → grid ratio = 0.667 ✅ (keeps the shape, square pixels)
3. 9x11 = 99 points   → grid ratio = 0.818 ⚠️  (rectangular pixels)

The script picks: 8x12 = 96 points
  RA step: 10/7 = 1.43°
  DEC step: 15/11 = 1.36°
  Pixel aspect: 1.43/1.36 = 1.05 (nearly square!)
```

### What the result looks like

```
8 columns x 12 rows = 96 pixels

█ █ █ █ █ █ █ █   ← row 12 (highest DEC)
█ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █   ← row 1 (lowest DEC)

Ready to plot radio intensity!
```

---

## CSV fields (26 columns)

### 1-3: identification and position in the grid

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `point_number` | int | Sequential point number | 1, 2, 3, ... |
| `grid_row` | int | Grid row (0-based) | 0, 1, 2, ... |
| `grid_col` | int | Grid column (0-based) | 0, 1, 2, ... |

### 4-7: target sky coordinates

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `target_ra_hours` | float | Target RA (decimal hours) | 19.990000 |
| `target_dec_degrees` | float | Target DEC (decimal degrees) | 40.730000 |
| `target_ra_hms` | string | RA as HH:MM:SS.SSS | 19:59:24.000 |
| `target_dec_dms` | string | DEC as ±DD:MM:SS.SS | +40:43:48.00 |

### 8-11: times and duration

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `start_time_utc` | ISO 8601 | Capture start (UTC) | 2025-01-15T14:30:22.123456+00:00 |
| `end_time_utc` | ISO 8601 | Capture end (UTC) | 2025-01-15T14:30:27.123456+00:00 |
| `duration_seconds` | float | Actual capture duration | 5.00 |
| `timestamp_unix` | int | Unix timestamp | 1705329022 |

### 12-15: files and status

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `data_filename` | string | Data file name | cygnus_a_survey_0042.dat |
| `metadata_filename` | string | Metadata file name | cygnus_a_survey_0042.meta |
| `capture_status` | string | Status: success/failed/skipped | success |
| `error_message` | string | Error message (if it failed) | GOTO command failed |

### 16-18: session information

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `session_name` | string | Session name | cygnus_a_survey |
| `session_timestamp` | ISO 8601 | Session start | 2025-01-15T14:25:00.000000+00:00 |
| `scan_id` | string | Unique session ID | 20250115_142500 |

### 19-21: observation parameters

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `capture_duration_planned` | float | Planned duration (seconds) | 5.0 |
| `slew_time_seconds` | float | Slew time | 8.45 |
| `settling_time_seconds` | float | Settling time | 2.0 |

### 22-24: horizontal coordinates (optional)

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `azimuth_degrees` | float | Azimuth in degrees | 235.45 |
| `altitude_degrees` | float | Altitude in degrees | 45.67 |
| `airmass` | float | Airmass | 1.42 |

### 25-26: notes and comments

| Field | Type | Description | Example |
|-------|------|-------------|---------|
| `notes` | string | Free notes | Strong source detected |
| `weather_conditions` | string | Weather conditions | Clear sky, 15°C |

---

## Command parameters

### Required

```bash
--session "session_name"         # Name for the CSV
--center-ra 19.99               # Centre RA (hours)
--center-dec 40.73              # Centre DEC (degrees)
--points 100                    # Number of points (adjusted for uniformity)
```

### Optional - INDI connection

```bash
--host localhost                # INDI server (default: localhost)
--port 7624                     # INDI port (default: 7624)
--device "EQMod Mount"          # Device name (default: auto)
```

### Optional - grid area

```bash
--width 1.0                     # Width in degrees (default: 1.0)
--height 1.0                    # Height in degrees (default: 1.0)
```

### Optional - capture

```bash
--capture-time 5.0              # Capture duration (default: 5.0s)
--settling-time 2.0             # Settling time (default: 2.0s)
--data-dir ./data               # Base directory (default: ./data)
```

---

## Real-use examples

### 1. Cygnus A survey (100 points)

```bash
python grid_generator.py \
  --session "cygnus_a_1420mhz_night1" \
  --center-ra 19.99 \
  --center-dec 40.73 \
  --width 3.0 \
  --height 3.0 \
  --points 100 \
  --capture-time 60.0
```

**Result:**
- 10 x 10 = 100 points (perfectly square pixels)
- ~2 hours of observing
- CSV: `cygnus_a_1420mhz_night1.csv`

### 2. Map of M51 (200 points over a rectangular area)

```bash
python grid_generator.py \
  --session "m51_continuum_survey" \
  --center-ra 13.495 \
  --center-dec 47.195 \
  --width 1.5 \
  --height 2.0 \
  --points 200 \
  --capture-time 30.0
```

**Result:**
- ~12 x 16 = 192 points (adjusted from 200 for square pixels)
- Area ratio kept
- ~2 hours of observing

### 3. Quick calibration test (25 points)

```bash
python grid_generator.py \
  --session "calibration_test" \
  --center-ra 12.0 \
  --center-dec 30.0 \
  --width 0.5 \
  --height 0.5 \
  --points 25 \
  --capture-time 3.0 \
  --settling-time 1.0
```

**Result:**
- 5 x 5 = 25 points (perfect!)
- ~2 minutes of testing

### 4. Large survey (500 points)

```bash
python grid_generator.py \
  --session "galactic_plane_survey" \
  --center-ra 18.0 \
  --center-dec 0.0 \
  --width 10.0 \
  --height 5.0 \
  --points 500 \
  --capture-time 120.0
```

**Result:**
- ~23 x 22 = 506 points (adjusted from 500)
- 10:5 = 2:1 ratio kept
- ~17 hours of observing (several nights)

---

## Processing the CSV

### Example 1: reading it with Python

```python
import pandas as pd

# Read the CSV
df = pd.read_csv('./data/20250115_143022/cygnus_a_survey.csv')

# Keep the successful captures
successful = df[df['capture_status'] == 'success']

print(f"Successful captures: {len(successful)}/{len(df)}")
print(f"Mean RA error: {successful['position_error_ra_arcmin'].mean():.2f}'")
print(f"Mean DEC error: {successful['position_error_dec_arcmin'].mean():.2f}'")
```

### Example 2: basic statistics

```python
import pandas as pd

df = pd.read_csv('./data/20250115_143022/cygnus_a_survey.csv')

# Capture times
print("Capture times:")
print(df['duration_seconds'].describe())

# Position errors
print("\nPointing errors (arcmin):")
print(df['position_error_ra_arcmin'].describe())
print(df['position_error_dec_arcmin'].describe())
```

### Example 3: plotting the grid

```python
import pandas as pd
import matplotlib.pyplot as plt

df = pd.read_csv('./data/20250115_143022/cygnus_a_survey.csv')

# Plot the positions
plt.figure(figsize=(10, 8))
plt.scatter(df['target_ra_hours'], df['target_dec_degrees'],
            c=df['point_number'], cmap='viridis')
plt.colorbar(label='Point Number')
plt.xlabel('RA (hours)')
plt.ylabel('DEC (degrees)')
plt.title('Grid Scan Pattern')
plt.grid(True)
plt.show()
```

---

## Integrating a radio receiver

### Step 1: find the capture point

In `grid_generator.py`, around line 347:

```python
# HERE: Integrate your radio astronomy data capture
# For now, simulate with sleep
await asyncio.sleep(capture_duration)
```

### Step 2: replace it with your code

```python
# Example for an SDR
from your_radio_module import capture_spectrum

# Start capture
start_time = datetime.now(timezone.utc)
capture_data['start_time_utc'] = start_time.isoformat()

# Capture real data
data_path = self.data_subdir / capture_data['data_filename']
await capture_spectrum(
    output_file=data_path,
    duration=capture_duration,
    center_freq=1420.405,  # MHz (HI line)
    bandwidth=2.0,          # MHz
    gain=40                 # dB
)

end_time = datetime.now(timezone.utc)
```

---

## Important notes

### 1. Directory structure

Each session creates a directory with a unique timestamp:
```
./data/20250115_143022/    ← never overwritten
```

### 2. Session names

Use descriptive names:
- ✅ `cygnus_a_1420mhz_jan15`
- ✅ `m51_survey_night1`
- ✅ `galactic_plane_5ghz`
- ❌ `test` (not descriptive)
- ❌ `scan1` (not descriptive)

### 3. Working out the time

Estimated time per point:
```
point_time = capture_time + settling_time + slew_time (approx. 5-15s)
total_time = num_points × point_time
```

### 4. Position errors

Errors are given in:
- **RA**: minutes of time (converted from hours)
- **DEC**: arcminutes (converted from degrees)

### 5. The CSV always has 26 columns

Optional fields are left empty when not available:
- `azimuth_degrees`, `altitude_degrees`, `airmass`
- `notes`, `weather_conditions`

**OPTIMISATION:** the mount's real coordinates are not read back after the GOTO, which saves ~0.5-1 s per point. That
makes long scans noticeably faster.

---

## Troubleshooting

### Problem: the CSV is not created

**Fix:**
```bash
# Check permissions
ls -ld ./data
mkdir -p ./data
chmod 755 ./data
```

### Problem: columns missing from the CSV

**Check:**
```bash
# Count the columns
head -1 ./data/*/session.csv | tr ',' '\n' | wc -l
# Should print: 30
```

### Problem: captures fail

**Check the log:**
- Message `GOTO command failed` → check that the mount is connected
- The CSV will have `capture_status = 'failed'`
- Look at `error_message` for the diagnosis

---

## Advantages in short

✅ **26 optimised fields** for efficient analysis
✅ **Standard CSV**, easy to process (pandas, Excel, etc.)
✅ **Organised by timestamp**
✅ **Sequential numbering** of captures
✅ **Full metadata** for every point
✅ **Precise times** (UTC, ISO 8601)
✅ **Full traceability** of the session
✅ **Optimised for speed** (does not read back the mount's real position)

> **Historical document.** This guide describes an early version of `grid_generator.py`. Some details no longer
> match each other or the current code: the column-count check says 30 while the text says 26, and the Python examples read
> `position_error_*` columns that are not in the field list. ALMITA's current planning goes through `observation_plan.py` and the
> OBSERVE page.
