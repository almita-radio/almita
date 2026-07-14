# Script de Prueba INDIpy para Radioastronomía

## Descripción

Script completo para evaluar la librería **INDIpy** como herramienta de control de monturas telescópicas en aplicaciones de radioastronomía. Implementa las operaciones fundamentales de control de monturas INDI.

## Requisitos del Sistema

- **Hardware**: Raspberry Pi 5 (o compatible)
- **Python**: 3.13.5 (compatible con 3.8+)
- **Sistema Operativo**: Linux (Raspbian/Raspberry Pi OS)

## Instalación

### 1. Instalar INDIpy

```bash
pip install indipy
```

### 2. Instalar servidor INDI (si no lo tienes)

```bash
sudo apt-get update
sudo apt-get install indi-bin indi-full
```

### 3. Hacer el script ejecutable (opcional)

```bash
chmod +x Testing_INDIpy.py
```

## Configuración del Servidor INDI

Antes de ejecutar el script, necesitas tener un servidor INDI corriendo con una montura conectada.

### Opción 1: Usar Telescope Simulator (para pruebas)

```bash
# Iniciar servidor INDI con simulador
indiserver -v indi_simulator_telescope
```

### Opción 2: Usar montura real (ejemplo: EQMod)

```bash
# Para monturas EQ (Skywatcher, etc.)
indiserver -v indi_eqmod_telescope
```

### Opción 3: Usar KStars/Ekos

Si usas KStars/Ekos, simplemente inicia Ekos y conecta tu montura. El servidor INDI estará disponible automáticamente.

## Uso del Script

### Modo básico (autodetección)

```bash
python Testing_INDIpy.py
```

Esto conectará a `localhost:7624` y usará la primera montura detectada.

### Especificar servidor remoto

```bash
python Testing_INDIpy.py --host 192.168.1.100 --port 7624
```

### Especificar dispositivo específico

```bash
python Testing_INDIpy.py --device "Telescope Simulator"
```

### Ejemplo completo

```bash
python Testing_INDIpy.py --host localhost --port 7624 --device "EQMod Mount"
```

## Operaciones Implementadas

El script ejecuta en secuencia las siguientes operaciones:

### 1. **GOTO a Coordenadas**
- Mueve la montura a coordenadas específicas (RA/DEC)
- Ejemplo: Vega (RA=18.6h, DEC=+38.78°)
- Espera confirmación de finalización del movimiento

### 2. **Activar/Desactivar Tracking**
- Controla el seguimiento sideral de la montura
- Prueba desactivación y reactivación
- Verifica estados correctamente

### 3. **Detectar Estado de Tracking**
- Lee el estado actual del seguimiento
- Valida si está activo o inactivo
- Usa múltiples métodos de detección (compatibilidad)

### 4. **SYNC de Coordenadas (Corrección en Caliente)**
- **CONCEPTO**: Actualiza las coordenadas internas de la montura SIN moverla físicamente
- **USO**: Después de plate solving para corregir errores de apuntado
- **EJEMPLO**: La montura cree estar en RA=18.6h, pero plate solving muestra RA=18.61h
- **RESULTADO**: La montura actualiza su posición interna a la correcta

## Características Técnicas

### Operaciones Asíncronas
INDIpy usa **asyncio** para operaciones no bloqueantes:
- Mayor fluidez en la ejecución
- Permite control simultáneo de múltiples dispositivos
- Mejor para aplicaciones complejas de radioastronomía

### Logging con Timestamps
Todos los mensajes incluyen fecha/hora con milisegundos:
```
[2025-01-28 15:30:45.123] [INFO] Conectando a servidor INDI...
[2025-01-28 15:30:45.456] [INFO] Dispositivo seleccionado: Telescope Simulator
[2025-01-28 15:30:47.789] [INFO] GOTO completado exitosamente
```

### Manejo Robusto de Sesiones
- Conexión/desconexión limpia
- Manejo de errores y timeouts
- Validación de estados de dispositivos
- Compatibilidad con múltiples tipos de monturas

## Salida Esperada

```
[2025-01-28 15:30:45.123] [INFO] Inicializando TelescopeController
[2025-01-28 15:30:45.124] [INFO] Servidor INDI: localhost:7624
[2025-01-28 15:30:45.125] [INFO] ================================================================================
[2025-01-28 15:30:45.126] [INFO] INICIANDO PRUEBAS DE INDIpy PARA RADIOASTRONOMÍA
[2025-01-28 15:30:45.127] [INFO] ================================================================================
[2025-01-28 15:30:45.128] [INFO] 
[2025-01-28 15:30:45.129] [INFO] NOTA TÉCNICA: INDIpy usa operaciones ASÍNCRONAS (asyncio)
[2025-01-28 15:30:45.130] [INFO] Los comandos se envían de forma no bloqueante y se esperan respuestas
[2025-01-28 15:30:45.131] [INFO] Conectando a servidor INDI en localhost:7624...
...
[2025-01-28 15:31:25.456] [INFO] ✓ Prueba GOTO exitosa
[2025-01-28 15:31:27.789] [INFO] ✓ Desactivación de tracking exitosa
[2025-01-28 15:31:29.012] [INFO] ✓ Detección de tracking exitosa: INACTIVO
[2025-01-28 15:31:31.234] [INFO] ✓ Activación de tracking exitosa
[2025-01-28 15:31:33.456] [INFO] ✓ Sincronización SYNC exitosa
...
[2025-01-28 15:31:35.678] [INFO] ================================================================================
[2025-01-28 15:31:35.679] [INFO] RESUMEN DE EVALUACIÓN DE INDIpy
[2025-01-28 15:31:35.680] [INFO] ================================================================================
```

## Resumen del Punto 4 (SYNC)

**Tu pregunta era sobre "corregir datos en caliente" como hace Ekos después del plate solving:**

### ¿Qué es SYNC?
- La montura siempre "cree" saber dónde está apuntando (basado en encoders/pasos del motor)
- Pero factores como flexión, errores de alineación, etc. causan diferencias con la realidad
- **Plate solving** toma una foto y determina dónde REALMENTE está apuntando la montura
- **SYNC** actualiza las coordenadas internas de la montura a la posición real

### Ejemplo Práctico:
1. La montura dice: "Estoy en RA=10h 00m 00s, DEC=45° 00' 00""
2. Tomas una foto y haces plate solving
3. El plate solving dice: "Estás en RA=10h 02m 15s, DEC=45° 03' 30""
4. Haces SYNC con las coordenadas reales
5. Ahora la montura sabe que está en RA=10h 02m 15s (sin haberse movido)
6. Los próximos GOTOs serán más precisos porque partimos de una referencia correcta

### Código de Ejemplo en el Script:
```python
# La montura cree estar en RA=18.6h, DEC=38.78°
# Pero plate solving dice que realmente está en RA=18.61h, DEC=38.79°
await controller.sync_coordinates(18.61, 38.79)
# Ahora la montura conoce su posición real sin moverse
```

## Evaluación de INDIpy para Radioastronomía

### ✅ VENTAJAS:
1. **100% Python nativo** - Sin dependencias de código C/C++
2. **Operaciones asíncronas** - Mejor para múltiples dispositivos simultáneos
3. **Instalación simple** - `pip install indipy` (no compilar)
4. **API limpia y pythonic** - Fácil de integrar en scripts existentes
5. **Soporte completo INDI** - Todas las operaciones estándar funcionan

### ⚠️ CONSIDERACIONES:
1. **Curva de aprendizaje** - Requiere conocer asyncio/await
2. **Documentación limitada** - Menos ejemplos que pyindi-client
3. **Comunidad pequeña** - Menos recursos y respuestas en foros

### 🎯 RECOMENDACIÓN:
**INDIpy es EXCELENTE para tu caso de uso en radioastronomía** porque:
- Scripts Python puros (sin compilar en cada actualización)
- Operaciones no bloqueantes (ideal para control de múltiples dispositivos: montura + receptor + rotador, etc.)
- Fácil integración con frameworks async modernos
- Perfecto para Raspberry Pi (bajo overhead)

## Solución de Problemas

### Error: "INDIpy no está instalado"
```bash
pip install indipy
```

### Error: "No se detectaron dispositivos"
- Verifica que el servidor INDI esté corriendo: `ps aux | grep indiserver`
- Verifica el puerto: `netstat -tulpn | grep 7624`
- Revisa los logs del servidor INDI

### Error: "Timeout esperando dispositivos"
- Aumenta el timeout en el código (default: 10s)
- Verifica que la montura esté conectada y encendida
- Revisa permisos del puerto serie (para monturas USB): `sudo usermod -a -G dialout $USER`

### La montura no se mueve en GOTO
- Verifica que la montura esté alineada (polar alignment)
- Verifica que el tracking esté activado
- Revisa límites de movimiento en la configuración INDI

## Próximos Pasos

### Para Producción:
1. **Implementar modelo de errores de apuntado** (TPoint/polynomial)
2. **Agregar logging a archivo** (no solo consola)
3. **Implementar reintentos automáticos** en caso de fallos
4. **Agregar validación de coordenadas** (límites físicos de la montura)
5. **Integrar con receptor de radio** para observaciones coordinadas

### Scripts Adicionales para Radioastronomía:
1. **Barrido de cielo** (sky survey) con patrón de grilla
2. **Seguimiento de fuente** con correcciones periódicas
3. **Drift scan** (la montura permanece fija, el cielo deriva)
4. **Interferometría** (control coordinado de múltiples telescopios)

## Contacto y Contribuciones

Este script es parte de un proyecto de evaluación de herramientas para radioastronomía. Modificar según necesidades específicas de tu setup.

## Licencia

Código de ejemplo educativo - Uso libre para proyectos de radioastronomía.

---

**Desarrollado para**: Raspberry Pi 5 con Python 3.13.5  
**Fecha**: Enero 2025  
**Objetivo**: Evaluar INDIpy como herramienta nativa Python para control de monturas en aplicaciones de radioastronomía
