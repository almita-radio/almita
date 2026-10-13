# ALMITA — Brief técnico y funcional

*Estado al 2026-10-13. Rama `web-polish-v1`. Pensado para quien no estuvo en las últimas sesiones: qué hace el
producto, cómo está armado, qué está probado y dónde leer el código.*

ALMITA es un radiotelescopio de la línea HI de 21 cm (1420.405 MHz) operado desde una Raspberry Pi con
StellarMate. Una montura OnStep apunta una antena a una grilla de puntos del cielo, un RTL-SDR captura el espectro
en cada punto y la cadena REDUCE → SCIENCE produce espectros calibrados y mapas de intensidad HI integrada. Todo
se opera desde una sola web autenticada.

Documentos hermanos:

| documento | contenido |
|---|---|
| [MANUAL_CORTAPALOS.pdf](MANUAL_CORTAPALOS.pdf) ([fuente .md](MANUAL_CORTAPALOS.md)) | Operación paso a paso |
| [BACKLOG.md](BACKLOG.md) | Estado de cada pedido, pendientes con prioridad y orden |
| [../FIELD_RUNBOOK.md](../FIELD_RUNBOOK.md) | Servicios y recuperación |

Las guías de hardware (BOM, cableado, mecánica) están en `docs/hardware/` de la rama `main`; ver §4.

---

## 1. Qué funciona hoy

**En una frase:** el recorrido CALIBRATE → OBSERVE → QUICKLOOK → REDUCE → SCIENCE está hecho en el instrumento
real y desplegado. Lo que falta es la mitad de ALIGN (Sol y SYNC), las paradas reales (STOP) y medir dos supuestos
físicos: el haz y la alimentación del LNA de la antena B.

**Bloqueos actuales**

- **La web es alcanzable desde Internet** con contraseña por HTTP sin cifrar (BL-040). El operador indicó el
  2026-10-13 que ese acceso es el que quiere; quedan tres comprobaciones suyas.
- **ALIGN no corrige todavía:** el SYNC nunca se envió a la montura real y ALIGN SOLAR nunca corrió (BL-026). La
  única corrida real, con HI, no logró estimar un offset.
- **STOP OBSERVATION:** dos pruebas reales el 2026-10-13. Cierre, estado y datos funcionan. **Tras un STOP
  durante un GOTO la montura queda en seguimiento** (BL-059): hay que apagarlo en Ekos. Falta el caso durante
  una captura (BL-027).
- **El haz no está medido** (BL-034): 20° en la configuración y 14° en `alignment.py`, ambos provisionales.
- **Antena B:** el Bias-T se envía, pero nadie midió que el LNA reciba alimentación (BL-031). Desde el
  2026-10-13 toda la configuración lo trae encendido por defecto.

Ninguno impide observar como se observó el 2026-10-08.

**Sin actividad en el instrumento desde el 2026-10-08:** ni sesiones, ni trabajos web, ni reinicios. Lo único
nuevo en `data/` son los restos de la corrida de tests del 12 y el latido del sistema. El 2026-10-13 hubo una
pasada de correcciones de software sin hardware (§2).

### Estado por funcionalidad

Cinco niveles, y ninguno implica el siguiente:

| nivel | significado |
|---|---|
| **impl** | El código está en `web-polish-v1`. |
| **tests** | Hay tests automatizados que lo cubren. Resultados en §5. |
| **datos** | Corrido sobre datos reales guardados, sin mover hardware. |
| **hardware** | Hecho en el instrumento real, con evidencia en `data/`. |
| **desplegado** | El proceso que corre hoy en el Pi ya lo tiene cargado (§4). |

Todas las filas están implementadas y desplegadas, salvo donde se indica.

| funcionalidad | tests | datos reales | hardware | limitaciones y pendientes |
|---|---|---|---|---|
| **ALIGN** (anillos alrededor del Sol o de una zona HI; offset; SYNC con control) | sí: alineación y SYNC con montura falsa | — | **parcial.** HI: una corrida real de 25 posiciones el 2026-10-07 (`WEB-HI-20261007T011610Z`), sin offset estimable. SOLAR: nunca. SYNC: nunca. | BL-026. Con este haz, HI rara vez estima un offset. La sección ALIGN de PIPELINE no tiene SYNC (BL-043). |
| **CALIBRATE** (Reference Wizard: 50 Ω, zonas HI ALTO/BAJO, perfil RELATIVE) | sí | sí: mapa de zonas con plan real | sí: tres wizards DONE (2026-10-02, 10-07, 10-08) y uno ABORTED. Perfiles a 40.2 y 42.1 dB. | STOP CURRENT STEP sin probar (BL-028). Contraste INCONCLUSIVE el 10-07 y el 10-08. Sin modelo de horizonte (BL-039). Solo RELATIVE: no hay Kelvin. |
| **Gain pilot** (1 o 2 capturas reales para elegir ganancia; RE-PLAN) | sí | — | sí: 2026-10-08, READY a 42.1 dB y RE-PLAN usado. | El aviso de ganancia sin perfil (BL-023) no se ha visto en una sesión real (BL-032). La consulta de estado sin pilot ya no deja un trabajo fallido (BL-048, comprobado en el servidor). |
| **OBSERVE** (PLAN, PREFLIGHT, START, STOP) | sí | — | sí: 400 puntos (10-02), 676 (10-07), 100 y tres de 25 (10-08). Todas COMPLETED, 0 fallidos en las dos últimas. | **STOP real sin probar** (BL-027). Fallas provocadas sin probar (BL-029). |
| **QUICKLOOK** (espectro, cascada, mapa, grilla nativa, vista interpolada) | sí | sí | sí: en vivo en todas las sesiones con perfil compatible; la última procesó 25/25. | Se bloquea si el perfil no coincide en ganancia. Es vista previa, no producto científico. |
| **RFI REF** (antena B, SDR 00000002, `rtl_tcp` en :1235) | sí | — | **parcial.** Seis sesiones reales con ACK de sintonía y `-T` en el log de lanzamiento. Alimentación del LNA sin medir. | BL-031. REDUCE no lo usa (BL-037). Bias-T encendido por defecto en la web, el esquema y los YAML de ejemplo desde el 2026-10-13; `false` explícito sigue disponible. `capture.py` por CLI directa necesita `--rfi-ref-bias-t`. |
| **MONITOR** (sesión, quicklook, RFI, stack 3D, cámara, log) | sí: navegador headless; relay de la cámara con cámara simulada | sí: stack 3D con datos reales | sí: usado en vivo. | Cámara: 503 intermitente (3 de 6 pedidos entre el 10-08 y el 10-13). Defecto del relay corregido y desplegado el 2026-10-13; el stream respondió bien tras el reinicio. La causa del lado de la cámara no se puede ver desde el Pi (BL-054). |
| **REDUCE** (nivel 0 → nivel 1, RELATIVE, LSRK) | sí | sí: cinco reducciones, la última el 2026-10-08 sobre la sesión de 100 puntos | — | Motor congelado. Topocentric exige los mismos metadatos que LSRK (BL-036). Las dos sesiones del 10-08 por la tarde no están reducidas. |
| **SCIENCE** (mapas A, B y C; LOO; exportes) | sí | sí: 400/400 puntos, calidad GOOD, pico de 1.03 GB; y la sesión de 100 puntos | — | Motor congelado. B y C interpolan: más píxeles no es más resolución. Depende del haz provisional. |
| **Servicios** (cuatro unidades systemd) | sí | — | sí: activas y habilitadas. `almita-observe-api` reiniciado el 2026-10-13 15:19 UTC con el build `e696038`; los otros tres corren desde el 2026-10-08. Unidades instaladas idénticas a `systemd/`. | Ninguna diferencia entre el servidor y el código de la rama (§4). |
| **Manual cortapalos** | — | revisado contra código y `data/` el 2026-10-12 y el 2026-10-13 | no: nunca se hizo el recorrido completo siguiéndolo | BL-033. Falta el procedimiento de Ekos. |

Orden de operación, por dependencias reales:

1. Ekos: alineación base de la montura.
2. ALIGN, opcional pero recomendado. No usa el perfil.
3. CALIBRATE, solo si no hay un perfil a la misma frecuencia, muestreo y **ganancia**.
4. OBSERVE.
5. REDUCE, con el mismo perfil.
6. SCIENCE, con frame LSRK.

Detalle y botones exactos: manual cortapalos.

---

## 2. Qué cambió

### Desde fd8a962 (2026-10-08 19:27 UTC)

| fecha (UTC) | qué | evidencia |
|---|---|---|
| 2026-10-08 20:50 | **Reinicio del Pi.** Los cuatro servicios arrancaron solos. | `last -x`; unidades con `NRestarts=0` |
| 2026-10-08 21:04 | OBSERVE real, 25 puntos, 30×30°, 10 s, 42.1 dB. | `data/mosaic/ALMITA-OBSERVE-20261008-21:04:06`: 25 HDF5, 0 fallidos, 15 min |
| 2026-10-08 22:33 | **b6eca65** — RFI REF con Bias-T encendido por defecto (BL-025). | 8 archivos, +66/−8 |
| 2026-10-08 22:35 | OBSERVE real, 25 puntos, 50×50°, 1 s, 42.1 dB, altura mínima pedida 1°. | `…20261008-22:34:35`: 25 HDF5, COMPLETED, 12 min |
| 2026-10-12 | Revisión de estado: este brief, el backlog y el manual. `.gitignore` cubre las salidas de montura. | **7fe9c56**, solo documentación y `.gitignore` |
| 2026-10-13 | Segunda revisión de estado. Sin cambios de código. Dos hallazgos nuevos sobre la antena B (BL-052, BL-053) y el aviso del manual ampliado. | **71af9d5**, solo documentación |
| 2026-10-13 | **Pasada autónoma del backlog**, sin hardware. Cierra BL-041, BL-044 a BL-049, BL-051, BL-052 y BL-056; corrige el relay de la cámara; abre BL-054 y BL-055. | **7b2eefb**, **4a472ce**, **2017a14**, **bc03cc2**, **e696038**; detalle en [BACKLOG.md §1.1](BACKLOG.md) |
| 2026-10-13 15:19 | **Reinicio de `almita-observe-api`**, una vez y sin trabajos activos. Carga b6eca65 y toda la pasada. | PID 711914, build `e696038`; comprobado con la web autenticada |

No hubo ALIGN, CALIBRATE, gain pilot, REDUCE ni SCIENCE después de fd8a962.

**Pasada autónoma del 2026-10-13: qué cambia para quien opera**
- **Bias-T de RFI REF encendido por defecto en todas partes:** el esquema de las especificaciones YAML
  (`observation_spec.py`), los ejemplos y el plan de forensics, además de la web, que ya lo tenía. Un `false`
  explícito se respeta, para otra cadena sin LNA. Una especificación vieja que no escriba el campo ahora
  revalida encendida.
- **Plan de forensics:** su única diferencia con la campaña de septiembre es ese Bias-T. El valor original queda
  anotado en el propio plan y las guardias (SHA del plan y pins de módulos congelados) se actualizaron dejando
  escritos los valores anteriores.
- **Scripts de evidencia** (`rf_chain_characterization.py`, `rf_gain_control_test.py`,
  `sun_detectability_test.py`): toman la frecuencia de la configuración central y exigen el ACK de `rtl_tcp`. No
  se ejecutaron en hardware después del cambio.
- **Cámara:** el relay ya no responde 503 mientras le queda un reintento, y deja el motivo en el journal.
- **Pie de página:** describe la autenticación y el transporte reales, y el SHA de «build» es el del arranque.
- **`mount_control.py`:** acepta `-33:27:00` como declinación.
- **`hw_hi_night_scan.py`:** cuando ningún objetivo HI es utilizable a esa hora termina en BLOCKED con su
  evidencia, en vez de una traza.
- Todo lo anterior está activo desde el reinicio de las 15:19 UTC (§4).

**b6eca65 — Bias-T de RFI REF**
- `/api/observe/defaults` devuelve `rfi_ref.bias_tee: true` y el formulario lo sigue y lo conserva al RE-PLAN.
- En ese commit el esquema (`observation_spec.py`) mantenía `false` por omisión; pasó a `true` el 2026-10-13 (BL-041).
- Cargado en el servidor desde el reinicio del 2026-10-13 (§4).

Hallazgos de la revisión del 2026-10-12, todos en el backlog:
- la web recibe tráfico de Internet (BL-040);
- un test escribe en el `data/` real (BL-046);
- el manual daba 10° como altura mínima por defecto y la web rellena 30° (corregido);
- las carpetas `data/alignment/SOLAR-*` no son corridas solares: son restos de tests.

Hallazgos de la revisión del 2026-10-13:
- `examples/science_forensics_experiment.yaml` tenía RFI REF activado con `bias_tee: false`, copiado de la
  campaña de septiembre, anterior a la confirmación del LNA de la antena B (BL-052, corregido el mismo día);
- las guías de hardware de `main` describen la cadena de RFI REF como antena → filtro notch FM → LNA → SDR y no
  dicen que el LNA se alimenta por Bias-T; el manual y este brief no mencionan el filtro (BL-053);
- además de :8088, el Pi escucha en todas las interfaces en los puertos 22, 80, 445, 5900, 6080 y 7624 (INDI,
  sin autenticación). No se verificó cuáles reenvía el router (BL-040);
- los escáneres de Internet siguen llegando a :8088 (§4).

### Desde 8dfb3ab hasta fd8a962


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
  Las guías de hardware de `main` ponen además un filtro notch FM antes del LNA; la cadena real está por
  confirmar (BL-053).
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

Comprobado el 2026-10-13 a las 15:20 UTC, después del único reinicio de esta pasada.

### Qué ejecuta cada servicio

`almita-observe-api` se reinició el 2026-10-13 a las 15:19:53 UTC, después de comprobar en un paso aparte que no
había trabajos ni observaciones (los dos PID de la última sesión eran procesos ya terminados). Los otros tres
servicios corren desde el reinicio del Pi del 2026-10-08 a las 20:50:50 UTC. Las unidades instaladas en
`/etc/systemd/system/` son idénticas a las de `systemd/` del repo.

| servicio | PID | código que tiene cargado | diferencia con HEAD (el código de producto de HEAD es el de b6eca65) |
|---|---|---|---|
| `almita-observe-api` (:8088) | 711914 | Build **`e696038`** (lo informa `/api/system/version`, resuelto al arrancar). `console/` se sirve en vivo. | Ninguna en código. Los commits posteriores de la rama solo cambian documentación. |
| `almita-console-watcher` | 824 | `almita_console_watcher.py`, sin cambios desde el 2026-10-06 | Ninguna |
| `almita-system-blackbox` | 679 | `almita_system_blackbox.py`, sin cambios desde el 2026-10-05 | Ninguna |
| `rtl_tcp` | 848 | Binario del sistema: `rtl_tcp -d 00000001 -a 127.0.0.1 -p 1234 -f 1420405752 -s 2400000 -g 40.2 -T` | Ninguna; coincide con `observer_config.json` |

- Los scripts que lanza la web (`alignment.py`, `capture.py`, `observation_gain_pilot.py`,
  `reduce_campaign_run.py`, `science_web_bridge.py`, …) se cargan en cada trabajo, así que ya corren el código
  del árbol de trabajo, incluida la pasada del 2026-10-13.
- No se reinició ningún otro servicio: `almita-console-watcher`, `almita-system-blackbox` y `rtl_tcp` no
  cambiaron, y conservan sus PID del 2026-10-08, igual que `indiserver`.
- **Comprobado en la web autenticada tras el reinicio** (pedidos locales con las credenciales del operador):
  - `/api/system/version` y `version.json`: build `e696038`, «HTTP (no TLS), single-user Basic authentication»;
  - las ocho páginas y `common.js` responden 200; sin credenciales o con contraseña equivocada, 401;
  - `/api/system/health`: `observe_api`, `field_console` y `console_watcher` en UP;
  - `/api/observe/defaults`: `rfi_ref.bias_tee: true`, 1420405752 Hz, 2400000 sps, 40.2 dB, altura mínima 30°;
  - cámara: con cero espectadores, el stream respondió 200 y entregó 2.1 MB en 6 s; estado STREAMING sin error;
  - gain pilot: la consulta de estado sobre una sesión sin pilot terminó con código 0 y PASS;
  - el journal del servicio no tiene errores desde el reinicio.
- Antes del reinicio el pie decía build `fd8a962` y «HTTP (LAN, no TLS, no authentication)».
- **No comprobado en un navegador real:** las páginas se pidieron por HTTP, no se miraron renderizadas.
- Fuera de systemd: `indiserver` en :7624, levantado por Ekos el 2026-10-08 a las 20:58. `almita-console-web`
  (la web vieja) está deshabilitada.
- Sin trabajos ni capturas activos el 2026-10-13: no hay procesos `capture.py`, `quicklook_live.py`,
  `alignment.py`, wizard, REDUCE ni SCIENCE.

### Exposición de red

El servidor escucha en `0.0.0.0:8088`. Entre el 2026-10-10 y el 2026-10-12 el journal registra pedidos desde 57
direcciones públicas distintas: 76 de escáneres, rechazados con 401, y una sesión autenticada desde una dirección
pública el 2026-10-11 a las 16:55 UTC. La autenticación es HTTP Basic sin TLS. El pie de página y el runbook
hablan de «LAN». Ver BL-040.

Del 2026-10-12 a las 23:45 al 2026-10-13 a las 12:35 UTC: pedidos desde 10 direcciones públicas más, todos
rechazados (401, o 400 por intentos de TLS contra un puerto HTTP). Ninguna sesión autenticada en ese lapso. Un
pedido local sin credenciales también recibe 401, así que la autenticación está activa.

Otros puertos que escuchan en todas las interfaces (`ss -ltn`, 2026-10-13): 22 (SSH), 80 (nginx), 445 (Samba),
5900 y 6080 (VNC y noVNC de StellarMate) y 7624 (INDI, que no tiene autenticación y mueve la montura). **No
verificado:** cuáles de ellos reenvía el router; desde el Pi no se puede saber.

### Qué SHA es cada cosa

| qué | SHA |
|---|---|
| Código desplegado en `almita-observe-api` | `e6960383…` (`e696038`), el último commit que cambia código |
| Último commit de la rama de trabajo | el de documentación más reciente; se informa al entregar cada revisión |

Los commits posteriores a `e696038` solo tocan `docs/`, `FIELD_RUNBOOK.md` y el PDF del manual, así que el
servidor no necesita reinicio por ellos.

### Ramas

- **Repositorio:** `git@github.com:almita-radio/almita.git` (https://github.com/almita-radio/almita).
- **Rama de trabajo:** `web-polish-v1`. El último SHA publicado antes de la pasada autónoma es
  `71af9d5e1fe04738fee0e33148ade2147f327108`. El SHA publicado con la pasada se informa al entregarla.
- **`main`** (`origin/main` = `9161fe8`, igual que el `main` local): base común `70b4a5c`.
  - `web-polish-v1` tiene todo el producto actual: 84 commits que `main` no tenía el 2026-10-13, antes del commit
    de esta revisión.
  - `main` tiene 19 commits de documentación y sitio (licencias open hardware, `docs/hardware/` con BOM, cableado
    y mecánica, sitio web) que esta rama no tiene.
  - Solo chocan en `README.md`. La integración está preparada en la rama aparte
    `integration/web-polish-v1-into-main`, sin fusionar ni desplegar (BL-035).
- **Ramas locales que no se suben a propósito:** `backup-antes-de-limpiar-github-20260810` y
  `respaldo-local-18-03`. Tienen historia anterior a la limpieza de GitHub y subirlas la desharía.
- Otras ramas locales (`backlog/wizard-preview-profile-picker`, `science/abc-shared-projection-wip`,
  `web/backlog-integration`, `science-forensics-v1`): todas son ancestros de `web-polish-v1`, así que no tienen
  nada que la rama de trabajo no tenga.
- **Servidor frente a ramas:** el proceso de :8088 corre `e696038`, el último commit con código de la rama.
  `main` no tiene nada del producto actual.

---

## 5. Validación

### Tests automatizados

**Corrida completa vigente: 2026-10-13** (14:29:35 a 15:05:39 UTC, 36 min 2 s), sobre `bc03cc2`, con
`.venv/bin/python -m pytest tests -q -p no:cacheprovider`. Se repitió la suite completa porque los cambios lo
pedían: los fallos de BL-047 solo aparecían en corrida completa, y BL-046 exigía comprobar que ningún test
tocara `data/`.

| resultado | cantidad |
|---|---|
| passed | 2555 |
| failed | 2 |
| skipped | 52 |

- Los tres tests que fallaron el 2026-10-12 pasan (BL-051 y BL-047).
- Las dos fallas son `test_hw_hi_night_scan.py::test_blocked_without_obstime_override…` y
  `…::test_evidence_logging_complete_on_blocked_preflight`. Dependen de la hora: a las 14:40 UTC ningún objetivo
  HI era utilizable y el script terminaba con una traza. El 12, a las 23:10, pasaban. Es BL-056, corregido
  después en `e696038`.
- La corrida guardó un listado de `data/` (archivos, tamaños y fechas, sin `data/runtime`) antes y después: ningún
  archivo nuevo, borrado ni modificado, pero cambiaron las fechas de `data/alignment`, `data/calibration` y
  `data/IQ`. Tres suites creaban y borraban cosas ahí; corregido en `e696038`.

**Corridas parciales del 2026-10-13, posteriores a la completa** (sobre `e696038`; la suite completa no se
repitió):

| qué | resultado |
|---|---|
| `test_hw_hi_night_scan.py`, con un test nuevo que fuerza el caso sin objetivo | 20 passed |
| `test_capture_preflight.py`, `test_almita_web_align_calibrate.py`, `test_alignment_engine_end_to_end.py`, con el listado de `data/` antes y después | 51 passed; `data/` idéntico, fechas incluidas |

**Corridas parciales del 2026-10-13, anteriores a la completa**, una por cambio:

| qué | resultado |
|---|---|
| Cámara, pie de página, test de ALIGN, literales de frecuencia (`test_mount_camera_relay`, `test_mount_camera_rotation`, `test_web_hardening`, `test_alignment_engine_end_to_end`, `test_sdr_tuning`, `test_sun_detectability`) | 99 passed |
| Gain pilot, quicklook y el test de 30 minutos de la consola | 23 passed |
| Bias-T y guardias de forensics (`test_mount_control_cli`, `test_observation_spec`, `test_observation_plan`, `test_almita_orchestrator_server`, `test_observation_orchestrator`, `test_science_forensics_*`, `test_sdr_tuning`) | 311 passed, 2 failed, 3 skipped: una expectativa por actualizar y los pins por mover; corregidos, y después `test_science_forensics_field` y `_bench` dieron 153 passed |

Ninguna corrida usó hardware. Los tres scripts de evidencia de BL-051 y `hw_hi_night_scan.py` no se ejecutaron
contra el instrumento.

**Resultados anteriores**

**Corrida completa del 2026-10-12** (23:10 a 23:43 UTC, 33 min 29 s), sobre el código de b6eca65, con
`.venv/bin/python -m pytest tests -q`. No usa hardware: la sintonía y RFI REF están sustituidos en
`tests/conftest.py`.

| resultado | cantidad |
|---|---|
| passed | 2540 |
| failed | 3 |
| skipped | 52 |

Las tres fallas, vueltas a correr aisladas el mismo día (corrida parcial, 3 tests, 20 s: 2 passed, 1 failed):

| test | en la corrida completa | aislado | causa |
|---|---|---|---|
| `test_sdr_tuning.py::test_no_frequency_literal_in_operational_python` | falla | **falla** | Real y determinista. fd8a962 versionó `rf_chain_characterization.py`, `rf_gain_control_test.py` y `sun_detectability_test.py`, que llevan el literal `1420405752`. BL-051. |
| `test_quicklook_live.py::test_run_exits_on_its_own_once_capture_reports_the_session_ended` | falla | pasa | Depende del orden: otro test deja `quicklook_live.STOP_REQUESTED` activado. BL-047. |
| `test_web_frontend.py::test_field_console_left_open_for_30_minutes…` | falla | pasa | Sensible al reloj con el Pi cargado. Ya conocida. BL-047. |

Cobertura que da esta corrida: todo lo que hay en `tests/` (164 archivos), incluidos alineación y SYNC con
montura falsa, wizard, gain pilot, preflight, orquestador, quicklook, REDUCE, SCIENCE y el frontend en chromium
headless con la API simulada.

Efecto secundario: la corrida creó `data/alignment/SOLAR-20261012-231035` (BL-046). No se borró.

El 2026-10-12 no se corrió nada más: ni conjuntos parciales adicionales ni pruebas en hardware.

**Corrida interrumpida del 2026-10-13** (12:35 a 12:40 UTC). Se lanzó la suite completa sin que ningún cambio de
código lo justificara y se detuvo con SIGINT a pedido del operador, al 19 %: 556 passed, 0 failed, 1 deselected,
en 5 min 16 s. Comando: `.venv/bin/python -m pytest tests -q -p no:cacheprovider --deselect
tests/test_alignment_engine_end_to_end.py::test_preflight_tracking_backend_timeout_fails_that_check_not_the_whole_preflight_call`
(se excluyó el test de BL-046 para no escribir en `data/`). **No es un resultado de la suite:** no llegó a
ninguno de los tres tests que fallaron el 12 y no reemplaza la corrida completa. No dejó archivos en `data/`.

Corridas completas anteriores, según el brief previo y sin repetir: 2026-10-06, 2447 passed; 2026-10-08,
detenida a propósito al 38 % sin fallas.

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
| 2026-10-08 | OBSERVE 25 puntos, 30×30°, 10 s, 42.1 dB (`…20261008-21:04:06`) | 25 HDF5, 0 fallidos, 15 min 00 s |
| 2026-10-08 | OBSERVE 25 puntos, 50×50°, 1 s, 42.1 dB, altura mínima 1° (`…20261008-22:34:35`) | 25 HDF5, COMPLETED, 11 min 57 s; QUICKLOOK 25/25 |

Duración real medida: unos 32 s por punto con capturas de 10 s, y unos 23 s por punto con capturas de 1 s. Las dos sesiones
de 25 puntos dieron 36 s y 29 s por punto: son grillas más abiertas, con GOTO más largos.

**Integridad de lo preservado.** El resguardo `/home/stellarmate/almita_preserve_20261002/` trae dos inventarios
del 2026-10-02, sin modificar: `manifest_before.sha256` (su propio SHA-256 empieza por `c30a7493`) y
`sizes_before.txt`. Los dos listan las mismas **3257** rutas. La carpeta tiene 3260 archivos: esos 3257 más los
tres archivos de inventario.

Las 3257 entradas son más que la sesión y su perfil:

| grupo | entradas | comparado con `data/` actual el 2026-10-13 |
|---|---|---|
| Sesión `mosaic/ALMITA-OBSERVE-20261002-01:45:08` | 3230 | 3230 coinciden en SHA-256 y tamaño |
| Perfil `calibration/WIZARD-20261002-013321-160933` | 18 | 18 coinciden en SHA-256 y tamaño |
| Mapas HI4PI (`reference/…`, 6 archivos) y `hi_sky_catalog_2000pts.csv` | 7 | 7 coinciden en SHA-256 y tamaño |
| `IQ/session.csv` | 1 | **distinto**, 533 → 1288 bytes |
| `runtime/mount_camera_config.json` | 1 | **distinto**, 105 → 128 bytes |

- La cifra de **3248** del reporte anterior es la suma de las dos primeras filas: se comprobó solo la sesión y
  su perfil, que era lo pedido, y no las otras 9 entradas. Esa comprobación se hizo dos veces, antes y después
  de la pasada autónoma, con el mismo resultado.
- Los dos archivos distintos son estado operativo que el sistema reescribe, no datos de la sesión:
  - `IQ/session.csv` es el índice de sesiones de `capture.py`. Ya no tiene la fila de la sesión del 2026-09-28
    y tiene cinco filas nuevas, de las sesiones del 2026-10-07 y del 2026-10-08;
  - `mount_camera_config.json` ganó `rotation_deg: 180` el 2026-10-07 (BL-014).
- **El resguardo en sí está íntegro:** sus 3257 archivos coinciden con su propio manifiesto (comprobado el
  2026-10-13 a las 15:36 UTC). Las versiones originales de esos dos archivos siguen ahí.
- **Conclusión, con su alcance:** sesión, perfil, mapas HI4PI y catálogo están idénticos al 2026-10-02 (3255 de
  3257). No se puede decir «3257 de 3257 idénticos»: dos archivos cambiaron por operación normal.
- **Sin explicar:** `data_bytes_before.txt` dice 11 317 446 987 bytes y los tamaños del inventario suman
  6 841 256 217 bytes. Lo más probable es que la primera cifra sea el tamaño de todo `data/` ese día y no solo
  de lo preservado, pero el resguardo no lo dice y no se puede comprobar hoy.
- Nada de esto se modificó: los inventarios se leyeron y los subconjuntos usados para comparar se escribieron
  fuera del resguardo.

Las revisiones del 2026-10-12 y de la mañana del 13 solo habían comparado cantidades, tamaños y fechas.

### Pendiente en hardware

La lista completa, con la evidencia que falta en cada caso, está en [BACKLOG.md §2.2](BACKLOG.md): BL-026 a
BL-034.

---

## 6. Limitaciones y archivos fuera de Git

Pendientes, prioridades y orden: [BACKLOG.md](BACKLOG.md).

**Limitaciones conocidas**
- El haz (FWHM 20°) es provisional: no está medido, y `alignment.py` usa 14° como placeholder.
- ALIGN HI rara vez estima un offset con este haz. La referencia útil es el Sol, que nunca se probó.
- RFI REF solo sirve de monitoreo: REDUCE no lo asocia a los puntos.
- No hay modelo de horizonte: CALIBRATE muestra «OBSTACLES NOT EVALUATED».
- Calibración solo RELATIVE: no hay Kelvin ni ganancia absoluta.
- En REDUCE, topocentric exige los mismos metadatos que LSRK (motor congelado).
- La sección ALIGN de PIPELINE no tiene SYNC.
- La cámara de la montura falla a ratos y la causa del lado del equipo no está determinada (BL-054).

**Fuera de Git, a propósito.** Nada de esto se borró, se movió ni se subió.

| qué | por qué queda fuera |
|---|---|
| `data/` (sesiones, perfiles, reducciones, mapas, runtime) | Datos y capturas. Ignorado, salvo `data/hi_sky_catalog_2000pts.csv`. Incluye la sesión de 400 puntos `ALMITA-OBSERVE-20261002-01:45:08` y su perfil `WIZARD-20261002-013321-160933`, intactos. |
| `/home/stellarmate/almita_preserve_20261002/` (6.4 GB) | Resguardo, fuera del repo. Se borra solo cuando el operador lo diga. |
| `align_ultima_revision.tar.gz`, `calib_ultima_revision.tar.gz` | Resguardos del 2026-09-28 con trabajos y perfiles de `data/`. |
| `mount_benchmark_targeted_*/` (4 carpetas), `mount_slew_model_output/` | Salidas de campañas de montura de agosto: CSV, JSON y PNG. Desde esta revisión están en `.gitignore`, igual que las demás `mount_benchmark_*`. |
| `mount_slew_data03.py`, `mount_slew_data03_physical.py`, `analyze_mount_slew_data03_physical.py` | Scripts de una campaña puntual de montura (agosto). Dos de ellos mueven la montura. Nada del producto los importa ni los cita. |
| `antenna_coupling_test.py`, `indoor_antenna_coupling_check.py`, `indoor_coupling_stage.py`, `indoor_coupling_finalize.py`, `rf_chain_isolation_02.py`, `rf_chain_isolation_02_analysis.py`, `sdr_config_fix_validation.py` | Scripts de investigación de la cadena RF (agosto). Sin tests, y nada del producto los importa ni los cita. |
| `=.9` | Archivo vacío del 2026-08-26, creado por error. |
| `~/.config/almita/web_auth.json` | Credenciales de la web. Fuera del repo por diseño. |
| Ramas `backup-antes-de-limpiar-github-20260810` y `respaldo-local-18-03` | Historia anterior a la limpieza de GitHub. |

Qué hacer con los scripts y los resguardos es BL-042.

Revisado otra vez el 2026-10-13: la lista de archivos sin versionar es la misma. Ninguno de esos scripts contiene
credenciales. Siete de los diez llevan escrita la frecuencia `1420405752`. La pasada autónoma no los tocó: son
scripts personales, siguen fuera de Git y la regla de BL-051 solo mira los versionados.

---

## 7. Manual cortapalos

- `docs/MANUAL_CORTAPALOS.pdf`, con fuente editable en `docs/MANUAL_CORTAPALOS.md`.
- Se regenera con `/usr/bin/python3 scripts/build_manual_pdf.py`, que usa el `markdown` del sistema y chromium
  headless.
- Está enlazado al inicio de `FIELD_RUNBOOK.md`.
- Revisado el 2026-10-12 contra el código, la web y `data/`. Cambios:
  - la versión del encabezado, que citaba `67df4a6` y un servidor arrancado a las 00:41;
  - la altura mínima por defecto: la web rellena 30°, no 10°;
  - el Bias-T de RFI REF: evidencia de las seis sesiones, y el aviso sobre las especificaciones YAML;
  - las sesiones de 25 puntos del 2026-10-08.
- Revisado de nuevo el 2026-10-13. Las referencias de versión seguían vigentes (`b6eca65` es el último commit
  con código de producto). Cambios: el aviso sobre el plan de forensics (BL-052), la nota sobre el filtro notch
  FM de la antena B (BL-053) y la fecha de revisión. PDF regenerado.
- **§14, «Pruebas de validación para el operador»** (2026-10-13): STOP OBSERVATION, STOP CURRENT STEP, ALIGN
  SOLAR (pendiente, sin Sol) y el recorrido nocturno completo, con botones, requisitos, resultado esperado,
  evidencias y recuperación. Describe el contrato real de los archivos: un `.h5.part` puede quedar si el STOP
  cae justo en la escritura, y el STOP no aborta un GOTO en curso. La configuración de la prueba D se pasó por
  PLAN ese día, en un directorio temporal, y fue aceptada. PDF de 12 páginas, revisado a la vista.
- Actualizado en la pasada autónoma del 2026-10-13: el párrafo del Bias-T de RFI REF (ahora encendido por
  defecto también en las especificaciones YAML), una fila nueva en «Problemas habituales» para la cámara y la
  versión del encabezado. PDF regenerado.
- **Antena B:** desde el 2026-10-13 coinciden el manual, este brief, la web (HTML, JS y
  `/api/observe/defaults`), el esquema de especificaciones, los tres YAML de `examples/` y el plan de forensics:
  LNA alimentado por el Bias-T del SDR 00000002, encendido por defecto, con `false` explícito disponible. La
  última sesión real (2026-10-08 22:35) lanzó `rtl_tcp -d 00000002 … -g 25.0 -T` y recibió el ACK de sintonía.
  Quedan fuera:
  - `capture.py` por CLI directa, que solo lo enciende con `--rfi-ref-bias-t` (módulo congelado);
  - las guías de hardware de `main`, que no mencionan el Bias-T de RFI REF (BL-053);
  - la medición de que el LNA recibe alimentación (BL-031).
- Sigue siendo un primer borrador: nadie hizo el recorrido completo siguiéndolo, y falta el procedimiento de Ekos.
