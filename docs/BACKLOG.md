# ALMITA backlog

Pedidos del operador y su estado real. Revisado contra el código, los commits y `data/` el 2026-10-08.

Marcas:

| marca | significado |
|---|---|
| **[H]** | Probado en el instrumento real. |
| **[D]** | Corrido sobre datos reales, sin hardware. |
| **[C]** | Solo tests. Nada está validado en hardware salvo que diga [H]. |

Contexto general: [PROJECT_BRIEF.md](PROJECT_BRIEF.md).

## Hecho y activo (`web-polish-v1`)

| id | pedido | commits | verificación |
|---|---|---|---|
| BL-001 | CALIBRATE: mapa Alt/Az de las zonas HI del wizard | 156bfbd, 8a66860 | [C]; plan real WIZARD-20261002 |
| BL-002 | OBSERVE: selector de perfil de calibración (explorador del servidor) | 4085ed8, 915f8fa | [C]; perfiles reales por HTTP |
| BL-003 | Indicador de estado centrado | 03d263a | [C] tests de navegador |
| BL-004 | Retiro del puerto 8090 | 704b168 | [C] |
| BL-005 | SCIENCE: A, B y C en una sola proyección, cobertura completa, ≤3 GB | cf450de, 4eec7f4, 9c5c8f2 | [D] 400/400, GOOD, pico de 1.03 GB |
| BL-006 | SCIENCE: exportes legibles y diagnósticos uniformes | b40e126 | [D] |
| BL-007 | Wizard: STOP, ABORT, fallas y pasos interrumpidos | adc2cac | [C]; ABORT [H] 2026-10-07; STOP CURRENT STEP sin probar en hardware |
| BL-008 | Frecuencia: configuración central + ACK de rtl_tcp | aae14d9 | [C] + [H] en OBSERVE y el wizard; bench `sdr --yes` sin correr |
| BL-009 | Tests sin dependencia de `data/` | fdc72e9, 8473f0c | [C] |
| BL-010 | Git: `data/` ignorado, fixtures, runbook | ef32d62, dbafb43 | — |
| BL-011 | `mount_control.py` no hacía await de `connect()` | 5ee573a | [C] |
| BL-012 | Incidente del wizard con 50 Ω (handle USB viejo, ACK anulado por fallas de librtlsdr, timeouts) | 8dfb3ab | [H] wizard DONE después del fix (WIZARD-20261007-010706) |
| BL-013 | Trabajos listados por hora de inicio; falla del wizard mostrada una sola vez | 547c5b0 | [C] |
| BL-014 | Cámara de montura rotada 180° y persistente | c9d6316 | [C]; configurada en vivo |
| BL-015 | REDUCE: perfil elegido en el explorador; perfiles con igual nombre distinguibles | 4aae46f | [C] + [D] |
| BL-016 | OBSERVE: una sesión terminada sola ya no muestra RUNNING | b1f9331 | [C]; caso real del 2026-10-07 |
| BL-017 | Gain pilot: las capturas reales daban 404 | f4cd4c5 | [H] pilot READY el 2026-10-08 |
| BL-018 | Gain pilot: RE-PLAN a la ganancia aprobada, sin bucle | ae4242b | [H] sesión de 25 puntos a 42.1 dB |
| BL-019 | MONITOR stack 3D: sin exagerar los picos | d2efeac | [D] render con datos reales |
| BL-020 | REDUCE y SCIENCE: avance en vivo en el log | 67df4a6 | [D] sesión de 100 puntos |
| BL-021 | Manual cortapalos (PDF) | 71e70d2 y siguientes | revisado contra el código; recorrido completo pendiente |
| BL-022 | ALIGN: SYNC (Sol y HI) con control de confianza y offset máximo; offset siempre visible | 4012330 | [C] montura falsa; **sin probar en la montura real** |
| BL-023 | Gain pilot avisa antes de aprobar una ganancia sin perfil; etiqueta topocentric corregida | 8f7ab05 | [C] |
| BL-024 | Brief del proyecto + sincronización de Git | fd8a962 | — |
| BL-025 | RFI REF: Bias-T encendido por defecto (la antena B tiene LNA, confirmado por el operador); el formulario sigue la configuración del servidor y se conserva al RE-PLAN | este commit | [C]; sesiones del 2026-10-08 ya lo pedían ON; alimentación del LNA sin medir |

Activación:
- `console/` se sirve en vivo.
- Los cambios de Python que importa el servidor necesitan reiniciar `almita-observe-api`, y solo sin trabajos
  activos (revisarlo en un paso aparte).
- Los scripts que lanza la web se cargan en cada trabajo.

## Abierto — necesita al operador

Comprobaciones físicas (agrupables en una sesión):
- **SYNC de ALIGN** de día: SOLAR sin SYNC, después con SYNC, y luego un ALIGN de confirmación (BL-022).
- **STOP OBSERVATION** real con una grilla corta: debe terminar en `ABORTED`, con los `.h5` completos.
- **STOP CURRENT STEP** del wizard durante una captura real (BL-007).
- **Fallas provocadas:** desconectar el USB de MAIN y apagar INDI, y ver el BLOCK y la recuperación.
- **Bench `sdr --yes`** en MAIN (BL-008) y la CLI de `mount_control.py` (BL-011).
- **Antena B — alimentación del LNA:** el operador confirmó el 2026-10-08 que la antena B tiene un LNA alimentado
  por el Bias-T de RFI REF (pregunta resuelta). Falta comprobar en hardware que el LNA recibe alimentación con
  BIAS-T marcado (ruido de RFI REF ON vs OFF, o tensión en el conector).
- **Ekos:** dejar escrito el procedimiento real de alineación, hora y sitio para el manual.
- **Recorrido completo** siguiendo el manual cortapalos.
- **Medir el haz** (FWHM): hoy es provisional (20° en la configuración, 14° como placeholder en `alignment.py`).
  Afecta la separación recomendada, el ALIGN y SCIENCE.

Decisiones:
- **Unir `web-polish-v1` con `main`.**
  - `main` tiene 19 commits de documentación y sitio que esta rama no tiene: licencias, `docs/hardware/` y sitio.
  - Solo chocan en `README.md`.
  - Propuesta: un PR de `web-polish-v1` → `main`, resolviendo el README.
- **REDUCE topocentric sin metadatos:** exige descongelar `reduce_engine` (REDUCE V1). Hoy solo se corrigió la
  etiqueta.
- **BL-002 opcional:** subir un perfil desde el disco del navegador. Hay que decidir dónde se guarda y quién puede
  escribir.
- **Modelo de horizonte:** necesita datos del sitio.
- **Ramas de respaldo** (`backup-antes-de-limpiar-github-20260810`, `respaldo-local-18-03`): no se suben, porque
  desharían la limpieza.
- **Respaldo `/home/stellarmate/almita_preserve_20261002/`:** se borra solo cuando el operador lo diga.
- **Archivos sin versionar** de investigación de agosto (`mount_slew_*`, `indoor_coupling_*`,
  `antenna_coupling_test.py`, `rf_chain_isolation_02*`, `sdr_config_fix_validation.py`,
  `analyze_mount_slew_data03_physical.py`), salidas `mount_benchmark_*/` y `mount_slew_model_output/`, respaldos
  `*_ultima_revision.tar.gz` y el archivo vacío `=.9`: ¿versionar, mover o borrar?

## Abierto — trabajo autónomo (sin hardware)

- Pie de página: dice «HTTP (LAN, no TLS, no authentication)», pero el servidor sí exige Basic Auth
  (`almita_web_system.version_info`).
- Sección ALIGN de PIPELINE: no tiene la opción de SYNC. Hoy siempre usa `--no-sync`.
- Política vieja de SYNC: `alignment_engine/sync_flow.py` y `targets/hi_reference.py` (motor de la sección de
  simulación) mantienen la política anterior, que bloqueaba el SYNC en HI. Hay que alinear la documentación o el
  código con BL-022.
- RFI REF: asociar las capturas de la antena B a cada punto en REDUCE. La interfaz existe
  (`docs/REDUCE_RFI_REF_INTERFACE.md`) y hoy informa UNAVAILABLE.
- Pendientes de FIELD_RUNBOOK §14:
  - identidad canónica de la sesión;
  - lock del proceso INDI;
  - `gain='auto'` como default en `sdr_capture.configure`;
  - lectores DS18B20 duplicados;
  - `README.md` y `docs/history/FLUJO_COMPLETO.md` desactualizados;
  - Dec negativa en la CLI de `mount_control.py`.
- `test_field_console_left_open_for_30_minutes...` es sensible al tiempo cuando el Pi está cargado.
