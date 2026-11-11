#!/usr/bin/env python3
"""
Descargador UDYAT para enlaces de s3.todus.cu generados por TGS3BOT.

Usa únicamente la biblioteca estándar de Python (sin pip install), ideal
para Termux (Android), Linux, macOS y Windows.

Cómo funciona:
  En lugar de resolver DNS hacia s3.todus.cu (que puede ir por una IP
  facturable), conecta directamente por IP a la dirección interna de
  toDus (por defecto 152.207.206.86) enviando:
    - Host: s3.todus.cu
    - User-Agent: Wget/1.21.3
    - SSL sin verificación de hostname (certificado self-signed de la IP)

  Como envía `Host: s3.todus.cu`, funciona tanto con enlaces anónimos
  (`/stream/hi/...`) como con enlaces oficiales firmados (`?X-Amz-Signature=...`).

Aviso importante (Cuba / ETECSA):
  Que la descarga salga a 0 MB depende exclusivamente de las reglas
  vigentes de ETECSA sobre esa IP en datos móviles (no en Wi-Fi).
  Haz una primera prueba con un archivo pequeño (< 1 MB) y comprueba tu
  saldo antes y después.

Uso rápido:
  python scripts/udyat_download.py "https://s3.todus.cu/..."
  python scripts/udyat_download.py enlaces_20261106_1200.txt -d ~/storage/downloads
  python scripts/udyat_download.py --test
"""

from __future__ import annotations

import argparse
import concurrent.futures
import http.client
import mimetypes
import os
import re
import ssl
import sys
import threading
import time
from urllib.parse import parse_qs, unquote, urljoin, urlsplit


DEFAULT_FREE_IP = os.environ.get("UDYAT_FREE_IP", "152.207.206.86").strip() or "152.207.206.86"
BACKUP_FREE_IP = "152.207.206.88"
DEFAULT_UA = "Wget/1.21.3"
TODUS_S3_HOSTS = {"s3.todus.cu", "152.207.206.86", "152.207.206.88"}
CHUNK_SIZE = 256 * 1024
RETRYABLE_STATUSES = {408, 425, 429}
CONTENT_RANGE_RE = re.compile(
    r"^\s*bytes\s+(?:(\d+)-(\d+)|\*)/(\d+|\*)\s*$",
    re.IGNORECASE,
)
_CT_TO_EXT = {
    "video/mp4": ".mp4",
    "video/x-matroska": ".mkv",
    "video/webm": ".webm",
    "video/quicktime": ".mov",
    "audio/mpeg": ".mp3",
    "audio/mp4": ".m4a",
    "audio/ogg": ".ogg",
    "audio/opus": ".opus",
    "image/jpeg": ".jpg",
    "image/png": ".png",
    "image/webp": ".webp",
    "application/pdf": ".pdf",
    "application/zip": ".zip",
    "application/vnd.android.package-archive": ".apk",
}


class DownloadError(RuntimeError):
    """Error definitivo en la descarga."""


class _RetryableError(RuntimeError):
    """Error transitorio: conviene reintentar manteniendo los bytes buenos."""


class _RestartError(RuntimeError):
    """Los bytes parciales no sirven: hay que reiniciar desde 0."""


def format_size(n: int) -> str:
    if n < 1024:
        return f"{n} B"
    if n < 1048576:
        return f"{n / 1024:.1f} KB"
    if n < 1073741824:
        return f"{n / 1048576:.1f} MB"
    return f"{n / 1073741824:.2f} GB"


def format_eta(seconds: float) -> str:
    if seconds <= 0 or seconds == float("inf"):
        return "--:--"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, sec = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    return f"{m:02d}:{sec:02d}"


def sanitize_filename(name: str, fallback: str = "") -> str:
    if not name:
        return fallback or f"todus_{int(time.time())}.bin"
    clean = name.replace("\\", "/")
    clean = os.path.basename(clean)
    clean = re.sub(r'[<>:"/|?*\x00-\x1f]+', "_", clean).strip(" .")
    if clean in ("", ".", ".."):
        return fallback or f"todus_{int(time.time())}.bin"
    if len(clean) > 200:
        stem, ext = os.path.splitext(clean)
        keep = max(20, 200 - len(ext))
        clean = (stem[:keep].rstrip(" .") or "archivo") + ext
    return clean


def parse_url_and_hint(raw_url: str) -> tuple[str, str | None]:
    """Separa el fragmento `#filename=...` (usado por enlaces.txt de TGS3BOT).

    Devuelve `(url_sin_fragmento, nombre_sugerido_o_None)`.
    """
    raw_url = raw_url.strip()
    sp = urlsplit(raw_url)
    hint = None
    if sp.fragment:
        frag = sp.fragment
        if frag.startswith("filename="):
            hint = unquote(frag[len("filename="):].split("&", 1)[0])
        else:
            qs = parse_qs(frag, keep_blank_values=False)
            if "filename" in qs and qs["filename"]:
                hint = qs["filename"][0]
    clean_url = sp._replace(fragment="").geturl()
    return clean_url, sanitize_filename(hint, "") if hint else None


def filename_from_content_disposition(header_val: str | None) -> str | None:
    if not header_val:
        return None
    # RFC 5987: filename*=UTF-8''nombre%20real.mp4
    m_ext = re.search(
        r"filename\*\s*=\s*(?:UTF-8|utf-8)?''([^;\r\n]+)",
        header_val,
        re.IGNORECASE,
    )
    if m_ext:
        candidate = unquote(m_ext.group(1).strip().strip('"'))
        sanitized = sanitize_filename(candidate, "")
        if sanitized:
            return sanitized
    # Clásico: filename="nombre.mp4" o filename=nombre.mp4
    m_std = re.search(
        r'filename\s*=\s*"([^"]+)"|filename\s*=\s*([^;\r\n]+)',
        header_val,
        re.IGNORECASE,
    )
    if m_std:
        candidate = (m_std.group(1) or m_std.group(2) or "").strip()
        sanitized = sanitize_filename(candidate, "")
        if sanitized:
            return sanitized
    return None


def filename_from_url_path(url: str) -> str:
    sp = urlsplit(url)
    base = unquote(os.path.basename(sp.path.rstrip("/")))
    if base and "." in base and len(base) >= 3:
        return sanitize_filename(base)
    if base and len(base) >= 3 and not re.fullmatch(r"[0-9a-f]{16,}", base, re.I):
        return sanitize_filename(base)
    return ""


def ext_from_content_type(ctype: str | None) -> str:
    if not ctype:
        return ""
    base = ctype.split(";", 1)[0].strip().lower()
    if base in _CT_TO_EXT:
        return _CT_TO_EXT[base]
    if base == "application/octet-stream":
        return ""
    return mimetypes.guess_extension(base) or ""


def parse_content_range(header_val: str | None) -> tuple[int | None, int | None, int | None]:
    """Devuelve `(start, end, total)` donde None representa '*'."""
    if not header_val:
        return None, None, None
    m = CONTENT_RANGE_RE.match(header_val)
    if not m:
        return None, None, None
    start = int(m.group(1)) if m.group(1) is not None else None
    end = int(m.group(2)) if m.group(2) is not None else None
    total = int(m.group(3)) if m.group(3) != "*" else None
    return start, end, total


class _SNIHTTPSConnection(http.client.HTTPSConnection):
    """HTTPSConnection que conecta por IP pero permite elegir el SNI TLS."""

    def __init__(self, connect_host: str, port: int | None = None, *,
                 sni_hostname: str | None = None, **kwargs):
        super().__init__(connect_host, port=port, **kwargs)
        self._sni_hostname = sni_hostname

    def connect(self):
        sock = self._create_connection(
            (self.host, self.port), self.timeout, self.source_address
        )
        if self._tunnel_host:
            self.sock = sock
            self._tunnel()
            server_name = self._tunnel_host
        else:
            server_name = self._sni_hostname or self.host
        self.sock = self._context.wrap_socket(sock, server_hostname=server_name)


def _build_ssl_context(verify_tls: bool = False) -> ssl.SSLContext:
    if verify_tls:
        return ssl.create_default_context()
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    return ctx


def _resolve_target(url: str, free_ip: str, force_ip: bool, allow_direct: bool) -> tuple[str, int, str, str, str]:
    """Calcula `(connect_host, port, scheme, path_and_query, http_host_header)`.

    Lanza `DownloadError` si la URL no pertenece a `s3.todus.cu` y no se
    habilitó `--allow-direct` ni `--force-ip` (para proteger los datos
    móviles del usuario).
    """
    sp = urlsplit(url)
    scheme = (sp.scheme or "https").lower()
    if scheme not in ("http", "https"):
        raise DownloadError(f"Esquema no soportado: {scheme}")
    orig_host = (sp.hostname or "").lower()
    if not orig_host:
        raise DownloadError(f"URL sin host válido: {url}")

    port = sp.port or (443 if scheme == "https" else 80)
    target = sp.path or "/"
    if sp.query:
        target = f"{target}?{sp.query}"

    is_todus = orig_host in TODUS_S3_HOSTS
    if not is_todus and not force_ip:
        if not allow_direct:
            raise DownloadError(
                f"El host '{orig_host}' no es s3.todus.cu; la IP gratuita "
                "solo sirve para toDus. Abortado para no consumir tus datos "
                "(usa --allow-direct si aun así quieres descargarlo directo)."
            )
        host_hdr = orig_host if sp.port is None else f"{orig_host}:{sp.port}"
        return orig_host, port, scheme, target, host_hdr

    # En IP directa conservamos Host: s3.todus.cu salvo que el origen
    # fuese un host distinto forzado con --force-ip.
    vhost = "s3.todus.cu" if is_todus else orig_host
    if sp.port is not None and sp.port not in (80, 443):
        vhost = f"{vhost}:{sp.port}"
    return free_ip, port, scheme, target, vhost


def _open_request(url: str, method: str, extra_headers: dict[str, str], *,
                  free_ip: str, user_agent: str, timeout: float,
                  force_ip: bool, allow_direct: bool,
                  sni: str | None = None, verify_tls: bool = False,
                  max_redirects: int = 5):
    """Abre conexión HTTP/HTTPS contra la IP UDYAT siguiendo redirecciones.

    Devuelve `(conn, resp, final_url)`. Quien llama DEBE cerrar `conn`.
    """
    current = url
    ctx = _build_ssl_context(verify_tls=verify_tls)

    for _ in range(max_redirects + 1):
        connect_host, port, scheme, target, host_hdr = _resolve_target(
            current, free_ip=free_ip, force_ip=force_ip,
            allow_direct=allow_direct,
        )
        headers = {
            "Host": host_hdr,
            "User-Agent": user_agent,
            "Accept": "*/*",
            "Accept-Encoding": "identity",
            "Connection": "close",
        }
        headers.update(extra_headers)

        if scheme == "https":
            use_sni = sni or (host_hdr.split(":")[0] if verify_tls else None)
            if use_sni:
                conn = _SNIHTTPSConnection(
                    connect_host, port=port, timeout=timeout,
                    context=ctx, sni_hostname=use_sni,
                )
            else:
                conn = http.client.HTTPSConnection(
                    connect_host, port=port, timeout=timeout, context=ctx,
                )
        else:
            conn = http.client.HTTPConnection(
                connect_host, port=port, timeout=timeout,
            )

        try:
            conn.request(method, target, headers=headers)
            resp = conn.getresponse()
        except Exception:
            conn.close()
            raise

        if resp.status in (301, 302, 303, 307, 308):
            loc = resp.getheader("Location")
            resp.read()
            conn.close()
            if not loc:
                raise DownloadError(f"Redirección HTTP {resp.status} sin Location")
            current = urljoin(current, loc)
            continue

        return conn, resp, current

    raise DownloadError("Demasiadas redirecciones")


def _probe_remote(url: str, *, free_ip: str, user_agent: str, timeout: float,
                  force_ip: bool, allow_direct: bool, sni: str | None,
                  verify_tls: bool) -> tuple[int | None, str | None, bool, str, str]:
    """Consulta tamaño, nombre, soporte Range y Content-Type sin bajar el archivo."""
    conn = None
    try:
        conn, resp, final_url = _open_request(
            url, "HEAD", {},
            free_ip=free_ip, user_agent=user_agent, timeout=timeout,
            force_ip=force_ip, allow_direct=allow_direct,
            sni=sni, verify_tls=verify_tls,
        )
        status = resp.status
        headers = dict(resp.getheaders())
        resp.read()
        conn.close()
        conn = None

        err_snippet = ""
        if status in (400, 403, 405, 501):
            # Muchos proxies / S3 (p. ej. /todus/file/...) rechazan HEAD con 403 o 405
            # aunque acepten GET con Range. Reintentamos con GET de 1 byte.
            conn, resp, final_url = _open_request(
                url, "GET", {"Range": "bytes=0-0"},
                free_ip=free_ip, user_agent=user_agent, timeout=timeout,
                force_ip=force_ip, allow_direct=allow_direct,
                sni=sni, verify_tls=verify_tls,
            )
            status = resp.status
            headers = dict(resp.getheaders())
            preview = resp.read(400)
            conn.close()
            conn = None
            if status >= 400 and preview:
                err_snippet = re.sub(r"\s+", " ", preview.decode("utf-8", "replace")).strip()[:160]

        if status in (401, 403, 404, 410):
            detail = f" [{err_snippet}]" if err_snippet else ""
            hint = (
                f'\n  Prueba: python {os.path.basename(sys.argv[0])} --diagnose "{url}"'
                if status in (401, 403) else ""
            )
            raise DownloadError(
                f"El servidor respondió HTTP {status}{detail} "
                "(enlace caducado, borrado por toDus o sin acceso público)"
                + hint
            )

        lower = {k.lower(): v for k, v in headers.items()}
        _, _, cr_total = parse_content_range(lower.get("content-range"))
        clen = lower.get("content-length")
        total = cr_total if cr_total is not None else (
            int(clen) if clen and clen.isdigit() else None
        )
        disp = filename_from_content_disposition(lower.get("content-disposition"))
        accepts_ranges = (
            status == 206
            or lower.get("accept-ranges", "").lower() == "bytes"
        )
        ctype = lower.get("content-type", "")
        return total, disp, accepts_ranges, final_url, ctype
    finally:
        if conn is not None:
            try:
                conn.close()
            except Exception:
                pass


def _choose_output_path(out_dir: str, explicit_out: str | None,
                        hint_name: str | None, disp_name: str | None,
                        final_url: str, ctype: str = "") -> str:
    if explicit_out:
        parent = os.path.dirname(os.path.abspath(explicit_out))
        if parent:
            os.makedirs(parent, exist_ok=True)
        return explicit_out
    os.makedirs(out_dir, exist_ok=True)
    name = (
        hint_name
        or disp_name
        or filename_from_url_path(final_url)
    )
    if not name:
        ext = ext_from_content_type(ctype) or ".bin"
        name = f"todus_{int(time.time())}{ext}"
    return os.path.join(out_dir, sanitize_filename(name))


class _ProgressBar:
    """Impresión de progreso en una sola línea (sin dependencias)."""

    def __init__(self, label: str, total: int | None, initial: int = 0):
        self.label = label
        self.total = total
        self.done = initial
        self._lock = threading.Lock()
        self._start = time.monotonic()
        self._start_bytes = initial
        self._last_print = 0.0
        self._is_tty = sys.stdout.isatty()

    def set_resume_base(self, base: int) -> None:
        with self._lock:
            self.done = base
            self._start = time.monotonic()
            self._start_bytes = base

    def update(self, delta: int = 0, *, absolute: int | None = None,
               total: int | None = None, force: bool = False):
        with self._lock:
            if total and not self.total:
                self.total = total
            if absolute is not None:
                self.done = absolute
            else:
                self.done += delta
            now = time.monotonic()
            if not force and now - self._last_print < (0.25 if self._is_tty else 3.0):
                return
            self._last_print = now
            elapsed = max(0.05, now - self._start)
            speed = max(0.0, (self.done - self._start_bytes) / elapsed)
            speed_s = f"{format_size(int(speed))}/s"
            if self.total and self.total > 0:
                pct = min(100.0, self.done * 100.0 / self.total)
                rem = max(0, self.total - self.done)
                eta = format_eta(rem / speed) if speed > 0 else "--:--"
                line = (
                    f"↓ {self.label}: {pct:5.1f}% "
                    f"({format_size(self.done)}/{format_size(self.total)}) "
                    f"@ {speed_s} ETA {eta}"
                )
            else:
                line = f"↓ {self.label}: {format_size(self.done)} @ {speed_s}"
            if self._is_tty:
                sys.stdout.write(f"\r{line:<78}"[:80])
                sys.stdout.flush()
            else:
                print(line, flush=True)

    def finish(self, ok: bool = True):
        if self._is_tty:
            self.update(force=True)
            sys.stdout.write("\n")
            sys.stdout.flush()
        elif ok:
            self.update(force=True)


def _download_sequential(url: str, dest_path: str, expected_total: int | None,
                         *, free_ip: str, user_agent: str, timeout: float,
                         retries: int, force_ip: bool, allow_direct: bool,
                         sni: str | None, verify_tls: bool,
                         progress: _ProgressBar) -> int:
    part_path = f"{dest_path}.part"
    if expected_total and os.path.exists(part_path):
        existing = os.path.getsize(part_path)
        if existing > expected_total:
            os.unlink(part_path)
        elif existing == expected_total and expected_total > 0:
            os.replace(part_path, dest_path)
            progress.set_resume_base(existing)
            progress.update(absolute=existing, total=expected_total, force=True)
            return existing

    last_err: str | None = None
    total = expected_total
    resume_seeded = False

    for attempt in range(1, retries + 2):
        offset = os.path.getsize(part_path) if os.path.exists(part_path) else 0
        if not resume_seeded and offset > 0:
            progress.set_resume_base(offset)
            resume_seeded = True
        progress.update(absolute=offset, total=total)
        req_headers = {"Range": f"bytes={offset}-"} if offset > 0 else {}
        conn = None
        try:
            conn, resp, _ = _open_request(
                url, "GET", req_headers,
                free_ip=free_ip, user_agent=user_agent, timeout=timeout,
                force_ip=force_ip, allow_direct=allow_direct,
                sni=sni, verify_tls=verify_tls,
            )
            status = resp.status

            if status in (401, 403, 404, 410):
                preview = resp.read(400)
                detail = ""
                if preview:
                    detail = " [" + re.sub(
                        r"\s+", " ", preview.decode("utf-8", "replace")
                    ).strip()[:160] + "]"
                raise DownloadError(
                    f"HTTP {status}{detail} (enlace caducado, eliminado o sin acceso)"
                )

            if status == 416:
                _, _, cr_total = parse_content_range(resp.getheader("Content-Range"))
                if cr_total and offset == cr_total:
                    os.replace(part_path, dest_path)
                    return offset
                raise _RestartError("El servidor rechazó el Range (HTTP 416)")

            if status in RETRYABLE_STATUSES or 500 <= status < 600:
                raise _RetryableError(f"HTTP {status} transitorio")

            if status not in (200, 206):
                raise DownloadError(f"HTTP {status} inesperado")

            if offset > 0 and status == 200:
                # El servidor ignoró el Range: reiniciamos limpio en esta misma respuesta.
                offset = 0
                mode = "wb"
                clen = resp.getheader("Content-Length")
                if clen and clen.isdigit():
                    total = int(clen)
                progress.set_resume_base(0)
            elif offset > 0 and status == 206:
                cr_start, _, cr_total = parse_content_range(resp.getheader("Content-Range"))
                if cr_start != offset:
                    raise _RestartError(
                        f"Content-Range incoherente ({resp.getheader('Content-Range')})"
                    )
                if cr_total is not None:
                    total = cr_total
                mode = "ab"
            else:
                # offset == 0
                clen = resp.getheader("Content-Length")
                if status == 206:
                    cr_start, _, cr_total = parse_content_range(resp.getheader("Content-Range"))
                    if cr_start not in (0, None):
                        raise _RestartError(
                            f"206 inesperado desde offset 0 ({resp.getheader('Content-Range')})"
                        )
                    if cr_total is not None:
                        total = cr_total
                elif clen and clen.isdigit():
                    total = int(clen)
                mode = "wb"

            if total:
                progress.update(absolute=offset, total=total)

            clen_resp = resp.getheader("Content-Length")
            expected_body = int(clen_resp) if (clen_resp and clen_resp.isdigit()) else None
            read_this_try = 0

            with open(part_path, mode) as fh:
                while True:
                    chunk = resp.read(CHUNK_SIZE)
                    if not chunk:
                        break
                    fh.write(chunk)
                    read_this_try += len(chunk)
                    progress.update(len(chunk), total=total)

            if expected_body is not None and read_this_try < expected_body:
                raise _RetryableError(
                    f"conexión cortada a mitad ({read_this_try}/{expected_body} bytes)"
                )

            final_size = os.path.getsize(part_path)
            if total and final_size < total:
                raise _RetryableError(
                    f"archivo incompleto ({final_size}/{total} bytes)"
                )

            os.replace(part_path, dest_path)
            return final_size

        except DownloadError:
            raise
        except _RestartError as e:
            last_err = str(e)
            if os.path.exists(part_path):
                try:
                    os.unlink(part_path)
                except OSError:
                    pass
            progress.set_resume_base(0)
        except (_RetryableError, http.client.HTTPException, OSError,
                TimeoutError, ssl.SSLError) as e:
            last_err = f"{type(e).__name__}: {e}" if str(e) else type(e).__name__
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

        if attempt <= retries:
            wait = min(2 ** (attempt - 1), 15)
            time.sleep(wait)

    raise DownloadError(f"falló tras {retries + 1} intentos ({last_err})")


def _download_parallel(url: str, dest_path: str, total: int, threads: int,
                       *, free_ip: str, user_agent: str, timeout: float,
                       retries: int, force_ip: bool, allow_direct: bool,
                       sni: str | None, verify_tls: bool,
                       progress: _ProgressBar) -> int:
    """Descarga paralela por Range. Si una parte recibe 200 (sin Range), avisa para fallback."""
    n_parts = min(threads, max(1, total // (2 * 1024 * 1024)))
    if n_parts <= 1:
        return _download_sequential(
            url, dest_path, total,
            free_ip=free_ip, user_agent=user_agent, timeout=timeout,
            retries=retries, force_ip=force_ip, allow_direct=allow_direct,
            sni=sni, verify_tls=verify_tls, progress=progress,
        )

    part_size = total // n_parts
    ranges = []
    part_paths = []
    for i in range(n_parts):
        start = i * part_size
        end = (start + part_size - 1) if i < n_parts - 1 else (total - 1)
        ranges.append((i, start, end))
        part_paths.append(f"{dest_path}.part{i}")

    def _fetch_part(idx: int, start: int, end: int) -> None:
        ppath = part_paths[idx]
        part_len = end - start + 1

        last_err: str | None = None
        for attempt in range(1, retries + 2):
            done = os.path.getsize(ppath) if os.path.exists(ppath) else 0
            if done >= part_len:
                return
            req_start = start + done
            conn = None
            try:
                conn, resp, _ = _open_request(
                    url, "GET", {"Range": f"bytes={req_start}-{end}"},
                    free_ip=free_ip, user_agent=user_agent, timeout=timeout,
                    force_ip=force_ip, allow_direct=allow_direct,
                    sni=sni, verify_tls=verify_tls,
                )
                if resp.status in (401, 403, 404, 410):
                    raise DownloadError(f"HTTP {resp.status} en parte {idx}")
                if resp.status == 200:
                    raise _RestartError("servidor devolvió 200 a Range")
                if resp.status in RETRYABLE_STATUSES or 500 <= resp.status < 600:
                    raise _RetryableError(f"HTTP {resp.status}")
                if resp.status != 206:
                    raise DownloadError(f"HTTP {resp.status} inesperado en parte {idx}")

                cr_start, _, _ = parse_content_range(resp.getheader("Content-Range"))
                if cr_start != req_start:
                    raise _RestartError(f"Content-Range inválido en parte {idx}")

                need = end - req_start + 1
                got = 0
                mode = "ab" if done > 0 else "wb"
                with open(ppath, mode) as fh:
                    while got < need:
                        chunk = resp.read(min(CHUNK_SIZE, need - got))
                        if not chunk:
                            break
                        fh.write(chunk)
                        got += len(chunk)
                        progress.update(len(chunk))
                if os.path.getsize(ppath) == part_len:
                    return
                raise _RetryableError(f"parte {idx} incompleta")
            except (DownloadError, _RestartError):
                raise
            except (_RetryableError, http.client.HTTPException, OSError,
                    TimeoutError, ssl.SSLError) as e:
                last_err = str(e) or type(e).__name__
            finally:
                if conn is not None:
                    try:
                        conn.close()
                    except Exception:
                        pass
            if attempt <= retries:
                time.sleep(min(2 ** (attempt - 1), 15))
        raise DownloadError(f"parte {idx} falló ({last_err})")

    initial_done = 0
    for i, s, e in ranges:
        ppath = part_paths[i]
        part_len = e - s + 1
        if os.path.exists(ppath):
            sz = os.path.getsize(ppath)
            if sz > part_len:
                os.unlink(ppath)
            else:
                initial_done += sz
    if initial_done:
        progress.set_resume_base(initial_done)

    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=n_parts) as pool:
            futs = [pool.submit(_fetch_part, i, s, e) for i, s, e in ranges]
            for fut in futs:
                fut.result()

        tmp_out = f"{dest_path}.part"
        with open(tmp_out, "wb") as out:
            for ppath in part_paths:
                with open(ppath, "rb") as src:
                    while True:
                        buf = src.read(1024 * 1024)
                        if not buf:
                            break
                        out.write(buf)
        if os.path.getsize(tmp_out) != total:
            raise DownloadError("tamaño ensamblado no coincide con el esperado")
        os.replace(tmp_out, dest_path)
        for ppath in part_paths:
            try:
                os.unlink(ppath)
            except OSError:
                pass
        return total
    except _RestartError:
        for ppath in part_paths:
            try:
                os.unlink(ppath)
            except OSError:
                pass
        raise


def download_one(raw_url: str, *, out_dir: str = ".", explicit_out: str | None = None,
                 free_ip: str = DEFAULT_FREE_IP, user_agent: str = DEFAULT_UA,
                 timeout: float = 60.0, retries: int = 4, threads: int = 1,
                 overwrite: bool = False, force_ip: bool = False,
                 allow_direct: bool = False, sni: str | None = None,
                 verify_tls: bool = False) -> str:
    """Descarga una URL por la IP UDYAT y devuelve la ruta final guardada."""
    clean_url, hint_name = parse_url_and_hint(raw_url)
    # Validación temprana del host antes de gastar red
    _resolve_target(
        clean_url, free_ip=free_ip, force_ip=force_ip,
        allow_direct=allow_direct,
    )

    total = None
    disp_name = None
    accepts_ranges = False
    final_url = clean_url
    ctype = ""
    try:
        total, disp_name, accepts_ranges, final_url, ctype = _probe_remote(
            clean_url,
            free_ip=free_ip, user_agent=user_agent, timeout=min(timeout, 30.0),
            force_ip=force_ip, allow_direct=allow_direct,
            sni=sni, verify_tls=verify_tls,
        )
    except DownloadError:
        raise
    except Exception:
        # Si el HEAD falla de forma transitoria, el GET secuencial reintentará.
        pass

    dest_path = _choose_output_path(
        out_dir, explicit_out, hint_name, disp_name, final_url, ctype
    )
    if os.path.exists(dest_path) and not overwrite:
        existing_size = os.path.getsize(dest_path)
        if total and existing_size == total:
            print(f"✓ Ya existe completo ({format_size(existing_size)}): {dest_path}")
            return dest_path
        if not explicit_out:
            stem, ext = os.path.splitext(dest_path)
            n = 1
            while os.path.exists(f"{stem}_{n}{ext}"):
                n += 1
            dest_path = f"{stem}_{n}{ext}"

    label = os.path.basename(dest_path)
    progress = _ProgressBar(label, total)
    ok = False
    try:
        if threads > 1 and accepts_ranges and total and total >= 4 * 1024 * 1024:
            try:
                _download_parallel(
                    final_url, dest_path, total, threads,
                    free_ip=free_ip, user_agent=user_agent, timeout=timeout,
                    retries=retries, force_ip=force_ip, allow_direct=allow_direct,
                    sni=sni, verify_tls=verify_tls, progress=progress,
                )
                ok = True
                return dest_path
            except _RestartError:
                # El servidor rechazó Range en paralelo: caemos a secuencial
                progress = _ProgressBar(label, total)

        _download_sequential(
            final_url, dest_path, total,
            free_ip=free_ip, user_agent=user_agent, timeout=timeout,
            retries=retries, force_ip=force_ip, allow_direct=allow_direct,
            sni=sni, verify_tls=verify_tls, progress=progress,
        )
        ok = True
        return dest_path
    finally:
        progress.finish(ok=ok)


def run_preflight_test(ips: list[str], user_agent: str, timeout: float = 5.0,
                       sni: str | None = None, verify_tls: bool = False,
                       port: int = 443) -> int:
    """Prueba conectividad TLS + HTTP contra las IPs UDYAT sin bajar archivos."""
    print(f"Probando conexión UDYAT (Host: s3.todus.cu · UA: {user_agent})...\n")
    any_ok = False
    test_url = "https://s3.todus.cu/" if port == 443 else f"https://s3.todus.cu:{port}/"
    for ip in ips:
        t0 = time.monotonic()
        conn = None
        try:
            conn, resp, _ = _open_request(
                test_url, "HEAD", {},
                free_ip=ip, user_agent=user_agent, timeout=timeout,
                force_ip=True, allow_direct=False,
                sni=sni, verify_tls=verify_tls, max_redirects=0,
            )
            ms = int((time.monotonic() - t0) * 1000)
            status = resp.status
            resp.read()
            any_ok = True
            tag = "recomendada" if ip == DEFAULT_FREE_IP else "alternativa"
            print(f"  ✓ {ip} ({tag}) → HTTP {status} en {ms} ms")
        except Exception as e:
            print(f"  ✗ {ip} → error: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
    print(
        "\nNota: cualquier código HTTP (200/403/404) confirma que el servidor responde.\n"
        "Que ETECSA cobre 0 MB solo puede comprobarse desde datos móviles en Cuba:\n"
        "baja primero un archivo pequeño y revisa tu saldo (*222# / *222*328#)."
    )
    return 0 if any_ok else 1


def run_diagnose(raw_url: str, *, free_ip: str = DEFAULT_FREE_IP,
                 backup_ip: str = BACKUP_FREE_IP, timeout: float = 8.0,
                 include_direct_dns: bool = True) -> int:
    """Diagnostica por qué una URL concreta falla (p. ej. HTTP 403) pidiendo solo 1 byte."""
    clean_url, _ = parse_url_and_hint(raw_url)
    sp = urlsplit(clean_url)
    orig_host = (sp.hostname or "s3.todus.cu").lower()

    probes: list[tuple[str, dict]] = [
        (
            f"1) UDYAT estándar ({free_ip}, sin SNI, UA {DEFAULT_UA})",
            {"free_ip": free_ip, "user_agent": DEFAULT_UA, "force_ip": True,
             "allow_direct": False, "sni": None},
        ),
        (
            f"2) IP gratuita + UA app toDus ({free_ip}, sin SNI, UA okhttp/4.9.0)",
            {"free_ip": free_ip, "user_agent": "okhttp/4.9.0", "force_ip": True,
             "allow_direct": False, "sni": None},
        ),
        (
            f"3) IP gratuita + SNI ({free_ip}, SNI {orig_host}, UA {DEFAULT_UA})",
            {"free_ip": free_ip, "user_agent": DEFAULT_UA, "force_ip": True,
             "allow_direct": False, "sni": orig_host},
        ),
        (
            f"4) IP respaldo ({backup_ip}, sin SNI, UA {DEFAULT_UA})",
            {"free_ip": backup_ip, "user_agent": DEFAULT_UA, "force_ip": True,
             "allow_direct": False, "sni": None},
        ),
    ]
    if include_direct_dns:
        probes.append((
            f"5) Origen DNS directo ({orig_host}, SNI {orig_host}, UA TGS3BOT/1.0)",
            {"free_ip": orig_host, "user_agent": "TGS3BOT/1.0 (+https://s3.todus.cu)",
             "force_ip": False, "allow_direct": True, "sni": orig_host},
        ))

    print(f"Diagnosticando (GET Range: bytes=0-0) → {clean_url}\n")
    ok_indices: list[int] = []
    for idx, (label, opts) in enumerate(probes, 1):
        conn = None
        t0 = time.monotonic()
        try:
            conn, resp, _ = _open_request(
                clean_url, "GET", {"Range": "bytes=0-0"},
                timeout=timeout, verify_tls=False, **opts,
            )
            ms = int((time.monotonic() - t0) * 1000)
            st = resp.status
            body = resp.read(300)
            snippet = ""
            if st >= 400 and body:
                snippet = " · " + re.sub(
                    r"\s+", " ", body.decode("utf-8", "replace")
                ).strip()[:120]
            mark = "✓" if st in (200, 206) else "✗"
            if st in (200, 206):
                ok_indices.append(idx)
            print(f"  {mark} {label}\n      → HTTP {st} ({ms} ms){snippet}")
        except Exception as e:
            print(f"  ✗ {label}\n      → error: {e}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass

    print()
    if 1 in ok_indices:
        print("→ La vía UDYAT estándar responde bien; puedes descargar normal.")
    elif 2 in ok_indices:
        print('→ Usa: --ua "okhttp/4.9.0"')
    elif 3 in ok_indices:
        print(f'→ Usa: --sni "{orig_host}"')
    elif 4 in ok_indices:
        print(f'→ Usa: --ip "{backup_ip}" (ojo: verifica saldo porque .88 puede consumir datos)')
    elif 5 in ok_indices:
        print(
            f"→ El enlace sigue vivo en {orig_host}, pero {free_ip} no lo sirve "
            "(habitual en rutas /todus/file/... frente a /stream/...). "
            "Para tener ruta /stream/... en el bot, activa TODUS_ANON_MIRROR=1."
        )
    else:
        print("→ Ninguna ruta pudo leer el objeto: el enlace caducó o requiere Bearer token.")
    return 0 if ok_indices else 1


def load_urls_from_file(path: str) -> list[str]:
    urls = []
    with open(path, encoding="utf-8-sig") as fh:
        for line in fh:
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            urls.append(s)
    return urls


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=(
            "Descarga enlaces de s3.todus.cu usando la IP interna (método UDYAT) "
            "y las cabeceras Host: s3.todus.cu + User-Agent: Wget/1.21.3."
        ),
        epilog=(
            "Ejemplos:\n"
            "  python scripts/udyat_download.py --test\n"
            '  python scripts/udyat_download.py "https://s3.todus.cu/stream/..."\n'
            "  python scripts/udyat_download.py enlaces_20261106_1200.txt -d Descargas\n"
            '  python scripts/udyat_download.py -t 4 "https://s3.todus.cu/stream/..."\n'
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "inputs", nargs="*",
        help="URL(s) de toDus o ruta a un archivo enlaces_*.txt del bot",
    )
    parser.add_argument(
        "-f", "--file", dest="txt_file",
        help="Archivo .txt con una URL por línea (como enlaces_*.txt)",
    )
    parser.add_argument(
        "-d", "--dir", default=".",
        help="Carpeta de destino (default: carpeta actual)",
    )
    parser.add_argument(
        "-o", "--output",
        help="Ruta exacta de salida (solo si descargas una sola URL)",
    )
    parser.add_argument(
        "--ip", default=DEFAULT_FREE_IP,
        help=f"IP de toDus a usar (default: {DEFAULT_FREE_IP}, o env UDYAT_FREE_IP)",
    )
    parser.add_argument(
        "--ua", default=DEFAULT_UA,
        help=f"User-Agent HTTP (default: {DEFAULT_UA})",
    )
    parser.add_argument(
        "-t", "--threads", type=int, default=1,
        help="Conexiones paralelas por Range (default: 1; ej. 4 para archivos grandes)",
    )
    parser.add_argument(
        "--retries", type=int, default=4,
        help="Reintentos ante cortes de red con reanudación por Range (default: 4)",
    )
    parser.add_argument(
        "--timeout", type=float, default=60.0,
        help="Timeout de conexión/lectura en segundos (default: 60)",
    )
    parser.add_argument(
        "--overwrite", action="store_true",
        help="Sobrescribir si ya existe el archivo de destino",
    )
    parser.add_argument(
        "--force-ip", action="store_true",
        help="Enviar cualquier host a --ip aunque no sea s3.todus.cu",
    )
    parser.add_argument(
        "--allow-direct", action="store_true",
        help="Permitir descargar directo si la URL NO es s3.todus.cu (consume datos)",
    )
    parser.add_argument(
        "--sni", default=None,
        help="Forzar TLS SNI (por defecto vacío al conectar por IP, igual que wget)",
    )
    parser.add_argument(
        "--verify-tls", action="store_true",
        help="Verificar el certificado TLS (fallará si la IP usa cert self-signed)",
    )
    parser.add_argument(
        "--test", action="store_true",
        help="Probar conectividad HEAD hacia la IP gratuita y salir",
    )
    parser.add_argument(
        "--diagnose", metavar="URL",
        help="Probar 1 byte contra IP gratuita, UA, SNI e IP respaldo para diagnosticar un 403",
    )

    args = parser.parse_args(argv)

    if args.diagnose:
        return run_diagnose(
            args.diagnose,
            free_ip=args.ip,
            timeout=min(args.timeout, 10.0),
        )

    if args.test:
        ips = [args.ip] if args.ip != DEFAULT_FREE_IP else [DEFAULT_FREE_IP, BACKUP_FREE_IP]
        return run_preflight_test(
            ips, user_agent=args.ua, timeout=min(args.timeout, 10.0),
            sni=args.sni, verify_tls=args.verify_tls,
        )

    urls: list[str] = []
    if args.txt_file:
        try:
            urls.extend(load_urls_from_file(args.txt_file))
        except OSError as e:
            print(f"✗ No se pudo leer {args.txt_file}: {e}", file=sys.stderr)
            return 2
    for item in args.inputs:
        if item.startswith(("http://", "https://")):
            urls.append(item)
        elif os.path.isfile(item):
            try:
                urls.extend(load_urls_from_file(item))
            except OSError as e:
                print(f"✗ No se pudo leer {item}: {e}", file=sys.stderr)
                return 2
        else:
            print(f"✗ Entrada ignorada (no es URL ni archivo existente): {item}", file=sys.stderr)
            return 2

    if not urls and not sys.stdin.isatty():
        for line in sys.stdin:
            s = line.strip()
            if s and not s.startswith("#"):
                urls.append(s)

    if not urls:
        parser.print_help()
        return 2

    if args.output and len(urls) > 1:
        print("✗ --output solo se puede usar con una única URL (usa --dir para lotes).", file=sys.stderr)
        return 2

    ok_count = 0
    total_count = len(urls)
    for idx, raw_url in enumerate(urls, 1):
        if total_count > 1:
            print(f"\n[{idx}/{total_count}] {raw_url}")
        try:
            saved = download_one(
                raw_url,
                out_dir=args.dir,
                explicit_out=args.output,
                free_ip=args.ip,
                user_agent=args.ua,
                timeout=args.timeout,
                retries=max(0, args.retries),
                threads=max(1, args.threads),
                overwrite=args.overwrite,
                force_ip=args.force_ip,
                allow_direct=args.allow_direct,
                sni=args.sni,
                verify_tls=args.verify_tls,
            )
            print(f"✓ Guardado: {saved} ({format_size(os.path.getsize(saved))})")
            ok_count += 1
        except KeyboardInterrupt:
            print("\n⊘ Interrumpido por el usuario (el .part queda guardado para reanudar).", file=sys.stderr)
            return 130
        except Exception as e:
            print(f"✗ Error: {e}", file=sys.stderr)

    if total_count > 1:
        print(f"\nResumen: {ok_count}/{total_count} descargados correctamente.")
    return 0 if ok_count == total_count else 1


if __name__ == "__main__":
    raise SystemExit(main())
