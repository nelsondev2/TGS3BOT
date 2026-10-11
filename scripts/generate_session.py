"""
Script para generar TG_SESSION_STRING (modo userbot).

Uso:
    python scripts/generate_session.py

Te pedirá:
1. API_ID (de https://my.telegram.org/apps)
2. API_HASH (de https://my.telegram.org/apps)
3. Número de teléfono (formato internacional +53...)
4. Código de verificación (que Telegram te enviará)
5. Password 2FA (solo si tienes activada la verificación en dos pasos)

Al final imprimirá un string largo que debes copiar y poner como
valor de la variable de entorno TG_SESSION_STRING.
"""
import asyncio
import sys
import os

# Permite importar telethon aunque el script corra desde scripts/
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon.sessions import StringSession
from telethon import TelegramClient


async def main():
    print("=" * 60)
    print("  Generador de TG_SESSION_STRING para Telethon")
    print("=" * 60)
    print()

    api_id_input = input("👉 API_ID: ").strip()
    try:
        api_id = int(api_id_input)
    except ValueError:
        print("❌ API_ID debe ser un número entero.")
        return

    api_hash = input("👉 API_HASH: ").strip()
    if not api_hash:
        print("❌ API_HASH no puede estar vacío.")
        return

    print("\n📱 Para iniciar sesión en Telegram necesitas:")
    print("   - El número de teléfono (formato internacional, ej: +5350155246)")
    print("   - El código de verificación que Telegram te enviará")
    print("   - Tu contraseña 2FA (solo si tienes verificación en dos pasos)")
    print()

    client = TelegramClient(StringSession(), api_id, api_hash)

    async def _phone():
        return input("👉 Número de teléfono: ").strip()

    async def _code():
        return input("👉 Código de verificación: ").strip()

    async def _password():
        return input("👉 Password 2FA (deja vacío si no tienes): ").strip() or None

    try:
        await client.start(
            phone=_phone,
            code_callback=_code,
            password=_password,
        )
    except Exception as e:
        print(f"❌ Error al iniciar sesión: {e}")
        return

    session_string = client.session.save()

    print()
    print("=" * 60)
    print("✅ SESIÓN GENERADA — copia este string completo:")
    print("=" * 60)
    print()
    print(session_string)
    print()
    print("=" * 60)
    print("Guárdalo como variable de entorno TG_SESSION_STRING.")
    print("=" * 60)

    # Verificar identidad
    me = await client.get_me()
    print(f"\nConectado como: @{getattr(me, 'username', None)} (ID: {me.id})")

    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
