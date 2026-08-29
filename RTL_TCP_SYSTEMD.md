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
