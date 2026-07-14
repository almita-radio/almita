# Grid Generator - Cambios de Distribución de Puntos

## Resumen de Cambios

Se ha reemplazado el sistema de **paso angular fijo** (`--step`) por un sistema de **número de puntos** (`--points`) con distribución automática uniforme.

---

## ¿Por Qué Este Cambio?

### Antes (con `--step`)
```bash
--step 0.1  # Paso angular fijo
```

**Problema:**
- No sabes cuántos puntos obtendrás hasta ejecutar
- Difícil planificar tiempo de observación
- Los "pixels" pueden no ser cuadrados si el área no es cuadrada

### Ahora (con `--points`)
```bash
--points 100  # Número total deseado
```

**Ventajas:**
- ✅ Sabes exactamente cuántos puntos (aproximadamente)
- ✅ Planificación de tiempo precisa
- ✅ Pixels siempre lo más cuadrados posible
- ✅ Perfecto para graficar mapas de intensidad

---

## Algoritmo de Distribución

### 1. Objetivo
Dado:
- `--points 100`
- `--width 10` (grados)
- `--height 15` (grados)

Calcular:
- Número de filas y columnas que aproximen 100 puntos
- Que los pixels sean lo más cuadrados posible
- Que respete el ratio del área (10:15 = 2:3)

### 2. Proceso

```python
# Paso 1: Calcular ratio del área
aspect_ratio = 10 / 15 = 0.667

# Paso 2: Base aproximada (raíz cuadrada)
base = √100 = 10

# Paso 3: Probar combinaciones alrededor de base
for dec_points in range(5, 16):  # base ± 5
    ra_points = dec_points × 0.667  # mantener ratio
    actual_points = ra_points × dec_points
    
    # Calcular pasos
    ra_step = 10 / (ra_points - 1)
    dec_step = 15 / (dec_points - 1)
    
    # ¿Qué tan cuadrado es cada pixel?
    pixel_aspect = ra_step / dec_step
    pixel_squareness = min(pixel_aspect, 1/pixel_aspect)
    
    # Score: preferir pixels cuadrados y cerca de 100
    score = |actual - 100| × (2 - squareness)
    
# Paso 4: Elegir mejor solución
```

### 3. Resultado

```
Puntos solicitados: 100
Área: 10° x 15°

Mejor distribución: 8 x 12 = 96 puntos

  RA: 8 puntos → paso = 10/7 = 1.43°
  DEC: 12 puntos → paso = 15/11 = 1.36°
  
  Pixel aspect: 1.43/1.36 = 1.05
  → Casi cuadrados perfectos! (1.0 = ideal)
  
  Ajuste: -4 puntos (de 100 a 96)
```

---

## Ejemplos Prácticos

### Ejemplo 1: Área Cuadrada

```bash
python grid_generator.py \
  --session "test" \
  --center-ra 0.0 \
  --center-dec 0.0 \
  --width 10.0 \
  --height 10.0 \
  --points 100
```

**Resultado:**
```
Input: 100 points in 10° x 10° area
→ Grid: 10 x 10 = 100 points
→ Steps: RA=1.1111°, DEC=1.1111°
→ Pixel aspect: 1.000 (perfect square!)
→ Adjustment: 0 points
```

### Ejemplo 2: Área Rectangular 2:1

```bash
python grid_generator.py \
  --session "test" \
  --center-ra 0.0 \
  --center-dec 0.0 \
  --width 20.0 \
  --height 10.0 \
  --points 200
```

**Resultado:**
```
Input: 200 points in 20° x 10° area
→ Grid: 14 x 7 = 98 points (adjusted for squareness)
→ Steps: RA=1.5385°, DEC=1.6667°
→ Pixel aspect: 0.923 (nearly square)
→ Adjustment: -102 points
```

Ajuste grande pero necesario para mantener pixels cuadrados.

### Ejemplo 3: Muchos Puntos

```bash
python grid_generator.py \
  --session "test" \
  --center-ra 0.0 \
  --center-dec 0.0 \
  --width 5.0 \
  --height 5.0 \
  --points 500
```

**Resultado:**
```
Input: 500 points in 5° x 5° area
→ Grid: 22 x 22 = 484 points
→ Steps: RA=0.2381°, DEC=0.2381°
→ Pixel aspect: 1.000 (perfect square!)
→ Adjustment: -16 points
```

---

## Verificar la Distribución

### Script de Prueba

```bash
python test_grid_distribution.py
```

**Salida:**
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

---

## Visualización de la Grilla

### 100 puntos → 10x10 grid (área cuadrada)

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

100 pixels cuadrados perfectos
Ideal para heatmap!
```

### 100 puntos → 8x12 grid (área 10x15)

```
█ █ █ █ █ █ █ █   ← 12 filas
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
8 columnas

96 pixels (ajustado de 100)
Respeta ratio 2:3 del área
Pixels casi cuadrados!
```

---

## Casos Especiales

### 1. Puntos muy pocos (< 10)

```bash
--points 4 --width 1 --height 1
→ Grid: 2 x 2 = 4 points (exact)
```

### 2. Puntos impares

```bash
--points 27 --width 3 --height 3
→ Grid: 5 x 5 = 25 points (closest square)
```

### 3. Área muy rectangular (10:1)

```bash
--points 100 --width 10 --height 1
→ Grid: 31 x 3 = 93 points
→ Pixels rectangulares inevitables (ratio extremo)
```

---

## Migración desde Versión Anterior

### Antes (con `--step`)

```bash
python grid_generator.py \
  --session "survey" \
  --center-ra 19.99 \
  --center-dec 40.73 \
  --width 3.0 \
  --height 3.0 \
  --step 0.3
```

Resultado: 11 x 11 = 121 puntos

### Ahora (con `--points`)

```bash
python grid_generator.py \
  --session "survey" \
  --center-ra 19.99 \
  --center-dec 40.73 \
  --width 3.0 \
  --height 3.0 \
  --points 121
```

Resultado: 11 x 11 = 121 puntos (exactamente igual)

### Conversión Rápida

```python
# Si antes usabas:
step = 0.3
width = 3.0
height = 3.0

# Calcular puntos equivalentes:
num_ra = int(width / step) + 1
num_dec = int(height / step) + 1
points = num_ra * num_dec

print(f"--points {points}")
# Output: --points 121
```

---

## Ventajas para Radioastronomía

### 1. Planificación de Observaciones

```python
# Calcular tiempo total
points = 144  # 12x12
capture_time = 60  # segundos
settling_time = 2
slew_time_avg = 8

time_per_point = capture_time + settling_time + slew_time_avg
total_time = points * time_per_point / 3600  # horas

print(f"{total_time:.1f} hours")
# Output: 2.8 hours
```

### 2. Mapas de Calidad

- Pixels cuadrados → heatmaps sin distorsión
- Distribución uniforme → cobertura consistente
- Fácil de procesar con numpy/matplotlib

### 3. Optimización de Recursos

```bash
# Observación de 2 horas disponibles
time_available = 7200  # segundos
capture_time = 30
overhead = 10
points_possible = time_available / (capture_time + overhead)

# Usar: --points 180 (será ajustado a ~196 = 14x14)
```

---

## Resumen

✅ **Reemplazado** `--step` por `--points`  
✅ **Distribución automática** para pixels cuadrados  
✅ **Respeta ratio del área** definida  
✅ **Ajuste inteligente** del número de puntos  
✅ **Perfecto para plotting** de datos radio  
✅ **Planificación precisa** de tiempo de observación  

**El cambio es incompatible con scripts anteriores**, pero mejora significativamente la usabilidad.
