"""
Bot de Telegram (Telethon) - Subida a S3 + reenvío al usuario.

Flujo:
1. El usuario envía una URL o reenvía un archivo por Telegram.
2. El bot descarga el archivo (secuencial o paralelo).
3. Lo sube a S3 (toDus S3 o cualquier S3 configurable).
4. Devuelve SIEMPRE la URL pública S3.
5. Opcionalmente reenvía el archivo por Telegram (/sendfile).

Modo único: bot oficial.
- TG_API_ID + TG_API_HASH + TG_BOT_TOKEN (login directo), o
- TG_API_ID + TG_API_HASH + TG_SESSION_STRING de un BOT (generado con
  scripts/generate_bot_session.py) para evitar re-logins en hosts efímeros.
- Se rechazan las sesiones de cuentas personales: este bot solo funciona con
  cuentas de bot.

Notas de esta versión:
- Sin límite de tamaño por defecto (MAX_FILE_SIZE_MB=0 y TG_UPLOAD_LIMIT_MB=0).
  Telegram sigue imponiendo su límite físico por archivo.
- Detección correcta de mensajes con vista previa (MessageMediaWebPage).
- Cola por usuario: los álbumes y los envíos seguidos no se pierden.
- SSRF con validación de redirecciones y de todas las IPs resueltas.
- El SESSION_STRING ya no se escribe en logs ni en disco.
- Uploads S3 con Content-Type correcto y concurrencia.
- Watchdog que no interrumpe trabajos activos.
"""
import asyncio
import ipaddress
import logging
import mimetypes
import os
import re
import signal
import socket
import threading
import time
import uuid
from collections import defaultdict, deque
from contextlib import asynccontextmanager
from urllib.parse import quote, unquote, urljoin, urlparse

import aiofiles
import aiohttp
import boto3
from aiohttp import web
from aiohttp.abc import AbstractResolver
from boto3.s3.transfer import TransferConfig
from botocore import UNSIGNED
from botocore.config import Config as BotoConfig
from telethon import TelegramClient, events
from telethon.errors import FloodWaitError, MessageNotModifiedError
from telethon.sessions import StringSession
from telethon.tl.types import (
    MessageMediaDocument,
    MessageMediaPhoto,
    MessageMediaWebPage,
)


# ============== CONFIG ==============
def _env_int(name, default, minimum=None):
    raw = os.environ.get(name)
    if raw is None or not raw.strip():
        return default
    try:
        value = int(raw.strip())
    except ValueError as err:
        raise SystemExit(
            f"❌ {name} debe ser un entero (valor recibido: {raw!r})"
        ) from err
    if minimum is not None and value < minimum:
        raise SystemExit(f"❌ {name} debe ser >= {minimum} (valor recibido: {value})")
    return value


def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on", "si", "sí")


def _parse_allowed_ids(raw):
    raw = (raw or "").strip()
    if not raw:
        return None
    result = set()
    for part in raw.replace(" ", "").split(","):
        if not part:
            continue
        try:
            result.add(int(part))
        except ValueError as err:
            raise SystemExit(
                f"❌ ALLOWED_TG_IDS contiene un ID inválido: {part!r}"
            ) from err
    return result or None


# --- Telegram ---
TG_API_ID = _env_int("TG_API_ID", 0, minimum=0)
TG_API_HASH = os.environ.get("TG_API_HASH", "").strip()
TG_BOT_TOKEN = os.environ.get("TG_BOT_TOKEN", "").strip()
TG_SESSION_STRING = os.environ.get("TG_SESSION_STRING", "").strip()
ALLOWED_TG_IDS = _parse_allowed_ids(os.environ.get("ALLOWED_TG_IDS", ""))

# --- S3 ---
S3_ENDPOINT = os.environ.get("S3_ENDPOINT", "https://s3.todus.cu").rstrip("/")
S3_BUCKET = os.environ.get("S3_BUCKET", "stream").strip("/")
S3_REGION = os.environ.get("S3_REGION", "us-east-1")
S3_ACCESS_KEY = os.environ.get("S3_ACCESS_KEY", "public")
S3_SECRET_KEY = os.environ.get("S3_SECRET_KEY", "public")
S3_UNSIGNED = _env_bool("S3_UNSIGNED", True)
# Base pública opcional (CDN, dominio propio, bucket virtual-host...).
# Si se define, la URL final será {S3_PUBLIC_BASE_URL}/{key}.
S3_PUBLIC_BASE_URL = os.environ.get("S3_PUBLIC_BASE_URL", "").strip().rstrip("/")

# --- Servidor ---
PORT = _env_int("PORT", 10000, minimum=1)

# --- Descarga ---
DOWNLOAD_PATH = os.environ.get("DOWNLOAD_PATH", "/tmp/tg_uploads").strip() or "/tmp/tg_uploads"
# 0 = sin límite de tamaño (solo lo que imponga Telegram / el disco).
MAX_FILE_SIZE = _env_int("MAX_FILE_SIZE_MB", 0, minimum=0) * 1024 * 1024
# 0 = sin comprobación previa de subida a Telegram (Telegram impone su tope).
TG_UPLOAD_LIMIT = _env_int("TG_UPLOAD_LIMIT_MB", 0, minimum=0) * 1024 * 1024
CHUNK_SIZE = _env_int("CHUNK_SIZE_MB", 8, minimum=1) * 1024 * 1024
# Tiempos máximos para archivos grandes. No son límites de tamaño.
DOWNLOAD_TIMEOUT = _env_int("DOWNLOAD_TIMEOUT_SECONDS", 3600, minimum=1)
UPLOAD_TIMEOUT = _env_int("UPLOAD_TIMEOUT_SECONDS", 3600, minimum=1)

# --- Watchdog ---
WATCHDOG_LIFETIME = _env_int("WATCHDOG_LIFETIME", 14100, minimum=60)
WATCHDOG_ENABLED = _env_bool("WATCHDOG_ENABLED", True)

# --- Descarga paralela ---
PARALLEL_URL_DOWNLOAD = _env_bool("PARALLEL_URL_DOWNLOAD", True)
PARALLEL_URL_PARTS = _env_int("PARALLEL_URL_PARTS", 4, minimum=1)
PARALLEL_URL_MIN_SIZE = _env_int("PARALLEL_URL_MIN_SIZE_MB", 50, minimum=0) * 1024 * 1024

# --- Seguridad ---
SSRF_BLOCK_PRIVATE = _env_bool("SSRF_BLOCK_PRIVATE", True)

# --- Comportamiento ---
# Por defecto solo se envía la URL S3 como texto (ahorra datos en toDus).
SEND_FILE_VIA_TG = _env_bool("SEND_FILE_VIA_TG", False)

# --- HTTP Headers ---
# "identity" evita que el servidor comprima la respuesta y rompa los rangos.
BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "*/*",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
    "Accept-Encoding": "identity",
}

# --- Logging ---
logging.basicConfig(
    level=getattr(logging, os.environ.get("LOG_LEVEL", "INFO").upper(), logging.INFO),
    format="%(asctime)s - %(levelname)s - %(name)s - %(message)s",
)
logging.getLogger("aiohttp.access").setLevel(logging.WARNING)
logging.getLogger("telethon").setLevel(os.environ.get("TELETHON_LOG_LEVEL", "WARNING"))
log = logging.getLogger("bot")

os.makedirs(DOWNLOAD_PATH, exist_ok=True)

# ============== S3 CONFIG ==============
_S3_CONFIG = BotoConfig(
    signature_version=UNSIGNED if S3_UNSIGNED else "s3v4",
    retries={"max_attempts": 3, "mode": "adaptive"},
    max_pool_connections=50,
    connect_timeout=30,
    read_timeout=1800,
    s3={"addressing_style": "path"},
)
_TRANSFER_CONFIG = TransferConfig(
    multipart_threshold=8 * 1024 * 1024,
    multipart_chunksize=16 * 1024 * 1024,
    max_concurrency=8,
    use_threads=True,
)
_s3_client = None
_s3_lock = threading.Lock()


def _get_s3_client():
    global _s3_client
    if _s3_client is None:
        with _s3_lock:
            if _s3_client is None:
                _s3_client = boto3.client(
                    "s3",
                    endpoint_url=S3_ENDPOINT,
                    aws_access_key_id=S3_ACCESS_KEY,
                    aws_secret_access_key=S3_SECRET_KEY,
                    region_name=S3_REGION,
                    config=_S3_CONFIG,
                )
    return _s3_client


# ============== ESTADO GLOBAL ==============
START_TIME = time.time()
_shutdown_event = asyncio.Event()
_tg_client = None
_watchdog_task = None
_floodwait_active = False

# Margen de disco que nunca se toca (para que el sistema respire).
DISK_MARGIN = 64 * 1024 * 1024
_DISK_CHECK_STEP = 32 * 1024 * 1024


# ============== UTILS ==============
def format_size(b):
    if b < 1024:
        return f"{b} B"
    if b < 1048576:
        return f"{b / 1024:.1f} KB"
    if b < 1073741824:
        return f"{b / 1048576:.1f} MB"
    return f"{b / 1073741824:.2f} GB"


URL_RE = re.compile(r"(https?://[^\s<>\"']+)", re.IGNORECASE)

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
    "audio/x-wav": ".wav",
    "audio/mp4": ".m4a",
    "audio/aac": ".aac",
    "audio/flac": ".flac",
    "audio/ogg": ".ogg",
    "application/ogg": ".ogg",
}
EXT_TO_CONTENT_TYPE = {ext: ct for ct, ext in CONTENT_TYPE_TO_EXT.items()}


def get_filename_from_url(url):
    try:
        name = os.path.basename(urlparse(url).path)
        name = unquote(name).strip()
        if name and len(name) > 0 and name not in (".", ".."):
            return name
    except Exception:
        pass
    return None


def ext_from_content_type(ct):
    if not ct:
        return None
    base = ct.split(";")[0].strip().lower()
    return CONTENT_TYPE_TO_EXT.get(base)


def guess_content_type(filename):
    ext = os.path.splitext(filename)[1].lower()
    if ext in EXT_TO_CONTENT_TYPE:
        return EXT_TO_CONTENT_TYPE[ext]
    guessed = mimetypes.guess_type(filename)[0]
    return guessed or "application/octet-stream"


def _truncate_utf8(text, max_bytes):
    encoded = text.encode("utf-8")
    if len(encoded) <= max_bytes:
        return text
    return encoded[:max_bytes].decode("utf-8", "ignore")


def sanitize_filename(name, max_bytes=180):
    """Limpia un nombre para usarlo como nombre local y clave S3."""
    name = (name or "").strip().replace("\\", "/")
    name = name.rsplit("/", 1)[-1]
    name = re.sub(r"[\x00-\x1f\x7f]", "", name)
    name = re.sub(r"[\s?#&]+", "_", name)
    name = name.strip("._")
    if not name:
        return f"file_{int(time.time())}"

    stem, ext = os.path.splitext(name)
    if len(ext.encode("utf-8")) > 24:
        stem, ext = name, ""
    max_stem_bytes = max(1, max_bytes - len(ext.encode("utf-8")))
    stem = _truncate_utf8(stem, max_stem_bytes).strip("._")
    if not stem:
        stem = f"file_{int(time.time())}"
    return f"{stem}{ext}"


def _trim_url(url):
    return (url or "").rstrip(".,;:!?)]}\"'")


def _url_for_log(url):
    """Quita query, fragment y credenciales para no filtrar tokens en logs."""
    try:
        p = urlparse(url)
        host = p.hostname or ""
        port = f":{p.port}" if p.port else ""
        return f"{p.scheme}://{host}{port}{p.path}"
    except Exception:
        return "<url>"


def _redact_text(text, limit=80):
    return URL_RE.sub("<url>", text or "")[:limit]


def _size_limit_exceeded(size):
    return MAX_FILE_SIZE > 0 and size > MAX_FILE_SIZE


def _free_bytes():
    st = os.statvfs(DOWNLOAD_PATH)
    return st.f_bavail * st.f_frsize


def check_disk_space(required_bytes=0):
    """Comprueba espacio libre para los bytes que realmente se esperan.

    Antes se exigían 2 * MAX_FILE_SIZE para cualquier archivo, lo que hacía
    imposible descargar incluso un archivo pequeño en un disco de 1 GB.
    """
    free = _free_bytes()
    needed = max(0, required_bytes) + DISK_MARGIN
    if free < needed:
        raise RuntimeError(
            f"Disco insuficiente: {format_size(free)} libres, "
            f"se necesitan {format_size(needed)}"
        )
    return free


def cleanup_paths(paths):
    for p in paths:
        try:
            os.unlink(p)
        except Exception:
            pass


def _is_downloadable_media(message):
    """True solo para media que Telethon puede descargar de verdad.

    MessageMediaWebPage (vista previa de un enlace) NO es un archivo del
    usuario; si se trata como media se sube la miniatura en vez del enlace.
    """
    media = getattr(message, "media", None)
    if not media:
        return False
    if isinstance(media, MessageMediaWebPage):
        return False
    return isinstance(media, (MessageMediaPhoto, MessageMediaDocument))


def _session_is_bot(me):
    """True si la sesión conectada es de un bot (no de una cuenta personal)."""
    return bool(getattr(me, "bot", False))


def _command_of(text):
    """Devuelve el primer token normalizado (/cmd@Bot -> /cmd)."""
    first = (text or "").strip().split(maxsplit=1)[0].lower() if text else ""
    if first.startswith("/") and "@" in first:
        first = first.split("@", 1)[0]
    return first


# ============== SSRF PROTECTION ==============
class SafeRequestError(RuntimeError):
    pass


def _validate_url_shape(url):
    try:
        p = urlparse(url)
    except Exception as e:
        return None, f"URL inválida: {e}"
    if p.scheme not in ("http", "https"):
        return None, f"Esquema no permitido: {p.scheme}"
    try:
        hostname = p.hostname
        port = p.port or (443 if p.scheme == "https" else 80)
    except ValueError as e:
        return None, f"Host o puerto inválido: {e}"
    if not hostname:
        return None, "Sin hostname"
    return (p, hostname, port), None


async def _resolve_host(host, port):
    loop = asyncio.get_running_loop()
    infos = await loop.getaddrinfo(
        host, port, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
    )
    ips = []
    for _family, _type, _proto, _canon, sockaddr in infos:
        raw_ip = str(sockaddr[0]).split("%", 1)[0]
        try:
            ip = ipaddress.ip_address(raw_ip)
        except ValueError:
            continue
        if ip not in ips:
            ips.append(ip)
    return ips


async def _validate_url(url):
    """Valida la URL y devuelve (ok, error, hostname, IPs resueltas).

    Las IPs se usan para fijar el resolver y evitar DNS rebinding entre la
    validación y la conexión real.
    """
    parsed, err = _validate_url_shape(url)
    if err:
        return False, err, None, None

    _p, hostname, port = parsed
    if not SSRF_BLOCK_PRIVATE:
        return True, None, hostname, None

    try:
        ips = await _resolve_host(hostname, port)
    except socket.gaierror as e:
        return False, f"No se pudo resolver el host: {e}", hostname, None
    except Exception as e:
        return False, f"Error resolviendo el host: {e}", hostname, None

    if not ips:
        return False, "El host no resolvió a ninguna IP", hostname, None

    for ip in ips:
        if not ip.is_global:
            return False, f"IP bloqueada (no pública): {ip}", hostname, None
    return True, None, hostname, ips


async def _is_safe_url(url):
    """SSRF protection: bloquea IPs no globales (privadas, loopback, CGNAT...)."""
    ok, err, _hostname, _ips = await _validate_url(url)
    return ok, err


class PinnedResolver(AbstractResolver):
    """Resolver que usa las IPs ya validadas y evita DNS rebinding.

    Si el host no fue fijado, delega en el resolver del sistema.
    """

    def __init__(self):
        self._pins = {}

    def pin(self, hostname, ips):
        if hostname and ips:
            self._pins[hostname.lower()] = list(ips)

    async def resolve(self, host, port=0, family=socket.AF_UNSPEC):
        pins = self._pins.get((host or "").lower())
        if pins:
            records = []
            for ip in pins:
                records.append({
                    "hostname": host,
                    "host": str(ip),
                    "port": port,
                    "family": socket.AF_INET6 if ip.version == 6 else socket.AF_INET,
                    "proto": socket.IPPROTO_TCP,
                    "flags": 0,
                })
            return records

        infos = await asyncio.get_running_loop().getaddrinfo(
            host, port, family=family,
            type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP,
        )
        return [
            {
                "hostname": host,
                "host": info[4][0],
                "port": port,
                "family": info[0],
                "proto": info[2],
                "flags": info[3],
            }
            for info in infos
        ]

    async def close(self):
        return None


_PINNED_RESOLVER = PinnedResolver()


@asynccontextmanager
async def _safe_request(session, method, url, headers=None, max_redirects=5):
    """Petición HTTP que valida la URL y cada redirección antes de seguirla."""
    current = url
    for _ in range(max_redirects + 1):
        ok, err, hostname, ips = await _validate_url(current)
        if not ok:
            raise SafeRequestError(f"URL no permitida: {err}")
        if ips:
            _PINNED_RESOLVER.pin(hostname, ips)

        resp = await session.request(
            method, current, headers=headers, allow_redirects=False
        )
        if resp.status in (301, 302, 303, 307, 308):
            location = resp.headers.get("Location")
            resp.release()
            if not location:
                raise SafeRequestError("Redirección sin cabecera Location")
            current = urljoin(current, location)
            if resp.status == 303 and method != "HEAD":
                method = "GET"
            continue

        try:
            yield resp
        finally:
            resp.release()
        return

    raise SafeRequestError("Demasiadas redirecciones")


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
        self.status_msg = status_msg
        self.chat_id = chat_id
        self.message = message


class UserJobQueue:
    """Cola FIFO por usuario.

    Permite que un álbum (varios mensajes) o varios enlaces seguidos se
    procesen en orden sin responder "ya tienes un trabajo en curso".
    """

    def __init__(self):
        self.active = {}
        self.pending = defaultdict(deque)
        self._lock = asyncio.Lock()
        self._runners = set()

    def has_user_job(self, user_id):
        return user_id in self.active or bool(self.pending.get(user_id))

    def has_any_job(self):
        return bool(self.active)

    def queued_count(self):
        return sum(len(q) for q in self.pending.values())

    def status(self, user_id):
        return (user_id in self.active, len(self.pending.get(user_id, ())))

    async def submit(self, job, coro_factory):
        """Devuelve True si arrancó ya; False si quedó en cola."""
        async with self._lock:
            if job.user_id in self.active or self.pending[job.user_id]:
                self.pending[job.user_id].append((job, coro_factory))
                return False
            self.active[job.user_id] = job

        runner = asyncio.create_task(self._run_user(job.user_id, job, coro_factory))
        self._runners.add(runner)
        runner.add_done_callback(self._runners.discard)
        return True

    async def _run_user(self, user_id, job, coro_factory):
        while True:
            if not job.cancel_requested:
                try:
                    job.task = asyncio.create_task(coro_factory())
                except Exception as exc:
                    log.exception("no se pudo arrancar el job %s: %s", job.job_id, exc)
                    job.task = None
                else:
                    try:
                        # asyncio.wait no propaga la excepción de la tarea hija.
                        await asyncio.wait({job.task})
                    except asyncio.CancelledError:
                        if job.task and not job.task.done():
                            job.task.cancel()
                        raise

                    if job.task.cancelled():
                        log.info("job cancelado por el usuario: %s", job.job_id)
                    else:
                        exc = job.task.exception()
                        if exc is not None:
                            log.error(
                                "job %s terminó con error: %s", job.job_id, exc,
                                exc_info=(type(exc), exc, exc.__traceback__),
                            )
            else:
                await tg_edit_status(job.chat_id, job.status_msg, _render_cancel())

            async with self._lock:
                if self.pending[user_id]:
                    job, coro_factory = self.pending[user_id].popleft()
                    self.active[user_id] = job
                    continue
                self.active.pop(user_id, None)
                self.pending.pop(user_id, None)
                return

    async def cancel_user(self, user_id):
        async with self._lock:
            active_job = self.active.get(user_id)
            pending_jobs = [j for j, _ in self.pending.pop(user_id, ())]
            for job in pending_jobs:
                job.cancel_requested = True
            if active_job is not None:
                active_job.cancel_requested = True
                if active_job.task and not active_job.task.done():
                    active_job.task.cancel()

        for job in pending_jobs:
            await tg_edit_status(job.chat_id, job.status_msg, _render_cancel())

        return active_job is not None or bool(pending_jobs)

    async def cancel_all(self):
        async with self._lock:
            jobs = list(self.active.values())
            for queue in self.pending.values():
                jobs.extend(j for j, _ in queue)
            self.pending.clear()
            for job in jobs:
                job.cancel_requested = True
                if job.task and not job.task.done():
                    job.task.cancel()
        return jobs


job_queue = UserJobQueue()


# ============== UI: TARJETAS ==============
ICON_PROCESSING = "⏳"
ICON_DONE = "✓"
ICON_ERROR = "✗"
ICON_CANCEL = "⊘"
ICON_LINK = "🔗"
ICON_FILE = "📄"


def _render_result_card(name, ext, size, s3_url, source=None):
    lines = [f"{ICON_DONE} Subido a S3"]
    if source:
        lines.append(f"📎 Origen: {source}")
    if ext:
        lines.append(f"{ICON_FILE} {name}.{ext}")
    else:
        lines.append(f"{ICON_FILE} {name}")

    meta_parts = []
    if ext:
        meta_parts.append(ext.upper())
    if size:
        meta_parts.append(format_size(size))
    meta_parts.append("nube")
    lines.append(" · ".join(meta_parts))
    lines.append("")
    lines.append(f"{ICON_LINK} {s3_url}")
    return "\n".join(lines)


def _render_status_progress(step, filename=None, size=None):
    icons = {
        "downloading": f"{ICON_PROCESSING} Descargando",
        "uploading": f"{ICON_PROCESSING} Subiendo",
        "receiving": f"{ICON_PROCESSING} Recibiendo",
    }
    parts = [icons.get(step, f"{ICON_PROCESSING} Procesando")]
    if filename:
        parts.append(filename)
    if size:
        parts.append(f"· {format_size(size)}")
    return " ".join(parts)


def _render_error(msg):
    return f"{ICON_ERROR} {msg}"


def _render_cancel():
    return f"{ICON_CANCEL} Cancelado"


def _render_help(sendfile_enabled=True):
    lines = [
        "🤖 Bot S3 — sube archivos a la nube y te devuelve el enlace",
        "",
        "¿Qué puedo hacer?",
        "  • Envía un enlace http(s)://... y lo subo",
        "  • Reenvía un archivo (foto, video, audio, doc) y lo subo",
        "",
        "Por defecto solo te envío la URL como texto (ahorra datos en toDus).",
        "Si quieres recibir el archivo además de la URL, usa /sendfile.",
        "",
        "Comandos",
        "  /cancel — cancela tu trabajo actual",
        "  /status — muestra si tienes trabajo en curso",
        "  /sendfile — toggle: recibir archivo + URL (gasta datos)",
        "  /textonly — desactivar el envío del archivo (solo URL)",
        "",
        "El bot no impone límite de tamaño; Telegram sí limita cada archivo",
        "(2 GB en bots, 4 GB en cuentas premium).",
    ]
    if not sendfile_enabled:
        lines.append("")
        lines.append("El envío de archivos por Telegram está desactivado por el administrador.")
    return "\n".join(lines)


def _render_sendfile_state(enabled, available=True):
    if not available:
        return (
            f"{ICON_ERROR} El envío de archivos está desactivado por el administrador.\n\n"
            "Solo recibirás la URL S3."
        )
    if enabled:
        return (
            f"{ICON_DONE} Modo archivo activado\n\n"
            "A partir de ahora recibirás el archivo además de la URL."
        )
    return (
        f"{ICON_DONE} Modo archivo desactivado\n\n"
        "Solo recibirás la URL S3 (ahorra datos en toDus)."
    )


def _render_unauthorized():
    return f"{ICON_ERROR} No estás autorizado para usar este bot."


def _render_no_active_job():
    return "No tienes trabajos activos."


def _render_no_url():
    return "Envía un enlace (http/https) o reenvía un archivo para procesar."


def _tipo_label(tipo):
    labels = {
        "imagen": "foto",
        "video": "video",
        "audio": "audio",
        "nota de voz": "nota de voz",
        "archivo": "documento",
    }
    return labels.get(tipo, tipo)


# ============== TELEGRAM HELPERS ==============
async def tg_send_status(chat_id, text):
    global _tg_client
    if _tg_client is None:
        return None
    try:
        msg = await _tg_client.send_message(chat_id, text)
        return msg
    except FloodWaitError as e:
        log.warning("tg_send_status FloodWait %ss", e.seconds)
    except Exception as e:
        log.warning("tg_send_status falló: %s", e)
    return None


async def tg_edit_status(chat_id, msg, text):
    global _tg_client
    if msg is None:
        return await tg_send_status(chat_id, text)
    if _tg_client is None:
        return msg
    try:
        await _tg_client.edit_message(chat_id, msg, text)
        return msg
    except MessageNotModifiedError:
        return msg
    except FloodWaitError as e:
        log.warning("tg_edit_status FloodWait %ss", e.seconds)
        return msg
    except Exception as e:
        log.warning("tg_edit_status falló: %s, enviando nuevo", e)
        return await tg_send_status(chat_id, text)


async def _respond(event, text):
    try:
        msg = await event.respond(text)
        return msg
    except FloodWaitError as e:
        log.warning("respond FloodWait %ss", e.seconds)
    except Exception as e:
        log.warning("respond falló: %s", e)
    return None


async def _send_file_via_telegram(chat_id, file_path, filename, size, caption):
    """Envía un archivo por Telegram con el tipo correcto.

    Devuelve (tipo_str, error_str).
    """
    global _tg_client
    if not SEND_FILE_VIA_TG:
        return None, "envío por TG desactivado"
    if TG_UPLOAD_LIMIT > 0 and size > TG_UPLOAD_LIMIT:
        return None, (
            f"archivo {format_size(size)} supera el límite configurado "
            f"{format_size(TG_UPLOAD_LIMIT)}"
        )
    if _tg_client is None:
        return None, "cliente de Telegram no disponible"

    ext = os.path.splitext(filename)[1].lower()
    try:
        if ext in IMAGE_EXT:
            await _tg_client.send_file(chat_id, file=file_path, caption=caption)
            return "imagen", None
        if ext in VIDEO_EXT:
            await _tg_client.send_file(
                chat_id, file=file_path, caption=caption, supports_streaming=True
            )
            return "video", None
        if ext in VOICE_EXT:
            await _tg_client.send_file(
                chat_id, file=file_path, caption=caption, voice_note=True
            )
            return "nota de voz", None
        if ext in AUDIO_EXT:
            await _tg_client.send_file(chat_id, file=file_path, caption=caption)
            return "audio", None
        await _tg_client.send_file(
            chat_id, file=file_path, caption=caption, force_document=True
        )
        return "archivo", None
    except FloodWaitError as e:
        log.warning("send_file FloodWait %ss", e.seconds)
        return None, f"Telegram pidió esperar {e.seconds}s"
    except Exception as e:
        log.warning("No se pudo enviar archivo a %s: %s", chat_id, e)
        return None, str(e)[:200]


# ============== S3 UPLOAD ==============
def _public_url(key):
    if S3_PUBLIC_BASE_URL:
        return f"{S3_PUBLIC_BASE_URL}/{quote(key)}"
    return f"{S3_ENDPOINT}/{S3_BUCKET}/{quote(key)}"


async def subir_a_s3(temp_path, filename, size):
    safe_name = sanitize_filename(filename)
    remote_key = f"{uuid.uuid4().hex[:8]}_{safe_name}"
    content_type = guess_content_type(safe_name)

    def _do_upload():
        client = _get_s3_client()
        with open(temp_path, "rb") as f:
            client.upload_fileobj(
                f, S3_BUCKET, remote_key,
                ExtraArgs={"ContentType": content_type},
                Config=_TRANSFER_CONFIG,
            )

    await asyncio.wait_for(asyncio.to_thread(_do_upload), timeout=UPLOAD_TIMEOUT)
    log.info("S3 OK %s (%s, %s)", remote_key, content_type, format_size(size))
    return _public_url(remote_key)


# ============== DESCARGA ==============
class RangeNotSupported(RuntimeError):
    pass


def _parse_content_range(value):
    m = re.match(r"bytes\s+(\d+)-(\d+)/(\d+|\*)", value or "")
    if not m:
        return None
    total = None if m.group(3) == "*" else int(m.group(3))
    return int(m.group(1)), int(m.group(2)), total


async def _download_sequential(session, url, temp_path, total, job):
    downloaded = 0
    content_type = None
    next_disk_check = _DISK_CHECK_STEP

    async with aiofiles.open(temp_path, "wb") as f:
        async with _safe_request(session, "GET", url, headers=BROWSER_HEADERS) as resp:
            if resp.status >= 400:
                raise RuntimeError(f"HTTP {resp.status}")
            content_type = resp.headers.get("Content-Type")
            content_encoding = (resp.headers.get("Content-Encoding") or "").lower()
            expected = int(resp.headers.get("Content-Length", 0) or 0) or total

            async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                await f.write(chunk)
                downloaded += len(chunk)
                if _size_limit_exceeded(downloaded):
                    raise RuntimeError(
                        f"Archivo supera el límite de {format_size(MAX_FILE_SIZE)}"
                    )
                if downloaded >= next_disk_check:
                    if _free_bytes() < DISK_MARGIN:
                        raise RuntimeError("Disco lleno durante la descarga")
                    next_disk_check = downloaded + _DISK_CHECK_STEP

    if expected and content_encoding in ("", "identity") and downloaded != expected:
        raise RuntimeError(
            f"Descarga incompleta: {format_size(downloaded)} de {format_size(expected)}"
        )
    return downloaded, content_type


async def _download_parallel(session, url, temp_path, total, job):
    """Descarga por rangos con cancelación limpia de todas las partes."""
    if total <= 0:
        raise RangeNotSupported("Tamaño desconocido")

    n_parts = min(PARALLEL_URL_PARTS, max(1, total // (10 * 1024 * 1024)))
    part_size = total // n_parts
    parts = []
    for i in range(n_parts):
        start = i * part_size
        end = start + part_size - 1 if i < n_parts - 1 else total - 1
        parts.append((i, start, end))

    check_disk_space(total * 2)

    part_paths = [None] * n_parts
    all_part_paths = [f"{temp_path}.part{i}" for i in range(n_parts)]
    content_type = None

    async def download_part(idx, start, end):
        nonlocal content_type
        path = f"{temp_path}.part{idx}"
        job.temp_paths.append(path)
        headers = dict(BROWSER_HEADERS)
        headers["Range"] = f"bytes={start}-{end}"
        expected_len = end - start + 1

        async with _safe_request(session, "GET", url, headers=headers) as resp:
            if resp.status != 206:
                raise RangeNotSupported(
                    f"Servidor no soporta Range (HTTP {resp.status} en parte {idx})"
                )
            encoding = (resp.headers.get("Content-Encoding") or "").lower()
            if encoding not in ("", "identity"):
                raise RangeNotSupported("El servidor comprimió una respuesta Range")

            parsed = _parse_content_range(resp.headers.get("Content-Range", ""))
            if parsed is None:
                raise RangeNotSupported("Respuesta 206 sin Content-Range válido")
            actual_start, actual_end, remote_total = parsed
            if actual_start != start or actual_end != end:
                raise RangeNotSupported(
                    f"Range no respetado en parte {idx}: "
                    f"{actual_start}-{actual_end}, esperado {start}-{end}"
                )
            if remote_total is not None and remote_total != total:
                raise RangeNotSupported(
                    f"El archivo cambió de tamaño ({remote_total} != {total})"
                )

            if not content_type:
                content_type = resp.headers.get("Content-Type")

            received = 0
            async with aiofiles.open(path, "wb") as f:
                async for chunk in resp.content.iter_chunked(CHUNK_SIZE):
                    await f.write(chunk)
                    received += len(chunk)
                    if received > expected_len:
                        raise RuntimeError(f"Parte {idx} excede el tamaño esperado")

        if received != expected_len:
            raise RuntimeError(
                f"Parte {idx} incompleta: {format_size(received)} "
                f"de {format_size(expected_len)}"
            )
        part_paths[idx] = path
        return path

    tasks = [
        asyncio.create_task(download_part(i, s, e))
        for i, s, e in parts
    ]
    try:
        try:
            await asyncio.gather(*tasks)
        except BaseException:
            # gather no cancela a las hermanas; hay que hacerlo y esperarlas
            # para que no sigan escribiendo .part después de la limpieza.
            for task in tasks:
                if not task.done():
                    task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            raise

        async with aiofiles.open(temp_path, "wb") as out:
            for path in part_paths:
                if not path:
                    raise RuntimeError("Parte faltante")
                async with aiofiles.open(path, "rb") as src:
                    while True:
                        chunk = await src.read(8 * 1024 * 1024)
                        if not chunk:
                            break
                        await out.write(chunk)
    finally:
        # Se limpia tanto en éxito como en fallo/cancelación.
        cleanup_paths(all_part_paths)

    final_size = os.path.getsize(temp_path)
    if final_size != total:
        raise RuntimeError(
            f"Descarga incompleta: {format_size(final_size)} de {format_size(total)}"
        )
    return total, content_type


# ============== PROCESAMIENTO ==============
async def _deliver(job, temp_path, filename, source):
    """Sube a S3 y entrega la URL (y el archivo si el usuario lo pidió)."""
    chat_id = job.chat_id
    status_msg = job.status_msg
    size = os.path.getsize(temp_path)

    await tg_edit_status(
        chat_id, status_msg,
        _render_status_progress("uploading", filename, size),
    )

    url_final = await subir_a_s3(temp_path, filename, size)

    stem = os.path.splitext(filename)[0]
    name = re.sub(r"^[0-9a-f]{8}_", "", stem).replace("_", " ")
    ext_out = os.path.splitext(filename)[1].lstrip(".").lower()

    card = _render_result_card(
        name=name, ext=ext_out, size=size, s3_url=url_final, source=source
    )

    send_file = (job.user_id in _sendfile_users) and SEND_FILE_VIA_TG
    if not send_file:
        await tg_edit_status(chat_id, status_msg, card)
        log.info("OK subido a S3 (textonly) para %s: %s", chat_id, filename)
        return

    tipo, send_err = await _send_file_via_telegram(
        chat_id, temp_path, filename, size, card
    )
    if tipo:
        log.info("OK enviado %s a %s: %s", tipo, chat_id, filename)
        await tg_edit_status(
            chat_id, status_msg,
            f"{ICON_DONE} Enviado como {_tipo_label(tipo)} · {format_size(size)}",
        )
    else:
        err_msg = f"No se pudo enviar como media: {send_err or 'desconocido'}"
        await tg_edit_status(
            chat_id, status_msg, card + f"\n\n{_render_error(err_msg)}"
        )


async def process_job(job):
    if job.kind == "tg_media":
        await _process_tg_media(job)
    else:
        await _process_url(job)


async def _process_tg_media(job):
    global _tg_client
    chat_id = job.chat_id
    status_msg = job.status_msg
    message = job.message

    if not message or not _is_downloadable_media(message):
        await tg_edit_status(
            chat_id, status_msg, _render_error("No hay media para procesar.")
        )
        return

    try:
        source_label = None
        fwd_from = getattr(message, "fwd_from", None)
        if fwd_from:
            from_name = getattr(fwd_from, "from_name", None)
            from_id = getattr(fwd_from, "from_id", None)
            if from_name:
                source_label = f"canal {from_name}"
            elif from_id:
                peer_type = type(from_id).__name__.replace("Peer", "").lower()
                peer_id = (
                    getattr(from_id, "user_id", None)
                    or getattr(from_id, "channel_id", None)
                    or getattr(from_id, "chat_id", None)
                )
                if peer_id:
                    source_label = f"{peer_type} {peer_id}"

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
            filename = f"tg_{uuid.uuid4().hex[:8]}.jpg"

        if _size_limit_exceeded(size_hint):
            await tg_edit_status(
                chat_id, status_msg,
                _render_error(
                    f"Archivo de {format_size(size_hint)} — supera el límite "
                    f"de {format_size(MAX_FILE_SIZE)}."
                ),
            )
            return

        safe_name = sanitize_filename(filename)
        ext = os.path.splitext(safe_name)[1] or ".bin"
        temp_path = os.path.join(DOWNLOAD_PATH, f"{uuid.uuid4().hex}{ext}")
        job.temp_paths.append(temp_path)

        check_disk_space(size_hint)

        await tg_edit_status(
            chat_id, status_msg,
            _render_status_progress("receiving", safe_name, size_hint),
        )

        last_disk_check = [0]

        def _progress(received, _total):
            if received - last_disk_check[0] >= _DISK_CHECK_STEP:
                last_disk_check[0] = received
                if _free_bytes() < DISK_MARGIN:
                    raise RuntimeError("Disco lleno durante la descarga")

        try:
            async with asyncio.timeout(DOWNLOAD_TIMEOUT):
                downloaded_path = await _tg_client.download_media(
                    message=message, file=temp_path,
                    progress_callback=_progress,
                )
        except asyncio.TimeoutError as e:
            raise RuntimeError(
                "Tiempo agotado descargando desde Telegram"
            ) from e
        except Exception as e:
            raise RuntimeError(f"Error descargando desde Telegram: {e}") from e

        if downloaded_path and os.path.exists(downloaded_path):
            if downloaded_path != temp_path:
                job.temp_paths.append(downloaded_path)
            temp_path = downloaded_path

        if not os.path.exists(temp_path):
            raise RuntimeError("La descarga no produjo ningún archivo")

        size = os.path.getsize(temp_path)
        if _size_limit_exceeded(size):
            raise RuntimeError(
                f"Archivo descargado ({format_size(size)}) supera el límite"
            )

        await _deliver(job, temp_path, safe_name, source_label)

    except asyncio.CancelledError:
        try:
            await tg_edit_status(chat_id, status_msg, _render_cancel())
        except Exception:
            pass
        raise
    except Exception as e:
        log.exception("error procesando media de Telegram")
        try:
            await tg_edit_status(chat_id, status_msg, _render_error(str(e)[:200]))
        except Exception:
            pass
    finally:
        cleanup_paths(job.temp_paths)
        job.temp_paths.clear()


async def _process_url(job):
    url = job.url
    chat_id = job.chat_id
    status_msg = job.status_msg

    safe, err = await _is_safe_url(url)
    if not safe:
        log.warning("SSRF bloqueado: %s - %s", _url_for_log(url), err)
        await tg_edit_status(
            chat_id, status_msg, _render_error(f"URL no permitida: {err}")
        )
        return

    raw_name = get_filename_from_url(url)
    if raw_name:
        filename = sanitize_filename(raw_name)
    else:
        filename = sanitize_filename(f"file_{int(time.time())}")
    ext = os.path.splitext(filename)[1] or ".bin"
    temp_path = os.path.join(DOWNLOAD_PATH, f"{uuid.uuid4().hex}{ext}")
    job.temp_paths.append(temp_path)

    try:
        await tg_edit_status(
            chat_id, status_msg,
            _render_status_progress("downloading", filename),
        )

        timeout = aiohttp.ClientTimeout(total=None, connect=30, sock_read=300)
        connector = aiohttp.TCPConnector(
            resolver=_PINNED_RESOLVER, use_dns_cache=False
        )
        async with aiohttp.ClientSession(
            timeout=timeout, connector=connector
        ) as session:
            async with asyncio.timeout(DOWNLOAD_TIMEOUT):
                total = 0
                accepts_ranges = False
                try:
                    async with _safe_request(
                        session, "HEAD", url, headers=BROWSER_HEADERS
                    ) as head:
                        if head.status < 400:
                            total = int(head.headers.get("Content-Length", 0) or 0)
                            accepts_ranges = (
                                head.headers.get("Accept-Ranges", "").lower() == "bytes"
                            )
                except SafeRequestError:
                    raise
                except Exception:
                    pass

                if _size_limit_exceeded(total):
                    raise RuntimeError(
                        f"Archivo de {format_size(total)} — supera el límite "
                        f"de {format_size(MAX_FILE_SIZE)}"
                    )

                use_parallel = (
                    PARALLEL_URL_DOWNLOAD
                    and accepts_ranges
                    and total >= PARALLEL_URL_MIN_SIZE
                    and total > 0
                )
                if use_parallel:
                    try:
                        check_disk_space(total * 2)
                    except RuntimeError as e:
                        log.info("Sin espacio para descarga paralela: %s", e)
                        use_parallel = False

                if not use_parallel:
                    check_disk_space(total)

                size_downloaded = 0
                content_type = None

                if use_parallel:
                    try:
                        size_downloaded, content_type = await _download_parallel(
                            session, url, temp_path, total, job
                        )
                    except RangeNotSupported as e:
                        log.info("Cayendo a descarga secuencial: %s", e)
                        size_downloaded, content_type = await _download_sequential(
                            session, url, temp_path, total, job
                        )
                else:
                    size_downloaded, content_type = await _download_sequential(
                        session, url, temp_path, total, job
                    )

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
                except Exception:
                    pass

        size = os.path.getsize(temp_path)
        if _size_limit_exceeded(size):
            raise RuntimeError(
                f"Archivo descargado ({format_size(size)}) supera el límite"
            )

        await _deliver(job, temp_path, filename, "web")

    except asyncio.CancelledError:
        try:
            await tg_edit_status(chat_id, status_msg, _render_cancel())
        except Exception:
            pass
        raise
    except SafeRequestError as e:
        log.warning("URL bloqueada: %s", e)
        try:
            await tg_edit_status(chat_id, status_msg, _render_error(str(e)[:200]))
        except Exception:
            pass
    except Exception as e:
        log.exception("error procesando URL")
        try:
            await tg_edit_status(chat_id, status_msg, _render_error(str(e)[:200]))
        except Exception:
            pass
    finally:
        cleanup_paths(job.temp_paths)
        job.temp_paths.clear()


# ============== TELEGRAM EVENT HANDLERS ==============
_sendfile_users = set()


async def on_tg_message(event):
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

        log.info("📩 de %s (%s): %s", sender_name, sender_id, _redact_text(body))

        if ALLOWED_TG_IDS is not None and sender_id not in ALLOWED_TG_IDS:
            log.warning("Usuario no autorizado: %s", sender_id)
            await _respond(event, _render_unauthorized())
            return

        low = body.lower()
        command = _command_of(low)

        if command in ("/start", "/help") or low in ("hola", "ayuda", "help"):
            await _respond(
                event,
                _render_help(sendfile_enabled=SEND_FILE_VIA_TG),
            )
            return

        if command in ("/cancel", "/cancelar") or low == "cancelar":
            cancelled = await job_queue.cancel_user(sender_id)
            await _respond(
                event,
                _render_cancel() if cancelled else _render_no_active_job(),
            )
            return

        if command == "/status":
            active, queued = job_queue.status(sender_id)
            if active:
                text = f"{ICON_PROCESSING} Tienes un trabajo en curso."
                if queued:
                    text += f"\n{queued} en cola."
            elif queued:
                text = f"{ICON_PROCESSING} {queued} trabajo(s) en cola."
            else:
                text = "✓ Sin trabajos activos."
            await _respond(event, text)
            return

        if command in ("/sendfile", "/textonly"):
            if not SEND_FILE_VIA_TG:
                await _respond(
                    event, _render_sendfile_state(False, available=False)
                )
                return
            # /textonly siempre desactiva (idempotente); /sendfile alterna.
            if command == "/textonly" or sender_id in _sendfile_users:
                _sendfile_users.discard(sender_id)
                await _respond(event, _render_sendfile_state(False))
            else:
                _sendfile_users.add(sender_id)
                await _respond(event, _render_sendfile_state(True))
            return

        # ===== MEDIA REENVIADA =====
        if _is_downloadable_media(event.message):
            status_msg = await tg_send_status(
                chat_id, f"{ICON_PROCESSING} Procesando..."
            )
            job = QueuedJob(
                user_id=sender_id, kind="tg_media", url=None,
                original_name=None, chat_id=chat_id, status_msg=status_msg,
            )
            job.message = event.message

            async def _factory():
                return await process_job(job)

            await job_queue.submit(job, _factory)
            return

        # ===== URL =====
        if not body:
            await _respond(event, _render_no_url())
            return

        match = URL_RE.search(body)
        if not match:
            await _respond(event, _render_no_url())
            return

        url = _trim_url(match.group(1))
        status_msg = await tg_send_status(
            chat_id, f"{ICON_PROCESSING} Procesando..."
        )
        job = QueuedJob(
            user_id=sender_id, kind="url", url=url,
            chat_id=chat_id, status_msg=status_msg,
        )

        async def _factory():
            return await process_job(job)

        await job_queue.submit(job, _factory)

    except Exception as e:
        log.exception("on_tg_message: %s", e)


# ============== WEB SERVER (healthcheck) ==============
async def health_handler(request):
    return web.json_response({
        "status": "healthy",
        "uptime": round(time.time() - START_TIME, 1),
        "active_jobs": len(job_queue.active),
        "queued_jobs": job_queue.queued_count(),
        "watchdog_enabled": WATCHDOG_ENABLED,
        "tg_connected": _tg_client is not None and _tg_client.is_connected(),
    })


async def ready_handler(request):
    connected = _tg_client is not None and _tg_client.is_connected()
    return web.json_response(
        {
            "ready": connected,
            "tg_connected": connected,
            "active_jobs": len(job_queue.active),
            "queued_jobs": job_queue.queued_count(),
        },
        status=200 if connected else 503,
    )


async def root_handler(request):
    return web.json_response({
        "status": "online",
        "service": "telegram-s3-bot",
    })


def make_web_app():
    app = web.Application()
    app.router.add_get("/", root_handler)
    app.router.add_get("/health", health_handler)
    app.router.add_get("/healthz", health_handler)
    app.router.add_get("/ready", ready_handler)
    return app


async def run_web():
    app = make_web_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    log.info("🌐 Web server escuchando en 0.0.0.0:%s", PORT)
    return runner


# ============== WATCHDOG + SHUTDOWN ==============
async def watchdog():
    deferred_since = None
    while not _shutdown_event.is_set():
        await asyncio.sleep(30)
        if not WATCHDOG_ENABLED:
            continue
        if _floodwait_active:
            # No reiniciar durante un FloodWait: empeoraría el bloqueo.
            continue
        uptime = time.time() - START_TIME
        if uptime <= WATCHDOG_LIFETIME:
            continue

        if job_queue.has_any_job():
            if deferred_since is None:
                deferred_since = time.time()
                log.info("watchdog: hay trabajos activos, posponiendo reinicio")
            if time.time() - deferred_since < 3600:
                continue
            log.warning("watchdog: trabajos activos > 1 h, reiniciando igualmente")

        log.warning("watchdog: %.0fs de uptime, iniciando shutdown graceful", uptime)
        _shutdown_event.set()
        await asyncio.sleep(5)
        os.kill(os.getpid(), signal.SIGTERM)
        return


def _setup_signal_handlers():
    def handler(signum, frame):
        log.info("Señal %s recibida, iniciando shutdown graceful...", signum)
        _shutdown_event.set()

        def _force_exit():
            time.sleep(10)
            log.warning("Forzando os._exit(0)")
            os._exit(0)

        threading.Thread(target=_force_exit, daemon=True).start()

    signal.signal(signal.SIGTERM, handler)
    signal.signal(signal.SIGINT, handler)


# ============== FLOODWAIT ==============
async def _handle_floodwait(e: FloodWaitError):
    """Espera sin bloquear el event loop (el healthcheck sigue respondiendo)."""
    global _floodwait_active
    wait = max(0, int(e.seconds))
    sleep_total = wait + 60
    log.error(
        "🚫 FloodWait de Telegram: hay que esperar %ss (%.1f min). "
        "No es un bug: Telegram limita los logins repetidos.",
        wait, wait / 60,
    )
    _floodwait_active = True
    try:
        await asyncio.wait_for(_shutdown_event.wait(), timeout=sleep_total)
        return
    except asyncio.TimeoutError:
        pass
    finally:
        _floodwait_active = False
    log.warning("FloodWait cumplido; saliendo para que el servicio reinicie")
    raise SystemExit(1)


# ============== MAIN ==============
async def main():
    global _tg_client, _watchdog_task

    if not TG_API_ID or not TG_API_HASH:
        log.error(
            "❌ Falta TG_API_ID o TG_API_HASH. Obténlos en "
            "https://my.telegram.org/apps"
        )
        raise SystemExit(1)
    if not TG_SESSION_STRING and not TG_BOT_TOKEN:
        log.error(
            "❌ Falta TG_BOT_TOKEN o TG_SESSION_STRING. "
            "Necesitas uno de los dos para arrancar."
        )
        raise SystemExit(1)

    _setup_signal_handlers()
    web_runner = await run_web()
    if WATCHDOG_ENABLED:
        _watchdog_task = asyncio.create_task(watchdog())

    try:
        session_path = os.path.join(DOWNLOAD_PATH, "tg_s3_bot.session")

        if TG_SESSION_STRING:
            log.info("🔗 Usando TG_SESSION_STRING de bot (sin re-login)")
            _tg_client = TelegramClient(
                StringSession(TG_SESSION_STRING),
                api_id=TG_API_ID,
                api_hash=TG_API_HASH,
            )
            try:
                await _tg_client.start()
            except FloodWaitError as e:
                await _handle_floodwait(e)
                return
        else:
            log.info("🤖 Iniciando Telethon en modo BOT (sesión: %s)", session_path)
            _tg_client = TelegramClient(
                session_path,
                api_id=TG_API_ID,
                api_hash=TG_API_HASH,
            )
            try:
                await _tg_client.start(bot_token=TG_BOT_TOKEN)
            except FloodWaitError as e:
                await _handle_floodwait(e)
                return
            log.info(
                "Para evitar re-logins, genera TG_SESSION_STRING localmente con "
                "scripts/generate_bot_session.py y configúralo como variable."
            )

        try:
            me = await _tg_client.get_me()
        except FloodWaitError as e:
            await _handle_floodwait(e)
            return

        me_name = (
            getattr(me, "username", None)
            or getattr(me, "first_name", None)
            or str(me.id)
        )
        # Solo modo bot: una sesión de cuenta personal abre la cuenta entera a
        # cualquiera que use el bot, así que se rechaza explícitamente.
        if not _session_is_bot(me):
            log.error(
                "❌ La sesión configurada es de una cuenta personal (id=%s), no "
                "de un bot. Este bot solo funciona en modo bot: usa "
                "TG_BOT_TOKEN o un TG_SESSION_STRING generado con "
                "scripts/generate_bot_session.py.",
                me.id,
            )
            raise SystemExit(1)

        log.info("✅ Telegram conectado como bot: %s (id=%s)", me_name, me.id)

        _tg_client.add_event_handler(
            on_tg_message, events.NewMessage(incoming=True)
        )

        if ALLOWED_TG_IDS is not None:
            log.info("Allowlist activa: %s IDs", len(ALLOWED_TG_IDS))
        if SSRF_BLOCK_PRIVATE:
            log.info("Protección SSRF activada (redirecciones incluidas)")
        limit = format_size(MAX_FILE_SIZE) if MAX_FILE_SIZE > 0 else "sin límite"
        log.info("BOT READY — tamaño máximo: %s", limit)

        await _shutdown_event.wait()

    finally:
        log.info("apagando limpiamente...")
        await job_queue.cancel_all()
        if _watchdog_task and not _watchdog_task.done():
            _watchdog_task.cancel()
        if _tg_client is not None:
            try:
                await _tg_client.disconnect()
            except Exception:
                pass
        await web_runner.cleanup()
        log.info("apagado completo")


if __name__ == "__main__":
    asyncio.run(main())
