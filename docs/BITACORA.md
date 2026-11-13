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
| 2026-11-12 | Feed nuevo: dipolo con balún Pawsey en RG-58 (NanoVNA + NanoVNA Saver, calibrado 1,2–1,6 GHz) | Brazos de 39 mm; balún de 37–38 mm entre soldaduras (largo que dio NEC con las chaquetas de PVC). **Al aire:** a 1420 MHz, 48 − j32 Ω, ROE 1,91; mínimo en 1496 MHz. **Dentro del casing:** a 1420 MHz, 60 − j6 Ω, **ROE 1,24** (−19,4 dB, ~0,05 dB de pérdida por desadaptación); mínimo ROE 1,14 en 1396 MHz. El plástico bajó la resonancia ~7 %, como se esperaba. La parte resistiva quedó en ~50 Ω. **Con la placa reflectora puesta:** a 1420 MHz, 58 + j51 Ω, ROE 2,5 (−7,4 dB, ~0,9 dB de pérdida por desadaptación); mínimo ROE 2,37 en 1408 MHz. Cortar 2 mm por brazo (a 37 mm) no la arregló: con placa, ROE 2,79. Sin placa, en cambio, el mínimo subió a 1480 MHz (~42 MHz por mm). **Con la placa a 85 mm y brazos de 37 mm:** a 1420 MHz, 56,5 + j2,6 Ω, **ROE 1,14** (−23,6 dB); mínimo ROE 1,03 en 1404 MHz. Según NEC, la eficiencia del plato con la placa a 85 mm (66 %) es mejor que a 45 mm (56 %), y queda cerca del óptimo del modelo (85–90 mm). Configuración que queda: placa a 85 mm. |
| **2026-11-12** | **Factor Y en la azotea** (wizard 19:05–19:10, feed nuevo con balún y placa a 85 mm, carga a 30,7 °C leída por su sensor) | **Y = 2,85 dB** (1,93; por canal entre 2,68 y 3,05), VÁLIDO: mismo proceso, misma sintonía, sin saturación. Tsys ≈ 188 K (176–199 K según el ruido del LNA, que se supone de su hoja de datos, no se midió); antena a la entrada del LNA ≈ 129 K. Casi el doble que el 5-11 (1,53 dB). **Hallazgo:** HI ALTO (plano galáctico, l = 330°, altura 36°) dio 1,7 dB **menos** potencia que HI BAJO (cielo frío, altura 63°), con el Sol más cerca de ALTO. Perfil nuevo para OBSERVE: PROFILE-20261112-221048. **Curva de potencia contra altura (19:15–19:25), con la misma medida del wizard:** azimut 100° (este; abajo lo tapa algo del techo) y 150° (sur-sureste), subiendo de 30° a 75° y bajando. El cielo hacia el este y sur-sureste está **caliente hasta ~60° de altura**: Y = 0,1–1,6 dB a 30–45°, 2,4–3,7 dB a 60°, **4,3–4,6 dB a 75°**. Las alturas bajas, además, fluctúan, lo que sugiere interferencia o terreno (cordillera, ciudad). HI ALTO (suroeste, 36°) dio Y = 4,5 dB. **Conclusión: con cielo limpio, Y ≈ 4,5 dB** (Tsys ≈ 130 K, antena ≈ 70 K, suponiendo el ruido del LNA de su hoja de datos). El 2,85 dB salió bajo porque HI BAJO quedó en el sector caliente (azimut 122°, altura 63°). La hipótesis del suelo por desborde queda descartada: la potencia baja al subir, no sube. |
| **2026-11-12** | **Segundo factor Y** (wizard 20:14–20:25, SDR a ~52 °C, carga a 27,8 °C) | **Y = 4,71 dB** (2,96; por canal 4,59–4,82), VÁLIDO. **Tsys ≈ 122 K**, antena ≈ 63 K (suponiendo el ruido del LNA de su hoja de datos). Esta vez HI BAJO quedó alto (74°), y el valor confirma la curva de potencia contra altura. El contraste HI ALTO contra HI BAJO salió **defendible a 6,2σ** (antes, 2,8σ, no concluyente). Perfil nuevo para OBSERVE: WIZARD-20261112-231442 / calibration_profile_v1. Con esto **el Kraken no hace falta**: la regla era ≥4 dB. |
| 2026-11-12 | Grid B_l20 repetido con conmutación (40 s, 25 puntos) + REDUCE + SCIENCE | Hidrógeno en los 25 puntos, corriéndose en frecuencia entre puntos. Comparado con el 5-11 (quicklook): misma forma de línea (correlación 0,77), base 4 veces más plana, línea/estructura de 3,1 a 14,4. Mapa de SCIENCE contra HI4PI: r = 0,47; nuestro máximo queda desplazado hacia el oeste. Ni el ancho del haz ni un error de apuntado lo explican de forma única (la búsqueda de corrimiento es ambigua y choca con el ALIGN de 0,16°). Hipótesis: la ventana de ±84 km/s de la conmutación y la sombra del gas rápido castigan los puntos del plano. Se comprueba pasando HI4PI por la misma ventana. **Espectro por espectro contra el cubo de HI4PI** (CAR_E01, haz de 20°, los 6 puntos dentro del cubo, l = 5–13°, b = −7…+7°): **correlación r = 0,94–0,99** (el 5-11 daba 0,43–0,77). Nuestro máximo queda 3–10 km/s por encima del de HI4PI (el 5-11 también, +3…+6): falta explicarlo. |
| **2026-11-12** | **Nube Chica de Magallanes (SMC)**, 20:33–20:42, con la conmutación centrada en +168 km/s topocéntrico (rama BL-105, sin pasar a producción), ±600 kHz, 2 min por pasada: SMC, punto vacío (RA 20 h, Dec −55°), SMC | Las dos pasadas a la SMC muestran la **misma joroba** (correlación 0,99), de +5,0 % y +4,1 %, centrada en **~+165 km/s LSRK** y de ~50–60 km/s de ancho. El punto vacío no la tiene. SMC menos vacío: +4,1 %; el vacío tiene ondulaciones de ±2 % que fijan la incertidumbre. ~~Detección probable~~ → **NO CONFIRMADA.** Una hora después, el corte SMC → vacío (21:33–21:51, 6 puntos y la SMC repetida al final, mismo método) no muestra la joroba en la SMC (−0,25 % y −1,55 %), y los puntos lejanos dan +1,6 a +1,9 %. Los puntos sueltos varían ±2 % por efectos del receptor. La ondulación de la conmutación depende de la dirección (alta hacia la SMC, que está al sur, en el sector caliente de la curva contra altura) y no de la temperatura (cambió 0,25 °C). El grid de las dos Nubes a 300 kHz y 48 s tampoco sirvió (r = −0,14 con HI4PI). Para las Nubes hace falta integrar más y alternar Nube y vacío muchas veces. |
| 2026-11-12 | **Grid de alta latitud** (22:00–22:28, RA 22 h, Dec −35°, l ≈ 10°, b ≈ −52°, 40° × 40°, 5 × 5, 40 s, conmutación, oeste) + REDUCE (RFI MASK, sin ESTIMATOR v2) + SCIENCE | Gas local débil (~1–3 % sobre Tsys). Mapa de SCIENCE contra HI4PI (todas las velocidades): **r = 0,62** (25 puntos), mejor que el 0,47 de l = 20°. Reproduce el degradado: la columna oeste más brillante, como en HI4PI. Pero cada punto tiene un error propio grande: los vecinos no se parecen (r = 0,12), la validación dejando uno fuera da r = 0,39, y hay valores integrados negativos (imposibles) en una esquina. Señal real, con mucha incertidumbre por punto. |
| 2026-11-12/13 | **Nube Grande (LMC): barrido horizontal** (23:46–00:19, a la altura de la LMC (43°), de −45° a +30° en azimut cada 7,5°, más la LMC repetida; rama BL-105, ±300 kHz, centro +278 km/s topocéntrico, 2 min por punto) | **NO DETECTADA.** En la LMC, el promedio en +230…+300 km/s LSRK sale **negativo** (−1,65 % y −1,62 % en la repetición). Puntos vecinos separados 7,5° saltan de −1,6 a +3,0 y vuelven a −1,6, algo físicamente imposible con un haz de 20°: es la base de la conmutación. Hallazgo útil: la repetición en la MISMA dirección da casi el mismo valor, con 13 min de diferencia. La base depende de hacia dónde apunta el plato y se repite; no es azar. |

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
| **Medir el haz en cruz con el Sol y calcular el área efectiva** (2026-11-11, rama `lobe-cross-scan`) | Hasta ahora el haz se supone redondo de 20°, y eso entra en la comparación con HI4PI, en los kelvin y en BL-105. Un plato de 90×60 debería dar un haz ovalado. LOBE ahora barre el Sol en RA y en Dec (CROSS, ~22 min) y entrega los dos anchos y el ángulo sólido. `lobe_aeff.py` baja de NOAA el flujo del Sol a 1415 MHz (promedio de San Vito y Learmonth, las dos estaciones confiables: en 30 días casi no fallan y coinciden en ~2–3 %, mientras Kaena Point lee ~9 % más bajo y Sagamore Hill casi no mide; dónde está la estación no importa, el flujo del Sol es el mismo para toda la Tierra) y saca el área efectiva y la eficiencia: una escala en kelvin independiente del factor Y. Área del plato supuesta: 0,463 m² (extremos redondeados; falta medir el tramo recto). Corre de 09:30 a 12:00 o de 15:00 a 17:00, no al mediodía. Sin probar en el cielo. |

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

- [x] Balún Pawsey en el feed v1 (2026-11-12): brazos de 37 mm y placa a 85 mm, ROE 1,14 a 1420 MHz. Falta confirmarlo en el cielo con el factor Y.
- [x] 1-Wire activado en la Pi 4 (2026-11-12, `dtoverlay=w1-gpio`). Los 4 sensores (sdr, lna, cable, carga) se leen desde el notebook a través del agente.
- [ ] Noche en la azotea: plano galáctico con conmutación de frecuencia + ESTIMATOR v2, y calibración con factor Y.
- [ ] Repetir la detección del 5-11 y confirmarla con REDUCE y SCIENCE.
- [ ] Nubes de Magallanes: conmutación con velocidad central configurable (BL-105).
- [ ] Medir el haz en cruz con el Sol (LOBE CROSS) y el área efectiva con `lobe_aeff.py`, en una salida de día después de un ALIGN solar PASS.
- [ ] Llevar el ESTIMATOR v2 a QUICKLOOK, al panel HYDROGEN LINE y al factor Y.
- [ ] Perfilar SCIENCE antes de decidir si se paraleliza.
- [ ] Ordenar el repositorio en GitHub (archivos sueltos, ramas, historial pesado).

## 7. De dónde viene

Las antenas me fascinan desde los 5 años; a los 10 armé mi primer transmisor de FM. A los 8, buscando estaciones de onda corta, capté un pedazo de *Theme One* de George Martin, probablemente de la BBC, y ahí partió el amor por la radio. A través de ACHAYA conocí el primer radiotelescopio de Chile, originalmente en el observatorio de Maipú, que sigue funcionando con sus receptores Collins de válvulas.

ALMITA nació como unos scripts y un dipolo. Lo vio un equipo de ALMA en un observatorio del Cajón del Maipo, y de ahí vino la invitación a presentarlo en un coloquio de ALMA.
