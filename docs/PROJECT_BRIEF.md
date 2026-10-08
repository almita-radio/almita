# ALMITA — Brief técnico y funcional

*Estado al 2026-10-08. Rama `web-polish-v1`. Pensado para quien no estuvo en las últimas sesiones: qué hace el
producto, cómo está armado, qué está probado y dónde leer el código.*

ALMITA es un radiotelescopio de la línea HI de 21 cm (1420.405 MHz) operado desde una Raspberry Pi con
StellarMate. Una montura OnStep apunta una antena a una grilla de puntos del cielo, un RTL-SDR captura el espectro
en cada punto y la cadena REDUCE → SCIENCE produce espectros calibrados y mapas de intensidad HI integrada. Todo
se opera desde una sola web autenticada.

Documentos hermanos:

| documento | contenido |
|---|---|
| [MANUAL_CORTAPALOS.pdf](MANUAL_CORTAPALOS.pdf) ([fuente .md](MANUAL_CORTAPALOS.md)) | Operación paso a paso |
| [BACKLOG.md](BACKLOG.md) | Estado de cada pedido |
| [../FIELD_RUNBOOK.md](../FIELD_RUNBOOK.md) | Servicios y recuperación |

Las guías de hardware (BOM, cableado, mecánica) están en `docs/hardware/` de la rama `main`; ver §4.

---

## 1. Qué funciona hoy: el recorrido del usuario

Marcas de verificación:

| marca | significado |
|---|---|
| **[H]** | Probado en el instrumento real, con evidencia en disco. |
| **[D]** | Corrido sobre datos reales, sin mover hardware. |
| **[C]** | Solo código y tests. |
| **[P]** | Pendiente. |

| etapa | qué hace | dónde (web) | estado |
|---|---|---|---|
| Preparación | Servicios, SDR MAIN y RFI, Bias-T, montura en Ekos/INDI. STATUS muestra servicios y dependencias. **REAL PREFLIGHT** lee la montura sin moverla. | STATUS, ALIGN/PIPELINE | [H] |
| ALIGN | Patrón de anillos alrededor del Sol (de día) o de una zona HI (de noche). Estima el offset de apuntado y, si es confiable, hace **SYNC** (nuevo, 4012330). Siempre muestra el offset al final. | ALIGN → REAL ALIGNMENT | HI [H] sin estimación (2026-10-07); SOLAR y SYNC [C] |
| CALIBRATE | **Reference Wizard**: 50 Ω en la entrada del LNA, reconectar la antena, zonas HI ALTO/BAJO con GOTO real, FINISH. Construye el perfil RELATIVE para OBSERVE, QUICKLOOK y REDUCE. | CALIBRATE → REFERENCE WIZARD | [H] 4 sesiones |
| PLAN / OBSERVE | Grilla ecuatorial (EASTMOST_SAFE o FIXED_CENTER). PLAN con PREFLIGHT (BLOCK/WARNING/PASS) y **gain pilot** opcional. START lanza la captura; STOP envía SIGINT. | OBSERVE | [H] 400, 676, 25 y 100 puntos. STOP real [P]. |
| QUICKLOOK | En vivo durante la sesión: espectro, cascada, mapa, grilla nativa, stack 3D y referencia RFI. | MONITOR | [H] |
| REDUCE | Nivel 0 (HDF5 crudo) → nivel 1 (espectros por punto, calibración RELATIVE, eje LSRK, máscaras, calidad). | REDUCE | [D] |
| SCIENCE | Mapas A (celdas medidas), B y C (interpolados en bloques, ≤3 GB). LOO y diagnósticos. Exportes. | SCIENCE | [D] |

Orden correcto, por dependencias reales:

1. Ekos: alineación base de la montura.
2. ALIGN, opcional pero recomendado. No usa el perfil.
3. CALIBRATE, solo si no hay un perfil a la misma frecuencia, muestreo y **ganancia**.
4. OBSERVE.
5. REDUCE, con el mismo perfil.
6. SCIENCE, con frame LSRK.

Detalle y botones exactos: manual cortapalos.

---

## 2. Qué cambió desde 8dfb3ab

Base: **8dfb3ab** (2026-10-07). Arregla el incidente del wizard con 50 Ω:
- detecta un handle USB de MAIN viejo (dispositivo re-enumerado después de iniciar `rtl_tcp`);
- las fallas de librtlsdr anulan el ACK de sintonía;
- las fallas se muestran en la web;
- agrega timeouts.

Después del fix, el wizard real se completó (WIZARD-20261007-010706, DONE). **[H]**

Commits posteriores, en orden. Casi todos salen de pedidos directos del operador.

| commit | cambio | decisión / cambio de comportamiento |
|---|---|---|
| 547c5b0 | Lista de trabajos | `list_jobs` ordena por `started_epoch` (lo más nuevo primero). La falla del wizard se muestra una sola vez, en su paso. |
| c9d6316 | Cámara de montura | Rotación de la imagen persistente (0/90/180/270) en `data/runtime/mount_camera_config.json`, fijada en 180° porque la cámara está montada al revés. |
| 4aae46f | REDUCE: elegir perfil | Explorador de archivos del **servidor**, compartido con OBSERVE (`U.mountProfileExplorer`, `console/common.js`). Los perfiles con el mismo nombre de archivo se distinguen por sesión, fecha y Hz/sps/dB. |
| b1f9331 | OBSERVE: estado efectivo | Una sesión que terminó sola ya no aparece como RUNNING. `reconcile_observation_status` deriva `effective_state` desde la propia sesión, porque la etiqueta del orquestador solo se cierra con STOP. |
| f4cd4c5 | Gain pilot: 404 | Las capturas reales del pilot no llegaban al servidor: la regex de rutas `_OPS_START_RE` era demasiado corta para `observe_gain_pilot_capture`. Ahora es `[a-z_]{3,40}`. Las fallas se muestran en el panel y las esperas son de 300 s. |
| ae4242b | Gain pilot: re-plan | Con una ganancia aprobada distinta de la del plan, el botón **RE-PLAN GRID AT X dB** replanifica la grilla con esa ganancia y prepara **una** recaptura de control. Antes caía en un bucle (pilot NOT PLANNED, plan todavía en la ganancia vieja). |
| d2efeac | MONITOR stack 3D | Altura y color con el rango completo de los datos, como la cascada. Antes, el rango 2–98 % sin recorte exageraba los picos unas 2.3 veces. Ahora hay una etiqueta con la escala en dB. |
| 67df4a6 | Log de trabajos | REDUCE muestra el avance punto por punto (lee el `events.jsonl` del motor congelado, sin tocarlo). SCIENCE muestra etapas y bloques (tiles). Las líneas no llevan llaves para no romper el parseo del JSON final. |
| 71e70d2 | Manual cortapalos | `docs/MANUAL_CORTAPALOS.{md,pdf}` y `scripts/build_manual_pdf.py`, enlazados desde `FIELD_RUNBOOK.md`. |
| 4012330 | **ALIGN con SYNC** | Decisión del operador: ALIGN debe corregir, no solo medir, tanto con el Sol como con HI. Ver el detalle debajo de la tabla. |
| 8f7ab05 | Aviso del gain pilot y etiqueta topocentric | Ver el detalle debajo de la tabla. |

**4012330 — ALIGN con SYNC**
- Hay un único camino de SYNC, `AlignmentRunner.maybe_sync` en `alignment.py`.
- Envía SYNC solo si se cumplen las cuatro condiciones:
  1. el operador lo marcó (casilla en la web, marcada por defecto);
  2. hay un offset estimado;
  3. confianza ≥ 0.65;
  4. offset ≤ `--max-sync-offset-deg` (5° por defecto, máximo 10). Este límite es nuevo.
- Procedimiento: GOTO a la posición compensada, SYNC a la referencia, y salir 2° y volver para medir la
  repetibilidad.
- HI ya no devuelve el estado fijo «NON-OBSERVATIONAL TEMPLATE — SYNC BLOCKED»: se juzga igual que el Sol
  (`PASS` / `LOW CONFIDENCE`).
- La web siempre muestra el offset (recuadro **ALIGNMENT RESULT**). PLAN y la página PIPELINE siguen con
  `--no-sync`.
- **El SYNC nunca se ha enviado a la montura real.**

**8f7ab05 — Aviso del gain pilot y etiqueta topocentric**
- Antes de APPROVE THIS GAIN, el gain pilot avisa si la ganancia no la cubre el perfil de QUICKLOOK. Usa el mismo
  `/validate` que el preflight. El aviso dice que el PLAN quedará BLOCKED y que REDUCE no calibrará esos puntos,
  ofrece salidas y lista los perfiles que sí sirven. Es solo un aviso; no bloquea. Caso real del 2026-10-08:
  42.1 dB aprobados contra un perfil de 40.2 dB.
- En REDUCE, la etiqueta «topocentric (no observer/target needed)» era falsa: el motor congelado exige hora,
  sitio y RA/Dec para cualquier frame. Solo se cambió el texto.

Este brief (commit de sincronización) agrega `docs/PROJECT_BRIEF.md`, actualiza `docs/BACKLOG.md` y versiona
scripts de evidencia que ya cita la documentación del producto (§6).

Alcance: 33 archivos y unas 1.8 mil líneas desde 8dfb3ab (`git diff --stat 8dfb3ab HEAD`).

---

## 3. Arquitectura

### Hardware

- **Cadena MAIN:** antena → Nooelec SAWbird H1 (LNA + filtro, alimentado por Bias-T) → RTL-SDR Blog V4 con
  **serial 00000001**.
- **RFI REF:** antena B → LNA (alimentado por el Bias-T del SDR de RFI REF; confirmado por el operador el
  2026-10-08) → RTL-SDR con **serial 00000002** (opcional). La web trae el Bias-T de RFI REF encendido por
  defecto y `capture.py` lo pasa como `-T` al `rtl_tcp` de :1235. Que el LNA reciba alimentación no está medido.
- **Montura:** OnStep, driver INDI «LX200 OnStep».
- **Otros:** sensores DS18B20 (temperatura del SDR y del LNA) y una cámara de montura (stream en MONITOR).

### Servicios y puertos

| servicio systemd | proceso | puerto / salida |
|---|---|---|
| `almita-observe-api` | `almita_orchestrator_server.py` | **:8088**, la web única (consola + API + streams), con HTTP Basic Auth. Credenciales en `~/.config/almita/web_auth.json`, fuera del repo. `KillMode=process`: reiniciarlo no corta capturas. |
| `almita-console-watcher` | `almita_console_watcher.py` | Escribe `data/runtime/almita_status.json` cada 2 s. Solo lectura. |
| `almita-system-blackbox` | `almita_system_blackbox.py` | Latido del sistema. No es científico. |
| `rtl_tcp` | `rtl_tcp -d 00000001 -a 127.0.0.1 -p 1234 -f 1420405752 -s 2400000 -g 40.2 -T` | 127.0.0.1:**1234**. `-T` activa el Bias-T. |

Fuera de systemd:
- **INDI** (:7624) lo levanta Ekos/StellarMate.
- **RFI REF** usa un `rtl_tcp` desechable en :1235, que abre `capture.py` por sesión.
- El puerto 8090 está retirado.

### Configuración central

- `observer_config.json`:
  - sitio: Santiago, −33.4489 / −70.6693, 570 m;
  - `observation_defaults`: **1420405752 Hz, 2400000 sps, 40.2 dB**, `beam_fwhm_deg` 20 (provisional, no
    medido), capture 10 s, settle 1 s.
- `sdr_tuning.py` es la única fuente de frecuencia y muestreo para OBSERVE, el wizard y ALIGN. Además verifica el
  ACK de `rtl_tcp` en el journal. ALIGN usa una ganancia fija propia de 40.2 dB (`alignment.DEFAULT_GAIN_DB`).
- Los perfiles de calibración son por ganancia. La compatibilidad compara frecuencia, muestreo, ganancia y
  topología, sin fecha (`calibration_foundation.check_calibration_compatibility_values`).

### Cómo corre el trabajo

- **Trabajos web** (ALIGN, CALIBRATE, gain pilot, REDUCE, SCIENCE): los gestiona `almita_web_ops.py`.
  - La tabla `STAGES` define, por etapa, si mueve hardware y qué recursos usa.
  - `build_command` arma la línea de comando y `start` la lanza.
  - Un runner desprendido (`--runner`) ejecuta el script real y escribe `data/runtime/web_ops/<job>/job.log`.
  - `classify` lee los artefactos reales y entrega PASS, PARTIAL o FAIL.
  - `stop` envía **SIGINT**.
  - Las etapas que mueven la montura exigen: escribir `MOVE`, preflight real sin BLOCK, montura en reposo
    (conectada, desaparcada, sin error, en `Idle`/`Tracking`) y que no haya una observación activa.
- **OBSERVE:**
  - `observation_plan.py` + `observation_preflight.py` hacen PLAN y PREFLIGHT.
  - `observation_orchestrator.py` lanza `capture.py` y `quicklook_live.py` desprendidos por sesión, en
    `data/mosaic/<sesión>/`.
  - `observation_gain_pilot.py` implementa el gain pilot.
- **Navegador:** `console/*.html|js` se sirven en vivo desde el árbol de fuentes. `common.js` contiene
  `AlmitaUI`: jobPanel, explorador de perfiles y mapas de cielo.

### Archivos clave por etapa

| etapa | backend / motor | web |
|---|---|---|
| Servidor y rutas | `almita_orchestrator_server.py`, `almita_web_ops.py`, `almita_web_system.py` (salud y versión) | `console/common.js`, `console/status.html` |
| ALIGN | `alignment.py` (el que usa la web), `alignment_engine/`, `hi4pi_map.py` (cielo HI4PI), `indi_telescope_control.py` | `console/align.{html,js}` |
| CALIBRATE | `calibrate_reference_wizard.py`, `calibration_engine/reference_wizard.py`, `calibration_engine/wizard_profile_converter.py` (perfil, exige 50 Ω en `LNA_INPUT`), `calibration_engine/spectral_contrast.py`, `calibration_foundation.py`, `calibration_profile_catalog.py` | `console/calibrate.{html,js}` |
| PLAN / OBSERVE | `observation_spec.py`, `observation_plan.py`, `observation_preflight.py`, `observation_orchestrator.py`, `capture.py`, `observation_gain_pilot.py`, `sdr_tuning.py`, `sdr_capture.py`, `rfi_monitor.py` | `console/observe.{html,js}`, `console/pipeline.{html,js}` |
| QUICKLOOK / MONITOR | `quicklook_live.py`, `quicklook_spectrum.py`, `quicklook_session_waterfall.py`, `almita_console_watcher.py`, `mount_camera.py` | `console/index.html`, `app.js`, `spectral_stack_3d.js` |
| REDUCE | `reduce_campaign_run.py` (puente de RUN), `almita_reduce.py` (PLAN), `reduce_engine/` | `console/reduce.{html,js}` |
| SCIENCE | `science_web_bridge.py` (mapas A/B/C en bloques), `science_engine/` | `console/science.{html,js}` |
| Estado compartido | `runtime_state.py` | — |

### Módulos congelados

No se tocan sin una decisión explícita del operador:
- `reduce_engine/`, `science_engine/`, `almita_reduce.py`, `almita_science.py`, `capture.py`, `sdr_capture.py` e
  `indi_telescope_control.py`. El PREFLIGHT real avisa si alguno cambió.
- `observation_orchestrator.py` y `observation_spec.py` (pins de forensics).

Ver `docs/REDUCE_V1_FREEZE.md`, `docs/SCIENCE_V1_FREEZE.md` y `docs/WEB_V1_FREEZE.md`.

### Datos

- `data/` está fuera de Git, salvo `data/hi_sky_catalog_2000pts.csv`.
- Subcarpetas: `mosaic/` (sesiones), `calibration/` (wizard y perfiles), `alignment/`, `reduced/`, `science/`,
  `runtime/`.
- Los insumos de los tests están en `tests/fixtures/`.

---

## 4. Qué está desplegado

- **Repositorio:** `git@github.com:almita-radio/almita.git` (https://github.com/almita-radio/almita).
- **Rama de trabajo:** `web-polish-v1`. El SHA publicado está en el commit que agrega este brief; se informa al
  entregarlo.
- **Servidor web** (`almita-observe-api`): arrancó el 2026-10-08 a las 12:55:22 UTC.
  - Ningún `.py` del repo cambió después de ese arranque, así que el Python cargado es el de HEAD.
  - `console/` se sirve en vivo, así que también es HEAD.
  - Los scripts que lanza (`alignment.py`, `reduce_campaign_run.py`, `science_web_bridge.py`, …) se cargan en
    cada trabajo.
  - **En la práctica corre el código de HEAD.** El SHA del pie de página se lee de Git la primera vez que se
    consulta, así que puede mostrar un commit anterior con el mismo código.
- **Otros servicios:**
  - watcher: desde 2026-10-06 09:22;
  - blackbox: desde 2026-10-05 22:40;
  - `rtl_tcp`: desde 2026-10-07 00:25.

  Su código no cambió después de esos arranques.
- **Diferencia con `main`:**
  - `web-polish-v1` tiene unos 82 commits que `main` no tiene.
  - `origin/main` tiene 19 commits de documentación y sitio (licencias open hardware, `docs/hardware/` con BOM,
    cableado y mecánica, sitio web) que esta rama no tiene.
  - Las dos ramas solo chocan en `README.md`.
  - No se hizo merge. Unir las ramas es una decisión del operador (PR); ver BACKLOG.
- **Ramas locales que no se suben a propósito:** `backup-antes-de-limpiar-github-20260810` y
  `respaldo-local-18-03`. Tienen historia anterior a la limpieza de GitHub y subirlas la desharía.
- Otras ramas locales (`backlog/…`, `science/…`, `web/backlog-integration`, `science-forensics-v1`) ya están
  integradas o son históricas.

---

## 5. Validación

### Tests automatizados

| conjunto | resultado |
|---|---|
| SCIENCE bridge + CLI de REDUCE + progreso en el log | 76 passed, 2 skipped |
| Alineación (HI v2, alignment v2, CLI) | 61 passed |
| SYNC guard (montura falsa: enviar / no pedido / baja confianza / offset grande / GOTO fallido / ruta HI completa) | 8 passed |
| Web: hardening, align/calibrate, sync_flow, frontend completo | 138 passed |
| Frontend + preflight de observación (después de 8f7ab05) | 90 passed |
| Stack 3D (consola) | 5 passed |

Los tests de navegador usan chromium headless con la API simulada (`tests/test_web_frontend.py`).

### 5.1 Suite completa

No se volvió a correr completa en esta sincronización: tarda unos 30 min en el Pi y ningún cambio la exige.

- La última corrida completa fue el 2026-10-06 (BL-009): 2447 passed, con las fallas de esa corrida corregidas y
  vueltas a correr en verde.
- El 2026-10-08 se lanzó una corrida completa y se detuvo a propósito al 38 %, sin ninguna falla hasta ese punto.
- Los cambios desde entonces están cubiertos por los conjuntos de la tabla de arriba.
- La prueba de estabilidad de la consola (`test_field_console_left_open_for_30_minutes…`) simula 30 min en
  segundos y pasó hoy dentro de `test_web_frontend.py`.

### Hardware y sesiones reales

| fecha (UTC) | qué | resultado |
|---|---|---|
| 2026-10-02 | OBSERVE 400 puntos, 30×30°, 10 s, 40.2 dB (`ALMITA-OBSERVE-20261002-01:45:08`) | 400 HDF5. Perfil `WIZARD-20261002-013321-160933` (contraste DEFENSIBLE). **Preservada.** |
| 2026-10-05/06 | SCIENCE sobre esa sesión | 400/400 puntos, calidad GOOD, ~6.5 min, pico de 1.03 GB |
| 2026-10-07 | Wizard (incidente 50 Ω) → fix → wizard DONE | `WIZARD-20261007-001113` ABORTED; `WIZARD-20261007-010706` DONE |
| 2026-10-07 | ALIGN HI real (25 posiciones) | `NO DEFENDIBLE DIFFERENTIAL HI STRUCTURE`: sin offset |
| 2026-10-07 | OBSERVE 676 puntos (`…20261007-02:00:15`) | 676 HDF5, COMPLETED |
| 2026-10-08 | Wizard a 42.1 dB (`WIZARD-20261008-010548`) | DONE, perfil READY (contraste INCONCLUSIVE) |
| 2026-10-08 | Gain pilot real (4 sesiones) | READY a 42.1 dB; flujo RE-PLAN usado |
| 2026-10-08 | OBSERVE 25 puntos (con pilot) y 100 puntos (`…01:50:03`), 42.1 dB | COMPLETED; QUICKLOOK y RFI REF en vivo |
| 2026-10-08 | REDUCE y SCIENCE desde la web sobre la sesión de 100 puntos | PASS; avance en el log comprobado con datos reales |

Duración real medida: unos 32 s por punto con capturas de 10 s, y unos 23 s por punto con capturas de 1 s.

### Pendiente en hardware

1. SYNC de ALIGN: primero SOLAR sin SYNC, después con SYNC, y luego un ALIGN de confirmación.
2. ALIGN SOLAR desde la web, que nunca se ha corrido.
3. STOP OBSERVATION real.
4. STOP CURRENT STEP del wizard durante una captura real (BL-007).
5. Fallas provocadas: desconectar MAIN y apagar INDI.
6. Bench `sdr --yes` (BL-008) y la CLI de `mount_control.py` (BL-011).
7. El aviso nuevo del gain pilot, visto en una sesión real.
8. Un recorrido completo siguiendo el manual.
9. Antena B: medir que su LNA recibe alimentación con el Bias-T de RFI REF (que tiene LNA ya está confirmado).

---

## 6. Bugs, limitaciones y backlog

El estado completo está en [BACKLOG.md](BACKLOG.md). Lo esencial:

**Limitaciones conocidas**
- El haz (FWHM 20°) es provisional: no está medido, y `alignment.py` usa 14° como placeholder.
- ALIGN HI rara vez estima un offset con este haz. La referencia útil es el Sol.
- RFI REF solo sirve de monitoreo: REDUCE no lo asocia a los puntos.
- No hay modelo de horizonte: CALIBRATE muestra «OBSTACLES NOT EVALUATED».
- Calibración solo RELATIVE: no hay Kelvin ni ganancia absoluta.
- En REDUCE, topocentric exige los mismos metadatos que LSRK (motor congelado).
- La sección ALIGN de PIPELINE no tiene SYNC.
- El pie de página dice «no authentication», pero el servidor sí exige Basic Auth. Es un texto equivocado.

**Archivos que quedaron fuera de Git (§7)**
- Scripts de investigación de agosto del operador.
- Salidas de benchmarks de montura.
- Respaldos `.tar.gz`.
- Un archivo vacío `=.9`.

Versionados en esta sincronización:
- `rf_chain_characterization.py`, `rf_gain_control_test.py`, `sun_detectability_test.py` y su test, porque la
  documentación y los comentarios del producto los citan como fuente de método;
- `examples/block-validation-01.yaml`, un ejemplo de especificación.

---

## 7. Manual cortapalos

- `docs/MANUAL_CORTAPALOS.pdf` (9 páginas), con fuente editable en `docs/MANUAL_CORTAPALOS.md`.
- Se regenera con `/usr/bin/python3 scripts/build_manual_pdf.py`, que usa el `markdown` del sistema y chromium
  headless.
- Está enlazado al inicio de `FIELD_RUNBOOK.md`.
- Es un primer borrador, revisado contra el código y la web, con marcas [H]/[D]/[C]/[P] por paso.
- Ya incluye el SYNC de ALIGN y el aviso del gain pilot.
- Su Anexo C lista las comprobaciones físicas pendientes.
- Falta documentar el procedimiento exacto de Ekos, que hace el operador.
