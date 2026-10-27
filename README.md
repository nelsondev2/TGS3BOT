# Bot de Telegram (Telethon) - Subida a S3

Bot de Telegram que escucha mensajes, descarga URLs enviadas por usuarios, las sube a S3 y **reenvía el archivo al usuario por Telegram + SIEMPRE muestra la URL pública S3**.

## ✨ Características

- ✅ **Cliente Telethon** (soporta modo **bot oficial** con `BOT_TOKEN` o modo **userbot** con `SESSION_STRING`)
- ✅ **La URL S3 siempre se muestra al usuario** (fix principal)
- ✅ **Protección SSRF**: bloquea IPs privadas/loopback/link-local
- ✅ **Allowlist de usuarios** opcional vía `ALLOWED_TG_IDS`
- ✅ **Event loop no bloqueado**: llamadas síncronas envueltas en `asyncio.to_thread`
- ✅ **Subida a toDus resistente al barrido**: PUT directo en una sola petición (sin multipart de boto3) y espejos en varios namespaces. Medido en vivo con `scripts/probe_todus.py` (26-27 oct): el bucket barre namespaces enteros sin aviso (la raíz y `users/tg_*` en <2 min; `dk/` cayó de un día para otro; `users/df/` pasó a morir a los 10-18 min; `hi/` es hoy el refugio más sólido) — eso era lo que hacía que los enlaces vencieran casi al instante
- ✅ **Verificación + espera anti-barrido + memoria**: el enlace se entrega solo tras comprobar con HEAD (antes y después de una espera de 150 s) que el objeto sigue vivo; si desaparece, cae automáticamente al siguiente namespace y recuerda cuál sobrevivió. Se entrega **enlace principal + espejo** en otro namespace; nunca se entrega un enlace muerto como ✓
- 🆓 **Descarga sin megas (Cuba)**: con `/udyat` el bot envía además un manifiesto `.udyat` para la app IroS3Dl que apunta a la IP gratuita de toDus (`152.207.206.86`); la ayuda del bot trae también la receta manual para 1DM+ (cabeceras UDYAT)
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
| `SEND_UDYAT_VIA_TG` | `1` | Permite enviar el manifiesto `.udyat` (descarga sin megas en Cuba) a quien lo active con `/udyat`. Solo aplica si el S3 es `s3.todus.cu`. `0` desactiva la función |
| `UDYAT_FREE_IP` | `152.207.206.86` | IP gratuita de toDus escrita en el manifiesto `.udyat` (respaldo conocido: `152.207.206.88`) |
| `LOG_LEVEL` | `INFO` | Nivel de logging |
| `S3_ENDPOINT` | `https://s3.todus.cu` | Endpoint S3 |
| `S3_BUCKET` | `stream` | Bucket S3 |
| `S3_UNSIGNED` | `1` | S3 sin firma (toDus): subida con PUT directo. `0` = boto3 firmado contra tu propio S3 |
| `S3_KEY_PREFIXES` | `hi,users/df,dk/tg_{user_id}` | Namespaces candidatos (CSV, en orden) para las keys: `<prefijo>/<user_id>/<nombre>_<rand>.<ext>`. El bot sube a `S3_MIRRORS` de ellos y recuerda cuál sobrevive (memoria en `s3_namespace_state.json`). Medido el 2026-10-27: `hi/` vive ≥28 min (objetos de ~16 h), `users/df/` muere a los 10-18 min, `dk/` y `video/` en <2 min. Acepta el placeholder `{user_id}` |
| `S3_MIRRORS` | `2` | A cuántos namespaces subir cada archivo (principal + espejos). Solo en modo unsigned; con S3 propio firmado siempre es 1 |
| `S3_SETTLE_SECONDS` | `150` | Espera anti-barrido: tras subir, espera este tiempo y re-verifica antes de entregar el enlace (caza el barrido rápido de <2 min; contra barridos lentos protege el espejo). `0` = desactivada |
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
| `/cancel`, `cancelar` | Cancela tu trabajo activo |
| `/status` | Verifica si tienes trabajo en curso |
| `/sendfile` | Toggle: recibir también el archivo (gasta datos) — por defecto OFF |
| `/udyat` (alias `/sinmegas`) | Toggle: recibir también el manifiesto `.udyat` para descargar sin gastar megas en Cuba (IroS3Dl) — por defecto OFF |
| `/textonly` | Alias de `/sendfile` (compatibilidad) |
| `<URL>` | Procesa cualquier URL http/https |
| `<archivo reenviado>` | Sube cualquier foto/video/audio/documento reenviado a S3 |

### Comportamiento por defecto (ahorra datos)

Por defecto el bot **SOLO envía la URL S3 como texto**, no reenvía el archivo. Esto es porque en toDus:
- ✅ Mensajes de texto = ilimitados (no consumen plan)
- ❌ Multimedia (fotos, videos, archivos) = consumen los 600 MB / 1 GB del plan

Si quieres recibir el archivo además de la URL, escribe `/sendfile` una vez. Para volver al modo ahorro, escribe `/sendfile` otra vez.

Para cambiar el comportamiento por defecto globalmente (todos los usuarios reciben archivo por defecto), configura la env var `SEND_FILE_VIA_TG=1`.

### Descargar sin gastar megas (UDYAT, solo Cuba)

El servidor `s3.todus.cu` responde también en una **IP interna que ETECSA no factura** (`152.207.206.86`), siempre que el cliente conecte a esa IP con las cabeceras `Host: s3.todus.cu` y `User-Agent: Wget/1.21.3` e ignore el certificado self-signed (sistema UDYAT, documentado en el repo `juanbas2005/todus-s3-api`). El bot no puede hacer eso por el usuario, pero sí prepararle todo:

- **IroS3Dl (automático):** activa `/udyat` en el bot y, tras cada subida, recibirás un archivo `<nombre>.udyat` (JSON de ~300 B con nombre, tamaño, MD5 y la URL ya reescrita a la IP gratuita). Descárgalo e impórtalo en IroS3Dl (mantén presionado el botón ➕ en la pantalla de descargas) y la app bajará el archivo sin consumir megas.
- **1DM+ (manual):** 1DM+ admite cabeceras personalizadas por descarga. Añade una descarga con el enlace del bot **cambiando el host** por la IP: `https://152.207.206.86/stream/...`, y en las cabeceras (headers) de esa descarga pon `Host: s3.todus.cu` y `User-Agent: Wget/1.21.3`. Si la app avisa del certificado (esa IP usa uno self-signed), acéptalo para continuar.

> ⚠️ Esto solo funciona desde la red cubana de ETECSA. Verifica la primera vez con un archivo pequeño y comprueba el saldo antes/después: que salga a 0 MB depende de cómo ETECSA clasifique esa IP, y la IP puede cambiar (ajústala con `UDYAT_FREE_IP`; respaldo conocido: `152.207.206.88`).

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

🔗 https://s3.todus.cu/stream/hi/123456789/archivo_a1b2c3d4.mp4
🔗 https://s3.todus.cu/stream/users/df/123456789/archivo_a1b2c3d4.mp4 (espejo)
```

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

**Ojo con la permanencia en toDus:** el bucket `stream` tiene un barrido activo cuyas reglas CAMBIAN sin aviso (medido con `scripts/probe_todus.py` el 26-27 oct 2026): la raíz, `users/tg_*` y `video/` mueren en <2 min; `dk/` era refugio un día y amaneció vacío al siguiente; `users/df/` pasó de refugio a morir a los 10-18 min; `hi/` es hoy lo más sólido (objetos de ~16 h). Por eso el bot ya no se casa con un namespace: sube a 2 namespaces (principal + espejo, hoy `hi/` + `users/df/`), espera 150 s para confirmar que sobreviven al barrido rápido antes de entregar los enlaces, y recuerda cuál sobrevivió para usarlo primero la próxima vez. Aun así, el bucket es público y anónimo también para borrar: ningún cliente puede hacer permanentes los enlaces en un bucket ajeno. Descarga pronto; para permanencia real usa tu propio S3 (`S3_UNSIGNED=0`) o, en el futuro, el flujo oficial de toDus con cuenta propia (reserva XMPP `todus:purl`, ver [ToDus-SDK](https://github.com/nyxthor-dev/toDus-SDK)). Si los enlaces volvieran a caer, re-mide con `scripts/probe_todus.py` y ajusta `S3_KEY_PREFIXES`.

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

## 🆘 Troubleshooting

### Los enlaces dan 404 / "vencen" casi al instante de subirlos

**Causa (medida en vivo dos días seguidos):** el bucket de toDus barre los objetos anónimos según el *namespace* de la key, y las reglas rotan sin aviso. Con `scripts/probe_todus.py` (10 objetos de prueba por ronda, HEADs a 0/2/5/10/18/28 min): la raíz, `users/tg_*/telegram/...`, `video/` y namespaces inventados mueren en <2 min; `dk/` sobrevivía el 26-oct y murió en <2 min el 27-oct; `users/df/` sobrevivía el 26 y el 27 moría entre los 10 y los 18 min; `hi/` aguantó los 28 min y en el bucket hay objetos suyos de ~16 h. Todos los PUT devuelven 200 y la verificación inmediata pasa incluso en objetos que morirán en minutos: el transporte nunca fue el problema, es la key… y ningún namespace fijo es para siempre (el fix anterior, `users/df/`, duró 1 día).

**Ahora el bot (adaptativo, sin namespace fijo):**
1. Sube con un solo PUT en streaming (Content-Type real + Content-Disposition con el nombre original).
2. Sube a **2 namespaces candidatos** (`S3_KEY_PREFIXES`, por defecto `hi` → `users/df` → `dk/tg_{user_id}`) y espera **`S3_SETTLE_SECONDS` (150 s)** para re-verificar que siguen vivos *antes* de entregar nada.
3. Entrega **enlace principal + espejo** en otro namespace: si un barrido lento tumba uno horas después, el otro puede seguir vivo. La tarjeta de resultado muestra ambos.
4. Si un namespace muere, queda relegado al final y el superviviente pasa a ser el primero (memoria en `s3_namespace_state.json`): el bot se re-adapta solo cuando toDus cambia las reglas. Solo si *ningún* namespace deja el archivo vivo verás un error honesto en vez de enlaces muertos.

**¿Y los repos de referencia?** `miguelorlandos06/s3_bot` sube a la raíz del bucket (`<uuid>_<nombre>`) sin verificar: sobre el barrido actual es el patrón que muere en <2 min, así que no es un modelo a seguir para esto. `nyxthor-dev/toDus-SDK` implementa el protocolo oficial de toDus y revela el mecanismo "correcto": la app reserva la subida por XMPP (`todus:purl`, con flag `persistent`) y el servidor asigna la URL — esos objetos quedan registrados y no dependen de adivinar namespaces. Es la vía con garantías a futuro, pero requiere una **cuenta de toDus** (número + contraseña) dedicada al bot. (El SDK también documenta `login_with_phone_only`, un bypass de autenticación del servidor: no se usa aquí.)

**Si aun así un enlace cae con el tiempo:** el bucket de toDus es público y anónimo hasta para borrar — toDus o terceros pueden purgarlo sin aviso y eso no lo puede evitar ningún cliente anónimo. Descarga los archivos pronto o usa tu propio S3 (`S3_UNSIGNED=0` + tus claves) para enlaces permanentes.

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
