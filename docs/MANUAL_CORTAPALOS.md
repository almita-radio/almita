# ALMITA — Manual cortapalos

**De la preparación del equipo a REDUCE y SCIENCE, con la versión instalada.**

Versión del software: rama `web-polish-v1`, commit `67df4a6` (2026-10-08). El servidor web corre desde las
00:41 UTC con el commit `f4cd4c5`. Los cambios posteriores son de la consola o de scripts que se cargan en cada
uso, así que ya están activos. Este es un **primer borrador**. Revisado contra el código y la web actuales, y
contra las sesiones reales guardadas en `data/`.

**Cómo leer las marcas de verificación**

| marca | significa |
|---|---|
| **[H]** | Hecho en el instrumento real. Hay evidencia en disco y se indica la fecha. |
| **[D]** | Corrido sobre datos reales, sin mover hardware (REDUCE, SCIENCE). |
| **[C]** | Comprobado solo en el código o con tests. No está probado en el instrumento. |
| **[P]** | Pendiente: nadie lo ha comprobado todavía. |

Todos los tramos del recorrido se han hecho en hardware, pero en noches distintas: ALIGN HI el 2026-10-07; wizard,
gain pilot, observaciones, REDUCE y SCIENCE el 2026-10-08. **Nunca se ha hecho el recorrido completo de una vez
siguiendo este manual.**

---

## 0. El orden correcto y por qué

```
1  Equipo + servicios
2  Montura en Ekos: conectar, desaparcar, ALINEAR
3  ALIGN de ALMITA: medir el offset con señal (Sol de día, HI de noche) y SYNC si es confiable
4  ¿Perfil válido a la ganancia que vas a usar?   sí → Ruta A   ·   no → Ruta B (CALIBRATE)
5  OBSERVE: PLAN → PREFLIGHT → (gain pilot) → START → COMPLETED
6  REDUCE → SCIENCE
```

Qué depende de qué en el código actual:

- **La alineación base de la montura se hace en Ekos/OnStep.** ALIGN de ALMITA arranca desde ahí: necesita que la
  montura ya apunte razonablemente bien, porque su búsqueda de offset cubre unos ±6°. Todo lo que hace GOTO
  depende del apuntado: las zonas HI del wizard, el gain pilot, ALIGN y OBSERVE. Por eso Ekos va primero.
- **ALIGN de ALMITA mide el error de apuntado con la señal de radio y puede corregirlo con SYNC**, tanto con el Sol
  como con HI. Solo envía SYNC si la confianza llega al umbral y el offset no supera el máximo. No usa el perfil
  de calibración, así que puede ir antes o después de CALIBRATE. Conviene hacerlo antes de OBSERVE.
  **[C; SYNC nunca probado en hardware]**
- **CALIBRATE no depende de ALIGN.** La parte de 50 Ω no mueve nada. HI ALTO y HI BAJO hacen GOTO a zonas
  amplias (haz de unos 20°), así que solo necesitan la alineación de Ekos. Hay un orden práctico: haz el 50 Ω
  cuando ya no vayas a tocar los cables de la antena. **[C]**
- **El perfil de calibración debe coincidir con la observación** en frecuencia, muestreo, **ganancia** y
  topología. Por eso CALIBRATE va antes de OBSERVE y con la misma ganancia con la que vas a observar. Si el gain
  pilot cambia la ganancia, el perfil deja de servir (ver §8). **[C][H 2026-10-08]**

---

## Parte 1 — Pasos comunes

### 1. Equipo y conexiones

1. **Cadena principal (MAIN):** antena → Nooelec SAWbird H1 (LNA + filtro) → RTL-SDR Blog V4 con **serial
    00000001**, por USB al Pi. El LNA se alimenta por el Bias-T del SDR.

2. **Antena B (RFI REF, opcional):** antena B → RTL-SDR con **serial 00000002**, por USB.
3. **Montura:** OnStep (LX200 OnStep) conectada al Pi. Revisa el trípode, los contrapesos, el recorrido libre de
    cables y que no haya nadie en el camino del GOTO.

4. Ten a mano la **terminación de 50 Ω** si vas a calibrar (Ruta B).
5. Enciende y deja estabilizar el equipo **10–15 min**.

**Para seguir:** todo conectado y la montura sin obstáculos en todo su recorrido.

### 2. Servicios y SDR

1. Abre la web: `http://<IP-del-Pi>:8088/`, con tu usuario y contraseña. La IP sale de `hostname -I`.
2. Ve a **STATUS**. En **SERVICES**, los cuatro servicios tienen que estar activos: `almita-observe-api`,
    `almita-console-watcher`, `almita-system-blackbox` y `rtl_tcp`. En **DEPENDENCIES**:
    - `indi` → UP. Aparece cuando Ekos ya está corriendo (paso 3).
    - `main_sdr` → AVAILABLE, con el serial 00000001 presente.
    - `rfi_sdr` → AVAILABLE, o DOWN si no usas la antena B; es opcional.
    - `mount` siempre dice NOT_EXPOSED: STATUS no se conecta a la montura, y eso es normal.

3. **Bias-T de MAIN:** lo enciende `rtl_tcp` al arrancar (opción `-T`) y alimenta el LNA. En OBSERVE aparece
    marcado y no se puede apagar. **No lo cambies.** **[C][H indirecto: hubo señal HI en las sesiones reales]**

4. Frecuencia, muestreo y ganancia **no se escriben a mano**. Salen de `observer_config.json` →
    `observation_defaults` (hoy **1420405752 Hz / 2400000 sps / 40.2 dB**) y la web rellena los campos sola.
    Cada captura vuelve a sintonizar y exige el ACK de `rtl_tcp`. **[C][H]**

**Para seguir:** STATUS sin bloqueos, salvo `indi`, que falta hasta el paso 3.

### 3. Montura en Ekos/INDI

1. En StellarMate/Ekos, arranca el perfil que incluye el driver **LX200 OnStep**. Así queda el servidor INDI en el
    puerto **7624**.

2. Conecta la montura, **desapárcala** (Unpark) y revisa que la hora y el sitio sean correctos. El sitio es
    Santiago, −33.4489° / −70.6693°, 570 m.

3. Alinea la montura **en Ekos** con tu procedimiento habitual: alineación por estrellas o plate solving y SYNC
    en Ekos. **[P: este manual no documenta los nombres de los menús de Ekos; ver comprobaciones físicas]**

4. En **ALIGN** (o en **PIPELINE**), pulsa **REAL PREFLIGHT** (en PIPELINE: **RUN REAL PREFLIGHT**). Se conecta
    a la montura sin moverla. ALMITA se conecta sola al dispositivo `LX200 OnStep` y le envía CONNECT si hace
    falta.

**Para seguir:** el preflight real no está en BLOCK y la montura está conectada, desaparcada, sin error OnStep y
en estado `Idle` o `Tracking`. Toda acción que mueve la montura exige esto, además de escribir **MOVE** en
mayúsculas. **[C][H]**

### 4. ALIGN — medir el offset y corregirlo con SYNC

Distingue dos cosas:

- **Alineación base** (Ekos, paso 3): deja la montura apuntando razonablemente bien. Es obligatoria.
- **ALIGN de ALMITA** (página **ALIGN**, sección **REAL ALIGNMENT**): mide la señal en anillos alrededor de una
  referencia, estima el error de apuntado (offset) y, si es confiable, corrige la montura con **SYNC**.

Pasos:

1. Elige el modo:
    - **SOLAR (day)**: el Sol es la referencia más fuerte y puntual, la mejor para medir el offset. El modo
      cambia OnStep a seguimiento solar y al terminar restaura el anterior. **[P: nunca corrido desde la web]**
    - **HI (night)**: usa la estructura HI del cielo. Es más débil y a menudo no alcanza para estimar el
      offset. **[H 2026-10-07: sin estimación]**

2. **HI:** pulsa **REFRESH SKY VIEW** y elige una zona candidata (A, B o C). **SOLAR:** el Sol tiene que estar
    sobre el horizonte.
3. Deja los valores sugeridos: **RING RADII** `5,2,0.6`, **POINTS PER RING** `8` (25 posiciones), **CAPTURE TIME
    PER POSITION** sugerido, **BEAM FWHM** `20` (de `observer_config.json`) y **MIN ELEVATION** `20`.

4. Pulsa **REAL PREFLIGHT** y después **PLAN (real, moves nothing)**. El plan comprueba que todo el patrón se
    mantenga sobre la altura mínima durante la corrida y caduca pasado un tiempo. Si caduca, vuelve a pulsar PLAN.

5. **SYNC THE MOUNT AT THE END** viene marcado. Con **MAX SYNC OFFSET** (5° por defecto) fijas el offset máximo
    que se acepta corregir. Si desmarcas la casilla, solo se mide.
6. Escribe **MOVE** y pulsa **RUN REAL ALIGNMENT**.

**Resultado:** al terminar, el recuadro **ALIGNMENT RESULT** muestra siempre:

- el offset (ΔRA hacia el este, ΔDec y el total en grados);
- la confianza y el umbral (0.65);
- si se envió SYNC o por qué no.

Cuándo se envía SYNC: solo si se cumplen las cuatro condiciones.

1. La casilla está marcada.
2. Hay offset estimado.
3. La confianza es ≥ 0.65.
4. El offset es ≤ MAX SYNC OFFSET.

El SYNC lleva la montura a la posición corregida, la sincroniza con la referencia y mide la repetibilidad
(sale 2° y vuelve).

| resultado | qué significa | qué hacer |
|---|---|---|
| `PASS` + **SYNC APPLIED** | Offset confiable, corregido. | Listo. Puedes repetir ALIGN para confirmar que el nuevo offset sale cercano a 0. |
| `PASS` + SYNC NOT SENT (offset > máximo) | Offset grande. Más probable que sea un mal ajuste que un error real. | Revisa la alineación en Ekos y repite. No subas el máximo a la ligera. |
| `LOW CONFIDENCE` | El ajuste no es confiable. No se corrige. | Repite. Con HI, prueba otra zona o el Sol de día. |
| `NO DEFENDIBLE DIFFERENTIAL HI STRUCTURE` | La señal HI no alcanza para estimar el offset (lo que salió el 2026-10-07). | Usa el Sol de día, o sigue solo con la alineación de Ekos. |
| `FAIL` / `INSUFFICIENT VALID POSITIONS` | Faltaron capturas válidas. | Revisa el SDR y la montura, y repite. |

**[C: tests; SYNC real pendiente en hardware]**

---

## Parte 2 — El perfil de calibración

### 5A. Ruta A: ya tienes un perfil válido

1. Un perfil válido está en `data/calibration/WIZARD-…/observe_profile/calibration_profile_v1.json` y tiene **la
    misma frecuencia, muestreo y ganancia** que vas a usar. Hoy hay perfiles a **40.2 dB**
    (`WIZARD-20261002-013321-160933`, `WIZARD-20261007-010706-387765`) y a **42.1 dB**
    (`WIZARD-20261008-010548-274525`).

2. Lo eliges en OBSERVE (§6, campo QUICKLOOK). El explorador solo deja seleccionar perfiles compatibles.

**Para seguir:** el explorador muestra el perfil como compatible con el formulario.

### 5B. Ruta B: calibración nueva con el Reference Wizard

Página **CALIBRATE** → **REFERENCE WIZARD**. **[H 2026-10-02, 10-07, 10-08]**

1. **Configuración.** Los valores sugeridos sirven: **CAPTURES PER REFERENCE** 5, **SECONDS PER CAPTURE** 2,
    **50 Ω STABILIZATION TIME** 20 s, **HI GOTO SETTLE TIME** 2 s.

    **CENTER FREQUENCY**, **SAMPLE RATE** y **GAIN** vienen de la configuración central. **La GAIN tiene que ser
    la misma con la que vas a observar.**

    Pulsa **START WIZARD**.

2. **50 Ω.** Desconecta la antena y pon la terminación de 50 Ω **en la entrada del LNA**. En **CONNECTION
    POINT** elige **LNA INPUT (replaces the antenna)**: es obligatorio, porque con otro punto no se construye el
    perfil para OBSERVE.

    Pulsa **I'VE CONNECTED IT — CONFIRM**, espera unos 20 s y pulsa **CAPTURE NOW**. Revisa el resultado y pulsa
    **NEXT**.

3. **Antena.** Quita los 50 Ω, reconecta la antena y pulsa **CONFIRM & CONTINUE TO HI ZONE PROPOSAL**. El wizard
    no deja llegar a HI ni mover la montura sin esta confirmación.

4. **Zonas.** Pulsa **PROPOSE HI ALTO / HI BAJO ZONES** y revisa el mapa (el mismo cielo que usará la captura).
    El aviso «OBSTACLES NOT EVALUATED» significa que no hay modelo de horizonte, así que mira el cielo a ojo.
    Pulsa **APPROVE THESE ZONES**, o **RE-PROPOSE (refresh)** para pedir otras.

5. **HI ALTO.** Escribe **MOVE** y pulsa **CAPTURE NOW (real GOTO + capture)**. Luego pulsa **NEXT: HI BAJO** y
    repite la captura.

6. Pulsa **FINISH — COMPUTE CONTRAST**.

**Para seguir:** la sección **OBSERVE CALIBRATION PROFILE** dice **READY** y da la ruta del perfil.

- El perfil se construye con las capturas de 50 Ω.
- El contraste HI ALTO / HI BAJO es un diagnóstico de la cadena. Puede salir `DEFENSIBLE_CONTRAST` o
  `INCONCLUSIVE`. Con INCONCLUSIVE el perfil igual quedó READY el 2026-10-07 y el 2026-10-08.

Botones de control:

- **STOP CURRENT STEP** detiene solo la captura en curso. Los archivos no se pierden. **[C]**
- **ABORT WIZARD** cierra la sesión sin borrar nada. **[H 2026-10-07]**

---

## Parte 3 — Observar

### 6. OBSERVE: configurar el mosaico y la captura

Página **OBSERVE**, sección **OBSERVATION SPECIFICATION**. Los valores están en la tabla de parámetros (§12).

1. **OBSERVATION NAME:** por ejemplo `ALMITA-OBSERVE`.
2. **PLACEMENT:**
    - `EASTMOST_SAFE`: el centro se calcula solo y es el más al este posible que mantiene todo el mosaico sobre la
      altura mínima durante la sesión.
    - `FIXED_CENTER`: para un objetivo concreto. Pide **CENTER RA** y **CENTER DEC**.

3. **CENTER DEC:** si lo dejas vacío con EASTMOST_SAFE, se usa la latitud del sitio (−33.45°), es decir, el
    mosaico pasa por el cenit.

4. **WIDTH / HEIGHT / ROWS / COLS:** la separación tiene que ser igual en los dos ejes:
    `WIDTH/(COLS−1) = HEIGHT/(ROWS−1)`. Si no lo es, PLAN da error.

5. **MIN PLANNING ALTITUDE, CAPTURE (s), SETTLE (s):** ver la tabla.
6. **MAIN:** Frequency, Sample rate y Gain vienen rellenos. Cambia la ganancia solo si tienes un perfil para esa
    ganancia, o mediante el gain pilot. El Bias-T siempre está encendido.

7. **RFI_REF / ANTENNA B:** **Enabled**, Gain 25 dB, BIAS-T marcado si la antena B tiene un LNA alimentado.
8. **QUICKLOOK:** **Enabled**, **Native Grid** e **Interpolated Preview**. Elige el perfil con **BROWSE ALMITA
    SERVER…**. Ese perfil es el que recibe QUICKLOOK.

9. **CONSOLE:** Enabled.
10. Pulsa **PLAN OBSERVATION**.

### 7. PREFLIGHT

PLAN muestra **ALMITA OBSERVATION PLAN**:

- puntos, huella, centro, separación;
- duración estimada y conservadora;
- disco necesario;
- «Start before …» (validez del plan);
- la lista **PREFLIGHT** con `[PASS]`, `[WARNING]` o `[BLOCK]`.

**Para seguir:** la insignia dice **READY** o **WARNING** (lee cada WARNING) y no hay ningún `[BLOCK]`.

Las líneas que más importan:

- `MAIN / rtl_tcp.service`, `MAIN / port listening`, `MAIN / SDR presence`;
- `INDI/mount`;
- `Quicklook`, `Quicklook / calibration match`;
- `Disk space`.

Si START dice que el plan está obsoleto («the plan is stale»), pulsa **RE-PLAN**. **[H]**

### 8. GAIN PILOT (opcional, sugerido)

Mide con 1 o 2 capturas reales si la ganancia satura (clipping) antes de lanzar la grilla. **[H 2026-10-08]**

1. Deja los valores sugeridos y pulsa **PLAN GAIN PILOT**.
2. Escribe **MOVE** y pulsa **CAPTURE PILOT NOW (real GOTO + capture)**.
3. Revisa la recomendación y pulsa **APPROVE THIS GAIN**.
    - Si la ganancia cambió, pulsa **VERIFY NEW GAIN (real capture)**.
    - **MEASURE LOW TOO** es opcional.

4. Al llegar a **READY**: si la ganancia verificada no es la del plan, pulsa **RE-PLAN GRID AT X dB**. El plan
    nuevo pide una sola recaptura de control.

> ⚠ **Ojo con el perfil.** Si la ganancia aprobada es distinta de la del perfil de QUICKLOOK, el nuevo PLAN queda
> **BLOCKED** en `Quicklook / calibration match`, y REDUCE tampoco podrá calibrar esos puntos. **La web lo avisa
> antes de APPROVE THIS GAIN**, con un recuadro amarillo que lista los perfiles del servidor que ya sirven para esa
> ganancia. Es solo un aviso: no impide aprobar. **[C]** Tienes tres opciones:
>
> - quedarte con la ganancia del perfil (**ABORT GAIN PILOT**);
> - crear un perfil a la nueva ganancia (Ruta B), que es lo que se hizo el 2026-10-08 a 42.1 dB;
> - o desactivar QUICKLOOK. Los datos se capturan igual, pero sin vista previa.

Si no usas el gain pilot, START no lo exige.

### 9. START, seguimiento, STOP y cierre

1. Pulsa **START OBSERVATION**. Lee el resumen y confírmalo. **[H: 400, 676, 25 y 100 puntos]**
2. **Seguimiento** en **MONITOR**:
    - **SESSION**: PROGRESS, CURRENT POINT, FAILED, DEFERRED, ELAPSED/REMAINING;
    - **QUICKLOOK**;
    - **RFI REFERENCE**;
    - **SPECTRAL STACK 3D**;
    - **ACTIVITY LOG**.

    Duración real medida: unos **32 s por punto con capturas de 10 s** (400 puntos ≈ 3 h 32 min) y unos **23 s
    por punto con capturas de 1 s**.

3. **STOP OBSERVATION** envía SIGINT, que es una parada limpia. Los puntos ya capturados se conservan, y cerrar
    los archivos puede tardar hasta 2 min. El estado pasa por `STOPPING` y termina en `ABORTED`. **[C; P en
    hardware]**

4. **Comprobar que terminó bien:**
    - En OBSERVE → **OBSERVATION RUN**, el estado es `COMPLETED` («SUCCESS — capture finished»).
    - En MONITOR, SESSION muestra todos los puntos, FAILED = 0, y DEFERRED explicado.
    - En la carpeta `data/mosaic/<sesión>/` hay tantos `.h5` como puntos.
    - Si hay dudas, mira `orchestrator_capture.log`.

5. Al terminar, **aparca la montura en Ekos**. ALMITA no la aparca.

---

## Parte 4 — Reducir y ver ciencia

### 10. REDUCE  **[D 2026-10-08]**

Página **REDUCE**:

1. **1 · CHOOSE INPUT:** pulsa **CAMPAIGN / SESSION** y elige la sesión en **CAMPAIGN**. Si no aparece, pulsa
    **REFRESH LIST**.

2. **2 · METADATA:** revisa que no haya nada en «MISSING / CONTRADICTORY».
3. **3 · RELATIVE CALIBRATION PROFILE:** elige **el mismo perfil de la observación** con **BROWSE ALMITA
    SERVER…**. La tabla de compatibilidad revisa punto por punto. Los incompatibles quedan UNCALIBRATED, y para
    eso tienes que marcar la casilla «I understand…».

4. **4 · PLAN:** **VELOCITY FRAME** = `lsrk`. SCIENCE solo acepta LSRK. Pulsa **PLAN**.
5. **5 · RUN:** pulsa **RUN REDUCE**. La ventana del log muestra el avance punto por punto.
6. **6 · RESULTS:** espectro por punto y **ARTIFACTS**. Quedan en `data/reduced/<campaña>/REDUCE-…/`.

### 11. SCIENCE  **[D 2026-10-08]**

Página **SCIENCE**:

1. **1 · CHOOSE A REDUCE RESULT:** elige la reducción en **REDUCE SESSION**.
2. **2 · COVERAGE:** revisa los puntos sin eje LSRK y los problemas de ingesta.
3. **3 · MAGNITUDE, CALIBRATION LEVEL & VELOCITY WINDOW:** elige **CALIBRATION LEVEL** = **RELATIVE (calibrated)**. Deja el resto como está: velocidad −100 a
    +100 km/s, beam desde `observer_config.json`, SUPPORT RADIUS y SMOOTHING en `auto`, B = 3, C = 6,
    QUALITY POLICY STANDARD y cobertura mínima 0.5.

4. **4 · PLAN**, luego **5 · RUN**. El log muestra las etapas y los bloques (tiles) de los mapas B y C.
5. **6 · A → B → C:**
    - A son las celdas medidas; al hacer clic se ve su espectro.
    - B y C son estimaciones entre puntos. Más píxeles no significa más resolución del instrumento.

    Abajo están el export combinado y **DOWNLOADS**. En disco quedan en
    `data/science/<campaña>/SCIENCE_WEB-…/`: `maps/` (PNG, SVG, PDF y `NOTES.md`), `points.csv` y
    `manifest.json`.

Hay una sola tarea REDUCE o SCIENCE a la vez en el Pi.

---

## 12. Tabla de parámetros

| parámetro | qué es | valor actual / recomendado | cuándo cambiarlo |
|---|---|---|---|
| Frecuencia / muestreo | Sintonía de MAIN | **1420405752 Hz / 2400000 sps**, de `observer_config.json` | Nunca desde la web. Solo editando la configuración central. |
| Ganancia MAIN | Ganancia del RTL-SDR | **40.2 dB** (central). Las sesiones del 2026-10-08 usaron 42.1. | Solo con el gain pilot y con un perfil a esa ganancia. |
| Bias-T MAIN | Alimenta el LNA SAWbird | Siempre encendido | Nunca. Sin él no hay LNA. |
| Centro (PLACEMENT) | Dónde cae el mosaico | `EASTMOST_SAFE`, con CENTER DEC vacío (= latitud, por el cenit) | `FIXED_CENTER` para un objetivo concreto. |
| Extensión | WIDTH × HEIGHT | 30° × 30° | Según el objetivo. El haz es de unos 20° (provisional), así que un campo menor apenas cubre un par de haces. |
| Separación / puntos | WIDTH/(COLS−1), igual en ambos ejes | 10×10 (3.3°, 100 puntos, ~40 min con 1 s); 20×20 (1.58°, 400 puntos, ~3.5 h con 10 s) | No separes más que **FWHM/3 ≈ 6.7°** (`beam_sampling_fraction`). Una grilla más fina no da más resolución del instrumento, porque el haz no está medido. |
| Altura mínima | MIN PLANNING ALTITUDE | Por defecto 10°. **Recomendado 20–30°**: la última sesión usó 30. | Más bajo da más cielo, pero más suelo en los lóbulos y obstáculos sin evaluar. |
| Duración por punto | CAPTURE (s) | **10 s** para mapas; 1–2 s para pruebas | El ruido baja con √t: 10 s es unas 3 veces menos ruido que 1 s. |
| Estabilización | SETTLE (s) | **2 s** | Súbelo si la montura vibra al terminar el GOTO. |
| RFI REF | Antena B y SDR 00000002 | Enabled, 25 dB, Bias-T según la antena B | Desactívalo si la antena B no está. MAIN no se afecta. Hoy solo sirve de monitoreo: REDUCE no lo usa. |
| QUICKLOOK | Vista previa en vivo | Enabled + Native Grid + Interpolated Preview | Desactívalo si no hay perfil compatible. Los datos no dependen de él. |
| Perfil | `…/observe_profile/calibration_profile_v1.json` | El más reciente a **la misma ganancia** | Cuando cambie la ganancia, la cadena RF o el punto de conexión. |

---

## 13. Problemas habituales

| síntoma | qué significa | qué hacer |
|---|---|---|
| **BLOCKED** en PLAN o en un botón | Un chequeo obligatorio falló. El motivo exacto está en la línea `[BLOCK]` o bajo el botón. | Lee esa línea, corrige la causa y vuelve a hacer PLAN. No hay forma de saltarse un BLOCK. |
| **SDR desconectado** (`MAIN / port listening` o `MAIN / SDR presence` en BLOCK; `main_sdr` DOWN en STATUS) | `rtl_tcp` no escucha o no ve el dongle | Revisa el USB y el serial 00000001. Si **no hay captura activa**, reinicia `rtl_tcp` (Anexo B) y vuelve a hacer PLAN. **[P en hardware]** |
| **Montura no disponible** (`INDI/mount` en BLOCK: «controller is not connected», «failed to connect to INDI server»; o «mount is parked» o «mount state …») | Ekos/INDI no corre, la montura no está conectada, está aparcada o se está moviendo | Arranca el perfil de Ekos, conecta y desaparca la montura, espera a que termine de moverse y vuelve a pulsar **REAL PREFLIGHT**. **[P en hardware]** |
| **Perfil incompatible** (`Quicklook / calibration match` en BLOCK) | La ganancia, la frecuencia o el muestreo del plan no coinciden con el perfil | Elige un perfil a esa ganancia, crea uno (Ruta B) o desactiva QUICKLOOK. **[H 2026-10-08]** |
| **Captura fallida** (`FAILED`, o FAILED > 0 en SESSION) | Un punto o la sesión no se pudo capturar | Los puntos ya guardados se conservan. Mira `orchestrator_capture.log`. Si faltan zonas, planifica una sesión nueva para esa zona. **No lances una segunda captura sobre la misma sesión.** |
| `DEGRADED` | Algo está atrasado o silencioso, sin que eso implique pérdida de datos | Revisa `last_successful_point_id`, `acquisition_stale` y `error` antes de actuar. |
| Sale «RUNNING» pero ya terminó | Etiqueta vieja | La web ya muestra el estado efectivo. Recarga la página. |

---

## Anexo A — Detalles técnicos

- **Servicios:**
    - `almita-observe-api` es la web única en :8088, con autenticación. Reiniciarlo **no corta capturas**
      (`KillMode=process`).
    - `almita-console-watcher` y `almita-system-blackbox` completan la lista de servicios de ALMITA.
    - `rtl_tcp`: `rtl_tcp -d 00000001 -a 127.0.0.1 -p 1234 -f 1420405752 -s 2400000 -g 40.2 -T`.
- **Puertos:** MAIN `rtl_tcp` en 127.0.0.1:1234; RFI REF en 127.0.0.1:1235 (lo abre la sesión); INDI en 7624.
  El puerto 8090 está retirado.
- **Fuente única de frecuencia:** `observer_config.json` → `observation_defaults`, leído por `sdr_tuning`. Lo
  usan OBSERVE, el wizard y ALIGN. Ojo: ALIGN usa su propia ganancia fija de 40.2 dB.
- **Compatibilidad de perfil:** compara frecuencia, muestreo, ganancia y topología; no compara la fecha. El
  perfil para OBSERVE solo se construye con 50 Ω en **LNA_INPUT**.
- **Duración en el plan:** la estimación usa unos 19 s de sobrecarga por punto (p90 25 s, ×1.15 en la versión
  conservadora). Lo real fue 32 s por punto con capturas de 10 s.
- **Visibilidad:** `capture.py` recalcula la altura al hacer cada GOTO y difiere el punto si no cumple
  (contador DEFERRED).

**Archivos:**

| qué | dónde |
|---|---|
| Sesiones | `data/mosaic/<sesión>/` (H5 en `data/`, `orchestrator_capture.log`, `orchestrator_quicklook.log`) |
| Gain pilot | `gain_pilot_state.json` dentro de la sesión |
| Perfiles | `data/calibration/WIZARD-…/` |
| ALIGN | `data/alignment/` |
| Trabajos de la web | `data/runtime/web_ops/<job>/job.log` |

- **PIPELINE** reúne preflight, ALIGN, CALIBRATE, REDUCE y SCIENCE en una página. Este manual usa las páginas
  dedicadas, que son las que tienen todos los controles.

## Anexo B — Comandos de recuperación

Antes de reiniciar cualquier cosa, **comprueba en un paso aparte** que no haya captura ni trabajo activo: SESSION
en MONITOR, o `ps -ef | grep '[c]apture.py'`. Para detener `capture.py` usa siempre **SIGINT**, nunca SIGTERM.

```
systemctl is-active almita-observe-api almita-console-watcher almita-system-blackbox rtl_tcp
ss -lntp | grep -E ':8088|:1234|:7624'
journalctl -u rtl_tcp.service -n 50 --no-pager
journalctl -u almita-observe-api.service -n 50 --no-pager
sudo systemctl restart rtl_tcp.service             # solo sin captura activa
sudo systemctl restart almita-observe-api.service  # no corta capturas
ps -ef | grep '[c]apture.py'; ps -ef | grep '[q]uicklook_live.py'
```

En terreno **nunca** ejecutes `git clean`, `git reset --hard`, `git checkout .` ni `git add .`, y no borres nada
de `data/`. Más detalle en `FIELD_RUNBOOK.md` §7–§12.

## Anexo C — Comprobaciones físicas que faltan

Agrupadas para hacerlas en una misma sesión con el operador:

1. **STOP OBSERVATION real:** con una grilla corta en curso, pulsar STOP y confirmar que termina en `ABORTED`, que
    los `.h5` están completos y que no queda ningún `.part`.

2. **STOP CURRENT STEP del wizard** durante una captura real de 50 Ω o HI (BL-007).
3. **Ekos:** dejar escrito el procedimiento real de alineación (nombres de menús, plate solving o estrellas,
    SYNC) y cómo se sincronizan la hora y el sitio con OnStep.

4. **Antena B:** confirmar si tiene un LNA que necesite el Bias-T de RFI REF.
5. **Fallos simulados a mano:** desconectar el USB de MAIN y comprobar el BLOCK y la recuperación de `rtl_tcp`;
    apagar o desconectar INDI y comprobar el BLOCK `INDI/mount`.

6. **ALIGN SOLAR con SYNC desde la web** (de día): primero con la casilla de SYNC desmarcada para ver el offset;
    después con SYNC, y repetir ALIGN para confirmar que el offset queda cerca de 0.
7. **Recorrido completo de una vez siguiendo este manual,** anotando dónde se aparta de la realidad.
