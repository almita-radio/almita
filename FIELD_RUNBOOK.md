# ALMITA FIELD RUNBOOK — AFC-00 / CONSOLE V1

*Actualizado 2026-10-06: servidor web único :8088, CALIBRATE (wizard 50 Ω + HI), OBSERVE con explorador de
perfiles, SCIENCE por bloques. Las secciones 16–18 son nuevas.*

One page. Read before touching hardware. If a command's output doesn't match
"Esperado", stop and think before continuing.

Repo: `/home/stellarmate/almita`

---

## 1. STARTUP CHECK

```
systemctl is-active almita-console-watcher.service
systemctl is-active almita-observe-api.service   # servidor web único :8088 (consola + API), con usuario/contraseña
systemctl is-active almita-system-blackbox.service
systemctl is-active rtl_tcp.service

systemctl status almita-console-watcher.service --no-pager
systemctl status almita-observe-api.service --no-pager

ss -lntp | grep ':8088'
ss -lntp | grep ':1234'

hostname -I
```

**Esperado:**
- Web única (consola + API): `0.0.0.0:8088`, pide usuario y contraseña (ver docs/WEB_OPERATIONS.md)
- rtl_tcp: `127.0.0.1:1234` (bind real de la unit instalada en este equipo — solo loopback, no LAN)
- Los cuatro servicios en `active`
- rtl_tcp arranca en la frecuencia/muestreo/ganancia de `observer_config.json` (`observation_defaults`:
  1420405752 Hz / 2400000 sps / 40.2 dB, Bias-T `-T`). Cada adquisición vuelve a sintonizar y exige el ACK
  de rtl_tcp en el journal (`set freq …`); sin ACK no captura.
- Nada escucha en :8090 (puerto retirado; `almita-console-web.service` deshabilitado — no reactivarlo).

Si `rtl_tcp.service` no existe o el bind es distinto en otro equipo: inspeccionar
`cat /etc/systemd/system/rtl_tcp.service` y documentar el real ahí, no asumir este.

## 2. OPEN CONSOLE

Desde un notebook/teléfono en la misma LAN:

```
http://<IP-DEL-PI>:8088/
```

Usar una de las IPs reportadas por `hostname -I` (ej. `192.168.1.165` en este equipo — puede cambiar según la red).

**Esperado sin sesión activa:**
- `Session: IDLE`
- `Quicklook: NO ACTIVE SESSION`
- `Instrument: READY` (o `DEGRADED` — si aparece, leer el detalle mostrado antes de asumir falla)

## 3. QUICK HEALTH CHECK

```
curl -s -u <usuario> http://127.0.0.1:8088/runtime/almita_status.json
```

Si `jq` está instalado (verificar con `which jq`; **no instalarlo si no está**):

```
curl -s -u <usuario> http://127.0.0.1:8088/runtime/almita_status.json | jq .
```

Revisar en la respuesta:
- `updated_utc` — reciente (segundos, no minutos/horas)
- `instrument.rtl_tcp_process`
- `instrument.rtl_tcp_listening`
- `instrument.sdr_temperature_c`
- `instrument.lna_temperature_c`
- `instrument.disk`
- `acquisition.state`
- `quicklook.state`

## 4. BEFORE CAPTURE

```
[ ] reloj correcto
[ ] coordenadas del sitio correctas
[ ] Internet no requerido / aislamiento físico si corresponde
[ ] alimentación estable
[ ] antena/cables/adaptadores revisados
[ ] mount/tripod/counterweights seguros
[ ] 50 ohm load disponible
[ ] Console accesible (ver §2)
[ ] rtl_tcp activo (ver §1)
[ ] temperaturas razonables (ver §3)
[ ] espacio en disco suficiente (ver §3)
[ ] no hay sesión anterior RUNNING falsa (acquisition.state en Console, ver §10)
```

## 5. AFC-00 FIELD ORDER

```
A. encender y estabilizar 10-15 min
B. revisar Console (§2, §3)
C. baseline 50 ohm
D. cambiar a antena
E. stationary sky baseline 30-60 s
F. mount short movement
G. mount medium movement
H. mount long movement
I. tracking check
J. mini campaign 3x3
K. observar Console/Quicklook durante la campaña
L. cierre ordenado (§13)
```

Sun/HI **no** son requisito de AFC-00.

## 6. CONSOLE STATES

- **IDLE** — sin sesión activa ni previa relevante.
- **STARTING** — sesión recién anunciada, aún no captura el primer punto.
- **RUNNING** — capturando puntos activamente.
- **COMPLETED** — sesión terminó exitosamente, todos los puntos planificados resueltos.
- **DEGRADED** — algo está atrasado/silencioso (staleness, proceso no detectado, pausa por seguridad); **no implica pérdida de datos automáticamente**.
- **ABORTED** — la sesión se detuvo por interrupción del usuario o error no recuperado.

**Importante:** ante `DEGRADED`, revisar `last_successful_point_id`, `acquisition_stale` y `error` en Console antes de actuar. No asumir falla de hardware sin mirar esos tres campos.

## 7. IF CONSOLE STOPS RESPONDING

```
systemctl status almita-observe-api.service --no-pager
journalctl -u almita-observe-api.service -n 50 --no-pager
```

Si solo `web` falló:

```
sudo systemctl restart almita-observe-api.service   # no corta capturas (KillMode=process)
```

Confirmar:

```
curl -I -u <usuario> http://127.0.0.1:8088/
```

No reiniciar el watcher si no es necesario.

## 8. IF WATCHER STOPS

```
systemctl status almita-console-watcher.service --no-pager
journalctl -u almita-console-watcher.service -n 50 --no-pager
```

Si solo el watcher falló:

```
sudo systemctl restart almita-console-watcher.service
```

El frontend puede seguir mostrando el último `almita_status.json` válido mientras el watcher está detenido (no cae, solo deja de actualizarse).

## 9. IF RTL_TCP IS DOWN

```
systemctl status rtl_tcp.service --no-pager
journalctl -u rtl_tcp.service -n 50 --no-pager
```

**NO reiniciar automáticamente si hay una captura activa.**

Antes de cualquier restart, confirmar en Console (§3):

```
acquisition.state != RUNNING
```

Si NO hay captura activa, el comando real de restart en este equipo es:

```
sudo systemctl restart rtl_tcp.service
```

No modificar la unit (`/etc/systemd/system/rtl_tcp.service`).

## 10. IF SESSION LOOKS STUCK

Revisar en Console:
- `acquisition.state`
- `acquisition_stale`
- `capture_process_detected`
- `current_point_id`
- `last_successful_point_id`
- `quicklook.state`
- `quicklook_stale`

Comandos seguros (solo lectura):

```
ps -ef | grep '[c]apture.py'
ps -ef | grep '[q]uicklook_live.py'
```

**NO matar procesos automáticamente. NO lanzar una segunda captura. NO lanzar un segundo Quicklook sobre la misma sesión sin entender el estado primero.**

## 11. LOGS

```
journalctl -u almita-console-watcher.service -n 100 --no-pager
journalctl -u almita-observe-api.service -n 100 --no-pager
journalctl -u almita-system-blackbox.service -n 100 --no-pager
journalctl -u rtl_tcp.service -n 100 --no-pager
```

`capture.py` y `quicklook_live.py` **no** son servicios systemd. Lanzados desde la web (OBSERVE), su salida
queda en la carpeta de la sesión (`data/mosaic/<sesión>/orchestrator_capture.log`, `orchestrator_quicklook.log`);
cada acción web (ALIGN, CALIBRATE, REDUCE, SCIENCE) deja `data/runtime/web_ops/<job>/job.log`. Lanzados a mano,
su salida vive en la terminal donde se ejecutaron.

## 12. SAFE RECOVERY RULES

**NUNCA ejecutar en este repo durante terreno:**

```
git clean -fd
git reset --hard
git checkout .
git add .
git add -A
```

**Motivo:** `data/` contiene decenas de GB de evidencia física que git NO versiona (`.gitignore` la excluye
entera salvo `data/hi_sky_catalog_2000pts.csv`; los insumos de tests viven en `tests/fixtures/`).

**Tampoco:**
- borrar HDF5
- mover campañas
- limpiar `data/`
- modificar el perfil de calibración
- cambiar el gain durante una sesión
- activar AGC
- correr una segunda instancia que consuma `rtl_tcp`
- abrir un segundo lector INDI
- hacer SYNC por improvisación
- sobrescribir o editar un perfil en `data/calibration/` (seleccionarlo en OBSERVE nunca lo modifica)

## 13. NORMAL SHUTDOWN

```
- confirmar sesión COMPLETED/ABORTED en Console
- esperar Quicklook final si corresponde
- revisar que el último punto esté persistido (last_successful_point_id)
- cerrar cualquier proceso de Capture iniciado manualmente
- NO es necesario detener Console V1 — puede permanecer residente
- apagar hardware siguiendo el procedimiento físico normal
```

Si el Pi se apagará:

```
sudo systemctl poweroff
```

No hay pasos adicionales de hardware documentados más allá de esto.

## 14. KNOWN OPEN ITEMS

*(NO BLOCKER FOR CONSOLE V1 — VERIFY OPERATIONALLY DURING FIELD USE)*

- `mount_control.py` `connect()` sin `await`
- visibility TOCTOU (chequeo de elevación puede quedar desactualizado para el GOTO)
- console header Gain/Bias-T histórico (texto fijo, no lee el valor real configurado)
- identidad de sesión canónica global (3 IDs distintos según el artefacto)
- CSV de sesión/mosaic no atómico
- INDI process lock (sin exclusión mutua forzada)
- fallback de gain en `sdr_capture.py`
- duplicación de lectores DS18B20
- documentación antigua (`FLUJO_COMPLETO.md`, `README.md`) con referencias desactualizadas

## 15. EMERGENCY PRINCIPLE

```
IF IN DOUBT:
STOP THE SESSION.
DO NOT MOVE THE MOUNT.
DO NOT START A SECOND SDR/INDI CONSUMER.
PRESERVE DATA.
CHECK THE CONSOLE AND LOGS.
```

## 16. CALIBRATE — REFERENCE WIZARD

Web → CALIBRATE → START WIZARD. Pasos: 50 Ω → reconectar antena → PROPOSE HI ALTO/HI BAJO → APPROVE → captura en
cada zona (GOTO real: escribir `MOVE`) → FINISH (construye el perfil para OBSERVE/QUICKLOOK).

- **50 Ω**: el cambio de terminación es físico y lo confirma el operador; el wizard nunca avanza solo.
- **Mapa HI**: zonas, orden de visita, zona activa y capa HI4PI en el instante del plan — son exactamente las
  coordenadas que usa la captura. "OBSTACLES NOT EVALUATED": no hay modelo de horizonte, revisar el cielo a ojo.
- **STOP CURRENT STEP** (visible mientras corre una captura): detiene ese paso (SIGINT). El paso queda donde
  estaba, el aviso dice cuántos archivos quedaron en disco; reintentar NO los sobrescribe (pasan a
  `captures/<tipo>/interrupted-<UTC>/`).
- **ABORT WIZARD**: detiene primero el paso en curso y cierra la sesión. No borra nada. Funciona aunque MAIN
  esté ocupado. Después se puede iniciar una sesión nueva.
- Al recargar la página, el wizard en curso (incluso uno interrumpido) se recupera leyendo su estado real.

## 17. OBSERVE — PERFIL DE CALIBRACIÓN

- **BROWSE ALMITA SERVER…** abre un explorador de `data/calibration` en el **servidor** (no del computador del
  navegador): carpetas, nombre, Hz / sps / dB y compatibilidad con el formulario. Un perfil incompatible o
  inválido no se puede seleccionar y muestra el motivo.
- El perfil seleccionado es exactamente el que recibe QUICKLOOK (`--calibration-profile`). Con QUICKLOOK
  activado, un perfil inexistente/inválido/incompatible **bloquea el PLAN** (antes solo advertía).

## 18. SCIENCE

- PLAN antes de RUN, una sola tarea REDUCE/SCIENCE a la vez en el Pi.
- A, B y C comparten proyección, huella y escala: A son las celdas medidas (deformadas en un campo amplio, con
  espectro al hacer clic); B/C son estimaciones entre puntos — más píxeles no es más resolución del instrumento.
- B/C se calculan por bloques con el motor congelado (resultado idéntico al cubo completo): sesión real de 400
  puntos ≈ 6.5 min, pico ≈ 1 GB, límite 3 GB.
- Bordes rayados/atenuados en B/C: píxeles con un solo punto real dentro del soporte (sobre todo el borde del
  campo). El detalle técnico de cada corrida queda en `maps/NOTES.md`.

