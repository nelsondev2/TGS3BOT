"""
Script de diagnóstico contra s3.todus.cu (no es parte del bot).

Sube archivos de texto diminutos con distintos patrones de key y dos
transportes distintos, comprueba cuáles siguen vivos en intervalos de
varios minutos y al final los borra todos.

Objetivo: averiguar empíricamente qué hace que los enlaces "venzan
casi al instante" (patrón de key barrido vs. transporte/UA vs. IP
extranjera), en vez de adivinar a partir de READMEs de terceros.

Uso:  python scripts/probe_todus.py            (13 min por defecto)
      PROBE_MINUTES=5 python scripts/probe_todus.py
Requiere: aiohttp (solo para replicar el transporte del bot).
"""
import os
import sys
import time
import asyncio
import urllib.request
import urllib.error
from urllib.parse import quote

BASE = "https://s3.todus.cu/stream"
RUN = (os.environ.get("GITHUB_RUN_ID") or str(int(time.time())))[-6:]
TOTAL_MIN = float(os.environ.get("PROBE_MINUTES", "13"))
FAKE_ID = "999000111"

UA_TODUS = "todus-client/2.0 (+https://s3.todus.cu)"
UA_BOT = "TGS3BOT/1.0 (+https://s3.todus.cu)"  # el que usa hoy el bot

BODY = (f"todus probe run {RUN}\n" * 100).encode()

# (etiqueta, key, transporte)
OBJECTS = [
    ("urllib patron ACTUAL del bot (tg_/telegram)",
     f"users/tg_{FAKE_ID}/telegram/20260101-000000-prb{RUN}/telegram_abc123_probe{RUN}.txt",
     "urllib"),
    ("urllib users/df/<id>/<file> (patron que sobrevive)",
     f"users/df/{FAKE_ID}/probe{RUN}_df.txt",
     "urllib"),
    ("urllib hi/<id>/<file> (patron que sobrevive)",
     f"hi/{FAKE_ID}/probe{RUN}_hi.txt",
     "urllib"),
    ("urllib users/xx/<id>/<file> (namespace inventado)",
     f"users/xx/{FAKE_ID}/probe{RUN}_xx.txt",
     "urllib"),
    ("urllib raiz <hex>_<file> (patron viejo)",
     f"deadbeef_probe{RUN}_root.txt",
     "urllib"),
    ("urllib users/df/ pero nombre con 'telegram'",
     f"users/df/{FAKE_ID}/telegram_probe{RUN}.txt",
     "urllib"),
    ("aiohttp EXACTO al bot (UA bot) bajo users/df/",
     f"users/df/{FAKE_ID}/probe{RUN}_df_botua.txt",
     "aiohttp_bot"),
    ("aiohttp EXACTO al bot patron ACTUAL (replica produccion)",
     f"users/tg_{FAKE_ID}/telegram/20260101-000000-prb{RUN}/telegram_abc123_probe{RUN}_bot.txt",
     "aiohttp_bot"),
]


def url_of(key):
    return f"{BASE}/{quote(key, safe='/')}"


def log(*a):
    print(f"[{time.strftime('%H:%M:%S')}Z]", *a, flush=True)


def put_urllib(key, ua):
    req = urllib.request.Request(
        url_of(key), data=BODY, method="PUT",
        headers={
            "User-Agent": ua,
            "Content-Type": "text/plain",
            "Content-Length": str(len(BODY)),
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
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
        if e.code == 405:  # fallback GET 1 byte
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


async def put_bot_style(key, tmp_path):
    """Replica EXACTA de _s3_put_direct del bot (aiohttp + file object)."""
    import aiohttp

    size = os.path.getsize(tmp_path)
    filename = os.path.basename(key)
    headers = {
        "Content-Type": "text/plain",
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
    log(f"=== PROBE todus run={RUN} total~{TOTAL_MIN} min ===")
    log(f"endpoint: {BASE}")

    tmp_path = f"/tmp/probe_{RUN}.txt"
    with open(tmp_path, "wb") as fh:
        fh.write(BODY)

    # --- Subidas ---
    log("--- PUTs ---")
    for label, key, kind in OBJECTS:
        if kind == "urllib":
            status, etag, snippet = put_urllib(key, UA_TODUS)
        else:
            status, etag, snippet = await put_bot_style(key, tmp_path)
        log(f"PUT [{label}]\n    key={key}\n    -> HTTP {status} ETag={etag} body={snippet!r}")

    # --- Rondas de supervivencia ---
    rounds = [0, 120, 180, 240, 240]  # acumulado: 0, 2, 5, 9, 13 min
    scale = max(0.05, TOTAL_MIN / 13.0)
    elapsed = 0
    history = {key: [] for _, key, _ in OBJECTS}
    for i, wait in enumerate(rounds):
        wait_s = int(wait * scale)
        if wait_s:
            log(f"--- durmiendo {wait_s}s ---")
            await asyncio.sleep(wait_s)
            elapsed += wait_s
        log(f"--- ronda {i} (t+{elapsed}s aprox) HEADs ---")
        for label, key, _ in OBJECTS:
            st = head_status(key)
            history[key].append(str(st))
            log(f"HEAD {st}  {key}")

    # --- Resumen ---
    log("=== RESUMEN (secuencia de HEAD por objeto) ===")
    for label, key, _ in OBJECTS:
        seq = " -> ".join(history[key])
        alive = history[key][-1] in ("200", "206")
        log(f"{'VIVO ' if alive else 'MUERTO'} [{label}] {key}\n    {seq}")

    # --- Limpieza ---
    log("--- DELETEs de limpieza ---")
    for _, key, _ in OBJECTS:
        log(f"DELETE {delete(key)}  {key}")
    try:
        req = urllib.request.Request(
            f"{BASE}?list-type=2&prefix=users%2Fdf%2F{FAKE_ID}%2F",
            headers={"User-Agent": UA_TODUS},
        )
        with urllib.request.urlopen(req, timeout=30) as r:
            log(f"listado final users/df/{FAKE_ID}/: {r.read()[:300]!r}")
    except Exception as e:  # noqa: BLE001
        log(f"listado final fallo: {e}")
    log("=== FIN PROBE ===")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
