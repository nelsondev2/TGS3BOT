#!/usr/bin/env python3
"""Suite de pruebas para scripts/udyat_download.py contra un servidor HTTPS local."""

from __future__ import annotations

import hashlib
import http.server
import os
import re
import socketserver
import ssl
import subprocess
import sys
import tempfile
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import udyat_download as udyat  # noqa: E402


class _ThreadingHTTPSServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True

    def handle_error(self, request, client_address):
        # Ignora desconexiones esperadas cuando el cliente aborta un 200 al pedir Range
        exc = sys.exc_info()[1]
        if isinstance(exc, (BrokenPipeError, ConnectionResetError, ssl.SSLEOFError)):
            return
        super().handle_error(request, client_address)


def _make_self_signed_cert(tmp_dir: str) -> tuple[str, str]:
    cert = os.path.join(tmp_dir, "cert.pem")
    key = os.path.join(tmp_dir, "key.pem")
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048",
            "-keyout", key, "-out", cert, "-days", "1", "-nodes",
            "-subj", "/CN=s3.todus.cu",
        ],
        check=True,
        capture_output=True,
    )
    return cert, key


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: str) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            b = fh.read(1024 * 1024)
            if not b:
                break
            h.update(b)
    return h.hexdigest()


def main() -> int:
    with tempfile.TemporaryDirectory() as tmp:
        cert, key = _make_self_signed_cert(tmp)
        sni_seen: list[str | None] = []
        observed_requests: list[dict] = []
        req_counts: dict[str, int] = {}
        lock = threading.Lock()

        small_data = os.urandom(400 * 1024)  # 400 KB
        big_data = os.urandom(5 * 1024 * 1024 + 137)  # > 5 MB para multi-hilo
        opaque_data = b"MP4-FAKE-" + os.urandom(120 * 1024)

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def log_message(self, fmt, *args):
                pass

            def _record(self, path_key: str) -> int:
                with lock:
                    req_counts[path_key] = req_counts.get(path_key, 0) + 1
                    n = req_counts[path_key]
                    observed_requests.append({
                        "method": self.command,
                        "path": self.path,
                        "host": self.headers.get("Host"),
                        "ua": self.headers.get("User-Agent"),
                        "ae": self.headers.get("Accept-Encoding"),
                        "range": self.headers.get("Range"),
                    })
                return n

            def do_HEAD(self):
                path_only = self.path.split("?", 1)[0]
                self._record(f"HEAD:{path_only}")
                if path_only in ("/forbidden", "/head403_get200", "/only_okhttp"):
                    self.send_response(403)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path_only == "/nohead":
                    self.send_response(405)
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return
                if path_only == "/parallel":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(big_data)))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    return
                if path_only == "/parallel_no_range":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(big_data)))
                    self.send_header("Accept-Ranges", "bytes")
                    self.end_headers()
                    return
                if path_only == "/disp_utf8":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(small_data)))
                    self.send_header(
                        "Content-Disposition",
                        "attachment; filename=\"fallback.bin\"; filename*=UTF-8''Canci%C3%B3n%20Final.mp3",
                    )
                    self.end_headers()
                    return
                if path_only == "/stream/todus/file/deadbeef0123456789abcdef01234567":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(opaque_data)))
                    self.send_header("Content-Type", "video/mp4")
                    self.end_headers()
                    return
                self.send_response(200)
                self.send_header("Content-Length", str(len(small_data)))
                self.send_header("Accept-Ranges", "bytes")
                self.end_headers()

            def do_GET(self):
                path_only = self.path.split("?", 1)[0]
                n = self._record(f"GET:{path_only}")
                rng = self.headers.get("Range")

                if path_only == "/forbidden":
                    err = b"<Error><Code>AccessDenied</Code></Error>"
                    self.send_response(403)
                    self.send_header("Content-Length", str(len(err)))
                    self.end_headers()
                    self.wfile.write(err)
                    return

                if path_only == "/only_okhttp":
                    if (self.headers.get("User-Agent") or "") != "okhttp/4.9.0":
                        err = b"403 Blocked UA"
                        self.send_response(403)
                        self.send_header("Content-Length", str(len(err)))
                        self.end_headers()
                        self.wfile.write(err)
                        return
                    body = small_data[:1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes 0-0/{len(small_data)}")
                    self.send_header("Content-Length", "1")
                    self.end_headers()
                    self.wfile.write(body)
                    return

                if path_only == "/head403_get200":
                    m = re.match(r"bytes=(\d+)-(\d*)$", rng or "")
                    if m:
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else len(small_data) - 1
                        body = small_data[start:end + 1]
                        self.send_response(206)
                        self.send_header("Content-Range", f"bytes {start}-{end}/{len(small_data)}")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(small_data)))
                    self.end_headers()
                    self.wfile.write(small_data)
                    return

                if path_only == "/redirect_ok":
                    self.send_response(302)
                    self.send_header("Location", "/stream/todus/file/signed?X-Amz-Signature=sig123")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                if path_only == "/redirect_external":
                    self.send_response(302)
                    self.send_header("Location", "https://evil-billed.example.com/a.bin")
                    self.send_header("Content-Length", "0")
                    self.end_headers()
                    return

                if path_only == "/busy503":
                    if n <= 2:
                        self.send_response(503)
                        self.send_header("Content-Length", "4")
                        self.end_headers()
                        self.wfile.write(b"busy")
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(small_data)))
                    self.end_headers()
                    self.wfile.write(small_data)
                    return

                if path_only == "/cut_resume":
                    if not rng:
                        cut = 150 * 1024
                        self.send_response(200)
                        self.send_header("Content-Length", str(len(small_data)))
                        self.end_headers()
                        self.wfile.write(small_data[:cut])
                        self.wfile.flush()
                        # Cierre abrupto tras corte a mitad
                        self.close_connection = True
                        return
                    m = re.match(r"bytes=(\d+)-(\d*)$", rng)
                    start = int(m.group(1))
                    end = len(small_data) - 1
                    chunk = small_data[start:end + 1]
                    self.send_response(206)
                    self.send_header("Content-Range", f"bytes {start}-{end}/{len(small_data)}")
                    self.send_header("Content-Length", str(len(chunk)))
                    self.end_headers()
                    self.wfile.write(chunk)
                    return

                if path_only == "/ignore_range":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(small_data)))
                    self.end_headers()
                    self.wfile.write(small_data)
                    return

                if path_only == "/bad206":
                    if rng:
                        # Content-Range desalineado a propósito
                        self.send_response(206)
                        self.send_header("Content-Range", f"bytes 0-99/{len(small_data)}")
                        self.send_header("Content-Length", "100")
                        self.end_headers()
                        self.wfile.write(small_data[:100])
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(small_data)))
                    self.end_headers()
                    self.wfile.write(small_data)
                    return

                if path_only == "/parallel":
                    m = re.match(r"bytes=(\d+)-(\d*)$", rng or "")
                    if m:
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else len(big_data) - 1
                        body = big_data[start:end + 1]
                        self.send_response(206)
                        self.send_header("Content-Range", f"bytes {start}-{end}/{len(big_data)}")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(big_data)))
                    self.end_headers()
                    self.wfile.write(big_data)
                    return

                if path_only == "/parallel_no_range":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(big_data)))
                    self.end_headers()
                    self.wfile.write(big_data)
                    return

                if path_only == "/stream/todus/file/deadbeef0123456789abcdef01234567":
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(opaque_data)))
                    self.send_header("Content-Type", "video/mp4")
                    self.end_headers()
                    self.wfile.write(opaque_data)
                    return

                if path_only == "/nohead":
                    m = re.match(r"bytes=(\d+)-(\d*)$", rng or "")
                    if m:
                        start = int(m.group(1))
                        end = int(m.group(2)) if m.group(2) else len(small_data) - 1
                        body = small_data[start:end + 1]
                        self.send_response(206)
                        self.send_header("Content-Range", f"bytes {start}-{end}/{len(small_data)}")
                        self.send_header("Content-Length", str(len(body)))
                        self.end_headers()
                        self.wfile.write(body)
                        return
                    self.send_response(200)
                    self.send_header("Content-Length", str(len(small_data)))
                    self.end_headers()
                    self.wfile.write(small_data)
                    return

                # Default: small_data (incl. signed, disp_utf8, /)
                self.send_response(200)
                self.send_header("Content-Length", str(len(small_data)))
                self.end_headers()
                self.wfile.write(small_data)

        srv = _ThreadingHTTPSServer(("127.0.0.1", 0), Handler)
        port = srv.server_address[1]
        srv_ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        srv_ctx.load_cert_chain(cert, key)
        srv_ctx.sni_callback = lambda sock, name, ctx: sni_seen.append(name)
        srv.socket = srv_ctx.wrap_socket(srv.socket, server_side=True)

        t = threading.Thread(target=srv.serve_forever, daemon=True)
        t.start()
        out_dir = os.path.join(tmp, "out")
        os.makedirs(out_dir, exist_ok=True)
        base = f"https://s3.todus.cu:{port}"

        try:
            # 1. URL oficial firmada + #filename= con espacios y '+'
            p1 = udyat.download_one(
                f"{base}/stream/todus/file/signed?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=deadbeef#filename=Mi%20Pel%C3%ADcula%20%2B%20HD.mp4",
                out_dir=out_dir, free_ip="127.0.0.1", retries=2,
            )
            assert os.path.basename(p1) == "Mi Película + HD.mp4", p1
            assert _sha256_file(p1) == _sha256_bytes(small_data)
            # Verificar cabeceras en red y ausencia de SNI por defecto
            signed_reqs = [r for r in observed_requests if r["path"].startswith("/stream/todus/file/signed")]
            assert signed_reqs, observed_requests
            for r in signed_reqs:
                assert r["host"] == f"s3.todus.cu:{port}", r
                assert r["ua"] == "Wget/1.21.3", r
                assert r["ae"] == "identity", r
                assert r["path"] == "/stream/todus/file/signed?X-Amz-Algorithm=AWS4-HMAC-SHA256&X-Amz-Signature=deadbeef", r
            assert all(s is None for s in sni_seen), f"SNI inesperado: {sni_seen}"
            print("PASS 1: URL firmada + #filename= + cabeceras exactas + sin SNI")

            # 2. Corte a mitad + reanudación por Range
            p2 = udyat.download_one(
                f"{base}/cut_resume#filename=cortado.bin",
                out_dir=out_dir, free_ip="127.0.0.1", retries=3,
            )
            assert _sha256_file(p2) == _sha256_bytes(small_data)
            assert req_counts.get("GET:/cut_resume") == 2, req_counts
            print("PASS 2: Corte a mitad + reanudación por Range (2 GETs, SHA-256 idéntico)")

            # 3. .part previo + servidor ignora Range (200 OK) -> reinicio limpio
            ign_dest = os.path.join(out_dir, "ign_range.bin")
            with open(ign_dest + ".part", "wb") as fh:
                fh.write(b"basura-previa" * 500)
            p3 = udyat.download_one(
                f"{base}/ignore_range", explicit_out=ign_dest,
                free_ip="127.0.0.1", retries=2,
            )
            assert _sha256_file(p3) == _sha256_bytes(small_data)
            print("PASS 3: Servidor devuelve 200 a Range -> reinicio limpio sin concatenar")

            # 4. Bad 206 (Content-Range incorrecto) -> descarta .part y reinicia desde 0
            bad_dest = os.path.join(out_dir, "bad206.bin")
            with open(bad_dest + ".part", "wb") as fh:
                fh.write(b"X" * 1024)
            p4 = udyat.download_one(
                f"{base}/bad206", explicit_out=bad_dest,
                free_ip="127.0.0.1", retries=2,
            )
            assert _sha256_file(p4) == _sha256_bytes(small_data)
            assert req_counts.get("GET:/bad206") == 2, req_counts
            print("PASS 4: 206 con Content-Range incoherente -> reinicio limpio a 200")

            # 5. 503 x2 -> recuperación al 3er intento; 403 -> fallo rápido (1 intento)
            p5 = udyat.download_one(
                f"{base}/busy503#filename=ocupado.bin",
                out_dir=out_dir, free_ip="127.0.0.1", retries=3,
            )
            assert _sha256_file(p5) == _sha256_bytes(small_data)
            assert req_counts.get("GET:/busy503") == 3, req_counts

            try:
                udyat.download_one(
                    f"{base}/forbidden#filename=prohibido.bin",
                    out_dir=out_dir, free_ip="127.0.0.1", retries=4,
                )
                raise AssertionError("403 debía fallar")
            except udyat.DownloadError as e:
                assert "AccessDenied" in str(e), str(e)
            assert req_counts.get("HEAD:/forbidden") == 1, req_counts
            assert req_counts.get("GET:/forbidden", 0) == 1, req_counts

            # 5b. HEAD -> 403 pero GET Range -> 206 (caso /todus/file/...)
            p5b = udyat.download_one(
                f"{base}/head403_get200#filename=oficial_file.bin",
                out_dir=out_dir, free_ip="127.0.0.1", retries=2,
            )
            assert _sha256_file(p5b) == _sha256_bytes(small_data)
            assert req_counts.get("HEAD:/head403_get200") == 1, req_counts
            print("PASS 5: 503 reintentado, 403 con detalle + fallback HEAD 403->GET 206")

            # 6. Descarga paralela (--threads 4) > 5 MB + fallback cuando 200 en paralelo
            p6 = udyat.download_one(
                f"{base}/parallel#filename=grande_par.bin",
                out_dir=out_dir, free_ip="127.0.0.1", threads=4, retries=2,
            )
            assert _sha256_file(p6) == _sha256_bytes(big_data)

            p6b = udyat.download_one(
                f"{base}/parallel_no_range#filename=grande_fb.bin",
                out_dir=out_dir, free_ip="127.0.0.1", threads=4, retries=2,
            )
            assert _sha256_file(p6b) == _sha256_bytes(big_data)
            print("PASS 6: Paralela 4 hilos y fallback a secuencial si no soporta Range")

            # 7. Redirección interna (302 dentro de s3.todus.cu) y bloqueo de redirección externa
            p7 = udyat.download_one(
                f"{base}/redirect_ok#filename=redir.bin",
                out_dir=out_dir, free_ip="127.0.0.1",
            )
            assert _sha256_file(p7) == _sha256_bytes(small_data)
            try:
                udyat.download_one(
                    f"{base}/redirect_external#filename=ext.bin",
                    out_dir=out_dir, free_ip="127.0.0.1",
                )
                raise AssertionError("redirección externa debía bloquearse")
            except udyat.DownloadError:
                pass
            print("PASS 7: Redirección interna OK y redirección a host ajeno bloqueada")

            # 8. Lote enlaces.txt: RFC 5987 Content-Disposition + URL opaca con Content-Type video/mp4
            batch_txt = os.path.join(tmp, "enlaces_20261106_1200.txt")
            batch_dir = os.path.join(tmp, "batch_out")
            with open(batch_txt, "w", encoding="utf-8") as fh:
                fh.write("# comentario ignorado\n\n")
                fh.write(f"{base}/disp_utf8\n")
                fh.write(f"{base}/stream/todus/file/deadbeef0123456789abcdef01234567\n")
            rc = udyat.main(["--ip", "127.0.0.1", "-d", batch_dir, batch_txt])
            assert rc == 0, rc
            files = sorted(os.listdir(batch_dir))
            assert "Canción Final.mp3" in files, files
            mp4s = [f for f in files if f.endswith(".mp4")]
            assert len(mp4s) == 1, files
            assert _sha256_file(os.path.join(batch_dir, mp4s[0])) == _sha256_bytes(opaque_data)
            print("PASS 8: Lote enlaces.txt (RFC 5987 filename* + extensión por Content-Type)")

            # 9. Bloqueo de host ajeno (protege datos móviles), fallback 405 HEAD y --test preflight
            try:
                udyat.download_one("https://example.com/archivo.zip", out_dir=out_dir)
                raise AssertionError("example.com debía bloquearse sin --allow-direct")
            except udyat.DownloadError as e:
                assert "no es s3.todus.cu" in str(e)

            p9 = udyat.download_one(
                f"{base}/nohead#filename=sinhead.bin",
                out_dir=out_dir, free_ip="127.0.0.1",
            )
            assert _sha256_file(p9) == _sha256_bytes(small_data)

            rc_test = udyat.run_preflight_test(
                ["127.0.0.1"], user_agent="Wget/1.21.3", timeout=3.0, port=port
            )
            assert rc_test == 0

            rc_diag = udyat.run_diagnose(
                f"{base}/only_okhttp",
                free_ip="127.0.0.1", backup_ip="127.0.0.1",
                timeout=3.0, include_direct_dns=False,
            )
            assert rc_diag == 0
            print("PASS 9: Guardia anti-consumo + fallback 405 HEAD + preflight + diagnose OK")
        finally:
            srv.shutdown()
            srv.server_close()

    print("\nALL 9 TESTS PASS")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
