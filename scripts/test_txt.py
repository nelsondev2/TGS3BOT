"""Pruebas unitarias e integración local para el comando /txt e historial de enlaces.

Uso:
    python scripts/test_txt.py
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


class TestTxtCommand(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmpdir = tempfile.mkdtemp(prefix="test_txt_")
        self.old_dl_path = bot.DOWNLOAD_PATH
        self.old_hist = bot._user_links_history
        bot.DOWNLOAD_PATH = self.tmpdir
        bot._user_links_history = None

    async def asyncTearDown(self):
        bot.DOWNLOAD_PATH = self.old_dl_path
        bot._user_links_history = self.old_hist
        shutil.rmtree(self.tmpdir, ignore_errors=True)

    async def test_history_record_update_and_persistence(self):
        bot._record_user_link(101, "j1", "uno.mp4", "https://s3.todus.cu/stream/hi/uno.mp4")
        bot._record_user_link(101, "j2", "dos.mp4", "https://s3.todus.cu/stream/hi/dos.mp4")
        bot._record_user_link(101, "j3", "tres.mp4", "https://s3.todus.cu/stream/hi/tres.mp4")
        bot._record_user_link(101, "j4", "cuatro.mp4", "https://s3.todus.cu/stream/hi/cuatro.mp4")
        bot._record_user_link(202, "j5", "otro.zip", "https://s3.todus.cu/stream/hi/otro.zip")

        self.assertEqual(
            bot._get_user_links(101, limit=3),
            [
                ("dos.mp4", "https://s3.todus.cu/stream/hi/dos.mp4"),
                ("tres.mp4", "https://s3.todus.cu/stream/hi/tres.mp4"),
                ("cuatro.mp4", "https://s3.todus.cu/stream/hi/cuatro.mp4"),
            ],
        )

        # Actualizar por guardián anti-barrido
        bot._update_user_link(101, "j3", "https://s3.todus.cu/stream/df/tres.mp4")
        bot._update_user_link(101, "j2", None)

        # Recargar desde disco para verificar persistencia
        bot._user_links_history = None
        self.assertEqual(
            bot._get_user_links(101, limit=2),
            [
                ("tres.mp4", "https://s3.todus.cu/stream/df/tres.mp4"),
                ("cuatro.mp4", "https://s3.todus.cu/stream/hi/cuatro.mp4"),
            ],
        )
        self.assertEqual(
            bot._get_user_links(202),
            [("otro.zip", "https://s3.todus.cu/stream/hi/otro.zip")],
        )

    async def test_txt_command_variants_and_worker_recording(self):
        class FakeSender:
            def __init__(self, uid):
                self.id = uid
                self.username = "user"
                self.first_name = "User"

        class FakeMsg:
            media = None

        class FakeEvent:
            def __init__(self, text, uid=555):
                self.chat_id = uid
                self.raw_text = text
                self.message = FakeMsg()
                self._uid = uid
                self.replies = []

            async def get_sender(self):
                return FakeSender(self._uid)

            async def respond(self, text, **kwargs):
                self.replies.append(text)

        sent_files = []

        class FakeClient:
            async def send_file(self, chat_id, file=None, force_document=False, caption=""):
                content = file.read().decode("utf-8") if hasattr(file, "read") else ""
                sent_files.append((chat_id, getattr(file, "name", None), content, caption))

        old_client = bot._tg_client
        old_allowed = bot.ALLOWED_TG_IDS
        old_userbot = bot._is_userbot_mode
        bot._tg_client = FakeClient()
        bot.ALLOWED_TG_IDS = None
        bot._is_userbot_mode = False
        try:
            # 1. Sin enlaces -> avisa
            ev_empty = FakeEvent("/txt 3")
            await bot.on_tg_message(ev_empty)
            self.assertTrue(any("No tienes enlaces" in r for r in ev_empty.replies))

            # 2. Encolar trabajos en UserJobQueue (1 por 1, no necesariamente lote)
            q = bot.UserJobQueue()
            for idx in range(1, 6):
                job = bot.QueuedJob(user_id=555, kind="url", chat_id=555)

                async def _make_coro(j=job, n=idx):
                    j.result_name = f"vid_{n}.mp4"
                    j.result_url = f"https://s3.todus.cu/stream/hi/vid_{n}.mp4"

                await q.enqueue(job, _make_coro)
            worker = q._workers.get(555)
            while q.has_user_job(555):
                await asyncio.sleep(0.01)
            if worker and not worker.done():
                worker.cancel()
                try:
                    await worker
                except asyncio.CancelledError:
                    pass

            sent_files.clear()  # ignorar el enlaces.txt automático del lote

            # 3. /txt 3 -> exactamente los 3 últimos enlaces (3, 4, 5)
            ev3 = FakeEvent("/txt 3")
            await bot.on_tg_message(ev3)
            self.assertEqual(len(sent_files), 1)
            lines = sent_files[0][2].strip().splitlines()
            self.assertEqual(len(lines), 3)
            self.assertIn("vid_3.mp4", lines[0])
            self.assertIn("vid_4.mp4", lines[1])
            self.assertIn("vid_5.mp4", lines[2])

            # 4. /txt sin número -> los 5 enlaces
            ev_all = FakeEvent("/txt")
            await bot.on_tg_message(ev_all)
            self.assertEqual(len(sent_files), 2)
            self.assertEqual(len(sent_files[1][2].strip().splitlines()), 5)

            # 5. /txt 0 -> error de validación
            ev_bad = FakeEvent("/txt 0")
            await bot.on_tg_message(ev_bad)
            self.assertTrue(any("mayor que 0" in r for r in ev_bad.replies))

            # 6. /txt clear -> limpia historial
            ev_clr = FakeEvent("/txt clear")
            await bot.on_tg_message(ev_clr)
            self.assertTrue(any("borrado" in r for r in ev_clr.replies))
            self.assertEqual(bot._get_user_links(555), [])
        finally:
            bot._tg_client = old_client
            bot.ALLOWED_TG_IDS = old_allowed
            bot._is_userbot_mode = old_userbot


if __name__ == "__main__":
    unittest.main(verbosity=2)
