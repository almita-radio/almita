# GUÍA VISUAL - Flujo de Operaciones

## 🔄 Diagrama de Flujo de Conexión

```
┌─────────────────────────────────────────────────────────────┐
│  INICIO                                                      │
└────────────────────────┬────────────────────────────────────┘
                         │
                         ▼
         ┌───────────────────────────────┐
         │ TelescopeController()          │
         │ - host: localhost              │
         │ - port: 7624                   │
         │ - device_name: None            │
         └───────────────┬────────────────┘
                         │
                         ▼
         ┌───────────────────────────────┐
         │ connect_to_server()            │
         │ 1. Crear INDIClient()          │
         │ 2. Conectar a servidor         │
         └───────────────┬────────────────┘
                         │
                         ▼
         ┌───────────────────────────────┐
         │ Esperar dispositivos           │
         │ (timeout: 10s)                 │
         └───────────────┬────────────────┘
                         │
                ┌────────┴────────┐
                │                 │
         ┌──────▼──────┐   ┌─────▼─────┐
         │ Dispositivos │   │    No     │
         │  disponibles │   │dispositivos│
         └──────┬───────┘   └─────┬─────┘
                │                 │
                ▼                 ▼
    ┌───────────────────┐  ┌──────────┐
    │ Seleccionar montura│  │  ERROR   │
    │ (auto/manual)       │  │  RETURN  │
    └──────────┬─────────┘  └──────────┘
               │
               ▼
    ┌───────────────────────┐
    │ Activar CONNECTION     │
    │ (propiedad INDI)       │
    └──────────┬─────────────┘
               │
               ▼
    ┌───────────────────────┐
    │ ✓ CONECTADO           │
    │ connected = True       │
    └───────────────────────┘
```

---

## 🎯 Diagrama de Flujo de GOTO

```
┌─────────────────────────────────────────────┐
│  goto_coordinates(ra, dec)                  │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
    ┌──────────────────────────────┐
    │ Verificar conexión           │
    └──────┬───────────────────────┘
           │
    ┌──────▼───────┐
    │ ¿Conectado?  │
    └──┬─────────┬─┘
       │ NO      │ SÍ
       ▼         ▼
   ┌────────┐  ┌──────────────────────┐
   │ ERROR  │  │ Obtener propiedad    │
   │ RETURN │  │ EQUATORIAL_EOD_COORD │
   └────────┘  └──────────┬───────────┘
                          │
                          ▼
              ┌──────────────────────┐
              │ Cambiar modo a TRACK │
              │ (ON_COORD_SET)       │
              └──────────┬───────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │ Enviar RA y DEC      │
              │ a propiedad          │
              └──────────┬───────────┘
                         │
                         ▼
              ┌──────────────────────┐
              │ Bucle de espera      │
              │ (hasta timeout 60s)  │
              └──────────┬───────────┘
                         │
       ┌─────────────────┼─────────────────┐
       │                 │                 │
       ▼                 ▼                 ▼
┌──────────┐      ┌──────────┐     ┌──────────┐
│ Estado=OK│      │Estado=   │     │ Timeout  │
│          │      │ALERT     │     │          │
└─────┬────┘      └────┬─────┘     └────┬─────┘
      │                │                 │
      ▼                ▼                 ▼
┌──────────┐      ┌──────────┐     ┌──────────┐
│ ✓ ÉXITO  │      │ ✗ ERROR  │     │ ⚠WARNING │
│ return   │      │ return   │     │ return   │
│ True     │      │ False    │     │ False    │
└──────────┘      └──────────┘     └──────────┘
```

---

## 🔄 Diagrama de Flujo de SYNC (Corrección)

```
┌─────────────────────────────────────────────┐
│  sync_coordinates(ra_real, dec_real)        │
│  ⚙️ COORDENADAS PROPORCIONADAS POR USUARIO  │
└──────────────────┬──────────────────────────┘
                   │
                   ▼
    ┌──────────────────────────────┐
    │ Obtener coordenadas ACTUALES │
    │ (lo que montura CREE tener)  │
    └──────────┬───────────────────┘
               │
               ▼
    ┌──────────────────────────────┐
    │ Calcular ERROR de apuntado:  │
    │ ΔRA = ra_real - ra_actual    │
    │ ΔDEC = dec_real - dec_actual │
    └──────────┬───────────────────┘
               │
               ▼
    ┌──────────────────────────────┐
    │ LOG: Error de apuntado       │
    │ Ejemplo: ΔRA=0.6 min         │
    │          ΔDEC=0.6 arcmin     │
    └──────────┬───────────────────┘
               │
               ▼
    ┌──────────────────────────────┐
    │ Cambiar modo a SYNC          │
    │ (ON_COORD_SET = SYNC)        │
    └──────────┬───────────────────┘
               │
               ▼
    ┌──────────────────────────────┐
    │ Enviar coordenadas REALES    │
    │ (que TÚ proporcionaste)      │
    │ 🚨 SIN MOVER MONTURA         │
    └──────────┬───────────────────┘
               │
               ▼
    ┌──────────────────────────────┐
    │ Esperar procesamiento (1s)   │
    └──────────┬───────────────────┘
               │
               ▼
    ┌──────────────────────────────┐
    │ Verificar coordenadas nuevas │
    │ (deben ser las reales)       │
    └──────────┬───────────────────┘
               │
               ▼
    ┌──────────────────────────────┐
    │ Restaurar modo TRACK         │
    │ (ON_COORD_SET = TRACK)       │
    └──────────┬───────────────────┘
               │
               ▼
    ┌──────────────────────────────┐
    │ ✓ SYNC COMPLETADO            │
    │ Montura conoce posición real │
    │ Próximos GOTOs más precisos  │
    └──────────────────────────────┘
```

---

## 🔀 Comparación: Proceso GOTO vs SYNC

```
┌─────────────────────────────────────────────────────────────────┐
│                         GOTO                                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────┐                            ┌─────────────┐    │
│  │ Posición    │         🚀 MUEVE           │ Nueva       │    │
│  │ Actual      │  ───────────────────────►  │ Posición    │    │
│  │ RA=10h      │      (físicamente)         │ RA=18.6h    │    │
│  │ DEC=45°     │                            │ DEC=38.8°   │    │
│  └─────────────┘                            └─────────────┘    │
│                                                                 │
│  • La montura SE MUEVE físicamente                             │
│  • Puede tardar minutos                                        │
│  • Tracking activado automáticamente                           │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│                         SYNC                                    │
├─────────────────────────────────────────────────────────────────┤
│                                                                 │
│  ┌─────────────┐                            ┌─────────────┐    │
│  │ Montura cree│         📍 ACTUALIZA       │ Montura sabe│    │
│  │ estar en:   │  ───────────────────────►  │ que está en:│    │
│  │ RA=18.6h    │      (solo memoria)        │ RA=18.61h   │    │
│  │ DEC=38.78°  │                            │ DEC=38.79°  │    │
│  └─────────────┘                            └─────────────┘    │
│       ▲                                             ▲           │
│       │                                             │           │
│       └─── 🚨 NO SE MUEVE FÍSICAMENTE               │           │
│                                                     │           │
│            Coordenadas proporcionadas por TI ──────┘           │
│            (de donde TÚ decidas obtenerlas)                    │
│                                                                 │
│  • La montura NO se mueve                                      │
│  • Instantáneo (< 1 segundo)                                   │
│  • Corrige error de apuntado conocido                          │
│  • TÚ proporcionas las coordenadas correctas                   │
│                                                                 │
└─────────────────────────────────────────────────────────────────┘
```

---

## 🎯 Caso de Uso: Workflow de Observación con Corrección

```
┌────────────────────────────────────────────────────────────────┐
│ WORKFLOW COMPLETO: OBSERVACIÓN PRECISA                         │
└────────────────────────────────────────────────────────────────┘

PASO 1: GOTO Inicial
──────────────────────
┌──────────────┐
│ Comando:     │
│ GOTO(18h,    │   🚀 Montura se mueve
│      38°)    │      (puede tener error de 1-5 arcominutos)
└──────┬───────┘
       │
       ▼
┌──────────────────────┐
│ Montura llega a      │
│ posición aproximada  │
│ RA=18h, DEC=38°      │
└──────┬───────────────┘
       │

PASO 2: TÚ Determinas Posición Real
─────────────────────────────────────
       │
       ▼
┌──────────────────────────────────┐
│ [FUERA DEL SCRIPT]               │
│                                  │
│ TÚ obtienes coordenadas reales   │    🔍 Puedes usar:
│ por el método que prefieras:     │       • Plate solving externo
│                                  │       • Software de análisis
│ - Plate solving (Ekos, etc.)     │       • Mediciones manuales
│ - Software especializado         │       • Cálculos propios
│ - Medición manual                │       • Cualquier otro método
│ - Cálculos astronómicos          │
└──────┬───────────────────────────┘
       │
       ▼
┌──────────────────────────────────┐
│ Coordenadas reales obtenidas:    │
│                                  │
│ RA=18h 01m 20s (18.022h)         │  ⚠️ Error detectado: 1.3 arcmin
│ DEC=38° 02' 10" (38.036°)        │
└──────┬───────────────────────────┘
       │

PASO 3: SYNC (Corrección)
──────────────────────────
       │
       ▼
┌──────────────────────┐
│ TÚ ejecutas:         │
│ SYNC(18.022h,        │    📍 Actualiza posición interna
│      38.036°)        │       SIN mover montura
│                      │       (coordenadas que TÚ proporcionas)
└──────┬───────────────┘
       │
       ▼
┌──────────────────────────────┐
│ ✓ Montura ahora sabe su      │
│   posición REAL              │
└──────┬───────────────────────┘
       │

PASO 4: GOTO Corregido (opcional)
──────────────────────────────────
       │
       ▼
┌──────────────────────┐
│ GOTO(18h, 38°)       │    🎯 Ahora el GOTO será preciso
│ de nuevo             │       porque partimos de posición
│                      │       correcta
└──────┬───────────────┘
       │
       ▼
┌──────────────────────────────┐
│ ✓ Montura llega con          │
│   precisión < 10 arcsec      │
└──────────────────────────────┘
```

---

## 🔧 Estados INDI Durante las Operaciones

```
┌────────────────────────────────────────────────────────┐
│  ESTADOS DE PROPIEDADES INDI                           │
└────────────────────────────────────────────────────────┘

IDLE (Inactivo)
───────────────
🟢 Verde
• Dispositivo listo
• No hay operación en curso
• Esperando comandos


BUSY (Ocupado)
───────────────
🟡 Amarillo
• Operación en progreso
• GOTO en movimiento
• Tracking ajustándose
• Esperar hasta OK


OK (Exitoso)
────────────
🟢 Verde
• Operación completada
• GOTO llegó a destino
• SYNC aplicado correctamente
• Listo para siguiente comando


ALERT (Error)
─────────────
🔴 Rojo
• Operación falló
• GOTO no pudo completarse
• Coordenadas fuera de límites
• Error de hardware


TRANSICIÓN TÍPICA DURANTE GOTO:
─────────────────────────────────
IDLE  →  BUSY  →  OK
🟢        🟡       🟢
         (30-120s dependiendo de distancia)


TRANSICIÓN DURANTE SYNC:
────────────────────────
IDLE  →  OK
🟢        🟢
         (< 1 segundo)
```

---

## 📊 Tabla de Comparación de Operaciones

```
┌─────────────┬──────────┬──────────┬──────────┬───────────────┐
│ Operación   │ Mueve    │ Duración │ Estado   │ Cuándo Usar   │
│             │ Montura? │ Típica   │ Final    │               │
├─────────────┼──────────┼──────────┼──────────┼───────────────┤
│ GOTO        │ ✓ SÍ     │ 30-120s  │ OK       │ Apuntar a     │
│             │          │          │          │ nuevo objetivo│
├─────────────┼──────────┼──────────┼──────────┼───────────────┤
│ SYNC        │ ✗ NO     │ < 1s     │ OK       │ Cuando TÚ     │
│             │          │          │          │ conoces las   │
│             │          │          │          │ coordenadas   │
│             │          │          │          │ reales        │
├─────────────┼──────────┼──────────┼──────────┼───────────────┤
│ SET_TRACKING│ ✗ NO     │ < 1s     │ OK       │ Activar/      │
│             │          │          │          │ desactivar    │
│             │          │          │          │ seguimiento   │
├─────────────┼──────────┼──────────┼──────────┼───────────────┤
│ GET_TRACKING│ ✗ NO     │ < 0.1s   │ N/A      │ Verificar     │
│             │          │          │          │ estado actual │
└─────────────┴──────────┴──────────┴──────────┴───────────────┘
```

---

## 🔄 Flujo Asyncio (vs Síncrono)

```
┌─────────────────────────────────────────────────────────┐
│ CÓDIGO SÍNCRONO (pyindi-client tradicional)            │
└─────────────────────────────────────────────────────────┘

client.goto(ra1, dec1)      ⏳ Espera 60s (bloqueado)
client.goto(ra2, dec2)      ⏳ Espera 60s (bloqueado)
client.goto(ra3, dec3)      ⏳ Espera 60s (bloqueado)
                            ────────────────────────────
                            TOTAL: 180 segundos

❌ NO puedes hacer nada mientras esperas
❌ Un solo dispositivo a la vez


┌─────────────────────────────────────────────────────────┐
│ CÓDIGO ASÍNCRONO (INDIpy)                              │
└─────────────────────────────────────────────────────────┘

task1 = mount.goto(ra, dec)         ⚡ No bloquea
task2 = camera.capture(30)          ⚡ Simultáneo
task3 = focuser.adjust()            ⚡ Simultáneo
await asyncio.gather(task1, task2, task3)
                            ────────────────────────────
                            TOTAL: 60 segundos (el más lento)

✓ Múltiples operaciones en paralelo
✓ Control de montura + cámara + focuser simultáneamente
✓ Mayor eficiencia en observaciones


EJEMPLO PRÁCTICO:
─────────────────

# SÍNCRONO (3 minutos)
for fuente in fuentes:
    goto(fuente)     # 60s
    captura()        # 30s
    procesa()        # 30s
# Total: 3 x 120s = 6 minutos

# ASÍNCRONO (4 minutos)
for fuente in fuentes:
    await asyncio.gather(
        goto(fuente),      # 60s
        preparar_camara()  # 10s (paralelo)
    )
    await captura()        # 30s
    await procesa()        # 30s (puede paralelizar con siguiente GOTO)
# Total: ~4 minutos (33% más rápido)
```

---

*Guía visual desarrollada para facilitar comprensión de INDIpy - Enero 2025*
