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
import logging
import threading
import socket
import ipaddress
from urllib.parse import urlparse, unquote, quote

import aiofiles
import aiohttp
import aioboto3
from aiohttp import web
from botocore import UNSIGNED
from botocore.config import Config as BotoConfig
from boto3.s3.transfer import TransferConfig

# Telethon: cliente de Telegram
from telethon import TelegramClient, events
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
SEND_FILE_VIA_TG = os.environ.get("SEND_FILE_VIA_TG", "1") == "1"

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
_s3_session = aioboto3.Session()

_loop = None
_shutdown_event = asyncio.Event()
_tg_client = None


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
        "status_msg", "chat_id",
    )

    def __init__(self, user_id, kind, url=None, original_name=None,
                 chat_id=None, status_msg=None):
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
async def subir_a_s3(temp_path, filename, size):
    safe_name = sanitize_filename(filename)
    remote_key = f"{uuid.uuid4().hex[:8]}_{safe_name}"
    async with _s3_session.client(
        "s3",
        endpoint_url=S3_ENDPOINT,
        aws_access_key_id=S3_ACCESS_KEY,
        aws_secret_access_key=S3_SECRET_KEY,
        region_name=S3_REGION,
        config=_S3_CONFIG,
    ) as s3:
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
                    f, S3_BUCKET, remote_key,
                    ExtraArgs={"ContentType": "application/octet-stream"},
                    Config=_TRANSFER_CONFIG,
                )
        await asyncio.wait_for(
            asyncio.to_thread(_do_upload),
            timeout=1800,
        )
    return f"{S3_ENDPOINT}/{S3_BUCKET}/{quote(remote_key)}"


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


# ============== MAIN PROCESSOR ==============
async def process_job(job):
    await _process_url(job)


async def _process_url(job):
    url = job.url
    chat_id = job.chat_id
    status_msg = job.status_msg

    # Validación SSRF
    safe, err = _is_safe_url(url)
    if not safe:
        log.warning(f"SSRF bloqueado: {url} - {err}")
        await tg_edit_status(chat_id, status_msg, f"❌ URL no permitida: {err}")
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
        status_msg = await tg_send_status(
            chat_id, f"📥 Descargando `{filename}`..."
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
                        f"Archivo {format_size(total)} supera el límite"
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
                # Renombrar archivo temporal
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

        status_msg = await tg_edit_status(
            chat_id, status_msg,
            f"☁️ Subiendo a S3...\n📊 `{format_size(size)}`"
        )

        url_final = await subir_a_s3(temp_path, filename, size)

        name = re.sub(
            r"^[0-9a-f]{8}_", "",
            os.path.splitext(filename)[0]
        ).replace("_", " ")
        ext_out = os.path.splitext(filename)[1].replace(".", "")

        # Caption SIEMPRE con la URL S3 visible
        caption = (
            f"┎ **NAME:** {name}\n"
            f"┠ **EXTENSION:** {ext_out}\n"
            f"┠ **SIZE:** {format_size(size)}\n"
            f"┠ **CLOUD:** S3\n"
            f"┖ **URL:** {url_final}"
        )

        tipo, send_err = await _send_file_via_telegram(
            chat_id, temp_path, filename, size, caption
        )

        # ===== FIX PRINCIPAL: SIEMPRE mostrar URL S3 =====
        if tipo:
            log.info(f"✅ Enviado {tipo} a {chat_id}: {filename}")
            # El caption ya se envió con el archivo. Editar el status_msg final.
            await tg_edit_status(
                chat_id, status_msg,
                f"✅ Enviado como **{tipo}**\n"
                f"📊 `{format_size(size)}` • `{ext_out}`\n"
                f"☁️ S3 URL: {url_final}"
            )
        else:
            # No se pudo enviar como media → enviar solo el caption con la URL
            await tg_edit_status(
                chat_id, status_msg,
                f"⚠️ No se pudo enviar como media: `{send_err or 'desconocido'}`\n\n"
                f"┎ **NAME:** {name}\n"
                f"┠ **EXTENSION:** {ext_out}\n"
                f"┠ **SIZE:** {format_size(size)}\n"
                f"┠ **CLOUD:** S3\n"
                f"┖ **URL:** {url_final}"
            )

    except asyncio.CancelledError:
        try:
            await tg_edit_status(chat_id, status_msg, "❌ Cancelado")
        except Exception:
            pass
        raise
    except Exception as e:
        log.exception("error procesando URL")
        try:
            await tg_edit_status(
                chat_id, status_msg,
                f"❌ Error: `{str(e)[:200]}`"
            )
        except Exception:
            pass
    finally:
        cleanup_paths(job.temp_paths)
        job.temp_paths.clear()


# ============== TELEGRAM EVENT HANDLERS ==============
async def on_tg_message(event):
    """Handler de mensajes entrantes de Telegram (Telethon)."""
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
        if not body:
            return

        log.info(f"📩 de {sender_name} ({sender_id}): {body[:80]}")

        # Verificar allowlist
        if ALLOWED_TG_IDS is not None and sender_id not in ALLOWED_TG_IDS:
            log.warning(f"Usuario no autorizado: {sender_id}")
            await event.respond("⛔ No estás autorizado para usar este bot.")
            return

        low = body.lower()

        if low in ("/start", "hola", "ayuda", "help", "/help"):
            await event.respond(
                "🤖 **Bot de subida a S3**\n\n"
                "Envíame un enlace (http/https) y lo descargaré, "
                "lo subiré a S3 y te devolveré el archivo + URL.\n\n"
                "**Comandos:**\n"
                "/cancel — cancelar tu trabajo\n"
                "/status — ver estado de tu trabajo"
            )
            return

        if low in ("/cancel", "cancelar"):
            result = await job_queue.cancel_user(sender_id)
            if result == "none":
                await event.respond("ℹ️ No tienes trabajos activos.")
            else:
                await event.respond("❌ Cancelado.")
            return

        if low == "/status":
            active = job_queue.has_user_job(sender_id)
            await event.respond(
                "🔄 Tienes un trabajo en curso." if active
                else "✅ Sin trabajos activos."
            )
            return

        match = URL_RE.search(body)
        if not match:
            await event.respond("Envía un enlace (http/https) para procesar.")
            return

        if job_queue.has_user_job(sender_id):
            await event.respond(
                "⚠️ Ya tienes un trabajo en curso. Usa /cancel para cancelarlo."
            )
            return

        url = match.group(1)
        # Enviar status inicial primero, luego crear job con ese mensaje
        status_msg = await tg_send_status(
            chat_id, f"📥 Descargando...\n🔗 `{url[:80]}`"
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
                await event.respond("⚠️ Ya tienes un trabajo en curso.")
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


# ============== MAIN ==============
async def main():
    global _loop, _tg_client
    _loop = asyncio.get_running_loop()

    # Validar config de Telegram
    if not TG_API_ID or not TG_API_HASH:
        log.error("❌ Falta TG_API_ID o TG_API_HASH. Obténlos en https://my.telegram.org/apps")
        return

    _setup_signal_handlers()

    web_runner = await run_web()
    asyncio.create_task(watchdog())

    # --- Inicializar Telethon ---
    if TG_BOT_TOKEN:
        # Modo bot oficial
        log.info("🤖 Iniciando Telethon en modo BOT (con BOT_TOKEN)")
        _tg_client = TelegramClient(
            "tg_s3_bot",
            api_id=TG_API_ID,
            api_hash=TG_API_HASH,
        )
        await _tg_client.start(bot_token=TG_BOT_TOKEN)
    elif TG_SESSION_STRING:
        # Modo userbot (sesión pre-generada)
        log.info("👤 Iniciando Telethon en modo USERBOT (con SESSION_STRING)")
        _tg_client = TelegramClient(
            "tg_s3_bot",
            api_id=TG_API_ID,
            api_hash=TG_API_HASH,
            session=TG_SESSION_STRING,
        )
        await _tg_client.start()
    else:
        log.error(
            "❌ Falta TG_BOT_TOKEN o TG_SESSION_STRING. "
            "Necesitas uno de los dos para arrancar."
        )
        return

    me = await _tg_client.get_me()
    me_name = getattr(me, "username", None) or getattr(me, "first_name", None) or str(me.id)
    log.info(f"✅ Telegram conectado como: {me_name} (id={me.id})")

    # Registrar handler (callback, event)
    # En Telethon: client.add_handler(callback, event_type)
    # events.NewMessage(incoming=True) filtra mensajes salientes automáticamente.
    _tg_client.add_handler(
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
