# rtl_tcp como servicio systemd

Esta configuración mantiene `rtl_tcp` escuchando sólo en la interfaz local
`127.0.0.1:1234` y lo reinicia automáticamente si el proceso termina.

## Unidad de servicio

Crear `/etc/systemd/system/rtl_tcp.service` con este contenido:

```ini
[Unit]
Description=RTL-SDR TCP server for ALMITA
After=network.target

[Service]
Type=simple
User=stellarmate
Group=stellarmate
ExecStart=/usr/bin/rtl_tcp -a 127.0.0.1 -p 1234 -f 1420405000 -s 2400000 -g 40.2 -T
Restart=always
RestartSec=3
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
```

Antes de habilitar la unidad, cerrar cualquier instancia manual de `rtl_tcp`
para evitar que dos procesos compitan por el SDR o por el puerto 1234.

## Activación

Después de crear o modificar la unidad:

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now rtl_tcp
sudo systemctl status rtl_tcp
```

`enable --now` configura el inicio automático durante el arranque y levanta el
servicio inmediatamente.

## Logs y verificación

Seguir el journal del servicio en vivo:

```bash
journalctl -u rtl_tcp -f
```

Confirmar que el servidor escucha en TCP/1234:

```bash
ss -lntp | grep 1234
```

El resultado esperado contiene un socket `LISTEN` en `127.0.0.1:1234`. Si el
servicio falla, revisar primero el journal por errores de acceso al dispositivo
USB, interfaz ocupada o puerto ya utilizado.

## RFI_REF: secundario desechable (V3) en paralelo con MAIN

MAIN (`rtl_tcp.service`, RTL-SDR Blog V4, serial `00000001`, `127.0.0.1:1234`)
es la única fuente autoritativa para Capture. Un segundo RTL-SDR (V3/R820T,
serial `00000002`) puede usarse como referencia de RFI en paralelo ("RFI_REF"),
pero **nunca** como parte de `rtl_tcp.service` ni de la ruta científica: se
lanza como proceso `rtl_tcp` temporal e independiente en `127.0.0.1:1235`, y
una falla, atraso o crash en él no debe poder bloquear, pausar ni alterar la
adquisición de MAIN.

Esta arquitectura fue validada en hardware con
[`dual_sdr_benchmark.py`](dual_sdr_benchmark.py):

```bash
python3 dual_sdr_benchmark.py --duration 180
```

El script nunca reinicia ni modifica `rtl_tcp.service`; lanza RFI_REF como
subproceso propio, verifica por PID (vía `ss -lntp`) que el socket de
`127.0.0.1:1235` pertenece realmente al subproceso que lanzó (para no
consumir por error un `rtl_tcp` manual preexistente en ese puerto), y aísla
cualquier falla del lado de RFI_REF de MAIN con
`asyncio.gather(..., return_exceptions=True)`. El quicklook FFT de RFI_REF
corre en un thread separado para no bloquear el loop que drena el socket de
MAIN. No persiste IQ completo de RFI_REF a propósito — es un sidecar de
referencia, no una fuente de datos científicos, por lo que un quicklook FFT
liviano (~5% de los bloques, sin guardar IQ) es suficiente para caracterizar
RFI sin agregar carga de I/O.

Resultado validado (corrida de 180s baseline + 180s dual, MAIN a 1420405000 Hz
/ 2.4 MS/s / gain 40.2 dB / Bias-T ON; RFI_REF a 1420405000 Hz / 2.4 MS/s /
gain 25 dB):

- ratio de throughput MAIN dual/baseline: 0.99999
- pérdidas/errores/discontinuidades en MAIN: 0
- resets USB: 0
- CPU promedio del sistema durante dual: 2.75%
- RAM usada: 23.3%
- duty del FFT quicklook de RFI_REF: ~5% de los bloques
- pérdidas en RFI_REF: 0

La evidencia completa (CSV por bloque, muestreo de recursos, logs, JSON) de
cada corrida se guarda bajo `data/dual_sdr_bench/<run_id>/` y no se versiona
en git (ver `.gitignore`) — es evidencia de runtime, no un artefacto de
código.

## RFI_REF integrado en Capture: sidecar opcional y desechable

El benchmark standalone de arriba valida el par rtl_tcp MAIN+RFI_REF de forma
aislada. [`rfi_monitor.py`](rfi_monitor.py) (`RFIReferenceMonitor`) lleva esa
misma arquitectura — verificación de ownership del socket por PID, subproceso
propio nunca gestionado por `rtl_tcp.service`, FFT quicklook en thread aparte,
sin persistir IQ — dentro de `capture.py`, como un sidecar real de campo.

**Regla de arquitectura no negociable:** MAIN es la única fuente científica
autoritativa. RFI_REF es opcional y desechable — una falla, crash, bind error,
device ausente, atraso de procesamiento o salida malformada en RFI_REF nunca
debe bloquear, pausar, ni alterar MAIN: ni su ganancia, ni `rtl_tcp.service`,
ni el HDF5 científico, ni el comportamiento de mount/GOTO, ni abortar la
sesión. RFI_REF es un monitor de diagnóstico, **no** un cancelador activo de
interferencia — no se resta ni se mezcla con la IQ de MAIN en esta etapa.

Activación (por defecto deshabilitado — comportamiento existente sin cambios):

```bash
python3 capture.py --csv <plan.csv> --settle 5 --capture 10 \
  --rfi-ref-enabled --rfi-ref-gain-db 25.0 --runtime-dir data/runtime
```

`--rfi-ref-gain-db` es independiente de `--sdr-gain` (nunca reutiliza la
ganancia de MAIN implícitamente). `--rfi-ref-port` (default `1235`) y
`--rfi-ref-serial` (default `00000002`) también son configurables; la
frecuencia y el sample rate de RFI_REF siempre replican los de MAIN
(`--sdr-freq`/`--sdr-rate`), para coincidir exactamente por diseño.

Capture inicia el sidecar como tarea de fondo (`asyncio.create_task`, nunca
bloqueante) justo después de que el SDR de MAIN queda conectado, y lo detiene
en el `finally` de `execute_observation_plan` — cubre toda salida (sesión
completa, pausada, abortada, `KeyboardInterrupt`). El estado de RFI_REF se
publica en `data/runtime/rfi_ref_status.json`, asociado al mismo `session_id`
canónico que ya usa Capture (nunca una segunda identidad de sesión), y el
watcher (`almita_console_watcher.py`) lo pliega dentro de `almita_status.json`
bajo la clave `rfi_ref`; un archivo ausente, de una sesión vieja, o sin el
campo (sesiones anteriores a esta integración) siempre se interpreta como
`DISABLED`, nunca como error. La Console local (`console/`) agrega un panel
**RFI REFERENCE** con estado/serial/ganancia/duty/occupancy/clipping/peak/
última actualización; una falla de RFI_REF nunca cambia el estado de la
sesión de MAIN a `ABORTED`.

### Validación de hardware "SATANIC"

Además del benchmark standalone, se ejecutó una validación de hardware
adversarial usando las clases reales de producción (`SDRCapture` de MAIN y
`RFIReferenceMonitor`) contra `rtl_tcp.service` real y un RFI_REF real en
`127.0.0.1:1235`, deliberadamente diseñada para romper RFI_REF preservando
MAIN:

- **A (normal):** MAIN+RFI_REF concurrentes — RFI_REF `RUNNING`, 0% clipping,
  todas las capturas de MAIN válidas.
- **B (matar RFI_REF a mitad de captura) — criterio obligatorio:** se envió
  `SIGKILL` únicamente al subproceso propio de RFI_REF mientras MAIN
  capturaba activamente. MAIN completó sus 6 capturas sin pérdidas ni
  discontinuidades; RFI_REF pasó a `FAILED` con el error capturado; cero
  resets USB; `rtl_tcp.service` nunca se tocó.
- **C (puerto 1235 ocupado por un proceso ajeno):** RFI_REF detectó el
  ownership ajeno por PID, nunca se conectó ni lo terminó, quedó
  `UNAVAILABLE`; MAIN completó su sesión con normalidad.
- **D (V3 ausente, simulado con un serial inexistente):** `rtl_tcp` rechazó
  cualquier fallback a otro dispositivo ("No matching devices found"),
  RFI_REF quedó `FAILED` con el diagnóstico real de `rtl_tcp`; MAIN no se vio
  afectado.
- **E (backpressure / FFT lento):** con un retardo artificial de FFT
  (parámetro sólo de prueba, vía `ALMITA_RFI_TEST_FFT_DELAY_S`), RFI_REF
  descartó su propio trabajo de FFT (bloques `dropped`) en vez de acumular
  atraso; MAIN mantuvo throughput normal.
- **F (carga tipo campo completa):** MAIN+RFI_REF junto con
  `almita_console_watcher.py` y `almita_console_server.py` corriendo en
  paralelo durante ~150s; MAIN sin pérdidas, `rfi_ref` reflejado en
  `almita_status.json`.

En todas las fases: `rtl_tcp.service` nunca se reinició ni modificó, ningún
proceso ajeno fue terminado, y las únicas terminaciones fueron sobre PIDs que
RFI_REF lanzó él mismo. La evidencia completa (JSON por fase, HDF5 de MAIN,
logs) se guarda bajo `data/rfi_ref_satanic/<run_id>/` y no se versiona en git.

Nota de alcance: esta validación de hardware se corrió a nivel SDR/software
(clases reales `SDRCapture`/`RFIReferenceMonitor`, hardware real MAIN+RFI_REF)
sin pasar por el loop de puntos con INDI/mount — la integración con el mount
real (GOTO/tracking) queda cubierta por los tests de integración offline en
`test_capture_rfi_ref_integration.py`, que sí ejercitan el ciclo de vida
completo de `execute_observation_plan()` con un telescopio simulado.
