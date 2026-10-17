# Bot de Telegram (Telethon) - Subida a S3

Bot de Telegram que recibe enlaces o archivos reenviados, los sube a S3 y **siempre devuelve la URL pública**. Por defecto solo envía la URL como texto: no transfiere el archivo por Telegram. Descargarlo desde S3 consume datos del plan de toDus (desde octubre de 2021 no es gratis), pero normalmente sale más barato que recibirlo por Telegram, que usa datos internacionales. Con `/sendfile` el bot también reenvía el archivo por Telegram.

## ✨ Características

- ✅ **Cliente Telethon en modo bot oficial** (`TG_BOT_TOKEN`, o `TG_SESSION_STRING` de un bot)
- ✅ **La URL S3 siempre se muestra al usuario**
- ✅ **Modo solo URL por defecto**: no transfiere el archivo por Telegram; la descarga desde S3 se hace cuando el usuario quiera
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
   - `TG_BOT_TOKEN` (de @BotFather) **o** `TG_SESSION_STRING` de un bot (opcional, evita re-logins)
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

### Paso 2: Crear el bot en @BotFather

El bot funciona **únicamente en modo bot** (cuentas personales no están soportadas).

1. En Telegram, habla con **@BotFather**.
2. Envía `/newbot` y sigue las instrucciones.
3. Copia el token que te da (formato `123456789:ABCDefgh...`).
4. Configúralo como `TG_BOT_TOKEN`.

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

**Importante:** esto evita el `FloodWaitError` que ocurre cuando Telegram rate-limita múltiples logins seguidos. El bot **rechaza** las sesiones de cuentas personales al arrancar: solo acepta sesiones generadas con `scripts/generate_bot_session.py` (o el login directo con `TG_BOT_TOKEN`).

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
| `TG_SESSION_STRING` | (vacío) | Sesión pre-generada **de un bot** (opcional; evita re-logins) |
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
| `/sendfile` | Toggle: recibir también el archivo (consume datos de Telegram) — por defecto OFF; requiere `SEND_FILE_VIA_TG=1` |
| `/textonly` | Desactiva el envío del archivo (solo URL); idempotente |
| `<URL>` | Procesa cualquier URL http/https |
| `<archivo reenviado>` | Sube cualquier foto/video/audio/documento reenviado a S3 |

### Comportamiento por defecto (solo URL)

Por defecto el bot **solo envía la URL S3 como texto** y no reenvía el archivo. Conviene distinguir qué tráfico consume cada cosa:

| Qué ocurre | ¿Consume datos? |
|---|---|
| El bot te envía la URL por Telegram (texto) | **Muy poco**: unos KB, no el tamaño del archivo |
| Descargas el archivo desde `s3.todus.cu` | **Sí**, del plan de toDus: primero datos nacionales y luego internacionales. No es gratis desde octubre de 2021 |
| Recibes el archivo por Telegram (`/sendfile`) | **Sí**, el tamaño completo, y por la red de Telegram (normalmente datos internacionales o WiFi) |
| Compartes la URL como texto dentro de toDus | **No**: la mensajería de texto de toDus es ilimitada |

En resumen: **la URL como texto es barata; lo que cuesta es mover el archivo**, ya sea por Telegram o descargándolo de S3. La ventaja de S3 es que la descarga usa datos nacionales de toDus en vez de datos internacionales, pero sigue consumiendo plan. El bot no evade la facturación de toDus.

Si quieres recibir el archivo además de la URL, escribe `/sendfile` una vez. Para volver al modo solo URL, escribe `/textonly` (o `/sendfile` otra vez).

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

Por defecto solo te envío la URL como texto (no se transfiere el archivo).
Descargarlo desde S3 consume datos de toDus; recibirlo por Telegram, datos
de tu plan de internet.
Si quieres recibir el archivo además de la URL, usa /sendfile.

Comandos
  /cancel — cancela tu trabajo actual
  /status — muestra si tienes trabajo en curso
  /sendfile — toggle: recibir archivo + URL (gasta datos)
  /textonly — desactivar el envío del archivo (solo URL)

El bot no impone límite de tamaño; Telegram sí limita cada archivo
(2 GB por archivo en modo bot).
```

## 📊 toDus y el consumo de datos

### Plan toDus (25 CUP, 35 días)

- ✅ **Mensajería por texto ilimitada**: no descuenta del plan, ni siquiera cuando se agotan los datos.
- 📦 **1 GB de datos** (desde abril de 2026; antes 600 MB) para el resto de servicios de la app y el correo Nauta.

### Qué consume el plan

- ❌ Enviar/recibir imágenes, videos, archivos y notas de voz.
- ❌ El correo Nauta y otros servicios dentro de toDus.
- ✅ **No consume**: los mensajes de texto puros.

ETECSA descuenta primero los datos nacionales y, si se agotan, los internacionales.

### ¿Las descargas de `s3.todus.cu` son gratis?

**No, desde el 21 de octubre de 2021.** Hasta esa fecha el tráfico de archivos de toDus era gratuito y la llamada «Comunidad S3» compartía películas, series y juegos de varios GB mediante enlaces y archivos `txt`. Con la versión 1.0 de toDus, ETECSA empezó a tasar las transferencias de archivos y creó la bolsa de mensajería de 600 MB. Los «trucos» que siguen circulando son de esa época y ya no funcionan:

- **Descargas S3**: el tráfico se descuenta del plan, igual que cualquier archivo.
- **«Piscina de fichas»** (repartir cuentas entre usuarios): no elimina el consumo, solo reparte el gasto entre varias fichas. toDus limita los MB multimedia por ventana de tiempo y deniega descargas al superarlo. Además, usar cuentas de terceros va contra los términos del servicio.
- **Descargas directas** (compartir un archivo desde el propio móvil por HTTP): fue el reemplazo que intentó la comunidad en 2021, pero exige que el emisor tenga el archivo y esté conectado; no sirve para un bot alojado en la nube.
- **APKs modificadas o clientes no oficiales**: toDus los bloquea y puede suspender la cuenta.

### ¿Se puede mandar un archivo como si fuera texto?

En teoría sí: se codifica en base64 y se trocea en mensajes. En la práctica **no es viable**:

1. Base64 añade un 33 %: 10 MB se convierten en ~13,3 MB.
2. toDus 2.1.0 limita cada mensaje a **1.024 caracteres** (abril de 2026). Un archivo de 10 MB necesitaría **unos 13.650 mensajes**.
3. Hay que numerar, verificar y reenviar cada trozo: cualquier pérdida corrompe el archivo.
4. El receptor necesitaría una app que reconstruya el archivo; la app oficial solo mostraría miles de burbujas de texto.
5. Un bot que envía miles de mensajes seguidos entra en FloodWait y pone en riesgo la cuenta. Disfrazar multimedia como texto también incumple los términos de toDus.

### Qué hace este bot

- Envía la **URL por Telegram como texto**: unos KB en lugar del archivo completo.
- **No promete ni intenta** que la descarga sea gratuita: no evade la facturación de toDus.
- Deja la descarga a decisión del usuario. Para ahorrar de verdad, descarga por WiFi o con datos nacionales, no con el bono internacional.
- Si además se reenvía el archivo por Telegram (`/sendfile`), se transfiere el archivo completo y consume datos de Telegram.

**Para activar el modo solo URL:**
```
/textonly
```
A partir de ese momento, el bot solo enviará URLs S3 (texto), no reenviará el archivo como media. Para volver a recibir el archivo, escribe `/sendfile`.

### Fuentes

- ETECSA actualiza el Plan toDus (Cubadebate, 1 de abril de 2026): <http://www.cubadebate.cu/noticias/2026/04/01/etecsa-actualiza-el-plan-todus/>
- Lanzan nuevo plan de datos para toDus, 1 GB y límite de 1.024 caracteres (CubitaNOW, 2 de abril de 2026): <https://noticias.cubitanow.com/lanzan-nuevo-plan-de-datos-para-la-app-nacional-todus-en-medio-de-control-digital-en-cuba>
- toDus lanza su versión 1.0 y empieza a tasar los archivos (Cubadebate, 21 de octubre de 2021): <http://www.cubadebate.cu/noticias/2021/10/21/la-aplicacion-cubana-para-mensajeria-instantanea-todus-lanza-su-version-1-0/>
- ETECSA limita las descargas S3 (Cubanet, 21 de octubre de 2021): <https://www.cubanet.org/etecsa-limita-descargas-s3-con-nueva-promocion-de-intranet/>
- Series y películas por toDus: del S3 gratuito a la limitación por ventanas de tiempo (elTOQUE, agosto de 2021): <https://eltoque.com/series-y-peliculas-por-todus-adios-paquete-semanal>

Las tarifas y límites de toDus cambian con frecuencia: si ETECSA los modifica, actualiza esta sección.

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
- **Allowlist de IDs**: si defines `ALLOWED_TG_IDS`, solo esos IDs pueden usar el bot. Sin la variable, cualquiera que hable con el bot puede usarlo.
- **Sin límite de tamaño por defecto**: `MAX_FILE_SIZE_MB=0`. Se comprueba el espacio real en disco antes de descargar; Telegram impone su propio tope por archivo.
- **Sanitización de nombres**: previene path traversal y limita el nombre a 180 bytes.
- **Sesión fuera de logs**: el `SESSION_STRING` nunca se imprime ni se escribe en disco por el bot.

## 📝 Notas importantes

### Sobre el watchdog
El watchdog reinicia el proceso cada `WATCHDOG_LIFETIME` segundos (default: 3h 55m). Si hay trabajos activos, espera hasta 1 hora a que terminen antes de reiniciar. Desactívalo con `WATCHDOG_ENABLED=0`.

### Sobre el modo bot
- El bot funciona **solo como bot** creado con @BotFather (o una sesión de bot pre-generada).
- Tiene los límites estándar de Telegram (2 GB por archivo) y los usuarios deben iniciar el bot con `/start`.
- **No intercepta conversaciones** con otros bots ni con otras personas.
- Si configuras por error `TG_SESSION_STRING` de una cuenta personal, el bot lo detecta al conectar y **se niega a arrancar** en lugar de exponer tu cuenta.

### Sobre el S3
El S3 por defecto es el público de toDus (`s3.todus.cu`, sin firma). Puedes cambiarlo a cualquier S3 compatible (AWS, MinIO, Cloudflare R2, etc.) seteando `S3_ENDPOINT`, `S3_BUCKET`, `S3_UNSIGNED=0`, `S3_ACCESS_KEY`, `S3_SECRET_KEY`.

Ten en cuenta que:

- El bucket es **público y sin firma**: cualquiera con la URL puede abrir el archivo. No subas contenido sensible.
- **La descarga no es tráfico gratis**: se descuenta del plan de toDus del usuario (nacional o internacional), como cualquier archivo.
- Los objetos no caducan solos; si te importa el almacenamiento, define una política de retención en el bucket.

### Sobre el disco
- `/tmp/tg_uploads` es efímero en Render free (se pierde en cada deploy).
- Los archivos temporales se eliminan después de subirlos a S3 y enviarlos por Telegram.
- El plan free de Render **no permite discos persistentes**. Para archivos grandes o sesión persistente necesitas un plan de pago con disco (ver el bloque comentado en `render.yaml`).
- `check_disk_space` usa el tamaño real esperado, no `2 × MAX_FILE_SIZE`.

## 🐛 Bugs corregidos respecto a la versión original

1. **Un enlace con vista previa se trataba como media** y podía subir la miniatura en vez del archivo
2. **SSRF evadible por redirecciones**; ahora se valida cada salto
3. **`check_disk_space` exigía 2 × MAX_FILE_SIZE** para cualquier archivo
4. **Tareas huérfanas en descarga paralela** que seguían escribiendo `.part`
5. **`Accept-Encoding: br` sin Brotli** provocaba errores de decodificación
6. **El `SESSION_STRING` se escribía en logs y disco**
7. **Uploads S3 seriales y con `Content-Type: application/octet-stream`**
8. **El watchdog mataba trabajos activos**
9. **FloodWait bloqueaba el event loop** y tumbaba el healthcheck
10. **Álbumes**: solo se procesaba la primera foto
11. **Markdown roto** en `/help` y captions con nombres especiales
12. **Nombres de archivo sin truncar** → `ENAMETOOLONG`
13. **`/sendfile` decía “activado” aunque el administrador lo tuviera desactivado**
14. **`/textonly` activaba el modo archivo** en vez de desactivarlo (era un alias del toggle)

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

### El bot no responde

**Comprobaciones:**
1. Abre el chat del bot e inícialo con `/start`; los bots no pueden escribir primero.
2. El mensaje debe contener una URL, ser un archivo/foto/video o un comando (`/start`, `/status`, `/cancel`, `/sendfile`).
3. Si defines `ALLOWED_TG_IDS`, tu ID tiene que estar en la lista o el bot responderá “No estás autorizado”.
4. Revisa los logs: si aparece “La sesión configurada es de una cuenta personal”, sustituye `TG_SESSION_STRING` por uno generado con `scripts/generate_bot_session.py` o borra esa variable y usa `TG_BOT_TOKEN`.
5. Si usas Render free, el servicio se suspende sin tráfico; el primer mensaje puede tardar ~1 minuto en arrancar.

## 📄 Licencia

Apache License 2.0 — ver el archivo `LICENSE`.
