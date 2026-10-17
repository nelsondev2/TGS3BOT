# Bot de Telegram (Telethon) - Subida a S3

Bot de Telegram que recibe enlaces o archivos reenviados, los sube a S3 y **siempre devuelve la URL pública**. Por defecto solo envía la URL como texto (ahorra datos en toDus); con `/sendfile` también reenvía el archivo.

## ✨ Características

- ✅ **Cliente Telethon** (modo **bot oficial** con `BOT_TOKEN` o **userbot** con `SESSION_STRING`)
- ✅ **La URL S3 siempre se muestra al usuario**
- ✅ **Sin límite de tamaño por defecto**: `MAX_FILE_SIZE_MB=0`; Telegram impone su tope físico
- ✅ **Detección correcta de enlaces con vista previa** (no sube la miniatura)
- ✅ **Cola por usuario**: los álbumes y envíos seguidos se procesan en orden
- ✅ **Protección SSRF**: bloquea IPs no globales y valida **cada redirección**
- ✅ **Allowlist de usuarios** opcional vía `ALLOWED_TG_IDS`
- ✅ **Event loop no bloqueado**: descargas/upload en hilos y `asyncio`
- ✅ **Descarga paralela robusta**: rechaza respuestas `200 OK` en `Range` y cancela las tareas hermanas
- ✅ **Detección de extensión desde `Content-Type`** cuando la URL no la tiene
- ✅ **Limpieza de archivos `.part`** incluso al cancelar o fallar
- ✅ **Envío por Telegram con tipo correcto** (imagen/video/audio/voz/documento) y captions sin Markdown roto
- ✅ **SIGTERM handler** para shutdown graceful
- ✅ **Healthchecks** en `/health`, `/healthz` y `/ready`
- ✅ **Watchdog que no interrumpe trabajos activos**
- ✅ **Sesión de Telegram fuera de los logs**
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

> **Nota sobre Render free:** el plan free no permite discos persistentes y suspende el servicio tras 15 minutos sin tráfico HTTP. Para un bot de Telegram necesitas un plan siempre encendido o un ping externo. Si usas free, configura `TG_SESSION_STRING` para no repetir el login en cada deploy. Para archivos grandes, usa un plan de pago con disco (ver comentario en `render.yaml`).

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

1. Ejecuta `python scripts/generate_session.py` en tu máquina local.
2. Sigue las instrucciones (teléfono, código y 2FA si lo tienes).
3. Guarda el string resultante como `TG_SESSION_STRING`.

### Paso 2.5: Generar SESSION_STRING para un BOT (sin discos persistentes)

Si tu plataforma tiene sistema de archivos efímero (como Render free) y quieres evitar re-logins:

1. En **tu máquina local** ejecuta:

```bash
python scripts/generate_bot_session.py
```

2. Introduce API_ID, API_HASH y el token de @BotFather.
3. Copia el string que imprime la terminal (no se escribe en los logs del servidor).
4. Configúralo como `TG_SESSION_STRING`.
5. Borra `TG_BOT_TOKEN` (o déjalo; `TG_SESSION_STRING` tiene prioridad) y redéploy.

**Importante:** esto evita el `FloodWaitError` que ocurre cuando Telegram rate-limita múltiples logins seguidos. El bot detecta automáticamente si la sesión es de bot o de usuario, así que una sesión de bot con `SESSION_STRING` sigue funcionando como bot (no activa el filtro de userbot).

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
| `MAX_FILE_SIZE_MB` | `0` | Tamaño máximo; `0` = **sin límite** (Telegram impone el suyo) |
| `TG_UPLOAD_LIMIT_MB` | `0` | Comprobación previa al enviar a Telegram; `0` = sin límite |
| `CHUNK_SIZE_MB` | `8` | Tamaño de chunk de descarga |
| `DOWNLOAD_TIMEOUT_SECONDS` | `3600` | Tiempo máximo de descarga (no es límite de tamaño) |
| `UPLOAD_TIMEOUT_SECONDS` | `3600` | Tiempo máximo de subida a S3 (no es límite de tamaño) |
| `WATCHDOG_ENABLED` | `1` | Activa watchdog |
| `WATCHDOG_LIFETIME` | `14100` | Segundos antes de reinicio (espera a que no haya jobs) |
| `PARALLEL_URL_DOWNLOAD` | `1` | Descarga paralela |
| `PARALLEL_URL_PARTS` | `4` | Partes paralelas |
| `PARALLEL_URL_MIN_SIZE_MB` | `50` | Tamaño mínimo para intentar descarga paralela |
| `SEND_FILE_VIA_TG` | `0` | Por defecto NO reenvía archivo (solo URL). `1` = reenviar siempre; los usuarios pueden activar con `/sendfile` |
| `LOG_LEVEL` | `INFO` | Nivel de logging |
| `S3_ENDPOINT` | `https://s3.todus.cu` | Endpoint S3 |
| `S3_BUCKET` | `stream` | Bucket S3 |
| `S3_REGION` | `us-east-1` | Región S3 |
| `S3_UNSIGNED` | `1` | S3 sin firma (toDus) |
| `S3_ACCESS_KEY` | `public` | Key (si S3_UNSIGNED=0) |
| `S3_SECRET_KEY` | `public` | Secret (si S3_UNSIGNED=0) |
| `S3_PUBLIC_BASE_URL` | (vacío) | Base pública opcional (CDN/dominio propio) |

Ver `.env.example` para referencia.

## 📋 Comandos disponibles en el bot

| Comando | Descripción |
|---------|-------------|
| `/start`, `hola`, `ayuda`, `help` | Mensaje de ayuda |
| `/cancel`, `cancelar` | Cancela tu trabajo activo |
| `/status` | Verifica si tienes trabajo en curso |
| `/sendfile` | Toggle: recibir también el archivo (gasta datos) — por defecto OFF; requiere `SEND_FILE_VIA_TG=1` |
| `/textonly` | Desactiva el envío del archivo (solo URL); idempotente |
| `<URL>` | Procesa cualquier URL http/https |
| `<archivo reenviado>` | Sube cualquier foto/video/audio/documento reenviado a S3 |

### Comportamiento por defecto (ahorra datos)

Por defecto el bot **SOLO envía la URL S3 como texto**, no reenvía el archivo. Esto es porque en toDus:
- ✅ Mensajes de texto = ilimitados (no consumen plan)
- ❌ Multimedia (fotos, videos, archivos) = consumen los 600 MB / 1 GB del plan

Si quieres recibir el archivo además de la URL, escribe `/sendfile` una vez. Para volver al modo ahorro, escribe `/sendfile` otra vez.

Para cambiar el comportamiento por defecto globalmente (todos los usuarios reciben archivo por defecto), configura la env var `SEND_FILE_VIA_TG=1`. Si el administrador deja `SEND_FILE_VIA_TG=0`, `/sendfile` responde que el envío está deshabilitado.

## 🎨 Interfaz minimalista

El bot ahora usa mensajes limpios y profesionales, sin caracteres ruidosos ni información irrelevante. Ejemplo de flujo:

### Mensaje inicial al enviar URL o archivo
```
⏳ Procesando...
```

### Progreso (se edita en el mismo mensaje)
```
⏳ Descargando archivo.mp4 · 15.2 MB
```

### Resultado final (modo default, solo URL)
```
✓ Subido a S3
📎 Origen: canalVideosCubanos
📄 archivo.mp4
MP4 · 15.2 MB · nube

🔗 https://s3.todus.cu/stream/a1b2c3d4_archivo.mp4
```

### Resultado final (modo `/sendfile`, recibe el archivo)
```
✓ Enviado como video · 15.2 MB
```
*(el archivo se envía como media con el caption del resultado)*

### Mensajes de error
```
✗ Archivo de 3.2 GB — Telegram no admite ese tamaño en modo bot.
```

### Comando /help
```
🤖 Bot S3 — sube archivos a la nube y te devuelve el enlace

¿Qué puedo hacer?
  • Envía un enlace http(s)://... y lo subo
  • Reenvía un archivo (foto, video, audio, doc) y lo subo

Por defecto solo te envío la URL como texto (ahorra datos en toDus).
Si quieres recibir el archivo además de la URL, usa /sendfile.

Comandos
  /cancel — cancela tu trabajo actual
  /status — muestra si tienes trabajo en curso
  /sendfile — toggle: recibir archivo + URL (gasta datos)
```

## 📊 ¿toDus consume megas por mensajes de texto?

**No.** Según investigación realizada (fuentes: cubadebate.cu, etecsa.cu, todus.cu, fonoma.com):

### Plan toDus (25 CUP, 35 días)
- ✅ **Mensajería por texto ILIMITADA** — los mensajes de texto no consumen datos del plan
- 📦 **600 MB / 1 GB de datos nacionales** — se usan para multimedia (fotos, videos, archivos)

### ¿Qué consume el plan de datos?
- ❌ Enviar/recibir imágenes
- ❌ Enviar/recibir videos
- ❌ Enviar/recibir archivos
- ❌ Enviar/recibir notas de voz
- ✅ **NO consume**: mensajes de texto puros

### ¿Se puede enmascarar multimedia como texto?

**Técnicamente sí, pero es impráctico:**
1. **Base64**: un archivo de 10 MB se convierte en ~13.3 MB en base64 (33% más grande)
2. **Límite de mensaje**: toDus/Telegram tienen límites por mensaje (4096 chars)
3. Un archivo de 10 MB necesitaría ~2500 mensajes de texto
4. El receptor tendría que decodificar y concatenar manualmente

**Solución mejor que ya está implementada en el bot:**

El bot envía la **URL pública S3 como mensaje de texto**, que es ilimitado en toDus. El receptor puede:
- Ver la URL sin gastar datos
- Descargar el archivo solo cuando tenga WiFi o quiera gastar datos
- Compartir la URL por otras vías (SMS, etc.)

**Para activar este modo en el bot:**
```
/textonly
```
A partir de ese momento, el bot solo enviará URLs S3 (texto), no reenviará el archivo como media. Para volver a recibir el archivo, escribe `/sendfile`.

## 📎 Aceptar archivos reenviados

El bot ahora soporta dos formas de entrada:

### 1. Enviar URL (como antes)
```
https://example.com/archivo.mp4
```

### 2. Reenviar o subir un archivo
- Reenvía cualquier mensaje que contenga media (foto, video, audio, documento) al bot
- O sube un archivo directamente desde tu dispositivo al chat del bot
- El bot lo descarga, lo sube a S3 y te devuelve la URL (y el archivo si tienes `/sendfile` activo)

### Tipos soportados
| Tipo | Extensiones |
|------|-------------|
| Imagen | .jpg, .jpeg, .png, .gif, .webp, .bmp |
| Video | .mp4, .mkv, .mov, .avi, .webm, .m4v, .3gp |
| Audio | .mp3, .wav, .m4a, .aac, .flac |
| Voz | .ogg, .opus |
| Documento | cualquier otro |

## 🏥 Health endpoints

- `GET /` → `{"status":"online","service":"telegram-s3-bot"}`
- `GET /health` → estado, uptime, jobs activos/en cola, watchdog, tg_connected
- `GET /healthz` → alias para Render
- `GET /ready` → `200` si Telegram está conectado, `503` si no

## 🛡️ Seguridad

- **SSRF Protection**: bloquea IPs no globales (privadas, loopback, link-local, CGNAT) y valida **cada redirección** antes de seguirla.
- **Allowlist de IDs**: si defines `ALLOWED_TG_IDS`, solo esos IDs pueden usar el bot (en userbot el dueño siempre puede).
- **Sin límite de tamaño por defecto**: `MAX_FILE_SIZE_MB=0`. Se comprueba el espacio real en disco antes de descargar; Telegram impone su propio tope por archivo.
- **Sanitización de nombres**: previene path traversal y limita el nombre a 180 bytes.
- **Sesión fuera de logs**: el `SESSION_STRING` nunca se imprime ni se escribe en disco por el bot.

## 📝 Notas importantes

### Sobre el watchdog
El watchdog reinicia el proceso cada `WATCHDOG_LIFETIME` segundos (default: 3h 55m). Si hay trabajos activos, espera hasta 1 hora a que terminen antes de reiniciar. Desactívalo con `WATCHDOG_ENABLED=0`.

### Sobre el modo bot vs userbot
- **Modo bot** (recomendado): usa un bot creado con @BotFather. Tiene límites estándar (2GB por archivo) y los usuarios deben iniciar el bot con `/start`. **No intercepta conversaciones con otros bots.**
- **Modo userbot**: usa tu cuenta personal. El bot escucha también tus mensajes salientes **solo en Saved Messages**, y detecta sus propias respuestas para no entrar en bucles. Pega URLs o reenvía archivos ahí. No interfiere con tus conversaciones con otros bots o usuarios.

### Sobre el S3
El S3 por defecto es el público de toDus (`s3.todus.cu`, sin firma). Puedes cambiarlo a cualquier S3 compatible (AWS, MinIO, Cloudflare R2, etc.) seteando `S3_ENDPOINT`, `S3_BUCKET`, `S3_UNSIGNED=0`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`.

### Sobre el disco
- `/tmp/tg_uploads` es efímero en Render free (se pierde en cada deploy).
- Los archivos temporales se eliminan después de subirlos a S3 y enviarlos por Telegram.
- El plan free de Render **no permite discos persistentes**. Para archivos grandes o sesión persistente necesitas un plan de pago con disco (ver el bloque comentado en `render.yaml`).
- `check_disk_space` usa el tamaño real esperado, no `2 × MAX_FILE_SIZE`.

## 🐛 Bugs corregidos respecto a la versión original

1. **Un enlace con vista previa se trataba como media** y podía subir la miniatura en vez del archivo
2. **El modo userbot no veía los mensajes de Saved Messages** (`incoming=True` descarta salientes)
3. **Una sesión de bot con `SESSION_STRING` activaba el filtro de userbot** y dejaba el bot mudo
4. **SSRF evadible por redirecciones**; ahora se valida cada salto
5. **`check_disk_space` exigía 2 × MAX_FILE_SIZE** para cualquier archivo
6. **Tareas huérfanas en descarga paralela** que seguían escribiendo `.part`
7. **`Accept-Encoding: br` sin Brotli** provocaba errores de decodificación
8. **El `SESSION_STRING` se escribía en logs y disco**
9. **Uploads S3 seriales y con `Content-Type: application/octet-stream`**
10. **El watchdog mataba trabajos activos**
11. **FloodWait bloqueaba el event loop** y tumbaba el healthcheck
12. **Álbumes**: solo se procesaba la primera foto
13. **Markdown roto** en `/help` y captions con nombres especiales
14. **Nombres de archivo sin truncar** → `ENAMETOOLONG`
15. **`/sendfile` decía “activado” aunque el administrador lo tuviera desactivado**

## 🆘 Troubleshooting

### `FloodWaitError: A wait of N seconds is required`

**Causa:** Telegram rate-limita logins. Si re-desplegas varias veces seguidas en poco tiempo, Telegram te bloquea por N segundos (~36 min en algunos casos).

**Solución inmediata:**
1. Espera los N segundos indicados por Telegram (no reintentes antes, eso empeora).
2. El bot ahora maneja `FloodWaitError` automáticamente: duerme el tiempo indicado y sale limpio. Render lo reiniciará pasado ese tiempo.

**Solución permanente:**
1. Ejecuta `python scripts/generate_bot_session.py` en tu máquina local.
2. Copia el string que imprime y configúralo como `TG_SESSION_STRING`.
3. A partir de ahí, el bot no vuelve a hacer login contra Telegram → no más FloodWait.

### Sesión se pierde en cada deploy (Render free)

La forma recomendada es usar `TG_SESSION_STRING`: el plan free de Render no permite discos persistentes y el sistema de archivos se pierde en cada deploy.

Si usas un plan de pago:
1. Descomenta el bloque `disk` de `render.yaml` y ajusta `sizeGB`.
2. Asegúrate de que está montado en `/tmp/tg_uploads` (o tu `DOWNLOAD_PATH`).
3. El bot guarda la sesión en `DOWNLOAD_PATH/tg_s3_bot.session`; si ese path es persistente, no necesitas re-login.

### `AttributeError: 'TelegramClient' object has no attribute 'add_handler'`

Esto ya está corregido. El método correcto es `add_event_handler(callback, event)`. Si lo ves, actualiza a la última versión del código.

### El bot no responde en modo userbot

**Síntoma:** Estás en modo userbot (`TG_SESSION_STRING` de una cuenta personal) y el bot no reacciona a lo que escribes.

**Comprobaciones:**
1. Escribe en **Saved Messages** (tu propio chat), no en el chat de otra persona. El bot solo escucha tus mensajes salientes en ese chat.
2. El mensaje debe contener una URL, ser un archivo/foto/video o un comando (`/start`, `/status`, `/cancel`, `/sendfile`).
3. El bot ignora sus propias respuestas para no entrar en bucles; si ves un mensaje con `✓`, `⏳`, `✗` o `🤖`, es del bot y no se reprocesa.
4. En userbot, los mensajes de otros usuarios solo se atienden si están en `ALLOWED_TG_IDS`; el dueño siempre está permitido.
5. Si usas una sesión de **bot** con `TG_SESSION_STRING`, el bot funciona en modo bot normal (no necesitas Saved Messages).

## 📄 Licencia

Apache License 2.0 — ver el archivo `LICENSE`.
