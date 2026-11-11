# Bitácora ALMITA

**Autor:** Felipe Fridman (CD3FDJ) · **Última actualización:** 2026-11-10

ALMITA es un radiotelescopio casero de la línea de 21 cm del hidrógeno neutro (1420,4 MHz), construido desde cero y por prueba y error, sin copiar proyectos de otros. Esta bitácora cuenta en qué estado está el instrumento, qué se decidió y por qué, y qué se ha medido de verdad, con fecha. Cuando algo es un modelo o una estimación, se dice.

---

## 1. Estado actual del instrumento (2026-11-10)

| Parte | Qué es hoy |
|---|---|
| **Plato** | Antena de grilla WiFi modificada, 90 × 60 cm, alimentada al centro. Barras horizontales (a lo largo del lado de 60 cm). Atrás, una malla de acero de 3–4 mm pegada y curvada con la grilla. Profundidad: 200 mm en el eje ancho y 67 mm en el angosto, así que el plato no tiene un solo foco. |
| **Feed v1 (casero)** | Dipolo horizontal de cinta de cobre, **paralelo a las barras**. En armado: placa reflectora de 12 × 12 cm a 45 mm del dipolo y distancia focal de 240 mm, en caja impresa (ASA o PETG blanco). Todavía **sin balún**: está planificado un balún Pawsey de 52 mm. |
| **LNA** | Nooelec SAWbird+ H1, a ~11 cm del dipolo y fuera de la caja (para poder cambiar la antena por la carga de 50 Ω). Se alimenta con el Bias-T del SDR. |
| **Receptor** | RTL-SDR Blog V3 (EEPROM reprogramada como MAIN, serial 00000001), enchufado directo a la Raspberry Pi 4. Entre LNA y SDR, 2 m de RG-58. |
| **En el trípode** | Raspberry Pi 4: servidor `rtl_tcp`, agente de instrumento de solo lectura, INDI (KStars/Ekos) para la montura. 1-Wire **apagado** por ahora (ver pendientes). |
| **En la casa** | Notebook con Ubuntu: web de ALMITA, captura, procesamiento y datos. Conectado a la Pi 4 por cable de red directo. |
| **Montura** | OnStep (LX200) vía INDI. |
| **Termómetros** | 4 × DS18B20: `sdr`, `lna`, `cable` y `load` (la carga de 50 Ω). Configurados en `observer_config.json`. |
| **Sintonía** | 1420,405752 MHz, 2,4 MS/s, ganancia 40,2 dB. Una sola fuente: `observer_config.json`. |
| **Haz** | ~20° (FWHM). |

## 2. El flujo de software

| Página | Para qué sirve |
|---|---|
| **ALIGN** | Alinear la montura con el Sol o con el HI. Incluye la sincronización con el Sol (SUN SYNC). |
| **CALIBRATE** | Asistente con la carga de 50 Ω en la entrada del LNA y cielo frío/caliente. **Desde 2026-11-10 calcula el factor Y** (carga / cielo frío), y con la temperatura de la carga, la Tsys. |
| **OBSERVE** | Grillas y mosaicos sobre el cielo. Opción **FREQUENCY SWITCHING** (conmutación de frecuencia). |
| **QUICKLOOK / MONITOR** | Vista en vivo mientras se observa, con máscaras de interferencia (RFI). |
| **REDUCE** | Convierte las capturas en espectros reducidos. Opciones: **RFI MASK** y **ESTIMATOR v2**. Corre en varios núcleos. |
| **SCIENCE** | Mapas del cielo, con el mapa esperado según HI4PI justo debajo para comparar formas. |
| **STATUS / TRANSIT / LOBE / SESSIONS** | Salud del sistema, tránsitos del Sol, haz de la antena y catálogo de sesiones. |

REDUCE y SCIENCE tienen sus motores **congelados**: cualquier cambio queda registrado con un commit y una referencia ("pin") autorizada. Las mejoras nuevas se agregan como opciones que envuelven al motor sin modificarlo, y sin la opción se obtiene exactamente el resultado de antes.

## 3. Resultados reales, con fecha

| Fecha | Qué se midió | Resultado |
|---|---|---|
| 2026-10-17 | Salud del V3 con carga de 50 Ω (interior) | A 1 s, el ruido calza con el límite teórico del radiómetro (Allan 6,98·10⁻⁴ contra 6,45·10⁻⁴ ideal). Sobre ~8 s domina la deriva de ganancia. Ninguna línea espuria con carga: las ~11 líneas vistas con antena entraban por la antena. |
| 2026-10-18 | Factor Y a mano | **Y ≈ 1 dB**, cuando se esperaban 5–6 dB. Ese día el conector del LNA tenía mal contacto (saltos de +6 dB al moverlo), así que las pruebas de Sol anteriores a las 16:57 no valen. |
| 2026-10-27 | Conector SMA del feed (NanoVNA + multímetro) | Cortocircuito intermitente (primer crimpado). Rehecho: ROE 1,29 a 1420 MHz. |
| 2026-10-28/29 | Cacería de interferencias (interior) | La **peineta de 357,14 kHz** y el bosque de líneas los **irradia la Raspberry Pi 5 por el aire**: desaparecen a 3 m y vuelven a 30 cm. El arnés de termómetros actuaba como antena. La NanoVNA barriendo también irradia. Ferritas en el USB del SDR: piso −6 dB. |
| 2026-11-01 | Raspberry Pi 4 contra Pi 5 | La Pi 4 queda **limpia a ≥1 m** del feed y sucia a 30 cm. Una persona cerca de la Pi o del feed acopla la peineta (+2 dB, ~30 líneas). El cable USB de 1,2 m hacía de antena: con el SDR directo en la Pi 4, el efecto desapareció. |
| 2026-11-03 | Portadora en 1420,04 MHz | +30 dB. Entra por el dipolo; origen desconocido. Aparece cuando se leen velocidades como +77 km/s. |
| **2026-11-05** | **Primera detección de HI** (azotea) | Corrida 23:01:25, 5 × 5 en l = +20°. Los espectros medidos coinciden con los que predice HI4PI llevados a nuestra referencia de velocidad: correlación +0,43…+0,77 en 6 puntos; con el eje espejado, −0,32…+0,11. El pico sigue punto a punto el corrimiento previsto. Queda pendiente repetirla y confirmarla con REDUCE/SCIENCE. |
| 2026-11-05 | Calibración de esa noche (recalculada el 10-11) | **Y = 1,53 dB.** Con la carga a ~17 °C (supuesto): Tsys ≈ 246 K. Se pierden ~4 dB antes del LNA. El punto HI ALTO salió 4,6 dB **sobre** la carga: interferencia. |
| 2026-11-09 | Conmutación de frecuencia (banco) | La forma de banda del receptor (±14,6 %) cae a ±0,12–0,19 % en el espectro plegado. Pausa medida tras cada cambio: 0,15 s; 95 % del tiempo grabando. |
| 2026-11-10 | Estimador espectral | El estimador clásico de REDUCE (Hann sin solape + mediana) aprovecha ~32 % de la integración. ESTIMATOR v2 (solape 50 % + promedio + curtosis espectral) aprovecha ~95 %. En datos reales con conmutación, ruido ÷1,7–2,1 (3 a 4,6 veces el tiempo efectivo). Sin conmutación, la estructura del receptor domina y no se nota. |
| 2026-11-10 | Corrida de 400 puntos con conmutación (banco, LNA sin antena) | Los 400 puntos completos; REDUCE con ESTIMATOR v2 en 7 núcleos tardó 3 min 50 s. Ruido por punto a 19 kHz: 0,29 %, prácticamente el límite teórico. Forma común de banda: 0,06 % rms. Cada punto trae un desnivel parejo propio de ~0,16 % (5 veces el ruido, sin deriva), y eso define el piso del mapa integrado de SCIENCE. La portadora de 1420,0398 MHz (+77 km/s) está en los 400 puntos, con +70 % a +340 %, y creció durante la corrida. La curtosis no la marca, porque es estable: quedó en la lista fija `known_rfi_lines.json`, y la MÁSCARA RFI la tapa (11 bins, en los 400 puntos). Sin antena: no dice nada del cielo. |
| 2026-11-10 | Balún Pawsey (modelo NEC + cálculo del PVC) | Con dos RG-58 pegados, la chaqueta de PVC baja mucho la velocidad de la onda entre las mallas (factor ~0,73), así que el balún mide **~37–38 mm entre soldaduras**, no los 52 mm del cuarto de onda en el aire. El equilibrio no depende del largo: corta la corriente que se fuga por fuera del coaxial en 30–38 dB en todo el rango (sin balún se fuga ~34 %). El modelo da para el dipolo ~160 Ω: ROE ~3,3 en 1420 MHz, por confirmar con el NanoVNA. |
| 2026-11-10 | GOES (1694,1 MHz) con el feed actual | Modelo NEC: ROE ~9,4 y solo ~35 % de la potencia llega al LNA; además el filtro del SAWbird+ H1 corta esa banda. Para GOES hace falta otro dipolo (~72 mm) y otro LNA. |
| 2026-11-11 | Curva de rotación: el método, validado con HI4PI | Con HI4PI a 2° el método del punto tangente da una curva plana de ~211–223 km/s (R 1,1–2,5 kpc). Con un haz de 20° el gas tangente queda en apenas ~2–5 % del pico y el borde aparente cae mucho: por eso la comparación honesta es ALMITA contra HI4PI pasado por el mismo haz. El borde pide 2–5 min por punto con conmutación. |
| 2026-11-11 | BL-105 construido (rama aparte) | Conmutación con velocidad central (`auto` hacia el borde terminal, o un número fijo como +270 para la Nube Mayor), plegado con línea base fuera de la línea, herramienta de curva de rotación y diagrama longitud–velocidad, centro en la prueba de hidrógeno de STATUS y conversión a Kelvin desde el Y-factor. Todo en la rama `bl-105-fs-center`, probado, **sin juntar con lo que corre en vivo**: falta la prueba de banco con el SDR. |
| 2026-11-11 | Salto de conmutación configurable (Nubes de Magallanes) | Felipe: «quiero aprovechar la circunpolaridad de las nubes!». Con el salto de siempre (±600 kHz = 253 km/s) el gas local de nuestra galaxia, a ~0 km/s, deja un fantasma negativo en ~+253 km/s: justo encima de la Nube Mayor (~+260), y hacia allá ese gas es varias veces más brillante que la Nube. Con ±300 kHz el fantasma cae en ~+127 y la ventana crece a ±148 km/s. Nuevo: `frequency_switching_offset_hz` en la especificación, `--fswitch-offset-hz` en capture.py, selector SWITCH OFFSET en OBSERVE. El planificador marca el gas local y sus fantasmas para que REDUCE no ajuste la línea base encima, y avisa si un fantasma cae en la ventana de la línea. Nube Menor: sirve el salto de siempre centrada en +145 (en +160 el fantasma ya roza el borde). Prueba de punta a punta con una Nube Mayor falsa: con 600 kHz el fantasma se la come, con 300 kHz sale entera. Rama `bl-105-fs-center`, falta la prueba de banco. |

## 4. Decisiones y por qué

| Decisión | Por qué |
|---|---|
| **Feed casero en vez de comprar uno** (KrakenRF en pausa) | El proyecto es aprender desde cero. El Kraken queda como respaldo si el feed v1 no rinde en la azotea. |
| **Dipolo paralelo a las barras** | Una grilla refleja solo el campo eléctrico paralelo a sus barras. Cruzado, el plato es casi transparente. |
| **Placa de 12 cm a 45 mm, foco a 240 mm** | Modelo NEC: eficiencia ~55 %, Y esperado ~5,5 dB (modelo, no medido). |
| **LNA pegado al dipolo** | Cada cm de cable antes del LNA suma ruido. El Y ≈ 1 dB apuntaba a pérdidas antes del LNA. |
| **La malla de acero se queda** | Modelo NEC: nunca empeora y mejora entre 0,4 y 3,9 dB según la separación de las barras. |
| **Balún Pawsey** (por hacer) | Sin balún, la malla del coaxial se vuelve un tercer brazo: deforma el haz y recoge el ruido de la electrónica cercana. Mide λ/4 en aire = 52 mm, porque la línea la forman las caras externas de las mallas con aire entre ellas. |
| **Dos computadores**: Pi 4 en el trípode, notebook en la casa | La Raspberry irradia. Solo lo mínimo va cerca de la antena. |
| **Una sola fuente de frecuencia y sintonía con acuse** | Hubo tres incidentes de frecuencia declarada distinta de la real. Ahora cada captura sintoniza explícitamente y espera la confirmación de `rtl_tcp`. |
| **Factor Y dentro de la calibración** | Es la cifra que dice si la antena funciona. Carga y cielo se miden con el mismo proceso de `rtl_tcp`, porque al reiniciarlo la ganancia salta ~1,7 dB. |
| **Conmutación de frecuencia** | Las ondas de ±2–6 % en los espectros eran la forma de banda del receptor derivando entre mediciones separadas por minutos. Alternar el oscilador cada pocos segundos las cancela. |
| **Motores congelados y opciones "opt-in"** | Reproducibilidad: un resultado de ayer se puede rehacer igual hoy. |
| **REDUCE en varios núcleos** | Mismos resultados, bit a bit, en menos tiempo. Con una captura en curso usa solo la mitad de los núcleos y siempre con prioridad baja. |

## 5. Reglas de oro (aprendidas a golpes)

1. **Nadie a menos de ~1,5 m** de la Raspberry o del feed durante una medición. Anotar quién está dónde.
2. **NanoVNA en pausa o desenchufada** durante cualquier medición de RF.
3. **Una variable a la vez.** Si cambian dos cosas juntas, el resultado no enseña nada.
4. **En interiores no se sacan conclusiones del cielo.** Primero hay que dejar anotado si la medición fue adentro o a cielo abierto.
5. **Carga y cielo en el mismo proceso de `rtl_tcp`**: sin reinicio entre medio.
6. **Bias-T:** al poner una carga de 50 Ω o algo sin LNA, verificar el bloqueo de continua o apagar el Bias-T.
7. **Todo crimpado nuevo se prueba con multímetro** (moviéndolo) **y con NanoVNA** antes de usarlo.
8. **Parar `capture.py` con Ctrl-C (SIGINT)**, nunca matándolo, para que la montura quede detenida y confirmada.
9. **Después de reiniciar el SDR, revisar que entregue señal** antes de medir.
10. **Respaldar los datos** antes de reutilizar un equipo. Los datos de octubre se perdieron con la Pi 5.

## 6. Pendientes

- [ ] Balún Pawsey (o manga de cobre) en el feed v1; recortar los brazos con la NanoVNA, ya con la caja cerrada y la placa puesta.
- [ ] Activar 1-Wire en la Pi 4 (`dtoverlay=w1-gpio` y reiniciar), para que la calibración lea la temperatura de la carga.
- [ ] Noche en la azotea: plano galáctico con conmutación de frecuencia + ESTIMATOR v2, y calibración con factor Y.
- [ ] Repetir la detección del 5-11 y confirmarla con REDUCE y SCIENCE.
- [ ] Nubes de Magallanes: conmutación con velocidad central configurable (BL-105).
- [ ] Llevar el ESTIMATOR v2 a QUICKLOOK, al panel HYDROGEN LINE y al factor Y.
- [ ] Perfilar SCIENCE antes de decidir si se paraleliza.
- [ ] Ordenar el repositorio en GitHub (archivos sueltos, ramas, historial pesado).

## 7. De dónde viene

Las antenas me fascinan desde los 5 años; a los 10 armé mi primer transmisor de FM. A los 8, buscando estaciones de onda corta, capté un pedazo de *Theme One* de George Martin, probablemente de la BBC, y ahí partió el amor por la radio. A través de ACHAYA conocí el primer radiotelescopio de Chile, originalmente en el observatorio de Maipú, que sigue funcionando con sus receptores Collins de válvulas.

ALMITA nació como unos scripts y un dipolo. Lo vio un equipo de ALMA en un observatorio del Cajón del Maipo, y de ahí vino la invitación a presentarlo en un coloquio de ALMA.
