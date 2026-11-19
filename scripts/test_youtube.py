"""Pruebas unitarias e integración local para el soporte de YouTube en bot.py.

Uso:
    python scripts/test_youtube.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import bot


class TestYoutubeHelpers(unittest.TestCase):
    def test_is_youtube_url(self):
        self.assertTrue(bot._is_youtube_url("https://www.youtube.com/watch?v=dQw4w9WgXcQ"))
        self.assertTrue(bot._is_youtube_url("https://youtu.be/dQw4w9WgXcQ"))
        self.assertTrue(bot._is_youtube_url("https://m.youtube.com/watch?v=dQw4w9WgXcQ"))
        self.assertTrue(bot._is_youtube_url("https://youtube.com/shorts/abc123XYZ"))
        self.assertTrue(bot._is_youtube_url("https://music.youtube.com/watch?v=abc123"))
        self.assertFalse(bot._is_youtube_url("https://youtu.be/"))
        self.assertFalse(bot._is_youtube_url("https://example.com/watch?v=123"))
        self.assertFalse(bot._is_youtube_url("https://notyoutube.com/watch?v=123"))
        self.assertFalse(bot._is_youtube_url("ftp://youtube.com/watch?v=123"))

    def test_is_youtube_collection_url(self):
        self.assertTrue(bot._is_youtube_collection_url("https://www.youtube.com/playlist?list=PL123"))
        self.assertTrue(bot._is_youtube_collection_url("https://www.youtube.com/@somechannel"))
        self.assertTrue(bot._is_youtube_collection_url("https://www.youtube.com/"))
        self.assertFalse(bot._is_youtube_collection_url("https://www.youtube.com/watch?v=abc&list=PL123"))
        self.assertFalse(bot._is_youtube_collection_url("https://youtu.be/abc123"))

    def test_normalize_quality(self):
        self.assertEqual(bot._normalize_yt_quality("720p"), "720")
        self.assertEqual(bot._normalize_yt_quality("1080P"), "1080")
        self.assertEqual(bot._normalize_yt_quality("mp3"), "audio")
        self.assertEqual(bot._normalize_yt_quality("audio"), "audio")
        self.assertEqual(bot._normalize_yt_quality("max"), "best")
        self.assertEqual(bot._normalize_yt_quality("best"), "best")
        self.assertIsNone(bot._normalize_yt_quality("4k"))
        self.assertIsNone(bot._normalize_yt_quality(""))

    def test_build_format_opts(self):
        fmt, pps, merge, ext = bot._build_yt_format_opts("720", True)
        self.assertIn("height<=720", fmt)
        self.assertEqual(pps, [])
        self.assertEqual(merge, "mp4")
        self.assertEqual(ext, ".mp4")

        fmt, pps, merge, ext = bot._build_yt_format_opts("720", False)
        self.assertNotIn("+", fmt)
        self.assertIsNone(merge)
        self.assertEqual(ext, ".mp4")

        fmt, pps, merge, ext = bot._build_yt_format_opts("audio", True)
        self.assertEqual(ext, ".mp3")
        self.assertEqual(pps[0]["preferredcodec"], "mp3")

        fmt, pps, merge, ext = bot._build_yt_format_opts("audio", False)
        self.assertEqual(ext, ".m4a")
        self.assertEqual(pps, [])

    def test_parse_yt_command(self):
        url, q = bot._parse_yt_command("/yt https://youtu.be/abc123")
        self.assertEqual(url, "https://youtu.be/abc123")
        self.assertIsNone(q)

        url, q = bot._parse_yt_command("/yt 1080p https://youtu.be/abc123")
        self.assertEqual(url, "https://youtu.be/abc123")
        self.assertEqual(q, "1080")

        url, q = bot._parse_yt_command("/ytmp3 https://www.youtube.com/watch?v=abc123")
        self.assertEqual(url, "https://www.youtube.com/watch?v=abc123")
        self.assertEqual(q, "audio")

        url, err = bot._parse_yt_command("/yt 999p https://youtu.be/abc123")
        self.assertIsNone(url)
        self.assertIn("Calidad no válida", err)

        url, err = bot._parse_yt_command("/yt https://example.com/file.mp4")
        self.assertIsNone(url)
        self.assertIn("YouTube válido", err)

    def test_find_ffmpeg(self):
        exe = bot._find_ffmpeg()
        self.assertIsNotNone(exe)
        self.assertTrue(os.path.isfile(exe))

    def test_find_deno(self):
        exe = bot._find_deno()
        self.assertIsNotNone(exe)
        self.assertTrue(os.path.isfile(exe))

    def test_human_error(self):
        msg = bot._yt_human_error(RuntimeError("ERROR: [youtube] Sign in to confirm you're not a bot"))
        self.assertIn("/ytcookies", msg)
        msg = bot._yt_human_error(RuntimeError("ERROR: [youtube] Private video"))
        self.assertIn("privado", msg)

    def test_validate_netscape_cookies(self):
        ok, err, cnt = bot._validate_netscape_cookies("texto cualquiera sin tabs")
        self.assertFalse(ok)
        self.assertEqual(cnt, 0)

        ok, err, cnt = bot._validate_netscape_cookies(".example.com\tTRUE\t/\tTRUE\t0\tfoo\tbar\n")
        self.assertFalse(ok)
        self.assertIn("youtube.com", err)

        sample = ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tVISITOR_INFO1_LIVE\tabc123\n"
        ok, cleaned, cnt = bot._validate_netscape_cookies(sample)
        self.assertTrue(ok)
        self.assertEqual(cnt, 1)
        self.assertIn("# Netscape HTTP Cookie File", cleaned)


class TestProcessYoutubeAsync(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_yt_")
        self.old_dl_path = bot.DOWNLOAD_PATH
        bot.DOWNLOAD_PATH = self.tmpdir
        self.edits = []

    async def asyncTearDown(self):
        bot.DOWNLOAD_PATH = self.old_dl_path
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def _fake_edit_status(self, chat_id, msg, text):
        self.edits.append(text)
        return msg or object()

    async def _fake_edit_quiet(self, chat_id, msg, text):
        self.edits.append(text)
        return msg

    async def test_process_youtube_success_and_cleanup(self):
        class FakeYDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def prepare_filename(self, info):
                out_dir = os.path.dirname(self.opts["outtmpl"])
                return os.path.join(out_dir, f"{info['id']}.{info['ext']}")

            def extract_info(self, url, download=True):
                info = {
                    "id": "vid123",
                    "title": "Mi Video de Prueba!",
                    "ext": "mp4",
                    "filesize": 1024,
                    "is_live": False,
                }
                mf = self.opts.get("match_filter")
                if mf:
                    res = mf(info, incomplete=False)
                    if res:
                        return info
                out_path = self.prepare_filename(info)
                with open(out_path, "wb") as f:
                    f.write(b"0" * 1024)
                for ph in self.opts.get("progress_hooks", []):
                    ph({"status": "downloading", "downloaded_bytes": 512, "total_bytes": 1024})
                    ph({"status": "downloading", "downloaded_bytes": 1024, "total_bytes": 1024})
                    ph({"status": "finished", "filename": out_path})
                return info

        uploaded = []

        async def fake_subir(temp_path, filename, size, **kwargs):
            uploaded.append((temp_path, filename, size, os.path.isfile(temp_path)))
            return "https://s3.todus.cu/stream/fake/Mi_Video_de_Prueba!.mp4", [], False

        job = bot.QueuedJob(
            user_id=42, kind="youtube",
            url="https://www.youtube.com/watch?v=vid123",
            chat_id=42, status_msg=object(), yt_quality="720",
        )

        with patch.object(bot, "_is_safe_url", return_value=(True, None)), \
             patch.object(bot, "tg_edit_status", side_effect=self._fake_edit_status), \
             patch.object(bot, "tg_edit_quiet", side_effect=self._fake_edit_quiet), \
             patch.object(bot, "subir_a_s3", side_effect=fake_subir), \
             patch("yt_dlp.YoutubeDL", FakeYDL):
            await bot.process_job(job)

        self.assertEqual(len(uploaded), 1)
        self.assertEqual(uploaded[0][1], "Mi_Video_de_Prueba!.mp4")
        self.assertEqual(uploaded[0][2], 1024)
        self.assertTrue(uploaded[0][3])
        self.assertEqual(job.result_url, "https://s3.todus.cu/stream/fake/Mi_Video_de_Prueba!.mp4")
        self.assertEqual(job.result_name, "Mi_Video_de_Prueba!.mp4")
        self.assertTrue(any("YouTube" in e and "Subido a S3" in e for e in self.edits))
        # El directorio temporal debe haberse limpiado
        self.assertEqual(os.listdir(self.tmpdir), [])

    async def test_process_youtube_fallback_clients_on_bot_error(self):
        seen_opts = []

        class FallbackYDL:
            def __init__(self, opts):
                self.opts = opts
                seen_opts.append(opts)

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def prepare_filename(self, info):
                out_dir = os.path.dirname(self.opts["outtmpl"])
                return os.path.join(out_dir, f"{info['id']}.{info['ext']}")

            def extract_info(self, url, download=True):
                if "extractor_args" not in self.opts:
                    raise RuntimeError("ERROR: [youtube] Sign in to confirm you’re not a bot")
                info = {
                    "id": "fb123",
                    "title": "Fallback OK",
                    "ext": "mp4",
                    "filesize": 512,
                    "is_live": False,
                }
                self.opts["match_filter"](info, incomplete=False)
                out_path = self.prepare_filename(info)
                with open(out_path, "wb") as f:
                    f.write(b"1" * 512)
                return info

        async def fake_subir(temp_path, filename, size, **kwargs):
            return "https://s3.todus.cu/stream/fake/Fallback_OK.mp4", [], False

        job = bot.QueuedJob(
            user_id=7, kind="youtube",
            url="https://youtu.be/fb123",
            chat_id=7, status_msg=object(), yt_quality="720",
        )
        old_synced = bot._yt_cookies_env_synced
        old_disabled = bot._yt_cookies_disabled_by_admin
        bot._yt_cookies_env_synced = True
        bot._yt_cookies_disabled_by_admin = True
        try:
            with patch.object(bot, "_is_safe_url", return_value=(True, None)), \
                 patch.object(bot, "tg_edit_status", side_effect=self._fake_edit_status), \
                 patch.object(bot, "tg_edit_quiet", side_effect=self._fake_edit_quiet), \
                 patch.object(bot, "subir_a_s3", side_effect=fake_subir), \
                 patch("yt_dlp.YoutubeDL", FallbackYDL):
                await bot.process_job(job)
        finally:
            bot._yt_cookies_env_synced = old_synced
            bot._yt_cookies_disabled_by_admin = old_disabled

        self.assertEqual(len(seen_opts), 2)
        self.assertNotIn("extractor_args", seen_opts[0])
        self.assertIn("extractor_args", seen_opts[1])
        self.assertEqual(job.result_url, "https://s3.todus.cu/stream/fake/Fallback_OK.mp4")

    async def test_ytcookies_b64_and_telegram_command(self):
        import base64
        sample = ".youtube.com\tTRUE\t/\tTRUE\t1893456000\tSID\txyz\n"
        b64 = base64.b64encode(sample.encode()).decode()

        old_b64 = bot.YT_COOKIES_B64
        old_synced = bot._yt_cookies_env_synced
        old_disabled = bot._yt_cookies_disabled_by_admin
        old_allowed = bot.ALLOWED_TG_IDS
        old_admin = bot.ADMIN_TG_IDS
        old_userbot = bot._is_userbot_mode
        bot.YT_COOKIES_B64 = b64
        bot._yt_cookies_env_synced = False
        bot._yt_cookies_disabled_by_admin = False
        bot.ALLOWED_TG_IDS = None
        bot.ADMIN_TG_IDS = {999}
        bot._is_userbot_mode = False
        try:
            p = bot._resolve_yt_cookiefile()
            self.assertIsNotNone(p)
            self.assertTrue(os.path.isfile(p))

            class FakeSender:
                id = 999
                username = "admin"
                first_name = "Admin"

            class FakeMsg:
                media = None

            class FakeEvent:
                def __init__(self, text):
                    self.chat_id = 999
                    self.raw_text = text
                    self.message = FakeMsg()
                    self.is_private = True
                    self.replies = []

                async def get_sender(self):
                    return FakeSender()

                async def respond(self, text, **kwargs):
                    self.replies.append(text)

            ev_st = FakeEvent("/ytcookies status")
            await bot.on_tg_message(ev_st)
            self.assertTrue(any("activas" in r for r in ev_st.replies))

            ev_b64 = FakeEvent("/ytcookies b64")
            await bot.on_tg_message(ev_b64)
            self.assertTrue(any(b64 in r for r in ev_b64.replies))

            ev_clr = FakeEvent("/ytcookies clear")
            await bot.on_tg_message(ev_clr)
            self.assertIsNone(bot._resolve_yt_cookiefile())
        finally:
            bot.YT_COOKIES_B64 = old_b64
            bot._yt_cookies_env_synced = old_synced
            bot._yt_cookies_disabled_by_admin = old_disabled
            bot.ALLOWED_TG_IDS = old_allowed
            bot.ADMIN_TG_IDS = old_admin
            bot._is_userbot_mode = old_userbot
            bot._yt_pending_cookies.clear()

    async def test_process_youtube_rejects_live_and_playlist(self):
        job_pl = bot.QueuedJob(
            user_id=1, kind="youtube",
            url="https://www.youtube.com/playlist?list=PL123",
            chat_id=1, status_msg=object(),
        )
        with patch.object(bot, "_is_safe_url", return_value=(True, None)), \
             patch.object(bot, "tg_edit_status", side_effect=self._fake_edit_status):
            await bot.process_job(job_pl)
        self.assertTrue(any("listas" in e.lower() for e in self.edits))
        self.assertIsNone(job_pl.result_url)

        self.edits.clear()

        class LiveYDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=True):
                info = {"id": "live1", "title": "Live", "ext": "mp4", "is_live": True}
                self.opts["match_filter"](info, incomplete=False)
                return info

        job_live = bot.QueuedJob(
            user_id=1, kind="youtube",
            url="https://www.youtube.com/watch?v=live1",
            chat_id=1, status_msg=object(),
        )
        with patch.object(bot, "_is_safe_url", return_value=(True, None)), \
             patch.object(bot, "tg_edit_status", side_effect=self._fake_edit_status), \
             patch.object(bot, "tg_edit_quiet", side_effect=self._fake_edit_quiet), \
             patch("yt_dlp.YoutubeDL", LiveYDL):
            await bot.process_job(job_live)
        self.assertTrue(any("en vivo" in e.lower() for e in self.edits))
        self.assertIsNone(job_live.result_url)

    async def test_process_youtube_cancel_mid_download(self):
        class CancelYDL:
            def __init__(self, opts):
                self.opts = opts

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def extract_info(self, url, download=True):
                info = {"id": "c1", "title": "Cancel", "ext": "mp4", "is_live": False}
                self.opts["match_filter"](info, incomplete=False)
                job.cancel_requested = True
                for ph in self.opts["progress_hooks"]:
                    ph({"status": "downloading", "downloaded_bytes": 10, "total_bytes": 100})
                return info

        job = bot.QueuedJob(
            user_id=1, kind="youtube",
            url="https://youtu.be/c1",
            chat_id=1, status_msg=object(),
        )
        with patch.object(bot, "_is_safe_url", return_value=(True, None)), \
             patch.object(bot, "tg_edit_status", side_effect=self._fake_edit_status), \
             patch.object(bot, "tg_edit_quiet", side_effect=self._fake_edit_quiet), \
             patch("yt_dlp.YoutubeDL", CancelYDL):
            with self.assertRaises(asyncio.CancelledError):
                await bot.process_job(job)

        self.assertTrue(any("Cancelado" in e for e in self.edits))
        self.assertEqual(os.listdir(self.tmpdir), [])

    async def test_on_tg_message_youtube_routing_and_commands(self):
        class FakeSender:
            def __init__(self, uid):
                self.id = uid
                self.username = "tester"
                self.first_name = "Tester"

        class FakeMessage:
            media = None

        class FakeEvent:
            def __init__(self, text, uid=999):
                self.chat_id = 999
                self.raw_text = text
                self.message = FakeMessage()
                self._uid = uid
                self.replies = []

            async def get_sender(self):
                return FakeSender(self._uid)

            async def respond(self, text):
                self.replies.append(text)

        enqueued = []

        async def fake_enqueue(job, factory):
            enqueued.append(job)

        async def fake_send_status(chat_id, text):
            return object()

        old_allowed = bot.ALLOWED_TG_IDS
        old_userbot = bot._is_userbot_mode
        old_ask = bot.YT_ASK_QUALITY
        bot.ALLOWED_TG_IDS = None
        bot._is_userbot_mode = False
        try:
            with patch.object(bot.job_queue, "enqueue", side_effect=fake_enqueue), \
                 patch.object(bot, "tg_send_status", side_effect=fake_send_status), \
                 patch.object(bot, "_is_safe_url", return_value=(True, None)):
                # 1a. URL directa de YouTube con YT_ASK_QUALITY=1 -> muestra botonera sin comando
                bot.YT_ASK_QUALITY = True
                bot._yt_pending_choices.clear()
                direct_kwargs = []
                ev1 = FakeEvent("mira esto https://youtu.be/dQw4w9WgXcQ")

                async def ev1_respond(text, **kwargs):
                    ev1.replies.append(text)
                    direct_kwargs.append(kwargs)

                ev1.respond = ev1_respond
                await bot.on_tg_message(ev1)
                self.assertEqual(len(enqueued), 0)
                self.assertEqual(len(bot._yt_pending_choices), 1)
                self.assertIn("buttons", direct_kwargs[0])
                bot._yt_pending_choices.clear()

                # 1b. URL directa de YouTube con YT_ASK_QUALITY=0 -> encola directo
                bot.YT_ASK_QUALITY = False
                ev1b = FakeEvent("mira esto https://youtu.be/dQw4w9WgXcQ")
                await bot.on_tg_message(ev1b)
                self.assertEqual(len(enqueued), 1)
                self.assertEqual(enqueued[0].kind, "youtube")
                self.assertEqual(enqueued[0].url, "https://youtu.be/dQw4w9WgXcQ")
                bot.YT_ASK_QUALITY = True

                # 1c. Enlace de YouTube con preview MessageMediaWebPage (ej. via @vid)
                from telethon.tl.types import MessageMediaWebPage, WebPageEmpty, MessageMediaPhoto, MessageMediaDocument
                bot._yt_pending_choices.clear()
                ev_wp = FakeEvent("https://www.youtube.com/watch?v=qnNMf3AaORY")
                ev_wp.message.media = MessageMediaWebPage(webpage=WebPageEmpty(id=1))
                ev_wp.message.via_bot_id = 123456
                wp_kwargs = []

                async def wp_respond(text, **kwargs):
                    ev_wp.replies.append(text)
                    wp_kwargs.append(kwargs)

                ev_wp.respond = wp_respond
                await bot.on_tg_message(ev_wp)
                self.assertEqual(len(enqueued), 1)  # NO encoló tg_media
                self.assertEqual(len(bot._yt_pending_choices), 1)
                self.assertIn("buttons", wp_kwargs[0])
                bot._yt_pending_choices.clear()

                # 1d. Enlace normal con preview MessageMediaWebPage -> kind="url"
                ev_wp_url = FakeEvent("https://example.com/archivo.zip")
                ev_wp_url.message.media = MessageMediaWebPage(webpage=WebPageEmpty(id=2))
                await bot.on_tg_message(ev_wp_url)
                self.assertEqual(len(enqueued), 2)
                self.assertEqual(enqueued[1].kind, "url")

                # 1e. Enlace de YouTube via bot inline con MessageMediaPhoto -> YouTube
                ev_vid_photo = FakeEvent("https://www.youtube.com/watch?v=qnNMf3AaORY")
                ev_vid_photo.message.media = MessageMediaPhoto()
                ev_vid_photo.message.via_bot_id = 123456
                vp_kwargs = []

                async def vp_respond(text, **kwargs):
                    ev_vid_photo.replies.append(text)
                    vp_kwargs.append(kwargs)

                ev_vid_photo.respond = vp_respond
                await bot.on_tg_message(ev_vid_photo)
                self.assertEqual(len(enqueued), 2)
                self.assertEqual(len(bot._yt_pending_choices), 1)
                self.assertIn("buttons", vp_kwargs[0])
                bot._yt_pending_choices.clear()

                # 1f. Documento real (MessageMediaDocument) -> sigue siendo tg_media
                ev_doc = FakeEvent("")
                ev_doc.message.media = MessageMediaDocument()
                await bot.on_tg_message(ev_doc)
                self.assertEqual(len(enqueued), 3)
                self.assertEqual(enqueued[2].kind, "tg_media")
                enqueued.pop()  # mantener índices siguientes de la prueba
                enqueued.pop()  # quitar también ev_wp_url para no desplazar índices

                # 2. URL normal -> kind="url"
                ev2 = FakeEvent("https://example.com/archivo.zip")
                await bot.on_tg_message(ev2)
                self.assertEqual(len(enqueued), 2)
                self.assertEqual(enqueued[1].kind, "url")

                # 3. /ytquality 1080
                ev3 = FakeEvent("/ytquality 1080")
                await bot.on_tg_message(ev3)
                self.assertEqual(bot._yt_user_quality.get(999), "1080")
                self.assertTrue(any("1080p" in r for r in ev3.replies))

                # 4. /ytmp3 <url>
                ev4 = FakeEvent("/ytmp3 https://www.youtube.com/watch?v=abc123")
                await bot.on_tg_message(ev4)
                self.assertEqual(len(enqueued), 3)
                self.assertEqual(enqueued[2].kind, "youtube")
                self.assertEqual(enqueued[2].yt_quality, "audio")

                # 5. /yt 480 <url> (calidad explícita -> directo sin botonera)
                ev5 = FakeEvent("/yt 480 https://youtube.com/shorts/xyz789")
                await bot.on_tg_message(ev5)
                self.assertEqual(len(enqueued), 4)
                self.assertEqual(enqueued[3].kind, "youtube")
                self.assertEqual(enqueued[3].yt_quality, "480")

                # 6. /yt <url> (sin calidad en modo bot -> muestra botonera)
                bot._yt_pending_choices.clear()
                responded_kwargs = []

                async def fake_respond(text, **kwargs):
                    ev6.replies.append(text)
                    responded_kwargs.append(kwargs)

                ev6 = FakeEvent("/yt https://youtu.be/dQw4w9WgXcQ")
                ev6.respond = fake_respond
                with patch.object(bot, "_is_safe_url", return_value=(True, None)):
                    await bot.on_tg_message(ev6)
                self.assertEqual(len(enqueued), 4)
                self.assertEqual(len(bot._yt_pending_choices), 1)
                self.assertIn("buttons", responded_kwargs[0])
                token = next(iter(bot._yt_pending_choices.keys()))

                # 7. Callback de otro usuario -> rechazado, no consume token
                class FakeCallbackEvent:
                    def __init__(self, data, sender_id=999, chat_id=999):
                        self.data = data
                        self.sender_id = sender_id
                        self.chat_id = chat_id
                        self.message_id = 777
                        self.answers = []
                        self.edits = []

                    async def answer(self, text="", alert=False):
                        self.answers.append((text, alert))

                    async def edit(self, text, buttons=None):
                        self.edits.append((text, buttons))
                        return self.message_id

                cb_other = FakeCallbackEvent(f"yt:{token}:1080".encode(), sender_id=111)
                await bot.on_yt_callback(cb_other)
                self.assertEqual(len(enqueued), 4)
                self.assertTrue(cb_other.answers[0][1])
                self.assertIn(token, bot._yt_pending_choices)

                # 8. Callback del usuario correcto -> encola con calidad 1080
                cb_ok = FakeCallbackEvent(f"yt:{token}:1080".encode(), sender_id=999)
                await bot.on_yt_callback(cb_ok)
                self.assertEqual(len(enqueued), 5)
                self.assertEqual(enqueued[4].kind, "youtube")
                self.assertEqual(enqueued[4].yt_quality, "1080")
                self.assertEqual(enqueued[4].url, "https://youtu.be/dQw4w9WgXcQ")
                self.assertIsNone(cb_ok.edits[0][1])
                self.assertNotIn(token, bot._yt_pending_choices)

                # 9. Callback expirado
                cb_exp = FakeCallbackEvent(f"yt:{token}:720".encode(), sender_id=999)
                await bot.on_yt_callback(cb_exp)
                self.assertEqual(len(enqueued), 5)
                self.assertTrue(cb_exp.answers[0][1])

                # 10. Callback de cancelar
                ev7 = FakeEvent("/yt https://youtu.be/dQw4w9WgXcQ")
                ev7.respond = fake_respond
                with patch.object(bot, "_is_safe_url", return_value=(True, None)):
                    await bot.on_tg_message(ev7)
                token2 = next(iter(bot._yt_pending_choices.keys()))
                cb_cancel = FakeCallbackEvent(f"yt:{token2}:cancel".encode(), sender_id=999)
                await bot.on_yt_callback(cb_cancel)
                self.assertEqual(len(enqueued), 5)
                self.assertIn("Cancelado", cb_cancel.edits[0][0])
                self.assertNotIn(token2, bot._yt_pending_choices)
        finally:
            bot.ALLOWED_TG_IDS = old_allowed
            bot._is_userbot_mode = old_userbot
            bot.YT_ASK_QUALITY = old_ask
            bot._yt_user_quality.pop(999, None)
            bot._yt_pending_choices.clear()


if __name__ == "__main__":
    unittest.main(verbosity=2)
