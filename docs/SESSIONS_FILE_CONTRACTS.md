# SESIONES: contratos reales de los archivos (BL-080)

Estudio hecho el 2026-10-21 en solo lectura sobre el `data/` del Pi (55 campañas, 16 REDUCE, 20 carpetas en
`data/science/`). No se escribió nada en `data/`. Es la base de `session_catalog.py` y de la página SESSIONS
(BL-079 a BL-090). Cuando un formato cambie, este documento y `tests/session_fixtures.py` cambian con él.

## 1. Familias

| familia | dónde | identidad | la usa SESSIONS |
|---|---|---|---|
| Campaña OBSERVE | `data/mosaic/<campaign_id>/` | `campaign_id` = nombre de la carpeta (`session_identity.py`, BL-050) | sí: una fila del catálogo por carpeta |
| Corrida REDUCE | `data/reduced/<nombre de observación>/REDUCE-<fecha>-<n>/` | nombre de la carpeta | sí: vinculada a su campaña |
| Corrida SCIENCE WEB | `data/science/<nombre de observación>/SCIENCE_WEB-<fecha>-<n>/` | nombre de la carpeta | sí: vinculada a su REDUCE |
| Análisis manual | `data/science/<...>/HI_MANUAL-*` y similares | nombre de la carpeta | sí, como **pendiente de asociar** |
| Perfil de calibración | `data/calibration/WIZARD-*/observe_profile/calibration_profile_v1.json` (+ `.npz`, + `.INVALIDATED.json`) | ruta | sí: se muestra, no se suma al tamaño de ninguna sesión |
| Registro de temperaturas | `data/runtime/temperature/YYYYMMDD.jsonl` | fecha | sí: por ventana de tiempo, con su cobertura |
| ALIGN, LOBE, bancos, pruebas de hardware | `data/alignment/`, `data/lobe/`, `data/bench/`, `data/hardware_tests/` | — | no en esta versión |
| Sesión preservada | `/home/stellarmate/almita_preserve_20261002/` (fuera del repositorio) | — | no en esta versión |

## 2. Campaña OBSERVE (`data/mosaic/<campaign_id>/`)

| archivo | presente en | qué aporta | notas |
|---|---|---|---|
| `observation_resolved.json` | 55/55 | `observation_name`, `planning_timestamp_utc`, `requested` (lo pedido), `main`/`rfi_ref`/`resolved` (lo efectivo), `quicklook.calibration_profile_path`, `observer_config` copiado | una campaña puede tenerlo corrupto: se informa `UNREADABLE` |
| `grid_metadata.json` | 55/55 | `session_name`, `session_id` (= sufijo de la carpeta), `grid.total_points` | |
| `mosaic.csv` | 55/55 | una fila por punto: `capture_status`, `start_time`/`end_time` (UTC), `grid_row`/`grid_col`, alt/az, errores | el estado de cada punto lo **declara** el orquestador; el catálogo no abre los HDF5 |
| `data/iq/<iq_session_dir>/` | 31/55 | las capturas `.h5` y `session_identity.json` | **las 31 campañas con carpeta de captura tienen `session_identity.json`**; una tiene `data/iq/` vacío (2026-10-17 07:02, sin capturas) y las 23 restantes no tienen `data/iq/`: son planes que nunca capturaron |
| `capture_timing_<YYYYMMDD_HHMMSS>.csv` | las que capturaron | el `session_id` de `capture.py` (identificador de captura) | |
| `stop_report.json` | 8 | parada del operador: fase, punto interrumpido, parada de la montura | desde `3bfeb98` |
| `orchestrator_capture.log` | 32/55 | la línea `Mount  LX200 OnStep` / `Mount  Telescope Simulator` | la campaña del 2026-10-17 07:02 tiene log pero ninguna captura: la pausó la regla de seguridad de la montura (estado NO_CAPTURE) |
| `quicklook/` | las que capturaron | PNG guardados, `session_waterfall_state.json` (fuente del stack 3D), `points/<n>/` | vistas anteriores a BL-063/BL-067 conservan su aspecto viejo |
| `observer_config.json`, `rfi_ref/`, `gain_pilot_state.json` | algunas | contexto | `observer_config` → `hardware.sdr_device` es una etiqueta fija (ver decisión 4) |

`observation_runtime.json` no se usa: queda en RUNNING por diseño (BL-016).

## 3. Corrida REDUCE

| archivo | presente en | qué aporta |
|---|---|---|
| `manifest.json` | 13/16 | `status` (COMPLETED/PARTIAL), conteos de puntos, `quality_counts`, `velocity_frame_counts`, **`source_campaign_root`** |
| `provenance.json` | 13/16 | **`git_commit`**, `pipeline_version`, `config_hash`, `calibration_profile_hash`, inicio y fin |
| `config.json` | 16/16 | `calibration_profile_path`, `velocity_frame` y el resto de parámetros |
| `logs/events.jsonl` | 16/16 | `REDUCE_CAMPAIGN_BEGIN` … `REDUCE_CAMPAIGN_END` |
| `points/<n>/master_spectrum.{json,h5}` | según avance | espectro guardado; `capture_refs[].source_path` apunta a la captura de la campaña |
| `qc/*.json` | las terminadas | resúmenes de calidad, velocidad, calibración y máscaras |

Las 3 corridas sin `manifest.json` son corridas interrumpidas: tienen `REDUCE_CAMPAIGN_BEGIN` y no tienen `END`.

## 4. Corrida SCIENCE WEB

`provenance.json` (`input_reduce_session_dir`, `input_reduce_manifest_sha256`, `git_commit`, `config_hash`),
`manifest.json`, `config.json`, `points.csv` y `maps/` (PNG/SVG/PDF y `NOTES.md`). Una carpeta real
(`SCIENCE_WEB-20261015-232340-610800`) tiene `provenance.json` y `manifest.json` vacíos o corruptos: queda como
**INTERRUPTED** y **pendiente de asociar**.

## 5. Decisiones técnicas

| # | decisión | respuesta | evidencia |
|---|---|---|---|
| 1 | Zona horaria de los nombres | **Resuelta:** los nombres de carpeta están en **UTC**. El Pi corre en `Etc/UTC` | `planning_timestamp_utc` 22:23:10.928Z = carpeta `via_lactea-20261018-22:23:11`; primer punto 22:24:15+00:00. La página muestra la hora de Chile con `America/Santiago` (horario de verano incluido) |
| 2 | Vínculo REDUCE → campaña | **Resuelta:** `manifest.json → source_campaign_root`. Si no hay manifiesto (corrida interrumpida), `points/*/master_spectrum.json → capture_refs[].source_path`. Si no hay ninguno de los dos: pendiente. La carpeta agrupa por nombre de observación, que **no** identifica la campaña | las 16 corridas reales quedan vinculadas así; ninguna mezcla campañas |
| 3 | Estado de la captura | **Resuelta:** `stop_report.json` → STOPPED; sin carpeta IQ → NOT_RUN (o NO_CAPTURE si hay log); todos los puntos `success`/diferidos → COMPLETED; resto → INCOMPLETE; `mosaic.csv` ilegible → UNKNOWN. Los puntos logrados son los que `mosaic.csv` declara `success` | se dice en la página: «declared by mosaic.csv». La validación de cada HDF5 sigue siendo cosa de REDUCE |
| 4 | Receptor | **Abierta para el operador.** El serial no basta y `observer_config.json` tampoco: el 2026-10-18 decía «RTL-SDR Blog V4», cuando el receptor ya era el V3. El receptor queda «not documented» hasta que el operador lo anote | la página muestra la etiqueta de configuración aparte y advierte que no es una lectura |
| 5 | Condición | **Parcial.** Solo el simulador queda registrado (log del orquestador: 4 campañas del 2026-10-17). La montura real no significa cielo abierto. El resto es UNKNOWN hasta que el operador lo anote | anotar las sesiones viejas (cielo abierto o interior) es trabajo del operador |
| 6 | Tamaño en disco | **Resuelta:** bloques reales (`st_blocks × 512`), cada inodo una vez, sin seguir enlaces simbólicos; la campaña por un lado, sus REDUCE/SCIENCE por otro; los perfiles nunca se suman. Recorrer todo el `data/` real toma 0.6 a 0.9 s y 28 MB | la página escribe el método debajo de la tabla |
| 7 | Índice y anotaciones | **Resuelta (el operador delegó la decisión, 2026-10-21):** `data/sessions/catalog_index.json` (descartable) y `data/sessions/annotations.json` + `annotations_log.jsonl` (no se regeneran). Clave: `campaign_id`. Una anotación cuya carpeta ya no existe se conserva y se lista como huérfana. **Fuera de Git a propósito**: el repositorio es público y las notas pueden traer datos del sitio o personales. Antes de cada cambio se copia el archivo a `data/sessions/backups/` (se guardan las últimas 50) | las copias están en el mismo disco del Pi: no protegen si falla la tarjeta |
| 8 | Qué cuenta como sesión | **Implementada, revisar:** solo las campañas OBSERVE. `TEST-*` se rotula TEST por su nombre y no se oculta. ALIGN, LOBE, bancos, pruebas de hardware y la sesión preservada quedan fuera de esta versión | ampliar es un cambio aparte |
| 9 | Stack 3D de sesiones guardadas | **Resuelta:** `spectral_stack_3d.js` exporta su clase y suma `showDocument(doc)`. SESSIONS crea su propia instancia con el `session_waterfall_state.json` guardado; nunca lee `/runtime/` ni toca MONITOR | test de navegador: ninguna llamada a `/runtime/` |
| 10 | HDF5 | **Resuelta:** el catálogo no abre ningún HDF5. Los atributos de una captura se leen solo con el botón READ CAPTURE ATTRIBUTES (`reduce_single_capture.inspect_capture_metadata`: atributos y forma, nunca las muestras) | test: el visor no carga `h5py` |
| 11 | Varias carpetas IQ | **Sin casos reales:** las 31 campañas con capturas tienen una sola. El visor las lista todas si aparecen | — |
| 12 | Reejecuciones | **Resuelta:** cada REDUCE y cada SCIENCE crea su propia carpeta con fecha; nada se sobrescribe. **REDUCE sí guarda la versión de código** (`provenance.json → git_commit`), igual que SCIENCE. La corrida preferida es una anotación y no toca los resultados | `via_lactea-20261015-20:55:13` tiene 4 REDUCE separadas |

## 6. Asociaciones pendientes en los datos reales (2026-10-21)

- `data/science/ALMITA-OBSERVE/HI_MANUAL-20261018_212734`: análisis manual sin `provenance.json`. Su nombre
  contiene el identificador de captura de `ALMITA-OBSERVE-20261018-21:27:05`, así que aparece como **candidato** en
  esa sesión. Se asocia solo si el operador lo confirma.
- `data/science/via_lactea/SCIENCE_WEB-20261015-232340-610800`: archivos de procedencia vacíos o corruptos, sin
  candidato.

## 7. Borrado para recuperar espacio (BL-093, `session_deletion.py`)

Implementado el 2026-10-21. **Nunca se probó sobre el `data/` real:** solo con fixtures en directorios temporales y
vistas previas (que no escriben nada en las carpetas de datos). La primera eliminación real la hace el operador desde
la página.

**Flujo.** Las casillas solo seleccionan. DELETE PRODUCTS… o DELETE SESSIONS… piden una vista previa al servidor. El
botón rojo, rotulado por el servidor («DELETE 4 SESSIONS — IRREVERSIBLE»), envía el lote con el token de esa vista
previa y `confirm: "DELETE"`. CANCEL no envía nada. No hay papelera: el espacio se libera de verdad.

**Qué pertenece a una sesión** (asociaciones del catálogo, nunca rutas leídas de un manifiesto y recorridas):
- la carpeta `data/mosaic/<campaign_id>/`;
- las REDUCE vinculadas con evidencia cuyos puntos leen **solo** esa campaña (se revisan todos los puntos);
- las SCIENCE WEB con vínculo LINKED a una de esas REDUCE.

**Nunca se borra en este flujo:** perfiles de calibración (compartidos), análisis manuales (pueden tener trabajo
único), SCIENCE con vínculo INCONSISTENT o PENDING, la REDUCE de una SCIENCE que se queda, el registro de
temperaturas, los exportes y `data/sessions/`.

**Productos regenerables.** QUICKLOOK y REDUCE solo se borran si se pueden regenerar ahora: las capturas están y su
perfil se puede cargar (existen `.json` y `.npz` y no hay marcador INVALIDATED). SCIENCE, mientras exista su REDUCE.
Antes de borrar una REDUCE o una SCIENCE se guardan sus `config.json`, `provenance.json` y `manifest.json` en el registro
del lote. El código actual puede diferir del `git_commit` original. En los datos del 2026-10-21, la mayoría de las
sesiones con V4 tienen el perfil INVALIDATED, así que sus QUICKLOOK y REDUCE **no** se pueden borrar como productos
(sí como sesión completa).

**Bloqueos.**
- Cualquier actividad del pipeline bloquea el lote entero: un trabajo web en curso, una observación activa
  (`active_campaign_reason`) o un script del pipeline corriendo fuera de la web.
- La campaña de 400 puntos del 2026-10-02 y todo lo derivado de ella están protegidos por nombre
  (`PRESERVED_CAMPAIGNS`), sin forma de quitar la protección desde la página o la API.
- Una sesión cuyas capturas lee una REDUCE de otra sesión no se puede borrar completa.
- Toda ruta se calcula en el servidor, debe estar dentro de `data/mosaic`, `data/reduced` o `data/science`, sin
  ningún enlace simbólico en el camino. Los enlaces que hay dentro se borran como enlaces, nunca su destino.

**Espacio.** Bloques reales de cada inodo único del lote. Un archivo con enlaces duros fuera del lote no libera nada
y se informa aparte. Es una estimación: el resultado muestra además el cambio real del espacio libre (`statvfs`)
antes y después.

**Carreras.** Un `flock` en `data/runtime/sessions_deletion.lock` (se puede mover con
`ALMITA_SESSIONS_DELETION_LOCK_PATH`) lo comparten el borrado, `almita_web_ops.start()` y el START de OBSERVE. Un
arranque se rechaza mientras corre un borrado, y el borrado espera (hasta 10 s) a que termine un arranque. Al
confirmar se rehace la vista previa con el candado tomado: si cambió algún archivo, la selección o el estado del
pipeline, no se borra nada y se pide una nueva vista previa. Repetir una solicitud ya ejecutada no toca nada. **Límite:**
un proceso lanzado a mano desde la terminal no toma el candado; lo detecta la revisión de procesos al confirmar, pero
no entre esa revisión y el borrado.

**Resultado y auditoría.** Por sesión: DELETED, PARTIAL (con cada archivo que quedó y su error), NOTHING TO DELETE o
BLOCKED. El registro queda en `data/sessions/deletions.jsonl` (una línea por lote) y en
`data/sessions/deletions/<lote>/` (`batch.json` y los parámetros guardados). Una sesión borrada completa sale de
`annotations.json` y queda registrada en `deleted_sessions.json` (nombre, fecha, lote), sin aparecer como fantasma en el
catálogo. Una sesión borrada en parte conserva sus notas. Las copias de `data/sessions/backups/` guardan notas, no
capturas: no sirven para recuperar nada de lo borrado.
