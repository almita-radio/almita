# Grid Generator - Guía de Uso y Estructura CSV

## Resumen

El `grid_generator.py` ejecuta escaneos de grilla controlando la montura y guardando toda la información en un archivo CSV con **26 campos** de datos.

**DISTRIBUCIÓN DE PUNTOS:** En lugar de especificar un paso angular, defines el **número total de puntos** y el script calcula automáticamente la mejor distribución para crear **pixels cuadrados** uniformes.

**NOTA:** No se leen las coordenadas reales de la montura después del GOTO para optimizar el tiempo de escaneo.

---

## Estructura de Salida

```
./data/
└── 20250115_143022/              ← Timestamp de la sesión
    ├── cygnus_a_survey.csv       ← Archivo CSV principal
    └── captures/                  ← Archivos de datos
        ├── cygnus_a_survey_0001.dat
        ├── cygnus_a_survey_0001.meta
        ├── cygnus_a_survey_0002.dat
        ├── cygnus_a_survey_0002.meta
        └── ...
```

---

## Uso Básico

### Ejemplo 1: Escaneo Simple (100 puntos)

```bash
python grid_generator.py \
  --session "cygnus_a_survey" \
  --center-ra 19.99 \
  --center-dec 40.73 \
  --points 100
```

**Resultado:**
- CSV: `./data/20250115_143022/cygnus_a_survey.csv`
- Área: 1° x 1° (por defecto)
- Puntos: ~100 (ajustado a 100 = 10x10 grid para pixels cuadrados)

### Ejemplo 2: Escaneo Rectangular (150 puntos en área 2°x3°)

```bash
python grid_generator.py \
  --session "m51_detailed" \
  --center-ra 13.5 \
  --center-dec 47.2 \
  --width 2.0 \
  --height 3.0 \
  --points 150
```

**Resultado:**
- Área: 2° x 3°
- Ratio área: 2:3 = 0.667
- Puntos solicitados: 150
- **Distribución calculada:** 10x15 = 150 puntos ✅ (ratio = 0.667, perfecto!)
- Paso RA: 0.222°, Paso DEC: 0.214° → Pixels casi cuadrados

### Ejemplo 3: Prueba Rápida (25 puntos)

```bash
python grid_generator.py \
  --session "test_grid" \
  --center-ra 0.0 \
  --center-dec 0.0 \
  --width 0.5 \
  --height 0.5 \
  --points 25
```

**Resultado:**
- Área: 0.5° x 0.5° (cuadrada)
- Puntos solicitados: 25
- **Distribución calculada:** 5x5 = 25 puntos ✅ (pixels cuadrados perfectos)

---

## Cómo Funciona la Distribución Automática

### Objetivo
Crear una grilla donde cada "pixel" sea lo más **cuadrado** posible para que los mapas de intensidad se vean bien.

### Ejemplo: 100 puntos en 10° x 15°

```
Área: 10° x 15°
Ratio: 10/15 = 0.667

Opciones posibles:
1. 10x10 = 100 puntos → ratio grilla = 1.0  ❌ (no respeta forma del área)
2. 8x12 = 96 puntos  → ratio grilla = 0.667 ✅ (respeta forma, pixels cuadrados)
3. 9x11 = 99 puntos  → ratio grilla = 0.818 ⚠️  (pixels rectangulares)

El script elige: 8x12 = 96 puntos
  RA step: 10/7 = 1.43°
  DEC step: 15/11 = 1.36°
  Pixel aspect: 1.43/1.36 = 1.05 (casi cuadrado!)
```

### Visualización del Resultado

```
8 columnas x 12 filas = 96 pixels

█ █ █ █ █ █ █ █   ← fila 12 (DEC máxima)
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
█ █ █ █ █ █ █ █   ← fila 1 (DEC mínima)

Perfecto para graficar intensidad de radio!
```

---

## Campos del CSV (26 columnas)

### 1-3: Identificación y Posición en Grilla

| Campo | Tipo | Descripción | Ejemplo |
|-------|------|-------------|---------|
| `point_number` | int | Número secuencial del punto | 1, 2, 3, ... |
| `grid_row` | int | Fila en la grilla (0-based) | 0, 1, 2, ... |
| `grid_col` | int | Columna en la grilla (0-based) | 0, 1, 2, ... |

### 4-7: Coordenadas Celestes Objetivo

| Campo | Tipo | Descripción | Ejemplo |
|-------|------|-------------|---------|
| `target_ra_hours` | float | RA objetivo (horas decimales) | 19.990000 |
| `target_dec_degrees` | float | DEC objetivo (grados decimales) | 40.730000 |
| `target_ra_hms` | string | RA en formato HH:MM:SS.SSS | 19:59:24.000 |
| `target_dec_dms` | string | DEC en formato ±DD:MM:SS.SS | +40:43:48.00 |

### 8-11: Tiempos y Duración

| Campo | Tipo | Descripción | Ejemplo |
|-------|------|-------------|---------|
| `start_time_utc` | ISO 8601 | Inicio de captura (UTC) | 2025-01-15T14:30:22.123456+00:00 |
| `end_time_utc` | ISO 8601 | Fin de captura (UTC) | 2025-01-15T14:30:27.123456+00:00 |
| `duration_seconds` | float | Duración real de captura | 5.00 |
| `timestamp_unix` | int | Timestamp Unix | 1705329022 |

### 12-15: Archivos y Estado

| Campo | Tipo | Descripción | Ejemplo |
|-------|------|-------------|---------|
| `data_filename` | string | Nombre archivo de datos | cygnus_a_survey_0042.dat |
| `metadata_filename` | string | Nombre archivo de metadatos | cygnus_a_survey_0042.meta |
| `capture_status` | string | Estado: success/failed/skipped | success |
| `error_message` | string | Mensaje de error (si falló) | GOTO command failed |

### 16-18: Información de Sesión

| Campo | Tipo | Descripción | Ejemplo |
|-------|------|-------------|---------|
| `session_name` | string | Nombre de sesión | cygnus_a_survey |
| `session_timestamp` | ISO 8601 | Inicio de sesión | 2025-01-15T14:25:00.000000+00:00 |
| `scan_id` | string | ID único de sesión | 20250115_142500 |

### 19-21: Parámetros de Observación

| Campo | Tipo | Descripción | Ejemplo |
|-------|------|-------------|---------|
| `capture_duration_planned` | float | Duración planeada (segundos) | 5.0 |
| `slew_time_seconds` | float | Tiempo de movimiento | 8.45 |
| `settling_time_seconds` | float | Tiempo de estabilización | 2.0 |

### 22-24: Coordenadas Horizontales (Opcional)

| Campo | Tipo | Descripción | Ejemplo |
|-------|------|-------------|---------|
| `azimuth_degrees` | float | Azimut en grados | 235.45 |
| `altitude_degrees` | float | Altitud en grados | 45.67 |
| `airmass` | float | Masa de aire | 1.42 |

### 25-26: Notas y Comentarios

| Campo | Tipo | Descripción | Ejemplo |
|-------|------|-------------|---------|
| `notes` | string | Anotaciones libres | Strong source detected |
| `weather_conditions` | string | Condiciones meteorológicas | Clear sky, 15°C |

---

## Parámetros del Comando

### Requeridos

```bash
--session "nombre_sesion"        # Nombre para el CSV
--center-ra 19.99               # Centro RA (horas)
--center-dec 40.73              # Centro DEC (grados)
--points 100                    # Número de puntos (será ajustado para uniformidad)
```

### Opcionales - Conexión INDI

```bash
--host localhost                # Servidor INDI (default: localhost)
--port 7624                     # Puerto INDI (default: 7624)
--device "EQMod Mount"          # Nombre dispositivo (default: auto)
```

### Opcionales - Área de Grilla

```bash
--width 1.0                     # Ancho en grados (default: 1.0)
--height 1.0                    # Alto en grados (default: 1.0)
```

### Opcionales - Captura

```bash
--capture-time 5.0              # Duración captura (default: 5.0s)
--settling-time 2.0             # Tiempo estabilización (default: 2.0s)
--data-dir ./data               # Directorio base (default: ./data)
```

---

## Ejemplos de Uso Real

### 1. Survey de Cygnus A (100 puntos)

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

**Resultado:**
- 10 x 10 = 100 puntos (pixels cuadrados perfectos)
- ~2 horas de observación
- CSV: `cygnus_a_1420mhz_night1.csv`

### 2. Mapa de M51 (200 puntos en área rectangular)

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

**Resultado:**
- ~12 x 16 = 192 puntos (ajustado de 200 para pixels cuadrados)
- Ratio área respetado
- ~2 horas de observación

### 3. Prueba Rápida de Calibración (25 puntos)

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

**Resultado:**
- 5 x 5 = 25 puntos (perfecto!)
- ~2 minutos de prueba

### 4. Survey Grande (500 puntos)

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

**Resultado:**
- ~23 x 22 = 506 puntos (ajustado de 500)
- Ratio 10:5 = 2:1 respetado
- ~17 horas de observación (multi-noche)

---

## Procesamiento del CSV

### Ejemplo 1: Leer con Python

```python
import pandas as pd

# Leer CSV
df = pd.read_csv('./data/20250115_143022/cygnus_a_survey.csv')

# Filtrar capturas exitosas
successful = df[df['capture_status'] == 'success']

print(f"Capturas exitosas: {len(successful)}/{len(df)}")
print(f"Error promedio RA: {successful['position_error_ra_arcmin'].mean():.2f}'")
print(f"Error promedio DEC: {successful['position_error_dec_arcmin'].mean():.2f}'")
```

### Ejemplo 2: Estadísticas Básicas

```python
import pandas as pd

df = pd.read_csv('./data/20250115_143022/cygnus_a_survey.csv')

# Resumen de tiempos
print("Tiempos de captura:")
print(df['duration_seconds'].describe())

# Resumen de errores de posición
print("\nErrores de posicionamiento (arcmin):")
print(df['position_error_ra_arcmin'].describe())
print(df['position_error_dec_arcmin'].describe())
```

### Ejemplo 3: Visualizar Grilla

```python
import pandas as pd
import matplotlib.pyplot as plt

df = pd.read_csv('./data/20250115_143022/cygnus_a_survey.csv')

# Gráfico de posiciones
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

## Integración con Receptor de Radio

### Paso 1: Localizar el Punto de Captura

En `grid_generator.py`, línea ~347:

```python
# HERE: Integrate your radio astronomy data capture
# For now, simulate with sleep
await asyncio.sleep(capture_duration)
```

### Paso 2: Reemplazar con tu Código

```python
# Ejemplo para SDR
from your_radio_module import capture_spectrum

# Start capture
start_time = datetime.now(timezone.utc)
capture_data['start_time_utc'] = start_time.isoformat()

# Capturar datos reales
data_path = self.data_subdir / capture_data['data_filename']
await capture_spectrum(
    output_file=data_path,
    duration=capture_duration,
    center_freq=1420.405,  # MHz (línea H-I)
    bandwidth=2.0,          # MHz
    gain=40                 # dB
)

end_time = datetime.now(timezone.utc)
```

---

## Notas Importantes

### 1. Estructura de Directorios

Cada sesión crea un directorio con timestamp único:
```
./data/20250115_143022/    ← No se sobrescribe nunca
```

### 2. Nombres de Sesión

Usa nombres descriptivos:
- ✅ `cygnus_a_1420mhz_jan15`
- ✅ `m51_survey_night1`
- ✅ `galactic_plane_5ghz`
- ❌ `test` (no descriptivo)
- ❌ `scan1` (no descriptivo)

### 3. Cálculo de Tiempo

Tiempo estimado por punto:
```
tiempo_punto = capture_time + settling_time + slew_time (aprox. 5-15s)
tiempo_total = num_puntos × tiempo_punto
```

### 4. Errores de Posición

Los errores se calculan en:
- **RA**: Minutos de tiempo (convertidos de horas)
- **DEC**: Arcominutos (convertidos de grados)

### 5. CSV siempre tiene 26 columnas

Campos opcionales quedan vacíos si no están disponibles:
- `azimuth_degrees`, `altitude_degrees`, `airmass`
- `notes`, `weather_conditions`

**OPTIMIZACIÓN:** No se leen las coordenadas reales de la montura después del GOTO para ahorrar ~0.5-1s por punto. Esto acelera significativamente los escaneos largos.

---

## Troubleshooting

### Problema: CSV no se crea

**Solución:**
```bash
# Verificar permisos
ls -ld ./data
mkdir -p ./data
chmod 755 ./data
```

### Problema: Faltan columnas en CSV

**Verificación:**
```bash
# Contar columnas
head -1 ./data/*/session.csv | tr ',' '\n' | wc -l
# Debe retornar: 30
```

### Problema: Capturas fallan

**Check log:**
- Mensaje: `GOTO command failed` → Verificar montura conectada
- CSV tendrá `capture_status = 'failed'`
- Revisar `error_message` para diagnóstico

---

## Resumen de Ventajas

✅ **26 campos optimizados** para análisis eficiente  
✅ **CSV estándar** fácil de procesar (pandas, Excel, etc.)  
✅ **Estructura organizada** por timestamp  
✅ **Enumeración secuencial** de capturas  
✅ **Metadatos completos** por cada punto  
✅ **Tiempos precisos** (UTC, ISO 8601)  
✅ **Trazabilidad completa** de la sesión  
✅ **Optimizado para velocidad** (no lee posición real de montura)  

**¡Listo para radioastronomía profesional!** 🛰️📡
