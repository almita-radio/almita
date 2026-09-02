# Resumen — ALMITA 50-Point Real Field E2E (RFI-REF-50PT-E2E-01)

Documento de síntesis, tracked en git. No reprocesa datos; el detalle físico
completo (HDF5, logs, timing CSV, snapshots de runtime, muestras de recursos)
vive fuera de git en `data/rfi_ref_50pt_field_e2e/` (gitignored) y se
preserva intacto en disco.

Base de partida: commit `5b73ee1` (DC-spike suppression para RFI_REF +
Antenna A interpolated preview).

## Grid y sesión

- Grid solicitado: ~50 puntos, geometría segura/moderada, sin patología
  cerca del polo.
- Grid realmente generado por `grid_generator.py`: **7×7 = 49 puntos**
  (altitud 62.6°–89.9°). No se relajaron los límites de seguridad para
  forzar exactamente 50 — el generador produjo la geometría segura más
  cercana disponible.
- Sesión final: `session_id=20260902_041554`, nombre `RFI-REF-50PT-E2E-01`.
- Mount: LX200 OnStep real, ruta de producción, sin simulador, sin SYNC,
  sin cambios de alineación.

## Intentos

1. **Intento 1** — 0 GOTOs reales; el gate de seguridad de OnStep rechazó
   todos los targets. Causa raíz: reloj interno de OnStep ~1h35min
   atrasado respecto al UTC real → horizonte mal calculado. Diagnosticado
   solo por lectura de propiedades INDI, sin ninguna escritura de
   SYNC/alineación/hora por parte del agente.
2. **Intento 2** — mismo rechazo, confirmado de nuevo antes de escalar al
   operador.
3. El operador corrigió hora y home de OnStep externamente.
4. **Intento 3** — 25/49 puntos reales completados con éxito; detenido
   intencionalmente con SIGINT limpio (no SIGTERM) para aplicar el fix de
   DC-spike y reiniciar la sesión desde el punto 1.
5. **Intento 4 (final)** — **49/49 puntos reales completados con éxito.**

## Resultado final (intento 4)

- **Mount**: 49/49 GOTOs reales exitosos, 0 estados implausibles.
- **MAIN** (RTL-SDR V4, `rtl_tcp.service`, 1420405000 Hz, 2.4 MS/s, 40.2 dB,
  Bias-T ON): nunca reiniciado ni modificado durante toda la sesión.
  49/49 HDF5 válidos, 0 stalls, 0 desconexiones, 0 archivos `.part`
  huérfanos.
- **RFI_REF** (RTL-SDR V3, puerto 1235, no bloqueante): activo durante toda
  la sesión, detenido limpiamente al final, puerto cerrado. Supresión de
  DC-spike (artefacto de hardware zero-IF del tuner R820T/R828D) validada
  en vivo: pico bajó de -64.7 dBFS/+4.7 kHz (antes del fix) a
  -86.1 dBFS/-717 kHz, ~3 dB sobre el piso de ruido (después del fix).
- **Quicklook**: 49/49 puntos procesados. Antenna A Native Grid (sin
  interpolación, contrato intacto) + Antenna A Interpolated Preview
  (producto opcional, claramente etiquetado como no-científico, en módulo
  separado). Antenna B RFI Spectrum / RFI Waterfall / RFI Occupancy Map
  poblados, correlacionados con las ventanas reales de captura de MAIN.
- **USB/kernel**: 0 eventos en toda la sesión.
- **Recursos**: carga/RAM/temperaturas dentro de rango normal; 1/314
  muestras de temperatura SDR mostró un valor espurio (298.9°C), atribuido
  a un glitch transitorio de CRC del sensor DS18B20 1-Wire (muestras
  adyacentes ~33°C) — reportado, no descartado ni ocultado.
- **Procesos ajenos matados**: 0.

## Veredicto

**`ALMITA 50-POINT REAL FIELD E2E = PASS`**

Puntos: 0 fallidos, 0 diferidos. La desviación de 49 vs. ~50 puntos fue una
decisión de geometría segura del generador de grid, no una falla de
seguridad ni una relajación de límites.

## Evidencia completa (no versionada)

`data/rfi_ref_50pt_field_e2e/20260902T034616Z/` — incluye `final_report.md`,
`summary.json`, `run.log`, timing CSV por punto, estadísticas RFI por
punto, snapshots finales de runtime, excerpts de log de kernel, muestras de
recursos y resumen de validación de HDF5.
