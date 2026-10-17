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

    def test_solo_sesiones_de_bot(self):
        self.assertTrue(bot._session_is_bot(SimpleNamespace(bot=True)))
        self.assertFalse(bot._session_is_bot(SimpleNamespace(bot=False)))
        self.assertFalse(bot._session_is_bot(SimpleNamespace()))

    def test_help_solo_enlace_y_consumo(self):
        text = bot._render_help()
        self.assertIn("no reenvío el archivo", text)
        self.assertIn("Descargarlo consume datos de toDus", text)
        self.assertNotIn("/sendfile", text)
        self.assertNotIn("ahorra datos", text)
        self.assertFalse(hasattr(bot, "SEND_FILE_VIA_TG"))
        self.assertFalse(hasattr(bot, "_send_file_via_telegram"))

    def test_result_card_y_comandos(self):
        card = bot._render_result_card(
            "archivo", "mp4", 1024, "https://s3.example/stream/x.mp4"
        )
        self.assertIn("https://s3.example/stream/x.mp4", card)
        self.assertIn("1.0 KB", card)
        # /cmd@Bot se normaliza al comando sin sufijo.
        self.assertEqual(bot._command_of("/cancel@MiBot"), "/cancel")
        self.assertEqual(bot._command_of("  /Start "), "/start")
        self.assertEqual(bot._command_of("hola"), "hola")


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


class _FakeEvent:
    def __init__(self, text):
        self.chat_id = 12345
        self.raw_text = text
        self.message = SimpleNamespace(raw_text=text, media=None, out=False, id=1)
        self.replies = []

    async def get_sender(self):
        return SimpleNamespace(id=12345, username="tester", first_name="Test")

    async def respond(self, text):
        self.replies.append(text)
        return None


class TestSoloEnlace(unittest.IsolatedAsyncioTestCase):
    async def test_allowlist_bloquea_a_otros(self):
        with patch.object(bot, "ALLOWED_TG_IDS", {999}):
            event = _FakeEvent("/status")
            await bot.on_tg_message(event)
        self.assertIn("No estás autorizado", event.replies[-1])

    async def test_sendfile_y_textonly_ya_no_existen(self):
        with patch.object(bot, "ALLOWED_TG_IDS", None):
            for texto in ("/sendfile", "/textonly"):
                event = _FakeEvent(texto)
                await bot.on_tg_message(event)
                self.assertIn("Envía un enlace", event.replies[-1])

    async def test_deliver_solo_envia_la_url(self):
        temp_path = os.path.join(_TEST_DOWNLOAD, "entrega.bin")
        with open(temp_path, "wb") as f:
            f.write(b"x" * 16)
        job = bot.QueuedJob(
            user_id=5, kind="url", url="https://example.com/x",
            chat_id=5, status_msg=object(),
        )
        edit = unittest.mock.AsyncMock(return_value=None)
        client = SimpleNamespace(send_file=unittest.mock.AsyncMock())

        async def fake_upload(path, filename, size):
            return "https://s3.example/stream/x.bin"

        with patch.object(bot, "tg_edit_status", new=edit), \
             patch.object(bot, "subir_a_s3", new=fake_upload), \
             patch.object(bot, "_tg_client", client):
            await bot._deliver(job, temp_path, "x.bin", "web")

        client.send_file.assert_not_awaited()
        card = edit.await_args_list[-1].args[2]
        self.assertIn("https://s3.example/stream/x.bin", card)
        self.assertIn("Subido a S3", card)
        os.unlink(temp_path)


class _FakeTelegramClient:
    def __init__(self, *args, **kwargs):
        self.handlers = []
        self.disconnected = False
        self.me = SimpleNamespace(id=42, bot=True, username="mi_bot")

    async def start(self, *args, **kwargs):
        return self

    async def get_me(self):
        return self.me

    async def disconnect(self):
        self.disconnected = True

    def add_event_handler(self, *args, **kwargs):
        self.handlers.append(args)

    def is_connected(self):
        return True


class TestMainSoloModoBot(unittest.IsolatedAsyncioTestCase):
    def _patches(self, client):
        runner = SimpleNamespace(cleanup=unittest.mock.AsyncMock())
        shutdown = asyncio.Event()
        shutdown.set()
        return [
            patch.object(bot, "TelegramClient", return_value=client),
            patch.object(bot, "StringSession", side_effect=lambda value: value),
            patch.object(bot, "TG_SESSION_STRING", "sesion-de-prueba"),
            patch.object(bot, "TG_BOT_TOKEN", ""),
            patch.object(bot, "WATCHDOG_ENABLED", False),
            patch.object(bot, "_setup_signal_handlers", lambda: None),
            patch.object(bot, "_shutdown_event", shutdown),
            patch.object(bot, "run_web", new=unittest.mock.AsyncMock(return_value=runner)),
        ]

    async def test_rechaza_sesion_de_cuenta_personal(self):
        client = _FakeTelegramClient()
        client.me = SimpleNamespace(id=42, bot=False, username="persona")
        patchers = self._patches(client)
        for p in patchers:
            p.start()
        try:
            with self.assertRaises(SystemExit) as ctx:
                await bot.main()
        finally:
            for p in patchers:
                p.stop()

        self.assertEqual(ctx.exception.code, 1)
        self.assertEqual(client.handlers, [])
        self.assertTrue(client.disconnected)

    async def test_arranca_con_sesion_de_bot(self):
        client = _FakeTelegramClient()
        patchers = self._patches(client)
        for p in patchers:
            p.start()
        try:
            await bot.main()
        finally:
            for p in patchers:
                p.stop()

        self.assertEqual(len(client.handlers), 1)
        self.assertTrue(client.disconnected)
