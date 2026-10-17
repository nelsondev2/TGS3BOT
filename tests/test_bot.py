"""Tests de las partes críticas del bot (sin red de Telegram ni S3)."""
import asyncio
import datetime
import os
import shutil
import socket
import tempfile
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

import aiohttp
from aiohttp import web

# El módulo crea DOWNLOAD_PATH al importarse; usamos un directorio temporal.
_TEST_DOWNLOAD = tempfile.mkdtemp(prefix="tgs3bot-test-")
os.environ["DOWNLOAD_PATH"] = _TEST_DOWNLOAD
os.environ.setdefault("TG_API_ID", "1")
os.environ.setdefault("TG_API_HASH", "test-hash")

import bot  # noqa: E402


class TestFilenameYLimites(unittest.TestCase):
    def test_sanitize_quita_path_traversal(self):
        self.assertEqual(bot.sanitize_filename("../../etc/passwd"), "passwd")
        self.assertEqual(bot.sanitize_filename("..\\..\\windows\\system.ini"), "system.ini")

    def test_sanitize_trunca_sin_perder_extension(self):
        name = bot.sanitize_filename("a" * 500 + ".mp4")
        self.assertTrue(name.endswith(".mp4"))
        self.assertLessEqual(len(name.encode("utf-8")), 180)

    def test_sanitize_unicode_no_excede_bytes(self):
        name = bot.sanitize_filename("ñ" * 300 + ".mp4")
        self.assertTrue(name.endswith(".mp4"))
        self.assertLessEqual(len(name.encode("utf-8")), 180)

    def test_sanitize_vacio(self):
        self.assertTrue(bot.sanitize_filename("...").startswith("file_"))

    def test_trim_url(self):
        self.assertEqual(
            bot._trim_url("https://example.com/a.mp4).,"),
            "https://example.com/a.mp4",
        )

    def test_filename_desde_url(self):
        self.assertEqual(
            bot.get_filename_from_url("https://example.com/a%20b.mp4?x=1"),
            "a b.mp4",
        )

    def test_sin_limite_por_defecto(self):
        self.assertEqual(bot.MAX_FILE_SIZE, 0)
        self.assertFalse(bot._size_limit_exceeded(10 ** 15))

    def test_content_type(self):
        self.assertEqual(bot.guess_content_type("video.mkv"), "video/x-matroska")
        self.assertEqual(bot.guess_content_type("foto.jpg"), "image/jpeg")
        self.assertEqual(
            bot.guess_content_type("desconocido.zzz"), "application/octet-stream"
        )

    def test_content_range(self):
        self.assertEqual(
            bot._parse_content_range("bytes 0-99/1000"), (0, 99, 1000)
        )
        self.assertEqual(
            bot._parse_content_range("bytes 100-199/*"), (100, 199, None)
        )
        self.assertIsNone(bot._parse_content_range(""))

    def test_disco_usa_tamano_real(self):
        with patch.object(bot, "_free_bytes", return_value=1024 ** 3):
            # 10 MB caben de sobra en 1 GB (antes se exigían ~4 GB).
            bot.check_disk_space(10 * 1024 ** 2)
            with self.assertRaises(RuntimeError):
                bot.check_disk_space(2 * 1024 ** 3)


class TestSSRF(unittest.IsolatedAsyncioTestCase):
    async def test_bloquea_ips_no_globales(self):
        malas = [
            "http://127.0.0.1/x",
            "http://2130706433/x",
            "http://0x7f000001/x",
            "http://[::1]/x",
            "http://localhost/x",
            "http://100.64.0.1/x",
            "http://198.18.0.1/x",
            "http://169.254.169.254/latest/meta-data/",
            "ftp://example.com/x",
        ]
        for url in malas:
            ok, _err = await bot._is_safe_url(url)
            self.assertFalse(ok, url)

    async def test_permite_ip_publica(self):
        ok, err = await bot._is_safe_url("http://8.8.8.8/x")
        self.assertTrue(ok, err)


class TestMediaYUI(unittest.TestCase):
    def test_webpage_no_es_media_descargable(self):
        from telethon.tl.types import MessageMediaWebPage, WebPage

        webpage = WebPage(
            id=0,
            url="https://example.com/video.mp4",
            display_url="example.com",
            hash=0,
            type="video",
            site_name="Example",
        )
        msg = SimpleNamespace(media=MessageMediaWebPage(webpage=webpage))
        self.assertFalse(bot._is_downloadable_media(msg))

    def test_documento_es_media_descargable(self):
        from telethon.tl.types import Document, MessageMediaDocument

        doc = Document(
            id=1,
            access_hash=0,
            file_reference=b"",
            date=datetime.datetime.now(),
            mime_type="video/mp4",
            size=123,
            dc_id=1,
            attributes=[],
        )
        msg = SimpleNamespace(media=MessageMediaDocument(document=doc))
        self.assertTrue(bot._is_downloadable_media(msg))

    def test_sin_media(self):
        self.assertFalse(bot._is_downloadable_media(SimpleNamespace(media=None)))

    def test_bot_output_y_comandos(self):
        card = bot._render_result_card(
            "archivo", "mp4", 1024, "https://s3.example/stream/x.mp4"
        )
        self.assertTrue(bot._looks_like_bot_output(card))
        self.assertTrue(bot._looks_like_bot_output("⏳ Procesando..."))
        self.assertTrue(bot._looks_like_command("/cancel@MiBot"))
        self.assertTrue(bot._looks_like_command("hola"))
        self.assertFalse(bot._looks_like_command("https://example.com/a.mp4"))


class TestCola(unittest.IsolatedAsyncioTestCase):
    async def test_cola_serializa_por_usuario(self):
        queue = bot.UserJobQueue()
        order = []

        async def make(n):
            order.append(f"start{n}")
            await asyncio.sleep(0.05)
            order.append(f"end{n}")

        job1 = bot.QueuedJob(1, "url")
        job2 = bot.QueuedJob(1, "url")
        self.assertTrue(await queue.submit(job1, lambda: make(1)))
        self.assertFalse(await queue.submit(job2, lambda: make(2)))
        await asyncio.sleep(0.25)
        self.assertEqual(order, ["start1", "end1", "start2", "end2"])
        self.assertFalse(queue.has_user_job(1))

    async def test_cancelar_usuario(self):
        queue = bot.UserJobQueue()
        started = asyncio.Event()

        async def slow():
            started.set()
            await asyncio.sleep(10)

        job1 = bot.QueuedJob(1, "url")
        job2 = bot.QueuedJob(1, "url")
        await queue.submit(job1, slow)
        await queue.submit(job2, slow)
        await started.wait()
        self.assertTrue(await queue.cancel_user(1))
        await asyncio.sleep(0.1)
        self.assertFalse(queue.has_user_job(1))


class TestSafeRequest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self._ssrf_patch = patch.object(bot, "SSRF_BLOCK_PRIVATE", False)
        self._ssrf_patch.start()
        self.app = web.Application()
        self.app.router.add_get("/redir", self._redir)
        self.app.router.add_get("/target", self._target)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self._ssrf_patch.stop()

    async def _redir(self, request):
        raise web.HTTPFound("/target")

    async def _target(self, request):
        return web.Response(text="ok")

    async def test_sigue_redirect_valido(self):
        async with aiohttp.ClientSession() as session:
            async with bot._safe_request(session, "GET", self.base + "/redir") as resp:
                self.assertEqual(resp.status, 200)
                self.assertTrue(str(resp.url).endswith("/target"))

    async def test_bloquea_redirect_a_url_no_permitida(self):
        async def fake_validate(url):
            if url.endswith("/target"):
                return False, "bloqueada", "127.0.0.1", None
            return True, None, "127.0.0.1", None

        with patch.object(bot, "_validate_url", side_effect=fake_validate):
            async with aiohttp.ClientSession() as session:
                with self.assertRaises(bot.SafeRequestError):
                    async with bot._safe_request(
                        session, "GET", self.base + "/redir"
                    ):
                        pass


class TestDescargaParalela(unittest.IsolatedAsyncioTestCase):
    PART = 10 * 1024 * 1024
    TOTAL = PART * 4

    async def asyncSetUp(self):
        self._ssrf_patch = patch.object(bot, "SSRF_BLOCK_PRIVATE", False)
        self._ssrf_patch.start()

        self.app = web.Application()
        self.app.router.add_get("/range", self._range_handler)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.url = f"http://127.0.0.1:{port}/range"

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self._ssrf_patch.stop()
        shutil.rmtree(_TEST_DOWNLOAD, ignore_errors=True)
        os.makedirs(_TEST_DOWNLOAD, exist_ok=True)

    async def _range_handler(self, request):
        header = request.headers.get("Range", "")
        start = int(header.split("=")[1].split("-")[0])
        idx = start // self.PART
        if idx == 1:
            # Simula un servidor que no soporta Range en una de las partes,
            # pero deja tiempo a que las demás escriban sus .part.
            await asyncio.sleep(0.5)
            return web.Response(status=200, body=b"nope")
        return web.Response(
            status=206,
            body=b"B" * self.PART,
            headers={
                "Content-Range": f"bytes {start}-{start + self.PART - 1}/{self.TOTAL}"
            },
        )

    async def test_no_deja_partes_huerfanas(self):
        job = bot.QueuedJob(1, "url")
        temp_path = os.path.join(_TEST_DOWNLOAD, f"{uuid.uuid4().hex}.bin")
        with patch.object(bot, "check_disk_space", return_value=0):
            async with aiohttp.ClientSession() as session:
                with self.assertRaises(bot.RangeNotSupported):
                    await bot._download_parallel(
                        session, self.url, temp_path, self.TOTAL, job
                    )
        # Las tareas hermanas deben haberse cancelado y limpiado.
        await asyncio.sleep(2.0)
        leftovers = [
            name for name in os.listdir(_TEST_DOWNLOAD) if ".part" in name
        ]
        self.assertEqual(leftovers, [])


if __name__ == "__main__":
    unittest.main()


class TestProcessURL(unittest.IsolatedAsyncioTestCase):
    PAYLOAD = b"\x89PNG\r\n\x1a\n" + b"x" * 4096

    async def asyncSetUp(self):
        self._ssrf_patch = patch.object(bot, "SSRF_BLOCK_PRIVATE", False)
        self._ssrf_patch.start()
        self.app = web.Application()
        self.app.router.add_get("/archivo.bin", self._file)
        self.app.router.add_get("/descarga", self._no_ext)
        self.runner = web.AppRunner(self.app)
        await self.runner.setup()
        self.site = web.TCPSite(self.runner, "127.0.0.1", 0)
        await self.site.start()
        port = self.site._server.sockets[0].getsockname()[1]
        self.base = f"http://127.0.0.1:{port}"

    async def asyncTearDown(self):
        await self.runner.cleanup()
        self._ssrf_patch.stop()

    async def _file(self, request):
        return web.Response(body=b"contenido-real", content_type="application/octet-stream")

    async def _no_ext(self, request):
        return web.Response(body=self.PAYLOAD, content_type="image/png")

    async def test_url_descarga_sube_y_limpia(self):
        job = bot.QueuedJob(
            user_id=1, kind="url", url=self.base + "/archivo.bin",
            chat_id=1, status_msg=object(),
        )
        captured = {}

        async def fake_upload(temp_path, filename, size):
            with open(temp_path, "rb") as fh:
                captured["data"] = fh.read()
            captured["filename"] = filename
            captured["size"] = size
            return "https://s3.example/stream/x_archivo.bin"

        edit = unittest.mock.AsyncMock(return_value=None)
        with patch.object(bot, "tg_edit_status", new=edit), \
             patch.object(bot, "subir_a_s3", new=fake_upload):
            await bot._process_url(job)

        self.assertEqual(captured["data"], b"contenido-real")
        self.assertEqual(captured["filename"], "archivo.bin")
        self.assertEqual(captured["size"], len(b"contenido-real"))
        self.assertEqual(job.temp_paths, [])
        leftovers = [
            n for n in os.listdir(_TEST_DOWNLOAD)
            if os.path.isfile(os.path.join(_TEST_DOWNLOAD, n))
        ]
        self.assertEqual(leftovers, [])
        self.assertTrue(
            any("s3.example" in str(call) for call in edit.await_args_list)
        )

    async def test_detecta_extension_desde_content_type(self):
        job = bot.QueuedJob(
            user_id=1, kind="url", url=self.base + "/descarga",
            chat_id=1, status_msg=object(),
        )
        captured = {}

        async def fake_upload(temp_path, filename, size):
            with open(temp_path, "rb") as fh:
                captured["data"] = fh.read()
            captured["filename"] = filename
            return "https://s3.example/stream/x"

        edit = unittest.mock.AsyncMock(return_value=None)
        with patch.object(bot, "tg_edit_status", new=edit), \
             patch.object(bot, "subir_a_s3", new=fake_upload):
            await bot._process_url(job)

        self.assertEqual(captured["data"], self.PAYLOAD)
        self.assertTrue(captured["filename"].endswith(".png"))


class TestPinnedResolver(unittest.IsolatedAsyncioTestCase):
    async def test_usa_ip_fijada(self):
        import ipaddress

        resolver = bot.PinnedResolver()
        resolver.pin("example.test", [ipaddress.ip_address("8.8.8.8")])
        records = await resolver.resolve("example.test", 443)
        self.assertEqual(records[0]["host"], "8.8.8.8")
        self.assertEqual(records[0]["family"], socket.AF_INET)

    async def test_fallback_al_sistema(self):
        resolver = bot.PinnedResolver()
        records = await resolver.resolve("localhost", 80)
        self.assertTrue(records)
        self.assertIn(records[0]["host"], ("127.0.0.1", "::1"))
