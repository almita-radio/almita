#!/usr/bin/env python3
"""
Control de Telescopio INDI - Script de pruebas individuales
Permite ejecutar comandos GOTO, SYNC, TRACKING por separado
Con información detallada del protocolo INDI
"""

import asyncio
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from typing import Optional, Dict, Tuple
import argparse
import sys
import re
import math
import time

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
        # Retained as an empty compatibility attribute.  Live XML is stored in
        # the bounded property cache/history below, never in one cumulative
        # string whose repeated concatenation becomes O(n²) over long runs.
        self.cached_properties: str = ""
        self.last_slew_busy_duration_sec: Optional[float] = None
        self.last_slew_command_to_ok_sec: Optional[float] = None
        self.final_target_error_deg: Optional[float] = None
        self._xml_receive_buffer = ""
        # Counts only spontaneous setTextVector publications.  A
        # getProperties reply is a defTextVector and must not advance this.
        self._onstep_status_update_seq = 0
        self._tracking_command_after_seq = None
        self._property_cache = {}
        self._property_history = {}
        self._property_condition = asyncio.Condition()
        self._reader_task = None
        self._reader_error = None
        self._write_lock = asyncio.Lock()
        self.compact_console = False
        
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
            await self._ensure_dispatcher()
            self.log("✓ Conexión TCP establecida")
            baseline = self._property_cache.get((self.device_name, "CONNECTION"), {}).get("update_seq", 0)
            await self._send_command('<getProperties version="1.7"/>')
            connection = await self._wait_property("CONNECTION", 3.0, baseline)
            if connection["elements"].get("CONNECT") != "On":
                connect_cmd = f'''<newSwitchVector device="{self.device_name}" name="CONNECTION">
  <oneSwitch name="CONNECT">On</oneSwitch>
  <oneSwitch name="DISCONNECT">Off</oneSwitch>
</newSwitchVector>'''
                baseline = connection["update_seq"]
                await self._send_command(connect_cmd)
                await self._wait_property("CONNECTION", 3.0, baseline,
                                          lambda x: x["elements"].get("CONNECT") == "On")
            if (self.device_name, "EQUATORIAL_EOD_COORD") not in self._property_cache:
                try:
                    await self._wait_property("EQUATORIAL_EOD_COORD", 5.0, 0)
                except asyncio.TimeoutError:
                    self.log("Propiedades capturadas sin coordenadas", "WARNING")
            return True

        except Exception as e:
            self.log(f"Error al conectar: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            return False
    
    async def _send_command(self, xml: str, timing_trace=None):
        """Envía comando XML al servidor"""
        if not self.writer:
            raise RuntimeError("No conectado")
        
        self.log_verbose(f"📤 Enviando XML:\n{xml}")
        lock_wait_started = time.perf_counter()
        if timing_trace:
            timing_trace("WRITE LOCK WAIT", details=[])
        async with self._write_lock:
            if timing_trace:
                timing_trace(
                    "WRITE LOCK ACQUIRED",
                    duration=time.perf_counter() - lock_wait_started,
                )
                write_started = time.perf_counter()
                timing_trace("WRITER WRITE START", details=[])
            self.writer.write((xml + '\n').encode())
            if timing_trace:
                timing_trace("WRITER WRITE END", duration=time.perf_counter() - write_started)
                drain_started = time.perf_counter()
                timing_trace("DRAIN START", details=[])
            await self.writer.drain()
            if timing_trace:
                timing_trace("DRAIN END", duration=time.perf_counter() - drain_started)

    async def _ensure_dispatcher(self):
        if self.reader is None:
            raise RuntimeError("No conectado")
        if self._reader_task is None or self._reader_task.done():
            self._reader_error = None
            self._reader_task = asyncio.create_task(self._reader_loop())

    async def _reader_loop(self):
        """The sole consumer of the INDI StreamReader."""
        try:
            while True:
                data = await self.reader.read(32768)
                if not data:
                    raise ConnectionError("INDI socket closed")
                for raw in self._feed_xml_messages(data.decode("utf-8", errors="ignore")):
                    try:
                        root = ET.fromstring(raw)
                    except ET.ParseError:
                        continue
                    name = root.attrib.get("name")
                    device = root.attrib.get("device") or self.device_name
                    if not name or not root.tag.endswith("Vector"):
                        continue
                    key = (device, name)
                    previous = self._property_cache.get(key)
                    seq = 1 if previous is None else previous["update_seq"] + 1
                    elements = {child.attrib.get("name"): (child.text or "").strip()
                                for child in root if child.attrib.get("name")}
                    item = {
                        "tag": root.tag, "state": root.attrib.get("state"),
                        "timestamp": root.attrib.get("timestamp"),
                        "received_at": datetime.now(timezone.utc).isoformat(),
                        "received_monotonic": asyncio.get_running_loop().time(),
                        "elements": elements, "raw": raw, "update_seq": seq,
                    }
                    self._property_cache[key] = item
                    self._property_history.setdefault(key, []).append(item)
                    del self._property_history[key][:-100]
                    async with self._property_condition:
                        self._property_condition.notify_all()
                # Let awakened waiters observe the complete TCP chunk before
                # an immediately-ready test/socket supplies the next one.
                await asyncio.sleep(0)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            self._reader_error = exc
            async with self._property_condition:
                self._property_condition.notify_all()

    async def _wait_property(self, name, timeout=1.0, after_seq=None, predicate=None,
                             wait_metrics=None):
        await self._ensure_dispatcher()
        key = (self.device_name, name)
        if after_seq is None:
            after_seq = self._property_cache.get(key, {}).get("update_seq", 0)
        deadline = asyncio.get_running_loop().time() + timeout
        entries_examined = 0
        async with self._property_condition:
            while True:
                for candidate in self._property_history.get(key, []):
                    entries_examined += 1
                    if (candidate["update_seq"] > after_seq
                            and (predicate is None or predicate(candidate))):
                        if wait_metrics is not None:
                            wait_metrics["history_entries_examined"] = entries_examined
                        return candidate
                item = self._property_cache.get(key)
                if item and item["update_seq"] > after_seq and (predicate is None or predicate(item)):
                    if wait_metrics is not None:
                        wait_metrics["history_entries_examined"] = entries_examined
                    return item
                if self._reader_error is not None:
                    if wait_metrics is not None:
                        wait_metrics["history_entries_examined"] = entries_examined
                    raise ConnectionError(str(self._reader_error))
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    if wait_metrics is not None:
                        wait_metrics["history_entries_examined"] = entries_examined
                    raise asyncio.TimeoutError
                try:
                    await asyncio.wait_for(self._property_condition.wait(), remaining)
                except asyncio.TimeoutError:
                    if wait_metrics is not None:
                        wait_metrics["history_entries_examined"] = entries_examined
                    raise

    @staticmethod
    def _angular_distance_deg(ra1_hours: float, dec1_deg: float,
                              ra2_hours: float, dec2_deg: float) -> float:
        """Separación angular esférica entre dos posiciones ecuatoriales."""
        ra1 = math.radians((ra1_hours % 24.0) * 15.0)
        ra2 = math.radians((ra2_hours % 24.0) * 15.0)
        dec1 = math.radians(dec1_deg)
        dec2 = math.radians(dec2_deg)
        delta_ra = ra2 - ra1
        y = math.hypot(
            math.cos(dec2) * math.sin(delta_ra),
            math.cos(dec1) * math.sin(dec2)
            - math.sin(dec1) * math.cos(dec2) * math.cos(delta_ra),
        )
        x = (
            math.sin(dec1) * math.sin(dec2)
            + math.cos(dec1) * math.cos(dec2) * math.cos(delta_ra)
        )
        return math.degrees(math.atan2(y, x))

    @staticmethod
    def _validated_goto_coordinates(ra_hours: float,
                                    dec_degrees: float) -> Optional[Tuple[float, float]]:
        """Valida un target GOTO y normaliza RA sin cambiar DEC."""
        try:
            ra = float(ra_hours)
            dec = float(dec_degrees)
        except (TypeError, ValueError):
            return None
        if not math.isfinite(ra) or not math.isfinite(dec):
            return None
        if not -90.0 <= dec <= 90.0:
            return None
        return ra % 24.0, dec

    @staticmethod
    def _split_complete_xml_messages(xml_text: str) -> Tuple[list, str]:
        """Separa vectores INDI completos y conserva el último fragmento parcial."""
        messages = []
        remaining = xml_text
        vector_start = re.compile(
            r'<(?P<tag>(?:def|set)(?:Number|Switch|Text|Light|BLOB)Vector)\b',
            re.IGNORECASE,
        )
        while True:
            start = vector_start.search(remaining)
            if not start:
                partial = remaining.rfind('<')
                return messages, remaining[partial:] if partial >= 0 else ""
            tag = start.group('tag')
            close = re.search(rf'</{re.escape(tag)}\s*>', remaining[start.start():], re.IGNORECASE)
            if not close:
                return messages, remaining[start.start():]
            end = start.start() + close.end()
            messages.append(remaining[start.start():end])
            remaining = remaining[end:]

    def _feed_xml_messages(self, xml_chunk: str) -> list:
        """Agrega un fragmento TCP y devuelve todos los vectores INDI completos."""
        self._xml_receive_buffer += xml_chunk
        messages, self._xml_receive_buffer = self._split_complete_xml_messages(
            self._xml_receive_buffer
        )
        return messages

    @staticmethod
    def _extract_eod_update(xml_messages) -> Tuple[Optional[str], Optional[float], Optional[float]]:
        """Extrae el último state/RA/DEC válido de EQUATORIAL_EOD_COORD."""
        state = None
        ra = None
        dec = None
        for message in xml_messages:
            try:
                root = ET.fromstring(message)
            except ET.ParseError:
                continue
            if root.attrib.get('name') != 'EQUATORIAL_EOD_COORD':
                continue
            state = root.attrib.get('state')
            values = {}
            for child in root:
                name = child.attrib.get('name')
                if name in ('RA', 'DEC') and child.text is not None:
                    try:
                        values[name] = float(child.text.strip())
                    except ValueError:
                        pass
            ra = values.get('RA')
            dec = values.get('DEC')
        return state, ra, dec

    def _final_eod_error_deg(self, target_ra_hours: float, target_dec_deg: float,
                             final_ra_hours: Optional[float],
                             final_dec_deg: Optional[float]) -> Optional[float]:
        """Compara el target EOD enviado con una lectura final EOD válida."""
        if final_ra_hours is None or final_dec_deg is None:
            return None
        values = (target_ra_hours, target_dec_deg, final_ra_hours, final_dec_deg)
        if not all(math.isfinite(value) for value in values):
            return None
        if not -90.0 <= final_dec_deg <= 90.0:
            return None
        return self._angular_distance_deg(
            target_ra_hours, target_dec_deg, final_ra_hours, final_dec_deg
        )
    
    async def get_coordinates(self, force_refresh: bool = False) -> Tuple[Optional[float], Optional[float]]:
        """
        Lee las coordenadas actuales del telescopio

        Args:
            force_refresh: Si True, solicita coordenadas frescas del servidor
        """
        get_coord_started = time.perf_counter()

        def coord_trace(label, duration=None, details=None):
            if self.compact_console:
                return
            now = time.perf_counter()
            utc = datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3]
            suffix = f" duration={duration:.6f} s" if duration is not None else ""
            print(f"[{utc}] [+{now - get_coord_started:.6f}] {label}{suffix}", flush=True)
            for detail in details or []:
                print(f"    {detail}", flush=True)

        coord_trace("GET_COORD ENTER", details=[f"force_refresh={force_refresh}"])
        try:
            self.log("Leyendo coordenadas actuales del telescopio...")
            key = (self.device_name, "EQUATORIAL_EOD_COORD")

            lookup_started = time.perf_counter()
            coord_trace("CACHE LOOKUP START")
            cached_item = self._property_cache.get(key)
            history = self._property_history.get(key, [])
            cache_seq = cached_item.get("update_seq", 0) if cached_item else 0
            coord_trace(
                "CACHE LOOKUP END",
                duration=time.perf_counter() - lookup_started,
                details=[f"cache_seq={cache_seq}", f"history_len={len(history)}"],
            )

            item = None
            if not force_refresh and cached_item is not None:
                self.log("✓ Usando coordenadas del cache")
                item = cached_item
            else:
                self.log("Solicitando coordenadas actualizadas al servidor...")
                get_coords = f'<getProperties device="{self.device_name}" name="EQUATORIAL_EOD_COORD" version="1.7"/>'
                baseline = cache_seq
                request_started = time.perf_counter()
                coord_trace("REQUEST EOD START", details=[f"baseline_seq={baseline}"])
                await self._send_command(get_coords)
                coord_trace("REQUEST EOD END", duration=time.perf_counter() - request_started)
                wait_started = time.perf_counter()
                wait_metrics = {}
                coord_trace(
                    "WAIT PROPERTY START",
                    details=[
                        f"baseline_seq={baseline}",
                        f"history_len={len(history)}",
                        "timeout=5.000 s",
                        f"history_start_index={len(history)}",
                    ],
                )
                try:
                    item = await self._wait_property(
                        "EQUATORIAL_EOD_COORD", 5.0, baseline,
                        lambda x: "RA" in x["elements"] and "DEC" in x["elements"],
                        wait_metrics=wait_metrics)
                except asyncio.TimeoutError:
                    coord_trace(
                        "WAIT PROPERTY END",
                        duration=time.perf_counter() - wait_started,
                        details=[
                            "result=timeout",
                            f"history_entries_examined={wait_metrics.get('history_entries_examined', 0)}",
                        ],
                    )
                    item = None
                else:
                    coord_trace(
                        "WAIT PROPERTY END",
                        duration=time.perf_counter() - wait_started,
                        details=[
                            f"returned_seq={item.get('update_seq')}",
                            f"state={item.get('state')}",
                            f"history_entries_examined={wait_metrics.get('history_entries_examined', 0)}",
                            f"history_len={len(self._property_history.get(key, []))}",
                        ],
                    )

            if item is None:
                self.log("❌ No se recibieron datos", "ERROR")
                coord_trace("GET_COORD RETURN", duration=time.perf_counter() - get_coord_started,
                            details=["result=None,None"])
                return None, None

            parse_started = time.perf_counter()
            coord_trace("PARSE RA/DEC START")
            elements = item.get("elements", {})
            try:
                ra = float(elements["RA"])
                dec = float(elements["DEC"])
            except (KeyError, TypeError, ValueError):
                ra = dec = None
            coord_trace("PARSE RA/DEC END", duration=time.perf_counter() - parse_started)

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

                coord_trace("GET_COORD RETURN", duration=time.perf_counter() - get_coord_started,
                            details=[f"RA={ra}", f"DEC={dec}"])
                return ra, dec
            else:
                self.log("⚠️  No se pudieron parsear las coordenadas", "WARNING")
                self.log("")
                self.log("🔍 DATOS RECIBIDOS DEL SERVIDOR:", "DEBUG")
                # Mostrar fragmentos relevantes
                lines = item.get("raw", "").split('\n')
                for line in lines[:10]:  # Primeras 10 líneas
                    if line.strip():
                        self.log(f"   {line[:100]}", "DEBUG")
                self.log("")
                self.log("💡 POSIBLES SOLUCIONES:", "INFO")
                self.log("   1. Espera 10 segundos y vuelve a intentar", "INFO")
                self.log("   2. Verifica que el telescopio esté conectado en KStars", "INFO")
                self.log("   3. Ejecuta con --verbose para ver más detalles", "INFO")
                coord_trace("GET_COORD RETURN", duration=time.perf_counter() - get_coord_started,
                            details=["result=None,None"])
                return None, None

        except Exception as e:
            self.log(f"Error al leer coordenadas: {e}", "ERROR")
            import traceback
            traceback.print_exc()
            coord_trace("GET_COORD RETURN", duration=time.perf_counter() - get_coord_started,
                        details=[f"error={type(e).__name__}: {e}"])
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
            goto_enter_clock = time.perf_counter()

            def precommand_trace(label, duration=None, details=None):
                if self.compact_console:
                    return
                now = time.perf_counter()
                utc = datetime.now(timezone.utc).strftime('%H:%M:%S.%f')[:-3]
                suffix = f" duration={duration:.3f} s" if duration is not None else ""
                print(f"[{utc}] [+{now - goto_enter_clock:.3f}] {label}{suffix}", flush=True)
                for detail in details or []:
                    print(f"    {detail}", flush=True)

            precommand_trace("GOTO ENTER")
            self.explain_indi('goto')
            self.final_target_error_deg = None
            operation_started = time.perf_counter()
            precommand_trace("VALIDATE TARGET START")
            validated = self._validated_goto_coordinates(ra_hours, dec_degrees)
            precommand_trace(
                "VALIDATE TARGET END",
                duration=time.perf_counter() - operation_started,
            )
            if validated is None:
                self.log(
                    f"Coordenadas GOTO inválidas: RA={ra_hours!r}, DEC={dec_degrees!r}. "
                    "RA y DEC deben ser finitas; DEC debe estar entre -90° y +90°.",
                    "ERROR",
                )
                return False
            ra_hours, dec_degrees = validated
            poll_interval_sec = 0.1
            precheck_timeout_sec = 30.0
            eod_seq = 0

            async def _wait_until_not_busy(timeout_sec: float) -> bool:
                """Block until EQUATORIAL_EOD_COORD is no longer Busy before sending a new slew."""
                nonlocal eod_seq
                deadline = asyncio.get_event_loop().time() + timeout_sec
                while True:
                    request_started = time.perf_counter()
                    precommand_trace(
                        "CURRENT EOD REQUEST START",
                        details=["property=EQUATORIAL_EOD_COORD", f"baseline update_seq={eod_seq}"],
                    )
                    await self._send_command(
                        f'<getProperties device="{self.device_name}" name="EQUATORIAL_EOD_COORD" version="1.7"/>',
                        timing_trace=precommand_trace,
                    )
                    precommand_trace(
                        "CURRENT EOD REQUEST END",
                        duration=time.perf_counter() - request_started,
                    )
                    wait_started = time.perf_counter()
                    precommand_trace(
                        "WAIT CURRENT EOD START",
                        details=["property=EQUATORIAL_EOD_COORD", f"baseline update_seq={eod_seq}",
                                 "timeout=0.200 s"],
                    )
                    try:
                        item = await self._wait_property("EQUATORIAL_EOD_COORD", 0.2, eod_seq)
                        eod_seq = item["update_seq"]
                        precommand_trace(
                            "WAIT CURRENT EOD END",
                            duration=time.perf_counter() - wait_started,
                            details=[f"seq received={eod_seq}", f"state={item['state']}"],
                        )
                    except asyncio.TimeoutError:
                        item = None
                        precommand_trace(
                            "WAIT CURRENT EOD END",
                            duration=time.perf_counter() - wait_started,
                            details=["result=timeout", f"baseline update_seq={eod_seq}"],
                        )
                    eod_state = item["state"] if item else None

                    if eod_state == "Alert":
                        self.log("❌ ERROR: Mount reporta Alert antes del nuevo GOTO", "ERROR")
                        return False
                    if eod_state == "Busy":
                        if asyncio.get_event_loop().time() >= deadline:
                            self.log("⚠️  TIMEOUT: Mount siguió en Busy antes de enviar nuevo GOTO", "WARNING")
                            return False
                        sleep_started = time.perf_counter()
                        precommand_trace("PRECHECK SLEEP START", details=["requested=0.100 s"])
                        await asyncio.sleep(poll_interval_sec)
                        precommand_trace("PRECHECK SLEEP END", duration=time.perf_counter() - sleep_started)
                        continue
                    if eod_state in ("Ok", "Idle"):
                        return True

                    if asyncio.get_event_loop().time() >= deadline:
                        self.log("⚠️  TIMEOUT: No se pudo confirmar estado no-Busy antes del GOTO", "WARNING")
                        return False
                    sleep_started = time.perf_counter()
                    precommand_trace("PRECHECK SLEEP START", details=["requested=0.100 s"])
                    await asyncio.sleep(poll_interval_sec)
                    precommand_trace("PRECHECK SLEEP END", duration=time.perf_counter() - sleep_started)

            # Leer posición actual primero
            operation_started = time.perf_counter()
            precommand_trace("READ CURRENT POSITION START", details=["operation=get_coordinates()"])
            current_ra, current_dec = await self.get_coordinates()
            precommand_trace(
                "READ CURRENT POSITION END",
                duration=time.perf_counter() - operation_started,
                details=[f"RA={current_ra}", f"DEC={current_dec}"],
            )

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

            operation_started = time.perf_counter()
            eod_seq = self._property_cache.get(
                (self.device_name, "EQUATORIAL_EOD_COORD"), {}
            ).get("update_seq", 0)
            precommand_trace(
                "PRE-COMMAND EOD PRECHECK START",
                details=["property=EQUATORIAL_EOD_COORD",
                         f"baseline update_seq={eod_seq}",
                         "timeout configured=30.000 s"],
            )
            if not await _wait_until_not_busy(precheck_timeout_sec):
                return False
            precommand_trace(
                "PRE-COMMAND EOD PRECHECK END",
                duration=time.perf_counter() - operation_started,
                details=[f"final seq={eod_seq}"],
            )

            # Asegurar modo SLEW
            slew_mode = f'''<newSwitchVector device="{self.device_name}" name="ON_COORD_SET">
  <oneSwitch name="TRACK">Off</oneSwitch>
  <oneSwitch name="SLEW">On</oneSwitch>
  <oneSwitch name="SYNC">Off</oneSwitch>
</newSwitchVector>'''

            self.log("Paso 1/2: Configurando modo SLEW (movimiento)...")
            operation_started = time.perf_counter()
            precommand_trace("SEND SLEW MODE START", details=["property=ON_COORD_SET"])
            await self._send_command(slew_mode, timing_trace=precommand_trace)
            precommand_trace("SEND SLEW MODE END", duration=time.perf_counter() - operation_started)
            sleep_started = time.perf_counter()
            precommand_trace("SLEW MODE SLEEP START", details=["requested=0.500 s"])
            await asyncio.sleep(0.5)
            precommand_trace("SLEW MODE SLEEP END", duration=time.perf_counter() - sleep_started)

            # Enviar coordenadas
            goto_cmd = f'''<newNumberVector device="{self.device_name}" name="EQUATORIAL_EOD_COORD">
  <oneNumber name="RA">{ra_hours}</oneNumber>
  <oneNumber name="DEC">{dec_degrees}</oneNumber>
</newNumberVector>'''

            self.log(f"Paso 2/2: Enviando coordenadas objetivo...")
            self.log(f"   RA  = {ra_hours} horas ({self._format_ra(ra_hours)})")
            self.log(f"   DEC = {dec_degrees}° ({self._format_dec(dec_degrees)})")
            self.log("   (Presiona Ctrl+C para cancelar)")
            operation_started = time.perf_counter()
            precommand_trace("GET EOD BASELINE START", details=["source=_property_cache"])
            baseline_eod_seq = self._property_cache.get(
                (self.device_name, "EQUATORIAL_EOD_COORD"), {}
            ).get("update_seq", 0)
            precommand_trace(
                "GET EOD BASELINE END",
                duration=time.perf_counter() - operation_started,
                details=[f"baseline update_seq={baseline_eod_seq}"],
            )
            eod_seq = baseline_eod_seq
            self.log(f"GOTO baseline_seq={baseline_eod_seq}")
            operation_started = time.perf_counter()
            precommand_trace("SEND GOTO COMMAND START", details=["property=EQUATORIAL_EOD_COORD"])
            await self._send_command(goto_cmd, timing_trace=precommand_trace)
            precommand_trace(
                "GOTO COMMAND SENT",
                duration=time.perf_counter() - operation_started,
                details=[f"goto_pre_command_total={time.perf_counter() - goto_enter_clock:.3f} s"],
            )
            self.log(f"GOTO command_sent_seq={baseline_eod_seq}")

            self.log("")
            self.log("⏳ Polling estado del movimiento (timeout: 120s)...")
            
            # Polling de estado y convergencia real al target
            start_time = asyncio.get_event_loop().time()
            timeout = 120  # 120 segundos máximo
            poll_count = 0
            busy_started_at: Optional[float] = None
            busy_finished_at: Optional[float] = None
            ok_detected_at: Optional[float] = None
            last_error_deg: Optional[float] = None
            stable_hits = 0
            last_stable_hit_seq: Optional[int] = None
            convergence_tol_deg = 0.25
            settle_tol_deg = 0.02
            settle_required_hits = 2
            corrective_retry_limit_deg = 1.0
            corrective_retry_sent = False
            self.last_slew_busy_duration_sec = None
            self.last_slew_command_to_ok_sec = None
            
            while True:
                poll_count += 1
                elapsed = asyncio.get_event_loop().time() - start_time
                
                if elapsed > timeout:
                    self.log("⚠️  TIMEOUT: El movimiento no completó en 120 segundos", "WARNING")
                    return False
                
                try:
                    # ACTIVE POLLING: Solicitar estado actual del telescopio
                    get_state = f'<getProperties device="{self.device_name}" name="EQUATORIAL_EOD_COORD" version="1.7"/>'
                    await self._send_command(get_state)
                    item = await self._wait_property("EQUATORIAL_EOD_COORD", 0.1, eod_seq)
                    eod_seq = item["update_seq"]
                    if item:
                        eod_state = item["state"]
                        item_seq = item["update_seq"]
                        try:
                            parsed_ra = float(item["elements"].get("RA"))
                            parsed_dec = float(item["elements"].get("DEC"))
                        except (TypeError, ValueError):
                            parsed_ra = parsed_dec = None

                        if parsed_ra is not None and parsed_dec is not None:
                            # Angular error uses spherical distance in hours/deg.
                            current_error_deg = self._angular_distance_deg(parsed_ra, parsed_dec, ra_hours, dec_degrees)

                            self.log_verbose(
                                f"   EOD seq={item_seq} state={eod_state} "
                                f"RA={parsed_ra:.8f} DEC={parsed_dec:.8f} "
                                f"error={current_error_deg:.6f}° "
                                f"post_command={item_seq > baseline_eod_seq}"
                            )

                            if item_seq <= baseline_eod_seq:
                                self.log_verbose(
                                    f"   EOD seq={item_seq} ignorado para convergencia "
                                    f"(baseline_seq={baseline_eod_seq})"
                                )
                            elif current_error_deg <= convergence_tol_deg:
                                if (
                                    last_error_deg is not None
                                    and last_stable_hit_seq is not None
                                    and item_seq > last_stable_hit_seq
                                    and abs(current_error_deg - last_error_deg) <= settle_tol_deg
                                ):
                                    stable_hits += 1
                                else:
                                    stable_hits = 1
                                last_stable_hit_seq = item_seq
                                self.log_verbose(
                                    f"   convergence=True stable_hit={stable_hits} seq={item_seq}"
                                )
                            else:
                                stable_hits = 0
                                last_stable_hit_seq = None
                            if item_seq > baseline_eod_seq:
                                last_error_deg = current_error_deg

                        if eod_state == "Busy":
                            if busy_started_at is None:
                                busy_started_at = asyncio.get_event_loop().time()
                            self.log_verbose(f"   Estado: Busy (movimiento en progreso)")
                            await asyncio.sleep(poll_interval_sec)
                            continue
                        elif eod_state in ("Ok", "Idle"):
                            if last_error_deg is not None and last_error_deg <= convergence_tol_deg and stable_hits >= settle_required_hits:
                                ok_detected_at = asyncio.get_event_loop().time()
                                if busy_started_at is not None:
                                    busy_finished_at = ok_detected_at
                                self.log(f"✓ GOTO completado (polling: {poll_count} iteraciones, {elapsed:.1f}s, error~{last_error_deg:.3f}°)")
                                break

                            if (
                                eod_state == "Idle"
                                and busy_started_at is not None
                                and not corrective_retry_sent
                                and last_error_deg is not None
                                and convergence_tol_deg < last_error_deg <= corrective_retry_limit_deg
                            ):
                                corrective_retry_sent = True
                                self.log(
                                    "↻ GOTO correctivo único: mount Idle fuera de tolerancia "
                                    f"(error~{last_error_deg:.3f}°); reenviando el mismo target"
                                )
                                baseline_eod_seq = self._property_cache.get(
                                    (self.device_name, "EQUATORIAL_EOD_COORD"), {}
                                ).get("update_seq", eod_seq)
                                eod_seq = baseline_eod_seq
                                self.log(f"GOTO correctivo baseline_seq={baseline_eod_seq}")
                                await self._send_command(goto_cmd)
                                busy_started_at = None
                                busy_finished_at = None
                                last_error_deg = None
                                stable_hits = 0
                                last_stable_hit_seq = None
                                await asyncio.sleep(poll_interval_sec)
                                continue

                            self.log_verbose(
                                f"   Estado: {eod_state} recibido, esperando convergencia real de coordenadas..."
                            )
                            await asyncio.sleep(poll_interval_sec)
                            continue
                        elif eod_state == "Alert":
                            self.log("❌ ERROR: El telescopio reportó Alert durante GOTO", "ERROR")
                            return False
                        else:
                            await asyncio.sleep(poll_interval_sec)
                            continue
                        
                except asyncio.TimeoutError:
                    # Timeout de lectura - solicitar estado de nuevo
                    self.log_verbose(f"   Poll {poll_count}: Timeout de lectura (reintentando...)")
                    continue

            if busy_started_at is not None and busy_finished_at is not None:
                self.last_slew_busy_duration_sec = busy_finished_at - busy_started_at
                self.log(f"⏱️  Tiempo de desplazamiento Busy->Ok: {self.last_slew_busy_duration_sec:.3f}s")
            else:
                self.log("⏱️  Tiempo de desplazamiento Busy->Ok: no disponible (Busy no detectado claramente)")

            if ok_detected_at is not None:
                self.last_slew_command_to_ok_sec = ok_detected_at - start_time
                self.log(f"⏱️  Tiempo comando->Ok: {self.last_slew_command_to_ok_sec:.3f}s")
            else:
                self.log("⏱️  Tiempo comando->Ok: no disponible")
            
            # Leer posición final
            self.log("Leyendo posición final...")
            final_ra, final_dec = await self.get_coordinates(force_refresh=True)
            self.final_target_error_deg = self._final_eod_error_deg(
                ra_hours, dec_degrees, final_ra, final_dec
            )

            if self.final_target_error_deg is not None:
                self.log("")
                self.log("📊 PRECISIÓN DEL GOTO:")
                final_error_arcmin = self.final_target_error_deg * 60.0
                self.log(f"   Error angular EOD: {final_error_arcmin:.2f} arcminutos")
                if final_error_arcmin < 5:
                    self.log("   ✓ Precisión EXCELENTE (< 5 arcmin)")
                elif final_error_arcmin < 15:
                    self.log("   ✓ Precisión BUENA (< 15 arcmin) - considerar SYNC")
                else:
                    self.log("   ⚠️  Precisión BAJA - SYNC recomendado")

            return True

        except asyncio.CancelledError:
            self.log("GOTO cancelado; propagando cancelación limpia", "WARNING")
            raise

        except KeyboardInterrupt:
            self.log("", "WARNING")
            self.log("=" * 80, "WARNING")
            self.log("GOTO INTERRUMPIDO POR USUARIO (Ctrl+C)", "WARNING")
            self.log("=" * 80, "WARNING")
            self.log("El telescopio puede estar en movimiento.", "WARNING")
            self.log("Verifica su posición actual antes de continuar.", "WARNING")
            self.log("")
            return False

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
    
    @staticmethod
    def _extract_tracking_state(xml_messages) -> str:
        """Normalize the latest TELESCOPE_TRACK_STATE vector."""
        result = "unknown"
        for message in xml_messages:
            try:
                root = ET.fromstring(message)
            except ET.ParseError:
                continue
            if root.attrib.get("name") != "TELESCOPE_TRACK_STATE":
                continue
            if root.attrib.get("state") == "Alert":
                result = "alert"
                continue
            values = {
                child.attrib.get("name"): (child.text or "").strip()
                for child in root
                if child.attrib.get("name") in ("TRACK_ON", "TRACK_OFF")
            }
            if values.get("TRACK_ON") == "On" and values.get("TRACK_OFF") == "Off":
                result = "on"
            elif values.get("TRACK_ON") == "Off" and values.get("TRACK_OFF") == "On":
                result = "off"
            else:
                result = "unknown"
        return result

    async def get_tracking_state(self, timeout: float = 1.0) -> str:
        """Read fresh tracking state as on/off/unknown/alert without changing it."""
        if timeout <= 0:
            return "unknown"
        query = (
            f'<getProperties device="{self.device_name}" '
            'name="TELESCOPE_TRACK_STATE" version="1.7"/>'
        )
        baseline = self._property_cache.get(
            (self.device_name, "TELESCOPE_TRACK_STATE"), {}
        ).get("update_seq", 0)
        await self._send_command(query)
        try:
            item = await self._wait_property("TELESCOPE_TRACK_STATE", timeout, baseline)
        except (asyncio.TimeoutError, ConnectionError):
            return "unknown"
        return self._extract_tracking_state([item["raw"]])

    @staticmethod
    def _unknown_onstep_status(reason: str) -> Dict:
        """Return an explicit non-healthy result when no INDI status was read."""
        return {
            "state": "unknown", "message": None, "is_error": False,
            "vector_state": None, "timestamp": None, "received_at": None,
            # ``fresh`` is retained for compatibility and means INDI XML only.
            "fresh": False, "indi_fresh": False, "hardware_fresh": False,
            "source": None, "update_seq": None,
            "reason": reason, "elements": {}, "raw": None,
        }

    def _extract_onstep_status(self, xml_messages) -> Optional[Dict]:
        """Extract the latest real OnStep Status text vector for this device."""
        latest = None
        for raw in xml_messages:
            try:
                root = ET.fromstring(raw)
            except ET.ParseError:
                continue
            if (root.tag not in ("defTextVector", "setTextVector")
                    or root.attrib.get("name") != "OnStep Status"
                    or root.attrib.get("device") not in (None, self.device_name)):
                continue
            hardware_fresh = root.tag == "setTextVector"
            if hardware_fresh:
                self._onstep_status_update_seq += 1
            elements = {
                child.attrib.get("name"): (child.text or "").strip()
                for child in root
                if child.tag in ("defText", "oneText") and child.attrib.get("name")
            }
            message = elements.get("Error") or None
            vector_state = root.attrib.get("state")
            vector_alert = str(vector_state).lower() == "alert"
            healthy = message in ("None", "Goto No Error")
            if vector_alert:
                state = "alert"
            elif message is None:
                state = "unknown"
            else:
                state = "healthy" if healthy else "error"
            latest = {
                "state": state,
                "message": message,
                "is_error": vector_alert or state == "error",
                "vector_state": vector_state,
                "timestamp": root.attrib.get("timestamp"),
                "received_at": datetime.now(timezone.utc).isoformat(),
                # Backwards compatible: this says only that a new XML vector
                # was received, not that the controller was just polled.
                "fresh": True,
                "indi_fresh": True,
                "hardware_fresh": hardware_fresh,
                "source": "indi_poll" if hardware_fresh else "indi_cached",
                "update_seq": self._onstep_status_update_seq,
                "reason": None,
                "elements": elements,
                "raw": raw,
            }
        return latest

    async def get_onstep_status(self, timeout: float = 1.0) -> Dict:
        """Read an INDI snapshot; this does not request a new hardware poll."""
        if timeout <= 0:
            return self._unknown_onstep_status("timeout")
        query = (
            f'<getProperties device="{self.device_name}" '
            'name="OnStep Status" version="1.7"/>'
        )
        baseline = self._property_cache.get(
            (self.device_name, "OnStep Status"), {}
        ).get("update_seq", 0)
        await self._send_command(query)
        try:
            item = await self._wait_property("OnStep Status", timeout, baseline)
        except asyncio.TimeoutError:
            return self._unknown_onstep_status("timeout")
        except ConnectionError:
            return self._unknown_onstep_status("absent")
        item = self._property_cache.get((self.device_name, "OnStep Status"), item)
        return self._extract_onstep_status([item["raw"]]) or self._unknown_onstep_status("absent")

    async def wait_onstep_status_update(self, timeout: float = 2.5) -> Dict:
        """Wait passively for the next status publication from driver polling.

        LX200 OnStep publishes ``setTextVector`` after its normal
        ``ReadScopeStatus()`` / ``:GU#`` cycle.  In contrast, a client
        ``getProperties`` request is answered with a cached ``defTextVector``.
        This method sends no request and accepts only the former.  The sequence
        is local to XML vectors received by this controller instance; identical
        consecutive payloads still count as distinct polling updates.
        """
        if timeout <= 0:
            return self._unknown_onstep_status("timeout")
        baseline = self._property_cache.get(
            (self.device_name, "OnStep Status"), {}
        ).get("update_seq", 0)
        try:
            item = await self._wait_property(
                "OnStep Status", timeout, baseline,
                lambda x: x["tag"] == "setTextVector")
        except asyncio.TimeoutError:
            return self._unknown_onstep_status("timeout")
        except ConnectionError:
            return self._unknown_onstep_status("absent")
        return self._extract_onstep_status([item["raw"]]) or self._unknown_onstep_status("absent")

    async def wait_tracking_state(self, expected_on: bool, timeout: float = 5.0) -> bool:
        """Wait read-only for real tracking state; fail on timeout, unknown, or Alert."""
        if timeout <= 0:
            return False
        expected = "on" if expected_on else "off"
        command_baseline = self._tracking_command_after_seq
        self._tracking_command_after_seq = None
        if command_baseline is not None:
            query = (
                f'<getProperties device="{self.device_name}" '
                'name="TELESCOPE_TRACK_STATE" version="1.7"/>'
            )
            await self._send_command(query)
            deadline = asyncio.get_running_loop().time() + timeout
            baseline = command_baseline
            while True:
                remaining = deadline - asyncio.get_running_loop().time()
                if remaining <= 0:
                    return False
                try:
                    item = await self._wait_property(
                        "TELESCOPE_TRACK_STATE", remaining, baseline
                    )
                except (asyncio.TimeoutError, ConnectionError):
                    return False
                state = self._extract_tracking_state([item["raw"]])
                if state == expected:
                    return True
                if state == "alert":
                    return False
                baseline = item["update_seq"]
        deadline = asyncio.get_event_loop().time() + timeout
        while True:
            remaining = deadline - asyncio.get_event_loop().time()
            if remaining <= 0:
                return False
            state = await self.get_tracking_state(timeout=min(0.5, remaining))
            if state == expected:
                return True
            if state == "alert":
                return False
            await asyncio.sleep(min(0.1, max(0.0, deadline - asyncio.get_event_loop().time())))

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

            self._tracking_command_after_seq = self._property_cache.get(
                (self.device_name, "TELESCOPE_TRACK_STATE"), {}
            ).get("update_seq", 0)
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
        if self._reader_task is not None:
            self._reader_task.cancel()
            try:
                await self._reader_task
            except asyncio.CancelledError:
                pass
            self._reader_task = None
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
