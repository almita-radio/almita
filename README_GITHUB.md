# 🔭 Control de Telescopio INDI para Radioastronomía

Sistema de control de telescopios vía protocolo INDI, diseñado para aplicaciones de radioastronomía en Raspberry Pi 5 con Python 3.13+.

## ✨ Características

- ✅ **GOTO**: Movimiento preciso del telescopio a coordenadas específicas
- ✅ **SYNC**: Corrección de apuntado sin movimiento físico
- ✅ **STATUS**: Lectura de coordenadas actuales (RA/DEC)
- ✅ **TRACKING**: Control de seguimiento sideral (on/off)
- 🔧 **XML/TCP directo**: Comunicación nativa con servidor INDI
- 📊 **Formato detallado**: Coordenadas en múltiples formatos (horas, grados, HMS/DMS)
- 📖 **Explicaciones educativas**: Cada comando muestra qué hace el protocolo INDI

## 🚀 Instalación Rápida

### En Raspberry Pi 5

```bash
cd "Testing INDIpy"
./setup_rpi.sh
```

### Requisitos previos

- KStars/Ekos instalado y corriendo
- Servidor INDI activo (puerto 7624)
- Python 3.13.5 o superior
- Dispositivo telescopio conectado (o simulador)

## 📖 Uso

### Leer coordenadas actuales

```bash
python indi_telescope_control.py --status
```

### GOTO a coordenadas específicas

```bash
# Vega (ejemplo)
python indi_telescope_control.py --goto 18.615 38.783

# Formato: RA en horas (0-24), DEC en grados (-90 a +90)
```

### SYNC - Corregir posición sin mover

```bash
# Después de plate solving o medición precisa
python indi_telescope_control.py --sync 18.616 38.784
```

### Control de tracking

```bash
# Activar seguimiento sideral
python indi_telescope_control.py --track_on

# Desactivar tracking
python indi_telescope_control.py --track_off
```

### Especificar dispositivo

```bash
# Con simulador (por defecto)
python indi_telescope_control.py --status --device "Telescope Simulator"

# Con montura real
python indi_telescope_control.py --status --device "EQMod Mount"
```

### Modo verbose (debugging)

```bash
python indi_telescope_control.py --goto 18.615 38.783 --verbose
```

## 🎓 Documentación

- **[RESUMEN_EJECUTIVO.md](RESUMEN_EJECUTIVO.md)**: Explicación detallada del comando SYNC
- **[GUIA_VISUAL.md](GUIA_VISUAL.md)**: Diagramas y flujos de trabajo
- **[INDICE.md](INDICE.md)**: Índice completo de documentación

## 🔧 Arquitectura Técnica

### Protocolo INDI

El script se comunica directamente con el servidor INDI mediante XML sobre TCP:

```xml
<!-- Ejemplo de comando GOTO -->
<newNumberVector device="Telescope Simulator" name="EQUATORIAL_EOD_COORD">
  <oneNumber name="RA">18.615</oneNumber>
  <oneNumber name="DEC">38.783</oneNumber>
</newNumberVector>
```

### Comandos implementados

| Comando | Propiedad INDI | Efecto |
|---------|----------------|--------|
| `--status` | `EQUATORIAL_EOD_COORD` | Lee coordenadas actuales |
| `--goto` | `ON_COORD_SET=SLEW` + coord | Mueve el telescopio |
| `--sync` | `ON_COORD_SET=SYNC` + coord | Actualiza modelo interno |
| `--track_on/off` | `TELESCOPE_TRACK_STATE` | Tracking sideral |

## 📊 Casos de Uso - Radioastronomía

### 1. Observación de fuente puntual

```bash
# 1. Apuntar a Cygnus A
python indi_telescope_control.py --goto 19.991 40.734

# 2. Verificar apuntado
python indi_telescope_control.py --status

# 3. Si hay offset conocido, corregir con SYNC
python indi_telescope_control.py --sync 19.992 40.735

# 4. Activar tracking
python indi_telescope_control.py --track_on
```

### 2. Barrido (drift scan)

```bash
# 1. Apuntar a posición inicial
python indi_telescope_control.py --goto 12.000 30.000

# 2. Desactivar tracking (dejar que el cielo derive)
python indi_telescope_control.py --track_off
```

## 🐛 Troubleshooting

### El driver INDI crashea

**Síntoma**: Log muestra `"getProperties missing version"`

**Solución**: Todos los comandos `<getProperties/>` **requieren** `version="1.7"`. El script ya lo incluye.

### No se detecta el dispositivo

```bash
# Listar dispositivos disponibles
python indi_telescope_control.py --status

# El error mostrará los dispositivos detectados
```

### Coordenadas no se actualizan después de SYNC

Esto es normal en simuladores. El SYNC actualiza el modelo interno pero puede no reflejarse inmediatamente en la UI. Hacer un `--status` después de 2-3 segundos.

## 📜 Historial de Versiones

### v1.0.0 (2026-02-13)
- ✅ Implementación inicial
- ✅ Comandos GOTO, SYNC, STATUS, TRACKING
- ✅ Soporte para Python 3.13+
- ✅ Documentación completa
- ✅ Fix crítico: `version="1.7"` en todos los getProperties

## 🤝 Contribuir

Este proyecto es para uso en radioastronomía. Si tienes mejoras o encuentras bugs:

1. Haz fork del repositorio
2. Crea una rama (`git checkout -b feature/mejora`)
3. Commit tus cambios (`git commit -am 'Agrega nueva funcionalidad'`)
4. Push a la rama (`git push origin feature/mejora`)
5. Abre un Pull Request

## 📄 Licencia

Este proyecto es de código abierto para uso educativo y científico en radioastronomía.

## 🙏 Agradecimientos

- Proyecto INDI (https://www.indilib.org/)
- KStars/Ekos
- Comunidad de radioastronomía amateur

## 📞 Contacto

Para preguntas sobre radioastronomía y uso del sistema, consulta la documentación o abre un issue.

---

**⚠️ ADVERTENCIA**: Este software controla hardware de telescopio. Úsalo con precaución:
- Verifica límites de movimiento antes de usar GOTO
- No uses en telescopios sin supervisión
- Prueba primero con simuladores
- Asegúrate de tener los permisos de hardware necesarios
