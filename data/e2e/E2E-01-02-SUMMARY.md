# Resumen humano — AFC-E2E-01 y AFC-E2E-02

Documento de síntesis. No reprocesa datos; consolida en un solo lugar lo que
hoy solo es reconstruible leyendo `mosaic.csv`, `alignment_result.json`,
`quicklook_live_status.json` y `publication_manifest.json` en cada corrida.
Base de partida: commit `86dba5bfd22e2f18a5e6ebf9621d0cac0db4ba7b`
(tag `afc-00-go-20260827`, 368/368 tests PASS).

Contexto físico: ambas corridas fueron **indoor, dentro de un departamento**.
No se espera ni se reclama detección HI galáctica científicamente válida en
ninguna de las dos. Ningún pico/RFI observado debe interpretarse como
detección astronómica.

## AFC-E2E-01 — `data/e2e/AFC-E2E-01-20260827T233127Z/`

- Contexto: primera corrida E2E posterior al GO de AFC-00, indoor.
- Preflight (Fase 0/1): `phase0_phase1_preflight.md` — rtl_tcp activo, INDI
  activo, gain fijo 40.2 dB / AGC off, Bias-T habilitado, release pinneado a
  `86dba5b` / `afc-00-go-20260827`.
- Blockers descubiertos (cada uno documentado en su propio `.md`, sin ninguna
  acción física de riesgo ejecutada):
  - **Fase 2** — `e2e_blocker_phase2_mount_status.md`: `mount_control.py
    --status` falló (`RuntimeWarning: coroutine 'INDITelescopeControl.connect'
    was never awaited` → `RuntimeError: No conectado`, `mount_control.py:119`).
    **No corregido** en este ciclo de commits (ver tabla de findings).
  - **Fase 3 (SDR)** — `e2e_blocker_phase3_sdr_entrypoint.md`: `sdr_capture.py
    --help` no tenía parser real y ejecutaba una demo con gain auto, violando
    el contrato de 40.2 dB manual. **Corregido** en Commit 1
    (`d922a88` — "fix: harden SDR baseline CLI contract").
  - **Fase 3 (calibración)** — `e2e_blocker_phase3_calibration_cli.md`:
    `build_calibration_foundation_v1.py` exigía `--antenna` incluso cuando
    solo correspondía construir el baseline 50Ω. **Corregido** en Commit 2
    (`cb6d3bc` — "fix: allow reference-only calibration foundation").
  - **Fase 4** — `e2e_blocker_phase4_indoor_alignment.md`: `alignment.py
    --reference auto --dry-run --no-sync` cayó a referencia HI (RA 15h,
    DEC −68.05°), PASS solo en dry-run; bloqueó las fases 5-13 de ese intento.
- Tras destrabar los blockers de Fase 3, el grid 3×3 (9 puntos) se ejecutó:
  **8/9 puntos capturados** (`mosaic.csv`, `session_id=20260828_005847`).
  El punto 7 quedó `capture_status=planned`, `visibility_deferred=true` — sus
  vecinos (puntos 6 y 8) sí se ejecutaron con `altitude_deg_at_goto` de
  33.36° y 31.43° respectivamente (`min_altitude_deg=30.0`). Causa: el punto
  era válido al generarse el grid, pero cayó bajo el límite de 30° para
  cuando le tocó su turno de ejecución (planning/visibility temporal —
  ver finding "visibility TOCTOU"). **No es un fallo de hardware ni de
  seguridad**; el sistema difirió el punto correctamente en vez de forzar
  un GOTO bajo el límite.
- Alignment: **49/49 muestras HDF5 capturadas** (`alignment_sample_001.h5`
  … `_049.h5`), `sync_applied=false`, `sync_executed=false`,
  `result_status="NO DEFENDIBLE DIFFERENTIAL HI STRUCTURE"` — resultado nulo
  correcto para un entorno indoor sin fuente conocida.
- Datos preservados íntegramente bajo `data/e2e/AFC-E2E-01-20260827T233127Z/`
  (grid, mosaic, observer_config, baseline 50Ω, alignment dry-run y live).
  Hardware operado siempre de forma segura: ningún blocker fue sorteado con
  una acción física; cada fase se detuvo antes de comprometer el hardware.

## AFC-E2E-02 — `data/e2e/AFC-E2E-02-20260828T010600Z/`

Misma campaña E2E, ejecutada como corrida separada tras E2E-01.

- **Fase 1 (detenida)**: ventana `01:44:07`–`02:58:25` UTC. Durante esta
  ventana corrió alignment (`alignment_sample_001.h5` mtime `01:57:41` →
  `alignment_sample_049.h5` mtime `02:26:14`; análisis `timestamp_utc
  02:57:33`) — **49/49 alignment preservado**, `sync_applied=false`. El grid
  de captura, en cambio, **no produjo ningún punto exitoso** en esta ventana
  (`quicklook_live/quicklook_live_status.json` y
  `quicklook_live_e2e02/quicklook_live_status.json`: ambos
  `points_processed=0`, `status=IDLE`). Esto es consistente con un bloqueo en
  preflight relacionado al contrato de `observer_config` — el mismo problema
  de fondo que motivó el Commit 3 (`493000d` — "fix: validate observer config
  lifecycle in preflight"). No existe un `.md` de blocker explícito para esta
  fase (a diferencia de E2E-01); la ventana de inactividad de ~1h20min entre
  el fin de alignment y el reinicio de captura es la evidencia disponible.
- **Re-ejecución posterior (exitosa)**: `capture_timing_20260828_041810.csv`
  inicia a las `04:18:10`. `mosaic.csv` y `session.csv` (manifest IQ)
  confirman **9/9 puntos `capture_status=success`**, ejecutados entre
  `04:18:47` y `04:22:56` (`session_id` interno de los HDF5:
  `20260828_041810`; gain=40.2, gain_mode=manual, bias_tee_enabled=true en
  los 9 archivos). **9 HDF5 exitosos**
  (`AFC-E2E-02_0001.h5` … `_0009.h5`).
- **Quicklook**: `quicklook_live_e2e02_run/quicklook_live_status.json` —
  `session_id=AFC-E2E-02-20260828-04:18:47`, `points_processed=9`,
  `points_success=9`, `status=OK`.
- **Publicación**: `public/publication_manifest.json` —
  `status=PASS`, `publication_state=READY`, `absent_derivatives=[]`,
  apuntando explícitamente a `quicklook_live_e2e02_run`.
- **Telemetría**: `telemetry/telemetry_summary.json` —
  `created_utc=04:25:33`, `status=OK` (SDR 34.0°C, LNA 26.0°C, rtl_tcp
  escuchando, mount `NOT_EXPOSED`).
- **Dashboard**: funcional de extremo a extremo para esta corrida (servidor
  estático activo, manifest READY, sin derivados ausentes).
- Sin interpretación HI científica: `result_status="NO DEFENDIBLE
  DIFFERENTIAL HI STRUCTURE"` en ambos alignment_result.json de E2E-01 y
  E2E-02 — resultado nulo correcto y esperado para un entorno indoor.

### SESSION TRACEABILITY NOTE

La misma corrida exitosa de AFC-E2E-02 usa **tres identificadores
temporales distintos** según qué artefacto se mire:

- `20260828-01:51:36` — id de plan/grid (hora de creación del grid,
  columna `session_id` en `mosaic.csv` y en `grid_metadata.json`).
- `20260828_041810` — id de ejecución de captura (atributo `session_id`
  embebido en los 9 HDF5, coincide con el nombre de
  `capture_timing_20260828_041810.csv`).
- `AFC-E2E-02-20260828-04:18:47` — id del directorio de salida IQ / Quicklook
  (usado por `quicklook_live_e2e02_run`).

Ninguno de los tres es incorrecto — corresponden a tres momentos distintos
del pipeline (creación del plan, inicio de `capture.py`, directorio de
salida) — pero la ausencia de un `session_id` canónico único propagado a
todos los productos es **deuda técnica abierta y no se corrige en este
ciclo de commits**.

## Tabla de findings

| Finding | E2E origen | Estado actual |
|---|---|---|
| `mount_control.py` `connect()` sin `await` | E2E-01 Fase 2 | OPEN |
| Visibility TOCTOU (grid válido al generarse, inválido al ejecutarse) | E2E-01 (8/9, punto 7) | OPEN |
| Console header "Gain: auto" / "Bias-T: N/D" incorrecto | hallado en auditoría de código, no en un `.md` de E2E | OPEN |
| `observer_config` lifecycle/preflight | E2E-01 Fase 4 / E2E-02 Fase 1 (blocker inferido) | RESOLVED (Commit `493000d`) |
| Dashboard empty-state / sesión vacía | E2E-02 (publication_state) | RESOLVED (Commit `fb6f61c`) |
| Identidad de sesión no canónica (3 IDs distintos) | E2E-02 (ver nota de trazabilidad arriba) | OPEN |
| CSV de plan/sesión no atómico | hallado en auditoría de código | OPEN |
| Ownership de proceso único para INDI | hallado en auditoría de código | OPEN |

Adicionalmente, dos blockers de E2E-01 Fase 3 quedaron **RESOLVED** por
commits de este mismo ciclo: el entrypoint CLI de `sdr_capture.py`
(Commit `d922a88`) y el requisito obligatorio de `--antenna` en
`build_calibration_foundation_v1.py` (Commit `cb6d3bc`).

Ningún finding "OPEN" fue corregido en este documento ni en los commits de
este ciclo — se mantienen abiertos deliberadamente para revisión humana.
