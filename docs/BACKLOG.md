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
| BL-059 | — | Tras un STOP durante un GOTO, la montura terminaba el movimiento y quedaba con el seguimiento encendido. **Corregido en origen y comprobado en la montura real el 2026-10-14** (`3bfeb98`, `1bcd07e`). | Causa: la interrupción llegaba a `capture.py` como cancelación; la única limpieza era pedir que se apagara el seguimiento con la montura todavía en movimiento. Solución: `mount_stop.py` aborta el movimiento, espera con límite, apaga el seguimiento, exige que siga apagado y comprueba con la posición que la montura ni se mueve ni sigue; si la comprobación falla, reapaga el seguimiento y repite la prueba. Solo declara parada confirmada con la lectura de INDI. Evidencia: §4.9 (durante captura), §4.10 (primer intento durante GOTO: abortó, pero OnStep reactivó el seguimiento segundos después y quedó «PARADA NO CONFIRMADA») y §4.11 (durante GOTO, PASS: reactivación detectada y corregida, confirmado en 17.9 s). | — | **Cerrado** para OBSERVE, bajo techo. Queda por ver: el caso sin orden de abortar (no aplica a este driver, que la acepta) y la parada en ALIGN, wizard y gain pilot con el código nuevo |
| BL-057 | — | `capture.py` no anunciaba el fin de la sesión cuando se detenía con STOP. **Corregido en origen** (`3bfeb98`): publica `SESSION_ABORTED` con el resultado de la parada de la montura, marca la sesión como pausada y escribe `stop_report.json`. | Probado con procesos y señales reales en `tests/test_capture_stop_integration.py`. La compensación desde afuera (`0148b78`) se conserva como respaldo. | operador | Verlo en la repetición de la prueba A |
| BL-060 | P2 | **Software hecho el 2026-10-15** (`882c06e`; ALIGN: `36bc57f`): PROPOSE descarta zonas que llegan a +12 min del meridiano dentro de 45 min + la espera (con datos reales: 1229 puntos descartados, 14 015 considerados); CAPTURE lo revisa antes del GOTO; RE-PROPOSE disponible después de aprobar (conserva HI ALTO medido); la web explica cómo borrar el error latcheado (tracking OFF/ON en Ekos). ALIGN: PLAN informa y RUN bloquea una corrida que cruza el límite (un ALIGN solar cerca de las 13:40 hora Chile). **Falta:** probarlo en la montura real. **El wizard no tiene en cuenta el límite de meridiano de OnStep, y un plan HI aprobado no se puede rehacer.** | 2026-10-13 19:32, `WIZARD-20261013-181623-230083`, trabajo `CALIBRATE_WIZARD_MOVE-20261013-193107-0995`: las 5 capturas de HI ALTO se completaron, pero la comprobación final encontró el seguimiento apagado y el paso falló («target NOT held … tracking off»). OnStep informa «Meridian Limit (W) Exceeded»; su límite es de 12 minutos pasado el meridiano y el objetivo llegó a un ángulo horario de +0.202 h (12.1 min) a las 19:31:59. El plan se había aprobado a las 18:53, cuando el objetivo estaba al este del meridiano. El wizard hizo lo correcto al rechazar el paso. Faltan dos cosas: avisar o evitar objetivos que cruzarán el límite durante la espera, y permitir RE-PROPOSE desde un paso HI ya aprobado (hoy solo se puede antes de aprobar). | autónomo el diseño; hardware para probarlo | Leer el límite de OnStep (`Minutes Past Meridian`) al proponer y al capturar; permitir volver a proponer zonas sin abortar la sesión. **Además:** el error de límite queda anotado en OnStep y ALMITA bloquea todo movimiento mientras siga ahí. Comprobado el 2026-10-13/14: no lo borran ni ir a home, ni un GOTO desde Ekos, ni reiniciar Ekos; **lo borra encender y apagar el seguimiento en Ekos**. Falta que la web lo diga al bloquear |
| BL-062 | — | El SDR de RFI REF (serial 00000002) se desconectó del USB en plena sesión. **Explicado y cerrado:** lo desconectó el operador un momento al ponerle una ferrita. | 2026-10-14 00:32:12 UTC, sesión `ALMITA-OBSERVE-20261014-00:28:48` (bajo techo): «usb 3-1: USB disconnect»; RFI REF quedó en FAILED («Connection reset by peer») y STATUS marcó `rfi_sdr` DOWN. **MAIN no se vio afectado:** la captura siguió y guardó el punto 7 después de la desconexión. | — | Nada. Sirve como evidencia parcial de BL-029: desconectar el SDR de RFI REF en plena sesión no afecta a MAIN y se ve en MONITOR y STATUS |
| BL-065 | P2 | **Botón PARAR MONTURA en todas las páginas.** Hecho el 2026-10-14 (`3bfeb98`). Probado una vez en la montura real con el seguimiento encendido: lo apagó y confirmó en 9.4 s. | Independiente del cierre normal de los trabajos: envía la parada limpia a la observación y a todo trabajo que pueda mover la montura, y después detiene la montura con `mount_stop.py` desde el propio servidor. Muestra la confirmación real o «PARADA NO CONFIRMADA». Es una parada por software sobre INDI, no un freno físico. | operador | Probarlo **durante un GOTO** y con una observación o un ALIGN corriendo, para ver que también detiene el trabajo |
| BL-058 | — | **Corregido el 2026-10-15** (`7f7e86c`): ABORT sin pilot responde «nada que abortar», exit 0, sin escribir nada. **ABORT GAIN PILOT** sin pilot planificado queda como trabajo FAIL («plan first»). | Dos casos el 2026-10-13 a las 17:48 sobre `TEST-STOP`. Es el mismo ruido que BL-048 corrigió para la consulta de estado. | autónomo | Tratarlo como «nada que abortar» |
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
| BL-068 | P2 | **Nivel de MAIN en el ADC frente a la ganancia.** Hallado el 2026-10-14 al revisar el gain pilot de `ALMITA-OBSERVE-20261014-14:59:28`: las muestras crudas ocupan solo los códigos 124 a 130 de 256 (desvío 0.63 códigos), y HIGH, LOW y la verificación final dan lo mismo (0.634). Todas las sesiones revisadas dan 0.63–0.65, también las de cielo abierto (2026-10-02, 10-07) y la de 42.1 dB (2026-10-08: 0.65, cuando +1.9 dB debería dar ~0.76 si dominara el ruido de la cadena de RF). Indica que el nivel lo domina el propio ADC, o que el cambio de ganancia no se aplicó: rtl_tcp confirma el comando pero no informa la ganancia real. Por eso el margen de «126 códigos» del gain pilot no significa holgura real, y su sugerencia de subir un paso no se apoya en nada. **Sirve bajo techo:** es una medición del instrumento, no del cielo, y no mueve la montura. Hacerla sin ninguna observación en curso. | — | Unos segundos de captura de MAIN a varias ganancias (p. ej. 30, 40.2, 44.5 y 49.6 dB), con 50 Ω y con la antena, y el desvío del ADC de cada una. Si el desvío no sigue a la ganancia, revisar el nivel que entrega la cadena LNA/filtro y si el tuner aplica la ganancia |
| BL-069 | P2 | **Parte de software hecha el 2026-10-15** (`bc2d4db`): MONITOR leía el archivo `temperature` del kernel, sin CRC (dejaba pasar −24 °C); ahora usa el lector con CRC y cada lectura reintenta una vez. Medido a las 02:30 hora Chile: 12/12 lecturas con CRC correcto en los dos sensores, pero `dmesg` volvió a registrar `max_slave_count 64` durante la noche. **Sigue pendiente el cableado (operador).** **Sensor de temperatura del SDR (DS18B20 `28-082471f4e41b`): lecturas con CRC fallido.** Hallado el 2026-10-14 en la sesión `ALMITA-OBSERVE-20261014-14:59:28`: 50 de 61 lecturas fallidas desde el punto 2 (15:04:55), contra 2 de 625 en la sesión de las 04:44. El sensor del LNA, en el mismo bus, no falló ninguna vez; los errores no dependen de la posición de la montura; cuando lee, da 35.8 °C (normal). El kernel registró una búsqueda del bus con 64 dispositivos (hay 2): ruido o mal contacto en la rama de ese sensor. Los datos IQ no se ven afectados; falta la temperatura del SDR en los metadatos de esos puntos. **Acción del operador:** mejorar los conectores de esa rama (datos, GND, 3.3 V y empalme al bus en GPIO4). No mueve la montura. | — | Tras el arreglo, decenas de lecturas seguidas del sensor del SDR sin CRC fallido y ningún aviso de `max_slave_count` en `dmesg`; después, una sesión con la temperatura del SDR presente en todos los puntos. Opcional: un reintento por lectura en `temperature_sensors.py` para los fallos esporádicos (no resuelve esta falla) |
| BL-071 | P2 | **Dos líneas locales en la banda, una por receptor** (sesión `ALMITA-OBSERVE-20261014-14:59:28`, bajo techo). **MAIN ~1419.5 MHz:** continua (75/79 puntos), deriva ±40 kHz, +0.8 dB; ya estaba en la referencia de 50 Ω del perfil `WIZARD-20261014-001224-467117` (1419.496 MHz, +0.2–0.4 dB), y por eso deja una columna oscura fija en QUICKLOOK. RFI_REF no la ve. **RFI_REF:** un peine intermitente (≈11 % de las filas) de ~14 líneas que se encienden juntas (1419.642, 1419.998, 1420.110, 1420.710, 1421.067 MHz…, +8 a +23 dB) y una línea fija de +37 dB en 1420.035 MHz; MAIN no ve el peine ni siquiera corrido ~131 kHz (corrimiento posible del cristal del RTL2838 genérico). Las ráfagas no siguen a los GOTO; leve coincidencia con la escritura a disco (14/20, no concluyente). Puede tener relación con el rasgo de +148–153 km/s del forense de SCIENCE: sin verificar | — | Banco bajo techo, sin observación en curso y sin mover la montura, cambiando un elemento por vez (50 Ω en el SDR con DC block o sin Bias-T, 50 Ω en la entrada del LNA, antena; dongle de RFI_REF desconectado; disco escribiendo o quieto; otro puerto USB y otro cable para RFI_REF), 30–60 s por paso, midiendo la línea de 1419.4–1419.62 MHz en MAIN y el peine en RFI_REF |
| BL-072 | P1 | **Línea móvil de MAIN (~1419.25–1421.3 MHz, −0.55…−1.1 MHz del centro): la genera la cadena SDR + LNA, no viene de afuera** (bancos del 2026-10-14, bajo techo; detalle en `data/bench/rf_lines/20261014/STATUS.md`). Sigue la sintonía del SDR (−300/+200 kHz, dos veces, una con el LNA sin alimentar). Su posición depende de la **temperatura del SDR y de la del LNA** (hielo en cada uno; ninguna sola la explica; −72 kHz/°C con hielo en el LNA, barrido de toda la banda con hielo en el SDR) y se pliega en los bordes de la banda. Su fuerza depende de lo conectado a la entrada (fuerte con antena o LNA, débil con 50 Ω al final del cable, ausente con 50 Ω en el SDR) y de la alimentación del LNA (con USB externo ≈ mitad que con Bias-T, no separado de la temperatura). En las campañas viejas está en casi todas; ausente o débil en las noches más frías (14/10 01:45–05:22 hora Chile, 7–9 °C afuera, LNA 25 °C). Descartados: aparatos del depto (teléfono, notebooks, cargadores, pantalla, cámara, luces, receiver, TV, blu-ray, refri, microondas), autooscilación del LNA, sorteo al arrancar rtl_tcp. Los DS18B20 tampoco (2026-10-15): la raya no se modula con su ciclo de conversión y sigue con los sensores despegados del SDR y el LNA; falta desconectar el bus 1-Wire en el Pi. Ninguna documentación de Nooelec ni del RTL-SDR V4 describe este caso; candidato no probado: convertidor conmutado interno del RTL2832U. | BL-071 | (1) Grabar una noche entera sin tocar, con Bias-T, raya + temperaturas de SDR y LNA (`rf_line_bench.py watch --save-spectra` + registro de sensores) para separar las dos temperaturas; (2) confirmar con SCIENCE que el rasgo de +148–153 km/s es esta raya (−0.72 MHz = +152 km/s); (3) decidir mitigación: temperatura estable, máscara según temperatura o mover el centro |

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
| BL-042 | — | **Cerrado el 2026-10-15:** scripts de investigación y respaldos `.tar.gz` archivados en `docs/history/` (`c376e38`); `=.9` (vacío) borrado; las ramas `backup-antes-de-limpiar-github-20260810` y `respaldo-local-18-03` subidas a GitHub como `archive/antes-de-limpiar-github-20260810` y `archive/respaldo-local-18-03` (decisión del operador; sin claves, el email ya era público). `almita_preserve_20261002/` y `data/` siguen solo en el Pi. Archivos locales fuera de Git: scripts de investigación de agosto, respaldos `*_ultima_revision.tar.gz`, el archivo vacío `=.9`, `/home/stellarmate/almita_preserve_20261002/` (6.4 GB) y las ramas `backup-antes-de-limpiar-github-20260810` y `respaldo-local-18-03`. | Lista y motivo de cada uno en PROJECT_BRIEF §6. Nada se borró ni se subió. | Mover los scripts a `scripts/legacy/` si se quieren conservar en Git; el resto queda local hasta que el operador diga |

### 2.4 Mejoras

| id | prioridad | mejora | quién | depende de |
|---|---|---|---|---|
| BL-075 | P1 | **LOBE: página nueva para medir el lóbulo de MAIN con un tránsito solar** (pedido del operador, 2026-10-15). **Hecho y desplegado el 2026-10-15 05:14 UTC (sin commit):** `lobe_transit.py` (deriva con la montura quieta y tracking OFF, o barrido en AR con tracking SOLAR; ganancia fija y AGC del RTL apagado explícitamente; IQ crudo de cada muestra en `data/lobe/<run>/iq/`; saturación por muestra; temperaturas SDR/LNA; potencia de banda con mediana móvil que saca rayas angostas; ajuste gaussiana + fondo lineal en potencia lineal, sin scipy; STOP con `mount_stop`), `console/lobe.html`/`lobe.js` (Sol y montura en el cielo, veredicto del ALIGN solar, plan con duración según el FWHM esperado, gráfico en vivo intensidad vs. tiempo cada 5 s con el ajuste), etapa `lobe` en `almita_web_ops.py` (física: preflight + MOVE; exige un ALIGN solar real PASS de menos de 12 h), rutas `/api/ops/lobe/status` y `/api/ops/lobe/samples`, enlace LOBE en el menú. Tests: `tests/test_lobe_transit.py`, `tests/test_lobe_web.py` (18) + 285 tests web existentes pasando. **Ojo:** el FWHM esperado no está medido (20° en `observer_config.json`, 14° en `alignment.py`, antena registrada como "Dipole"); una deriva de ±1.5×20° dura ~4 h. | operador | Primer uso a cielo abierto tras un ALIGN solar PASS |
| BL-076 | — | **Corregido el 2026-10-15** (`8c52947`): `tune_explicitly` envía 0x08 0 en cada adquisición, lee el «set agc mode» de rtl_tcp, rechaza capturar si dice 1 y lo guarda en el registro de sintonía y en los atributos HDF5 (`rtl_agc`). El pipeline no apaga el AGC digital del RTL2832 (comando rtl_tcp 0x08): `tune_explicitly` fija la ganancia del tuner pero rtl_tcp conserva el AGC que dejó un cliente anterior (SDR++ lo encendió el 2026-10-14; se limpió reiniciando rtl_tcp). LOBE ya lo apaga. | autónomo | Decidir si `sdr_tuning.tune_explicitly` envía 0x08 0 siempre (toca la ruta de captura de OBSERVE) |
| BL-077 | P2 | **SCIENCE con grillas giradas (ANGLE de OBSERVE, `e4b0a0b`).** Hoy SCIENCE rechaza con un mensaje claro una campaña con `rotation_deg ≠ 0`: su tablero A/B/C está alineado con RA/Dec y las esquinas de una grilla girada quedarían fuera. REDUCE y QUICKLOOK no se ven afectados. | autónomo | Pasar cada punto al marco de la grilla (sin tocar los motores congelados) y rotular los mapas "girado θ° desde el Norte" |
| BL-078 | P3 | **Bloquear el driver de TV de Linux para el RTL-SDR** (`dvb_usb_rtl28xxu`, `rtl2832_sdr`, `rtl2832`, `rtl2830`; pregunta del operador, 2026-10-17). Hoy no hay ningún bloqueo: cada vez que `rtl_tcp` se detiene, Linux le devuelve el dongle al driver de TV, que lo pasa a modo TV y consulta el receptor infrarrojo cada 200 ms con errores (`rc.query() failed=-71`, visto el 2026-10-17 09:41 UTC). Al arrancar, `rtl_tcp` tiene que quitárselo ("Detached kernel driver"), una carrera que a veces termina en `usb_claim_interface error`. Bloquearlo deja el dongle quieto entre sesiones y elimina esa carrera; solo se pierde usar el dongle como TV DVB-T (que en Chile no sirve: la TV es ISDB-Tb). Reversible borrando el archivo. Los módulos estaban sin uso (contador 0) con `rtl_tcp` corriendo, así que se pueden descargar en caliente sin reiniciar. | operador | Crear `/etc/modprobe.d/almita-rtlsdr-blacklist.conf`, descargar los módulos sin trabajos activos y comprobar el rms de MAIN después |
| BL-073 | — | **Corregido el 2026-10-15** (`e0ad8d1`): medido, tras atender un cliente rtl_tcp ignora SIGTERM (90 s → SIGKILL) y SIGINT (>200 s); `TimeoutStopSec=5` en la unidad (instalada y recargada): un reinicio tarda ~6 s. `rtl_tcp` no se cierra con SIGTERM: cada `systemctl restart rtl_tcp` espera 90 s y termina en SIGKILL (visto 4 veces el 2026-10-14). | autónomo | Probar `KillSignal=SIGINT` (o `TimeoutStopSec` corto) en la unidad; reinicio solo sin trabajos activos |
| BL-074 | P3 | MAIN a **2.048 MS/s** muestra una línea fija fuerte (+2 dB) a −64 kHz del centro (a 1.8 MS/s +0.2 dB a −49 kHz; a 2.4 MS/s no aparece). Artefacto del muestreo; ALMITA usa 2.4 MS/s. | — | No usar 2.048 MS/s en MAIN sin revisarlo |
| BL-070 | P3 | MONITOR, INSTRUMENT: **carga del sistema del último minuto** (`LOAD 1M`, p. ej. «1.37 / 4 cores») entre RAM y DISK (pedido del operador, 2026-10-14). El CPU que ya se mostraba es el total de los 4 cores (0–100 %, 100 % = los 4 ocupados), tomado en 50 ms. **Hecho, sin commit y sin desplegar:** `almita_console_watcher.py` publica `load_1m` y `cpu_cores`; `console/app.js` lo muestra; test en `tests/test_watcher_operator_stop.py`. **Falta:** reiniciar `almita-console-watcher` y `almita-observe-api` (servicios de sistema) con ninguna observación en curso; no se hizo porque estaba corriendo la sesión `ALMITA-OBSERVE-20261014-14:59:28` | autónomo | que no haya trabajos activos |
| BL-061 | — | MONITOR, SPECTRAL STACK 3D con fondo negro. **Hecho y desplegado el 2026-10-14** (`454c6b3`): fondo negro, marco tenue y extremo bajo de la escala más claro. Visto en una captura de pantalla de MONITOR. | — | — |
| BL-064 | — | SCIENCE: mapas **B y C con la misma forma que A**, y negro donde no hay datos (pedido del operador, 2026-10-14). **Hecho:** B y C se dibujan solo dentro del contorno del tablero medido (el borde exterior de las celdas de A), en vez de un rectángulo completo; todo lo demás va en negro, también en el lienzo de A. Vale para las imágenes de la página, los PNG/SVG/PDF de B y C y la figura combinada A/B/C. Es solo presentación: los valores de B y C y sus archivos de datos no cambian. | Comprobado regenerando en una carpeta temporal los mapas de la reducción real `REDUCE-20261014-004721-315027`. Se aplica a los próximos RUN de SCIENCE; los resultados ya guardados hay que volver a correrlos para verlos así | — |
| BL-067 | — | Línea oscura a 1419.716 MHz en la cascada de la sesión de 625 puntos (pregunta del operador, 2026-10-14). **Causa:** la sesión usó el perfil `WIZARD-20261002-013321-160933`, cuya referencia de 50 Ω tiene una interferencia a 1419.716 MHz (+68 % sobre su entorno, marcada en su propia `spur_mask`); las capturas crudas de la sesión no la tienen, y al dividir por la referencia queda un hueco de −0.8 dB en todas. REDUCE ya descartaba esos canales; QUICKLOOK no. **Hecho:** QUICKLOOK deja fuera los canales que el perfil marca como interferencia (cascada de sesión, stack 3D y valor del mapa); el explorador de perfiles muestra cuántos canales de interferencia tiene cada perfil y avisa cuando son más de lo habitual (el del 2 de octubre: 23, los demás: 6); OBSERVE propone por defecto el perfil compatible más reciente, y si el campo tiene uno más viejo, avisa que existe uno nuevo. El perfil del 2 de octubre no se retiró: es el de la sesión preservada de 400 puntos | Comprobado con los perfiles reales del servidor. Se aplica a las próximas sesiones; las vistas ya guardadas no se regeneran. Todos los perfiles marcan además un canal a 1419.761 MHz, que sí aparece en las capturas crudas (×1.5): una interferencia presente siempre | — |
| BL-066 | — | MONITOR, SPECTRAL STACK 3D: con 625 capturas se veía «una sola muestra» (pregunta del operador, 2026-10-14). **Causa:** la escala abarca todo el rango de los datos, y dos capturas de la sesión nocturna (puntos 72 y 73, a 6–7° de altura, a las 05:11 UTC) tienen +3.3 y +9.2 dB sobre toda la banda, mientras las otras 623 varían ±0.2 dB: quedaban aplastadas en una lámina plana. **Hecho:** las capturas muy distintas del resto se detectan (desvío mediano > máx(1 dB, 8 sigmas robustas)), la escala se toma de las capturas ordinarias, y las atípicas se dibujan recortadas en blanco y se nombran en la leyenda; el botón FULL RANGE vuelve a la escala completa. Las alturas de las capturas ordinarias siguen siendo proporcionales a los dB (BL-019). | Visto con la sesión real `ALMITA-OBSERVE-20261014-04:44:09`: ahora se ve la estructura de las 623 capturas | — |
| BL-063 | — | QUICKLOOK, vista «NATIVE GRID (INTERPOLATED PREVIEW)» con menos interpolación (pedido del operador, 2026-10-14). **Hecho:** ya no rellena todo el polígono entre puntos, solo hasta 0.75 separaciones de grilla alrededor de cada punto real; resolución de unos 4 píxeles por celda en vez de 60×60 fijo; sin suavizado adicional al dibujar. **Bug encontrado y corregido de paso:** la geometría de la grilla leía la columna `ra` del CSV, que está en horas, como si fueran grados, y eso comprimía 15 veces el eje este-oeste de esta vista. El mapa NATIVE GRID exacto no cambia: se dibuja por fila y columna. | Comparado antes y después con las sesiones reales de 100 y 25 puntos del 2026-10-08. Se aplica desde la próxima observación (QUICKLOOK se lanza por sesión); las vistas ya guardadas no se regeneran | — |
| BL-043 | — | **Hecho el 2026-10-15** (`5de9537`, desbloqueado por el operador para la salida a cielo abierto): PIPELINE tiene el mismo checkbox de SYNC y el máximo de offset que ALIGN; la regla de `alignment.py` (confianza ≥ umbral y offset ≤ máximo; PLAN nunca sincroniza) no cambió. Sección ALIGN de PIPELINE sin opción de SYNC: siempre usa `--no-sync`. Junto con esto, decidir si `alignment_engine` adopta la política de SYNC de `alignment.py` (BL-049). | autónomo | BL-026, para no exponer un SYNC sin probar. **No se habilita antes.** |
| BL-050 | — | **Resuelto el 2026-10-15** (módulos descongelados por el operador): `configure()` exige ganancia explícita (`bc2d4db`); un solo lector DS18B20 con CRC (`bc2d4db`); identidad canónica = la campaña, con `session_identity.py` y `last_session.json` guardando `campaign_id` e `iq_session_dir` (`2207491`); lock de montura `mount_lock.py`: el primer movimiento de un proceso reclama la montura (flock), parar nunca lo necesita, la web rechaza un trabajo físico si otro proceso la tiene. Queda el README, que se resuelve con BL-035. Lo que queda de FIELD_RUNBOOK §14: identidad canónica de la sesión; lock del proceso INDI; `gain='auto'` como default en `sdr_capture.configure`; lectores DS18B20 duplicados; `README.md` desactualizado. | Los tres primeros tocan módulos congelados (`capture.py`, `indi_telescope_control.py`, `sdr_capture.py`): decisión del operador. El README conviene resolverlo con BL-035, porque es el único archivo que choca con `main`. Los lectores DS18B20 ya comparten el rango de plausibilidad; unirlos es limpieza sin efecto visible | — |

### 2.5 Iniciativa GESTOR Y VISOR DE SESIONES (BL-079 a BL-093)

Pedido del operador, 2026-10-21. Ninguna tarea de las etapas 1 a 3 necesita hardware: trabajan sobre archivos ya
guardados.

**Estado al 2026-10-21 (pasada nocturna, pedido del operador «desarrolla la cosa»):** etapas 1, 2 y 3 implementadas
(BL-080 a BL-090) y con tests; **no desplegadas**: la página y las rutas nuevas necesitan reiniciar
`almita-observe-api`, y no se reinició nada. Etapa 4 y BL-093 sin empezar. Sin hardware: ni capturas, ni montura, ni
SDR. Archivos: `session_catalog.py`, `almita_web_sessions.py`, `console/sessions.html`, `console/sessions.js`, cambios
aditivos en `almita_orchestrator_server.py` (rutas `/api/sessions/*`, dos archivos estáticos), `console/common.js`
(SESSIONS en el menú), `console/spectral_stack_3d.js` (`showDocument`, clase exportada) y `console/styles.css`
(bloque SESSIONS y el menú que baja de línea bajo 1220 px: con SESSIONS ya no cabía en una línea a 1024 px en ninguna
página). Contratos y decisiones: [SESSIONS_FILE_CONTRACTS.md](SESSIONS_FILE_CONTRACTS.md).

| nivel | estado |
|---|---|
| impl | sí: etapas 1 a 3 |
| tests | sí: `tests/test_session_catalog.py` (14), `tests/test_sessions_web.py` (3), `tests/test_sessions_frontend.py` (1, Chromium), sobre fixtures en `tmp_path` (`tests/session_fixtures.py`). Además pasan, un archivo por proceso: `test_web_frontend.py` (57, con SESSIONS en la prueba de desborde), `test_web_hardening.py` (58, con SESSIONS en las guardias), `test_almita_console.py` (109), `test_orchestrator_web.py`, `test_lobe_web.py`, `test_web_theme.py`, `test_web_auth.py` y las guardias que recorren el repositorio. No se corrió la suite completa |
| datos | sí, en solo lectura: indexar todo el `data/` real toma 0.6 a 0.9 s y 28 MB; con un servidor de prueba en localhost (índice y notas redirigidos al scratchpad, nada escrito en `data/`) se abrieron sesiones reales en todas las pestañas, se dibujó el stack 3D guardado de `ALMITA-OBSERVE-20261018-21:27:05`, el espectro REDUCE de un punto, las temperaturas, y se exportó un informe (10 figuras, 1.5 MB, sin IQ) |
| hardware | — |
| desplegado | **no**: falta reiniciar `almita-observe-api` sin trabajos activos (revisarlo en un paso aparte) |

**Objetivo.** Una página SESIONES desde donde el operador administra las observaciones y abre toda su historia:
parámetros, calibración, capturas, QUICKLOOK, stack 3D, corridas de REDUCE, productos de SCIENCE, temperaturas,
incidencias y archivos. El visor recupera sesiones antiguas sin depender de la observación en curso y sin
recalcular nada al abrirlas.

#### Resumen

| id | etapa | tarea | prioridad | quién | depende de | estado |
|---|---|---|---|---|---|---|
| BL-079 | todas | Paraguas de la iniciativa: reglas comunes y criterios mínimos | P2 | autónomo | — | criterios 1 a 8 cubiertos por tests; falta desplegar |
| BL-080 | 1 | Estudio de los archivos reales y de sus contratos (antes de diseñar el índice) | P2 | autónomo; el operador revisa las decisiones | — | hecho ([SESSIONS_FILE_CONTRACTS.md](SESSIONS_FILE_CONTRACTS.md)); falta que el operador revise las decisiones 4, 5, 7 y 8 |
| BL-081 | 1 | Catálogo: indexador de solo lectura, índice regenerable, tamaño en disco | P2 | autónomo | BL-080 | impl + tests + datos; sin desplegar |
| BL-082 | 1 | Asociaciones con evidencia (perfil, REDUCE, SCIENCE, temperaturas) y «pendiente de asociar» | P2 | autónomo | BL-080, BL-081 | impl + tests + datos (16/16 REDUCE vinculadas, 2 SCIENCE pendientes); sin desplegar |
| BL-083 | 1 | Página SESIONES: lista con búsqueda, filtros, orden y paginación | P2 | autónomo | BL-081 | impl + tests + vista con datos reales; sin desplegar |
| BL-084 | 1 | Visor de una sesión: ocho secciones y estados distinguidos | P2 | autónomo | BL-081, BL-082 | impl + tests + vista con datos reales; sin desplegar |
| BL-085 | 1 | QUICKLOOK y stack 3D históricos, aislados de la sesión en curso | P2 | autónomo | BL-084 | impl + tests + stack real dibujado; sin desplegar |
| BL-086 | 1 | Juego de fixtures y tests del catálogo y las asociaciones | P2 | autónomo | BL-080 | hecho |
| BL-087 | 2 | Anotaciones del operador: nombre, notas, etiquetas, favorito, referencia, descartada, archivada | P2 | autónomo | BL-081 | impl + tests; sin desplegar; sin respaldo de `annotations.json` (decisión 7) |
| BL-088 | 2 | Informe exportable y manifiesto estructurado, sin IQ ni secretos | P2 | autónomo | BL-084, BL-087 | impl + tests + un exporte real (al scratchpad); sin desplegar |
| BL-089 | 3 | Historial de corridas: varias REDUCE por sesión, varias SCIENCE por REDUCE, corrida preferida | P2 | autónomo; el operador elige la preferida | BL-082, BL-087 | impl + tests; sin desplegar |
| BL-090 | 3 | Comparar dos sesiones o dos análisis de una sesión | P3 | autónomo | BL-089 | impl + tests + vista con datos reales: configuración, temperaturas, `config.json`, espectros de un mismo punto superpuestos (dos REDUCE) y mapas A/B/C guardados lado a lado; lo no registrado sale «NOT RECORDED», nunca «same»; sin desplegar |
| BL-091 | 4 | Preparación y cierre de sesión: receptor, instancia de `rtl_tcp`, comprobaciones inicial y final | P2 | autónomo el registro; el operador hace los cambios físicos | BL-084; se vincula con BL-092 | abierto |
| BL-092 | 4 | Comprobación de estabilidad de la cadena (por definir; hoy no existe) | P2 | operador + autónomo | BL-072 | abierto, sin diseño |
| BL-093 | posterior | Administración de espacio: eliminar archivos con vista previa | P3 | operador | BL-087, BL-089 | fuera de la primera versión |

#### BL-079 — Reglas comunes y criterios mínimos

Valen para todas las tareas de la iniciativa:

- **Solo lectura sobre lo guardado.** Indexar o abrir una sesión no mueve, renombra, reescribe ni borra nada en
  `data/` ni en las carpetas preservadas. Las anotaciones del operador se guardan aparte de las capturas.
- **Abrir no ejecuta.** Abrir una sesión no lanza REDUCE, SCIENCE, QUICKLOOK ni trabajos físicos, y no toca la
  montura, los SDR, `rtl_tcp` ni INDI. Indexar tampoco accede a hardware.
- **Lo guardado se muestra como fue guardado.** Si algo hay que reconstruir, es una acción explícita del operador,
  nombra lo que se regenera, lo escribe aparte y conserva el original.
- **Evidencia antes que inferencia.** Condición del cielo, receptor y vínculos científicos solo con evidencia
  registrada; lo que sea deducción se rotula como deducción. La cercanía en el tiempo no basta para asociar.
- **Cuatro estados distintos:** «no ejecutado», «fallido», «archivo faltante» y «metadato desconocido». Una sesión
  incompleta se puede abrir.
- **Memoria del Pi:** carga diferida, paginación y productos resumidos. Nunca cargar el IQ para navegar.
- **Reutilizar** identidades, listados y dibujos existentes (tabla de abajo) en vez de crear otros paralelos.
- **Web:** mismo puerto único 8088, misma autenticación y que siga funcionando a través de Funnel.
- **Motores congelados** (REDUCE V1, SCIENCE V1, `capture.py`) no se modifican para construir el visor.
- **Historia:** se conservan los datos originales y todas las versiones de resultados.

Criterios mínimos de aceptación de la iniciativa (cada uno lo cierra la tarea indicada):

1. Abrir una sesión antigua con sus parámetros y productos disponibles aunque haya otra sesión en curso (BL-084).
2. Explorar su stack 3D sin cambiar el estado de la observación actual (BL-085).
3. Navegar sesiones incompletas o con archivos faltantes (BL-084, BL-086).
4. Mostrar y elegir entre varias reducciones y varias corridas de SCIENCE de una observación, sin mezclarlas
   (BL-089).
5. Nombres, notas y etiquetas sobreviven a reconstruir el índice (BL-087).
6. Indexar varias veces no duplica sesiones ni modifica capturas (BL-081, BL-086).
7. Exportar un informe trazable, sin secretos y sin IQ por defecto (BL-088).
8. Catálogo y asociaciones probados con fixtures, sin hardware y sin escribir en los datos reales (BL-086).

**Lo que ya existe y se reutiliza** (visto el 2026-10-21 leyendo el código y listando `data/`, sin modificar nada):

| pieza | dónde | qué aporta | límite conocido |
|---|---|---|---|
| Identidad canónica | `session_identity.py` (BL-050, `2207491`) | `campaign_id` = carpeta `data/mosaic/<campaign_id>/`; `resolve()` la encuentra desde cualquiera de los tres identificadores | Corregido el 2026-10-21: las 31 campañas con carpeta de captura tienen `session_identity.json`; las otras 24 nunca capturaron (23 sin `data/iq/`, una con `data/iq/` vacío) |
| Listados de campañas | `almita_web_ops.py`: `campaigns()`, `reduce_list_campaigns()`, `_campaign_session_label()`, caché de preparación de REDUCE | Etiqueta legible y preparación para REDUCE | Pensados para elegir entrada de REDUCE: tienen límites de cantidad y de escaneo (`limit`, `scan_budget`) |
| Perfiles | `calibration_profile_catalog.py` (BL-002, BL-015, BL-067) y marcador `INVALIDATED` (2026-10-17) | Perfil, compatibilidad, interferencias marcadas, invalidación | — |
| Stack 3D | `console/spectral_stack_3d.js` (BL-019, BL-061, BL-066) | Dibujo 3D con escala robusta y atípicas | Hoy lo alimenta MONITOR con la sesión en curso (ver decisión 9) |
| Cascada | `quicklook_session_waterfall.py`, `quicklook/session_waterfall.json` por campaña | Cascada ya calculada | Las vistas viejas no tienen las correcciones de BL-063/BL-067 |
| REDUCE | `data/reduced/<objetivo>/REDUCE-*/`: `config.json` (perfil, frame), `points/<n>/master_spectrum.json`, `logs/events.jsonl`, `qc/` | Parámetros, espectros por punto, eventos | La carpeta agrupa por **objetivo** (`via_lactea`), no por campaña (ver decisión 2) |
| SCIENCE | `data/science/<objetivo>/SCIENCE_WEB-*/`: `provenance.json` (`input_reduce_session_dir`, `input_reduce_manifest_sha256`, `config_hash`, `git_commit`), `manifest.json`, `maps/` | Vínculo verificable con su REDUCE y versión de código | — |
| Temperaturas | `temperature_logger.py` → `data/runtime/temperature/` | Registro continuo de SDR, LNA, cable y carga | Es externo a la sesión: hay que mostrar su origen y qué parte de la sesión cubre |
| Parada | `stop_report.json` en la campaña (BL-057) | Cómo terminó una sesión detenida | Solo desde `3bfeb98` |

**Decisiones técnicas.** Respondidas en BL-080 con evidencia: [SESSIONS_FILE_CONTRACTS.md §5](SESSIONS_FILE_CONTRACTS.md).
Resueltas: 1 (los nombres están en UTC), 2, 3, 6, 9, 10 y 12 (REDUCE **sí** guarda `git_commit` en `provenance.json`:
lo que decía abajo era incorrecto). Implementadas, pendientes de revisión del operador: 7 y 8. Abiertas para el
operador: 4 (receptor) y 5 (condición de las sesiones viejas). 11: sin casos reales. Texto original del 2026-10-21:

1. **Hora de los nombres.** `via_lactea-20261018-22:23:11` y `capture_timing_20261018_222343.csv` no dicen su zona
   horaria. Averiguar de qué reloj vienen antes de mostrar «hora de Chile».
2. **Vínculo REDUCE → campaña.** `config.json` de REDUCE guarda el perfil pero no la campaña; la referencia está
   en cada `points/<n>/master_spectrum.json`. Ver si una reducción puede mezclar campañas y qué hacer si pasa.
3. **Estado de la captura.** `observation_runtime.json` queda en RUNNING por diseño (BL-016). Decidir qué fuente
   manda para «terminada / detenida / incompleta» (`stop_report.json`, `mosaic.csv`, `capture_timing_*.csv`, plan
   en `grid_metadata.json`) y cómo se cuentan puntos logrados contra planificados.
4. **Receptor.** El serial `00000001` fue del V4 hasta el 2026-10-17 y desde entonces es el V3 regrabado: el
   serial solo no identifica el receptor. Hace falta un registro de cambios de la cadena con fecha, y lo que salga
   de ahí se rotula «documentado por el operador», no «medido».
5. **Condición (cielo abierto / interior / simulador).** Ninguna sesión vieja la registra de forma uniforme.
   Fuentes posibles: el selector de montura LX200/SIM (desde 2026-10-17), el guard `INDOOR_MODE` del banco forense,
   §4.0 de este backlog y la anotación del operador. Sin fuente: «desconocida».
6. **Tamaño en disco.** Elegir el método (bloques reales en disco; enlaces duros contados una vez por inodo;
   perfiles y referencias compartidas mostrados aparte y no sumados a cada sesión) y su costo en el Pi (caché por
   fecha de modificación). La página tiene que decir cómo se calculó.
7. **Dónde viven el índice y las anotaciones.** El índice es descartable y se regenera; las anotaciones no.
   Definir ubicación (`data/` está fuera de Git), formato, respaldo, clave (`campaign_id`) y qué pasa con una
   anotación cuya carpeta ya no existe (se conserva, marcada huérfana).
8. **Qué cuenta como sesión.** Además de `data/mosaic/` hay `data/calibration/WIZARD-*`, `data/alignment/`,
   `data/lobe/`, `data/bench/`, `data/hardware_tests/` y `/home/stellarmate/almita_preserve_20261002/` (fuera del
   repositorio). Decidir cuáles son sesiones del catálogo, cuáles productos vinculados y cuáles quedan fuera en la
   primera versión. Los restos de tests (`TEST-STOP-*` en `data/mosaic/`, los `SOLAR-*` de BL-055) se rotulan como
   prueba; no se ocultan ni se borran.
9. **Stack 3D histórico.** Ver si el dibujo actual acepta una fuente por campaña sin pasar por el estado global
   de la sesión en curso (`current_session.json`, `last_session.json`, el visor en vivo de QUICKLOOK).
10. **HDF5.** Si el visor necesita atributos de las capturas (ganancia, `rtl_agc` de BL-076, temperaturas por
    punto), medir cuánto cuesta abrirlas en el Pi y leer solo atributos, nunca el IQ.
11. **Campañas con varias carpetas IQ** (reanudaciones): cómo mostrarlas sin mezclarlas.
12. **Reejecuciones.** Confirmar que REDUCE y SCIENCE ya crean una carpeta nueva por corrida (los IDs con fecha lo
    sugieren) y que nada se sobrescribe. REDUCE no guarda la versión de código; SCIENCE sí (`git_commit`).

#### Etapa 1 — Catálogo y visor histórico

**BL-080 — Estudio de los archivos reales.** Recorrer en solo lectura cada familia de sesiones y de productos y
escribir sus contratos reales: qué archivos hay, cuáles son obligatorios, qué campos llevan, desde qué fecha o
commit existen y qué identificadores enlazan unos con otros. Responder las decisiones 1 a 12 con evidencia o
dejarlas marcadas como no resueltas.
*Acepta cuando:* hay un documento en `docs/` con el contrato de cada familia y la respuesta (o la falta de
respuesta) a cada decisión, citando ejemplos reales; no se escribió nada en `data/`; el operador revisó las
decisiones 4, 5, 7 y 8.

**BL-081 — Catálogo.** Indexador de solo lectura que descubre las sesiones con la identidad canónica y produce un
índice regenerable con: identidad, nombre descriptivo, fecha en hora de Chile, estado de captura, puntos
logrados/planificados, estado de calibración, REDUCE y SCIENCE, receptor (si está documentado), condición, tamaño
en disco y método de cálculo.
*Acepta cuando:* indexar dos veces seguidas da el mismo índice, sin sesiones duplicadas; las capturas no cambian
(foto de archivos, tamaños y fechas antes y después, en fixtures); una sesión que no se puede leer entera aparece
igual, con lo que falta marcado; el indexador nunca abre el IQ.

**BL-082 — Asociaciones.** Enlazar sesión ↔ perfil, sesión ↔ REDUCE, REDUCE ↔ SCIENCE y sesión ↔ registro de
temperaturas, cada vínculo con su evidencia (archivo y campo). Lo ambiguo queda como «pendiente de asociar».
*Acepta cuando:* cada vínculo mostrado nombra su evidencia; dos campañas cercanas en el tiempo sin referencia
explícita no se asocian; una SCIENCE cuyo `input_reduce_manifest_sha256` no coincide con su REDUCE queda marcada
como inconsistente, no asociada en silencio; las temperaturas externas muestran origen y qué parte de la sesión
cubren.

**BL-083 — Página SESIONES.** Página nueva en la consola, con búsqueda, filtros (estado, condición, receptor,
etapas hechas, etiquetas cuando existan) y orden, paginada.
*Acepta cuando:* con las 55 campañas actuales la página carga sin leer cada sesión entera; funciona en :8088 con
la autenticación actual y a través de Funnel; abrirla no cambia nada en STATUS, MONITOR ni la sesión en curso.

**BL-084 — Visor de una sesión.** Ocho secciones:

1. **Resumen:** objetivo, notas, condiciones, duración, etapas e incidencias.
2. **Observación:** parámetros pedidos y efectivos; frecuencia, muestreo, ganancia, integración, sitio, grilla,
   giro, coordenadas y recorrido.
3. **Calibración:** perfil usado, compatibilidad, referencias e invalidaciones.
4. **Capturas / QUICKLOOK:** ver BL-085.
5. **REDUCE:** parámetros, perfil, frame de velocidades, máscaras, incertidumbres, avisos, informes y espectros
   guardados.
6. **SCIENCE:** mapas A/B/C, cobertura, incertidumbre, validaciones y exportes disponibles. Un SCIENCE rechazado
   (por ejemplo una grilla girada, BL-077) se muestra como rechazado, no como fallido.
7. **Instrumento:** temperaturas e incidencias registradas en la sesión, con su origen si vienen de un registro
   externo. Un punto sin temperatura del SDR (como en BL-069) es «metadato desconocido».
8. **Archivos:** inventario, tamaños, ubicaciones y descarga de productos.

*Acepta cuando:* se abre una sesión antigua mientras otra está en curso y ninguna de las dos cambia; cada sección
distingue los cuatro estados de BL-079; una sesión con archivos borrados a propósito (fixture) se abre y dice qué
falta; la descarga no ofrece archivos de autenticación ni de configuración con secretos.

**BL-085 — QUICKLOOK y stack 3D históricos.** Espectros, cascada, mapas y stack 3D interactivo de una sesión
guardada, con el dibujo existente. Elegir un punto muestra sus metadatos y qué productos tiene.
*Acepta cuando:* explorar el stack 3D de una sesión antigua no cambia `current_session.json`, `last_session.json`
ni lo que muestra MONITOR; los productos guardados se ven como se generaron (si son anteriores a BL-063/BL-067 se
dice); regenerar una vista es una acción aparte, escribe en otra ubicación y conserva la original.

**BL-086 — Fixtures y tests.** Fixtures pequeñas en `tests/`, armadas a partir de los contratos de BL-080: sesión
completa, detenida, incompleta, con archivos faltantes, sin `session_identity.json`, con varias REDUCE y varias
SCIENCE, con una SCIENCE de manifiesto distinto y una sesión de prueba (`TEST-STOP-*`).
*Acepta cuando:* los tests trabajan en `tmp_path`, no leen ni escriben `data/` (misma regla que BL-009/BL-046) y
no tocan hardware; cubren los criterios 3, 6 y 8 de BL-079.

#### Etapa 2 — Organización e informe exportable

**BL-087 — Anotaciones del operador.** Nombre descriptivo, notas y etiquetas editables; favorito y marca de
referencia; «descartada para el análisis» con motivo (los datos se conservan); archivar y desarchivar en el
catálogo, sin mover archivos. Todo se guarda aparte de las capturas (decisión 7).
*Acepta cuando:* borrar y reconstruir el índice conserva todas las anotaciones; ninguna anotación cambia un archivo
de captura ni de producto; una sesión descartada sigue abriéndose y muestra el motivo.

**BL-088 — Informe exportable.** Un informe legible y un manifiesto estructurado con identidad, configuración,
notas, resultados, figuras y avisos, para compartir evidencia con otro colaborador sin pasar toda la captura.
*Acepta cuando:* por defecto no incluye IQ; nunca incluye credenciales, tokens ni archivos de autenticación (test
que lo busca en la salida); cada resultado del informe nombra la corrida y el archivo de donde sale; generar el
informe solo lo deja disponible para descargar: no lo publica ni lo sube a GitHub.

#### Etapa 3 — Historial de análisis y comparación

**BL-089 — Historial de corridas.** Una observación puede tener varias REDUCE y cada REDUCE varias SCIENCE. Cada
corrida muestra identidad, entradas, parámetros, perfil, versión de código cuando exista y salidas. Se puede marcar
una corrida como preferida sin borrar las otras (la marca es una anotación, BL-087). Volver a correr crea un
resultado aparte.
*Acepta cuando:* con una fixture de dos REDUCE y tres SCIENCE, cada SCIENCE aparece bajo su REDUCE y elegir una no
mezcla mapas ni espectros de otra; cambiar la preferida no modifica ningún archivo de resultado; está confirmado
(decisión 12) que una nueva corrida no sobrescribe la anterior.

**BL-090 — Comparación.** Dos sesiones, o dos análisis de una misma sesión: configuración, temperaturas, espectros
y mapas, con escalas y unidades compatibles.
*Acepta cuando:* las diferencias que impiden comparar directo (frecuencia central, muestreo, ganancia, perfil,
frame de velocidades, receptor, grilla o giro distintos) se señalan antes de mostrar la comparación; no se
reescala en silencio.

#### Etapa 4 — Preparación y cierre de sesión

**BL-091 — Preparación y cierre.** Registrar por sesión: receptor e instancia de `rtl_tcp` (puerto, opción `-T`,
hora de arranque), frecuencia, muestreo y ganancia; referencias de carga y perfil asociados; comprobaciones iniciales
y finales (por ejemplo el rms después de arrancar `rtl_tcp`, que ya detectó un SDR mudo el 2026-10-16, y el AGC
apagado de BL-076). Avisar cuando un reinicio o un cambio documentado de la cadena obliga a revisar la calibración
(como el cambio de V4 a V3 del 2026-10-17, que invalidó los perfiles).
*Acepta cuando:* el visor muestra la deriva y las diferencias medidas entre el inicio y el cierre **sin inventar
umbrales de aceptación**; no se corrige ninguna captura por temperatura sin un modelo validado; abrir el gestor no
reconecta, reinicia, sintoniza ni mueve nada. Los cambios físicos siguen necesitando la confirmación del operador.

**BL-092 — Comprobación de estabilidad de la cadena (por definir).** No existe todavía. Se anota para que BL-091
tenga dónde engancharse. Insumos conocidos: la raya móvil de MAIN que depende de las temperaturas del SDR y del
LNA (BL-072), el Allan de potencia total del V3 (~0.1, sin ganancia por integración, 2026-10-17), el conector
intermitente del LNA (2026-10-18) y la carga casera con el sensor reubicado (2026-10-17).
*Acepta cuando:* el operador aprobó un procedimiento escrito (qué se mide, cuánto dura, con qué carga). Sus
umbrales salen de mediciones, no se fijan de antemano.

#### Posterior — Administración de espacio

**BL-093 — Eliminar archivos.** Tres acciones distintas, que la interfaz no mezcla: **archivar en el catálogo**
(BL-087), **descartar para el análisis** (BL-087) y **eliminar archivos** (esta tarea). La eliminación queda fuera
de la primera versión.
*Acepta cuando* (antes de implementar): hay un diseño aprobado por el operador con vista previa de los archivos
afectados, sus dependencias (perfiles, REDUCE y SCIENCE que los usan), el espacio que se recupera y una
confirmación explícita. Las sesiones preservadas (por ejemplo la de 400 puntos del 2026-10-02 y su perfil, BL-067,
y `/home/stellarmate/almita_preserve_20261002/`, BL-042) y las referencias compartidas quedan protegidas.

**Vínculos con otros pedidos:** BL-050 (identidad canónica), BL-016 (RUNNING por diseño), BL-002/BL-015/BL-067
(perfiles), BL-019/BL-061/BL-066 (stack 3D), BL-063 (vista NATIVE GRID), BL-064 y BL-077 (mapas de SCIENCE),
BL-020 (logs de REDUCE y SCIENCE), BL-037 (RFI REF: el visor no asocia lo que REDUCE no asocia), BL-055 y BL-042
(restos de tests y carpetas preservadas), BL-069 (temperatura faltante), BL-072 y BL-076 (estabilidad y AGC),
BL-040 (la web es alcanzable desde Internet: la descarga y el informe no pueden exponer secretos).

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
- Iniciativa SESIONES (§2.5): BL-080 antes que todo lo demás; BL-081 antes de BL-082 a BL-085 y de BL-087;
  BL-087 antes de BL-088 y BL-089; BL-089 antes de BL-090; BL-072 antes de BL-092. BL-091 se vincula con BL-092 pero no lo espera.
  No necesita hardware hasta la etapa 4.

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
| **STOP OBSERVATION durante un GOTO** | FAIL el 2026-10-13: la montura quedó en seguimiento (§4.2) | **PASS en la montura real el 2026-10-14 02:22 (§4.11)**, tras un intento fallido a las 02:13 que llevó a una segunda corrección (§4.10) |
| STOP CURRENT STEP del wizard, tres casos | PASS los tres (§4.4 a §4.6) | El wizard ahora también aborta el GOTO; sin repetir |
| ALIGN, gain pilot | Sin prueba de parada en hardware | Usan la misma parada confirmada; sin probar |
| PARAR MONTURA (web) | No existía | **PASS con la montura en seguimiento** (2026-10-14 02:19: confirmado en 9.4 s). Sin probar durante un GOTO ni con un trabajo corriendo |

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

## 4.10 STOP OBSERVATION durante un GOTO, primer intento con la corrección (2026-10-14 02:13, bajo techo)

| | |
|---|---|
| Sesión | `data/mosaic/ALMITA-OBSERVE-20261014-02:11:15` |
| STOP | 02:13:03 UTC, durante el GOTO del punto 2 (log: «MOTION START», «GOTO cancelado») |
| Montura | **Se detuvo de inmediato:** orden de abortar aceptada, «no moviéndose» 0.8 s después. Seguimiento apagado en 3 lecturas seguidas. **Segundos más tarde el seguimiento volvió a encenderse solo**; la comprobación de posición midió +21 s de RA en 5.7 s y el código se rindió sin reapagarlo. Lectura posterior: montura en seguimiento |
| Lo que dijo el sistema | **«PARADA NO CONFIRMADA»** con ese motivo en el log, en `stop_report.json`, en OBSERVE (estado BLOCKED) y en MONITOR. No declaró éxito |
| Datos | 1 `.h5` completo; nada del punto 2; ningún `.part` |
| Resultado | **FAIL en la montura, informado correctamente.** Llevó a `1bcd07e`: si la comprobación falla, se relee el seguimiento, se reapaga y se repite la prueba (hasta 4 veces) |

## 4.11 STOP OBSERVATION durante un GOTO, con la segunda corrección (2026-10-14 02:22, bajo techo)

| | |
|---|---|
| Sesión | `data/mosaic/ALMITA-OBSERVE-20261014-02:20:15` |
| STOP | 02:22:51.98 UTC, durante el GOTO del punto 2 |
| Parada de la montura | **CONFIRMADA en 17.9 s.** Abortar aceptado; «no moviéndose» en el acto; seguimiento apagado ×3; primera comprobación de posición fallida (+22 s de RA en 5.7 s); **seguimiento encontrado encendido otra vez y reapagado**; segunda comprobación: +6.0 s de RA y Dec fija en 5.7 s, montura quieta. Una reactivación |
| Vigilancia posterior (lecturas cada 13 s hasta las 02:24:34, sin enviar órdenes) | Seguimiento apagado todo el tiempo, RA avanzando al ritmo sidéreo (+53 s en 53 s), sin error en OnStep. El operador no tocó Ekos |
| Estados | `ABORTED` en el orquestador, la API (nota «MOUNT STOP CONFIRMED … after 1 late reactivation») y MONITOR. Respuesta del STOP en 24 s |
| Datos y recursos | 1 `.h5` completo y validado; nada del punto 2; ningún `.part`; MAIN liberado a las 02:23:10; RFI REF cerrado a las 02:23:15 |
| Resultado | **PASS** |

Sin determinar: quién vuelve a encender el seguimiento después de abortar (OnStep o Ekos) y por qué la RA se mueve
unos 22 s en ese lapso. Pasó igual las dos veces, una sola reactivación cada vez.

**PARAR MONTURA**, 2026-10-14 02:19 UTC: pulsado con la montura en seguimiento tras el intento fallido. Resultado en
el journal del servidor: «MOUNT STOP CONFIRMED (9.43 s): not slewing, tracking OFF, STATIONARY», sin trabajos que
detener. El operador vio apagarse el seguimiento.

## 5. Tests

- **Corrida completa vigente, 2026-10-15** (sobre `a2059d0`, un proceso de pytest por archivo por la memoria del
  Pi): **173 archivos, 2634 passed, 0 failed, 54 skipped.** Se repitió porque cambiaron `sdr_tuning.py`,
  `sdr_capture.py`, `indi_telescope_control.py` (lock de montura), `alignment.py`, el wizard y el lector de
  temperatura. Durante la noche se corrigieron dos fallas propias antes de esta corrida: el banco de la raya tenía
  la frecuencia escrita a mano (regla de una sola fuente) y el hash revisado de `goto`/`set_tracking` cambió con el lock.
- **Corrida anterior, 2026-10-14** (01:58 a 02:40 UTC, 42 min 37 s, iniciada sobre `527ef32`): 2589
  passed, 4 failed, 53 skipped. Se repitió la suite completa porque cambió `capture.py`. Las cuatro fallas, todas
  corregidas después y vueltas a correr (112 passed, 3 skipped en esas tres suites):
  - dos en `test_quicklook_native_grid_map.py`: esperaban la ascensión recta de la grilla sin convertir de horas
    a grados. Era el error corregido en BL-063; faltaba actualizar estas dos expectativas, que no estaban en las
    suites que corrí entonces;
  - `test_frozen_modules_untouched`: una segunda guardia de módulos congelados, con sus propios pins, que no se
    había actualizado. Ahora lee los mismos pins que la herramienta de campo, para que no puedan divergir;
  - `test_frozen_surface_unchanged_against_pins`: falló porque durante la corrida se commiteó un cambio de
    `capture.py` (el recuento del informe de parada). Pasa con el pin actual.
- **La corrida coincidió con sesiones reales del operador.** El listado de `data/` de antes y después difiere solo
  en lo que esas sesiones, un SCIENCE y dos reinicios del servidor escribieron; ningún cambio viene de los tests.
- **Pruebas de parada con procesos y señales reales** (montura INDI simulada que sigue moviéndose y siguiendo):
  `test_capture_stop_integration.py` 10 passed; `test_mount_stop_integration.py` 17 passed.
- **Después de la corrida completa** cambiaron `mount_stop.py` (repetición de la prueba de parada) y el recuento
  del informe en `capture.py`; se corrieron sus suites, no la completa.
- Corridas completas anteriores: 2026-10-13, 2555 passed, 2 failed; 2026-10-12, 2540 passed, 3 failed.

Detalle en [PROJECT_BRIEF.md §5](PROJECT_BRIEF.md#5-validación).
