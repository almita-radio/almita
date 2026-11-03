# 📐 Grid Generator - Scan Grid Generator

## 🎯 Purpose

Generates JSON files with RA/DEC coordinates for systematic scans of sky regions in radio astronomy.

## 🚀 Basic use

```bash
python grid_generator.py --center-ra 19.99 --center-dec 40.73 --width 2.0 --height 2.0 --step 0.1
```

This command generates a 2°×2° grid centred on **Cygnus A** with a point every 0.1°.

## 📋 Parameters

| Parameter | Type | Description | Example |
|-----------|------|-------------|---------|
| `--center-ra` | float | Centre in RA (hours, 0-24) | `19.99` |
| `--center-dec` | float | Centre in DEC (degrees, -90 to +90) | `40.73` |
| `--width` | float | Width in degrees | `2.0` |
| `--height` | float | Height in degrees | `2.0` |
| `--step` | float | Spacing between points (degrees) | `0.1` |
| `--pattern` | str | Scan pattern | `snake` |
| `--output` | str | Output JSON file | `grid.json` |
| `--preview` | flag | Only show the first 10 points | - |

## 🔀 Scan patterns

### 1. **RASTER** (line by line)
```
-----> (line 1)
-----> (line 2)
-----> (line 3)
```
- Simplest
- Always left to right
- More telescope movement

```bash
python grid_generator.py --center-ra 18.5 --center-dec 40.0 --width 3.0 --height 3.0 --step 0.15 --pattern raster
```

### 2. **SNAKE** ⭐ Recommended
```
-----> (line 1)
<----- (line 2)
-----> (line 3)
```
- **Most efficient**
- Alternates direction on each line
- Less telescope movement time
- Ideal for large areas

```bash
python grid_generator.py --center-ra 18.5 --center-dec 40.0 --width 3.0 --height 3.0 --step 0.15 --pattern snake
```

### 3. **SPIRAL** (outward from the centre)
```
Starts at the centre and spirals outward
```
- Useful for compact sources
- Gives the centre of the region priority
- Good when the main target is at the centre

```bash
python grid_generator.py --center-ra 19.99 --center-dec 40.73 --width 1.5 --height 1.5 --step 0.1 --pattern spiral
```

## 📊 Practical examples

### Fine scan of Cygnus A
```bash
# Small region (1°×1°) at high resolution (0.05°)
python grid_generator.py \
    --center-ra 19.99 \
    --center-dec 40.73 \
    --width 1.0 \
    --height 1.0 \
    --step 0.05 \
    --pattern snake \
    --output cygnus_a_fine.json
```
**Result**: ~400 points, ~1 hour of capture

### Quick scan of a large region
```bash
# Large region (5°×5°) at low resolution (0.25°)
python grid_generator.py \
    --center-ra 18.5 \
    --center-dec 40.0 \
    --width 5.0 \
    --height 5.0 \
    --step 0.25 \
    --pattern snake \
    --output wide_field.json
```
**Result**: ~400 points, ~1 hour of capture

### Preview before generating
```bash
# See the first 10 points without saving a file
python grid_generator.py \
    --center-ra 19.99 \
    --center-dec 40.73 \
    --width 2.0 \
    --height 2.0 \
    --step 0.1 \
    --pattern snake \
    --preview
```

## 📄 JSON file format

The generated file contains:

```json
{
  "metadata": {
    "generated_at": "2024-02-13T20:30:00",
    "generator_version": "1.0",
    "description": "Grid de escaneo de cielo - Patrón snake"
  },
  "configuration": {
    "center_ra_hours": 19.99,
    "center_dec_degrees": 40.73,
    "width_degrees": 2.0,
    "height_degrees": 2.0,
    "step_degrees": 0.1,
    "scan_pattern": "snake"
  },
  "statistics": {
    "total_points": 441,
    "ra_min": 19.8567,
    "ra_max": 20.1233,
    "dec_min": 39.73,
    "dec_max": 41.73
  },
  "points": [
    {
      "id": 0,
      "ra_hours": 19.8567,
      "dec_degrees": 39.73,
      "row": 0,
      "col": 0,
      "pattern": "snake",
      "direction": "forward"
    },
    ...
  ]
}
```

(The `description` string is written in Spanish by the generator itself.)

## ⏱️ Time estimate

The script computes the estimated time automatically:

| Points | Time/point | Total time |
|--------|------------|------------|
| 100    | 10s        | ~17 minutes |
| 400    | 10s        | ~1 hour    |
| 1000   | 10s        | ~2.8 hours |

**Note**: the time per point includes:
- the telescope GOTO (~3-5s)
- settling (~1-2s)
- data capture (~3-5s)

## 🔗 Next steps

Once the JSON file is generated, use it with the capture program:

```bash
# 1. Generate the grid
python grid_generator.py --center-ra 19.99 --center-dec 40.73 --width 2.0 --height 2.0 --step 0.1 --output grid.json

# 2. Capture data (next script)
python radio_capture.py --grid grid.json --output observations.json
```

## 🧮 Working out the resolution

To choose a suitable `--step`:

```
Beam width (HPBW) of your antenna in degrees
└─> Use step = HPBW / 2 (Nyquist criterion)

Example:
  21 cm (HI line) antenna with a 1 m reflector
  HPBW ≈ 70 λ / D ≈ 70 × 0.21 m / 1 m ≈ 15°
  recommended step ≈ 7.5°
```

(An earlier version of this page said HPBW ≈ 0.2° for a 1 m dish, which is off by a factor of ~70. ALMITA's ~90 × 60 cm dish
is planned with a provisional 20° beam.)

## 💡 Tips

1. **Use `--preview`** before generating large grids
2. **The snake pattern** is almost always the best option
3. **A small step** = better resolution but more time
4. **Check the coordinates** beforehand with tools such as Stellarium
5. **Mind the sky's rotation**: during long observations the area moves

## 🐛 Troubleshooting

**Too many points generated**:
```bash
# Lower the resolution (increase step)
--step 0.2  # instead of 0.1
```

**Too few points**:
```bash
# Raise the resolution (reduce step)
--step 0.05  # instead of 0.1
```

**Coordinates out of range**:
```bash
# RA must be between 0-24 hours
# DEC must be between -90 and +90 degrees
```

---

**Written for**: INDI control system for radio astronomy
**Version**: 1.0
**Date**: 2024-02-13
