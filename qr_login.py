"""
QR Code Login for Telegram Userbot
Scan karo, session .env me save ho jayega, bas!
"""
import os, sys, asyncio

try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from telethon import TelegramClient
    from telethon.sessions import StringSession
except ImportError:
    print("❌ Telethon not installed! Run: pip install telethon")
    sys.exit(1)

try:
    import qrcode
except ImportError:
    print("❌ qrcode not installed! Run: pip install qrcode")
    sys.exit(1)

API_ID = int(os.environ.get("API_ID", 2040))
API_HASH = os.environ.get("API_HASH", "b18441a1ff607e10a989891a5462e627")


def display_qr(url):
    """Terminal me QR code dikhata hai"""
    qr = qrcode.QRCode(version=1, box_size=1, border=2)
    qr.add_data(url)
    qr.make(fit=True)
    # Windows terminal ke liye invert=True better dikhta hai
    qr.print_ascii(invert=True)


def save_session_to_env(session_str):
    """Session string ko .env file me save karta hai"""
    env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")

    lines = []
    found = False

    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        for i, line in enumerate(lines):
            if line.startswith("USERBOT_SESSION="):
                lines[i] = f"USERBOT_SESSION={session_str}\n"
                found = True
                break

    if not found:
        # Agar last line me newline nahi hai toh pehle newline daal do
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += "\n"
        lines.append(f"USERBOT_SESSION={session_str}\n")

    with open(env_path, "w", encoding="utf-8") as f:
        f.writelines(lines)

    print(f"\n{'═'*55}")
    print(f"  💾 Session saved to .env file!")
    print(f"  📋 USERBOT_SESSION={session_str[:30]}...")
    print(f"{'═'*55}\n")


async def qr_login():
    client = TelegramClient(
        StringSession(), API_ID, API_HASH,
        device_model="Desktop",
        system_version="Windows 11",
        app_version="4.14.9"
    )

    await client.connect()

    # Agar pehle se authorized hai (rare case)
    if await client.is_user_authorized():
        print("✅ Already authorized!")
        session_str = client.session.save()
        save_session_to_env(session_str)
        await client.disconnect()
        return

    print(f"\n{'═'*55}")
    print(f"  📱  QR CODE LOGIN")
    print(f"  ─────────────────────────────────────────────")
    print(f"  1. Open Telegram App on your phone")
    print(f"  2. Go to Settings > Devices > Link Desktop Device")
    print(f"  3. Scan the QR code below")
    print(f"{'═'*55}\n")

    qr = await client.qr_login()
    display_qr(qr.url)
    print("  ⏳ Waiting for you to scan...")

    try:
        logged_in = False
        while not logged_in:
            try:
                # 30 sec wait, agar expire ho gaya toh naya QR banayega
                result = await qr.wait(timeout=30)
                logged_in = True
            except asyncio.TimeoutError:
                # QR expired, naya generate karo
                print("\n  🔄 QR expired! Generating new one...\n")
                await qr.recreate()
                display_qr(qr.url)
                print("  ⏳ Waiting for you to scan...")
    except Exception as e:
        print(f"\n  ❌ Login failed: {e}")
        await client.disconnect()
        return

    # Login successful!
    me = await client.get_me()
    print(f"\n  ✅ Successfully logged in!")
    print(f"  📱 Account: {me.first_name} (@{me.username or 'N/A'})")
    print(f"  🆔 ID: {me.id}")

    # Session save karo .env me
    session_str = client.session.save()
    save_session_to_env(session_str)

    print("  🎉 Ab aap userbot start kar sakte hain!")
    print("     Command: .\\start_userbot.bat\n")

    await client.disconnect()


if __name__ == "__main__":
    try:
        asyncio.run(qr_login())
    except KeyboardInterrupt:
        print("\n👋 Cancelled.")
