import os
import sys
import random
import asyncio
import logging

try:
    from telethon import TelegramClient, errors
    from telethon.sessions import StringSession
    from dotenv import load_dotenv
except ImportError:
    print("❌ Missing packages. Run: pip install telethon python-dotenv")
    sys.exit(1)

# Load environment variables
load_dotenv()
API_ID = int(os.environ.get("API_ID", 2040))
API_HASH = os.environ.get("API_HASH", "b18441a1ff607e10a989891a5462e627")
SESSION_STRING = os.environ.get("PROMO_SESSION", "")

if not SESSION_STRING:
    print("❌ PROMO_SESSION not found in .env!")
    print("   Bhai, Promo bot ke liye ek naya account banakar uska session PROMO_SESSION me daalo.")
    sys.exit(1)

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("AutoPromo")

# Aapki di hui Link/Promo groups ki list
TARGET_GROUPS = [
    -1003731701537, -1004294114933, -1003139036639, -1003953915147, 
    -1003085027822, -1003760514520, -1003245429631, -1003247032511
]

def generate_promo_message():
    """Har baar ek naya aur random message generate karega taaki Telegram ko bot na lage"""
    headers = [
        "🔥 𝐃𝐢𝐫𝐞𝐜𝐭 𝐃𝐨𝐰𝐧𝐥𝐨𝐚𝐝 𝐅𝐢𝐥𝐞 (𝐍𝐨 𝗕𝗮𝗸𝗰𝗵𝗼𝗱𝗶)",
        "🎬 𝐁𝐞𝐬𝐭 𝐌𝐨𝐯𝐢𝐞𝐬 & 𝐖𝐞𝐛 𝐒𝐞𝐫𝐢𝐞𝐬 (𝐍𝐨 𝐀𝐝𝐬)",
        "🚀 𝐅𝐚𝐬𝐭 & 𝐃𝐢𝐫𝐞𝐜𝐭 𝐃𝐨𝐰𝐧𝐥𝐨𝐚𝐝 𝐋𝐢𝐧𝐤𝐬",
        "⚡ 𝟏-𝐂𝐥𝐢𝐜𝐤 𝐃𝐨𝐰𝐧𝐥𝐨𝐚𝐝 𝐌𝐨𝐯𝐢𝐞𝐬 (𝐇𝐃)"
    ]
    
    lines = [
        "▪️ Hollywood (Hin Dubbed)",
        "▪️ South Indian Movies",
        "▪️ Netflix & Amazon Series",
        "▪️ Bollywood Blockbusters",
        "▪️ High-Quality 4K / 1080p / 720p",
        "▪️ Request Your Favorite Movies"
    ]
    
    footers = [
        "👉 𝐉𝐨𝐢𝐧 𝐍𝐨𝐰: https://t.me/FlimfyBoxx",
        "👇 𝐂𝐥𝐢𝐜𝐤 𝐇𝐞𝐫𝐞 𝐓𝐨 𝐉𝐨𝐢𝐧:\nhttps://t.me/FlimfyBoxx",
        "🔗 𝐉𝐨𝐢𝐧 𝐎𝐮𝐫 𝐂𝐡𝐚𝐧𝐧𝐞𝐥:\nhttps://t.me/FlimfyBoxx",
        "✅ 𝟏𝟎𝟎% 𝐀𝐝-𝐅𝐫𝐞𝐞 𝐂𝐡𝐚𝐧𝐧𝐞𝐥: https://t.me/FlimfyBoxx"
    ]
    
    # 3 se 5 random lines uthayega
    random.shuffle(lines)
    selected_lines = lines[:random.randint(3, 5)]
    
    msg = f"{random.choice(headers)}\n\n"
    msg += "\n".join(selected_lines)
    msg += f"\n\n{random.choice(footers)}"
    
    return msg

async def auto_promo():
    client = TelegramClient(StringSession(SESSION_STRING), API_ID, API_HASH, 
                            device_model="PromoBot", system_version="Windows 11")
    await client.connect()
    
    if not await client.is_user_authorized():
        log.error("❌ Session expired! Run qr_login.py again.")
        return

    log.info("✅ Auto Promo Bot Started! Mode: ULTRA SAFE 🛡️")
    log.info("   -> Ek message bhejega, phir 1 se 1.5 ghante aaram karega.")
    
    while True:
        # Har cycle me groups ka order aage-piche kar do (randomize)
        random.shuffle(TARGET_GROUPS)
        
        for group_id in TARGET_GROUPS:
            msg = generate_promo_message()
            
            try:
                log.info(f"📤 Sending promo to group {group_id}...")
                await client.send_message(group_id, msg, link_preview=False)
                log.info("✅ Message sent successfully!")
            except errors.FloodWaitError as e:
                log.warning(f"⚠️ Flood wait for {e.seconds} seconds. Waiting...")
                await asyncio.sleep(e.seconds)
                continue
            except Exception as e:
                log.error(f"❌ Failed to send to {group_id}: {e}")
                
            # 🛡️ ULTRA SAFE DELAY 🛡️
            # Random wait between 60 minutes (3600 sec) to 90 minutes (5400 sec)
            # Har message ke baad delay alag hoga, insaano ki tarah!
            wait_time = random.randint(3600, 5400)
            log.info(f"😴 Sleeping for {wait_time // 60} minutes to stay undetected...\n")
            await asyncio.sleep(wait_time)

if __name__ == '__main__':
    try:
        asyncio.run(auto_promo())
    except KeyboardInterrupt:
        print("\n👋 Auto Promo stopped.")
