"""
Bot de Telegram (Telethon) - Subida a S3 + reenvío al usuario.

Flujo:
1. Usuario envía URL por Telegram
2. Bot descarga el archivo (secuencial o paralelo)
3. Sube el archivo a S3 (toDus S3 o cualquier S3 configurable)
4. Reenvía el archivo al usuario por Telegram con su tipo correcto
5. SIEMPRE muestra la URL pública S3 al usuario

Mejoras aplicadas:
- Cliente Telethon (userbot o bot API)
- SSRF protection (bloqueo de IPs privadas/loopback/link-local)
- Allowlist de usuarios (ALLOWED_TG_IDS)
- URL S3 SIEMPRE visible al usuario
- Llamadas síncronas envueltas con asyncio.to_thread
- Subida a toDus con PUT directo (sin multipart de boto3) y ESPEJOS en
  varios namespaces: el bucket barre sin aviso namespaces enteros —
  medido con scripts/probe_todus.py; por eso los enlaces vencían casi
  al instante. Se entrega enlace principal + espejo en otro namespace
- Verificación post-subida (HEAD + tamaño) y espera anti-barrido
  antes de entregar el enlace, con caída automática al siguiente
  namespace (y memoria del que sobrevivió) si el objeto desaparece
- Descarga paralela robusta: rechaza respuestas 200 OK en peticiones Range
- Limpieza de archivos .part en cancelación/fallo
- Detección de Content-Type cuando la URL no tiene extensión clara
- Future gestionado con add_done_callback
- SIGTERM handler limpio para graceful shutdown en Render
- PORT configurable vía env var (Render lo inyecta)
- Watchdog opcional
- Todas las constantes externalizadas a env vars
"""
import os
import re
import time
import uuid
import signal
import asyncio
import json
import io
import hashlib
import logging
import mimetypes
import threading
import socket
import ipaddress
from urllib.parse import urlparse, unquote, quote

import aiofiles
import aiohttp
from aiohttp import web
from botocore import UNSIGNED
from botocore.config import Config as BotoConfig
from boto3.s3.transfer import TransferConfig

# Telethon: cliente de Telegram
from telethon import TelegramClient, events
from telethon.sessions import StringSession
from telethon.errors import FloodWaitError
from telethon.tl.types import (
    DocumentAttributeFilename,
    DocumentAttributeVideo,
    DocumentAttributeAudio,
)
from telethon.tl.functions.messages import SendMessageRequest


# ============== CONFIG ==============
# --- Telegram ---
# Para USERBOT (cuenta personal): API_ID + API_HASH + SESSION_STRING
# Para BOT oficial: API_ID + API_HASH + BOT_TOKEN
# Telethon soporta ambos modos; aquí implementamos los dos.
TG_API_ID = int(os.environ.get("TG_API_ID", "0") or "0")
TG_API_HASH = os.environ.get("TG_API_HASH", "")
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "")  # modo bot oficial
TG_SESSION_STRING = os.environ.get("TG_SESSION_STRING", "")  # modo userbot

# CSV de IDs de Telegram autorizados. Vacío = permitir a todos (NO recomendado)
_allowed_env = os.environ.get("ALLOWED_TG_IDS", "").strip()
ALLOWED_TG_IDS = (
    set(int(u) for u in _allowed_env.replace(" ", "").split(",") if u)
    if _allowed_env else None  # None = sin restricción
)

# --- S3 ---
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "https://s3.todus.cu")
S3_BUCKET = os.environ.get("S3_BUCKET", "stream")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "public")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "public")
S3_UNSIGNED = os.environ.get("S3_UNSIGNED", "1") == "1"
# Namespace de las keys dentro del bucket. IMPORTANTE (medido el
# 2026-10-26/27 con scripts/probe_todus.py desde una IP extranjera):
# el bucket público de toDus BARRE los objetos anónimos según el
# namespace de la key, y las reglas cambian sin aviso:
#   - raíz, users/tg_*, video/ y cualquier namespace inventado:
#     muertos en <2 min (ambos días).
#   - dk/: refugio el día 26 (objetos de días); el 27 amaneció vacío
#     y todo lo nuevo murió en <2 min.
#   - users/df/: refugio el día 26; el 27 sobrevivía 10 min pero a
#     los 18 min ya estaba borrado.
#   - hi/: vivo tras 28 min el día 27, con objetos de ~16 h en el
#     bucket: hoy es el refugio más sólido.
# El transporte, el User-Agent y el nombre de archivo son
# irrelevantes: solo manda la key. Por eso los enlaces "vencían"
# aunque la subida y la verificación inmediata fueran correctas, y
# por eso ya no se usa UN namespace fijo sino la lista de candidatos
# de más abajo (S3_KEY_PREFIXES), con espejos y memoria del ganador.
# S3_KEY_PREFIX (singular) queda solo como compatibilidad: los
# valores heredados "users"/"users/df" se interpretan como "usar la
# lista de candidatos por defecto"; cualquier otro valor explícito
# se respeta como único namespace.
# Formato resultante: "<prefijo>/<user_id>/<archivo>"; también se
# acepta el placeholder {user_id} dentro del prefijo (ej.
# "dk/tg_{user_id}"). "none" = keys planas en la raíz (solo para tu
# propio bucket; en toDus morirían en minutos).
_key_prefix_env = os.environ.get("S3_KEY_PREFIX", "users/df").strip().strip("/")
if _key_prefix_env == "users":
    # Valor del render.yaml de la versión anterior: "users" a secas
    # daría users/<id>/... (muerta en <2 min); se normaliza al valor
    # heredado "users/df" (= lista de candidatos por defecto).
    _key_prefix_env = "users/df"
S3_KEY_PREFIX = "" if _key_prefix_env.lower() in ("none", "-") else (_key_prefix_env or "users/df")
# Reintentos de subida (PUT + verificación) ante fallos transitorios.
S3_UPLOAD_RETRIES = max(1, int(os.environ.get("S3_UPLOAD_RETRIES", "3") or "3"))
# Tiempo total máximo (segundos) para subir y verificar un archivo.
S3_UPLOAD_TIMEOUT = int(os.environ.get("S3_UPLOAD_TIMEOUT", "1800") or "1800")
# Verificar con HEAD que el objeto existe (y su tamaño) ANTES de entregar
# el enlace al usuario. Sin esto, una subida fantasma se entrega como ✓
# y el usuario descubre el 404 al tocar el enlace.
S3_VERIFY_UPLOAD = os.environ.get("S3_VERIFY_UPLOAD", "1") == "1"
S3_VERIFY_ATTEMPTS = max(1, int(os.environ.get("S3_VERIFY_ATTEMPTS", "6") or "6"))
# Namespaces CANDIDATOS (en orden). Las reglas del barrido de toDus
# cambian sin aviso (dk/ era refugio el 2026-10-26 y amaneció vacío el
# 27), así que el bot no se casa con uno solo: sube a S3_MIRRORS de
# estos namespaces y entrega TODOS los enlaces resultantes (principal
# + espejo). Si mañana tumba un namespace entero, el espejo en otro
# sigue vivo. El orden se auto-ajusta: el último que
# sobrevivió pasa a ser el primero (memoria en
# DOWNLOAD_PATH/s3_namespace_state.json).
_DEFAULT_KEY_PREFIXES = ["hi", "users/df", "dk/tg_{user_id}"]
_prefixes_env = os.environ.get("S3_KEY_PREFIXES", "").strip()
if _prefixes_env:
    S3_KEY_PREFIXES = []
    for _pfx in _prefixes_env.split(","):
        _pfx = _pfx.strip().strip("/")
        if _pfx == "users":
            _pfx = "users/df"
        if _pfx and _pfx.lower() not in ("none", "-") and _pfx not in S3_KEY_PREFIXES:
            S3_KEY_PREFIXES.append(_pfx)
    if not S3_KEY_PREFIXES:
        S3_KEY_PREFIXES = list(_DEFAULT_KEY_PREFIXES)
elif S3_KEY_PREFIX == "":
    S3_KEY_PREFIXES = [""]  # "none" explícito: raíz (solo bucket propio)
elif S3_KEY_PREFIX != "users/df":
    # Elección explícita distinta del default anterior: se respeta tal cual.
    S3_KEY_PREFIXES = [S3_KEY_PREFIX]
else:
    # "users/df" (o sin configurar) = despliegue heredado: ya NO es
    # df-primero, sino la lista completa de candidatos por defecto.
    S3_KEY_PREFIXES = list(_DEFAULT_KEY_PREFIXES)
# A cuántos namespaces subir cada archivo (1 = sin espejo). Solo aplica
# en modo unsigned; con tu propio S3 firmado siempre es 1.
S3_MIRRORS = max(1, int(os.environ.get("S3_MIRRORS", "2") or "2"))
# Espera anti-barrido: tras subir y verificar, se espera este tiempo y
# se vuelve a comprobar que el objeto sigue vivo ANTES de entregar el
# enlace (el barrido rápido medido actúa en <2 min). Ojo: también hay
# barridos lentos (users/df/ murió a los 10-18 min el 2026-10-27) que
# ninguna espera razonable caza: contra esos protege el ESPEJO en otro
# namespace, no el settle. 0 = desactivada.
S3_SETTLE_SECONDS = int(os.environ.get("S3_SETTLE_SECONDS", "150") or "0")

# --- Servidor ---
PORT = int(os.environ.get("PORT", "10000"))

# --- Descarga ---
DOWNLOAD_PATH = os.environ.get("DOWNLOAD_PATH", "/tmp/tg_uploads")
MAX_FILE_SIZE = int(os.environ.get("MAX_FILE_SIZE_MB", "2000")) * 1024 * 1024
# Telegram tiene límite de 2GB para bots y 4GB para premium
TG_UPLOAD_LIMIT = int(os.environ.get("TG_UPLOAD_LIMIT_MB", "2000")) * 1024 * 1024
CHUNK_SIZE = int(os.environ.get("CHUNK_SIZE_MB", "16")) * 1024 * 1024

# --- Watchdog ---
WATCHDOG_LIFETIME = int(os.environ.get("WATCHDOG_LIFETIME", "14100"))
WATCHDOG_ENABLED = os.environ.get("WATCHDOG_ENABLED", "1") == "1"

# --- Descarga paralela ---
PARALLEL_URL_DOWNLOAD = os.environ.get("PARALLEL_URL_DOWNLOAD", "1") == "1"
PARALLEL_URL_PARTS = int(os.environ.get("PARALLEL_URL_PARTS", "4"))
PARALLEL_URL_MIN_SIZE = int(os.environ.get("PARALLEL_URL_MIN_SIZE_MB", "50")) * 1024 * 1024

# --- Seguridad ---
SSRF_BLOCK_PRIVATE = os.environ.get("SSRF_BLOCK_PRIVATE", "1") == "1"

# --- Comportamiento ---
ALWAYS_SHOW_S3_URL = os.environ.get("ALWAYS_SHOW_S3_URL", "1") == "1"
# Por defecto NO se reenvía el archivo (solo URL S3 como texto).
# Esto ahorra datos en toDus (los mensajes de texto son ilimitados).
# Cambiar a "1" para que por defecto SÍ se reenvíe el archivo.
# Los usuarios pueden activar/desactivar con /sendfile.
SEND_FILE_VIA_TG = os.environ.get("SEND_FILE_VIA_TG", "0") == "1"
# Manifiesto .udyat para descargar "sin gastar megas" vía la IP gratuita
# de toDus (sistema UDYAT / app IroS3Dl; solo desde la red de ETECSA en
# Cuba). Es un JSON de ~300 B, así que el interruptor global va en "1"
# (a diferencia de SEND_FILE_VIA_TG, aquí no hay riesgo de gastar datos):
# solo lo recibe quien lo activa con /udyat, y solo si el S3 en uso es
# s3.todus.cu. "0" desactiva la función por completo.
SEND_UDYAT_VIA_TG = os.environ.get("SEND_UDYAT_VIA_TG", "1") == "1"
# IP que ETECSA no factura para s3.todus.cu (puede cambiar; respaldo
# conocido: 152.207.206.88).
UDYAT_FREE_IP = os.environ.get("UDYAT_FREE_IP", "152.207.206.86").strip() or "152.207.206.86"

# --- ToDus oficial (ToDus-SDK) ---
# La vía anónima depende de que el barrido respete algún namespace, y
# toDus rota las reglas sin aviso (hi/ también acabó cayendo). La vía
# con garantías es el flujo OFICIAL de la app: reservar la subida por
# XMPP (todus:purl) con una cuenta de toDus, para que el servidor asigne
# la key y el objeto quede registrado (ver nyxthor-dev/toDus-SDK).
# "auto" (default): usa la vía oficial si hay cuenta configurada
# (login desde el bot con /toduslogin, o TODUS_PHONE/TODUS_PASSWORD)
# y cae a la anónima adaptativa si la oficial falla.
# "official": como auto (el fallback se mantiene por resiliencia).
# "anonymous": nunca usa el SDK (comportamiento anterior).
TODUS_UPLOAD_MODE = os.environ.get("TODUS_UPLOAD_MODE", "auto").strip().lower() or "auto"
# Credenciales de arranque (opcionales). El login interactivo del bot
# (/toduslogin por SMS) guarda las suyas en DOWNLOAD_PATH; si estas env
# están definidas, tienen prioridad sobre el número guardado.
TODUS_PHONE = os.environ.get("TODUS_PHONE", "").strip()
TODUS_PASSWORD = os.environ.get("TODUS_PASSWORD", "").strip()
# Pedir la reserva como persistente (persistent='true' en todus:purl).
# Si el servidor rechaza ese modo, se reintenta automáticamente con
# persistent='false' (el modo chat de la app).
TODUS_PERSISTENT = os.environ.get("TODUS_PERSISTENT", "1") == "1"
# Tras una subida oficial OK, subir además una copia anónima como
# espejo (doble subida; por defecto no).
TODUS_ANON_MIRROR = os.environ.get("TODUS_ANON_MIRROR", "0") == "1"
# Quién puede gestionar la cuenta de toDus desde Telegram
# (/toduslogin, /toduslogout...). En modo userbot el dueño siempre
# puede; en modo bot hacen falta IDs aquí (CSV). Sin esto, el login
# interactivo queda deshabilitado (usar env TODUS_PHONE/PASSWORD).
_admin_env = os.environ.get("ADMIN_TG_IDS", "").strip()
ADMIN_TG_IDS = (
    set(int(u) for u in _admin_env.replace(" ", "").split(",") if u)
    if _admin_env else None
)

# --- HTTP Headers ---
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,*/*;q=0.8"
    ),
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "gzip, deflate, br",
}

os.makedirs(DOWNLOAD_PATH, exist_ok=True)

logging.basicConfig(
    level=os.environ.get("LOG_LEVEL", "INFO"),
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(os.environ.get("TELETHON_LOG_LEVEL", "WARNING"))
log = logging.getLogger("bot")

# --- S3 Config ---
# Solo se usa boto3 en modo firmado (S3_UNSIGNED=0, un S3 de verdad tipo
# AWS/MinIO/R2). En modo unsigned (toDus, el default) la subida es un PUT
# HTTP directo con aiohttp — ver _s3_put_direct: es el transporte que
# usan los clientes estables contra ese backend (MinIO) y el que quedó
# verificado en vivo con scripts/probe_todus.py.
_S3_CONFIG = BotoConfig(
    signature_version=UNSIGNED if S3_UNSIGNED else "s3v4",
    retries={"max_attempts": 3, "mode": "adaptive"},
    max_pool_connections=50,
    connect_timeout=30,
    read_timeout=600,
    s3={"addressing_style": "path"},
)
_TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=8 * 1024 * 1024,
    multipart_chunksize=64 * 1024 * 1024,
    max_concurrency=16,
    use_threads=False,  # más seguro en contexto async
)
_S3_USER_AGENT = "TGS3BOT/1.0 (+https://s3.todus.cu)"

_loop = None
_shutdown_event = asyncio.Event()
_tg_client = None
_tg_owner_id = None  # ID del dueño (en modo userbot) para filtrar mensajes
_is_userbot_mode = False  # True si usa TG_SESSION_STRING (sin BOT_TOKEN)


# ============== HELPERS ==============
def _run_sync(func, *args, **kwargs):
    """Envuelve una función síncrona en un hilo."""
    return asyncio.to_thread(func, *args, **kwargs)


# ============== SSRF PROTECTION ==============
def _is_safe_url(url):
    """SSRF protection: bloquea IPs privadas, loopback, link-local."""
    if not SSRF_BLOCK_PRIVATE:
        return True, None
    try:
        p = urlparse(url)
    except Exception as e:
        return False, f"URL inválida: {e}"
    if p.scheme not in ("http", "https"):
        return False, f"Esquema no permitido: {p.scheme}"
    if not p.hostname:
        return False, "Sin hostname"
    try:
        ip = socket.gethostbyname(p.hostname)
    except socket.gaierror as e:
        return False, f"No se pudo resolver el host: {e}"
    try:
        ip_obj = ipaddress.ip_address(ip)
    except ValueError as e:
        return False, f"IP inválida: {ip} ({e})"
    if (
        ip_obj.is_private
        or ip_obj.is_loopback
        or ip_obj.is_link_local
        or ip_obj.is_reserved
        or ip_obj.is_multicast
        or ip_obj.is_unspecified
    ):
        return False, f"IP bloqueada (privada/loopback/link-local): {ip}"
    return True, None


# ============== JOB QUEUE ==============
class QueuedJob:
    __slots__ = (
        "job_id", "user_id", "kind", "url", "original_name",
        "task", "created_at", "cancel_requested", "temp_paths",
        "status_msg", "chat_id", "message",
    )

    def __init__(self, user_id, kind, url=None, original_name=None,
                 chat_id=None, status_msg=None, message=None):
        self.job_id = uuid.uuid4().hex
        self.user_id = user_id
        self.kind = kind
        self.url = url
        self.original_name = original_name
        self.task = None
        self.created_at = time.time()
        self.cancel_requested = False
        self.temp_paths = []
        self.status_msg = status_msg  # mensaje de estado a editar
        self.chat_id = chat_id
        self.message = message  # mensaje original de Telegram (para media)


class SimpleLock:
    def __init__(self):
        self.active = {}
        self._lock = asyncio.Lock()

    def has_user_job(self, user_id):
        return user_id in self.active

    async def run(self, job, coro_factory):
        async with self._lock:
            if job.user_id in self.active:
                raise ValueError("user_already_running")
            self.active[job.user_id] = job
        try:
            coro = coro_factory()
            job.task = asyncio.create_task(coro)
            await job.task
        finally:
            async with self._lock:
                self.active.pop(job.user_id, None)

    async def cancel_user(self, user_id):
        async with self._lock:
            job = self.active.get(user_id)
            if job is None:
                return "none"
            job.cancel_requested = True
            if job.task and not job.task.done():
                job.task.cancel()
            return "active"


job_queue = SimpleLock()


# ============== UTILS ==============
def format_size(b):
    if b < 1024:
        return f"{b} B"
    if b < 1048576:
        return f"{b / 1024:.1f} KB"
    if b < 1073741824:
        return f"{b / 1048576:.1f} MB"
    return f"{b / 1073741824:.2f} GB"


URL_RE = re.compile(
    r"(https?://[^\s<>\"']+)",
    re.IGNORECASE,
)

IMAGE_EXT = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp"}
VIDEO_EXT = {".mp4", ".mkv", ".mov", ".avi", ".webm", ".m4v", ".3gp"}
AUDIO_EXT = {".mp3", ".wav", ".m4a", ".aac", ".flac"}
VOICE_EXT = {".ogg", ".opus"}

CONTENT_TYPE_TO_EXT = {
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/gif": ".gif",
    "image/webp": ".webp",
    "image/bmp": ".bmp",
    "video/mp4": ".mp4",
    "video/x-matroska": ".mkv",
    "video/quicktime": ".mov",
    "video/x-msvideo": ".avi",
    "video/webm": ".webm",
    "video/3gpp": ".3gp",
    "audio/mpeg": ".mp3",
    "audio/wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
}


def get_filename_from_url(url):
    try:
        name = os.path.basename(urlparse(url).path)
        if name and len(name) > 2:
            return unquote(name)
    except Exception:
        pass
    return None


def ext_from_content_type(ct):
    if not ct:
        return None
    base = ct.split(";")[0].strip().lower()
    return CONTENT_TYPE_TO_EXT.get(base)


def sanitize_filename(name):
    name = name.replace("/", "_").replace("\\", "_")
    name = re.sub(r"[\s?#&]+", "_", name)
    return name.strip("._") or f"file_{int(time.time())}"


def check_disk_space(required_extra=0):
    """Verifica espacio libre. required_extra = bytes adicionales necesarios."""
    st = os.statvfs(DOWNLOAD_PATH)
    free = st.f_bavail * st.f_frsize
    min_needed = 2 * MAX_FILE_SIZE + required_extra
    if free < min_needed:
        raise RuntimeError(
            f"Disco insuficiente: {format_size(free)} libres, "
            f"se necesitan {format_size(min_needed)}"
        )
    return free


def cleanup_paths(paths):
    """Elimina archivos temporales silenciosamente."""
    for p in paths:
        try:
            os.unlink(p)
        except Exception:
            pass


# ============== TELEGRAM SEND/EDIT HELPERS ==============
# ============== UI: tarjetas profesionales ==============
# Diseño minimalista: solo info útil al usuario, sin caracteres de borde ruidosos.

# Iconos limpios (Telegram los renderiza bien como emoji)
ICON_PROCESSING = "⏳"
ICON_DONE = "✓"
ICON_ERROR = "✗"
ICON_CANCEL = "⊘"
ICON_LINK = "🔗"
ICON_FILE = "📄"
ICON_DOWNLOAD = "↓"
ICON_CLOUD = "☁"


def _render_result_card(name, ext, size, s3_url, kind="archivo",
                         source=None, send_file=False, sent_as=None,
                         mirror_url=None):
    """Genera la tarjeta final del resultado.

    Formato limpio y profesional:
        ✓ Subido a S3
        Nombre archivo.mp4
        MP4 · 15.2 MB · nube
        https://s3.todus.cu/stream/a1b2c3d4_archivo.mp4
    """
    lines = [f"{ICON_DONE} Subido a S3"]

    if source:
        lines.append(f"📎 Origen: {source}")
    lines.append(f"{ICON_FILE} {name}.{ext}" if ext and not name.endswith(f".{ext}") else f"{ICON_FILE} {name}")

    meta_parts = []
    if ext:
        meta_parts.append(ext.upper())
    if size:
        meta_parts.append(format_size(size))
    meta_parts.append("nube")
    if meta_parts:
        lines.append(" · ".join(meta_parts))

    if sent_as:
        labels = {"imagen": "foto", "video": "video", "audio": "audio",
                  "nota de voz": "nota de voz", "archivo": "documento"}
        lines.append(f"Enviado como {labels.get(sent_as, sent_as)}")

    lines.append("")  # separador
    lines.append(f"{ICON_LINK} {s3_url}")
    if mirror_url:
        # Segundo enlace al mismo archivo en otro namespace: si toDus
        # tumba un namespace entero, el espejo puede seguir vivo.
        lines.append(f"{ICON_LINK} {mirror_url} (espejo)")
    return "\n".join(lines)


def _render_status_progress(step, filename=None, size=None):
    """Estado de progreso minimalista.

    step: 'downloading' | 'uploading' | 'receiving' | 'verifying'
    """
    icons = {"downloading": f"{ICON_PROCESSING} Descargando",
             "uploading": f"{ICON_PROCESSING} Subiendo",
             "receiving": f"{ICON_PROCESSING} Recibiendo",
             "verifying": f"{ICON_PROCESSING} Verificando enlace"}
    base = icons.get(step, f"{ICON_PROCESSING} Procesando")
    parts = [base]
    if filename:
        parts.append(filename)
    if size:
        parts.append(f"· {format_size(size)}")
    return " ".join(parts)


def _render_error(msg):
    """Mensaje de error limpio."""
    return f"{ICON_ERROR} {msg}"


def _render_cancel():
    return f"{ICON_CANCEL} Cancelado"


def _render_help(is_userbot=False, is_admin=False):
    """Tarjeta de ayuda profesional."""
    lines = [
        f"🤖 **Bot S3** — sube archivos a la nube y te devuelve el enlace",
        "",
        "¿Qué puedo hacer?",
        f"  • Envía un enlace `http(s)://...` y lo subo",
        f"  • Reenvía un archivo (foto, video, audio, doc) y lo subo",
        "",
        "Por defecto solo te envío la **URL** como texto (ahorra datos en toDus).",
        "Si quieres recibir el archivo además de la URL, usa /sendfile.",
        "",
        "Descarga los enlaces pronto: el bucket público de toDus puede purgar archivos sin aviso.",
        "Para descargar sin gastar megas (solo Cuba): activa /udyat y te envío el manifiesto para la app IroS3Dl. Con 1DM+: cambia el host del enlace por 152.207.206.86 y añade las cabeceras `Host: s3.todus.cu` y `User-Agent: Wget/1.21.3`.",
        "",
        "Comandos",
        "  /cancel — cancela tu trabajo actual",
        "  /status — muestra si tienes trabajo en curso",
        "  /sendfile — toggle: recibir archivo + URL (gasta datos)",
        "  /udyat — toggle: manifiesto .udyat (descargar sin megas en Cuba)",
    ]
    if is_userbot:
        lines.extend([
            "",
            "👤 **Modo userbot**: solo respondo a mensajes del dueño.",
            "Pega enlaces o reenvía archivos en *Saved Messages* para usar el bot.",
        ])
    if is_admin:
        lines.extend([
            "",
            "🔐 **Admin — cuenta de toDus (vía oficial)**",
            "  /toduslogin NÚMERO — login por SMS (o NÚMERO CONTRASEÑA)",
            "  /toduscode 123456 — completar el login por SMS",
            "  /todusstatus — estado de la sesión de toDus",
            "  /toduslogout — cerrar sesión (vuelve a la vía anónima)",
        ])
    return "\n".join(lines)


def _render_sendfile_state(enabled):
    if enabled:
        return f"{ICON_DONE} Modo archivo activado\n\nA partir de ahora recibirás el archivo además de la URL."
    else:
        return f"{ICON_DONE} Modo archivo desactivado\n\nSolo recibirás la URL S3 (ahorra datos en toDus)."


def _mask_phone(phone):
    """5350123456 -> 53501…56 (no mostrar el número completo en chats)."""
    p = str(phone or "")
    return f"{p[:5]}…{p[-2:]}" if len(p) >= 8 else p


def _render_todus_status():
    creds = _todus_creds_load()
    lines = ["📲 **Cuenta de toDus (vía oficial)**", ""]
    phone = creds.get("phone") or ""
    if not phone or not creds.get("password"):
        lines += [
            "Estado: sin cuenta configurada → las subidas van por la vía anónima.",
            "",
            "Para activarla: /toduslogin NÚMERO (registro por SMS), "
            "/toduslogin NÚMERO CONTRASEÑA si ya tienes la contraseña, o "
            "define TODUS_PHONE/TODUS_PASSWORD en el entorno.",
        ]
    else:
        lines.append(f"Número: {_mask_phone(phone)}")
        if creds.get("jwt"):
            exp = creds.get("jwt_exp") or 0
            if exp:
                lines.append(
                    "Sesión: activa (token hasta "
                    + time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime(exp)) + ")")
            else:
                lines.append("Sesión: activa")
        else:
            lines.append(
                "Sesión: credenciales guardadas; el login se hace al subir.")
        via = ("oficial (reserva del servidor)" if _todus_official_enabled()
               else "anónima (TODUS_UPLOAD_MODE=anonymous)")
        lines.append(f"Vía de subida: {via}")
    lines += ["", "/toduslogin · /toduscode · /toduslogout"]
    return "\n".join(lines)


def _render_udyat_state(enabled):
    if enabled:
        return (
            f"{ICON_DONE} Manifiesto UDYAT activado\n\n"
            "Tras cada subida te enviaré también el archivo .udyat para "
            "descargar SIN gastar megas: impórtalo en la app IroS3Dl "
            "(mantén presionado el botón ➕ en Descargas). "
            "Solo funciona desde la red de ETECSA en Cuba; prueba con un "
            "archivo pequeño y revisa tu saldo la primera vez."
        )
    else:
        return f"{ICON_DONE} Manifiesto UDYAT desactivado\n\nSolo recibirás la URL S3."


def _render_unauthorized():
    return f"{ICON_ERROR} No estás autorizado para usar este bot."


def _render_busy():
    return f"{ICON_PROCESSING} Ya tienes un trabajo en curso. Usa /cancel para detenerlo."


def _render_no_active_job():
    return "No tienes trabajos activos."


def _render_active_job():
    return f"{ICON_PROCESSING} Tienes un trabajo en curso."


def _render_no_url():
    return f"Envía un enlace (http/https) o reenvía un archivo para procesar."


def _tipo_label(tipo):
    """Convierte el tipo interno a una etiqueta amigable."""
    labels = {
        "imagen": "foto",
        "video": "video",
        "audio": "audio",
        "nota de voz": "nota de voz",
        "archivo": "documento",
    }
    return labels.get(tipo, tipo)


async def tg_send_status(chat_id, text):
    """Envía un mensaje de estado inicial y lo retorna para editar después."""
    global _tg_client
    try:
        msg = await _tg_client.send_message(chat_id, text)
        return msg
    except Exception as e:
        log.warning(f"tg_send_status falló: {e}")
        return None


async def tg_edit_status(chat_id, msg, text):
    """Edita un mensaje existente. Si falla, envía uno nuevo."""
    global _tg_client
    if msg is None:
        return await tg_send_status(chat_id, text)
    try:
        await _tg_client.edit_message(chat_id, msg, text)
        return msg
    except Exception as e:
        log.warning(f"tg_edit_status falló: {e}, enviando nuevo")
        return await tg_send_status(chat_id, text)


# ============== S3 UPLOAD ==============
# Por qué NO boto3/multipart contra toDus (causa de "los enlaces vencen
# casi al instante"):
#   1. La versión anterior subía con boto3 upload_fileobj, que para
#      archivos >8 MB usa multipart de S3 (CreateMultipartUpload /
#      UploadPart / CompleteMultipartUpload). El backend de toDus no es
#      AWS: los clientes que funcionan de forma estable contra
#      s3.todus.cu (todus-client y los demás bots públicos) suben con un
#      PUT HTTP directo, en una sola petición, sin firmar. (Este
#      transporte, con keys en el namespace correcto, es el que quedó
#      verificado en vivo contra el bucket real.)
#   2. El NAMESPACE de la key (la causa principal del "vence casi al
#      instante", medida con scripts/probe_todus.py el 2026-10-26/27):
#      un barrido del bucket borra los objetos según el namespace de
#      la key, aunque el PUT devuelva 200 y la verificación HEAD
#      inmediata pase. La raíz, users/tg_*, video/ y namespaces
#      inventados mueren en <2 min. Y los "refugios" ROTAN sin aviso:
#      dk/ era refugio el día 26 y cementerio (<2 min) el 27;
#      users/df/ pasó de refugio a morir a los 10-18 min; hi/ aguantó
#      ≥28 min y guarda objetos de ~16 h. Conclusión: ningún
#      namespace fijo sirve para siempre. Por eso subir_a_s3() sube a
#      S3_MIRRORS namespaces candidatos (hoy: hi/ primero, luego
#      users/df/), entrega principal + espejo, y recuerda cuál
#      sobrevivió para probarlo primero la próxima vez.
#   3. La URL se entregaba al usuario sin comprobar que el objeto
#      existiera. Ahora se verifica con HEAD (y el tamaño) tras una
#      espera anti-barrido; si el objeto desapareció se cae al
#      siguiente namespace. Nunca se entrega como ✓ un enlace muerto.
#
# Ojo: el bucket es público y anónimo también para borrar, y toDus
# puede cambiar mañana qué namespaces respeta su barrido. Si los
# enlaces volvieran a caer, re-mide con scripts/probe_todus.py y
# ajusta S3_KEY_PREFIXES. La vía con garantías es subir por el flujo
# oficial de toDus (reserva XMPP todus:purl con una cuenta de toDus,
# ver ToDus-SDK) o usar tu propio S3 con S3_UNSIGNED=0.

class _S3RetryableError(RuntimeError):
    """Fallo transitorio de subida (red / HTTP 5xx): vale la pena reintentar."""


class _S3UploadExhausted(RuntimeError):
    """Se agotaron los reintentos en UN namespace (probar el siguiente)."""


# Extensión -> Content-Type. Se parte del mapa inverso ya definido arriba
# y se añaden tipos que mimetypes no siempre conoce.
mimetypes.add_type("application/vnd.android.package-archive", ".apk")
mimetypes.add_type("video/x-matroska", ".mkv")
mimetypes.add_type("audio/opus", ".opus")
_EXT_TO_CONTENT_TYPE = {ext: ct for ct, ext in CONTENT_TYPE_TO_EXT.items()}
_EXT_TO_CONTENT_TYPE.update({
    ".pdf": "application/pdf",
    ".zip": "application/zip",
    ".rar": "application/vnd.rar",
    ".7z": "application/x-7z-compressed",
    ".tar": "application/x-tar",
    ".gz": "application/gzip",
    ".txt": "text/plain",
    ".epub": "application/epub+zip",
    ".apk": "application/vnd.android.package-archive",
    ".opus": "audio/opus",
})


def _guess_content_type(filename):
    """Content-Type real según el nombre (antes todo era octet-stream)."""
    ext = os.path.splitext(filename or "")[1].lower()
    return (
        _EXT_TO_CONTENT_TYPE.get(ext)
        or mimetypes.guess_type(filename or "")[0]
        or "application/octet-stream"
    )


def _content_disposition(filename):
    """Content-Disposition con el nombre original (RFC 5987 si no es ASCII)."""
    name = os.path.basename(filename or "archivo.bin") or "archivo.bin"
    try:
        name.encode("ascii")
        safe = name.replace("\\", "_").replace('"', "_")
        return f'attachment; filename="{safe}"'
    except UnicodeEncodeError:
        fallback = name.encode("ascii", "replace").decode().replace('"', "_")
        return (
            f'attachment; filename="{fallback}"; '
            f"filename*=UTF-8''{quote(name, safe='')}"
        )


def build_s3_key(user_id, filename, prefix=None):
    """Key dentro de un namespace candidato:

        <prefijo>/<user_id>/<stem>_<rand8><ext>
        (hoy el primer candidato es hi: hi/<user_id>/...)

    Es la forma exacta de los objetos que sobreviven horas en el
    bucket (verificado en vivo con scripts/probe_todus.py). El sufijo
    _<rand8> evita que dos subidas con el mismo nombre se sobrescriban
    (los nombres con sufijos numéricos son habituales en el bucket y
    también sobrevivieron al probe). El nombre de descarga real va en
    Content-Disposition, así que el usuario conserva su nombre original.
    El prefijo admite el placeholder {user_id} (ej. dk/tg_{user_id}).
    """
    safe_name = re.sub(
        r"[^A-Za-z0-9._-]+", "_", sanitize_filename(filename)
    ).strip("._") or f"file_{int(time.time())}.bin"
    stem, ext = os.path.splitext(safe_name)
    unique_name = (
        f"{stem}_{uuid.uuid4().hex[:8]}{ext}" if stem else safe_name
    )
    pfx = S3_KEY_PREFIX if prefix is None else prefix
    if not pfx:
        # Sin namespace: raíz del bucket (solo buckets propios; en
        # toDus el barrido lo eliminaría en minutos).
        return unique_name
    uid = str(user_id) if user_id else "0"
    if "{user_id}" in pfx:
        base = pfx.replace("{user_id}", uid).strip("/")
    else:
        base = f"{pfx}/{uid}"
    return f"{base}/{unique_name}"


def s3_public_url(key):
    """URL pública de una key (codificando cada segmento, no las '/')."""
    return (
        f"{S3_ENDPOINT.rstrip('/')}/{S3_BUCKET.strip('/')}/"
        f"{quote(key, safe='/')}"
    )


# --- Memoria del orden de namespaces ---
# El barrido de toDus cambia qué namespaces respeta (dk/ cayó de un
# día para otro). El bot recuerda cuál sobrevivió la última vez y lo
# prueba primero; el que muere pasa al final. Se persiste en disco
# para sobrevivir reinicios (si el disco es efímero, se re-aprende).
_NS_STATE_FILE = os.path.join(DOWNLOAD_PATH, "s3_namespace_state.json")
_ns_order = None


def _ns_candidates():
    """Namespaces candidatos, con el último ganador primero."""
    global _ns_order
    if _ns_order is None:
        order = []
        try:
            with open(_NS_STATE_FILE) as fh:
                saved = json.load(fh).get("order", [])
            order = [x for x in saved if x in S3_KEY_PREFIXES]
        except Exception:
            pass
        order += [x for x in S3_KEY_PREFIXES if x not in order]
        _ns_order = order or list(S3_KEY_PREFIXES) or [""]
    return list(_ns_order)


def _ns_reorder(winner=None, loser=None):
    """Sube el namespace ganador al frente / hunde el perdedor (sync)."""
    global _ns_order
    order = _ns_candidates()
    changed = False
    if loser in order and order[-1] != loser:
        order.remove(loser)
        order.append(loser)
        changed = True
    if winner in order and order[0] != winner:
        order.remove(winner)
        order.insert(0, winner)
        changed = True
    _ns_order = order
    if changed:
        try:
            with open(_NS_STATE_FILE, "w") as fh:
                json.dump({"order": order}, fh)
        except Exception:
            pass


async def _s3_put_direct(temp_path, key, filename, size):
    """Subida unsigned (toDus): UN solo PUT HTTP con el archivo en streaming.

    Es exactamente el método de todus-client y de los bots que funcionan
    contra s3.todus.cu. Sin multipart, sin firma, con Content-Type real y
    Content-Disposition para que la descarga conserve el nombre original.
    """
    url = s3_public_url(key)
    headers = {
        "Content-Type": _guess_content_type(filename),
        "Content-Disposition": _content_disposition(filename),
        "Content-Length": str(size),
        "User-Agent": _S3_USER_AGENT,
    }
    # total=None: el límite global lo pone subir_a_s3 (S3_UPLOAD_TIMEOUT).
    # sock_read generoso para que una subida lenta no muera entre chunks.
    timeout = aiohttp.ClientTimeout(
        total=None, connect=30, sock_connect=30, sock_read=600
    )
    f = open(temp_path, "rb")
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            # Pasar el file object hace que aiohttp fije Content-Length y
            # lea en streaming (sin Transfer-Encoding: chunked, que los
            # S3-compatibles no aceptan en un PutObject sin firmar).
            async with session.put(url, data=f, headers=headers) as resp:
                if resp.status in (200, 201, 204):
                    # Un PutObject correcto devuelve cuerpo vacío. Algunos
                    # backends responden 200 con un <Error> XML dentro:
                    # eso NO es una subida válida aunque el status lo sea.
                    body = await resp.text()
                    if "<Error>" in body:
                        raise _S3RetryableError(
                            f"S3 devolvió un error con HTTP {resp.status}: "
                            f"{body[:200].strip()}"
                        )
                    if not resp.headers.get("ETag"):
                        log.warning(
                            "S3: PUT %s sin ETag en la respuesta (%s); "
                            "se fía de la verificación posterior",
                            resp.status, key,
                        )
                    return
                detail = (await resp.text())[:200].strip()
                if resp.status >= 500:
                    raise _S3RetryableError(
                        f"S3 respondió HTTP {resp.status} al subir: {detail}"
                    )
                raise RuntimeError(
                    f"S3 rechazó la subida (HTTP {resp.status}): {detail}"
                )
    except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
        raise _S3RetryableError(f"error de red subiendo a S3: {e}") from e
    finally:
        try:
            f.close()
        except Exception:
            pass


async def _s3_put_signed(temp_path, key, filename, size):
    """Subida firmada (S3_UNSIGNED=0): boto3 en un hilo, como antes.

    En un S3 de verdad (AWS/MinIO/R2) el multipart de boto3 es correcto;
    solo se corrigen Content-Type/Content-Disposition y la clasificación
    de errores reintentables (5xx/red) frente a definitivos (4xx).
    """
    def _do_upload():
        import boto3
        client = boto3.client(
            "s3",
            endpoint_url=S3_ENDPOINT,
            aws_access_key_id=S3_ACCESS_KEY,
            aws_secret_access_key=S3_SECRET_KEY,
            region_name=S3_REGION,
            config=_S3_CONFIG,
        )
        with open(temp_path, "rb") as f:
            client.upload_fileobj(
                f, S3_BUCKET, key,
                ExtraArgs={
                    "ContentType": _guess_content_type(filename),
                    "ContentDisposition": _content_disposition(filename),
                },
                Config=_TRANSFER_CONFIG,
            )

    try:
        await asyncio.to_thread(_do_upload)
    except Exception as e:
        status = None
        response = getattr(e, "response", None)
        if isinstance(response, dict):
            status = (
                response.get("ResponseMetadata", {}) or {}
            ).get("HTTPStatusCode")
        if status and 400 <= int(status) < 500:
            raise RuntimeError(
                f"S3 rechazó la subida (HTTP {status}): {str(e)[:200]}"
            ) from e
        raise _S3RetryableError(
            f"error subiendo a S3: {str(e)[:200]}"
        ) from e


def _remote_size(headers):
    """Tamaño total del objeto según Content-Range o Content-Length."""
    crange = headers.get("Content-Range", "")
    if "/" in crange:
        try:
            return int(crange.rsplit("/", 1)[1])
        except (ValueError, IndexError):
            pass
    clen = headers.get("Content-Length")
    if clen is not None:
        try:
            return int(clen)
        except ValueError:
            pass
    return None


async def _verify_s3_object(url, expected_size):
    """Comprueba que el objeto recién subido existe y tiene su tamaño.

    Devuelve (estado, detalle):
      "ok"           -> existe (y el tamaño coincide si el servidor lo dice)
      "dead"         -> el servidor confirma que NO existe o que el tamaño
                        difiere (la subida no quedó; hay que re-subir)
      "unverifiable" -> no se pudo comprobar (403 en bucket privado, HEAD
                        no soportado, red caída…). No bloquea el enlace:
                        un chequeo opcional nunca debe tumbar una subida.
    Solo lee cabeceras: jamás descarga el cuerpo del archivo.
    """
    headers = {
        "User-Agent": _S3_USER_AGENT,
        "Cache-Control": "no-cache, no-store",
        "Pragma": "no-cache",
    }
    timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
    delay = 1.0
    last_detail = "sin respuesta del servidor"
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(1, S3_VERIFY_ATTEMPTS + 1):
                try:
                    async with session.head(
                        url, headers=headers, allow_redirects=True
                    ) as resp:
                        status = resp.status
                        resp_headers = resp.headers
                        if status in (405, 501):
                            # HEAD no soportado: GET de 1 byte, sin leer cuerpo
                            async with session.get(
                                url,
                                headers={**headers, "Range": "bytes=0-0"},
                                allow_redirects=True,
                            ) as get_resp:
                                status = get_resp.status
                                resp_headers = get_resp.headers
                    if status in (200, 206):
                        remote = _remote_size(resp_headers)
                        # remote == 0 con tamaño esperado > 0 se trata como
                        # "tamaño no informado" (hay frontends que devuelven
                        # Content-Length: 0 en HEAD): no debe tumbar la subida.
                        if (
                            remote
                            and expected_size
                            and remote != expected_size
                        ):
                            return "dead", (
                                f"tamaño en S3 {format_size(remote)} != "
                                f"esperado {format_size(expected_size)}"
                            )
                        return "ok", f"HTTP {status}"
                    if status in (401, 403):
                        if S3_UNSIGNED:
                            # Bucket público (toDus): la lectura anónima
                            # funciona, así que un 403 al verificar es un
                            # problema real del objeto, no de permisos.
                            last_detail = (
                                f"HTTP {status} (el objeto no existe o "
                                f"no es accesible)"
                            )
                        else:
                            # Bucket privado firmado: un 403 anónimo es
                            # esperable aunque el objeto exista.
                            return "unverifiable", (
                                f"HTTP {status} al verificar "
                                f"(bucket no público)"
                            )
                    elif status in (404, 410):
                        last_detail = f"HTTP {status} (el objeto no existe)"
                    elif status >= 500:
                        last_detail = f"HTTP {status} al verificar"
                    else:
                        # Cualquier otro status raro (el 401/403 en modo
                        # público ya quedó tratado arriba como "no
                        # existe" y sigue el bucle de reintentos).
                        return "unverifiable", f"HTTP {status} al verificar"
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    last_detail = f"error de red al verificar: {e}"
                if attempt < S3_VERIFY_ATTEMPTS:
                    await asyncio.sleep(delay)
                    delay = min(delay * 2, 8.0)
    except Exception as e:  # la verificación nunca debe romper la subida
        return "unverifiable", f"verificación no disponible: {e}"
    # Agotados los intentos: solo un 404/410 confirmado cuenta como muerto;
    # 5xx/red se tratan como "no verificable" para no tumbar la subida.
    if "no existe" in last_detail:
        return "dead", last_detail
    return "unverifiable", last_detail


async def _upload_and_verify(temp_path, key, url, filename, size):
    """Sube a UNA key con reintentos y verifica que el objeto quedó.

    Devuelve None si el objeto quedó activo (o no verificable en un
    bucket privado). Lanza _S3UploadExhausted si se agotan los
    reintentos en este namespace; los errores 4xx definitivos del PUT
    se propagan como RuntimeError (fallar rápido, como siempre).
    """
    put = _s3_put_direct if S3_UNSIGNED else _s3_put_signed
    last_error = "error desconocido"
    try:
        async with asyncio.timeout(S3_UPLOAD_TIMEOUT):
            for attempt in range(1, S3_UPLOAD_RETRIES + 1):
                try:
                    await put(temp_path, key, filename, size)
                except _S3RetryableError as e:
                    last_error = str(e)
                    log.warning(
                        f"S3 intento {attempt}/{S3_UPLOAD_RETRIES} "
                        f"falló ({key}): {e}"
                    )
                    if attempt < S3_UPLOAD_RETRIES:
                        await asyncio.sleep(min(2 ** attempt, 16))
                        continue
                    break

                if not S3_VERIFY_UPLOAD:
                    log.info(f"S3 subido (sin verificar): {key}")
                    return

                state, detail = await _verify_s3_object(url, size)
                if state == "ok":
                    log.info(
                        f"S3 OK verificado: {key} ({format_size(size)})"
                    )
                    return
                if state == "unverifiable":
                    log.warning(
                        f"S3: no se pudo verificar {key} ({detail}); "
                        f"se da por bueno provisionalmente"
                    )
                    return

                # state == "dead": el PUT dijo 200 pero el objeto no está.
                last_error = (
                    f"el archivo no quedó activo en S3 tras subirlo ({detail})"
                )
                log.warning(
                    f"S3 intento {attempt}/{S3_UPLOAD_RETRIES}: {last_error}"
                )
                if attempt < S3_UPLOAD_RETRIES:
                    await asyncio.sleep(min(2 ** attempt, 16))
    except (asyncio.TimeoutError, TimeoutError):
        raise RuntimeError(
            f"Tiempo agotado ({S3_UPLOAD_TIMEOUT}s) subiendo a S3"
        )
    raise _S3UploadExhausted(last_error)


# ============== TODUS OFICIAL (ToDus-SDK) ==============
# Flujo de la app oficial de toDus:
#   1. Login con una cuenta real (número cubano): registro por SMS
#      (users.reserve -> código -> users.register devuelve la
#      contraseña/secreto de 96 hex) o contraseña ya conocida.
#      POST auth.todus.cu/v2/auth/token -> JWT.
#      NUNCA se usa el "login_with_phone_only" del SDK (es un bypass
#      de autenticación del servidor: entrar como cualquier número).
#   2. Reserva XMPP todus:purl con el tamaño y tipo de archivo: el
#      servidor responde con la URL de subida (put, prefirmada) y la
#      de descarga (get). El objeto queda registrado a nombre de la
#      cuenta, fuera del barrido de anónimos.
#   3. PUT en streaming a la URL asignada (application/octet-stream,
#      como la app) y entrega de la URL get verificada.
# Si CUALQUIER paso falla, subir_a_s3 cae a la vía anónima adaptativa.
# El SDK (todus-sdk, sync) se importa de forma perezosa dentro de los
# wrappers *_sync y se ejecuta con asyncio.to_thread.

# Credenciales persistidas del login interactivo (como la sesión de TG:
# viven en DOWNLOAD_PATH; en Render necesitan el disco del render.yaml
# para sobrevivir redespliegues, o define TODUS_PHONE/PASSWORD).
_TODUS_CREDS_FILE = os.path.join(DOWNLOAD_PATH, "todus_credentials.json")
_todus_creds = None  # None = aún no cargado; dict tras la primera carga
_todus_lock = asyncio.Lock()


class _TodusTokenExpired(RuntimeError):
    """El JWT de toDus caducó: hay que hacer login de nuevo."""


def _todus_creds_load():
    """Credenciales efectivas {phone, password, jwt, jwt_exp} (cache)."""
    global _todus_creds
    if _todus_creds is None:
        data = {}
        try:
            with open(_TODUS_CREDS_FILE, encoding="utf-8") as fh:
                data = json.load(fh) or {}
        except Exception:
            data = {}
        _todus_creds = {
            "phone": str(data.get("phone") or ""),
            "password": str(data.get("password") or ""),
            "jwt": str(data.get("jwt") or ""),
            "jwt_exp": float(data.get("jwt_exp") or 0),
        }
        # Las env, si están, mandan sobre lo guardado (bootstrap).
        if TODUS_PHONE:
            if _todus_creds["phone"] != TODUS_PHONE:
                _todus_creds["jwt"] = ""
                _todus_creds["jwt_exp"] = 0
            _todus_creds["phone"] = TODUS_PHONE
        if TODUS_PASSWORD:
            _todus_creds["password"] = TODUS_PASSWORD
    return _todus_creds


def _todus_creds_save():
    """Persiste las credenciales en memoria a disco (0600)."""
    try:
        with open(_TODUS_CREDS_FILE, "w", encoding="utf-8") as fh:
            json.dump(_todus_creds, fh)
        try:
            os.chmod(_TODUS_CREDS_FILE, 0o600)
        except Exception:
            pass
    except Exception as e:
        log.warning(f"ToDus: no se pudieron guardar las credenciales: {e}")


def _todus_creds_clear():
    """Cierra la sesión guardada (las env, si existen, siguen valiendo)."""
    global _todus_creds
    _todus_creds = {
        "phone": TODUS_PHONE or "",
        "password": TODUS_PASSWORD or "",
        "jwt": "",
        "jwt_exp": 0,
    }
    try:
        os.unlink(_TODUS_CREDS_FILE)
    except Exception:
        pass


def _todus_has_account():
    creds = _todus_creds_load()
    return bool(creds.get("phone") and creds.get("password"))


def _todus_official_enabled():
    """¿Procede intentar la vía oficial en esta subida?"""
    return TODUS_UPLOAD_MODE != "anonymous" and _todus_has_account()


def _jwt_exp(token):
    """Epoch de caducidad del JWT (0 si no se puede saber)."""
    try:
        from todus import jwt_decode_payload
        return float(jwt_decode_payload(token).get("exp") or 0)
    except Exception:
        return 0


# --- Wrappers síncronos del SDK (se llaman con asyncio.to_thread) ---
# Devuelven tuplas con el teléfono ya normalizado por el SDK
# (53XXXXXXXX) y traducen TokenExpiredError a _TodusTokenExpired.

def _todus_make_client(phone, password=""):
    from todus import ToDusClient2
    return ToDusClient2(phone_number=phone, password=password)


def _todus_login_sync(phone, password):
    """Login con contraseña -> (teléfono normalizado, JWT)."""
    client = _todus_make_client(phone, password)
    client.login()
    if not client.token:
        raise RuntimeError("login sin token")
    return client.phone_number, client.token


def _todus_request_code_sync(phone):
    """Pide el SMS de registro -> teléfono normalizado."""
    client = _todus_make_client(phone)
    client.request_code()
    return client.phone_number


def _todus_validate_code_sync(phone, code):
    """Valida el código SMS -> (teléfono normalizado, contraseña 96 hex)."""
    client = _todus_make_client(phone)
    client.validate_code(code)
    if not client.password:
        raise RuntimeError("el servidor no devolvió contraseña")
    return client.phone_number, client.password


def _todus_reserve_sync(phone, token, size, file_type, filename, persistent):
    """Reserva todus:purl -> (put_url, get_url).

    Réplica de ToDusFileMixin.reserve_upload_url pero pasando
    persistent= (el SDK siempre reserva con persistent='false') y con
    deadline propio (el bucle del SDK puede girar para siempre si el
    servidor no responde nunca).
    """
    from todus import stanza, util
    from todus.errors import TokenExpiredError as _SdkExpired
    client = _todus_make_client(phone)
    sid = util.generate_token(5)
    sanitized = util.sanitize_filename(filename, int(file_type))
    deadline = time.monotonic() + 60
    try:
        with client._xmpp_session(token) as sock:
            sock.sendall(stanza.upload_query(
                sid, size, int(file_type),
                persistent=persistent, file_name=sanitized,
            ).encode())
            while True:
                response = client._recv_all(sock)
                if response is None:
                    raise RuntimeError(
                        "el servidor cerró la conexión durante la reserva")
                if response == "":
                    if time.monotonic() > deadline:
                        raise RuntimeError("timeout esperando la reserva (60 s)")
                    continue
                if "i='" + sid + "-3'" in response and "put='" in response:
                    put = re.search(r"put=['\"]([^'\"]+)['\"]", response)
                    get = re.search(r"get=['\"]([^'\"]+)['\"]", response)
                    if put and get:
                        return (put.group(1).replace("amp;", ""),
                                get.group(1).replace("amp;", ""))
                    raise RuntimeError("respuesta de reserva sin URLs")
                if "<not-authorized/>" in response:
                    raise _TodusTokenExpired("token rechazado en la reserva")
    except _SdkExpired as e:
        raise _TodusTokenExpired(str(e)) from e


def _todus_resolve_sync(phone, token, url):
    """Resuelve una URL corta de toDus a su URL real (todus:gurl)."""
    from todus.errors import TokenExpiredError as _SdkExpired
    client = _todus_make_client(phone)
    try:
        return client.get_real_download_url(token, url)
    except _SdkExpired as e:
        raise _TodusTokenExpired(str(e)) from e


async def _todus_ensure_token(force=False):
    """JWT válido de la cuenta configurada (None si no hay cuenta).

    Reutiliza el JWT guardado mientras no conste caducado; si caducó
    (o force=True), hace login con la contraseña y persiste el nuevo.
    """
    creds = _todus_creds_load()
    if not creds.get("phone") or not creds.get("password"):
        return None

    def _usable(c):
        jwt = c.get("jwt") or ""
        if not jwt or force:
            return ""
        exp = c.get("jwt_exp") or _jwt_exp(jwt)
        # Sin exp conocido se fía del JWT hasta que el servidor lo
        # rechace (entonces el caller reintenta con force=True).
        return jwt if (not exp or exp - time.time() > 300) else ""

    jwt = _usable(creds)
    if jwt:
        return jwt
    async with _todus_lock:
        creds = _todus_creds_load()
        jwt = _usable(creds)
        if jwt:
            return jwt
        phone_n, jwt = await asyncio.to_thread(
            _todus_login_sync, creds["phone"], creds["password"])
        creds["phone"] = phone_n or creds["phone"]
        creds["jwt"] = jwt
        creds["jwt_exp"] = _jwt_exp(jwt)
        _todus_creds_save()
        log.info("ToDus: login OK (JWT renovado)")
        return jwt


# FileType del protocolo toDus según extensión (0=FILE 1=VOICE 2=AUDIO
# 3=VIDEO 4=PICTURE 5=PROFILE; ver todus.types.FileType).
_TODUS_TYPE_BY_EXT = {
    ".opus": 1,
    ".mp3": 2, ".m4a": 2, ".aac": 2, ".flac": 2, ".wav": 2, ".ogg": 2,
    ".mp4": 3, ".mkv": 3, ".mov": 3, ".webm": 3, ".avi": 3, ".m4v": 3,
    ".jpg": 4, ".jpeg": 4, ".png": 4, ".gif": 4, ".webp": 4, ".bmp": 4,
}


def _todus_file_type(filename):
    ext = os.path.splitext(filename or "")[1].lower()
    return _TODUS_TYPE_BY_EXT.get(ext, 0)


async def _put_presigned(temp_path, url, size):
    """PUT en streaming a la URL prefirmada que asignó el servidor.

    Igual que la app oficial (y que ToDusFileMixin.upload_file):
    Content-Type application/octet-stream y Content-Length exacto.
    NO se añaden Content-Disposition ni Content-Type real: en una URL
    prefirmada, cabeceras no contempladas pueden romper la firma.
    """
    headers = {
        "Content-Type": "application/octet-stream",
        "Content-Length": str(size),
    }
    timeout = aiohttp.ClientTimeout(
        total=None, connect=30, sock_connect=30, sock_read=600
    )
    f = open(temp_path, "rb")
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.put(url, data=f, headers=headers) as resp:
                if resp.status in (200, 201, 204):
                    body = await resp.text()
                    if "<Error>" in body:
                        raise _S3RetryableError(
                            f"el PUT oficial devolvió un error con HTTP "
                            f"{resp.status}: {body[:200].strip()}"
                        )
                    return
                detail = (await resp.text())[:200].strip()
                if resp.status >= 500:
                    raise _S3RetryableError(
                        f"PUT oficial HTTP {resp.status}: {detail}")
                raise RuntimeError(
                    f"PUT oficial rechazado (HTTP {resp.status}): {detail}")
    except (aiohttp.ClientError, asyncio.TimeoutError, ConnectionError) as e:
        raise _S3RetryableError(f"error de red en el PUT oficial: {e}") from e
    finally:
        try:
            f.close()
        except Exception:
            pass


async def _verify_official_url(url, size):
    """Verifica la URL de descarga oficial con GET Range bytes=0-0.

    Nunca HEAD: una URL prefirmada para GET puede rechazarlo por la
    firma. Devuelve (estado, detalle) como _verify_s3_object, pero
    aquí solo 404/410 confirmado (o tamaño distinto) cuenta como
    muerto; cualquier otra cosa es "unverifiable" (el PUT aceptado
    por el servidor ya es buena señal: no tumbar la entrega).
    """
    timeout = aiohttp.ClientTimeout(total=30, connect=10, sock_read=20)
    headers = {
        "User-Agent": _S3_USER_AGENT,
        "Range": "bytes=0-0",
        "Cache-Control": "no-cache, no-store",
        "Pragma": "no-cache",
    }
    last = "sin respuesta"
    saw_404 = False
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            for attempt in range(3):
                try:
                    async with session.get(url, headers=headers,
                                           allow_redirects=True) as resp:
                        if resp.status in (200, 206):
                            remote = _remote_size(resp.headers)
                            if remote and size and remote != size:
                                return "dead", (
                                    f"tamaño remoto {format_size(remote)} "
                                    f"!= esperado {format_size(size)}")
                            return "ok", f"HTTP {resp.status}"
                        if resp.status in (404, 410):
                            saw_404 = True
                            last = f"HTTP {resp.status}"
                        else:
                            return "unverifiable", f"HTTP {resp.status}"
                except (aiohttp.ClientError, asyncio.TimeoutError) as e:
                    last = f"error de red: {e}"
                if attempt < 2:
                    await asyncio.sleep(1.5)
    except Exception as e:
        return "unverifiable", str(e)[:120]
    return ("dead", last) if saw_404 else ("unverifiable", last)


async def _todus_reserve(phone, token, size, ftype, filename):
    """Reserva con re-login si el token caducó. Devuelve (put, get, token).

    Si TODUS_PERSISTENT está activo se pide primero persistent=true y,
    si el servidor lo rechaza, se cae solo a persistent=false.
    """
    persistents = [True, False] if TODUS_PERSISTENT else [False]
    last_err = None
    for persistent in persistents:
        for _try in range(2):
            try:
                put, get = await asyncio.to_thread(
                    _todus_reserve_sync, phone, token, size, ftype,
                    filename, persistent)
                log.info(f"ToDus: reserva OK (persistent={persistent})")
                return put, get, token
            except _TodusTokenExpired:
                log.warning("ToDus: token caducado en la reserva; re-login")
                token = await _todus_ensure_token(force=True)
                if not token:
                    raise RuntimeError("sin sesión de toDus (re-login falló)")
            except Exception as e:
                last_err = e
                log.warning(
                    f"ToDus: reserva (persistent={persistent}) falló: {e}")
                break
    raise RuntimeError(f"reserva de toDus falló: {last_err}")


async def _upload_official(temp_path, filename, size, status_cb=None):
    """Sube por el flujo oficial y devuelve la URL de descarga.

    Lanza excepción ante cualquier fallo: subir_a_s3 decide entonces
    caer a la vía anónima adaptativa.
    """
    token = await _todus_ensure_token()
    if not token:
        raise RuntimeError(
            "cuenta de toDus no configurada (usa /toduslogin o "
            "TODUS_PHONE/TODUS_PASSWORD)")
    phone = _todus_creds_load().get("phone") or ""
    ftype = _todus_file_type(filename)

    async def _notify(step):
        if status_cb:
            try:
                await status_cb(_render_status_progress(step, filename, size))
            except Exception:
                pass

    await _notify("uploading")
    put_url, get_url, token = await _todus_reserve(
        phone, token, size, ftype, filename)
    log.info(f"ToDus: put={put_url[:90]} get={get_url[:90]}")

    # PUT a la URL asignada. Los 5xx/red reintentan con la misma URL;
    # un 4xx suele ser firma caducada/rechazada: se vuelve a reservar.
    last_err = None
    try:
        async with asyncio.timeout(S3_UPLOAD_TIMEOUT):
            for attempt in range(1, S3_UPLOAD_RETRIES + 1):
                try:
                    await _put_presigned(temp_path, put_url, size)
                    last_err = None
                    break
                except _S3RetryableError as e:
                    last_err = e
                    log.warning(f"ToDus: PUT intento {attempt} falló: {e}")
                    if attempt < S3_UPLOAD_RETRIES:
                        await asyncio.sleep(min(2 ** attempt, 16))
                except RuntimeError as e:
                    last_err = e
                    if attempt < S3_UPLOAD_RETRIES:
                        log.warning(
                            f"ToDus: PUT rechazado ({e}); re-reservando")
                        put_url, get_url, token = await _todus_reserve(
                            phone, token, size, ftype, filename)
                        await asyncio.sleep(1)
    except (asyncio.TimeoutError, TimeoutError):
        raise RuntimeError(
            f"tiempo agotado ({S3_UPLOAD_TIMEOUT}s) en la subida oficial")
    if last_err is not None:
        raise RuntimeError(f"PUT oficial falló: {last_err}")
    log.info("ToDus: PUT oficial OK")

    # URL de descarga: si es la corta (todus.cu/...), se resuelve a la
    # real vía todus:gurl; si ya es una URL de objeto, va tal cual.
    deliver = get_url
    if (urlparse(get_url).hostname or "").lower() == "todus.cu":
        for _try in range(2):
            try:
                resolved = await asyncio.to_thread(
                    _todus_resolve_sync, phone, token, get_url)
                if resolved:
                    deliver = resolved
                break
            except _TodusTokenExpired:
                token = await _todus_ensure_token(force=True)
                if not token:
                    break
            except Exception as e:
                log.warning(
                    f"ToDus: no se pudo resolver la URL corta ({e}); "
                    "se entrega tal cual")
                break

    await _notify("verifying")
    state, detail = await _verify_official_url(deliver, size)
    if state == "dead":
        raise RuntimeError(
            f"la URL oficial no responde ({detail}): {deliver[:90]}")
    if state == "unverifiable":
        log.warning(
            f"ToDus: URL oficial no verificable ({detail}); se entrega "
            "porque el servidor aceptó el PUT")
    return deliver


async def subir_a_s3(temp_path, filename, size, user_id=None, status_cb=None):
    """Sube el archivo a S3 y devuelve (url_principal, [urls_espejo]).

    Por qué espejos + espera anti-barrido (medido con
    scripts/probe_todus.py): el bucket público de toDus barre sin
    aviso namespaces enteros (la raíz y users/tg_* en <2 min; dk/,
    refugio durante días, amaneció vacío al día siguiente). Un solo
    namespace fijo es cuestión de tiempo que caiga. Por eso:

    1. Se sube a S3_MIRRORS namespaces candidatos (en el orden
       aprendido: el último que sobrevivió, primero).
    2. Se espera S3_SETTLE_SECONDS y se comprueba que siguen vivos
       ANTES de entregar ningún enlace (el barrido rápido actúa en
       <2 min, así que un enlace entregado ya superó esa ventana;
       contra los barridos lentos — users/df/ murió a los 10-18 min —
       el seguro es el espejo del punto 3, no la espera).
    3. Se entregan TODOS los enlaces vivos (principal + espejo): si
       un namespace cae mañana, el otro puede seguir funcionando.
    4. El namespace que muere queda relegado al final para la
       próxima subida (memoria en s3_namespace_state.json).

    Solo si ningún namespace consigue dejar el archivo vivo se lanza
    RuntimeError (mejor un error honesto que un ✓ con enlaces muertos).

    Si hay cuenta de toDus configurada, ANTES se intenta la vía
    oficial (_upload_official: reserva todus:purl + PUT a la URL que
    asigna el servidor, objeto registrado fuera del barrido). Todo lo
    anterior queda entonces como respaldo automático.
    """
    actual_size = os.path.getsize(temp_path)
    if size and size != actual_size:
        log.warning(
            f"S3: tamaño declarado {size} != real {actual_size}; uso el real"
        )
    size = actual_size

    # --- Vía oficial de toDus (ToDus-SDK) si hay cuenta ---
    # Cualquier fallo cae a la vía anónima adaptativa de abajo: la
    # subida nunca se pierde porque el SDK o la sesión fallen.
    if _todus_official_enabled():
        try:
            url = await _upload_official(
                temp_path, filename, size, status_cb)
            mirrors = []
            if TODUS_ANON_MIRROR and S3_UNSIGNED:
                try:
                    pfx = _ns_candidates()[0]
                    mkey = build_s3_key(user_id, filename, pfx)
                    murl = s3_public_url(mkey)
                    await _upload_and_verify(
                        temp_path, mkey, murl, filename, size)
                    mirrors = [murl]
                except Exception as e:
                    log.warning(
                        f"ToDus: espejo anónimo falló (se ignora): {e}")
            log.info(
                f"ToDus oficial OK final: principal={url} espejos={mirrors}")
            return url, mirrors
        except Exception as e:
            log.warning(
                f"ToDus oficial falló ({e}); se cae a la vía anónima "
                "adaptativa")

    mirrors = S3_MIRRORS if S3_UNSIGNED else 1
    settle = S3_SETTLE_SECONDS if (S3_UNSIGNED and S3_VERIFY_UPLOAD) else 0
    candidates = _ns_candidates()
    tried = []
    collected = []  # [(prefix, url)]
    errors = []

    async def _notify(step):
        if status_cb:
            try:
                await status_cb(
                    _render_status_progress(step, filename, size)
                )
            except Exception:
                pass

    async def _collect(prefixes):
        got = []
        for prefix in prefixes:
            if len(collected) + len(got) >= mirrors:
                break
            tried.append(prefix)
            key = build_s3_key(user_id, filename, prefix)
            url = s3_public_url(key)
            await _notify("uploading")
            try:
                await _upload_and_verify(temp_path, key, url, filename, size)
            except _S3UploadExhausted as e:
                errors.append(f"{prefix or 'raíz'}: {e}")
                log.warning(f"S3: namespace '{prefix}' agotado: {e}")
                await asyncio.to_thread(_ns_reorder, None, prefix)
                continue
            got.append((prefix, url))
        return got

    collected += await _collect(candidates)

    # Espera anti-barrido (hasta 2 rondas si todo muere y quedan
    # namespaces por probar).
    for _round in range(2):
        if not collected or settle <= 0:
            break
        await _notify("verifying")
        log.info(
            f"S3: esperando {settle}s para confirmar que sobrevive al "
            f"barrido: {[u for _, u in collected]}"
        )
        await asyncio.sleep(settle)
        survivors = []
        for prefix, url in collected:
            state, detail = await _verify_s3_object(url, size)
            if state == "dead":
                errors.append(
                    f"{prefix or 'raíz'}: barrido tras {settle}s ({detail})"
                )
                log.warning(
                    f"S3: el barrido eliminó {url} antes de entregarlo "
                    f"({detail}); namespace relegado"
                )
                await asyncio.to_thread(_ns_reorder, None, prefix)
            else:
                survivors.append((prefix, url))
        collected = survivors
        if collected:
            break
        remaining = [x for x in candidates if x not in tried]
        if not remaining:
            break
        collected += await _collect(remaining)

    if not collected:
        raise RuntimeError(
            "No se pudo dejar el archivo activo en S3 en ningún "
            f"namespace. Detalles: {'; '.join(errors[-3:]) or 'sin detalle'}. "
            "El bucket puede estar barriendo todo ahora mismo; "
            "reenvíalo en unos minutos."
        )

    winner = collected[0][0]
    await asyncio.to_thread(_ns_reorder, winner, None)
    urls = [u for _, u in collected]
    log.info(f"S3 OK final: principal={urls[0]} espejos={urls[1:]}")
    return urls[0], urls[1:]

# ============== DOWNLOAD ==============
async def _download_sequential(session, url, temp_path, total, job):
    downloaded = 0
    final_size = None
    content_type = None
    async with aiofiles.open(temp_path, "wb") as f:
        async with session.get(url, headers=BROWSER_HEADERS) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
            ct = resp.headers.get("Content-Type")
            final_size = int(resp.headers.get("Content-Length", 0)) or total
            async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                await f.write(chunk)
                downloaded += len(chunk)
                if downloaded > MAX_FILE_SIZE:
                    raise RuntimeError("Archivo supera el límite")
    return downloaded, ct


async def _download_parallel(session, url, temp_path, total, job):
    """Descarga paralela. Rechaza respuestas 200 (deben ser 206)."""
    if total <= 0:
        raise RuntimeError("Tamaño desconocido, no se puede descargar en paralelo")

    n_parts = min(PARALLEL_URL_PARTS, max(1, total // (10 * 1024 * 1024)))
    part_size = total // n_parts
    parts = []
    for i in range(n_parts):
        start = i * part_size
        end = start + part_size - 1 if i < n_parts - 1 else total - 1
        parts.append((i, start, end))

    part_paths = [None] * n_parts
    content_type = None

    async def download_part(idx, start, end):
        path = f"{temp_path}.part{idx}"
        job.temp_paths.append(path)
        headers = dict(BROWSER_HEADERS)
        headers["Range"] = f"bytes={start}-{end}"
        async with session.get(url, headers=headers) as resp:
            if resp.status != 206:
                raise RuntimeError(
                    f"Servidor no soporta Range (HTTP {resp.status} en parte {idx}). "
                    f"Cayendo a descarga secuencial."
                )
            nonlocal content_type
            if not content_type:
                content_type = resp.headers.get("Content-Type")
            actual_start = int(
                resp.headers.get("Content-Range", "bytes 0-0/0")
                .split(" ")[1].split("-")[0]
            )
            if actual_start != start:
                raise RuntimeError(f"Range no respetado en parte {idx}")
            bytes_received = 0
            async with aiofiles.open(path, "wb") as f:
                async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                    await f.write(chunk)
                    bytes_received += len(chunk)
                    if bytes_received > (end - start + 1) + 1024 * 1024:
                        raise RuntimeError(f"Parte {idx} excede tamaño esperado")
        part_paths[idx] = path
        return path

    try:
        await asyncio.gather(*[download_part(i, s, e) for i, s, e in parts])

        async with aiofiles.open(temp_path, "wb") as out:
            for p in part_paths:
                if not p:
                    raise RuntimeError("Parte faltante")
                async with aiofiles.open(p, "rb") as src:
                    while True:
                        chunk = await src.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        await out.write(chunk)
        return total, content_type
    finally:
        cleanup_paths([p for p in part_paths if p])


# ============== TELEGRAM FILE SENDING ==============
async def _send_file_via_telegram(chat_id, file_path, filename, size, caption):
    """Envía un archivo por Telegram con el tipo correcto (imagen/video/audio/doc).

    Devuelve: (tipo_str, error_str)
    """
    global _tg_client
    if not SEND_FILE_VIA_TG:
        return None, "envío por TG desactivado"
    if size > TG_UPLOAD_LIMIT:
        return None, f"archivo {format_size(size)} supera límite TG {format_size(TG_UPLOAD_LIMIT)}"

    ext = os.path.splitext(filename)[1].lower()
    try:
        if ext in IMAGE_EXT:
            await _tg_client.send_file(
                chat_id, file=file_path, caption=caption, parse_mode="markdown"
            )
            return "imagen", None
        if ext in VIDEO_EXT:
            await _tg_client.send_file(
                chat_id, file=file_path, caption=caption,
                parse_mode="markdown", supports_streaming=True,
            )
            return "video", None
        if ext in VOICE_EXT:
            await _tg_client.send_file(
                chat_id, file=file_path, caption=caption, parse_mode="markdown",
                voice_note=True,
            )
            return "nota de voz", None
        if ext in AUDIO_EXT:
            await _tg_client.send_file(
                chat_id, file=file_path, caption=caption, parse_mode="markdown",
            )
            return "audio", None
        # Documento genérico
        await _tg_client.send_file(
            chat_id, file=file_path, caption=caption, parse_mode="markdown",
            force_document=True,
        )
        return "archivo", None
    except Exception as e:
        log.warning(f"No se pudo enviar archivo a {chat_id}: {e}")
        return None, str(e)[:200]


# ============== UDYAT (descarga sin megas, solo Cuba) ==============
# En Cuba, descargar de s3.todus.cu consume megas facturables, pero el
# mismo MinIO responde en una IP interna (UDYAT_FREE_IP) que ETECSA no
# factura. La receta del cliente (sistema UDYAT; formato documentado en
# juanbas2005/todus-s3-api §15): conectar a la IP con las cabeceras
# Host: s3.todus.cu y User-Agent: Wget/1.21.3, ignorando el certificado
# self-signed. El bot no puede hacer eso por el usuario (depende de su
# red y de su gestor), pero sí generarle el manifiesto .udyat listo
# para importar en la app IroS3Dl:
#   {"f": nombre, "s": tamaño, "md5": ..., "c": [{"url", "size"}],
#    "h": IP gratuita, "original_host": "s3.todus.cu",
#    "platform": "android"}
# Se emite UN solo chunk con la URL principal, como el ejemplo
# documentado: la semántica de una lista con varios chunks no está
# documentada (¿partes? ¿alternativas?) y arriesgaría la descarga.

def _udyat_supported():
    """UDYAT solo tiene sentido contra el bucket público de toDus."""
    return (
        S3_UNSIGNED
        and (urlparse(S3_ENDPOINT).hostname or "") == "s3.todus.cu"
    )


def _md5_file_sync(path):
    """MD5 hex de un archivo en streaming (se llama vía to_thread)."""
    h = hashlib.md5()
    with open(path, "rb") as f:
        for block in iter(lambda: f.read(1 << 20), b""):
            h.update(block)
    return h.hexdigest()


def _udyat_free_url(public_url):
    """Reescribe el host de la URL pública a la IP gratuita (UDYAT)."""
    return urlparse(public_url)._replace(netloc=UDYAT_FREE_IP).geturl()


def build_udyat_manifest(public_url, filename, size, md5_hex):
    """Manifiesto .udyat (IroS3Dl) para descargar sin gastar megas."""
    return {
        "f": os.path.basename(filename or "archivo.bin"),
        "s": size,
        "md5": md5_hex,
        "c": [{"url": _udyat_free_url(public_url), "size": size}],
        "h": UDYAT_FREE_IP,
        "original_host": urlparse(public_url).hostname or "s3.todus.cu",
        "platform": "android",
    }


async def _maybe_send_udyat(job, chat_id, temp_path, filename, size, public_url):
    """Si el usuario activó /udyat, le envía el manifiesto .udyat.

    Nunca rompe el flujo principal: cualquier fallo aquí solo se loguea.
    """
    if not SEND_UDYAT_VIA_TG or job.user_id not in _udyat_users:
        return
    if not _udyat_supported():
        return
    try:
        md5_hex = await asyncio.to_thread(_md5_file_sync, temp_path)
        manifest = build_udyat_manifest(public_url, filename, size, md5_hex)
        data = json.dumps(
            manifest, ensure_ascii=False, indent=2
        ).encode("utf-8")
        stem = (
            os.path.splitext(os.path.basename(filename or "archivo"))[0]
            or "archivo"
        )
        payload = io.BytesIO(data)
        payload.name = f"{stem}.udyat"
        await _tg_client.send_file(
            chat_id, file=payload,
            caption=(
                "🆓 Manifiesto UDYAT: impórtalo en IroS3Dl (mantén "
                "presionado el botón ➕) para descargar SIN gastar "
                "megas (solo desde Cuba)."
            ),
            force_document=True,
        )
        log.info(f"UDYAT enviado a {chat_id}: {payload.name}")
    except Exception as e:
        log.warning(f"No se pudo enviar el manifiesto UDYAT a {chat_id}: {e}")


# ============== MAIN PROCESSOR ==============
async def process_job(job):
    if job.kind == "tg_media":
        await _process_tg_media(job)
    else:
        await _process_url(job)


async def _process_tg_media(job):
    """Descarga un archivo reenviado desde Telegram y lo sube a S3.

    Soporta:
    - Fotos, videos, audios, documentos
    - Mensajes reenviados (fwd_from) — identifica el chat de origen
    - Captions del mensaje original (no se procesan, solo se ignoran)
    - Mensajes de grupos/canales
    """
    global _tg_client
    chat_id = job.chat_id
    status_msg = job.status_msg
    message = job.message

    if not message or not message.media:
        await tg_edit_status(chat_id, status_msg, _render_error("No hay media para procesar."))
        return

    try:
        # Detectar origen si es reenviado de otro chat
        source_label = None
        fwd_from = getattr(message, "fwd_from", None)
        if fwd_from:
            # fwd_from puede tener from_id (usuario) o from_name (canal oculto)
            from_id = getattr(fwd_from, "from_id", None)
            from_name = getattr(fwd_from, "from_name", None)
            if from_name:
                source_label = f"canal {from_name}"
            elif from_id:
                # from_id es PeerUser/PeerChannel/PeerChat
                peer_type = type(from_id).__name__.replace("Peer", "").lower()
                peer_id = getattr(from_id, "user_id", None) or getattr(from_id, "channel_id", None) or getattr(from_id, "chat_id", None)
                if peer_id:
                    source_label = f"{peer_type} {peer_id}"

        # Determinar nombre y extensión del archivo
        filename = None
        size_hint = 0

        doc = getattr(message.media, "document", None)
        if doc:
            for attr in (doc.attributes or []):
                fname = getattr(attr, "file_name", None)
                if fname:
                    filename = fname
                    break
            if not filename:
                mime = doc.mime_type or "application/octet-stream"
                ext = ext_from_content_type(mime) or ".bin"
                filename = f"tg_{uuid.uuid4().hex[:8]}{ext}"
            size_hint = doc.size or 0
        else:
            # Photo
            filename = f"tg_{uuid.uuid4().hex[:8]}.jpg"
            size_hint = 0

        # Verificar tamaño si está disponible
        if size_hint and size_hint > MAX_FILE_SIZE:
            await tg_edit_status(
                chat_id, status_msg,
                _render_error(f"Archivo de {format_size(size_hint)} — supera el límite de {format_size(MAX_FILE_SIZE)}.")
            )
            return

        safe_name = sanitize_filename(filename)
        ext = os.path.splitext(safe_name)[1] or ".bin"
        temp_path = os.path.join(DOWNLOAD_PATH, f"{uuid.uuid4().hex}{ext}")
        job.temp_paths.append(temp_path)

        check_disk_space()
        # Mensaje de progreso único, minimalista
        await tg_edit_status(
            chat_id, status_msg,
            _render_status_progress("receiving", safe_name, size_hint)
        )

        # Descargar con Telethon
        try:
            async with asyncio.timeout(1800):
                await _tg_client.download_media(
                    message=message,
                    file=temp_path,
                )
        except asyncio.TimeoutError:
            raise RuntimeError("Tiempo agotado descargando desde Telegram")
        except Exception as e:
            raise RuntimeError(f"Error descargando desde Telegram: {e}")

        if not os.path.exists(temp_path):
            raise RuntimeError("La descarga no produjo ningún archivo")

        size = os.path.getsize(temp_path)
        if size > MAX_FILE_SIZE:
            raise RuntimeError(f"Archivo descargado ({format_size(size)}) supera el límite")

        filename = safe_name

        # Mensaje de progreso: subiendo
        await tg_edit_status(
            chat_id, status_msg,
            _render_status_progress("uploading", filename, size)
        )

        async def _s3_status(text):
            await tg_edit_status(chat_id, status_msg, text)

        url_final, _url_espejos = await subir_a_s3(
            temp_path, filename, size, user_id=job.user_id,
            status_cb=_s3_status,
        )
        url_espejo = _url_espejos[0] if _url_espejos else None

        name = re.sub(
            r"^[0-9a-f]{8}_", "",
            os.path.splitext(filename)[0]
        ).replace("_", " ")
        ext_out = os.path.splitext(filename)[1].replace(".", "")

        send_file = (job.user_id in _sendfile_users) and SEND_FILE_VIA_TG

        if not send_file:
            # Solo URL como texto (default)
            await tg_edit_status(
                chat_id, status_msg,
                _render_result_card(
                    name=name, ext=ext_out, size=size, s3_url=url_final, mirror_url=url_espejo,
                    source=source_label
                )
            )
            log.info(f"OK subido a S3 (textonly) para {chat_id}: {filename}")
            await _maybe_send_udyat(job, chat_id, temp_path, filename, size, url_final)
            return

        # Enviar archivo + caption
        caption = _render_result_card(
            name=name, ext=ext_out, size=size, s3_url=url_final, mirror_url=url_espejo,
            source=source_label
        )
        tipo, send_err = await _send_file_via_telegram(
            chat_id, temp_path, filename, size, caption
        )

        if tipo:
            log.info(f"OK enviado {tipo} a {chat_id}: {filename}")
            # El archivo ya fue enviado con el caption. El status_msg final lo reemplazamos por algo limpio.
            await tg_edit_status(
                chat_id, status_msg,
                f"{ICON_DONE} Enviado como {_tipo_label(tipo)} · {format_size(size)}"
            )
        else:
            # Falló el envío → al menos la URL quedó en el caption intentado. Mostramos URL y error.
            err_msg = f"No se pudo enviar como media: {send_err or 'desconocido'}"
            await tg_edit_status(
                chat_id, status_msg,
                _render_result_card(
                    name=name, ext=ext_out, size=size, s3_url=url_final, mirror_url=url_espejo,
                    source=source_label
                ) + f"\n\n{_render_error(err_msg)}"
            )
        await _maybe_send_udyat(job, chat_id, temp_path, filename, size, url_final)

    except asyncio.CancelledError:
        try:
            await tg_edit_status(chat_id, status_msg, _render_cancel())
        except Exception:
            pass
        raise
    except Exception as e:
        log.exception("error procesando media de Telegram")
        try:
            await tg_edit_status(
                chat_id, status_msg,
                _render_error(str(e)[:200])
            )
        except Exception:
            pass
    finally:
        cleanup_paths(job.temp_paths)
        job.temp_paths.clear()


async def _process_url(job):
    url = job.url
    chat_id = job.chat_id
    status_msg = job.status_msg

    # Validación SSRF
    safe, err = _is_safe_url(url)
    if not safe:
        log.warning(f"SSRF bloqueado: {url} - {err}")
        await tg_edit_status(chat_id, status_msg, _render_error(f"URL no permitida: {err}"))
        return

    raw_name = get_filename_from_url(url)
    if raw_name and "." in raw_name:
        filename = sanitize_filename(raw_name)
    else:
        filename = sanitize_filename(raw_name or f"file_{int(time.time())}")
    ext = os.path.splitext(filename)[1] or ".bin"
    temp_path = os.path.join(DOWNLOAD_PATH, f"{uuid.uuid4().hex}{ext}")
    job.temp_paths.append(temp_path)

    try:
        check_disk_space()
        # Estado inicial: descargando
        await tg_edit_status(
            chat_id, status_msg,
            _render_status_progress("downloading", filename)
        )

        async with aiohttp.ClientSession() as session:
            async with asyncio.timeout(1200):
                total = 0
                accepts_ranges = False
                try:
                    async with session.head(url, headers=BROWSER_HEADERS) as h:
                        if h.status < 400:
                            total = int(h.headers.get("Content-Length", 0))
                            accepts_ranges = (
                                h.headers.get("Accept-Ranges", "").lower() == "bytes"
                            )
                except Exception:
                    pass

                if total and total > MAX_FILE_SIZE:
                    raise RuntimeError(
                        f"Archivo de {format_size(total)} — supera el límite"
                    )

                size_downloaded = 0
                content_type = None

                if (
                    PARALLEL_URL_DOWNLOAD
                    and accepts_ranges
                    and total >= PARALLEL_URL_MIN_SIZE
                ):
                    try:
                        size_downloaded, content_type = await _download_parallel(
                            session, url, temp_path, total, job
                        )
                    except RuntimeError as e:
                        if "no soporta Range" in str(e):
                            log.info("Cayendo a descarga secuencial")
                            size_downloaded, content_type = await _download_sequential(
                                session, url, temp_path, total, job
                            )
                        else:
                            raise
                else:
                    size_downloaded, content_type = await _download_sequential(
                        session, url, temp_path, total, job
                    )

        # Si la URL no tenía extensión clara, usar Content-Type
        if (not raw_name or "." not in raw_name) and content_type:
            inferred_ext = ext_from_content_type(content_type)
            if inferred_ext:
                filename = filename.rsplit(".", 1)[0] + inferred_ext
                new_temp = temp_path.rsplit(".", 1)[0] + inferred_ext
                try:
                    os.rename(temp_path, new_temp)
                    if temp_path in job.temp_paths:
                        job.temp_paths.remove(temp_path)
                    job.temp_paths.append(new_temp)
                    temp_path = new_temp
                    ext = inferred_ext
                except Exception:
                    pass

        size = os.path.getsize(temp_path)

        # Estado: subiendo
        await tg_edit_status(
            chat_id, status_msg,
            _render_status_progress("uploading", filename, size)
        )

        async def _s3_status(text):
            await tg_edit_status(chat_id, status_msg, text)

        url_final, _url_espejos = await subir_a_s3(
            temp_path, filename, size, user_id=job.user_id,
            status_cb=_s3_status,
        )
        url_espejo = _url_espejos[0] if _url_espejos else None

        name = re.sub(
            r"^[0-9a-f]{8}_", "",
            os.path.splitext(filename)[0]
        ).replace("_", " ")
        ext_out = os.path.splitext(filename)[1].replace(".", "")

        send_file = (job.user_id in _sendfile_users) and SEND_FILE_VIA_TG

        if not send_file:
            # Default: solo URL como texto
            await tg_edit_status(
                chat_id, status_msg,
                _render_result_card(
                    name=name, ext=ext_out, size=size, s3_url=url_final, mirror_url=url_espejo,
                    source="web"
                )
            )
            log.info(f"OK subido a S3 (textonly) para {chat_id}: {filename}")
            await _maybe_send_udyat(job, chat_id, temp_path, filename, size, url_final)
            return

        # Enviar archivo + caption
        caption = _render_result_card(
            name=name, ext=ext_out, size=size, s3_url=url_final, mirror_url=url_espejo,
            source="web"
        )
        tipo, send_err = await _send_file_via_telegram(
            chat_id, temp_path, filename, size, caption
        )

        if tipo:
            log.info(f"OK enviado {tipo} a {chat_id}: {filename}")
            await tg_edit_status(
                chat_id, status_msg,
                f"{ICON_DONE} Enviado como {_tipo_label(tipo)} · {format_size(size)}"
            )
        else:
            err_msg = f"No se pudo enviar como media: {send_err or 'desconocido'}"
            await tg_edit_status(
                chat_id, status_msg,
                _render_result_card(
                    name=name, ext=ext_out, size=size, s3_url=url_final, mirror_url=url_espejo,
                    source="web"
                ) + f"\n\n{_render_error(err_msg)}"
            )
        await _maybe_send_udyat(job, chat_id, temp_path, filename, size, url_final)

    except asyncio.CancelledError:
        try:
            await tg_edit_status(chat_id, status_msg, _render_cancel())
        except Exception:
            pass
        raise
    except Exception as e:
        log.exception("error procesando URL")
        try:
            await tg_edit_status(
                chat_id, status_msg,
                _render_error(str(e)[:200])
            )
        except Exception:
            pass
    finally:
        cleanup_paths(job.temp_paths)
        job.temp_paths.clear()


# ============== TELEGRAM EVENT HANDLERS ==============
# Usuarios que han activado /sendfile (recibir el archivo además de la URL)
# Por defecto el bot SOLO envía la URL (ahorra datos en toDus).
_sendfile_users = set()
# Usuarios que han activado /udyat (recibir el manifiesto .udyat para
# descargar sin gastar megas vía IroS3Dl).
_udyat_users = set()
# Login de toDus en curso esperando el código SMS:
# user_id -> {"phone": str (normalizado), "ts": float}
_todus_pending_login = {}


def _is_admin(user_id):
    """¿Puede gestionar la cuenta de toDus del bot (/toduslogin...)?

    En modo userbot, solo el dueño de la cuenta. En modo bot, solo los
    IDs de ADMIN_TG_IDS. Sin ninguna de las dos cosas devuelve False:
    el login interactivo queda deshabilitado (usar las env
    TODUS_PHONE/TODUS_PASSWORD).
    """
    if user_id is None:
        return False
    if _is_userbot_mode and _tg_owner_id is not None:
        return user_id == _tg_owner_id
    return ADMIN_TG_IDS is not None and user_id in ADMIN_TG_IDS


async def _todus_finish_login(event, sender_id, phone, code):
    """Completa el registro SMS: valida el código, hace login y guarda."""
    try:
        phone_n, password = await asyncio.to_thread(
            _todus_validate_code_sync, phone, code)
        phone_n, jwt = await asyncio.to_thread(
            _todus_login_sync, phone_n, password)
    except Exception as e:
        await event.respond(
            f"{ICON_ERROR} El código no validó: {str(e)[:180]}\n"
            "Revisa el código e inténtalo de nuevo, o /toduslogin "
            "NÚMERO para pedir otro.")
        return
    _todus_pending_login.pop(sender_id, None)
    _todus_creds_load()
    _todus_creds.update(
        phone=phone_n, password=password, jwt=jwt, jwt_exp=_jwt_exp(jwt))
    _todus_creds_save()
    log.info(f"ToDus: login por SMS completado ({_mask_phone(phone_n)})")
    await event.respond(
        f"{ICON_DONE} Sesión de toDus iniciada ({_mask_phone(phone_n)}).\n\n"
        "A partir de ahora las subidas usan la vía oficial (el servidor "
        "asigna el enlace: queda fuera del barrido de anónimos). Si la "
        "vía oficial falla, el bot cae solo a la anterior.\n"
        "Estado cuando quieras: /todusstatus")


async def _todus_password_login(event, phone, password):
    """Login directo con contraseña ya conocida de la cuenta."""
    try:
        phone_n, jwt = await asyncio.to_thread(
            _todus_login_sync, phone, password)
    except Exception as e:
        await event.respond(f"{ICON_ERROR} Login fallido: {str(e)[:180]}")
        return
    _todus_creds_load()
    _todus_creds.update(
        phone=phone_n, password=password, jwt=jwt, jwt_exp=_jwt_exp(jwt))
    _todus_creds_save()
    log.info(f"ToDus: login con contraseña completado ({_mask_phone(phone_n)})")
    await event.respond(
        f"{ICON_DONE} Sesión de toDus iniciada ({_mask_phone(phone_n)}).\n"
        "⚠️ Borra el mensaje con la contraseña: queda en el historial "
        "del chat. La próxima vez basta el login automático.")


async def _handle_todus_command(event, body, sender_id):
    """Despacha /toduslogin, /toduscode, /todusstatus y /toduslogout.

    Solo se llega aquí si _is_admin(sender_id) ya dio True.
    """
    parts = body.split()
    cmd = parts[0].lower() if parts else ""
    if cmd == "/todusstatus":
        await event.respond(_render_todus_status())
        return
    if cmd == "/toduslogout":
        _todus_creds_clear()
        _todus_pending_login.pop(sender_id, None)
        await event.respond(
            f"{ICON_DONE} Sesión de toDus cerrada.\n\n"
            "Las próximas subidas usarán la vía anónima (salvo que "
            "estén definidas TODUS_PHONE/TODUS_PASSWORD por entorno).")
        return
    if cmd == "/toduslogin":
        if len(parts) == 2:
            try:
                phone_n = await asyncio.to_thread(
                    _todus_request_code_sync, parts[1].strip())
            except Exception as e:
                await event.respond(
                    f"{ICON_ERROR} No se pudo pedir el código SMS: "
                    f"{str(e)[:180]}")
                return
            _todus_pending_login[sender_id] = {
                "phone": phone_n, "ts": time.time()}
            await event.respond(
                "📩 Código SMS enviado. Respóndeme con el código de 6 "
                "dígitos (o usa /toduscode 123456). Caduca en 5 minutos.")
            return
        if len(parts) == 3:
            await _todus_password_login(
                event, parts[1].strip(), parts[2].strip())
            return
        await event.respond(
            "Uso: /toduslogin NÚMERO (login por SMS) o /toduslogin "
            "NÚMERO CONTRASEÑA si ya tienes la contraseña de la cuenta.")
        return
    if cmd == "/toduscode":
        pend = _todus_pending_login.get(sender_id)
        if not pend:
            await event.respond(
                f"{ICON_ERROR} No hay login SMS en curso. "
                "Empieza con /toduslogin NÚMERO.")
            return
        if time.time() - pend["ts"] > 300:
            _todus_pending_login.pop(sender_id, None)
            await event.respond(
                f"{ICON_ERROR} El código caducó (5 min). "
                "Vuelve a empezar con /toduslogin.")
            return
        if len(parts) != 2 or not parts[1].isdigit():
            await event.respond("Uso: /toduscode 123456")
            return
        await _todus_finish_login(
            event, sender_id, pend["phone"], parts[1].strip())
        return
    await event.respond(
        "Comandos de toDus: /toduslogin · /toduscode · "
        "/todusstatus · /toduslogout")


async def on_tg_message(event):
    """Handler de mensajes entrantes de Telegram (Telethon).

    Acepta:
    - URLs (http/https) en texto → descarga y sube a S3
    - Archivos reenviados (fotos, videos, audios, documentos) → sube a S3
    """
    try:
        chat_id = event.chat_id
        sender = await event.get_sender()
        sender_id = sender.id if sender else None
        sender_name = (
            getattr(sender, "username", None)
            or getattr(sender, "first_name", None)
            or str(sender_id)
        )
        body = (event.raw_text or "").strip()

        # ===== FILTRO MODO USERBOT =====
        # En modo userbot, solo procesar mensajes del dueño (en Saved Messages
        # o chats donde el dueño es el sender). Esto evita que el userbot
        # intercepte respuestas de @BotFather u otros bots cuando el dueño
        # habla con ellos.
        if _is_userbot_mode and sender_id != _tg_owner_id:
            # Si hay allowlist Y el sender está en ella, también procesar
            if not (ALLOWED_TG_IDS is not None and sender_id in ALLOWED_TG_IDS):
                return  # ignorar silenciosamente

        log.info(f"📩 de {sender_name} ({sender_id}): {body[:80] if body else '(media)'}")

        # Verificar allowlist
        if ALLOWED_TG_IDS is not None and sender_id not in ALLOWED_TG_IDS:
            log.warning(f"Usuario no autorizado: {sender_id}")
            await event.respond(_render_unauthorized())
            return

        low = body.lower() if body else ""

        if low in ("/start", "hola", "ayuda", "help", "/help"):
            await event.respond(_render_help(is_userbot=_is_userbot_mode, is_admin=_is_admin(sender_id)))
            return

        if low in ("/cancel", "cancelar"):
            result = await job_queue.cancel_user(sender_id)
            if result == "none":
                await event.respond(_render_no_active_job())
            else:
                await event.respond(_render_cancel())
            return

        if low == "/status":
            active = job_queue.has_user_job(sender_id)
            await event.respond(
                _render_active_job() if active else "✓ Sin trabajos activos."
            )
            return

        if low in ("/sendfile", "/textonly"):
            if sender_id in _sendfile_users:
                _sendfile_users.discard(sender_id)
                await event.respond(_render_sendfile_state(enabled=False))
            else:
                _sendfile_users.add(sender_id)
                await event.respond(_render_sendfile_state(enabled=True))
            return

        if low in ("/udyat", "/sinmegas"):
            if not SEND_UDYAT_VIA_TG or not _udyat_supported():
                await event.respond(
                    f"{ICON_ERROR} El manifiesto UDYAT no está disponible en este bot "
                    "(solo aplica al S3 público de toDus)."
                )
                return
            if sender_id in _udyat_users:
                _udyat_users.discard(sender_id)
                await event.respond(_render_udyat_state(enabled=False))
            else:
                _udyat_users.add(sender_id)
                await event.respond(_render_udyat_state(enabled=True))
            return

        # ===== CUENTA DE TODUS (solo admin) =====
        if low.startswith(("/toduslogin", "/toduscode")) or low in (
                "/todusstatus", "/toduslogout"):
            if not _is_admin(sender_id):
                await event.respond(
                    f"{ICON_ERROR} Solo el administrador del bot puede "
                    "gestionar la cuenta de toDus.")
                return
            await _handle_todus_command(event, body, sender_id)
            return

        # Código SMS "suelto" mientras hay un login en curso: se
        # captura aquí para no obligar a escribir /toduscode.
        _pend = _todus_pending_login.get(sender_id)
        if _pend and _is_admin(sender_id):
            _code = body.replace(" ", "").strip()
            if _code.isdigit() and 4 <= len(_code) <= 8:
                if time.time() - _pend["ts"] <= 300:
                    await _todus_finish_login(
                        event, sender_id, _pend["phone"], _code)
                else:
                    _todus_pending_login.pop(sender_id, None)
                    await event.respond(
                        f"{ICON_ERROR} El código caducó (5 min). "
                        "Vuelve a empezar con /toduslogin.")
                return

        # ===== DETECCIÓN DE MEDIA REENVIADA =====
        if event.message and event.message.media:
            if job_queue.has_user_job(sender_id):
                await event.respond(_render_busy())
                return

            # Mensaje inicial mínimo: el procesador lo editará después
            status_msg = await tg_send_status(
                chat_id, f"{ICON_PROCESSING} Procesando..."
            )
            job = QueuedJob(
                user_id=sender_id, kind="tg_media", url=None,
                original_name=None,
                chat_id=chat_id, status_msg=status_msg,
            )
            job.message = event.message

            async def _factory():
                return await process_job(job)

            try:
                await job_queue.run(job, _factory)
            except ValueError as e:
                if "user_already_running" in str(e):
                    await event.respond(_render_busy())
                else:
                    log.exception("job error")
            except Exception:
                log.exception("job error")
            return

        # ===== DETECCIÓN DE URL =====
        if not body:
            await event.respond(_render_no_url())
            return

        match = URL_RE.search(body)
        if not match:
            await event.respond(_render_no_url())
            return

        if job_queue.has_user_job(sender_id):
            await event.respond(_render_busy())
            return

        url = match.group(1)
        # Mensaje inicial mínimo — el procesador lo editará con el progreso
        status_msg = await tg_send_status(
            chat_id, f"{ICON_PROCESSING} Procesando..."
        )
        job = QueuedJob(
            user_id=sender_id, kind="url", url=url,
            chat_id=chat_id, status_msg=status_msg,
        )

        async def _factory():
            return await process_job(job)

        try:
            await job_queue.run(job, _factory)
        except ValueError as e:
            if "user_already_running" in str(e):
                await event.respond(_render_busy())
            else:
                log.exception("job error")
        except Exception:
            log.exception("job error")

    except Exception as e:
        log.exception(f"on_tg_message: {e}")


# ============== WEB SERVER (para healthcheck de Render) ==============
START_TIME = time.time()


async def health_handler(request):
    return web.json_response({
        "status": "healthy",
        "uptime": round(time.time() - START_TIME, 1),
        "active_jobs": len(job_queue.active),
        "watchdog_enabled": WATCHDOG_ENABLED,
        "tg_connected": _tg_client is not None and _tg_client.is_connected(),
    })


async def root_handler(request):
    return web.json_response({
        "status": "online",
        "service": "telegram-s3-bot",
    })


def make_web_app():
    a = web.Application()
    a.router.add_get("/", root_handler)
    a.router.add_get("/health", health_handler)
    a.router.add_get("/healthz", health_handler)  # alias Render
    return a


async def run_web():
    a = make_web_app()
    runner = web.AppRunner(a)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info(f"🌐 Web server escuchando en 0.0.0.0:{PORT}")
    return runner


# ============== WATCHDOG + SHUTDOWN ==============
async def watchdog():
    while not _shutdown_event.is_set():
        await asyncio.sleep(30)
        uptime = time.time() - START_TIME
        if WATCHDOG_ENABLED and uptime > WATCHDOG_LIFETIME:
            log.warning(f"watchdog: {uptime:.0f}s, iniciando shutdown graceful")
            _shutdown_event.set()
            await asyncio.sleep(5)
            os.kill(os.getpid(), signal.SIGTERM)
            return


def _setup_signal_handlers():
    """Registra handler de SIGTERM para shutdown graceful en Render."""
    def handler(signum, frame):
        log.info(f"Señal {signum} recibida, iniciando shutdown graceful...")
        _shutdown_event.set()
        def _force_exit():
            time.sleep(10)
            log.warning("Forzando os._exit(0)")
            os._exit(0)
        threading.Thread(target=_force_exit, daemon=True).start()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


# ============== FLOODWAIT HANDLING ==============
_session_string_saved = False


def _handle_floodwait(e: FloodWaitError):
    """Maneja FloodWaitError sin crashear — duerme y deja que Render reinicie solo."""
    wait = e.seconds
    log.error(
        f"🚫 FloodWait de Telegram: hay que esperar {wait}s ({wait/60:.1f} min). "
        f"Esto NO es un bug — Telegram rate-limita logins repetidos. "
        f"El proceso dormirá y saldrá para que Render lo reinicie después."
    )
    # Duerme un poco más del wait + margen, pero en pasos pequeños para poder
    # responder a SIGTERM si llega.
    sleep_total = wait + 60  # 1 min de margen
    log.info(f"Durmiendo {sleep_total}s antes de salir graceful...")
    end_at = time.time() + sleep_total
    while time.time() < end_at:
        if _shutdown_event.is_set():
            log.info("Shutdown solicitado, saliendo antes de FloodWait completo")
            return
        time.sleep(5)
    log.warning("FloodWait cumplido, saliendo para que Render reinicie el proceso")
    os._exit(0)


# ============== MAIN ==============
async def main():
    global _loop, _tg_client, _session_string_saved
    _loop = asyncio.get_running_loop()

    # Validar config de Telegram
    if not TG_API_ID or not TG_API_HASH:
        log.error("❌ Falta TG_API_ID o TG_API_HASH. Obténlos en https://my.telegram.org/apps")
        return

    _setup_signal_handlers()

    web_runner = await run_web()
    asyncio.create_task(watchdog())

    # --- Inicializar Telethon ---
    # Sesión persistente: si hay TG_SESSION_STRING, úsala (incluso en modo bot).
    # Si no, usa archivo en disco persistente (DOWNLOAD_PATH) para no re-login en cada deploy.
    session_path = os.path.join(DOWNLOAD_PATH, "tg_s3_bot.session")
    os.makedirs(DOWNLOAD_PATH, exist_ok=True)

    # Detectar si la sesión en disco ya existe (evita el bloque de "primer login")
    global _session_string_saved
    if os.path.exists(session_path):
        _session_string_saved = True
        log.info(f"📦 Sesión existente encontrada en {session_path}")

    if TG_SESSION_STRING:
        # StringSession tiene prioridad — no requiere re-login nunca
        log.info("🔗 Usando TG_SESSION_STRING pre-generada (sin re-login)")
        _tg_client = TelegramClient(
            StringSession(TG_SESSION_STRING),
            api_id=TG_API_ID,
            api_hash=TG_API_HASH,
        )
        try:
            await _tg_client.start()
        except FloodWaitError as e:
            _handle_floodwait(e)
            return
        # Marcar modo userbot para filtrar mensajes
        global _is_userbot_mode
        _is_userbot_mode = True

    elif TG_BOT_TOKEN:
        # Modo bot oficial con sesión en disco persistente
        log.info(f"🤖 Iniciando Telethon en modo BOT (sesión: {session_path})")
        _tg_client = TelegramClient(
            session_path,
            api_id=TG_API_ID,
            api_hash=TG_API_HASH,
        )
        try:
            await _tg_client.start(bot_token=TG_BOT_TOKEN)
        except FloodWaitError as e:
            _handle_floodwait(e)
            return

        # Si la sesión recién se creó, mostrar el StringSession para que el usuario
        # lo guarde como env var y evitar futuros re-logins.
        if not _session_string_saved:
            try:
                session_str = _tg_client.session.save()
                # Guardar en disco para futuras referencias
                hint_path = os.path.join(DOWNLOAD_PATH, "SESSION_STRING.txt")
                with open(hint_path, "w") as f:
                    f.write(session_str)
                log.warning(
                    "⚠️  PRIMER LOGIN — guarda este SESSION_STRING como env var "
                    f"TG_SESSION_STRING para evitar re-logins y FloodWait:\n"
                    f"{session_str}\n"
                    f"(También guardado en {hint_path})"
                )
            except Exception as e:
                log.warning(f"No se pudo exportar SESSION_STRING: {e}")

    else:
        log.error(
            "❌ Falta TG_BOT_TOKEN o TG_SESSION_STRING. "
            "Necesitas uno de los dos para arrancar."
        )
        return

    me = await _tg_client.get_me()
    me_name = getattr(me, "username", None) or getattr(me, "first_name", None) or str(me.id)
    log.info(f"✅ Telegram conectado como: {me_name} (id={me.id})")

    # Guardar el ID del dueño (en modo userbot) para filtrar mensajes
    global _tg_owner_id
    _tg_owner_id = me.id
    if _is_userbot_mode:
        log.info(
            f"👤 MODO USERBOT activado — solo responderé a mensajes del dueño "
            f"(id={_tg_owner_id}) o de usuarios en ALLOWED_TG_IDS. "
            f"Para usar el bot, envíame URLs/archivos a tu chat 'Saved Messages' "
            f"o a tu propia cuenta."
        )

    # Registrar handler (callback, event)
    # En Telethon: client.add_event_handler(callback, event_type)
    # events.NewMessage(incoming=True) filtra mensajes salientes automáticamente.
    _tg_client.add_event_handler(
        on_tg_message,
        events.NewMessage(incoming=True)
    )

    if ALLOWED_TG_IDS is not None:
        log.info(f"Allowlist activa: {len(ALLOWED_TG_IDS)} IDs")
    if SSRF_BLOCK_PRIVATE:
        log.info("Protección SSRF activada")
    log.info(f"BOT READY — límite {format_size(MAX_FILE_SIZE)}")

    try:
        await _shutdown_event.wait()
    finally:
        log.info("apagando limpiamente...")
        # Cancelar jobs activos
        for uid, job in list(job_queue.active.items()):
            if job.task and not job.task.done():
                job.task.cancel()
        try:
            await _tg_client.disconnect()
        except Exception:
            pass
        await web_runner.cleanup()
        log.info("apagado completo")


if __name__ == "__main__":
    asyncio.run(main())
