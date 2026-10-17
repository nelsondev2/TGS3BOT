"""
Genera un TG_SESSION_STRING para un BOT sin escribirlo en los logs del servidor.

Uso:
    python scripts/generate_bot_session.py

Te pedirá:
1. API_ID (de https://my.telegram.org/apps)
2. API_HASH (de https://my.telegram.org/apps)
3. Token del bot (de @BotFather)

Al final imprime el string en tu terminal local. Guárdalo como
TG_SESSION_STRING y borra TG_BOT_TOKEN (o déjalo; la sesión tiene prioridad).
"""
import asyncio
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from telethon import TelegramClient
from telethon.sessions import StringSession


async def main():
    print("=" * 60)
    print("  Generador de TG_SESSION_STRING para un BOT")
    print("=" * 60)

    api_id_raw = input("👉 API_ID: ").strip()
    try:
        api_id = int(api_id_raw)
    except ValueError:
        print("❌ API_ID debe ser un número entero.")
        return
    api_hash = input("👉 API_HASH: ").strip()
    if not api_hash:
        print("❌ API_HASH no puede estar vacío.")
        return
    bot_token = input("👉 Token del bot (@BotFather): ").strip()
    if not bot_token:
        print("❌ El token del bot no puede estar vacío.")
        return

    client = TelegramClient(StringSession(), api_id, api_hash)
    try:
        await client.start(bot_token=bot_token)
    except Exception as e:
        print(f"❌ Error al iniciar sesión: {e}")
        return

    session_string = client.session.save()
    me = await client.get_me()
    print()
    print("=" * 60)
    print("✅ SESIÓN GENERADA — copia este string completo:")
    print("=" * 60)
    print(session_string)
    print("=" * 60)
    print(f"Bot: @{getattr(me, 'username', None)} (ID: {me.id})")
    print("Guárdalo como TG_SESSION_STRING.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
