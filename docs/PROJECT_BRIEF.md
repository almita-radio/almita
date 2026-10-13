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

- **La web es alcanzable desde Internet** con contraseña por HTTP sin cifrar (BL-040). Es una decisión del
  operador y es lo único urgente.
- **ALIGN no corrige todavía:** el SYNC nunca se envió a la montura real y ALIGN SOLAR nunca corrió (BL-026). La
  única corrida real, con HI, no logró estimar un offset.
- **STOP OBSERVATION nunca se probó** en una sesión real (BL-027).
- **El haz no está medido** (BL-034): 20° en la configuración y 14° en `alignment.py`, ambos provisionales.
- **Antena B:** el Bias-T se envía, pero nadie midió que el LNA reciba alimentación (BL-031). Además, el plan
  del experimento de forensics lo lleva apagado (BL-052).

Ninguno impide observar como se observó el 2026-10-08.

**Sin actividad desde el 2026-10-08.** Entre la revisión del 2026-10-12 y la del 2026-10-13 no hubo commits de
producto, sesiones, trabajos web ni reinicios. Lo único nuevo en `data/` son los restos de la corrida de tests
del 12 y el latido del sistema.

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
| **Gain pilot** (1 o 2 capturas reales para elegir ganancia; RE-PLAN) | sí | — | sí: 2026-10-08, READY a 42.1 dB y RE-PLAN usado. | El aviso de ganancia sin perfil (BL-023) no se ha visto en una sesión real (BL-032). |
| **OBSERVE** (PLAN, PREFLIGHT, START, STOP) | sí | — | sí: 400 puntos (10-02), 676 (10-07), 100 y tres de 25 (10-08). Todas COMPLETED, 0 fallidos en las dos últimas. | **STOP real sin probar** (BL-027). Fallas provocadas sin probar (BL-029). |
| **QUICKLOOK** (espectro, cascada, mapa, grilla nativa, vista interpolada) | sí | sí | sí: en vivo en todas las sesiones con perfil compatible; la última procesó 25/25. | Se bloquea si el perfil no coincide en ganancia. Es vista previa, no producto científico. |
| **RFI REF** (antena B, SDR 00000002, `rtl_tcp` en :1235) | sí | — | **parcial.** Seis sesiones reales con ACK de sintonía y `-T` en el log de lanzamiento. Alimentación del LNA sin medir. | BL-031. REDUCE no lo usa (BL-037). El esquema de la CLI deja el Bias-T apagado por omisión (BL-041) y el plan de forensics lo fija en `false` (BL-052). |
| **MONITOR** (sesión, quicklook, RFI, stack 3D, cámara, log) | sí: navegador headless | sí: stack 3D con datos reales | sí: usado en vivo. | El stream de la cámara respondió 503 el 2026-10-12; causa no investigada. |
| **REDUCE** (nivel 0 → nivel 1, RELATIVE, LSRK) | sí | sí: cinco reducciones, la última el 2026-10-08 sobre la sesión de 100 puntos | — | Motor congelado. Topocentric exige los mismos metadatos que LSRK (BL-036). Las dos sesiones del 10-08 por la tarde no están reducidas. |
| **SCIENCE** (mapas A, B y C; LOO; exportes) | sí | sí: 400/400 puntos, calidad GOOD, pico de 1.03 GB; y la sesión de 100 puntos | — | Motor congelado. B y C interpolan: más píxeles no es más resolución. Depende del haz provisional. |
| **Servicios** (cuatro unidades systemd) | sí | — | sí: activas y habilitadas; arrancaron solas tras el reinicio del 2026-10-08. Unidades instaladas idénticas a `systemd/`. | El servidor tiene cargado un archivo anterior a HEAD (§4, BL-044). Pie de página con texto falso (BL-045). |
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
| 2026-10-13 | Segunda revisión de estado. Sin cambios de código. Dos hallazgos nuevos sobre la antena B (BL-052, BL-053) y el aviso del manual ampliado. | Commit de documentación |

No hubo ALIGN, CALIBRATE, gain pilot, REDUCE ni SCIENCE después de fd8a962.

**b6eca65 — Bias-T de RFI REF**
- `/api/observe/defaults` devuelve `rfi_ref.bias_tee: true` y el formulario lo sigue y lo conserva al RE-PLAN.
- El esquema (`observation_spec.py`) mantiene `false` por omisión, para que los planes archivados revaliden igual.
- La parte del servidor no está cargada todavía (§4).

Hallazgos de esta revisión, todos en el backlog:
- la web recibe tráfico de Internet (BL-040);
- un test escribe en el `data/` real (BL-046);
- el manual daba 10° como altura mínima por defecto y la web rellena 30° (corregido);
- las carpetas `data/alignment/SOLAR-*` no son corridas solares: son restos de tests.

Hallazgos de la revisión del 2026-10-13:
- `examples/science_forensics_experiment.yaml` tiene RFI REF activado con `bias_tee: false`. Copia la
  configuración de la campaña de septiembre, anterior a la confirmación del LNA de la antena B (BL-052);
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

Comprobado el 2026-10-12 y otra vez el 2026-10-13 a las 12:35 UTC, sin reiniciar ni desplegar nada. El 13 todo
seguía igual: mismos PID, mismas horas de arranque y `NRestarts=0`.

### Qué ejecuta cada servicio

Los cuatro servicios arrancaron con el reinicio del Pi, el 2026-10-08 a las 20:50:50 UTC, y no se reiniciaron
desde entonces (`NRestarts=0`). Las unidades instaladas en `/etc/systemd/system/` son idénticas a las de
`systemd/` del repo.

| servicio | PID | código que tiene cargado | diferencia con HEAD (el código de producto de HEAD es el de b6eca65) |
|---|---|---|---|
| `almita-observe-api` (:8088) | 825 | `almita_orchestrator_server.py` como estaba en **fd8a962**: el archivo se editó a las 22:28, después del arranque. El resto de los módulos que importa no cambió desde antes del arranque. `console/` se sirve en vivo. | Una: `/api/observe/defaults` no devuelve `rfi_ref.bias_tee`. Sin efecto visible, porque la casilla ya viene marcada en el HTML. |
| `almita-console-watcher` | 824 | `almita_console_watcher.py`, sin cambios desde el 2026-10-06 | Ninguna |
| `almita-system-blackbox` | 679 | `almita_system_blackbox.py`, sin cambios desde el 2026-10-05 | Ninguna |
| `rtl_tcp` | 848 | Binario del sistema: `rtl_tcp -d 00000001 -a 127.0.0.1 -p 1234 -f 1420405752 -s 2400000 -g 40.2 -T` | Ninguna; coincide con `observer_config.json` |

- Los scripts que lanza la web (`alignment.py`, `capture.py`, `reduce_campaign_run.py`, `science_web_bridge.py`,
  …) se cargan en cada trabajo, así que corren el código del árbol de trabajo.
- Fuera de systemd: `indiserver` en :7624, levantado por Ekos el 2026-10-08 a las 20:58. `almita-console-web`
  (la web vieja) está deshabilitada.
- Sin trabajos ni capturas activos el 2026-10-13: no hay procesos `capture.py`, `quicklook_live.py`,
  `alignment.py`, wizard, REDUCE ni SCIENCE.
- **No verificado:** el SHA que muestra el pie de página de la web. Exige iniciar sesión, y esta revisión no usó
  las credenciales. El servidor lo lee de Git la primera vez que se consulta, así que puede decir `fd8a962` o
  `b6eca65` según cuándo fue esa primera consulta.

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

### Ramas

- **Repositorio:** `git@github.com:almita-radio/almita.git` (https://github.com/almita-radio/almita).
- **Rama de trabajo:** `web-polish-v1`. Antes de esta revisión, local y `origin/web-polish-v1` coincidían en
  `7fe9c56d480b78c529e5b22ac667adfc7bfc3084` (comprobado con `git fetch` el 2026-10-13). El SHA publicado con
  esta revisión se informa al entregarla.
- **`main`** (`origin/main` = `9161fe8`, igual que el `main` local): base común `70b4a5c`.
  - `web-polish-v1` tiene todo el producto actual: 84 commits que `main` no tenía el 2026-10-13, antes del commit
    de esta revisión.
  - `main` tiene 19 commits de documentación y sitio (licencias open hardware, `docs/hardware/` con BOM, cableado
    y mecánica, sitio web) que esta rama no tiene.
  - Solo chocan en `README.md`. No se hizo merge: es una decisión del operador (BL-035).
- **Ramas locales que no se suben a propósito:** `backup-antes-de-limpiar-github-20260810` y
  `respaldo-local-18-03`. Tienen historia anterior a la limpieza de GitHub y subirlas la desharía.
- Otras ramas locales (`backlog/wizard-preview-profile-picker`, `science/abc-shared-projection-wip`,
  `web/backlog-integration`, `science-forensics-v1`): todas son ancestros de `web-polish-v1`, así que no tienen
  nada que la rama de trabajo no tenga.
- **Servidor frente a ramas:** el proceso de :8088 tiene el Python de `fd8a962`; la rama, el remoto y el árbol de
  trabajo tienen el de `b6eca65` más documentación. `main` no tiene nada del producto actual.

---

## 5. Validación

### Tests automatizados

Los resultados vigentes son los del 2026-10-12. Desde entonces solo cambió documentación (y `.gitignore`), así
que siguen describiendo el código de HEAD.

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

El 2026-10-12 se comprobó que la sesión de 400 puntos (400 HDF5, 6.1 GB), su perfil y el resguardo
`almita_preserve_20261002` siguen intactos: mismos archivos, tamaños y fechas antes y después de esta revisión.
Comprobado otra vez el 2026-10-13, antes y después de la revisión: 400 HDF5 y 6.1 GB en la sesión, 37 MB en el
perfil `WIZARD-20261002-013321-160933`, 3260 archivos y 6.4 GB en el resguardo, y ningún archivo modificado
después del 2026-10-03 en ninguno de los tres.

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
- El pie de página dice «LAN, no authentication». Las dos cosas son falsas hoy.

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

Revisado otra vez el 2026-10-13: la lista de archivos sin versionar es la misma y el árbol no tiene cambios
locales en archivos versionados. Ninguno de esos scripts contiene credenciales. Siete de los diez llevan escrita
la frecuencia `1420405752`, así que versionarlos tal cual sumaría casos a BL-051. Siguen fuera de Git: no
pertenecen al producto.

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
- **Antena B:** el manual, este brief, la web (HTML, JS y `/api/observe/defaults` en el árbol de trabajo) y
  `examples/overnight-400.yaml` coinciden: LNA alimentado por el Bias-T del SDR 00000002, encendido por defecto.
  La última sesión real (2026-10-08 22:35) lanzó `rtl_tcp -d 00000002 … -g 25.0 -T` y recibió el ACK de sintonía.
  No coinciden:
  - el valor por omisión del esquema, `false` (BL-041);
  - el plan de forensics, `false` explícito con RFI REF activado (BL-052);
  - `examples/block-validation-01.yaml`, `false` pero con RFI REF desactivado, así que no tiene efecto;
  - las guías de hardware de `main`, que no mencionan el Bias-T de RFI REF (BL-053).
- Sigue siendo un primer borrador: nadie hizo el recorrido completo siguiéndolo, y falta el procedimiento de Ekos.
