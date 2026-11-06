"""Harness de diagnóstico: ejerce el camino de descarga de URLs de bot.py
contra URLs reales e imprime dónde falla cada una.

Uso:
    python scripts/test_url_download.py [url ...]

Sin argumentos usa una lista de URLs de prueba. NO toca Telegram ni S3:
solo _is_safe_url, HEAD y _download_sequential/_download_parallel.
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
import bot


class FakeJob:
    def __init__(self):
        self.temp_paths = []


async def diagnose(url: str) -> None:
    print(f"\n=== {url}")
    safe, err = bot._is_safe_url(url)
    print(f"  SSRF: safe={safe} err={err}")
    if not safe:
        return

    raw_name = bot.get_filename_from_url(url)
    print(f"  raw_name={raw_name!r}")

    fd, temp_path = tempfile.mkstemp(dir=bot.DOWNLOAD_PATH)
    os.close(fd)
    job = FakeJob()
    try:
        total = 0
        accepts_ranges = False
        try:
            async with aiohttp.ClientSession() as hs:
                async with asyncio.timeout(60):
                    async with hs.head(url, headers=bot.BROWSER_HEADERS,
                                       allow_redirects=True) as h:
                        print(f"  HEAD: status={h.status}")
                        if h.status < 400:
                            total = int(h.headers.get("Content-Length", 0))
                            accepts_ranges = (
                                h.headers.get("Accept-Ranges", "").lower() == "bytes"
                            )
        except Exception as e:
            print(f"  HEAD falló: {type(e).__name__}: {str(e)[:180]}")
        print(f"  total={total} ranges={accepts_ranges}")

        async with aiohttp.ClientSession() as session:
            async with asyncio.timeout(300):
                if (bot.PARALLEL_URL_DOWNLOAD and accepts_ranges
                        and total >= bot.PARALLEL_URL_MIN_SIZE):
                    got, ct = await bot._download_parallel(
                        session, url, temp_path, total, job, None)
                else:
                    got, ct = await bot._download_sequential(
                        session, url, temp_path, total, job, None)
        size = os.path.getsize(temp_path)
        print(f"  OK: bajados={got} size_en_disco={size} ct={ct!r}")
    except Exception as e:
        print(f"  ✗ ERROR: {type(e).__name__}: {str(e)[:200]}")
    finally:
        for p in job.temp_paths + [temp_path]:
            try:
                os.remove(p)
            except OSError:
                pass


async def amain(urls):
    # Cap de 32 MB por descarga para el diagnóstico
    old_max = bot.MAX_FILE_SIZE
    bot.MAX_FILE_SIZE = min(old_max, 32 * 1024 * 1024)
    try:
        for u in urls:
            await diagnose(u)
    finally:
        bot.MAX_FILE_SIZE = old_max


if __name__ == "__main__":
    urls = sys.argv[1:] or [
        "https://pypi.org/simple/aiohttp/",
        "https://api.github.com/repos/nelsondev2/TGS3BOT",
        "https://codeload.github.com/nelsondev2/TGS3BOT/tar.gz/refs/heads/main",
        "https://github.com/nelsondev2/TGS3BOT/archive/refs/heads/main.tar.gz",
    ]
    asyncio.run(amain(urls))
