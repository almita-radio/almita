# ALMITA backlog

Pedidos del operador y su estado real. Revisado contra el código, los commits, los servicios instalados y `data/`
el 2026-10-12. Contexto general: [PROJECT_BRIEF.md](PROJECT_BRIEF.md).

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

Todos están implementados, y todos menos una parte de BL-025 están desplegados: el servidor web arrancó con el
reinicio del Pi del 2026-10-08 a las 20:50 UTC, después de todos esos commits.

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
| BL-009 | Tests sin dependencia de `data/` | fdc72e9, 8473f0c | sí: no leen `data/` | — | — | reabierto en parte: un test todavía escribe ahí (BL-046) |
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
| BL-025 | RFI REF: Bias-T encendido por defecto | b6eca65 | sí | — | parcial: `-T` enviado en 6 sesiones reales; alimentación del LNA sin medir | abierto: BL-031 (medición), BL-044 (despliegue) |

Detalle de BL-025: la consola (HTML y JS) está activa. El cambio de `almita_orchestrator_server.py` no está cargado,
porque el archivo se editó después del arranque del servidor. No cambia lo que ve el operador: sin ese dato, el
formulario usa la casilla ya marcada del HTML.

## 2. Abierto

Prioridades: **P0** riesgo activo, se decide ya. **P1** bloquea confiar en el producto en terreno. **P2** mejora
la operación. **P3** limpieza.

«Quién»: **operador** necesita a alguien junto al instrumento o una decisión; **autónomo** se puede hacer sin
hardware y sin consultar.

### 2.1 Bugs

| id | prioridad | qué | evidencia | quién | falta para cerrar |
|---|---|---|---|---|---|
| BL-045 | P1 | El pie de página dice «HTTP (LAN, no TLS, no authentication)». El servidor sí exige Basic Auth y además recibe tráfico de Internet (BL-040). | `almita_web_system.version_info`; journal del 2026-10-10 al 12 | autónomo | Corregir el texto, test, y reiniciar el servidor (BL-044) |
| BL-046 | P2 | Un test escribe en el `data/` real: cada corrida deja una carpeta `data/alignment/SOLAR-<fecha>` en `PREFLIGHT_FAILED`. Contradice BL-009. | `SOLAR-20261006-010009`, `SOLAR-20261008-191903` y `SOLAR-20261012-231035`, creada por la corrida de esta revisión, que además tocó las fechas de `data/calibration` y `data/IQ` sin dejar archivos; `tracking_timeout_s: 0.05` solo existe en `tests/test_alignment_engine_end_to_end.py` | autónomo | Redirigir `output_root` a `tmp_path`; decidir con el operador si se borran las carpetas ya creadas |
| BL-051 | P2 | `test_no_frequency_literal_in_operational_python` falla siempre desde fd8a962: los tres scripts de evidencia versionados entonces (`rf_chain_characterization.py`, `rf_gain_control_test.py`, `sun_detectability_test.py`) llevan el literal `1420405752`. | Corrida completa del 2026-10-12; falla también aislado | autónomo | Leer la frecuencia de `sdr_tuning` en esos scripts, o excluirlos de la regla con una lista explícita |
| BL-047 | P3 | Tests que fallan solo en la corrida completa: `test_run_exits_on_its_own_once_capture_reports_the_session_ended` (otro test deja `quicklook_live.STOP_REQUESTED` activado) y `test_field_console_left_open_for_30_minutes…` (sensible al reloj con el Pi cargado). | Corrida completa del 2026-10-12: fallan; aislados pasan | autónomo | Restaurar el flag en un fixture; hora simulada |
| BL-048 | P3 | Al abrir OBSERVE sin pilot planificado, el trabajo `observe_gain_pilot_admin status` termina con código 1 («plan first») y queda en la lista de trabajos. | `OBSERVE_GAIN_PILOT_ADMIN-20261008-210419-d82e` y `…-223450-9c9f` | autónomo | Confirmar cómo se muestra en la web (no verificado) y tratarlo como «sin pilot», no como falla |

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
| BL-040 | **P0** | **La web :8088 es alcanzable desde Internet.** | Del 2026-10-10 al 12 el journal registra 57 direcciones públicas distintas; 76 pedidos de escáneres rechazados con 401 y una sesión autenticada desde una IP pública el 2026-10-11 16:55 UTC. La contraseña viaja por HTTP sin cifrar (Basic Auth). Esa web puede mover la montura. No se verificó si otros puertos (22, 5900, 6080, 445) también están reenviados. | Confirmar que la sesión del 11 fue de ustedes. Cerrar el reenvío de puertos del router y entrar por VPN (Tailscale o WireGuard). Si fue ajena, cambiar la contraseña. |
| BL-035 | P1 | Unir `web-polish-v1` con `main`. | `main` tiene 19 commits de documentación y sitio que la rama no tiene; la rama tiene más de 80 que `main` no tiene. Solo chocan en `README.md`. | Un PR `web-polish-v1` → `main` resolviendo el README, después de BL-026 y BL-027 |
| BL-041 | P2 | `rfi_ref.bias_tee` vale `false` por omisión en el esquema (`observation_spec.py`, módulo congelado). | La web manda `true`. Una especificación YAML por CLI que no lo escriba deja el LNA de la antena B sin alimentación. `examples/overnight-400.yaml` lo trae en `true`; `examples/block-validation-01.yaml` tiene RFI REF desactivado. | Mantener el esquema (los planes archivados revalidan igual) y dejarlo advertido en el manual, que ya lo dice |
| BL-036 | P3 | REDUCE topocentric sin metadatos. | Exige descongelar `reduce_engine`. Hoy solo se corrigió la etiqueta. | No hacerlo mientras SCIENCE solo acepte LSRK |
| BL-037 | P2 | RFI REF: asociar las capturas de la antena B a cada punto en REDUCE. | La interfaz existe (`docs/REDUCE_RFI_REF_INTERFACE.md`) y hoy informa UNAVAILABLE. Toca REDUCE V1 congelado. | Decidir después de BL-031: no tiene sentido sin saber que la antena B mide bien |
| BL-038 | P3 | Subir un perfil desde el disco del navegador (resto de BL-002). | Hay que decidir dónde se guarda y quién puede escribir. | Posponer |
| BL-039 | P3 | Modelo de horizonte. | CALIBRATE muestra «OBSTACLES NOT EVALUATED». Necesita datos del sitio. | Posponer hasta tener sitio fijo |
| BL-042 | P3 | Archivos locales fuera de Git: scripts de investigación de agosto, respaldos `*_ultima_revision.tar.gz`, el archivo vacío `=.9`, `/home/stellarmate/almita_preserve_20261002/` (6.4 GB) y las ramas `backup-antes-de-limpiar-github-20260810` y `respaldo-local-18-03`. | Lista y motivo de cada uno en PROJECT_BRIEF §6. Nada se borró ni se subió. | Mover los scripts a `scripts/legacy/` si se quieren conservar en Git; el resto queda local hasta que el operador diga |

### 2.4 Mejoras

| id | prioridad | mejora | quién | depende de |
|---|---|---|---|---|
| BL-044 | P2 | Reiniciar `almita-observe-api` para cargar b6eca65 y lo que siga. Solo sin trabajos activos, comprobado en un paso aparte. | operador (autoriza) | Conviene juntarlo con BL-045 |
| BL-043 | P2 | Sección ALIGN de PIPELINE sin opción de SYNC: siempre usa `--no-sync`. | autónomo | BL-026, para no exponer un SYNC sin probar |
| BL-049 | P3 | Política vieja de SYNC en `alignment_engine/sync_flow.py` y `targets/hi_reference.py`, que bloquea el SYNC en HI. Alinear el código o la documentación con BL-022. | autónomo | BL-026 |
| BL-050 | P3 | Pendientes de FIELD_RUNBOOK §14: identidad canónica de la sesión; lock del proceso INDI; `gain='auto'` como default en `sdr_capture.configure`; lectores DS18B20 duplicados; `README.md` y `docs/history/FLUJO_COMPLETO.md` desactualizados; Dec negativa en la CLI de `mount_control.py`. | autónomo, salvo los que tocan módulos congelados | — |

## 3. Orden propuesto

1. **BL-040.** Es la única con riesgo activo y no depende de nada. Diez minutos del operador en el router.
2. **Sin hardware, en paralelo:** BL-045, BL-046 y BL-051, y BL-047 si queda tiempo. Dejan la suite en verde y
   el árbol listo para un solo reinicio.
3. **BL-044:** un reinicio del servidor, autorizado por el operador, que carga b6eca65 y BL-045.
4. **Salida a terreno de día:** BL-031 (sin mover la montura) → BL-026 (ALIGN SOLAR y SYNC) → BL-027 (STOP real)
   → BL-029. Hacerla siguiendo el manual cubre BL-033.
5. **Con el Sol ya medido:** BL-034 (haz). Después BL-043 y BL-049.
6. **Noche siguiente:** BL-028 y BL-032 dentro de una sesión normal.
7. **BL-035** (PR a `main`) cuando BL-026 y BL-027 estén cerrados.
8. El resto (BL-030, BL-036 a BL-039, BL-041, BL-042, BL-048, BL-050) no bloquea a nadie.

Dependencias que fijan el orden:

- BL-026 antes de BL-034, BL-043 y BL-049.
- BL-031 antes de BL-037.
- BL-045 antes de BL-044, para reiniciar una sola vez.
- BL-026 y BL-027 antes de BL-035.

## 4. Activación

- `console/` se sirve en vivo.
- Los cambios de Python que importa el servidor necesitan reiniciar `almita-observe-api`, y solo sin trabajos
  activos (revisarlo en un paso aparte).
- Los scripts que lanza la web se cargan en cada trabajo.

## 5. Tests

Corrida completa del 2026-10-12 sobre b6eca65: 2540 passed, 3 failed, 52 skipped, en 33 min 29 s. Las tres
fallas son BL-051 (real) y BL-047 (dos que pasan aisladas). Detalle en
[PROJECT_BRIEF.md §5](PROJECT_BRIEF.md#5-validación).
