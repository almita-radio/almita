#!/usr/bin/env python3
"""
Control de Telescopio INDI - Script de pruebas individuales
Permite ejecutar comandos GOTO, SYNC, TRACKING por separado
Con información detallada del protocolo INDI
"""

import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime
from typing import Optional, Dict, Tuple
import argparse
import sys
import re

class INDITelescopeControl:
    """
    Controlador de telescopio INDI con comandos individuales
    y debugging detallado del protocolo
    """
    
    def __init__(self, host: str = "localhost", port: int = 7624, 
                 device_name: str = "Telescope Simulator", verbose: bool = False):
        self.host = host
        self.port = port
        self.device_name = device_name
        self.verbose = verbose
        self.reader: Optional[asyncio.StreamReader] = None
        self.writer: Optional[asyncio.StreamWriter] = None
        self.current_ra: Optional[float] = None
        self.current_dec: Optional[float] = None
        self.cached_properties: str = ""  # Cache de propiedades recibidas
        
    def log(self, message: str, level: str = "INFO", force: bool = False):
        """Imprime mensaje con timestamp
        
        Args:
            message: Mensaje a imprimir
            level: Nivel del mensaje (INFO, WARNING, ERROR, VERBOSE)
            force: Si True, imprime siempre ignorando verbose
        """
        # Solo mostrar si: verbose está activado, es error/warning, o force=True
        if self.verbose or level in ["ERROR", "WARNING"] or force:
            timestamp = datetime.now().strftime('%Y-%m-%d %H:%M:%S.%f')[:-3]
            print(f"[{timestamp}] [{level}] {message}")
            sys.stdout.flush()
    
    def log_verbose(self, message: str):
        """Log solo en modo verbose"""
        if self.verbose:
            self.log(message, "VERBOSE", force=True)
    
    def explain_indi(self, operation: str):
        """Explica la operación INDI que se está realizando (solo en modo verbose)"""
        if not self.verbose:
            return
            
        explanations = {
            'connect': """
╔══════════════════════════════════════════════════════════════════════════════╗
║ OPERACIÓN INDI: CONEXIÓN AL DISPOSITIVO                                     ║
╚══════════════════════════════════════════════════════════════════════════════╝

📡 PROTOCOLO INDI:
   INDI usa XML sobre TCP para comunicación cliente-servidor.
   
🔧 QUÉ ESTAMOS HACIENDO:
   1. Abrir conexión TCP al servidor INDI (puerto 7624 por defecto)
   2. Enviar <getProperties/> para enumerar dispositivos
   3. Buscar el dispositivo de telescopio
   4. Enviar comando CONNECTION con CONNECT=On
   
📤 COMANDO XML QUE SE ENVÍA:
   <newSwitchVector device="Telescope Simulator" name="CONNECTION">
     <oneSwitch name="CONNECT">On</oneSwitch>
     <oneSwitch name="DISCONNECT">Off</oneSwitch>
   </newSwitchVector>
   
📥 RESPUESTA ESPERADA:
   <setSwitchVector device="..." name="CONNECTION" state="Ok">
     <oneSwitch name="CONNECT">On</oneSwitch>
   </setSwitchVector>
   
⏱️  TÍPICAMENTE TARDA: < 1 segundo
""",
            'goto': """
╔══════════════════════════════════════════════════════════════════════════════╗
║ OPERACIÓN INDI: GOTO (SLEW) - MOVIMIENTO DEL TELESCOPIO                     ║
╚══════════════════════════════════════════════════════════════════════════════╝

📡 PROTOCOLO INDI:
   GOTO usa la propiedad EQUATORIAL_EOD_COORD con modo SLEW activo.
   
🔧 QUÉ ESTAMOS HACIENDO:
   1. Verificar que ON_COORD_SET esté en modo "SLEW" (movimiento)
   2. Enviar nuevas coordenadas RA/DEC
   3. El telescopio SE MUEVE físicamente a esas coordenadas
   4. Esperar hasta que state="Ok" (movimiento completado)
   
📤 COMANDO XML QUE SE ENVÍA:
   <newNumberVector device="Telescope Simulator" name="EQUATORIAL_EOD_COORD">
     <oneNumber name="RA">18.615</oneNumber>    <!-- Horas (0-24) -->
     <oneNumber name="DEC">38.783</oneNumber>   <!-- Grados (-90 a +90) -->
   </newNumberVector>
   
📥 PROGRESO DE RESPUESTAS:
   1. state="Busy"  → Telescopio en movimiento
   2. state="Ok"    → Movimiento completado
   3. state="Alert" → Error (fuera de límites, obstrucción, etc.)
   
⚠️  IMPORTANTE:
   • El telescopio SE MUEVE FÍSICAMENTE
   • Puede tardar 30-120 segundos según distancia
   • El tracking se activa automáticamente al llegar
   
⏱️  TÍPICAMENTE TARDA: 30-120 segundos (depende de distancia angular)
""",
            'sync': """
╔══════════════════════════════════════════════════════════════════════════════╗
║ OPERACIÓN INDI: SYNC - CORRECCIÓN DE POSICIÓN SIN MOVIMIENTO                ║
╚══════════════════════════════════════════════════════════════════════════════╝

📡 PROTOCOLO INDI:
   SYNC actualiza la posición interna del telescopio SIN MOVERLO.
   
🔧 QUÉ ESTAMOS HACIENDO:
   1. Leer coordenadas ACTUALES (lo que el telescopio CREE tener)
   2. Cambiar ON_COORD_SET a modo "SYNC"
   3. Enviar coordenadas REALES (las que TÚ proporcionas)
   4. El telescopio actualiza su modelo interno
   5. Restaurar ON_COORD_SET a modo "TRACK"
   
📤 SECUENCIA DE COMANDOS XML:
   
   A) Cambiar a modo SYNC:
      <newSwitchVector device="Telescope Simulator" name="ON_COORD_SET">
        <oneSwitch name="TRACK">Off</oneSwitch>
        <oneSwitch name="SLEW">Off</oneSwitch>
        <oneSwitch name="SYNC">On</oneSwitch>
      </newSwitchVector>
   
   B) Enviar coordenadas REALES:
      <newNumberVector device="Telescope Simulator" name="EQUATORIAL_EOD_COORD">
        <oneNumber name="RA">18.616</oneNumber>
        <oneNumber name="DEC">38.784</oneNumber>
      </newNumberVector>
   
   C) Restaurar modo TRACK:
      <newSwitchVector device="Telescope Simulator" name="ON_COORD_SET">
        <oneSwitch name="TRACK">On</oneSwitch>
        <oneSwitch name="SLEW">Off</oneSwitch>
        <oneSwitch name="SYNC">Off</oneSwitch>
      </newSwitchVector>

📥 RESULTADO:
   • El telescopio NO SE MUEVE
   • Coordenadas internas actualizadas
   • Próximos GOTOs serán más precisos
   
🎯 CUÁNDO USAR SYNC:
   • Después de plate solving (análisis de imagen)
   • Para corregir error de apuntado conocido
   • Al inicio de sesión con estrella de referencia
   
⚠️  IMPORTANTE:
   • El telescopio NO SE MUEVE FÍSICAMENTE
   • Solo actualiza su modelo interno
   • Usar coordenadas de alta precisión (ej: plate solving)
   
⏱️  TÍPICAMENTE TARDA: < 1 segundo
""",
            'tracking': """
╔══════════════════════════════════════════════════════════════════════════════╗
║ OPERACIÓN INDI: TRACKING - SEGUIMIENTO SIDERAL                              ║
╚══════════════════════════════════════════════════════════════════════════════╝

📡 PROTOCOLO INDI:
   Tracking controla el seguimiento del movimiento celeste.
   
🔧 QUÉ ESTAMOS HACIENDO:
   Activar/desactivar el motor de seguimiento que compensa la rotación terrestre.
   
📤 COMANDO XML PARA ACTIVAR:
   <newSwitchVector device="Telescope Simulator" name="TELESCOPE_TRACK_STATE">
     <oneSwitch name="TRACK_ON">On</oneSwitch>
     <oneSwitch name="TRACK_OFF">Off</oneSwitch>
   </newSwitchVector>

📤 COMANDO XML PARA DESACTIVAR:
   <newSwitchVector device="Telescope Simulator" name="TELESCOPE_TRACK_STATE">
     <oneSwitch name="TRACK_ON">Off</oneSwitch>
     <oneSwitch name="TRACK_OFF">On</oneSwitch>
   </newSwitchVector>
   
🌟 MODOS DE TRACKING (ON_COORD_SET):
   • TRACK  → Seguimiento normal, objetos se mantienen centrados
   • SLEW   → Movimiento rápido (GOTO)
   • SYNC   → Actualización de posición sin movimiento
   
⚠️  TRACKING vs TRACK MODE:
   • TELESCOPE_TRACK_STATE: ON/OFF del motor
   • ON_COORD_SET: Qué hacer cuando se envían coordenadas
   
💡 USOS TÍPICOS:
   • TRACK_ON:  Para astrofotografía, observación prolongada
   • TRACK_OFF: Para mover manualmente, mantenimiento, parking
   
⏱️  TÍPICAMENTE TARDA: < 1 segundo
"""
        }
        
        if operation in explanations:
            print(explanations[operation])
    
    async def connect(self) -> bool:
        """Conecta al servidor INDI"""
        try:
            self.explain_indi('connect')

            self.log(f"Conectando a servidor INDI en {self.host}:{self.port}...")
            self.reader, self.writer = await asyncio.open_connection(self.host, self.port)
            self.log("✓ Conexión TCP establecida")

            # Solicitar propiedades
            self.log("Solicitando propiedades del servidor...")
            await self._send_command('<getProperties version="1.7"/>')

            # Leer datos inmediatamente (sin sleep que bloquea)
            self.log("Recibiendo propiedades...")
            all_data = b""
            chunk_count = 0

            # Leer con timeout de 3 segundos o hasta que tengamos el dispositivo
            start_time = asyncio.get_event_loop().time()
            max_time = 3.0  # Reducido de 5 a 3 segundos
            device_found = False
            
            while (asyncio.get_event_loop().time() - start_time) < max_time:
                try:
                    data = await asyncio.wait_for(self.reader.read(32768), timeout=0.5)
                    if data:
                        chunk_count += 1
                        all_data += data
                        self.log_verbose(f"Chunk {chunk_count}: {len(data)} bytes (total: {len(all_data)})")
                        
                        # Salir en cuanto encontremos nuestro dispositivo
                        if self.device_name.encode() in all_data:
                            device_found = True
                            self.log_verbose(f"✓ Dispositivo '{self.device_name}' encontrado en chunk {chunk_count}")
                            break
                    else:
                        break
                except asyncio.TimeoutError:
                    # Si ya encontramos el dispositivo, salir
                    if device_found or all_data:
                        break
                    continue

            self.log(f"✓ Recibidos {len(all_data)} bytes en {chunk_count} chunks")

            # Guardar propiedades en cache
            self.cached_properties = all_data.decode('utf-8', errors='ignore')

            # Verificar que el dispositivo existe
            if self.device_name.encode() not in all_data:
                # Listar dispositivos disponibles
                devices = []
                import re
                for match in re.finditer(rb'device="([^"]+)"', all_data):
                    dev = match.group(1).decode('utf-8', errors='ignore')
                    if dev not in devices:
                        devices.append(dev)

                self.log(f"❌ Dispositivo '{self.device_name}' no encontrado", "ERROR")
                self.log("", "INFO")
                self.log("Dispositivos disponibles en el servidor:", "INFO")
                for dev in devices:
                    self.log(f"  • {dev}", "INFO")
                self.log("", "INFO")
                return False

            self.log(f"✓ Dispositivo '{self.device_name}' encontrado")

            # Verificar si ya está conectado
            if b'<oneSwitch name="CONNECT">On</oneSwitch>' in all_data or b'<defSwitch name="CONNECT" label="Connect">\nOn' in all_data:
                self.log(f"✓ Dispositivo ya está conectado")
            else:
                # Conectar al dispositivo
                connect_cmd = f'''<newSwitchVector device="{self.device_name}" name="CONNECTION">
  <oneSwitch name="CONNECT">On</oneSwitch>
  <oneSwitch name="DISCONNECT">Off</oneSwitch>
</newSwitchVector>'''

                self.log("Conectando al dispositivo...")
                await self._send_command(connect_cmd)
                # Esperar confirmación rápida (reducido de 2s a 1s)
                await asyncio.sleep(1)
                self.log("✓ Comando de conexión enviado")

            # CLAVE: Seguir leyendo hasta tener las coordenadas (optimizado)
            self.log("Capturando propiedades del dispositivo...")

            start_time = asyncio.get_event_loop().time()
            max_duration = 5.0  # Timeout máximo reducido de 8s a 5s
            has_coordinates = False

            while (asyncio.get_event_loop().time() - start_time) < max_duration:
                try:
                    data = await asyncio.wait_for(self.reader.read(32768), timeout=0.8)
                    if data:
                        additional_data = data.decode('utf-8', errors='ignore')
                        self.cached_properties += additional_data

                        # Salir EN CUANTO tengamos las coordenadas
                        if 'EQUATORIAL_EOD_COORD' in self.cached_properties and not has_coordinates:
                            has_coordinates = True
                            elapsed = asyncio.get_event_loop().time() - start_time
                            self.log_verbose(f"✓ EQUATORIAL_EOD_COORD encontrado en {elapsed:.1f}s")
                            # Leer un poco más para asegurar datos completos
                            await asyncio.sleep(0.5)
                            break
                except asyncio.TimeoutError:
                    # Si ya tenemos coordenadas, salir
                    if has_coordinates:
                        break
                    continue

            if 'EQUATORIAL_EOD_COORD' in self.cached_properties:
                self.log(f"✓ Propiedades completas capturadas: {len(self.cached_properties)} bytes (incluye coordenadas)")
            else:
                self.log(f"⚠️  Propiedades capturadas: {len(self.cached_properties)} bytes (sin coordenadas)", "WARNING")

                # GUARDAR EL CACHE PARA DEBUGGING
                with open('debug_cache.xml', 'w') as f:
                    f.write(self.cached_properties)
                self.log(f"💾 Cache guardado en debug_cache.xml para análisis", "DEBUG")

                # Listar TODAS las propiedades que SÍ tiene
                import re
                all_props = re.findall(r'<def\w+Vector[^>]*name="([^"]+)"', self.cached_properties)
                self.log(f"", "DEBUG")
                self.log(f"Total de propiedades en cache: {len(set(all_props))}", "DEBUG")
                self.log(f"Todas las propiedades capturadas:", "DEBUG")
                for prop in sorted(set(all_props)):
                    self.log(f"  • {prop}", "DEBUG")

            self.log("")
            return True

        except Exception as e:
            self.log(f"Error al conectar: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            return False
    
    async def _send_command(self, xml: str):
        """Envía comando XML al servidor"""
        if not self.writer:
            raise RuntimeError("No conectado")
        
        self.log_verbose(f"📤 Enviando XML:\n{xml}")
        self.writer.write((xml + '\n').encode())
        await self.writer.drain()
    
    async def get_coordinates(self, force_refresh: bool = False) -> Tuple[Optional[float], Optional[float]]:
        """
        Lee las coordenadas actuales del telescopio

        Args:
            force_refresh: Si True, solicita coordenadas frescas del servidor
        """
        try:
            import re  # Importar al inicio

            self.log("Leyendo coordenadas actuales del telescopio...")

            all_data = ""

            # Si no forzamos refresh Y tenemos cache con coordenadas, usarlo
            if not force_refresh and 'EQUATORIAL_EOD_COORD' in self.cached_properties:
                self.log("✓ Usando coordenadas del cache")
                all_data = self.cached_properties
            else:
                # Pedir ESPECÍFICAMENTE solo las coordenadas
                self.log("Solicitando coordenadas actualizadas al servidor...")
                get_coords = f'<getProperties device="{self.device_name}" name="EQUATORIAL_EOD_COORD" version="1.7"/>'
                await self._send_command(get_coords)

                # Esperar más tiempo para coordenadas frescas después de comandos
                await asyncio.sleep(2.5 if force_refresh else 1.5)

                # Leer respuesta
                attempts = 0
                while attempts < 5:
                    try:
                        data = await asyncio.wait_for(self.reader.read(16384), timeout=1.0)
                        if data:
                            chunk = data.decode('utf-8', errors='ignore')
                            all_data += chunk

                            # Si encontramos coordenadas, salir
                            if 'EQUATORIAL_EOD_COORD' in all_data and '<defNumber' in all_data:
                                break
                        attempts += 1
                    except asyncio.TimeoutError:
                        attempts += 1
                        continue

                if all_data:
                    # Actualizar solo la parte de coordenadas en el cache
                    if 'EQUATORIAL_EOD_COORD' in all_data:
                        # Reemplazar o agregar las coordenadas en el cache
                        import re
                        # Eliminar coordenadas viejas del cache si existen
                        self.cached_properties = re.sub(
                            r'<defNumberVector[^>]*name="EQUATORIAL_EOD_COORD"[^>]*>.*?</defNumberVector>',
                            '',
                            self.cached_properties,
                            flags=re.DOTALL
                        )
                        # Agregar las nuevas
                        self.cached_properties += all_data
                        self.log(f"✓ Coordenadas actualizadas en cache")
                    else:
                        self.log(f"✓ Respuesta recibida: {len(all_data)} bytes")

            if not all_data:
                self.log("❌ No se recibieron datos", "ERROR")
                return None, None

            self.log_verbose(f"Respuesta completa ({len(all_data)} bytes):")
            if self.verbose:
                # Mostrar la respuesta completa en modo verbose
                self.log_verbose(all_data)
            else:
                self.log_verbose(all_data[:500] + "...")

            # Parsear RA y DEC con el patrón CORRECTO (valores en líneas separadas con espacios)
            patterns = [
                # Patrón CORRECTO: Maneja espacios, tabs, y saltos de línea
                (r'<defNumber[^>]*name="RA"[^>]*>\s*([\d.+-]+)\s*</defNumber>',
                 r'<defNumber[^>]*name="DEC"[^>]*>\s*([\d.+-]+)\s*</defNumber>'),
                # Patrón alternativo para setNumberVector (cuando se actualizan valores)
                (r'<oneNumber[^>]*name="RA"[^>]*>\s*([\d.+-]+)\s*</oneNumber>',
                 r'<oneNumber[^>]*name="DEC"[^>]*>\s*([\d.+-]+)\s*</oneNumber>'),
            ]

            ra = None
            dec = None

            for i, (ra_pattern, dec_pattern) in enumerate(patterns):
                self.log_verbose(f"Intentando patrón {i+1}...")
                ra_match = re.search(ra_pattern, all_data)
                dec_match = re.search(dec_pattern, all_data)

                if ra_match and dec_match:
                    try:
                        ra = float(ra_match.group(1))
                        dec = float(dec_match.group(1))
                        self.log_verbose(f"✓ Patrón {i+1} funcionó: RA={ra}, DEC={dec}")
                        break
                    except (ValueError, IndexError) as e:
                        self.log_verbose(f"✗ Patrón {i+1} falló al convertir: {e}")
                        continue
                else:
                    self.log_verbose(f"✗ Patrón {i+1} no coincidió")

            if ra is not None and dec is not None:
                self.current_ra = ra
                self.current_dec = dec

                self.log("")
                self.log("=" * 80)
                self.log("📍 COORDENADAS ACTUALES DEL TELESCOPIO")
                self.log("=" * 80)
                self.log(f"RA  (Ascensión Recta):")
                self.log(f"  • {ra:.6f} horas")
                self.log(f"  • {self._format_ra(ra)}")
                self.log(f"  • {ra * 15:.4f}° (grados)")
                self.log("")
                self.log(f"DEC (Declinación):")
                self.log(f"  • {dec:.6f}°")
                self.log(f"  • {self._format_dec(dec)}")
                self.log("=" * 80)
                self.log("")

                return ra, dec
            else:
                self.log("⚠️  No se pudieron parsear las coordenadas", "WARNING")
                self.log("")
                self.log("🔍 DATOS RECIBIDOS DEL SERVIDOR:", "DEBUG")
                # Mostrar fragmentos relevantes
                lines = all_data.split('\n')
                for line in lines[:10]:  # Primeras 10 líneas
                    if line.strip():
                        self.log(f"   {line[:100]}", "DEBUG")
                self.log("")
                self.log("💡 POSIBLES SOLUCIONES:", "INFO")
                self.log("   1. Espera 10 segundos y vuelve a intentar", "INFO")
                self.log("   2. Verifica que el telescopio esté conectado en KStars", "INFO")
                self.log("   3. Ejecuta con --verbose para ver más detalles", "INFO")
                return None, None

        except Exception as e:
            self.log(f"Error al leer coordenadas: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            return None, None
    
    def _format_ra(self, ra_hours: float) -> str:
        """Formatea RA en hh:mm:ss"""
        hours = int(ra_hours)
        minutes = int((ra_hours - hours) * 60)
        seconds = ((ra_hours - hours) * 60 - minutes) * 60
        return f"{hours:02d}h{minutes:02d}m{seconds:05.2f}s"
    
    def _format_dec(self, dec_deg: float) -> str:
        """Formatea DEC en ±dd:mm:ss"""
        sign = '+' if dec_deg >= 0 else '-'
        dec_deg = abs(dec_deg)
        degrees = int(dec_deg)
        minutes = int((dec_deg - degrees) * 60)
        seconds = ((dec_deg - degrees) * 60 - minutes) * 60
        return f"{sign}{degrees:02d}°{minutes:02d}'{seconds:05.2f}\""
    
    async def goto(self, ra_hours: float, dec_degrees: float) -> bool:
        """Ejecuta comando GOTO"""
        try:
            self.explain_indi('goto')

            # Leer posición actual primero
            current_ra, current_dec = await self.get_coordinates()

            if current_ra is not None and current_dec is not None:
                # Calcular distancia angular
                delta_ra = abs(ra_hours - current_ra) * 15  # Convertir horas a grados
                delta_dec = abs(dec_degrees - current_dec)
                distance = (delta_ra**2 + delta_dec**2)**0.5

                self.log("")
                self.log("🎯 GOTO - MOVIMIENTO DEL TELESCOPIO")
                self.log(f"   Desde: RA={current_ra:.4f}h, DEC={current_dec:.4f}°")
                self.log(f"   Hacia: RA={ra_hours:.4f}h, DEC={dec_degrees:.4f}°")
                self.log(f"   Distancia angular: {distance:.2f}°")
                self.log(f"   Tiempo estimado: {max(30, int(distance * 2))} segundos")
                self.log("")

            # Asegurar modo SLEW
            slew_mode = f'''<newSwitchVector device="{self.device_name}" name="ON_COORD_SET">
  <oneSwitch name="TRACK">Off</oneSwitch>
  <oneSwitch name="SLEW">On</oneSwitch>
  <oneSwitch name="SYNC">Off</oneSwitch>
</newSwitchVector>'''

            self.log("Paso 1/2: Configurando modo SLEW (movimiento)...")
            await self._send_command(slew_mode)
            await asyncio.sleep(0.5)

            # Enviar coordenadas
            goto_cmd = f'''<newNumberVector device="{self.device_name}" name="EQUATORIAL_EOD_COORD">
  <oneNumber name="RA">{ra_hours}</oneNumber>
  <oneNumber name="DEC">{dec_degrees}</oneNumber>
</newNumberVector>'''

            self.log(f"Paso 2/2: Enviando coordenadas objetivo...")
            self.log(f"   RA  = {ra_hours} horas ({self._format_ra(ra_hours)})")
            self.log(f"   DEC = {dec_degrees}° ({self._format_dec(dec_degrees)})")
            self.log("   (Presiona Ctrl+C para cancelar)")
            await self._send_command(goto_cmd)

            self.log("")
            self.log("⏳ Polling estado del movimiento (timeout: 120s)...")
            
            # Polling de estado - esperar hasta que state="Ok"
            start_time = asyncio.get_event_loop().time()
            timeout = 120  # 120 segundos máximo
            state = "Busy"
            poll_count = 0
            
            while state == "Busy":
                poll_count += 1
                elapsed = asyncio.get_event_loop().time() - start_time
                
                if elapsed > timeout:
                    self.log("⚠️  TIMEOUT: El movimiento no completó en 120 segundos", "WARNING")
                    return False
                
                try:
                    # ACTIVE POLLING: Solicitar estado actual del telescopio
                    get_state = f'<getProperties device="{self.device_name}" name="EQUATORIAL_EOD_COORD" version="1.7"/>'
                    await self._send_command(get_state)
                    
                    # Leer respuesta con timeout
                    data = await asyncio.wait_for(self.reader.read(8192), timeout=1.0)
                    if data:
                        response = data.decode('utf-8', errors='ignore')
                        self.log_verbose(f"Poll {poll_count}: Recibidos {len(data)} bytes")
                        
                        # Buscar estado de EQUATORIAL_EOD_COORD
                        if 'EQUATORIAL_EOD_COORD' in response:
                            if 'state="Ok"' in response:
                                state = "Ok"
                                self.log(f"✓ GOTO completado (polling: {poll_count} iteraciones, {elapsed:.1f}s)")
                            elif 'state="Busy"' in response:
                                self.log_verbose(f"   Estado: Busy (movimiento en progreso)")
                            elif 'state="Alert"' in response:
                                self.log("❌ ERROR: El telescopio reportó Alert durante GOTO", "ERROR")
                                return False
                        
                except asyncio.TimeoutError:
                    # Timeout de lectura - solicitar estado de nuevo
                    self.log_verbose(f"   Poll {poll_count}: Timeout de lectura (reintentando...)")
                    continue
                
                # Pausa entre polls (más larga para no saturar)
                if state == "Busy":
                    await asyncio.sleep(0.5)
            
            # Leer posición final
            self.log("Leyendo posición final...")
            final_ra, final_dec = await self.get_coordinates(force_refresh=True)

            if final_ra is not None:
                ra_error = abs(final_ra - ra_hours) * 60  # minutos
                dec_error = abs(final_dec - dec_degrees) * 60  # arcominutos
                self.log("")
                self.log("📊 PRECISIÓN DEL GOTO:")
                self.log(f"   Error RA:  {ra_error:.2f} arcminutos")
                self.log(f"   Error DEC: {dec_error:.2f} arcminutos")
                if ra_error < 5 and dec_error < 5:
                    self.log("   ✓ Precisión EXCELENTE (< 5 arcmin)")
                elif ra_error < 15 and dec_error < 15:
                    self.log("   ✓ Precisión BUENA (< 15 arcmin) - considerar SYNC")
                else:
                    self.log("   ⚠️  Precisión BAJA - SYNC recomendado")

            return True

        except (KeyboardInterrupt, asyncio.CancelledError):
            self.log("", "WARNING")
            self.log("=" * 80, "WARNING")
            self.log("GOTO INTERRUMPIDO POR USUARIO (Ctrl+C)", "WARNING")
            self.log("=" * 80, "WARNING")
            self.log("El telescopio puede estar en movimiento.", "WARNING")
            self.log("Verifica su posición actual antes de continuar.", "WARNING")
            self.log("")
            raise KeyboardInterrupt  # Re-lanzar para manejo superior

        except Exception as e:
            self.log(f"Error en GOTO: {e}", "ERROR")
            return False
    
    async def sync(self, ra_real: float, dec_real: float) -> bool:
        """Ejecuta comando SYNC"""
        try:
            self.explain_indi('sync')

            # Leer posición que el telescopio CREE tener
            believed_ra, believed_dec = await self.get_coordinates()

            if believed_ra is None or believed_dec is None:
                self.log("No se pudieron leer coordenadas actuales", "ERROR")
                return False

            # Calcular offset
            offset_ra_arcmin = (ra_real - believed_ra) * 60  # minutos de arco
            offset_dec_arcmin = (dec_real - believed_dec) * 60  # arcominutos

            self.log("")
            self.log("🔧 SYNC - CORRECCIÓN DE POSICIÓN")
            self.log("=" * 80)
            self.log(f"Posición que el telescopio CREE tener:")
            self.log(f"   RA  = {believed_ra:.6f}h ({self._format_ra(believed_ra)})")
            self.log(f"   DEC = {believed_dec:.6f}° ({self._format_dec(believed_dec)})")
            self.log("")
            self.log(f"Posición REAL (que TÚ proporcionas):")
            self.log(f"   RA  = {ra_real:.6f}h ({self._format_ra(ra_real)})")
            self.log(f"   DEC = {dec_real:.6f}° ({self._format_dec(dec_real)})")
            self.log("")
            self.log("📏 OFFSET DETECTADO:")
            self.log(f"   ΔRA  = {offset_ra_arcmin:+.2f} arcminutos ({offset_ra_arcmin/60:+.4f} grados)")
            self.log(f"   ΔDEC = {offset_dec_arcmin:+.2f} arcminutos ({offset_dec_arcmin/60:+.4f} grados)")

            total_offset = (offset_ra_arcmin**2 + offset_dec_arcmin**2)**0.5
            self.log(f"   Total = {total_offset:.2f} arcminutos")
            self.log("")

            if total_offset < 1:
                self.log("   ℹ️  Offset muy pequeño (< 1 arcmin) - SYNC puede no ser necesario")
            elif total_offset < 5:
                self.log("   ✓ Offset moderado (< 5 arcmin) - SYNC mejorará precisión")
            else:
                self.log("   ⚠️  Offset grande (> 5 arcmin) - SYNC muy recomendado")

            self.log("")
            self.log("=" * 80)
            self.log("")

            # Paso 1: Cambiar a modo SYNC
            sync_mode = f'''<newSwitchVector device="{self.device_name}" name="ON_COORD_SET">
  <oneSwitch name="TRACK">Off</oneSwitch>
  <oneSwitch name="SLEW">Off</oneSwitch>
  <oneSwitch name="SYNC">On</oneSwitch>
</newSwitchVector>'''

            self.log("Paso 1/3: Cambiando a modo SYNC...")
            await self._send_command(sync_mode)
            await asyncio.sleep(0.5)

            # Paso 2: Enviar coordenadas reales
            sync_cmd = f'''<newNumberVector device="{self.device_name}" name="EQUATORIAL_EOD_COORD">
  <oneNumber name="RA">{ra_real}</oneNumber>
  <oneNumber name="DEC">{dec_real}</oneNumber>
</newNumberVector>'''

            self.log("Paso 2/3: Enviando coordenadas REALES al telescopio...")
            self.log("   🚨 El telescopio NO SE MOVERÁ, solo actualizará su modelo interno")
            self.log("   (Presiona Ctrl+C para cancelar)")
            await self._send_command(sync_cmd)
            await asyncio.sleep(1)

            # Paso 3: Restaurar modo TRACK
            track_mode = f'''<newSwitchVector device="{self.device_name}" name="ON_COORD_SET">
  <oneSwitch name="TRACK">On</oneSwitch>
  <oneSwitch name="SLEW">Off</oneSwitch>
  <oneSwitch name="SYNC">Off</oneSwitch>
</newSwitchVector>'''

            self.log("Paso 3/3: Restaurando modo TRACK...")
            await self._send_command(track_mode)
            await asyncio.sleep(0.5)

            self.log("")
            self.log("✓ SYNC completado exitosamente")
            self.log("")
            self.log("💡 RESULTADO:")
            self.log("   • El telescopio ahora conoce su posición real")
            self.log("   • Próximos GOTOs serán más precisos")
            self.log(f"   • Se corrigió un offset de {total_offset:.2f} arcminutos")
            self.log("")

            # IMPORTANTE: Invalidar cache y leer coordenadas ACTUALIZADAS
            self.log("Leyendo coordenadas actualizadas después del SYNC...")
            await asyncio.sleep(2)  # Dar tiempo extra al servidor
            new_ra, new_dec = await self.get_coordinates(force_refresh=True)

            if new_ra is not None:
                self.log("Coordenadas después del SYNC:")
                self.log(f"   RA  = {new_ra:.6f}h")
                self.log(f"   DEC = {new_dec:.6f}°")

                if abs(new_ra - ra_real) < 0.001 and abs(new_dec - dec_real) < 0.001:
                    self.log("   ✓ SYNC verificado: coordenadas coinciden")
                else:
                    self.log("   ⚠️  Pequeña diferencia detectada (normal en simulador)")

            return True

        except (KeyboardInterrupt, asyncio.CancelledError):
            self.log("", "WARNING")
            self.log("=" * 80, "WARNING")
            self.log("SYNC INTERRUMPIDO POR USUARIO (Ctrl+C)", "WARNING")
            self.log("=" * 80, "WARNING")
            self.log("El modo del telescopio puede quedar inconsistente.", "WARNING")
            self.log("Ejecuta --track_on para restaurar modo normal.", "WARNING")
            self.log("")
            raise KeyboardInterrupt

        except Exception as e:
            self.log(f"Error en SYNC: {e}", "ERROR")
            return False
    
    async def set_tracking(self, enable: bool) -> bool:
        """Activa o desactiva el tracking"""
        try:
            self.explain_indi('tracking')

            state = "ON" if enable else "OFF"
            action = "ACTIVANDO" if enable else "DESACTIVANDO"

            self.log(f"{action} tracking...")

            track_cmd = f'''<newSwitchVector device="{self.device_name}" name="TELESCOPE_TRACK_STATE">
  <oneSwitch name="TRACK_ON">{'On' if enable else 'Off'}</oneSwitch>
  <oneSwitch name="TRACK_OFF">{'Off' if enable else 'On'}</oneSwitch>
</newSwitchVector>'''

            await self._send_command(track_cmd)
            await asyncio.sleep(1)

            self.log(f"✓ Tracking {state}")

            if enable:
                self.log("")
                self.log("💡 Tracking ACTIVADO:")
                self.log("   • El telescopio compensará la rotación de la Tierra")
                self.log("   • Los objetos permanecerán centrados en el campo de visión")
                self.log("   • Ideal para astrofotografía y observación prolongada")
            else:
                self.log("")
                self.log("💡 Tracking DESACTIVADO:")
                self.log("   • El telescopio permanecerá estático")
                self.log("   • Los objetos se moverán en el campo de visión")
                self.log("   • Útil para movimiento manual o mantenimiento")

            return True

        except (KeyboardInterrupt, asyncio.CancelledError):
            self.log("", "WARNING")
            self.log("Operación de tracking interrumpida por usuario (Ctrl+C)", "WARNING")
            self.log("")
            raise KeyboardInterrupt

        except Exception as e:
            self.log(f"Error al cambiar tracking: {e}", "ERROR")
            return False
    
    async def disconnect(self):
        """Desconecta del servidor"""
        if self.writer:
            self.writer.close()
            await self.writer.wait_closed()
        self.log("Desconectado")


async def main():
    parser = argparse.ArgumentParser(
        description='Control de Telescopio INDI - Comandos individuales',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
EJEMPLOS DE USO:

1. Ver coordenadas actuales:
   python indi_telescope_control.py --status

2. GOTO a Vega (RA=18.615h, DEC=38.783°):
   python indi_telescope_control.py --goto 18.615 38.783

3. SYNC con coordenadas corregidas:
   python indi_telescope_control.py --sync 18.616 38.784

4. Activar tracking:
   python indi_telescope_control.py --track_on

5. Desactivar tracking:
   python indi_telescope_control.py --track_off

6. Modo verbose (ver XML):
   python indi_telescope_control.py --goto 18.615 38.783 --verbose

COORDENADAS POPULARES PARA PRUEBAS:
  Vega:      RA=18.615h  DEC=+38.783°
  Altair:    RA=19.846h  DEC=+8.868°
  Deneb:     RA=20.690h  DEC=+45.280°
  Polaris:   RA=2.530h   DEC=+89.264°
  Betelgeuse: RA=5.919h  DEC=+7.407°
"""
    )

    parser.add_argument('--host', type=str, default='localhost',
                       help='Servidor INDI (default: localhost)')
    parser.add_argument('--port', type=int, default=7624,
                       help='Puerto INDI (default: 7624)')
    parser.add_argument('--device', type=str, default='Telescope Simulator',
                       help='Nombre del dispositivo (default: Telescope Simulator)')
    parser.add_argument('--verbose', '-v', action='store_true',
                       help='Modo verbose (muestra XML)')

    # Comandos mutuamente exclusivos
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--status', action='store_true',
                      help='Mostrar coordenadas actuales')
    group.add_argument('--goto', nargs=2, type=float, metavar=('RA', 'DEC'),
                      help='GOTO a coordenadas (RA en horas, DEC en grados)')
    group.add_argument('--sync', nargs=2, type=float, metavar=('RA', 'DEC'),
                      help='SYNC con coordenadas reales')
    group.add_argument('--track_on', action='store_true',
                      help='Activar tracking')
    group.add_argument('--track_off', action='store_true',
                      help='Desactivar tracking')

    args = parser.parse_args()

    print("=" * 80)
    print("CONTROL DE TELESCOPIO INDI")
    print("=" * 80)
    print()

    # Crear controlador
    controller = INDITelescopeControl(
        host=args.host,
        port=args.port,
        device_name=args.device,
        verbose=args.verbose
    )

    success = False

    try:
        # Conectar
        if not await controller.connect():
            sys.exit(1)

        print()
        print("=" * 80)

        # Ejecutar comando
        if args.status:
            print("COMANDO: LEER COORDENADAS ACTUALES")
            print("=" * 80)
            print()
            await controller.get_coordinates()
            success = True

        elif args.goto:
            ra, dec = args.goto
            success = await controller.goto(ra, dec)

        elif args.sync:
            ra, dec = args.sync
            success = await controller.sync(ra, dec)

        elif args.track_on:
            success = await controller.set_tracking(True)

        elif args.track_off:
            success = await controller.set_tracking(False)

    except KeyboardInterrupt:
        print()
        print("=" * 80)
        print("⚠️  OPERACIÓN CANCELADA POR USUARIO (Ctrl+C)")
        print("=" * 80)
        print()

        # Desconectar limpiamente
        await controller.disconnect()

        sys.exit(130)  # Standard exit code for Ctrl+C

    except Exception as e:
        print()
        print("=" * 80)
        print(f"✗ ERROR INESPERADO: {e}")
        print("=" * 80)
        import traceback
        traceback.print_exc()
        success = False

    finally:
        # Asegurar desconexión
        if controller.writer:
            await controller.disconnect()

    print()
    print("=" * 80)
    if success:
        print("✓ OPERACIÓN COMPLETADA EXITOSAMENTE")
    else:
        print("✗ OPERACIÓN FALLÓ")
    print("=" * 80)

    sys.exit(0 if success else 1)


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        # Captura final por si acaso
        print()
        print("Programa terminado por usuario.")
        sys.exit(130)
