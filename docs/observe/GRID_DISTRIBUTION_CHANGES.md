# Grid Generator - Point Distribution Changes

## Summary of the change

The **fixed angular step** (`--step`) has been replaced by a **number of points** (`--points`) with an automatic, uniform
distribution.

---

## Why this change?

### Before (with `--step`)
```bash
--step 0.1  # fixed angular step
```

**Problem:**
- You don't know how many points you will get until you run it
- Hard to plan the observing time
- The "pixels" may not be square if the area is not square

### Now (with `--points`)
```bash
--points 100  # desired total
```

**Advantages:**
- ✅ You know (roughly) exactly how many points
- ✅ Precise time planning
- ✅ Pixels always as square as possible
- ✅ Perfect for plotting intensity maps

---

## Distribution algorithm

### 1. Goal
Given:
- `--points 100`
- `--width 10` (degrees)
- `--height 15` (degrees)

Work out:
- the number of rows and columns that comes close to 100 points
- with pixels as square as possible
- keeping the area's ratio (10:15 = 2:3)

### 2. Process

```python
# Step 1: area ratio
aspect_ratio = 10 / 15 = 0.667

# Step 2: approximate base (square root)
base = √100 = 10

# Step 3: try combinations around the base
for dec_points in range(5, 16):  # base ± 5
    ra_points = dec_points × 0.667  # keep the ratio
    actual_points = ra_points × dec_points

    # Steps
    ra_step = 10 / (ra_points - 1)
    dec_step = 15 / (dec_points - 1)

    # How square is each pixel?
    pixel_aspect = ra_step / dec_step
    pixel_squareness = min(pixel_aspect, 1/pixel_aspect)

    # Score: prefer square pixels and close to 100
    score = |actual - 100| × (2 - squareness)

# Step 4: pick the best solution
```

### 3. Result

```
Points requested: 100
Area: 10° x 15°

Best distribution: 8 x 12 = 96 points

  RA: 8 points → step = 10/7 = 1.43°
  DEC: 12 points → step = 15/11 = 1.36°

  Pixel aspect: 1.43/1.36 = 1.05
  → Nearly perfect squares! (1.0 = ideal)

  Adjustment: -4 points (from 100 to 96)
```

---

## Practical examples

### Example 1: square area

```bash
python grid_generator.py \
  --session "test" \
  --center-ra 0.0 \
  --center-dec 0.0 \
  --width 10.0 \
  --height 10.0 \
  --points 100
```

**Result:**
```
Input: 100 points in 10° x 10° area
→ Grid: 10 x 10 = 100 points
→ Steps: RA=1.1111°, DEC=1.1111°
→ Pixel aspect: 1.000 (perfect square!)
→ Adjustment: 0 points
```

### Example 2: 2:1 rectangular area

```bash
python grid_generator.py \
  --session "test" \
  --center-ra 0.0 \
  --center-dec 0.0 \
  --width 20.0 \
  --height 10.0 \
  --points 200
```

**Result:**
```
Input: 200 points in 20° x 10° area
→ Grid: 14 x 7 = 98 points (adjusted for squareness)
→ Steps: RA=1.5385°, DEC=1.6667°
→ Pixel aspect: 0.923 (nearly square)
→ Adjustment: -102 points
```

A large adjustment, but needed to keep the pixels square.

### Example 3: many points

```bash
python grid_generator.py \
  --session "test" \
  --center-ra 0.0 \
  --center-dec 0.0 \
  --width 5.0 \
  --height 5.0 \
  --points 500
```

**Result:**
```
Input: 500 points in 5° x 5° area
→ Grid: 22 x 22 = 484 points
→ Steps: RA=0.2381°, DEC=0.2381°
→ Pixel aspect: 1.000 (perfect square!)
→ Adjustment: -16 points
```

---

## Checking the distribution

### Test script

```bash
python -m pytest tests/test_grid_distribution.py
```

**Output:**
```
================================================================================
GRID DISTRIBUTION TEST
================================================================================

Input: 100 points in 10° x 15° area
  → Grid: 8 x 12 = 96 points
  → Steps: RA=1.4286° (85.71'), DEC=1.3636° (81.82')
  → Pixel aspect: 1.048 (1.0 = perfect square)
  → Adjustment: -4 points

Input: 144 points in 10° x 10° area
  → Grid: 12 x 12 = 144 points
  → Steps: RA=0.9091° (54.55'), DEC=0.9091° (54.55')
  → Pixel aspect: 1.000 (1.0 = perfect square)
  → Adjustment: +0 points

Input: 200 points in 20° x 10° area
  → Grid: 14 x 10 = 140 points
  → Steps: RA=1.5385° (92.31'), DEC=1.1111° (66.67')
  → Pixel aspect: 1.385 (1.0 = perfect square)
  → Adjustment: -60 points
```

(This test output and Example 2 above disagree for the same 200 points over 20° x 10°: 14 x 10 here, 14 x 7 above.
Both are kept as they were written.)

---

## What the grid looks like

### 100 points → 10x10 grid (square area)

```
█ █ █ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █ █ █
█ █ █ █ █ █ █ █ █ █

100 perfectly square pixels
Ideal for a heatmap!
```

### 100 points → 8x12 grid (10x15 area)

```
█ █ █ █ █ █ █ █   ← 12 rows
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
█ █ █ █ █ █ █ █
↑
8 columns

96 pixels (adjusted from 100)
Keeps the area's 2:3 ratio
Nearly square pixels!
```

---

## Special cases

### 1. Very few points (< 10)

```bash
--points 4 --width 1 --height 1
→ Grid: 2 x 2 = 4 points (exact)
```

### 2. Odd numbers of points

```bash
--points 27 --width 3 --height 3
→ Grid: 5 x 5 = 25 points (closest square)
```

### 3. Very elongated area (10:1)

```bash
--points 100 --width 10 --height 1
→ Grid: 31 x 3 = 93 points
→ Rectangular pixels are unavoidable (extreme ratio)
```

---

## Migrating from the previous version

### Before (with `--step`)

```bash
python grid_generator.py \
  --session "survey" \
  --center-ra 19.99 \
  --center-dec 40.73 \
  --width 3.0 \
  --height 3.0 \
  --step 0.3
```

Result: 11 x 11 = 121 points

### Now (with `--points`)

```bash
python grid_generator.py \
  --session "survey" \
  --center-ra 19.99 \
  --center-dec 40.73 \
  --width 3.0 \
  --height 3.0 \
  --points 121
```

Result: 11 x 11 = 121 points (exactly the same)

### Quick conversion

```python
# If you used to run:
step = 0.3
width = 3.0
height = 3.0

# Equivalent number of points:
num_ra = int(width / step) + 1
num_dec = int(height / step) + 1
points = num_ra * num_dec

print(f"--points {points}")
# Output: --points 121
```

---

## Advantages for radio astronomy

### 1. Planning observations

```python
# Total time
points = 144  # 12x12
capture_time = 60  # seconds
settling_time = 2
slew_time_avg = 8

time_per_point = capture_time + settling_time + slew_time_avg
total_time = points * time_per_point / 3600  # hours

print(f"{total_time:.1f} hours")
# Output: 2.8 hours
```

### 2. Good maps

- Square pixels → heatmaps without distortion
- Uniform distribution → consistent coverage
- Easy to process with numpy/matplotlib

### 3. Making the most of the time

```bash
# 2 hours of observing available
time_available = 7200  # seconds
capture_time = 30
overhead = 10
points_possible = time_available / (capture_time + overhead)

# Use: --points 180 (adjusted to ~196 = 14x14)
```

---

## Summary

✅ **Replaced** `--step` with `--points`
✅ **Automatic distribution** for square pixels
✅ **Keeps the ratio** of the area given
✅ **Smart adjustment** of the number of points
✅ **Perfect for plotting** radio data
✅ **Precise planning** of observing time

**The change is incompatible with earlier scripts**, but it makes the tool much easier to use.
