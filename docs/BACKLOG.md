# ALMITA backlog

Pedidos del operador y su estado real. Revisado contra el código, los commits, los servicios instalados y `data/`
el 2026-10-12 y de nuevo el 2026-10-13. Contexto general: [PROJECT_BRIEF.md](PROJECT_BRIEF.md).

Entre las dos revisiones no se cerró ningún pendiente: no hubo commits de producto, sesiones ni reinicios. La
revisión del 13 agrega BL-052 y BL-053 y amplía la evidencia de BL-040.

**Pasada autónoma del 2026-10-13** (commits `7b2eefb`, `4a472ce`, `2017a14`, `bc03cc2`, `e696038`): cierra
BL-041, BL-044, BL-045, BL-046, BL-047, BL-048, BL-049, BL-051, BL-052 y BL-056, corrige el 503 de la cámara en el
software y abre BL-054 y BL-055. Detalle en §1.1. No se usó hardware: ni capturas, ni bench, ni GOTO, ni SYNC. El
servidor web se reinició una vez, a las 15:19:53 UTC, y corre el build `e696038`.

Un pedido tiene cinco niveles de comprobación y ninguno implica el siguiente:

| nivel | significado |
|---|---|
| **impl** | El código está en `web-polish-v1`. |
| **tests** | Hay tests automatizados que lo cubren y pasan. |
| **datos** | Corrido sobre datos reales guardados, sin mover hardware. |
| **hardware** | Hecho en el instrumento real, con evidencia en disco. |
| **desplegado** | El proceso que corre hoy en el Pi ya lo tiene cargado. |

En las tablas, `sí` lleva su evidencia, `no` significa que falta y `—` que no aplica. Un pedido solo se da por
cerrado cuando tiene todos los niveles que le aplican. Las evidencias de hardware anteriores al 2026-10-08 vienen
del backlog anterior; el 2026-10-12 solo se comprobó que sus artefactos siguen en `data/`.

## 1. Pedidos entregados (BL-001 a BL-025)

Todos están implementados y desplegados: el servidor web se reinició el 2026-10-13 a las 15:19 UTC y corre el
build `e696038`, posterior a todos esos commits.

| id | pedido | commits | tests | datos | hardware | estado |
|---|---|---|---|---|---|---|
| BL-001 | CALIBRATE: mapa Alt/Az de las zonas HI del wizard | 156bfbd, 8a66860 | sí | sí, plan real WIZARD-20261002 | no: nadie comparó el mapa con el cielo | cerrado salvo hardware |
| BL-002 | OBSERVE: selector de perfil (explorador del servidor) | 4085ed8, 915f8fa | sí | sí, perfiles reales por HTTP | sí: las sesiones del 2026-10-08 llevan un perfil del servidor | **cerrado**; la subida desde el navegador es BL-038 |
| BL-003 | Indicador de estado centrado | 03d263a | sí, navegador | — | — | **cerrado** |
| BL-004 | Retiro del puerto 8090 | 704b168 | sí | — | sí: nada escucha en :8090 (2026-10-12) | **cerrado** |
| BL-005 | SCIENCE: A, B y C en una proyección, ≤3 GB | cf450de, 4eec7f4, 9c5c8f2 | sí | sí: 400/400, GOOD, pico 1.03 GB | — | **cerrado** |
| BL-006 | SCIENCE: exportes legibles y diagnósticos uniformes | b40e126 | sí | sí | — | **cerrado** |
| BL-007 | Wizard: STOP, ABORT, fallas y pasos interrumpidos | adc2cac | sí | — | parcial: ABORT sí (2026-10-07); STOP CURRENT STEP no | abierto: BL-028 |
| BL-008 | Frecuencia: configuración central + ACK de rtl_tcp | aae14d9 | sí | — | parcial: sí en OBSERVE y wizard; bench `sdr --yes` no | abierto: BL-030 |
| BL-009 | Tests sin dependencia de `data/` | fdc72e9, 8473f0c, 7b2eefb | sí: no leen ni escriben en `data/` (corrida completa del 2026-10-13 con foto de `data/` antes y después) | — | — | **cerrado** (BL-046) |
| BL-010 | Git: `data/` ignorado, fixtures, runbook | ef32d62, dbafb43 | — | — | — | **cerrado** |
| BL-011 | `mount_control.py` no hacía await de `connect()` | 5ee573a | sí | — | no | abierto: BL-030 |
| BL-012 | Incidente del wizard con 50 Ω | 8dfb3ab | sí | — | sí: WIZARD-20261007-010706 DONE | **cerrado** |
| BL-013 | Trabajos por hora de inicio; falla del wizard una sola vez | 547c5b0 | sí | — | — | **cerrado** |
| BL-014 | Cámara de montura rotada 180° y persistente | c9d6316 | sí | — | sí: `mount_camera_config.json` en vivo | **cerrado** |
| BL-015 | REDUCE: perfil en el explorador; nombres iguales distinguibles | 4aae46f | sí | sí | — | **cerrado** |
| BL-016 | OBSERVE: una sesión terminada sola ya no muestra RUNNING | b1f9331 | sí | sí: caso del 2026-10-07 | — | **cerrado** en la web; la etiqueta de `observation_runtime.json` sigue en RUNNING por diseño |
| BL-017 | Gain pilot: las capturas reales daban 404 | f4cd4c5 | sí | — | sí: pilot READY el 2026-10-08 | **cerrado** |
| BL-018 | Gain pilot: RE-PLAN a la ganancia aprobada, sin bucle | ae4242b | sí | — | sí: sesión de 25 puntos a 42.1 dB | **cerrado** |
| BL-019 | MONITOR stack 3D sin exagerar los picos | d2efeac | sí | sí | — | **cerrado** |
| BL-020 | REDUCE y SCIENCE: avance en vivo en el log | 67df4a6 | sí | sí: sesión de 100 puntos | — | **cerrado** |
| BL-021 | Manual cortapalos (PDF) | 71e70d2 y siguientes | — | revisado contra código y `data/` el 2026-10-12 | no: sin recorrido completo | abierto: BL-033 |
| BL-022 | ALIGN: SYNC (Sol y HI) con confianza y offset máximo | 4012330 | sí, montura falsa | — | **no: el SYNC nunca se envió a la montura real** | abierto: BL-026 |
| BL-023 | Gain pilot avisa de ganancia sin perfil; etiqueta topocentric | 8f7ab05 | sí | — | no: ninguna sesión posterior usó el pilot | abierto: BL-032 |
| BL-024 | Brief del proyecto + sincronización de Git | fd8a962 | — | — | — | **cerrado** |
| BL-025 | RFI REF: Bias-T encendido por defecto | b6eca65, 7b2eefb | sí | — | parcial: `-T` enviado en 6 sesiones reales; alimentación del LNA sin medir | abierto: BL-031 (medición). El esquema y el plan de forensics ya coinciden (BL-041, BL-052) |

### 1.1 Cerrado en la pasada autónoma del 2026-10-13

| id | qué se hizo | commit | evidencia | falta |
|---|---|---|---|---|
| BL-051 | `rf_chain_characterization.py`, `rf_gain_control_test.py` y `sun_detectability_test.py` leen frecuencia y muestreo de la configuración central (`sdr_tuning`). Los dos que usan `SDRCapture` sintonizan con `tune_explicitly` y guardan el registro de sintonía; el de socket directo verifica el ACK de `rtl_tcp` antes de escribir cada archivo. Los scripts sin versionar no se tocaron. | 7b2eefb, bc03cc2 | `test_no_frequency_literal_in_operational_python` pasa; los dos scripts entraron a la regla «nunca `configure()` sin evidencia» | Nunca se ejecutaron en hardware después del cambio: la próxima corrida real de cualquiera de los tres es su primera prueba |
| BL-046 | El test de timeout del preflight de ALIGN escribe en `tmp_path`, igual que los tests de las rutas web de ALIGN y CALIBRATE y los del preflight de `capture.py`. | 7b2eefb, e696038 | Corrida completa con foto de `data/` (archivos, tamaños y fechas) antes y después: `data/` idéntico antes y después en las tres suites que lo tocaban (`e696038`). En la corrida completa previa a ese commit no quedó ningún archivo nuevo, pero cambiaron las fechas de `data/alignment`, `data/calibration` y `data/IQ`: los tests de las rutas web de ALIGN y CALIBRATE y los del preflight de `capture.py` creaban y borraban carpetas y archivos de prueba ahí. Ahora trabajan en `tmp_path` | Borrar las tres carpetas ya creadas es BL-055 |
| BL-045 | El pie de página y STATUS dicen «HTTP (no TLS), single-user Basic authentication». El SHA de «build» se resuelve al arrancar el proceso, no en la primera consulta. | 7b2eefb | `test_health_and_version_never_leak_secrets_or_paths` | — . Desplegado y visto en la web autenticada tras el reinicio: pie y `version.json` con el texto nuevo y build `e696038` |
| BL-047 | `quicklook_live.STOP_REQUESTED` se limpia antes de cada test; el test de 30 minutos de la consola tiene 300 s de reloj real para sus ~1000 pedidos. | 7b2eefb | Corrida completa del 2026-10-13: los dos tests pasan dentro de la suite | — |
| BL-048 | `observation_gain_pilot.py status` sin pilot planificado responde «not planned» con código 0, y la web lo clasifica como PASS. Los demás comandos siguen exigiendo un pilot planificado. | 7b2eefb | `tests/test_gain_pilot_status.py` (3 tests) | — . Comprobado en el servidor reiniciado: el trabajo `OBSERVE_GAIN_PILOT_ADMIN-20261013-152020-78c5` terminó con código 0, PASS, «no gain pilot planned for this session» |
| BL-049 | Documentado en `alignment_engine/sync_flow.py` y `targets/hi_reference.py`: el motor bloquea el SYNC solo desde plantillas sintéticas y difiere a propósito de `alignment.py` (que desde 4012330 juzga HI como el Sol). No se cambió ninguna política de SYNC. | 7b2eefb | Solo docstrings | Unificar las dos políticas es una decisión posterior a BL-026; queda anotada en BL-043 |
| BL-041 | `rfi_ref.bias_tee` vale `true` por omisión en el esquema (`observation_spec.py`). Un `false` explícito se respeta. `examples/block-validation-01.yaml` pasó a `true`. | 7b2eefb, 4a472ce | `test_rfi_ref_bias_tee_defaults_on_when_field_omitted`, `test_rfi_ref_bias_tee_explicit_false_is_kept`; API, formulario, RE-PLAN y los tres YAML de `examples/` validan en `true` | Consecuencia buscada: una especificación vieja que no escriba el campo ahora revalida con el Bias-T encendido. `capture.py` por CLI directa (módulo congelado) sigue necesitando `--rfi-ref-bias-t` |
| BL-052 | El plan de forensics lleva `bias_tee: true` y sus tres plantillas de comando llevan `--rfi-ref-bias-t`. El valor de la campaña original queda en `bias_tee_feature_campaign: false`, con nota. | 7b2eefb, 4a472ce, 2017a14 | Guardias actualizadas y documentadas: SHA del plan en `scripts/science_forensics_bench.py` (anterior `8bacb7ab…`, anotado) y pins de `science_forensics` y `observation_spec.py` en `scripts/science_forensics_field.py` (anteriores `02b8e5a` y `26fa261`, anotados). `test_frozen_surface_unchanged_against_pins` pasa | Es la única diferencia con la campaña de septiembre y solo afecta a RFI REF (otro SDR). Para volver atrás: `false` en el YAML y en `plan.py`, y restaurar las guardias anotadas |
| BL-050 (parte) | `mount_control.py` acepta la declinación negativa en sexagesimal (`-33:27:00`). `docs/history/FLUJO_COMPLETO.md` lleva un aviso de documento histórico. | 7b2eefb | `tests/test_mount_control_cli.py` (6 casos nuevos) | El resto de BL-050 sigue abierto (§2.4) |
| Cámara, 503 | Ver BL-054. El software respondía 503 con un reintento todavía pendiente; corregido. | 7b2eefb | `tests/test_mount_camera_relay.py` (3 tests) | Desplegado. Tras el reinicio el stream respondió 200 con 2.1 MB en 6 s. La causa del lado de la cámara es BL-054 |

| BL-044 | `almita-observe-api` reiniciado una vez, el 2026-10-13 a las 15:19:53 UTC, después de comprobar en un paso aparte que no había trabajos ni observaciones. No se reinició nada más. | — | PID 711914, build `e696038`; `rtl_tcp`, watcher, blackbox e `indiserver` conservan sus PID y horas de arranque del 2026-10-08. Con las credenciales del operador: las ocho páginas y `common.js` responden 200, `/api/system/health` UP, `/api/observe/defaults` con `rfi_ref.bias_tee: true`, contraseña equivocada 401, journal sin errores | — |
| BL-056 | `hw_hi_night_scan.py` terminaba con una traza cuando ningún objetivo HI era utilizable a esa hora; ahora termina en BLOCKED con su evidencia, y todas las salidas BLOCKED quedan escritas en la bitácora de la sesión. Encontrado por la corrida completa (2 tests fallaron a las 14:40 UTC; a las 23:10 del día anterior pasaban). | e696038 | `tests/test_hw_hi_night_scan.py`: 20 passed, con un test nuevo que fuerza el caso | Es un script de prueba en hardware; no se ejecutó contra la montura |

Además: `rf_chain_characterization.py` reventaba al escribir su CSV (campos no escalares en los registros), después
de terminar las capturas. Corregido en `bc03cc2`; comprobado solo con un caso mínimo, no en hardware.

## 2. Abierto

Prioridades: **P0** riesgo activo, se decide ya. **P1** bloquea confiar en el producto en terreno. **P2** mejora
la operación. **P3** limpieza.

«Quién»: **operador** necesita a alguien junto al instrumento o una decisión; **autónomo** se puede hacer sin
hardware y sin consultar.

### 2.1 Bugs

Los cinco bugs de esta lista (BL-045, BL-046, BL-047, BL-048 y BL-051) se cerraron el 2026-10-13: ver §1.1.

| id | prioridad | qué | evidencia | quién | falta para cerrar |
|---|---|---|---|---|---|
| BL-054 | P2 | **Cámara de montura: 503 intermitente.** La parte de software está corregida; la del equipo no se puede comprobar desde el Pi. | El journal del servidor tiene 3 respuestas 503 y 3 respuestas 200 de `/mount_camera/stream` desde el 2026-10-08, con un 200 un minuto después de un 503. La cámara es una ESP32-CAM por WiFi (`192.168.1.164:81`, un solo cliente a la vez); el 2026-10-13 respondía ping (4 a 42 ms) y tenía abiertos los puertos 80 y 81. El servidor no registraba el motivo de cada 503, así que la causa de esos tres no se puede reconstruir. Defecto encontrado: el relay marcaba ERROR en cada intento fallido y respondía 503 aunque quedara un reintento; además nunca soltaba a los espectadores cuando se rendía. Corregido en 7b2eefb, y ahora el motivo queda en el journal. No se abrió el stream de la cámara para probar, porque le habría quitado la imagen a quien la estuviera mirando. | operador | Después del reinicio (BL-044): si vuelve a aparecer «STREAM UNAVAILABLE», mirar LAST ERROR en MONITOR o `journalctl -u almita-observe-api \| grep "mount camera"` y anotar el mensaje. Con eso se distingue WiFi débil, cámara colgada u otro cliente ocupando el stream |

### 2.2 Pruebas pendientes en hardware

Todas necesitan al operador con la montura y la antena en posición. BL-026 a BL-030 caben en una misma salida.

| id | prioridad | prueba | cierra | evidencia que falta |
|---|---|---|---|---|
| BL-026 | P1 | ALIGN SOLAR desde la web: primero sin SYNC, después con SYNC, y un ALIGN de confirmación. | BL-022 | `alignment_result.json` con offset y confianza; SYNC APPLIED; segundo ALIGN con offset cercano a 0. Hoy no hay ninguna corrida solar real: las dos carpetas `SOLAR-*` son restos de tests (BL-046). |
| BL-027 | P1 | STOP OBSERVATION real con una grilla corta. | — | Estado `ABORTED`, `.h5` completos, ningún `.part` |
| BL-028 | P2 | STOP CURRENT STEP del wizard durante una captura real. | BL-007 | Captura detenida, archivos conservados, wizard reanudable |
| BL-029 | P2 | Fallas provocadas: desconectar el USB de MAIN; apagar INDI. | — | BLOCK visible en PLAN y STATUS, y recuperación documentada |
| BL-030 | P3 | Bench `sdr --yes` en MAIN y la CLI de `mount_control.py`. | BL-008, BL-011 | Salida del bench con ACK; un GOTO por CLI |
| BL-031 | P1 | Antena B: medir que el LNA recibe alimentación con el Bias-T de RFI REF. | BL-025 | Nivel de ruido de RFI REF con BIAS-T marcado y desmarcado, o tensión en el conector. No se puede sacar de los datos guardados: las seis sesiones con RFI REF usaron `-T` y no hay ninguna sin él. No mueve la montura. |
| BL-032 | P2 | Ver el aviso del gain pilot en una sesión real: aprobar una ganancia sin perfil. | BL-023 | Captura de pantalla o `gain_pilot_state.json` de esa sesión |
| BL-033 | P1 | Recorrido completo siguiendo el manual, y dejar escrito el procedimiento de Ekos (alineación, hora, sitio). | BL-021 | Manual anotado donde se aparte de la realidad |
| BL-034 | P2 | Medir el haz (FWHM). Hoy es provisional: 20° en la configuración y 14° en `alignment.py`. | — | Barrido del Sol; afecta la separación de la grilla, ALIGN y SCIENCE. Depende de BL-026. |

### 2.3 Decisiones del operador

| id | prioridad | decisión | contexto | opción propuesta |
|---|---|---|---|---|
| BL-040 | P2 | **La web :8088 es alcanzable desde Internet.** Decidido: el operador indicó el 2026-10-13 que el acceso por Internet funciona como él quiere. No se cambió router, reenvíos, firewall, autenticación ni acceso remoto. Quedan comprobaciones. | Del 2026-10-10 al 12 el journal registra 57 direcciones públicas distintas; 76 pedidos de escáneres rechazados con 401 y una sesión autenticada desde una IP pública el 2026-10-11 16:55 UTC. El 2026-10-13 siguen llegando escáneres (10 direcciones más, todas rechazadas con 401 o 400). La contraseña viaja por HTTP sin cifrar (Basic Auth) y esa web puede mover la montura. El Pi también escucha en todas las interfaces en 22, 80, 445, 5900, 6080 y 7624 (INDI: sin autenticación y mueve la montura); desde el Pi no se puede saber cuáles reenvía el router. | Comprobaciones pendientes, todas del operador: (1) confirmar que la sesión del 2026-10-11 fue suya; (2) mirar en el router qué puertos se reenvían además de :8088, sobre todo 7624; (3) decidir si la contraseña sin cifrar es aceptable o si conviene una VPN |
| BL-035 | P1 | Unir `web-polish-v1` con `main`. | `main` tiene 19 commits de documentación y sitio que la rama no tiene; la rama tiene más de 80 que `main` no tiene. Solo chocan en `README.md`. | Un PR `web-polish-v1` → `main` resolviendo el README, después de BL-026 y BL-027 |
| BL-053 | P3 | Cadena de la antena B: la documentación no coincide. | Las guías de `main` (`docs/hardware/RF_AND_WIRING.md`, `BUILD.md`, `BOM.md`) dicen antena → filtro notch FM (Nooelec Flamingo FM) → LNA de banda ancha → SDR, y no dicen que el LNA se alimenta por Bias-T. El manual y el brief dicen antena B → LNA → SDR, sin filtro. No se puede comprobar sin mirar el equipo. Encontrado el 2026-10-13. | Que el operador confirme la cadena real; corregir el manual y el brief en esta rama y las guías al unir con `main` (BL-035) |
| BL-055 | P3 | Borrar los restos de tests en `data/alignment/`: `SOLAR-20261006-010009`, `SOLAR-20261008-191903` y `SOLAR-20261012-231035`. | Las creó el test corregido en BL-046; están en `PREFLIGHT_FAILED` y no contienen capturas. No se borraron porque están en `data/`. | Borrarlas: confunden, porque parecen corridas solares y no hay ninguna |
| BL-036 | P3 | REDUCE topocentric sin metadatos. | Exige descongelar `reduce_engine`. Hoy solo se corrigió la etiqueta. | No hacerlo mientras SCIENCE solo acepte LSRK |
| BL-037 | P2 | RFI REF: asociar las capturas de la antena B a cada punto en REDUCE. | La interfaz existe (`docs/REDUCE_RFI_REF_INTERFACE.md`) y hoy informa UNAVAILABLE. Toca REDUCE V1 congelado. | Decidir después de BL-031: no tiene sentido sin saber que la antena B mide bien |
| BL-038 | P3 | Subir un perfil desde el disco del navegador (resto de BL-002). | Hay que decidir dónde se guarda y quién puede escribir. | Posponer |
| BL-039 | P3 | Modelo de horizonte. | CALIBRATE muestra «OBSTACLES NOT EVALUATED». Necesita datos del sitio. | Posponer hasta tener sitio fijo |
| BL-042 | P3 | Archivos locales fuera de Git: scripts de investigación de agosto, respaldos `*_ultima_revision.tar.gz`, el archivo vacío `=.9`, `/home/stellarmate/almita_preserve_20261002/` (6.4 GB) y las ramas `backup-antes-de-limpiar-github-20260810` y `respaldo-local-18-03`. | Lista y motivo de cada uno en PROJECT_BRIEF §6. Nada se borró ni se subió. | Mover los scripts a `scripts/legacy/` si se quieren conservar en Git; el resto queda local hasta que el operador diga |

### 2.4 Mejoras

| id | prioridad | mejora | quién | depende de |
|---|---|---|---|---|
| BL-043 | P2 | Sección ALIGN de PIPELINE sin opción de SYNC: siempre usa `--no-sync`. Junto con esto, decidir si `alignment_engine` adopta la política de SYNC de `alignment.py` (BL-049). | autónomo | BL-026, para no exponer un SYNC sin probar. **No se habilita antes.** |
| BL-050 | P3 | Lo que queda de FIELD_RUNBOOK §14: identidad canónica de la sesión; lock del proceso INDI; `gain='auto'` como default en `sdr_capture.configure`; lectores DS18B20 duplicados; `README.md` desactualizado. | Los tres primeros tocan módulos congelados (`capture.py`, `indi_telescope_control.py`, `sdr_capture.py`): decisión del operador. El README conviene resolverlo con BL-035, porque es el único archivo que choca con `main`. Los lectores DS18B20 ya comparten el rango de plausibilidad; unirlos es limpieza sin efecto visible | — |

## 3. Orden propuesto

Todo lo que se podía hacer sin hardware ni decisiones está hecho. Lo que sigue necesita al operador.

1. **Salida a terreno de día:** BL-031 (sin mover la montura) → BL-026 (ALIGN SOLAR y SYNC) → BL-027 (STOP real)
   → BL-029. Hacerla siguiendo el manual cubre BL-033. Con el equipo a la vista, confirmar la cadena de la
   antena B (BL-053) y anotar el mensaje de la cámara si vuelve a fallar (BL-054).
2. **Con el Sol ya medido:** BL-034 (haz). Después BL-043, que incluye decidir la política de SYNC del motor.
3. **Noche siguiente:** BL-028 y BL-032 dentro de una sesión normal.
4. **BL-035** (PR a `main`) cuando BL-026 y BL-027 estén cerrados; lleva consigo el README (BL-050) y las guías
   de hardware (BL-053).
5. **Decisiones sueltas, sin fecha:** las tres comprobaciones de BL-040, BL-055 (borrar restos de tests), BL-042,
   BL-036 a BL-039 y lo que queda de BL-050.

Dependencias que fijan el orden:

- BL-026 antes de BL-034 y de BL-043.
- BL-031 antes de BL-037.
- BL-053 antes de BL-035, para corregir las guías de hardware en el mismo PR.
- BL-026 y BL-027 antes de BL-035.

## 4. Activación

- `console/` se sirve en vivo.
- Los cambios de Python que importa el servidor necesitan reiniciar `almita-observe-api`, y solo sin trabajos
  activos (revisarlo en un paso aparte).
- Los scripts que lanza la web se cargan en cada trabajo.

## 5. Tests

- **Corrida completa vigente, 2026-10-13** (14:29 a 15:05 UTC, 36 min 2 s, sobre `bc03cc2`): 2555 passed,
  2 failed, 52 skipped. Las dos fallas eran BL-056 (dependen de la hora) y se corrigieron después, en `e696038`.
  Los tres tests que fallaban el 2026-10-12 (BL-051 y BL-047) pasan.
- **Después de esa corrida** cambiaron `hw_hi_night_scan.py` y tres archivos de tests (`e696038`). Se corrieron
  solo sus suites: `test_hw_hi_night_scan.py` 20 passed; `test_capture_preflight.py`,
  `test_almita_web_align_calibrate.py` y `test_alignment_engine_end_to_end.py` 51 passed. No se repitió la
  corrida completa.
- Corrida completa anterior, 2026-10-12 sobre b6eca65: 2540 passed, 3 failed, 52 skipped.

Detalle en [PROJECT_BRIEF.md §5](PROJECT_BRIEF.md#5-validación).
