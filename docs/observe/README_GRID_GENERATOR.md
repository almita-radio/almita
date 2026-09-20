# 📐 Grid Generator - Generador de Grilla de Escaneo

## 🎯 Propósito

Genera archivos JSON con coordenadas RA/DEC para escaneo sistemático de regiones del cielo en radioastronomía.

## 🚀 Uso Básico

```bash
python grid_generator.py --center-ra 19.99 --center-dec 40.73 --width 2.0 --height 2.0 --step 0.1
```

Este comando genera una grilla de 2°×2° centrada en **Cygnus A** con puntos cada 0.1°.

## 📋 Parámetros

| Parámetro | Tipo | Descripción | Ejemplo |
|-----------|------|-------------|---------|
| `--center-ra` | float | Centro en RA (horas, 0-24) | `19.99` |
| `--center-dec` | float | Centro en DEC (grados, -90 a +90) | `40.73` |
| `--width` | float | Ancho en grados | `2.0` |
| `--height` | float | Alto en grados | `2.0` |
| `--step` | float | Separación entre puntos (grados) | `0.1` |
| `--pattern` | str | Patrón de escaneo | `snake` |
| `--output` | str | Archivo de salida JSON | `grid.json` |
| `--preview` | flag | Solo mostrar primeros 10 puntos | - |

## 🔀 Patrones de Escaneo

### 1. **RASTER** (línea por línea)
```
-----> (línea 1)
-----> (línea 2)
-----> (línea 3)
```
- Más simple
- Siempre de izquierda a derecha
- Más movimientos del telescopio

```bash
python grid_generator.py --center-ra 18.5 --center-dec 40.0 --width 3.0 --height 3.0 --step 0.15 --pattern raster
```

### 2. **SNAKE** (serpiente) ⭐ Recomendado
```
-----> (línea 1)
<----- (línea 2)
-----> (línea 3)
```
- **Más eficiente**
- Alterna dirección en cada línea
- Menos tiempo de movimiento del telescopio
- Ideal para áreas grandes

```bash
python grid_generator.py --center-ra 18.5 --center-dec 40.0 --width 3.0 --height 3.0 --step 0.15 --pattern snake
```

### 3. **SPIRAL** (espiral desde centro)
```
Empieza en el centro y va hacia afuera en espiral
```
- Útil para fuentes compactas
- Prioriza el centro de la región
- Bueno si el objetivo principal está en el centro

```bash
python grid_generator.py --center-ra 19.99 --center-dec 40.73 --width 1.5 --height 1.5 --step 0.1 --pattern spiral
```

## 📊 Ejemplos Prácticos

### Escaneo fino de Cygnus A
```bash
# Región pequeña (1°×1°) con alta resolución (0.05°)
python grid_generator.py \
    --center-ra 19.99 \
    --center-dec 40.73 \
    --width 1.0 \
    --height 1.0 \
    --step 0.05 \
    --pattern snake \
    --output cygnus_a_fine.json
```
**Resultado**: ~400 puntos, ~1 hora de captura

### Escaneo rápido de región grande
```bash
# Región grande (5°×5°) con baja resolución (0.25°)
python grid_generator.py \
    --center-ra 18.5 \
    --center-dec 40.0 \
    --width 5.0 \
    --height 5.0 \
    --step 0.25 \
    --pattern snake \
    --output wide_field.json
```
**Resultado**: ~400 puntos, ~1 hora de captura

### Preview antes de generar
```bash
# Ver primeros 10 puntos sin guardar archivo
python grid_generator.py \
    --center-ra 19.99 \
    --center-dec 40.73 \
    --width 2.0 \
    --height 2.0 \
    --step 0.1 \
    --pattern snake \
    --preview
```

## 📄 Formato del Archivo JSON

El archivo generado contiene:

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

## ⏱️ Estimación de Tiempo

El script calcula automáticamente el tiempo estimado:

| Puntos | Tiempo/punto | Tiempo total |
|--------|--------------|--------------|
| 100    | 10s          | ~17 minutos  |
| 400    | 10s          | ~1 hora      |
| 1000   | 10s          | ~2.8 horas   |

**Nota**: El tiempo por punto incluye:
- GOTO del telescopio (~3-5s)
- Estabilización (~1-2s)
- Captura de datos (~3-5s)

## 🔗 Siguientes Pasos

Una vez generado el archivo JSON, úsalo con el capturador:

```bash
# 1. Generar grilla
python grid_generator.py --center-ra 19.99 --center-dec 40.73 --width 2.0 --height 2.0 --step 0.1 --output grid.json

# 2. Capturar datos (próximo script)
python radio_capture.py --grid grid.json --output observations.json
```

## 🧮 Cálculo de Resolución

Para decidir el `--step` adecuado:

```
Ancho del haz (HPBW) de tu antena en grados
└─> Usar step = HPBW / 2 (criterio Nyquist)

Ejemplo: 
  Antena de 21cm (línea HI) con reflector de 1m
  HPBW ≈ 0.2° 
  step recomendado = 0.1°
```

## 💡 Tips

1. **Usa `--preview`** antes de generar grillas grandes
2. **Patrón snake** es casi siempre la mejor opción
3. **Step pequeño** = mejor resolución pero más tiempo
4. **Verifica coordenadas** con herramientas como Stellarium antes
5. **Considera rotación del cielo**: Para observaciones largas, el área se mueve

## 🐛 Troubleshooting

**Demasiados puntos generados**:
```bash
# Reducir resolución (aumentar step)
--step 0.2  # en vez de 0.1
```

**Muy pocos puntos**:
```bash
# Aumentar resolución (reducir step)
--step 0.05  # en vez de 0.1
```

**Coordenadas fuera de rango**:
```bash
# RA debe estar entre 0-24 horas
# DEC debe estar entre -90 y +90 grados
```

---

**Creado para**: Sistema de control INDI para radioastronomía  
**Versión**: 1.0  
**Fecha**: 2024-02-13
