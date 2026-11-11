"""Test local del camino de descarga de bot.py con un servidor INESTABLE.

Casos (sin internet, servidor aiohttp local):
  1. flaky: el 1er intento entrega la mitad y corta; el 2do honra
     Range -> el archivo final debe ser el completo (reanudación).
  2. full200: corta a mitad y el 2do IGNORA el Range (HTTP 200) ->
     re-descarga limpia desde cero, archivo completo.
  3. forbidden: 403 siempre -> falla al PRIMER intento (los 4xx
     definitivos no se reintentan).
  4. slow: cuelga la lectura entre chunks -> los timeouts de
     transporte se reintentan hasta agotar y el error final es claro.
  5. bad206: tras entregar la mitad y cortar, el reintento con Range
     devuelve 206 con Content-Range ERRÓNEO -> reinicio limpio a 0 y
     el siguiente intento (200 completo) salva el archivo.
  6. busy503: dos 503 y luego un 200 -> el reintento por 5xx funciona
     (antes del fix del orden de except, el 503 fallaba al instante).

Uso: python scripts/test_flaky_download.py
"""
import asyncio
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import aiohttp
from aiohttp import web

import bot

FULL = bytes((i % 251) for i in range(240_000))  # contenido verificable


async def _range_response(request, offset):
    resp = web.StreamResponse(status=206, headers={
        "Content-Type": "application/octet-stream",
        "Content-Range": f"bytes {offset}-{len(FULL) - 1}/{len(FULL)}",
        "Content-Length": str(len(FULL) - offset),
    })
    await resp.prepare(request)
    await resp.write(FULL[offset:])
    await resp.write_eof()
    return resp


class _CutResponse(web.StreamResponse):
    """Escribe nbytes, deja que el cliente los consuma y ABORTA la
    conexión sin terminar el cuerpo (Content-Length incompleto)."""
    def __init__(self, nbytes):
        super().__init__(status=200, headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(FULL)),
        })
        self._nbytes = nbytes

    async def _start(self, request, *a, **kw):
        r = await super()._start(request, *a, **kw)
        await self.write(FULL[:self._nbytes])
        # Determinista: que el cliente lea los bytes ANTES del corte
        await asyncio.sleep(0.3)
        try:
            request.transport.close()
        except Exception:
            pass
        return r


def make_app(state):
    async def flaky(request):
        state["flaky"] += 1
        rng = request.headers.get("Range", "")
        if rng == "bytes=120000-":
            return await _range_response(request, 120000)
        return _CutResponse(120_000)

    async def full200(request):
        state["full200"] += 1
        if state["full200"] == 1:
            return _CutResponse(90_000)
        # Ignora el Range: 200 con TODO (re-descarga limpia).
        return web.Response(status=200, body=FULL,
                            content_type="application/octet-stream")

    async def forbidden(request):
        state["forbidden"] += 1
        return web.Response(status=403, text="denied")

    async def slow(request):
        state["slow"] += 1
        resp = web.StreamResponse(status=200, headers={
            "Content-Type": "application/octet-stream",
            "Content-Length": str(len(FULL)),
        })
        await resp.prepare(request)
        await resp.write(FULL[:10_000])
        await asyncio.sleep(30)  # nunca llega: sock_read corta antes
        return resp

    async def bad206(request):
        state["bad206"] += 1
        n = state["bad206"]
        if n == 1:
            return _CutResponse(60_000)
        if n == 2:
            # 206 con Content-Range que NO casa con el Range pedido.
            resp = web.StreamResponse(status=206, headers={
                "Content-Type": "application/octet-stream",
                "Content-Range": "bytes 0-3/240000",
                "Content-Length": "4",
            })
            await resp.prepare(request)
            await resp.write(b"zzzz")
            await resp.write_eof()
            return resp
        # 3er intento: el cliente re-descargó desde 0 -> 200 completo.
        return web.Response(status=200, body=FULL,
                            content_type="application/octet-stream")

    async def busy503(request):
        state["busy503"] += 1
        if state["busy503"] < 3:
            return web.Response(status=503, text="try later")
        return web.Response(status=200, body=FULL,
                            content_type="application/octet-stream")

    app = web.Application()
    app.router.add_get("/flaky", flaky)
    app.router.add_get("/full200", full200)
    app.router.add_get("/forbidden", forbidden)
    app.router.add_get("/slow", slow)
    app.router.add_get("/bad206", bad206)
    app.router.add_get("/busy503", busy503)
    return app


class FakeJob:
    def __init__(self):
        self.temp_paths = []


async def run_case(base, path):
    fd, tmp = tempfile.mkstemp(dir=bot.DOWNLOAD_PATH)
    os.close(fd)
    got, err = None, None
    try:
        async with aiohttp.ClientSession() as session:
            got, _ct = await bot._download_sequential(
                session, base + path, tmp, 0, FakeJob(), None)
    except Exception as e:
        err = f"{type(e).__name__}: {str(e)[:110]}"
    size = os.path.getsize(tmp)
    identical = False
    if size == len(FULL):
        with open(tmp, "rb") as fh:
            identical = fh.read() == FULL
    os.remove(tmp)
    return {"got": got, "err": err, "size": size, "identical": identical}


async def amain():
    failures = []

    old_retries = bot.URL_DOWNLOAD_RETRIES
    old_timeout = bot._DL_TIMEOUT
    bot.URL_DOWNLOAD_RETRIES = 5
    bot._DL_TIMEOUT = aiohttp.ClientTimeout(
        total=None, connect=5, sock_connect=5, sock_read=1)

    state = {k: 0 for k in
             ("flaky", "full200", "forbidden", "slow", "bad206", "busy503")}
    app = make_app(state)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", 0)
    await site.start()
    port = site._server.sockets[0].getsockname()[1]
    base = f"http://127.0.0.1:{port}"
    try:
        r = await run_case(base, "/flaky")
        ok = r["err"] is None and r["identical"] and state["flaky"] == 2
        print(f"1 flaky/resume Range: {r} reqs={state['flaky']} -> "
              f"{'PASS' if ok else 'FAIL'}")
        if not ok:
            failures.append("flaky")

        r = await run_case(base, "/full200")
        ok = r["err"] is None and r["identical"] and state["full200"] == 2
        print(f"2 full-200 restart: {r} reqs={state['full200']} -> "
              f"{'PASS' if ok else 'FAIL'}")
        if not ok:
            failures.append("full200")

        r = await run_case(base, "/forbidden")
        ok = (r["err"] is not None and "403" in r["err"]
              and state["forbidden"] == 1)
        print(f"3 403 fail-fast: {r} reqs={state['forbidden']} -> "
              f"{'PASS' if ok else 'FAIL'}")
        if not ok:
            failures.append("forbidden")

        r = await run_case(base, "/slow")
        ok = (r["err"] is not None and "5" in r["err"]
              and state["slow"] == 5)
        print(f"4 slow/timeouts agotados: {r} reqs={state['slow']} -> "
              f"{'PASS' if ok else 'FAIL'}")
        if not ok:
            failures.append("slow")

        r = await run_case(base, "/bad206")
        ok = (r["err"] is None and r["identical"]
              and state["bad206"] == 3)
        print(f"5 206-malo -> restart -> 200: {r} reqs={state['bad206']} -> "
              f"{'PASS' if ok else 'FAIL'}")
        if not ok:
            failures.append("bad206")

        r = await run_case(base, "/busy503")
        ok = (r["err"] is None and r["identical"]
              and state["busy503"] == 3)
        print(f"6 503x2 -> 200: {r} reqs={state['busy503']} -> "
              f"{'PASS' if ok else 'FAIL'}")
        if not ok:
            failures.append("busy503")
    finally:
        await runner.cleanup()
        bot.URL_DOWNLOAD_RETRIES = old_retries
        bot._DL_TIMEOUT = old_timeout

    print()
    if failures:
        print("FALLOS:", ", ".join(failures))
        sys.exit(1)
    print("TODOS LOS CASOS OK")


if __name__ == "__main__":
    asyncio.run(amain())
