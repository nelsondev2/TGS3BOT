# Bot de Telegram (Telethon) - Subida a S3

Bot de Telegram que escucha mensajes, descarga URLs enviadas por usuarios, las sube a S3 y **reenvía el archivo al usuario por Telegram + SIEMPRE muestra la URL pública S3**.

## ✨ Características

- ✅ **Cliente Telethon** (soporta modo **bot oficial** con `BOT_TOKEN` o modo **userbot** con `SESSION_STRING`)
- ✅ **La URL S3 siempre se muestra al usuario** (fix principal)
- ✅ **Protección SSRF**: bloquea IPs privadas/loopback/link-local
- ✅ **Allowlist de usuarios** opcional vía `ALLOWED_TG_IDS`
- ✅ **Event loop no bloqueado**: llamadas síncronas envueltas en `asyncio.to_thread`
- ✅ **Descarga paralela robusta**: rechaza respuestas `200 OK` en peticiones `Range`
- ✅ **Detección de extensión desde `Content-Type`** cuando la URL no la tiene
- ✅ **Limpieza de archivos `.part`** en cancelación o fallo
- ✅ **Envío por Telegram con tipo correcto** (imagen/video/audio/voz/documento)
- ✅ **SIGTERM handler** para shutdown graceful en Render
- ✅ **Healthcheck endpoint** en `/health` y `/healthz`
- ✅ **Watchdog opcional**
- ✅ **Todas las constantes externalizadas** a variables de entorno

## 🚀 Deploy en Render

### Opción 1: Blueprint (recomendado)

1. Sube este repo a GitHub/GitLab.
2. En Render: **New → Blueprint**.
3. Selecciona tu repo. Render detectará `render.yaml` automáticamente.
4. Configura las variables sensibles en el panel:
   - `TG_API_ID` y `TG_API_HASH` (de https://my.telegram.org/apps)
   - `TG_BOT_TOKEN` (de @BotFather) **o** `TG_SESSION_STRING` (modo userbot)
   - `ALLOWED_TG_IDS` (CSV de IDs autorizados)

### Opción 2: Web Service manual

1. Render: **New → Web Service**.
2. Conecta tu repo.
3. **Runtime**: Python 3.
4. **Build Command**: `pip install -r requirements.txt`
5. **Start Command**: `python bot.py`
6. **Health Check Path**: `/health`
7. Añade las variables de entorno listadas en `.env.example`.

### Opción 3: Docker

1. Render: **New → Web Service**.
2. Conecta tu repo.
3. **Environment**: Docker.
4. Render usará el `Dockerfile` incluido.

## 🔧 Configuración de Telegram

### Paso 1: Obtener API_ID y API_HASH

1. Visita https://my.telegram.org/apps
2. Inicia sesión con tu número de teléfono.
3. Crea una nueva app (los campos son informativos).
4. Copia el `api_id` y `api_hash`.

### Paso 2: Elegir modo de operación

#### Opción A: Modo BOT (recomendado para producción)

1. En Telegram, habla con **@BotFather**.
2. Envía `/newbot` y sigue las instrucciones.
3. Copia el token que te da (formato `123456789:ABCDefgh...`).
4. Configúralo como `TG_BOT_TOKEN`.

#### Opción B: Modo USERBOT (uso con tu cuenta personal)

1. Genera una sesión string ejecutando:

```python
# scripts/generate_session.py
import asyncio
from telethon.sessions import StringSession
from telethon import TelegramClient

async def main():
    client = TelegramClient(
        "session", 
        api_id=int(input("API_ID: ")),
        api_hash=input("API_HASH: ")
    )
    await client.start(
        phone=lambda: input("Phone (+53...): "),
        code_callback=lambda: input("Código: "),
        password=lambda: input("2FA password (si tienes): ")
    )
    print("\n=== GUARDA ESTO ===")
    print(StringSession.save(client.session))
    print("===================\n")
    await client.disconnect()

asyncio.run(main())
```

2. Guarda el string resultante como `TG_SESSION_STRING`.

### Paso 2.5: Generar SESSION_STRING para un BOT (alternativa al disco persistente)

Si tu plataforma de deploy tiene sistema de archivos efímero (como Render free) y quieres evitar re-logins, puedes extraer el `SESSION_STRING` después del primer arranque exitoso del bot:

1. Arranca el bot por primera vez con `TG_BOT_TOKEN`.
2. En los logs verás un mensaje `⚠️ PRIMER LOGIN — guarda este SESSION_STRING...` con un string largo.
3. Copia ese string y configúralo como variable de entorno `TG_SESSION_STRING`.
4. Borra `TG_BOT_TOKEN` (o déjalo, `TG_SESSION_STRING` tiene prioridad) y redéploy.
5. A partir de ahí, el bot usa la sesión string y nunca más necesita re-login.

**Importante:** Esto evita el `FloodWaitError` que ocurre cuando Telegram rate-limita múltiples logins seguidos.

### Paso 3: Obtener tu ID de Telegram (para ALLOWED_TG_IDS)

1. Habla con **@userinfobot** o **@RawDataBot** en Telegram.
2. Te devolverá tu ID numérico (ej: `123456789`).
3. Añádelo a `ALLOWED_TG_IDS` (CSV: `123456789,987654321`).

## 🔧 Variables de entorno

| Variable | Default | Descripción |
|---------|---------|-------------|
| `TG_API_ID` | (vacío) | API ID de https://my.telegram.org/apps |
| `TG_API_HASH` | (vacío) | API HASH de https://my.telegram.org/apps |
| `TG_BOT_TOKEN` | (vacío) | Token del bot (@BotFather) |
| `TG_SESSION_STRING` | (vacío) | Sesión pre-generada (modo userbot) |
| `ALLOWED_TG_IDS` | (vacío = todos) | CSV de IDs autorizados |
| `SSRF_BLOCK_PRIVATE` | `1` | Bloquea IPs privadas/loopback |
| `PORT` | `10000` | Puerto HTTP (Render lo inyecta) |
| `DOWNLOAD_PATH` | `/tmp/tg_uploads` | Directorio temporal |
| `MAX_FILE_SIZE_MB` | `2000` | Tamaño máximo de archivo |
| `TG_UPLOAD_LIMIT_MB` | `2000` | Límite de subida por Telegram |
| `CHUNK_SIZE_MB` | `16` | Tamaño de chunk |
| `WATCHDOG_ENABLED` | `1` | Activa watchdog |
| `WATCHDOG_LIFETIME` | `14100` | Segundos antes de reinicio |
| `PARALLEL_URL_DOWNLOAD` | `1` | Descarga paralela |
| `PARALLEL_URL_PARTS` | `4` | Partes paralelas |
| `ALWAYS_SHOW_S3_URL` | `1` | Mostrar URL S3 siempre |
| `SEND_FILE_VIA_TG` | `1` | Enviar archivo por TG además de URL |
| `LOG_LEVEL` | `INFO` | Nivel de logging |
| `S3_ENDPOINT` | `https://s3.todus.cu` | Endpoint S3 |
| `S3_BUCKET` | `stream` | Bucket S3 |
| `S3_UNSIGNED` | `1` | S3 sin firma (toDus) |
| `S3_ACCESS_KEY` | `public` | Key (si S3_UNSIGNED=0) |
| `S3_SECRET_KEY` | `public` | Secret (si S3_UNSIGNED=0) |

Ver `.env.example` para referencia.

## 📋 Comandos disponibles en el bot

| Comando | Descripción |
|---------|-------------|
| `/start`, `hola`, `ayuda`, `help` | Mensaje de ayuda |
| `/cancel`, `cancelar` | Cancela tu trabajo activo |
| `/status` | Verifica si tienes trabajo en curso |
| `<URL>` | Procesa cualquier URL http/https |

## 🏥 Health endpoints

- `GET /` → `{"status":"online","service":"telegram-s3-bot"}`
- `GET /health` → estado, uptime, jobs activos, watchdog, tg_connected
- `GET /healthz` → alias para Render

## 🛡️ Seguridad

- **SSRF Protection**: bloquea accesos a `127.0.0.1`, `10.x`, `192.168.x`, `169.254.x` (metadata cloud), etc.
- **Allowlist de IDs**: solo los IDs listados en `ALLOWED_TG_IDS` pueden usar el bot.
- **Validación de tamaño**: límite configurable por archivo.
- **Sanitización de nombres**: previene path traversal en archivos temporales.

## 📝 Notas importantes

### Sobre el watchdog
El watchdog reinicia el proceso cada `WATCHDOG_LIFETIME` segundos (default: 3h 55m). Útil en planes free de Render para evitar fugas de memoria. Desactívalo con `WATCHDOG_ENABLED=0`.

### Sobre el modo bot vs userbot
- **Modo bot** (recomendado): usa un bot creado con @BotFather. Tiene límites estándar (2GB por archivo) y los usuarios deben iniciar el bot con `/start`.
- **Modo userbot**: usa tu cuenta personal. Sin límites artificiales pero usa tu cuenta para enviar/recibir. Útil si quieres procesar mensajes en chats privados/grupos donde tú estás.

### Sobre el S3
El S3 por defecto es el público de toDus (`s3.todus.cu`, sin firma). Puedes cambiarlo a cualquier S3 compatible (AWS, MinIO, Cloudflare R2, etc.) seteando `S3_ENDPOINT`, `S3_BUCKET`, `S3_UNSIGNED=0`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`.

### Sobre el disco
- `/tmp/tg_uploads` es efímero en Render (se pierde en cada deploy).
- Los archivos temporales se eliminan después de subirlos a S3 y enviarlos por Telegram.
- Si necesitas caché persistente, monta un disco en `render.yaml`.

## 🐛 Bugs corregidos respecto a la versión original

1. **URL S3 no se mostraba al usuario cuando el envío tenía éxito** → FIX PRINCIPAL
2. Descarga paralela podía corromper archivos si el servidor ignoraba `Range`
3. `check_disk_space` no consideraba el espacio extra para descarga paralela
4. Llamadas síncronas bloqueaban el event loop
5. Futures huérfanos sin gestionar
6. Archivos `.part` no se limpiaban en cancelación
7. Sin protección SSRF
8. Sin allowlist de usuarios
9. Sin detección de Content-Type
10. SIGTERM sin handler registrado
11. Sin timeout en upload S3
12. Sesión Telethon no persistente → `FloodWaitError` tras múltiples deploys

## 🆘 Troubleshooting

### `FloodWaitError: A wait of N seconds is required`

**Causa:** Telegram rate-limita logins. Si re-desplegas varias veces seguidas en poco tiempo, Telegram te bloquea por N segundos (~36 min en algunos casos).

**Solución inmediata:**
1. Espera los N segundos indicados por Telegram (no reintentes antes, eso empeora).
2. El bot ahora maneja `FloodWaitError` automáticamente: duerme el tiempo indicado y sale limpio. Render lo reiniciará pasado ese tiempo.

**Solución permanente:**
1. Tras el primer arranque exitoso, copia el `SESSION_STRING` de los logs (mensaje `⚠️ PRIMER LOGIN —`).
2. Configúralo como variable de entorno `TG_SESSION_STRING`.
3. A partir de ahí, el bot nunca más hace login contra Telegram → no más FloodWait.

### Sesión se pierde en cada deploy (Render free)

Si no quieres usar `TG_SESSION_STRING`, asegúrate de que:
1. El disco persistente definido en `render.yaml` está correctamente montado en `/tmp/tg_uploads` (o el valor de `DOWNLOAD_PATH`).
2. El plan de Render soporta discos persistentes (el plan free los soporta con tamaño máximo 1 GB).

El bot guarda la sesión en `DOWNLOAD_PATH/tg_s3_bot.session`. Si ese path es persistente, no necesitas re-login en cada deploy.

### `AttributeError: 'TelegramClient' object has no attribute 'add_handler'`

Esto ya está corregido. El método correcto es `add_event_handler(callback, event)`. Si lo ves, actualiza a la última versión del código.

## 📄 Licencia

Uso personal/educativo.
