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
| BL-001 | CALIBRATE: mapa Alt/Az de las zonas HI del wizard | 156bfbd, 8a66860 | sí | sí, plan real WIZARD-20261002 | parcial: el 2026-10-14 (`WIZARD-20261014-001224-467117`) el operador vio que la montura fue a HI ALTO y a HI BAJO en la dirección que muestra el mapa. **Fue bajo techo:** comprueba que la montura apunta hacia donde dice el mapa, no que ahí esté el cielo esperado | abierto: falta verlo a cielo abierto |
| BL-002 | OBSERVE: selector de perfil (explorador del servidor) | 4085ed8, 915f8fa | sí | sí, perfiles reales por HTTP | sí: las sesiones del 2026-10-08 llevan un perfil del servidor | **cerrado**; la subida desde el navegador es BL-038 |
| BL-003 | Indicador de estado centrado | 03d263a | sí, navegador | — | — | **cerrado** |
| BL-004 | Retiro del puerto 8090 | 704b168 | sí | — | sí: nada escucha en :8090 (2026-10-12) | **cerrado** |
| BL-005 | SCIENCE: A, B y C en una proyección, ≤3 GB | cf450de, 4eec7f4, 9c5c8f2 | sí | sí: 400/400, GOOD, pico 1.03 GB | — | **cerrado** |
| BL-006 | SCIENCE: exportes legibles y diagnósticos uniformes | b40e126 | sí | sí | — | **cerrado** |
| BL-007 | Wizard: STOP, ABORT, fallas y pasos interrumpidos | adc2cac | sí | — | sí: ABORT (2026-10-07) y STOP CURRENT STEP en 50 Ω y en HI, durante GOTO y durante captura (2026-10-13) | **cerrado** (BL-028) |
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
| BL-059 | **P1** | **Tras un STOP durante un GOTO, la montura terminaba el movimiento y quedaba con el seguimiento ENCENDIDO.** Corregido en origen el 2026-10-14 (`3bfeb98`), **sin probar todavía en la montura**. | Causa: la interrupción llegaba a `capture.py` como cancelación de su tarea; la única limpieza era pedir que se apagara el seguimiento mientras la montura todavía se movía, sin abortar ni esperar, y OnStep restauraba el seguimiento al terminar el GOTO. Solución: `mount_stop.py` aborta el movimiento, espera con límite a que termine, apaga el seguimiento, exige que siga apagado en lecturas consecutivas (lo vuelve a apagar si un GOTO tardío lo reactiva) y comprueba con las coordenadas que la montura no se mueve ni sigue. Solo declara parada confirmada con la lectura de INDI; si no, «PARADA NO CONFIRMADA» con el motivo. Excepción autorizada al congelamiento de `capture.py`, limitada a la parada, la limpieza y el anuncio del fin de sesión. Probado con procesos y señales reales contra una montura simulada que sigue moviéndose y siguiendo (§5). | operador | **Repetir en la montura** el STOP durante un GOTO y durante una captura (manual §14, prueba A). Hasta entonces, STOP OBSERVATION durante GOTO sigue contando como fallido en hardware. No está comprobado que el driver real acepte la orden de abortar ni cuánto tarda OnStep en detenerse |
| BL-057 | — | `capture.py` no anunciaba el fin de la sesión cuando se detenía con STOP. **Corregido en origen** (`3bfeb98`): publica `SESSION_ABORTED` con el resultado de la parada de la montura, marca la sesión como pausada y escribe `stop_report.json`. | Probado con procesos y señales reales en `tests/test_capture_stop_integration.py`. La compensación desde afuera (`0148b78`) se conserva como respaldo. | operador | Verlo en la repetición de la prueba A |
| BL-060 | P2 | **El wizard no tiene en cuenta el límite de meridiano de OnStep, y un plan HI aprobado no se puede rehacer.** | 2026-10-13 19:32, `WIZARD-20261013-181623-230083`, trabajo `CALIBRATE_WIZARD_MOVE-20261013-193107-0995`: las 5 capturas de HI ALTO se completaron, pero la comprobación final encontró el seguimiento apagado y el paso falló («target NOT held … tracking off»). OnStep informa «Meridian Limit (W) Exceeded»; su límite es de 12 minutos pasado el meridiano y el objetivo llegó a un ángulo horario de +0.202 h (12.1 min) a las 19:31:59. El plan se había aprobado a las 18:53, cuando el objetivo estaba al este del meridiano. El wizard hizo lo correcto al rechazar el paso. Faltan dos cosas: avisar o evitar objetivos que cruzarán el límite durante la espera, y permitir RE-PROPOSE desde un paso HI ya aprobado (hoy solo se puede antes de aprobar). | autónomo el diseño; hardware para probarlo | Leer el límite de OnStep (`Minutes Past Meridian`) al proponer y al capturar; permitir volver a proponer zonas sin abortar la sesión. **Además:** el error de límite queda anotado en OnStep y ALMITA bloquea todo movimiento mientras siga ahí. Comprobado el 2026-10-13/14: no lo borran ni ir a home, ni un GOTO desde Ekos, ni reiniciar Ekos; **lo borra encender y apagar el seguimiento en Ekos**. Falta que la web lo diga al bloquear |
| BL-062 | — | El SDR de RFI REF (serial 00000002) se desconectó del USB en plena sesión. **Explicado y cerrado:** lo desconectó el operador un momento al ponerle una ferrita. | 2026-10-14 00:32:12 UTC, sesión `ALMITA-OBSERVE-20261014-00:28:48` (bajo techo): «usb 3-1: USB disconnect»; RFI REF quedó en FAILED («Connection reset by peer») y STATUS marcó `rfi_sdr` DOWN. **MAIN no se vio afectado:** la captura siguió y guardó el punto 7 después de la desconexión. | — | Nada. Sirve como evidencia parcial de BL-029: desconectar el SDR de RFI REF en plena sesión no afecta a MAIN y se ve en MONITOR y STATUS |
| BL-065 | P1 | **Botón PARAR MONTURA en todas las páginas.** Hecho el 2026-10-14 (`3bfeb98`), sin probar en la montura. | Independiente del cierre normal de los trabajos: envía la parada limpia a la observación y a todo trabajo que pueda mover la montura (ALIGN, wizard, gain pilot), y después detiene la montura con la misma secuencia de `mount_stop.py` desde el propio servidor. Muestra la confirmación real o «PARADA NO CONFIRMADA». Declara sus límites: es una parada por software sobre INDI, no un freno físico; si INDI o el controlador no responden, no puede detener nada. | operador | Pulsarlo una vez con la montura quieta y otra durante un GOTO, y anotar lo que muestra y lo que hace la montura |
| BL-058 | P3 | **ABORT GAIN PILOT** sin pilot planificado queda como trabajo FAIL («plan first»). | Dos casos el 2026-10-13 a las 17:48 sobre `TEST-STOP`. Es el mismo ruido que BL-048 corrigió para la consulta de estado. | autónomo | Tratarlo como «nada que abortar» |
| BL-054 | P2 | **Cámara de montura: 503 intermitente.** La parte de software está corregida; la del equipo no se puede comprobar desde el Pi. | El journal del servidor tiene 3 respuestas 503 y 3 respuestas 200 de `/mount_camera/stream` desde el 2026-10-08, con un 200 un minuto después de un 503. La cámara es una ESP32-CAM por WiFi (`192.168.1.164:81`, un solo cliente a la vez); el 2026-10-13 respondía ping (4 a 42 ms) y tenía abiertos los puertos 80 y 81. El servidor no registraba el motivo de cada 503, así que la causa de esos tres no se puede reconstruir. Defecto encontrado: el relay marcaba ERROR en cada intento fallido y respondía 503 aunque quedara un reintento; además nunca soltaba a los espectadores cuando se rendía. Corregido en 7b2eefb, y ahora el motivo queda en el journal. No se abrió el stream de la cámara para probar, porque le habría quitado la imagen a quien la estuviera mirando. | operador | Después del reinicio (BL-044): si vuelve a aparecer «STREAM UNAVAILABLE», mirar LAST ERROR en MONITOR o `journalctl -u almita-observe-api \| grep "mount camera"` y anotar el mensaje. Con eso se distingue WiFi débil, cámara colgada u otro cliente ocupando el stream |

### 2.2 Pruebas pendientes en hardware

Todas necesitan al operador con la montura y la antena en posición. BL-026 a BL-030 caben en una misma salida.

| id | prioridad | prueba | cierra | evidencia que falta |
|---|---|---|---|---|
| BL-026 | P1 | ALIGN SOLAR desde la web: primero sin SYNC, después con SYNC, y un ALIGN de confirmación. Guía: manual §14, prueba C. **Sin Sol disponible el 2026-10-13.** | BL-022 | `alignment_result.json` con offset y confianza; SYNC APPLIED; segundo ALIGN con offset cercano a 0. Hoy no hay ninguna corrida solar real: las carpetas `SOLAR-*` son restos de tests (BL-055). |
| BL-027 | P1 | STOP OBSERVATION real. **Tres pruebas el 2026-10-13** (§4.1 a §4.3): tras un GOTO, durante un GOTO y durante una captura. | — | **Comprobado en hardware:** cierre en 6 a 7 s; estado `ABORTED` coherente en orquestador, API y MONITOR (desde el arreglo `0148b78`); solo `.h5` completos y validados; ningún `.part` en ninguna de las tres; SDR liberados. **Queda abierto por la montura:** tras un STOP durante un GOTO queda en seguimiento (BL-059). Se cierra cuando BL-059 esté resuelto y probado |
| BL-028 | P2 | STOP CURRENT STEP del wizard. **Probado el 2026-10-13 en sus tres casos, PASS los tres:** 50 Ω durante la captura (§4.4), HI ALTO durante el GOTO (§4.5) y HI ALTO durante la captura (§4.6). | BL-007 | **Cerrado.** Queda como observación, no como fallo: el STOP no frena un GOTO en curso (la montura lo completa) |
| BL-029 | P2 | Fallas provocadas: desconectar el USB de MAIN; apagar INDI. **Hecho de paso el 2026-10-14 (bajo techo):** desconexión del SDR de RFI REF en plena sesión, sin efecto en MAIN (BL-062). | — | Falta lo principal: el USB de MAIN y la caída de INDI, con BLOCK visible en PLAN y STATUS y recuperación documentada |
| BL-030 | P3 | Bench `sdr --yes` en MAIN y la CLI de `mount_control.py`. | BL-008, BL-011 | Salida del bench con ACK; un GOTO por CLI |
| BL-031 | P1 | Antena B: medir que el LNA recibe alimentación con el Bias-T de RFI REF. | BL-025 | Nivel de ruido de RFI REF con BIAS-T marcado y desmarcado, o tensión en el conector. No se puede sacar de los datos guardados: las seis sesiones con RFI REF usaron `-T` y no hay ninguna sin él. No mueve la montura. |
| BL-032 | P2 | Ver el aviso del gain pilot en una sesión real: aprobar una ganancia sin perfil. | BL-023 | Captura de pantalla o `gain_pilot_state.json` de esa sesión |
| BL-033 | P1 | Recorrido completo nocturno siguiendo el manual (§14, prueba D), y dejar escrito el procedimiento de Ekos (alineación, hora, sitio). **Hecho como prueba de flujo, bajo techo, el 2026-10-14** (§4.8): ALIGN → OBSERVE → QUICKLOOK/RFI REF → REDUCE → SCIENCE, todo desde la web y sin errores. | BL-021 | Repetirlo a cielo abierto, que es lo único que valida los resultados; y el procedimiento de Ekos, que sigue sin escribirse |
| BL-034 | P2 | Medir el haz (FWHM). Hoy es provisional: 20° en la configuración y 14° en `alignment.py`. | — | Barrido del Sol; afecta la separación de la grilla, ALIGN y SCIENCE. Depende de BL-026. |

### 2.3 Decisiones del operador

| id | prioridad | decisión | contexto | opción propuesta |
|---|---|---|---|---|
| BL-040 | P2 | **La web :8088 es alcanzable desde Internet.** Decidido: el operador indicó el 2026-10-13 que el acceso por Internet funciona como él quiere. No se cambió router, reenvíos, firewall, autenticación ni acceso remoto. Quedan comprobaciones. | Del 2026-10-10 al 12 el journal registra 57 direcciones públicas distintas; 76 pedidos de escáneres rechazados con 401 y una sesión autenticada desde una IP pública el 2026-10-11 16:55 UTC. El 2026-10-13 siguen llegando escáneres (10 direcciones más, todas rechazadas con 401 o 400). La contraseña viaja por HTTP sin cifrar (Basic Auth) y esa web puede mover la montura. El Pi también escucha en todas las interfaces en 22, 80, 445, 5900, 6080 y 7624 (INDI: sin autenticación y mueve la montura); desde el Pi no se puede saber cuáles reenvía el router. | Comprobaciones pendientes, todas del operador: (1) confirmar que la sesión del 2026-10-11 fue suya; (2) mirar en el router qué puertos se reenvían además de :8088, sobre todo 7624; (3) decidir si la contraseña sin cifrar es aceptable o si conviene una VPN |
| BL-035 | P1 | Unir `web-polish-v1` con `main`. | `main` tiene 19 commits de documentación y sitio que la rama no tiene; la rama tiene todo el producto. Solo chocan en `README.md`. **Preparado el 2026-10-13** en la rama `integration/web-polish-v1-into-main` (`d86b99f`, publicada; la única diferencia con `web-polish-v1` son 12 archivos de documentación, licencias y sitio): las dos ramas originales quedan intactas, el README está resuelto y nada se fusionó ni se desplegó. | El PR no se pudo abrir desde el Pi (no hay `gh` ni token): abrirlo en borrador desde https://github.com/almita-radio/almita/compare/main...integration/web-polish-v1-into-main?expand=1 y fusionarlo después de BL-026 y BL-027 |
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
| BL-061 | — | MONITOR, SPECTRAL STACK 3D con fondo negro. **Hecho y desplegado el 2026-10-14** (`454c6b3`): fondo negro, marco tenue y extremo bajo de la escala más claro. Visto en una captura de pantalla de MONITOR. | — | — |
| BL-064 | — | SCIENCE: mapas **B y C con la misma forma que A**, y negro donde no hay datos (pedido del operador, 2026-10-14). **Hecho:** B y C se dibujan solo dentro del contorno del tablero medido (el borde exterior de las celdas de A), en vez de un rectángulo completo; todo lo demás va en negro, también en el lienzo de A. Vale para las imágenes de la página, los PNG/SVG/PDF de B y C y la figura combinada A/B/C. Es solo presentación: los valores de B y C y sus archivos de datos no cambian. | Comprobado regenerando en una carpeta temporal los mapas de la reducción real `REDUCE-20261014-004721-315027`. Se aplica a los próximos RUN de SCIENCE; los resultados ya guardados hay que volver a correrlos para verlos así | — |
| BL-063 | — | QUICKLOOK, vista «NATIVE GRID (INTERPOLATED PREVIEW)» con menos interpolación (pedido del operador, 2026-10-14). **Hecho:** ya no rellena todo el polígono entre puntos, solo hasta 0.75 separaciones de grilla alrededor de cada punto real; resolución de unos 4 píxeles por celda en vez de 60×60 fijo; sin suavizado adicional al dibujar. **Bug encontrado y corregido de paso:** la geometría de la grilla leía la columna `ra` del CSV, que está en horas, como si fueran grados, y eso comprimía 15 veces el eje este-oeste de esta vista. El mapa NATIVE GRID exacto no cambia: se dibuja por fila y columna. | Comparado antes y después con las sesiones reales de 100 y 25 puntos del 2026-10-08. Se aplica desde la próxima observación (QUICKLOOK se lanza por sesión); las vistas ya guardadas no se regeneran | — |
| BL-043 | P2 | Sección ALIGN de PIPELINE sin opción de SYNC: siempre usa `--no-sync`. Junto con esto, decidir si `alignment_engine` adopta la política de SYNC de `alignment.py` (BL-049). | autónomo | BL-026, para no exponer un SYNC sin probar. **No se habilita antes.** |
| BL-050 | P3 | Lo que queda de FIELD_RUNBOOK §14: identidad canónica de la sesión; lock del proceso INDI; `gain='auto'` como default en `sdr_capture.configure`; lectores DS18B20 duplicados; `README.md` desactualizado. | Los tres primeros tocan módulos congelados (`capture.py`, `indi_telescope_control.py`, `sdr_capture.py`): decisión del operador. El README conviene resolverlo con BL-035, porque es el único archivo que choca con `main`. Los lectores DS18B20 ya comparten el rango de plausibilidad; unirlos es limpieza sin efecto visible | — |

## 3. Orden propuesto

Todo lo que se podía hacer sin hardware ni decisiones está hecho. Lo que sigue necesita al operador.

0. **Primero, y se puede hacer bajo techo:** repetir STOP OBSERVATION durante un GOTO y durante una captura con
   la corrección del 2026-10-14, y probar PARAR MONTURA (BL-059, BL-065; manual §14, prueba A). Es lo único que
   convierte la corrección en un hecho.

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

## Resumen de las paradas al 2026-10-14

| parada | en hardware (bajo techo) | con la corrección del 2026-10-14 |
|---|---|---|
| STOP OBSERVATION durante una captura | PASS (§4.3) | **PASS en la montura real, 2026-10-14 (§4.9)** |
| STOP OBSERVATION tras un GOTO, antes de capturar | Datos PASS; cierre FAIL, corregido y visto en la segunda prueba (§4.1, §4.2) | Sin repetir |
| **STOP OBSERVATION durante un GOTO** | **FAIL: la montura quedó en seguimiento (§4.2)** | **Corregido en código; sigue fallido hasta repetirlo en la montura** |
| STOP CURRENT STEP del wizard, tres casos | PASS los tres (§4.4 a §4.6) | El wizard ahora también aborta el GOTO; sin repetir |
| ALIGN, gain pilot | Sin prueba de parada en hardware | Usan la misma parada confirmada; sin probar |
| PARAR MONTURA (web) | No existía | Nuevo; sin probar en la montura |

## 4.0 Condiciones de las pruebas del 2026-10-13 y 2026-10-14: bajo techo

**Todas las pruebas de §4.1 a §4.7 se hicieron con la montura y la antena bajo techo** (lo indicó el operador el
2026-10-14, después de hacerlas). La montura se movió y los SDR capturaron de verdad, pero la antena no miraba
al cielo.

| vale como prueba en el instrumento | no vale |
|---|---|
| Paradas (STOP OBSERVATION, STOP CURRENT STEP): tiempos de cierre, estados, archivos, liberación de los SDR | Cualquier resultado que dependa de la señal del cielo: contraste HI ALTO / HI BAJO, ALIGN con HI o con el Sol, QUICKLOOK como imagen del cielo |
| Comportamiento de la montura: GOTO, seguimiento, error de llegada, límite de meridiano de OnStep, qué pasa al parar | Que las zonas del mapa correspondan al cielo real (BL-001) |
| Flujo completo del wizard y construcción del perfil con 50 Ω en la entrada del LNA (la referencia de 50 Ω no depende del cielo; sí de la temperatura del lugar) | La calidad científica de cualquier captura con antena |
| Lanzamiento de RFI REF con `-T` y su cierre | Lo que RFI REF dice del entorno de radio en terreno |

**Un SYNC hecho bajo techo no tiene sentido y puede desajustar el apuntado:** ALIGN mediría ruido. El 2026-10-13 se
lanzaron dos ALIGN con SYNC marcado (Sol a las 17:32 y HI a las 17:36) y los dos se detuvieron antes de terminar;
no llegó a enviarse ningún SYNC.

**Sin confirmar:** si las sesiones del 2026-10-02 al 2026-10-08 (la de 400 puntos, las de 676, 100 y 25, los
wizards y el ALIGN HI) fueron a cielo abierto. Este backlog y el brief las cuentan como datos reales del cielo.

## 4.1 Primera prueba real de STOP OBSERVATION (2026-10-13)

| | |
|---|---|
| Sesión | `data/mosaic/TEST-STOP-20261013-17:47:25` (25 puntos planificados, 10 s, 40.2 dB) |
| STOP pedido | 17:50:39 UTC |
| Fase | Punto 4: GOTO terminado a las 17:50:37.5; el STOP llegó 1.5 s después, en la confirmación de seguimiento o la estabilización, **antes de la captura**. No valida ni «durante captura» ni «durante GOTO» |
| Captura detenida | 17:50:45.6 UTC, **6 s** después del STOP |
| SDR | MAIN: cliente desconectado a las 17:50:40 y `rtl_tcp` de nuevo en «listening». RFI REF: `rtl_tcp` cerrado a las 17:50:45.6 y puerto 1235 libre (cerrado con SIGKILL tras 5 s, que es su comportamiento conocido) |
| QUICKLOOK | Proceso terminado; último archivo a las 17:50:43 |
| Archivos | 3 `.h5` de 12.07 MB, cada uno con 24 000 000 muestras, `file_state: complete` y evidencia de sintonía `RTL_TCP_SERVER_ACK`; validados con el mismo validador de la captura. **0 `.part`** |
| Puntos | 3 `success`, 22 `planned` (el punto 4 quedó `planned`: nunca empezó a capturar), 0 fallidos |
| Montura (lectura INDI, sin enviar órdenes) | Seguimiento apagado (`TRACK_OFF`), sin movimiento, sin error, desaparcada, en las coordenadas del punto 4. No hay línea en el log que diga qué apagó el seguimiento |
| Estado mostrado | **Incorrecto.** Orquestador en `STOPPING` durante 120 s y después `DEGRADED` «did not exit within 120s of SIGINT»; MONITOR y la API en `DEGRADED` «session went silent while RUNNING». Lo esperado era `ABORTED` |
| Resultado | **Datos: PASS. Estado y cierre: FAIL**, corregido en `0148b78` y pendiente de volver a probar |
| Evidencia guardada | `stop_test_evidence/` dentro de la sesión: los JSON de estado tal como quedaron y los extractos del journal |

## 4.2 Segunda prueba real de STOP OBSERVATION (2026-10-13), con el arreglo desplegado

| | |
|---|---|
| Sesión | `data/mosaic/TEST-STOP-20261013-18:01:12` |
| STOP pedido | 18:03:16 UTC |
| Fase | **Durante el GOTO del punto 3**: el log tiene «MOTION START» y, a las 18:03:16.98, «GOTO cancelado». Es el caso A2 del manual, no el A1 |
| Cierre | Respuesta del STOP a las 18:03:23: **7 s**. Estado `ABORTED` en el orquestador, en la API (`effective_state`) y en MONITOR («stopped by the operator»). El arreglo de `0148b78` funciona |
| SDR | MAIN liberado a las 18:03:18; RFI REF cerrado a las 18:03:23; puerto 1235 libre |
| Archivos | 2 `.h5` completos y validados (24 000 000 muestras cada uno, sintonía por ACK). **0 `.part`**. Punto 3 sin archivo, `planned` |
| Montura (lectura INDI, sin enviar órdenes) | **Terminó el GOTO y quedó en seguimiento sidéreo** en las coordenadas del punto 3. El log dice «Tracking OFF could not be confirmed». BL-059 |
| Resultado | **Cierre y estado: PASS. Datos: PASS. Montura: FAIL** |

## 4.3 Tercera prueba real de STOP OBSERVATION (2026-10-13): durante una captura

| | |
|---|---|
| Sesión | `data/mosaic/ALMITA-OBSERVE-20261013-18:07:14` (25 puntos, **20 s** por punto, 40.2 dB) |
| STOP pedido | 18:09:01 UTC |
| Fase | **Durante la captura del punto 2.** El log llega a `SDR FLUSH OK` y `TEMP PRE` (18:08:54.6), que es donde empieza la captura de 20 s, y no tiene la línea `SDR CAPTURE OK`: el STOP cayó unos 6 s dentro de la captura. Es el caso A1 del manual |
| Cierre | Respuesta del STOP a las 18:09:08: **7 s**. `ABORTED` en el orquestador, la API y MONITOR |
| SDR | MAIN liberado a las 18:09:02; RFI REF cerrado a las 18:09:07.8; puerto 1235 libre |
| Archivos | 1 `.h5` (punto 1), completo y validado: 48 000 000 muestras, sintonía por ACK. **0 `.part`.** Del punto 2 no quedó ningún archivo |
| Puntos | 1 `success`, 1 `capturing` (el punto 2 quedó con esa marca en `mosaic.csv`; no se pasó a fallido), 23 `planned` |
| Montura (lectura INDI a las 18:09:37, sin enviar órdenes) | Quieta en las coordenadas del punto 2, **seguimiento apagado**, sin error. El log no tiene esta vez el aviso «Tracking OFF could not be confirmed». No se puede distinguir desde los registros si lo apagó la limpieza de `capture.py` o el operador en Ekos |
| Otro | Una lectura del sensor de temperatura del SDR falló por CRC a las 18:08:54 (`TEMP PRE SDR=ERR`); el sistema la descartó, como debe |
| Resultado | **PASS** en cierre, estado, datos y SDR |

## 4.4 Primera prueba real de STOP CURRENT STEP del wizard (2026-10-13): paso de 50 Ω

| | |
|---|---|
| Sesión | `data/calibration/WIZARD-20261013-181623-230083` (5 capturas de 5 s, 40.2 dB) |
| STOP pedido | 18:17:04 UTC, trabajo `CALIBRATE_WIZARD-20261013-181648-da0e`; terminó en ese mismo segundo |
| Fase | Durante las capturas de 50 Ω: 2 de 5 completas, la tercera en curso. Este paso no mueve la montura |
| Tras el STOP | El wizard siguió en `STABILIZE_50R`; interrupción registrada como `STOPPED_BY_OPERATOR` con «2 file(s) kept»; botón en RETRY CAPTURE. `capture_000.h5` y `capture_001.h5` completos y validados; nada de la tercera captura, ni `.part`. SDR MAIN liberado |
| RETRY (18:31:48) | Los dos archivos del intento interrumpido pasaron a `captures/AMBIENT_50R/interrupted-20261013T183149220021Z/` con el mismo tamaño y la misma fecha: no se sobrescribieron. El reintento dejó 5 capturas nuevas, `capture_000` a `capture_004`. El wizard avanzó a `RESULT_50R` y la interrupción dejó de mostrarse |
| Archivos | 7 `.h5` en total, los 7 validados (12 000 000 muestras cada uno). **0 `.part`** |
| Resultado | **PASS** |

## 4.5 STOP CURRENT STEP del wizard en HI ALTO, durante el GOTO (2026-10-13)

| | |
|---|---|
| Sesión | `data/calibration/WIZARD-20261013-181623-230083`, trabajo `CALIBRATE_WIZARD_MOVE-20261013-185347-a7d8` |
| GOTO | Enviado a las 18:54:01.4, de RA 19.195 h / Dec −43.29° a RA 16.111 h / Dec −51.80° |
| STOP pedido | 18:54:16 UTC, **durante el GOTO**: el log dice «GOTO cancelado» a las 18:54:16.06, sin haber empezado ninguna captura. El trabajo terminó en ese mismo segundo |
| Wizard | Siguió en `READY_HI_ALTO`; interrupción `STOPPED_BY_OPERATOR` con 0 archivos. HI ALTO sin medir |
| Archivos | Ninguno en `captures/HI_ALTO/`. **0 `.part`** |
| SDR | MAIN liberado a las 18:54:16 |
| Montura (lectura INDI a las 18:54:38, sin enviar órdenes) | **Terminó el GOTO**: está en el objetivo de HI ALTO. **Seguimiento apagado**, sin error; la RA avanza al ritmo sidéreo, como corresponde a una montura quieta sin seguimiento |
| Diferencia con OBSERVE (BL-059) | Aquí la montura no quedó en seguimiento. El wizard solo lo enciende después del GOTO, y el STOP llegó antes; en OBSERVE ya venía encendido desde el punto anterior |
| Resultado | **PASS.** Confirma que el STOP no frena el movimiento: la montura completa el GOTO |

## 4.6 STOP CURRENT STEP del wizard en HI ALTO, durante la captura (2026-10-13)

| | |
|---|---|
| Sesión | `data/calibration/WIZARD-20261013-181623-230083`, trabajo `CALIBRATE_WIZARD_MOVE-20261013-192622-da59` |
| Antes del STOP | GOTO corto (5.0°) completado; el log dice «sidereal tracking ON confirmed (was off)»; montura en el objetivo con 0.02° de error; primera captura de 5 s empezada |
| STOP pedido | 19:26:51 UTC, **durante la primera captura**. El trabajo terminó a las 19:26:52 |
| Wizard | Siguió en `READY_HI_ALTO`; interrupción `STOPPED_BY_OPERATOR` con 0 archivos |
| Archivos | Ninguno en `captures/HI_ALTO/` (la captura interrumpida no deja archivo). **0 `.part`** |
| SDR | MAIN liberado a las 19:26:51 |
| Montura (lectura INDI a las 19:28:43, sin enviar órdenes; el operador no tocó Ekos) | **Seguimiento apagado**, quieta, sin error. La RA leída (16.1447 h) es la del objetivo (16.1136 h) más 112 s de deriva sidérea, que es el tiempo transcurrido desde el STOP: el seguimiento se apagó en el momento de la parada. El wizard lo dejó como estaba antes del paso |
| Resultado | **PASS** |

## 4.7 Wizard completo tras las pruebas de STOP (2026-10-14)

| | |
|---|---|
| Sesión | `data/calibration/WIZARD-20261014-001224-467117`, de 00:12 a 00:18 UTC: 5 min 30 s de principio a fin |
| Configuración | 5 capturas de 2 s por referencia, 1420405752 Hz, 2400000 sps, 40.2 dB; 50 Ω en `LNA_INPUT` |
| Zonas | Aprobadas a las 00:14 y capturadas a las 00:16 y 00:17, sin pausa. HI ALTO: RA 16.153 h, Dec −51.37°, a 35.8° de altura. HI BAJO: RA 23.101 h, Dec −43.35°, a 64.0° |
| Apuntado | Error de llegada 0.12° y 0.13° (tolerancia 0.25°), mantenido durante las capturas con seguimiento confirmado |
| Archivos | 15 `.h5` (5 por referencia), los 15 validados. Ningún `.part`, ninguna interrupción |
| Contraste | `INCONCLUSIVE` (0.20 sigma, umbral 3). **Bajo techo no puede salir otra cosa:** la antena no veía el cielo, así que no hay diferencia de HI que medir. No dice nada de la cadena |
| Perfil | **READY**: `observe_profile/calibration_profile_v1.json`, nivel `RELATIVE_INSTRUMENTAL`, misma frecuencia, muestreo y ganancia que la configuración central |
| Montura al terminar | Quieta, sin seguimiento, sin error en OnStep |
| Wizards anteriores del día | `WIZARD-20261013-181623-230083` y `WIZARD-20261013-193915-787403`, abortados: conservan todos sus archivos (12 y 5) |

## 4.8 Flujo completo desde la web, bajo techo (2026-10-14)

Prueba de flujo: comprueba que cada etapa arranca, termina y entrega lo que la siguiente necesita. No valida nada
del cielo (§4.0).

| etapa | hora UTC | resultado |
|---|---|---|
| CALIBRATE (wizard) | 00:12 a 00:18 | Perfil READY a 40.2 dB (§4.7) |
| ALIGN HI | 00:23 a 00:27 | 10 posiciones capturadas; `NO DEFENDIBLE DIFFERENTIAL HI STRUCTURE`; SYNC pedido y **no enviado** («no offset estimated»); montura quieta al terminar. Trabajo PARTIAL, que es lo correcto |
| OBSERVE | 00:36 a 00:46 | `data/mosaic/ALMITA-OBSERVE-20261014-00:36:00`: 25 de 25 puntos, 0 fallidos, 0 diferidos, 9 min 17 s; grilla de 10×10° con 1 s por punto, 40.2 dB; 25 `.h5`, ningún `.part`; `SESSION COMPLETE` con OnStep y `rtl_tcp` en HEALTHY. Terminó sola |
| QUICKLOOK | en vivo | 25 de 25 puntos procesados, sin errores, con el perfil `WIZARD-20261002-013321-160933` |
| RFI REF | en vivo | Lanzado con `-T`, ACK de sintonía; 864 bloques procesados y 155 descartados; cerrado al terminar (con SIGKILL tras 5 s, su comportamiento conocido) |
| REDUCE | 00:47, 13 s | `REDUCE-20261014-004721-315027`: 25 de 25 puntos, todos RELATIVE, LSRK y calidad GOOD |
| SCIENCE | 00:50 y 01:01, 20 s cada vez | 25 puntos usados, mapas A, B y C y exportes escritos. La segunda corrida ya tiene B y C con la forma de A y fondo negro (BL-064) |
| MONITOR | todo el tiempo | Usado por el operador para seguir la sesión |

Observaciones:

- La etiqueta interna del orquestador sigue en `RUNNING` después de una sesión que termina sola; la web muestra el
  estado efectivo, `COMPLETED`. Es lo ya descrito en BL-016.
- QUICKLOOK y REDUCE usaron el perfil del 2026-10-02, no el nuevo del 2026-10-14. Los dos son a 40.2 dB y
  compatibles; no es un error.
- La montura quedó quieta, sin seguimiento y sin error al final.

## 4.9 STOP OBSERVATION durante una captura, con la corrección (2026-10-14, bajo techo)

Primera corrida real del código nuevo (`3bfeb98`).

| | |
|---|---|
| Sesión | `data/mosaic/ALMITA-OBSERVE-20261014-02:03:05` (400 puntos planificados, 15 s por punto, 40.2 dB) |
| STOP pedido | 02:05:32.78 UTC, **durante la captura del punto 2** (según `stop_report.json` y el log: `SDR FLUSH OK`, `TEMP PRE`, sin `SDR CAPTURE OK`) |
| Parada de la montura | **CONFIRMADA en 10.8 s.** Orden de abortar enviada y aceptada por el driver real; estado «no moviéndose» leído de inmediato; seguimiento apagado en 3 lecturas consecutivas, sin reactivaciones; posición coherente con una montura quieta (RA +6.0 s y Dec +0″ en 5.7 s, frente a +5.7 s esperados) |
| Lectura independiente (02:06:58, sin enviar órdenes) | Seguimiento apagado, sin movimiento, sin error en OnStep, RA avanzando al ritmo sidéreo. El operador confirmó a la vista: montura quieta, seguimiento apagado |
| Fin de sesión | Publicado por `capture.py`: `SESSION_ABORTED`, sin error, con el veredicto de la parada. Código de salida 130 |
| Estados | `ABORTED` en el orquestador, la API (con la nota «MOUNT STOP CONFIRMED») y MONITOR |
| Cierre | Respuesta del STOP a las 02:05:49: **17 s** (antes eran 6 a 7 s; la diferencia es la comprobación de la montura). SDR MAIN liberado a las 02:05:43, RFI REF cerrado a las 02:05:48, puerto 1235 libre |
| Archivos | 1 `.h5` (punto 1), completo y validado (36 000 000 muestras). Ningún archivo ni `.part` del punto 2. `mosaic.csv`: 1 `success`, 1 `capturing`, 398 `planned` |
| Defecto menor encontrado | `stop_report.json` decía «planned: 400» en el recuento de puntos, porque contaba las filas en memoria. Corregido: ahora cuenta desde el archivo del plan |
| Resultado | **PASS.** Es el caso «durante una captura»: la montura ya estaba quieta. **El caso durante un GOTO sigue pendiente** |

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
