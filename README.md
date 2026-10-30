# Bot de Telegram (Telethon) - Subida a S3

Bot de Telegram que escucha mensajes, descarga URLs enviadas por usuarios, las sube a S3 y **reenvía el archivo al usuario por Telegram + SIEMPRE muestra la URL pública S3**.

## ✨ Características

- ✅ **Cliente Telethon** (soporta modo **bot oficial** con `BOT_TOKEN` o modo **userbot** con `SESSION_STRING`)
- ✅ **La URL S3 siempre se muestra al usuario** (fix principal)
- ✅ **Protección SSRF**: bloquea IPs privadas/loopback/link-local
- ✅ **Allowlist de usuarios** opcional vía `ALLOWED_TG_IDS`
- ✅ **Event loop no bloqueado**: llamadas síncronas envueltas en `asyncio.to_thread`
- ✅ **Subida a toDus resistente al barrido**: PUT directo en una sola petición (sin multipart de boto3) y espejos en varios namespaces. Medido en vivo con `scripts/probe_todus.py` (26-27 oct): el bucket barre namespaces enteros sin aviso (la raíz y `users/tg_*` en <2 min; `dk/` cayó de un día para otro; `users/df/` pasó a morir a los 10-18 min; `hi/` es hoy el refugio más sólido) — eso era lo que hacía que los enlaces vencieran casi al instante
- ✅ **Verificación + guardián anti-barrido + memoria**: el objeto se comprueba con HEAD al subir y el enlace se entrega **al instante** (principal + espejo, subidos en paralelo); un guardián en segundo plano espera 150 s, re-verifica las copias y, si el barrido borró alguna, **edita tu mensaje** con el enlace bueno (o marca el error). La memoria recuerda qué namespace sobrevive; con `S3_DELIVER_FAST=0` se vuelve a la espera bloqueante previa
- 📲 **Subida OFICIAL a toDus (ToDus-SDK)**: con una cuenta de toDus (login desde el propio bot con `/toduslogin` por SMS), cada subida se **reserva** en el servidor (`todus:purl`, como la app oficial) y el objeto queda registrado fuera del barrido de anónimos. Si la vía oficial falla, cae sola a la anónima adaptativa. Es la respuesta a que hasta `hi/` acabara venciendo
- 📋 **Varios archivos seguidos, sin rechazos**: reenvía los que quieras — se encolan y se suben **uno a uno**, cada uno en **un solo mensaje** que se edita con el % en vivo hasta convertirse en la tarjeta con el enlace. En archivos grandes la descarga y la subida van **solapadas** (⚡ `Recibiendo 60% · Subiendo 55%`): el bot empieza a subir a toDus mientras aún recibe de Telegram, y el total se acerca a la más lenta de las dos fases en vez de a la suma (`STREAM_OVERLAP=0` lo desactiva). Al terminar un lote de ≥2 recibes un `enlaces.txt` con todas las URLs (una por línea, preparadas para que el gestor descargue cada archivo con su nombre original (el nombre va en la propia URL/`Content-Disposition`, más `#filename=` de refuerzo)), listo para importar en **IDM/1DM+** (Android; si su explorador no lo ve en Android 11+, es el *scoped storage*: guárdalo en Descargas y usa el icono de la mano = selector del sistema)
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
| `SEND_FILE_VIA_TG` | `0` | Por defecto NO reenvía archivo (solo URL). Cambiar a `1` para reenviar archivo. Los usuarios pueden opt-in con `/sendfile` |
| `TODUS_UPLOAD_MODE` | `auto` | `auto` = vía oficial si hay cuenta de toDus (y anónima si no la hay o si la oficial falla). `official` = como `auto` (el fallback se mantiene por resiliencia). `anonymous` = nunca usar el SDK (comportamiento anterior) |
| `TODUS_PHONE` | *(vacío)* | Número cubano (53XXXXXXXX) de la cuenta de toDus del bot. Opcional si haces login con `/toduslogin`; si está definida, manda sobre el número guardado |
| `TODUS_PASSWORD` | *(vacío)* | Contraseña/secreto (96 hex) de esa cuenta. Opcional: `/toduslogin` la obtiene por SMS y la guarda en `todus_credentials.json` |
| `TODUS_PERSISTENT` | `1` | Pedir la reserva `todus:purl` como persistente (`persistent=true`); si el servidor la rechaza, reintenta solo con `persistent=false` (modo "chat" de la app) |
| `TODUS_ANON_MIRROR` | `0` | `1` = tras una subida oficial OK, subir además una copia anónima como espejo (doble subida; por defecto no) |
| `TODUS_UPLOAD_AS_FILE` | `1` | Reservar todo como tipo FILE (0) en la vía oficial, sea video/audio/foto: los objetos de tipo video quedan en `todus/video/...`, sin lectura pública (`AccessDenied`), y el enlace no serviría fuera de la app (bug #22). `0` = tipo por extensión (anterior) |
| `ADMIN_TG_IDS` | *(vacío)* | CSV de IDs de Telegram que pueden usar `/toduslogin` y compañía en **modo bot**. En modo userbot no hace falta (solo el dueño). Sin esto, el login interactivo queda deshabilitado en modo bot (usar `TODUS_PHONE`/`TODUS_PASSWORD`) |
| `LOG_LEVEL` | `INFO` | Nivel de logging |
| `S3_ENDPOINT` | `https://s3.todus.cu` | Endpoint S3 |
| `S3_BUCKET` | `stream` | Bucket S3 |
| `S3_UNSIGNED` | `1` | S3 sin firma (toDus): subida con PUT directo. `0` = boto3 firmado contra tu propio S3 |
| `S3_KEY_PREFIXES` | `hi,users/df,dk/tg_{user_id}` | Namespaces candidatos (CSV, en orden) para las keys: `<prefijo>/<user_id>/<rand8>/<nombre>`. El último segmento es el nombre legible: es de donde los gestores (1DM+/IDM) sacan el nombre al descargar; la unicidad la da el `<rand8>` intermedio. El bot sube a `S3_MIRRORS` de ellos y recuerda cuál sobrevive (memoria en `s3_namespace_state.json`). Medido el 2026-10-27: `hi/` vive ≥28 min (objetos de ~16 h), `users/df/` muere a los 10-18 min, `dk/` y `video/` en <2 min. Acepta el placeholder `{user_id}` |
| `S3_MIRRORS` | `2` | A cuántos namespaces subir cada archivo (principal + espejos). Solo en modo unsigned; con S3 propio firmado siempre es 1 |
| `S3_SETTLE_SECONDS` | `150` | Ventana anti-barrido: un guardián espera este tiempo tras la entrega y re-verifica las copias (caza el barrido rápido de <2 min y edita tu mensaje si alguna murió; contra barridos lentos protege el espejo). `0` = desactivada |
| `S3_DELIVER_FAST` | `1` | `1` = entrega inmediata en cuanto pasa la 1ª verificación (el settle lo vigila el guardián en segundo plano). `0` = esperar todo el settle antes de entregar ningún enlace (modo clásico) |
| `STREAM_OVERLAP` | `1` | Solape descarga→subida: la subida a S3 lee el temporal mientras la descarga lo escribe (Telegram o URL secuencial con tamaño conocido), y el tiempo total pasa de bajar+subir a ≈la fase más lenta. El temporal siempre se guarda entero: si el solape falla, se reintenta con la subida clásica desde el disco. `0` = clásico (bajar entero y luego subir) |
| `STREAM_OVERLAP_MIN_MB` | `4` | Tamaño mínimo para activar el solape (por debajo no compensa) |
| `STREAM_STALL_SECONDS` | `300` | Si la descarga deja de producir bytes este tiempo, la subida solapada en curso se aborta (luego se sube clásico) |
| `S3_KEY_PREFIX` | *(heredado)* | Solo compatibilidad: `users`/`users/df` (valores viejos) = usar la lista por defecto de `S3_KEY_PREFIXES`. Cualquier otro valor se respeta como único namespace. `none` = raíz (solo buckets propios) |
| `S3_UPLOAD_RETRIES` | `3` | Reintentos de subida + verificación ante fallos transitorios |
| `S3_UPLOAD_TIMEOUT` | `1800` | Tiempo total máximo (s) para subir y verificar un archivo |
| `S3_VERIFY_UPLOAD` | `1` | Comprobar con HEAD que el objeto existe antes de entregar el enlace |
| `S3_VERIFY_ATTEMPTS` | `6` | Intentos de la verificación HEAD (con backoff) |
| `S3_ACCESS_KEY` | `public` | Key (si S3_UNSIGNED=0) |
| `S3_SECRET_KEY` | `public` | Secret (si S3_UNSIGNED=0) |

Ver `.env.example` para referencia.

## 📋 Comandos disponibles en el bot

| Comando | Descripción |
|---------|-------------|
| `/start`, `hola`, `ayuda`, `help` | Mensaje de ayuda |
| `/cancel`, `cancelar` | Cancela tu trabajo en curso y vacía tu cola |
| `/status` | Muestra cuántos trabajos tienes en curso o en cola |
| `/sendfile` | Toggle: recibir también el archivo (gasta datos) — por defecto OFF |
| `/toduslogin <número>` | *(solo admin)* Login de la cuenta de toDus del bot por SMS (o `/toduslogin <número> <contraseña>`) |
| `/toduscode <código>` | *(solo admin)* Completa el login por SMS (o responde solo con el código) |
| `/todusstatus` | *(solo admin)* Estado de la sesión de toDus |
| `/toduslogout` | *(solo admin)* Cierra la sesión de toDus (vuelve a la vía anónima) |
| `/textonly` | Alias de `/sendfile` (compatibilidad) |
| `<URL>` | Procesa cualquier URL http/https |
| `<archivo reenviado>` | Sube cualquier foto/video/audio/documento reenviado a S3. Puedes reenviar **varios seguidos**: suben en cola, uno a uno, y al final recibes un `enlaces.txt` con todos los enlaces (y el nombre original de cada archivo) para IDM/1DM+ |

### Comportamiento por defecto (ahorra datos)

Por defecto el bot **SOLO envía la URL S3 como texto**, no reenvía el archivo. Esto es porque en toDus:
- ✅ Mensajes de texto = ilimitados (no consumen plan)
- ❌ Multimedia (fotos, videos, archivos) = consumen los 600 MB / 1 GB del plan

Si quieres recibir el archivo además de la URL, escribe `/sendfile` una vez. Para volver al modo ahorro, escribe `/sendfile` otra vez.

Para cambiar el comportamiento por defecto globalmente (todos los usuarios reciben archivo por defecto), configura la env var `SEND_FILE_VIA_TG=1`.

### Subida oficial con cuenta de toDus (ToDus-SDK) — la vía que no vence

La vía anónima depende de que el servidor respete algún *namespace*, y toDus rota las reglas sin aviso: `dk/` cayó en un día, `users/df/` pasó a morir a los 10-18 min y hasta `hi/` acabó venciendo. La vía con garantías es la de la **app oficial**: antes de subir, el cliente **reserva** la subida por XMPP (`todus:purl`) con una cuenta de toDus; el servidor asigna las URLs de subida/descarga y el objeto queda **registrado** a nombre de la cuenta, fuera del barrido de objetos anónimos. El bot lo hace con [ToDus-SDK](https://github.com/nyxthor-dev/toDus-SDK) y el login se hace **desde el propio bot**:

**Activarlo (una sola vez):**
1. En **modo bot**, define `ADMIN_TG_IDS` con tu ID de Telegram (solo esos IDs pueden gestionar la cuenta; en modo userbot no hace falta: solo el dueño).
2. Escríbele al bot `/toduslogin 53XXXXXXXX` con un número cubano que controles: toDus enviará un **SMS con un código** a ese número.
3. Responde al bot con el código (o `/toduscode 123456`). El bot registra la cuenta, guarda la contraseña que devuelve el servidor en `DOWNLOAD_PATH/todus_credentials.json` (permisos 0600) y queda logueado. Acto seguido te la muestra **una sola vez** (solo en chats privados): guárdala — es la que fijarás en `TODUS_PASSWORD` si tu servicio no tiene disco — y borra ese mensaje.
4. Sube algo: en los logs verás `ToDus: reserva OK (persistent=True)` y las URLs asignadas por el servidor.

Alternativa sin pasar por Telegram: define `TODUS_PHONE` y `TODUS_PASSWORD` en el entorno. Con `/todusstatus` ves el estado de la sesión y con `/toduslogout` la cierras (vuelve la vía anónima).

**Cómo se comporta:**
- Pide la reserva con `persistent=true`; si el servidor la rechaza, reintenta con `persistent=false` (el modo "chat" de la app). Se puede forzar con `TODUS_PERSISTENT=0`.
- El PUT va en streaming a la URL asignada, como la app (`application/octet-stream`, longitud exacta); si la URL prefirmada caduca (4xx) se vuelve a reservar. La URL de descarga se verifica antes de entregarla (GET con Range, porque un HEAD puede romper la firma de una URL prefirmada para GET) y las URLs cortas `todus.cu/...` se resuelven a la real.
- Todo se reserva como **tipo FILE** (`TODUS_UPLOAD_AS_FILE=1`), sea video, audio o foto: los objetos de tipo video caen bajo `todus/video/...`, un prefijo **sin lectura pública** (`AccessDenied`, verificado en producción), y el enlace no serviría fuera de la app; los de `todus/file/...` sí descargan sin credenciales. Y la verificación cuenta 401/403 como enlace muerto, con rescate vía `todus:gurl`: nunca se entrega un «✓» que no descargue (bug #22).
- El JWT se renueva solo cuando caduca, con la contraseña guardada.
- **Fallback total:** si cualquier paso oficial falla (sin red hacia toDus, sesión rechazada, SDK caído…), la subida cae automáticamente a la vía anónima adaptativa de siempre. Nunca se pierde una subida por el SDK.
- En Render, las credenciales sobreviven a reinicios/redespliegues solo si `DOWNLOAD_PATH` está en un disco persistente — y **Render solo ofrece discos en planes de pago**: con `plan: free` el bloque `disk:` del blueprint no crea ningún disco y el login se perdería al primer reinicio. En el plan free, fija `TODUS_PHONE`/`TODUS_PASSWORD` (la contraseña te la muestra el bot una sola vez tras el login por SMS) y el bot entrará solo tras cada rearranque.

**Seguridad:** el login usa el flujo *legítimo* de la app (SMS a tu número o tu contraseña). El SDK también documenta `login_with_phone_only`, que explota que `/v2/auth/token` acepta cualquier UUID como "contraseña" para entrar como **cualquier** número: es un bypass de autenticación del servidor y en este bot **no se usa** — todo pasa por una cuenta tuya de verdad.

## 🎨 Interfaz minimalista

El bot ahora usa mensajes limpios y profesionales, sin caracteres ruidosos ni información irrelevante. Ejemplo de flujo:

### Mensaje inicial al enviar URL o archivo
```
⏳ Procesando...
```

### Progreso (un solo mensaje que se va editando, con % en vivo)
```
⏳ En cola (posición 2)…
⏳ Recibiendo 45% archivo.mp4 · 15.2 MB
⏳ Subiendo 78% archivo.mp4 · 15.2 MB
⏳ ⚡ Recibiendo 60% · Subiendo 55% archivo.mp4 · 15.2 MB   ← con solape: ambas fases a la vez
```
Ese mismo mensaje se convierte al final en la tarjeta con el enlace: no quedan mensajes intermedios sueltos en el chat.

### Resultado final (modo default, solo URL)
```
✓ Subido a S3
📎 Origen: canalVideosCubanos
📄 archivo.mp4
MP4 · 15.2 MB · nube

🔗 https://s3.todus.cu/stream/hi/123456789/archivo_a1b2c3d4.mp4
🔗 https://s3.todus.cu/stream/users/df/123456789/archivo_a1b2c3d4.mp4 (espejo)
```

Mientras el guardián anti-barrido sigue vigilando, la tarjeta puede llevar una última línea `⏳ comprobando que el enlace sobrevive…` que desaparece sola al confirmarse (o el enlace se sustituye/edita si el servidor lo borra).

### Resultado final (modo `/sendfile`, recibe el archivo)
```
✓ Enviado como video · 15.2 MB
```
*(el archivo se envía como media con el caption del resultado)*

### Mensajes de error
```
✗ Archivo de 3.2 GB — supera el límite de 2.0 GB.
```

### Comando /help
```
🤖 Bot S3 — sube archivos a la nube y te devuelve el enlace

¿Qué puedo hacer?
  • Envía un enlace http(s)://... y lo subo
  • Reenvía un archivo (foto, video, audio, doc) y lo subo

Por defecto solo te envío la URL como texto (ahorra datos en toDus).
Si quieres recibir el archivo además de la URL, usa /sendfile.

Descarga los enlaces pronto: el bucket público de toDus puede purgar archivos sin aviso.

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
A partir de ese momento, el bot solo enviará URLs S3 (texto), no reenviará el archivo como media. Para desactivar, escribe `/textonly` otra vez.

## 📎 Aceptar archivos reenviados

El bot ahora soporta dos formas de entrada:

### 1. Enviar URL (como antes)
```
https://example.com/archivo.mp4
```

### 2. Reenviar o subir un archivo
- Reenvía cualquier mensaje que contenga media (foto, video, audio, documento) al bot
- O sube un archivo directamente desde tu dispositivo al chat del bot
- El bot lo descarga, lo sube a S3 y te devuelve el archivo + URL

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
- **Modo bot** (recomendado): usa un bot creado con @BotFather. Tiene límites estándar (2GB por archivo) y los usuarios deben iniciar el bot con `/start`. **No intercepta conversaciones con otros bots.**
- **Modo userbot**: usa tu cuenta personal. Sin límites artificiales pero usa tu cuenta para enviar/recibir. **El bot solo responde a mensajes del dueño** (tú mismo) — pega URLs en tu chat *Saved Messages* para usarlo. No interfiere con tus conversaciones con otros bots o usuarios.

### Sobre el S3
El S3 por defecto es el público de toDus (`s3.todus.cu`, sin firma). Puedes cambiarlo a cualquier S3 compatible (AWS, MinIO, Cloudflare R2, etc.) seteando `S3_ENDPOINT`, `S3_BUCKET`, `S3_UNSIGNED=0`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`.

**Ojo con la permanencia en toDus:** el bucket `stream` tiene un barrido activo cuyas reglas CAMBIAN sin aviso (medido con `scripts/probe_todus.py` el 26-27 oct 2026): la raíz, `users/tg_*` y `video/` mueren en <2 min; `dk/` era refugio un día y amaneció vacío al siguiente; `users/df/` pasó de refugio a morir a los 10-18 min; `hi/` aguantó más (objetos de ~16 h el 27-oct) pero también acabó cayendo poco después. Por eso el bot ya no se casa con un namespace: sube a 2 namespaces (principal + espejo, hoy `hi/` + `users/df/`), espera 150 s para confirmar que sobreviven al barrido rápido antes de entregar los enlaces, y recuerda cuál sobrevivió para usarlo primero la próxima vez. Aun así, el bucket es público y anónimo también para borrar: ningún cliente puede hacer permanentes los enlaces en un bucket ajeno. Descarga pronto; para permanencia real usa tu propio S3 (`S3_UNSIGNED=0`) o activa la **vía oficial** con tu cuenta de toDus (`/toduslogin`, ver «Subida oficial con cuenta de toDus» más arriba), que es la que usa la app y no depende del barrido. Si los enlaces volvieran a caer, re-mide con `scripts/probe_todus.py` y ajusta `S3_KEY_PREFIXES`.

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
13. Subida con multipart de boto3 contra toDus (su backend MinIO no es AWS y los clientes estables usan un solo PUT, como `todus-client`) → PUT directo en una sola petición, verificado en vivo
14. Keys fuera de los namespaces que respeta el barrido de toDus (raíz en v1 y `users/tg_<id>/telegram/...` en v2: ambas eliminadas en <2 min aunque el PUT y la verificación inmediata fueran correctos — medido con `scripts/probe_todus.py`) → keys `users/df/<id>/...`
15. La URL S3 se entregaba sin comprobar que el objeto existiera → verificación HEAD + tamaño con re-subida automática
16. El fix de namespace fijo (`users/df/`) dejó de bastar en 24 h: toDus cambió el barrido y `users/df/` pasó a morir a los 10-18 min (y `dk/`, refugio el día anterior, a <2 min) → subida adaptativa: candidatos `S3_KEY_PREFIXES` con `hi/` primero, espejo en 2 namespaces, espera anti-barrido de 150 s antes de entregar y memoria persistente del namespace que sobrevive
17. Ni la subida adaptativa bastó a la larga: el barrido rotó otra vez, tumbó también `hi/` y los enlaces volvieron a vencer → **vía oficial con ToDus-SDK**: reserva `todus:purl` con cuenta propia (login por SMS desde el bot con `/toduslogin`, solo admin), PUT a la URL que asigna el servidor, JWT auto-renovable y caída automática a la vía anónima si algo falla
18. Reenviar varios archivos seguidos solo subía el primero («ya tienes un trabajo en curso» para el resto, y se perdían) y el «Subiendo» parecía colgado durante minutos: espejos secuenciales + espera anti-barrido bloqueante de 150 s + ninguna señal de progreso (y editar con el mismo texto creaba mensajes huérfanos) → **cola FIFO por usuario** con lote `enlaces.txt` para IDM/1DM+, espejos **en paralelo**, **% en vivo** en un único mensaje editado hasta entregar el enlace y guardián anti-barrido en segundo plano. Se elimina de paso toda la función UDYAT/`.udyat` (interfaz limpia)
19. El `enlaces.txt` del lote no aparecía en el explorador de 1DM+ y las descargas salían con el nombre de la key (`<stem>_<uuid8>`): lo primero es el *scoped storage* de Android 11+ (el explorador interno de 1DM+ no ve archivos de otras apps; su propio FAQ indica usar el icono de la mano —selector del sistema— tras guardar el .txt en Descargas; el caption ahora trae los pasos exactos y el plan B del portapapeles) y lo segundo se arregla con el truco que 1DM+ documenta en su changelog 15.4: cada línea lleva `#filename=<nombre original>` al final de la URL (el fragmento no viaja al servidor; si el gestor lo ignora, manda el `Content-Disposition` de siempre)
20. `#filename=` como único mecanismo de nombre no funcionó en Android: verificado por el usuario que 1DM+/IDM al importar el .txt nombran cada descarga desde la COLA de la URL (y la URL oficial `get=` de toDus es opaca, ni nombre ni extensión; la documentación del SDK recomienda por eso pasar el nombre aparte). Ahora el nombre se garantiza en origen: en la vía anónima la key termina en el nombre legible (`<prefijo>/<user_id>/<rand8>/<nombre>`, sin acentos; la unicidad la da el segmento intermedio), y en la oficial el PUT prefirmado intenta fijar `Content-Disposition` con el nombre exacto, reintentando sin ella si el servidor la rechaza por no estar firmada (la verificación registra en el log la cabecera que devuelve el GET, para confirmar si toDus la sirve). El `#filename=` se mantiene como refuerzo inofensivo
21. El procesamiento tardaba la suma de bajar (Telegram/URL→servidor) y subir (servidor→toDus), porque la subida empezaba solo con el archivo entero en disco. Ahora ambas fases se solapan (`STREAM_OVERLAP`): los PUT leen el temporal mientras crece a través de un cuerpo «follow» con `Content-Length` explícito —formato de red idéntico al PUT de siempre, sin `chunked` (verificado por captura: aiohttp con generador async + cabecera CL no usa chunked)—. Seguridad: una subida solapada solo puede terminar bien si la descarga escribió EXACTAMENTE el tamaño anunciado (el generador explota ante cualquier desviación, productor muerto o atasco >`STREAM_STALL_SECONDS`, y un PUT truncado no materializa el objeto); si el solape falla y la descarga acabó bien, se reintenta la subida clásica desde el disco, así que el peor caso es «como antes más un intento». No solapan: fotos de Telegram (sin tamaño previo), URLs sin `Content-Length` o con descarga paralela por rangos (partes sueltas) ni el modo firmado boto3 (espera al archivo completo)
22. Los enlaces oficiales de VIDEO «no servían»: la reserva pedía el tipo por extensión (3=VIDEO) y toDus asignaba el `get=` bajo `todus/video/<fecha>/<hash>`, un prefijo que el bucket no sirve en anónimo (`AccessDenied`, verificado contra un enlace real de producción; la app y el SDK —`_needs_auth`/`_download_headers`— descargan esos objetos con `Authorization: Bearer`). Los de tipo archivo (`todus/file/...`) sí son públicos. La verificación oficial además trataba el 403 como «no verificable» y entregaba el enlace igual. Arreglo triple: (1) se reserva TODO como FILE (0) salvo `TODUS_UPLOAD_AS_FILE=0` — somos un bot de enlaces descargables, no un cliente de chat —; (2) 401/403 cuentan como muerto en la verificación; (3) ante un `get=` no descargable se pide la URL real vía `todus:gurl` (lo que la app hace siempre antes de descargar) y se verifica también esa; si tampoco sirve, la subida cae a la vía anónima. El usuario siempre recibe un enlace que descarga

## 🆘 Troubleshooting

### Los enlaces dan 404 / "vencen" casi al instante de subirlos

**Causa (medida en vivo dos días seguidos):** el bucket de toDus barre los objetos anónimos según el *namespace* de la key, y las reglas rotan sin aviso. Con `scripts/probe_todus.py` (10 objetos de prueba por ronda, HEADs a 0/2/5/10/18/28 min): la raíz, `users/tg_*/telegram/...`, `video/` y namespaces inventados mueren en <2 min; `dk/` sobrevivía el 26-oct y murió en <2 min el 27-oct; `users/df/` sobrevivía el 26 y el 27 moría entre los 10 y los 18 min; `hi/` aguantó los 28 min y en el bucket hay objetos suyos de ~16 h. Todos los PUT devuelven 200 y la verificación inmediata pasa incluso en objetos que morirán en minutos: el transporte nunca fue el problema, es la key… y ningún namespace fijo es para siempre (el fix anterior, `users/df/`, duró 1 día).

**Ahora el bot (adaptativo, sin namespace fijo):**
1. Sube con un solo PUT en streaming (Content-Type real + Content-Disposition con el nombre original).
2. Sube a **2 namespaces candidatos** (`S3_KEY_PREFIXES`, por defecto `hi` → `users/df` → `dk/tg_{user_id}`) **a la vez**, entrega el enlace al pasar la 1ª verificación y un guardián lo re-verifica durante **`S3_SETTLE_SECONDS` (150 s)**, editando tu mensaje si el barrido borra alguna copia (`S3_DELIVER_FAST=0` restaura la espera bloqueante *antes* de entregar nada).
3. Entrega **enlace principal + espejo** en otro namespace: si un barrido lento tumba uno horas después, el otro puede seguir vivo. La tarjeta de resultado muestra ambos.
4. Si un namespace muere, queda relegado al final y el superviviente pasa a ser el primero (memoria en `s3_namespace_state.json`): el bot se re-adapta solo cuando toDus cambia las reglas. Solo si *ningún* namespace deja el archivo vivo verás un error honesto en vez de enlaces muertos.

**Actualización — la vía que ya no depende del barrido:** lo adaptativo de arriba sigue siendo el respaldo automático, pero incluso `hi/` terminó cayendo. Por eso el bot ahora intenta primero la **vía oficial** con una cuenta de toDus (reserva `todus:purl` vía ToDus-SDK; login desde el bot con `/toduslogin`, ver la sección «Subida oficial con cuenta de toDus»). Si tus enlaces siguen venciendo, comprueba con `/todusstatus` que hay sesión y que los logs dicen `ToDus: reserva OK`: sin cuenta, ya ningún namespace anónimo es fiable.

**¿Y los repos de referencia?** `miguelorlandos06/s3_bot` sube a la raíz del bucket (`<uuid>_<nombre>`) sin verificar: sobre el barrido actual es el patrón que muere en <2 min, así que no es un modelo a seguir para esto. `nyxthor-dev/toDus-SDK` implementa el protocolo oficial de toDus y revela el mecanismo "correcto": la app reserva la subida por XMPP (`todus:purl`, con flag `persistent`) y el servidor asigna la URL — esos objetos quedan registrados y no dependen de adivinar namespaces. Es la vía con garantías a futuro, pero requiere una **cuenta de toDus** (número + contraseña) dedicada al bot. (El SDK también documenta `login_with_phone_only`, un bypass de autenticación del servidor: no se usa aquí.)

**Si aun así un enlace cae con el tiempo:** el bucket de toDus es público y anónimo hasta para borrar — toDus o terceros pueden purgarlo sin aviso y eso no lo puede evitar ningún cliente anónimo. Descarga los archivos pronto o usa tu propio S3 (`S3_UNSIGNED=0` + tus claves) para enlaces permanentes.

### 1DM+/IDM no encuentra el `enlaces.txt` al importar

Es el *scoped storage* de Android 11+: el explorador interno de 1DM+ no puede ver archivos guardados por otras apps (Telegram). Solución (la del propio FAQ de 1DM+): guarda el `.txt` en la carpeta **Descargas**, ve a importar y, si no aparece, toca el **icono de la mano** en la barra de su explorador para abrir el **selector de archivos del sistema** y elígelo ahí. Alternativa sin archivos: abre el `.txt`, copia todo el contenido y usa «Importar desde el portapapeles».

Además, el nombre de la descarga se garantiza en origen (bug #20): en la vía anónima la URL termina en el nombre del archivo y en la oficial el objeto lleva `Content-Disposition` cuando el servidor lo acepta; cada línea conserva también el refuerzo `#filename=<nombre>` (ese fragmento no se envía al servidor).

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
2. El plan de Render admite discos persistentes (⚠️ el plan **free NO** tiene discos: o subes a un plan de pago, o usas `TG_SESSION_STRING`/`TG_BOT_TOKEN` y `TODUS_PHONE`/`TODUS_PASSWORD` por entorno, que no necesitan disco).

El bot guarda la sesión en `DOWNLOAD_PATH/tg_s3_bot.session`. Si ese path es persistente, no necesitas re-login en cada deploy.

### `AttributeError: 'TelegramClient' object has no attribute 'add_handler'`

Esto ya está corregido. El método correcto es `add_event_handler(callback, event)`. Si lo ves, actualiza a la última versión del código.

### El bot responde a cualquier mensaje que recibo en otros bots

**Síntoma:** Estás en modo userbot (`TG_SESSION_STRING` sin `TG_BOT_TOKEN`). Cuando hablas con @BotFather u otro bot y te responden, tu userbot intercepta la respuesta y dice "Envía un enlace...".

**Causa:** En modo userbot, el handler `events.NewMessage(incoming=True)` se dispara para **todos** los mensajes entrantes a tu cuenta, incluyendo respuestas de otros bots.

**Solución:** Ya está corregido en la versión actual. El bot ahora:
1. Detecta automáticamente el modo userbot.
2. Obtiene tu ID de Telegram tras el login.
3. Solo procesa mensajes donde `sender_id == tu_id` (tú mismo) o de IDs en `ALLOWED_TG_IDS`.
4. Ignora silenciosamente todos los demás mensajes (respuestas de otros bots, mensajes de terceros, etc.).

**Cómo usar el bot en modo userbot:**
- Abre tu chat **Saved Messages** (mensajes guardados) en Telegram.
- Pega ahí las URLs o reenvía archivos.
- El bot procesará esos mensajes y responderá en el mismo chat.
- Tus conversaciones con otros bots/usuarios no se ven afectadas.

Si quieres permitir que otros usuarios usen tu userbot, añade sus IDs a `ALLOWED_TG_IDS`.

## 📄 Licencia

Uso personal/educativo.
