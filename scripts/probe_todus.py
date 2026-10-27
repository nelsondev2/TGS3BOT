"""
Script de diagnóstico contra s3.todus.cu (no es parte del bot).

v2 (2026-10-27): las reglas del barrido parecen haber cambiado (el
namespace dk/ desapareció). Este probe hace:

  1. CENSO del bucket: pagina ListObjectsV2 y agrega por namespace
     (2 primeros segmentos): conteo, objeto más viejo/más nuevo y
     bytes. Dice qué namespaces contienen cosas viejas AHORA.
  2. MATRIZ de subidas: objetos pequeños y grandes (25 MB) en varios
     namespaces (users/df, hi, dk/tg_, dk/dd, video, users/tg_ control,
     df con subcarpeta), con urllib y con el transporte exacto del bot
     (aiohttp, PDF grande como los del usuario).
  3. Rondas HEAD hasta ~28 min y limpieza final (DELETE de todo).

Uso:  python scripts/probe_todus.py
      PROBE_MINUTES=10 python scripts/probe_todus.py
Requiere: aiohttp (solo para replicar el transporte del bot).
"""
import os
import sys
import time
import asyncio
import urllib.request
import urllib.error
import xml.etree.ElementTree as ET
from urllib.parse import quote, urlencode

BASE = "https://s3.todus.cu/stream"
RUN = (os.environ.get("GITHUB_RUN_ID") or str(int(time.time())))[-6:]
TOTAL_MIN = float(os.environ.get("PROBE_MINUTES", "28"))
FAKE_ID = "999000111"

UA_TODUS = "todus-client/2.0 (+https://s3.todus.cu)"
UA_BOT = "TGS3BOT/1.0 (+https://s3.todus.cu)"  # el que usa hoy el bot

BODY = (f"todus probe run {RUN}\n" * 100).encode()
BIG_SIZE = 25 * 1024 * 1024


def url_of(key):
    return f"{BASE}/{quote(key, safe='/')}"


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}Z]", *a, flush=True)


# ---------------------------------------------------------------- censo
NS = {"s3": "http://s3.amazonaws.com/doc/2006-03-01/"}


def census(max_keys=20000, timebox=240):
    log("=== CENSO del bucket (ListObjectsV2 paginado) ===")
    t0 = time.time()
    agg = {}
    total = 0
    token = None
    while total < max_keys and (time.time() - t0) < timebox:
        params = {"list-type": "2", "max-keys": "1000"}
        if token:
            params["continuation-token"] = token
        url = f"{BASE}?{urlencode(params)}"
        req = urllib.request.Request(url, headers={"User-Agent": UA_TODUS})
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                data = r.read()
        except Exception as e:  # noqa: BLE001
            log(f"censo: error listando tras {total} keys: {e}")
            break
        root = ET.fromstring(data)
        truncated = (root.findtext("s3:IsTruncated", "false", NS) == "true")
        token = root.findtext("s3:NextContinuationToken", None, NS)
        for c in root.findall("s3:Contents", NS):
            key = c.findtext("s3:Key", "", NS)
            lm = c.findtext("s3:LastModified", "", NS)
            try:
                size = int(c.findtext("s3:Size", "0", NS))
            except ValueError:
                size = 0
            parts = key.split("/")
            ns = "/".join(parts[:2]) if len(parts) >= 2 else "(raíz)"
            a = agg.setdefault(ns, {"n": 0, "bytes": 0, "min": lm, "max": lm})
            a["n"] += 1
            a["bytes"] += size
            if lm and (not a["min"] or lm < a["min"]):
                a["min"] = lm
            if lm and lm > a["max"]:
                a["max"] = lm
            total += 1
        if not truncated:
            break
    log(f"censo: {total} objetos en {time.time() - t0:.0f}s "
        f"(tope {'alcanzado' if total >= max_keys else 'no'})")
    for ns in sorted(agg, key=lambda k: -agg[k]["n"]):
        a = agg[ns]
        gb = a["bytes"] / 1e9
        log(f"  NS {ns!r}: n={a['n']} bytes={gb:.2f}GB "
            f"viejo={a['min']} nuevo={a['max']}")
    log("=== FIN CENSO ===")
    return agg


# ---------------------------------------------------------------- http
def put_urllib(key, data, ua, ctype):
    req = urllib.request.Request(
        url_of(key), data=data, method="PUT",
        headers={
            "User-Agent": ua,
            "Content-Type": ctype,
            "Content-Length": str(len(data)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=300) as r:
            return r.status, r.headers.get("ETag"), r.read()[:120]
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("ETag") if e.headers else None, e.read()[:120]
    except Exception as e:  # noqa: BLE001
        return f"ERR:{type(e).__name__}", None, str(e)[:120].encode()


def head_status(key):
    url = url_of(key)
    req = urllib.request.Request(
        url, method="HEAD", headers={"User-Agent": UA_TODUS},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        if e.code == 405:
            try:
                greq = urllib.request.Request(
                    url, headers={"User-Agent": UA_TODUS, "Range": "bytes=0-0"},
                )
                with urllib.request.urlopen(greq, timeout=30) as gr:
                    gr.read(10)
                    return gr.status
            except urllib.error.HTTPError as ge:
                return ge.code
            except Exception as ge:  # noqa: BLE001
                return f"ERR:{type(ge).__name__}"
        return e.code
    except Exception as e:  # noqa: BLE001
        return f"ERR:{type(e).__name__}"


def delete(key):
    req = urllib.request.Request(
        url_of(key), method="DELETE", headers={"User-Agent": UA_TODUS},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception as e:  # noqa: BLE001
        return f"ERR:{type(e).__name__}"


async def put_bot_style(key, tmp_path, filename, ctype):
    """Réplica EXACTA de _s3_put_direct del bot (aiohttp + file object)."""
    import aiohttp

    size = os.path.getsize(tmp_path)
    headers = {
        "Content-Type": ctype,
        "Content-Disposition": f'attachment; filename="{filename}"',
        "Content-Length": str(size),
        "User-Agent": UA_BOT,
    }
    timeout = aiohttp.ClientTimeout(
        total=None, connect=30, sock_connect=30, sock_read=600
    )
    f = open(tmp_path, "rb")
    try:
        async with aiohttp.ClientSession(timeout=timeout) as session:
            async with session.put(
                url_of(key), data=f, headers=headers
            ) as resp:
                body = (await resp.text())[:120]
                return resp.status, resp.headers.get("ETag"), body.encode()
    except Exception as e:  # noqa: BLE001
        return f"ERR:{type(e).__name__}", None, str(e)[:120].encode()
    finally:
        f.close()


async def main():
    log(f"=== PROBE v2 todus run={RUN} objetivo~{TOTAL_MIN} min ===")
    log(f"endpoint: {BASE}")

    # 1) Censo (en hilo para no bloquear el loop)
    await asyncio.to_thread(census)

    # 2) Preparar cuerpos
    tmp_big = f"/tmp/probe_big_{RUN}.bin"
    with open(tmp_big, "wb") as fh:
        fh.write(os.urandom(BIG_SIZE))
    with open(tmp_big, "rb") as fh:
        big_bytes = fh.read()

    # (etiqueta, key, kind, data, ctype, filename_bot)
    r8 = RUN
    objects = [
        ("df small urllib",
         f"users/df/{FAKE_ID}/probe{RUN}_df.txt", "urllib", BODY, "text/plain", None),
        ("df BOT-EXACT PDF 25MB (como el usuario)",
         f"users/df/{FAKE_ID}/Documento_Prueba_{r8}.pdf", "bot", None,
         "application/pdf", "Documento_Prueba.pdf"),
        ("df big mp4 nombre plano urllib",
         f"users/df/{FAKE_ID}/Video_Prueba_Ep01.mp4", "urllib", big_bytes, "video/mp4", None),
        ("hi small urllib",
         f"hi/{FAKE_ID}/probe{RUN}_hi.txt", "urllib", BODY, "text/plain", None),
        ("hi big mp4 urllib",
         f"hi/{FAKE_ID}/Video_Prueba_Ep01.mp4", "urllib", big_bytes, "video/mp4", None),
        ("dk/tg_ small urllib",
         f"dk/tg_{FAKE_ID}/probe{RUN}_dk.txt", "urllib", BODY, "text/plain", None),
        ("dk/dd small urllib",
         f"dk/dd/probe{RUN}_dd.txt", "urllib", BODY, "text/plain", None),
        ("video small urllib",
         f"video/{FAKE_ID}/probe{RUN}_video.txt", "urllib", BODY, "text/plain", None),
        ("CONTROL users/tg_ telegram small urllib",
         f"users/tg_{FAKE_ID}/telegram/probe{RUN}_tg.txt", "urllib", BODY, "text/plain", None),
        ("df con subcarpeta (profundidad) small urllib",
         f"users/df/{FAKE_ID}/sub/probe{RUN}_deep.txt", "urllib", BODY, "text/plain", None),
    ]

    log("--- PUTs ---")
    for label, key, kind, data, ctype, fname in objects:
        t0 = time.time()
        if kind == "urllib":
            status, etag, snippet = await asyncio.to_thread(
                put_urllib, key, data, UA_TODUS, ctype
            )
        else:
            status, etag, snippet = await put_bot_style(
                key, tmp_big, fname, ctype
            )
        log(f"PUT [{label}] {time.time()-t0:.1f}s\n    key={key}\n"
            f"    -> HTTP {status} ETag={etag} body={snippet!r}")

    # 3) Rondas de supervivencia: 0, 2, 5, 10, 18, 28 min acumulados
    rounds = [0, 120, 180, 300, 480, 600]
    scale = max(0.05, TOTAL_MIN / 28.0)
    elapsed = 0
    history = {key: [] for _, key, *_ in objects}
    for i, wait in enumerate(rounds):
        wait_s = int(wait * scale)
        if wait_s:
            log(f"--- durmiendo {wait_s}s ---")
            await asyncio.sleep(wait_s)
            elapsed += wait_s
        log(f"--- ronda {i} (t+{elapsed}s aprox) HEADs ---")
        for label, key, *_ in objects:
            st = await asyncio.to_thread(head_status, key)
            history[key].append(str(st))
            log(f"HEAD {st}  {key}  [{label}]")

    log("=== RESUMEN (secuencia HEAD por objeto) ===")
    for label, key, *_ in objects:
        seq = " -> ".join(history[key])
        alive = history[key][-1] in ("200", "206")
        log(f"{'VIVO ' if alive else 'MUERTO'} [{label}] {key}\n    {seq}")

    log("--- DELETEs de limpieza ---")
    for _, key, *_ in objects:
        st = await asyncio.to_thread(delete, key)
        log(f"DELETE {st}  {key}")
    log("=== FIN PROBE v2 ===")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
