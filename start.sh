#!/bin/bash
# Script de arranque para desarrollo local
set -e

# Cargar .env si existe
if [ -f .env ]; then
    set -a
    # shellcheck disable=SC1091
    source .env
    set +a
    echo "✅ Variables cargadas desde .env"
fi

# Verificar Python
if ! python -c "import sys; sys.exit(0 if sys.version_info >= (3,11) else 1)" 2>/dev/null; then
    echo "❌ Necesitas Python 3.11+"
    python --version 2>&1 || true
    exit 1
fi

# Verificar variables obligatorias
if [ -z "${TG_API_ID:-}" ] || [ -z "${TG_API_HASH:-}" ]; then
    echo "❌ Faltan variables obligatorias:"
    echo "   TG_API_ID y TG_API_HASH — obténlas en https://my.telegram.org/apps"
    exit 1
fi

if [ -z "${TG_BOT_TOKEN:-}" ] && [ -z "${TG_SESSION_STRING:-}" ]; then
    echo "❌ Necesitas TG_BOT_TOKEN (modo bot) o TG_SESSION_STRING (modo userbot)."
    echo "   Para generar SESSION_STRING: python scripts/generate_session.py"
    echo "   Para un bot: python scripts/generate_bot_session.py"
    exit 1
fi

# Instalar dependencias si hace falta
if ! python -c "import aiohttp, aiofiles, boto3, telethon" 2>/dev/null; then
    echo "📦 Instalando dependencias..."
    pip install -r requirements.txt
fi

# Arrancar
echo "🚀 Arrancando bot en puerto ${PORT:-10000}..."
exec python bot.py
