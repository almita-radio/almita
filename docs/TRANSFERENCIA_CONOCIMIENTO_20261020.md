# ALMITA — transferencia de conocimiento (20 oct 2026)

Resumen del trabajo de Felipe con Claude, para quedar al día. Horas en hora de Chile (UTC−3).

---

## 1. Estado al 20 oct, noche

- **Llueve. Todo adentro y DESARMADO.** Hoy no hay pruebas.
- Felipe va a **cambiar el cable feed→LNA** (lo armó a mano y no quedó bien) y a **limpiar el SDR y el LNA** con aire y limpiacontactos. Tiene que comprar conectores SMA.
- No hay ningún perfil de calibración válido para observar el cielo (ver §4).

## 2. Hardware actual

- **MAIN = RTL-SDR V3** (el que antes era RFI_REF). El V4 murió el 17 oct. Al V3 se le reescribió el serial EEPROM 00000002 → **00000001**, para que el software lo trate como MAIN sin cambiar código. Backup del EEPROM: `data/hardware_backups/eeprom_V3_sn00000002_20261017.bin`.
  - Los rótulos del código ("RTL-SDR Blog V4", tuner R828D) quedaron obsoletos: describen al V3.
  - Nada en el software distingue datos del V4 de datos del V3. **La frontera es la fecha: 17 oct.**
- **No hay RFI_REF.** Tiene que quedar desactivado; el formulario OBSERVE ya parte apagado.
- **LNA Nooelec** alimentado por el Bias-T del SDR (rtl_tcp con `-T`). Aporta **30.8 dB** (medido el 17 oct).
- **Sintonía:** 1420.405752 MHz, 2.4 MS/s, ganancia 40.2 dB, AGC del RTL apagado. La única fuente es `observer_config.json`. Hay un test que prohíbe escribir la frecuencia en otro lugar del código.
- **Montura:** LX200 OnStep por INDI/Ekos.
  - **Activar siempre el auto-flip de OnStep antes de cualquier sesión.** El 13 oct, 12 min pasado el meridiano, OnStep paró el seguimiento en vez de dar vuelta; el error quedó pegado y hubo que apagar y prender el tracking en Ekos.
  - El software rechaza trabajos que crucen +12 min del meridiano. Si el auto-flip queda activo, esa regla se puede revisar.
- **Sensores de temperatura (DS18B20):** sdr, lna, cable y load. Los registra un servicio cada 60 s y se ven en STATUS.
- **Raspberry Pi 5:** memoria justa. No correr dos trabajos pesados (SCIENCE/REDUCE) a la vez.

## 3. Software

- Repo `almita-radio/almita`, rama **`web-polish-v1`**, al día con GitHub (último commit 6b0674d). Ese día se subieron 12 commits, uno por tema:
  - temperaturas;
  - invalidación de perfiles;
  - botón del LNA (Bias-T);
  - selector de montura LX200 / Simulador;
  - QUICKLOOK que arranca aunque la primera captura tarde;
  - ajuste solar y lectura en vivo del ALIGN;
  - escritorio remoto VNC;
  - scripts de pruebas de hardware;
  - integración web, con modo DÍA/NOCHE;
  - una entrada del backlog y un arreglo de test.
- **Tests:** suite completa ~2740, todos pasan (20 oct).
- **Web:** `http://192.168.1.165:8088/`, usuario `felipe`. Páginas: OBSERVE, CALIBRATE, ALIGN, LOBE, PIPELINE, REDUCE, SCIENCE, STATUS, DESKTOP.
- **Módulos "congelados":** `capture.py` y `observation_orchestrator.py` tienen un candado de tests. Cualquier cambio necesita el OK de Felipe y actualizar la versión fijada en `scripts/science_forensics_field.py`. Hoy están fijados en 72fd7cf y 8b0659e.
- Las ramas `science-forensics-v1` (análisis forense, experimento y kit de campo) son **locales, no subidas**.
- `data/` no se sube a git: capturas, perfiles y análisis viven solo en el Pi.

## 4. Hallazgos firmes del hardware

1. **Conector del LNA intermitente** (18 oct, 16:55–17:02, terraza, apuntando al Sol). Al mover el conector del LNA el nivel saltó **+5.9 dB** y se quedó arriba. Mover el SDR o el cable USB no cambió nada, y la presencia de Felipe tampoco.
   - El nivel "bajo y tranquilo" de ese día era el estado de **mal contacto**.
   - **Todos los resultados de Sol/ALIGN/ON-OFF del 18 antes de las 16:57 son inválidos.**
   - Ese mismo día se encontró la palanca del freno de AR rozando la polea de la correa; Felipe la limó. Es una causa probable de pasos perdidos.
2. **Ganancia en dos niveles del SDR** (noche del 17 al 18, carga de 50 Ω, 145 acciones):
   - El nivel bajo resistió retunes, apagar y prender el Bias-T, reenviar ganancia y reconexiones.
   - Solo **reiniciar rtl_tcp** lo hizo saltar +1.77 dB, plano en toda la banda. El estado lo guarda el SDR, no el LNA.
   - Consecuencia: **comparar o calibrar solo dentro del mismo proceso de rtl_tcp.**
3. **El SDR a veces arranca "mudo"** después de reiniciar rtl_tcp (visto con el V4; rms ~0.66). Se despierta sintonizando 100 MHz y volviendo. **Revisar el rms después de cada reinicio.**
4. **Temperatura:** −0.10 dB/°C en la cadena (correlación −0.97). Afuera, ±10 °C equivale a ±1 dB: hace falta corrección o referencia.
5. **Estabilidad:** con la carga de 50 Ω el radiómetro está limitado solo por ruido hasta ~8 s, y después domina la deriva de ganancia. Con la antena adentro la potencia es inestable (Allan ~0.1).
6. **Líneas espurias** (+305, −408, +121, +535, −293 kHz…): con la carga de 50 Ω no aparecen, así que **entran por la antena** desde el ambiente. Aparte hay una **línea que se mueve** con la temperatura del SDR y del LNA, generada en la cadena, a −0.72 MHz ≈ **+152 km/s** (backlog BL-072). Probablemente es el rasgo de **+148 km/s** de SCIENCE Forensics; no está confirmado.
7. **Cargas caseras:** la primera metía RF por el cable del sensor. La segunda, blindada con papel aluminio, quedó +0.28 dB sobre la comercial. **Usar la carga comercial como referencia**, con el sensor por fuera.

**Perfiles de calibración:** todos los del V4 están **invalidados** (marca `*.INVALIDATED.json`), igual que el WIZARD del 17 oct 03:44, que se tomó con la antena en vez de la carga. El único válido del V3 es WIZARD-20261017-092052 (adentro). **Hace falta un WIZARD nuevo, con buen contacto, antes de la ciencia.**

## 5. Análisis de la corrida de 30 puntos (18 oct, 18:27, sesión 20261018_212734)

Productos en `data/science/ALMITA-OBSERVE/HI_MANUAL-20261018_212734/`: figuras, `puntos_HI.csv` y scripts. Campo: l = 308–352°, b = −22…+22°, ~5° entre puntos.

- **El cielo casi no se distingue de la carga:** Y ≈ 1 dB, cuando se esperan 5–6 dB. Equivale a ~1000 K de ruido de sistema. Lo más probable es pérdida en el conector o cable antes del LNA, coherente con el +5.9 dB del punto 4.1.
- **Ganancia inestable:** 1–4.6 % dentro de cada punto (el ideal es 0.09 %). Los "puntos calientes" dependen de la orientación de la montura, lo que sugiere un cable o conector que se flexiona.
- **Bump de +3–5 % en 1420.335–1420.360 MHz**, en los 30 puntos. Veredicto: **probablemente no es HI**, pero no está descartado.
  - **A favor de que sea HI:**
    - cae justo donde estaría el HI local con las correcciones Doppler de esa noche;
    - el ancho (~15 km/s) y la amplitud son plausibles;
    - con ~6° de error de apuntado, la comparación con HI4PI pierde valor.
  - **En contra:**
    - no se mueve con la corrección Doppler (pendiente +0.24, el HI daría −1), y esa prueba no depende del apuntado;
    - no hay contraste entre el plano galáctico y b = ±22°;
    - el equipo fluctúa al mismo nivel que el rasgo;
    - está a ~57 kHz del LO, zona de artefactos del RTL.
  - Como el LO está justo en f₀, el HI local siempre cae cerca del bin DC. **La prueba decisiva es correr el LO** (por ejemplo +300 kHz): un rasgo del equipo se mueve con el LO, el HI se queda en su frecuencia.

## 6. Próximos pasos acordados

1. **Adentro, al rearmar:**
   - salud del SDR (`scripts/hardware/sdr_health.py`, comparar con LNA 30.8 dB);
   - rms después de arrancar rtl_tcp;
   - **repetir la prueba de mover el cable** con el cable nuevo;
   - que los 4 sensores lean.
   - **Bias-T apagado** al conectar o desconectar.
2. **Afuera, primera noche despejada:**
   - auto-flip de OnStep activado;
   - **medir el Y (cielo contra carga)**: tiene que dar varios dB, idealmente >4. Si da ~1 dB, se para ahí.
3. **Nubes de Magallanes** (siempre sobre el horizonte en Santiago, al sur):
   - **SMC** (~+170 km/s) cae en **~1419.60 MHz**, dentro de la banda actual. Modo **ON/OFF** contra un punto vacío a la misma altura, cada par del mismo lado del meridiano. Mejor ventana: 22:00–02:00 (culmina ~51° a las 00:30).
   - **LMC** (~+265 km/s, ~1419.15 MHz) queda **fuera de la banda actual**. Hay que mover el LO, por ejemplo a 1419.80 MHz, cambiarlo en `observer_config.json` y hacer un WIZARD nuevo. De paso eso sirve como prueba de retune del bump. Culmina ~54° a las 05:00.
4. **Pendientes del backlog:**
   - BL-078: bloquear el driver de TV del RTL;
   - SYNC solo se ha probado con una montura falsa; falta validarlo en la montura real;
   - LOBE necesita primero un ALIGN solar que pase;
   - con el simulador, STOP MOUNT da una falsa "PARADA NO CONFIRMADA".

## 7. Cómo trabajar con Felipe

- **Español, directo y sin jerga interna.** Somos "ermanos" (sin H). Horas en hora de Chile.
- **Una pregunta no es un "dale".** Si hay ambigüedad sobre actuar, preguntar antes. Nunca cambiar estado (montura, trabajos, servicios, hardware) sin un OK explícito.
- **Antes de interpretar cualquier resultado de hardware, preguntar si fue adentro o con cielo abierto.** Adentro no se sacan conclusiones de cielo ni se hace SYNC.
- **Detener `capture.py` siempre con SIGINT**, nunca SIGTERM.
- **Antes de reiniciar un servicio**, revisar en un paso aparte que no haya trabajos corriendo.
- **Bias-T:** cualquier carga de 50 Ω directo en el SDR necesita DC block o rtl_tcp sin `-T`.
- **Git:** agregar archivos por nombre, nunca `git add .`; nada de `reset --hard` ni `clean`; subir solo si Felipe lo pide. Reportar los fallos tal como son.
- **Ciencia:** no clasificar rasgos como HI o RFI sin evidencia. Usar "observado / consistente con / no resuelto".
