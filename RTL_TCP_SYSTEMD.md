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
