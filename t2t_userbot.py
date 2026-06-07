"""
T2T Userbot — Telegram-to-Telegram Channel Forwarder
Forwards MKV/MP4 files from target channels to @FlimfyBoxBot PM.
"""
import os, re, sys, random, asyncio, logging
from datetime import datetime, timedelta
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    pass

try:
    from telethon import TelegramClient, events, errors
    from telethon.tl.types import DocumentAttributeFilename, DocumentAttributeVideo, MessageMediaDocument
    from telethon.sessions import StringSession
except ImportError:
    print("pip install telethon")
    sys.exit(1)

import db_utils

logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s", datefmt="%H:%M:%S")
log = logging.getLogger("T2T")

# Config
API_ID = int(os.environ.get("API_ID", 2040))
API_HASH = os.environ.get("API_HASH", "b18441a1ff607e10a989891a5462e627")
OWNER_ID = int(os.environ.get("OWNER_ID", 0))
SESSION_STRING = os.environ.get("USERBOT_SESSION", "")
FLIMFYBOX_BOT = "FlimfyBoxBot"
CHANNEL_COOLDOWN = 3600
FILE_DELAY_MIN, FILE_DELAY_MAX = 1, 2       # Fast burst: tiny gap between files in a batch
BATCH_MIN, BATCH_MAX = 10, 20               # Files per burst batch
BATCH_PAUSE_MIN, BATCH_PAUSE_MAX = 15, 30   # Cooldown between burst batches
MAX_FILES_PER_CHANNEL = 100                  # Hard cap per channel per run
MIN_FILE_SIZE = 10 * 1024 * 1024             # 10 MB — accept almost any video file
EXCLUDED_KEYWORDS = ["promo", "trailer", "sample", "1xbet", "sponsor"]
ALLOWED_MIME_TYPES = {"video/mp4", "video/x-matroska", "video/webm", "video/avi",
                     "video/quicktime", "application/octet-stream",
                     "application/x-matroska", "video/x-msvideo"}
ALLOWED_EXTENSIONS = {".mkv", ".mp4", ".avi", ".webm", ".mov", ".wmv", ".flv"}
MIN_MSG_GAP = 5                              # Min gap for text commands only
_last_msg_time = 0.0
is_paused = False
_resume_event = None

if not SESSION_STRING:
    print("\n❌ USERBOT_SESSION not found in .env!")
    print("   Pehle QR login karo: python qr_login.py\n")
    sys.exit(1)

session_storage = StringSession(SESSION_STRING)

client = TelegramClient(session_storage, API_ID, API_HASH,
    device_model="Desktop", system_version="Windows 11", app_version="4.14.9")

# ── DB Setup ──
def t2t_ensure_tables(conn):
    cur = conn.cursor()
    cur.execute("""
        CREATE TABLE IF NOT EXISTS t2t_channels (
            id SERIAL PRIMARY KEY,
            channel_link TEXT NOT NULL UNIQUE,
            channel_id BIGINT,
            channel_title TEXT,
            status TEXT DEFAULT 'pending',
            priority INTEGER DEFAULT 0,
            last_forwarded_msg_id INTEGER DEFAULT 0,
            total_files_found INTEGER DEFAULT 0,
            total_files_forwarded INTEGER DEFAULT 0,
            added_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            started_at TIMESTAMP,
            completed_at TIMESTAMP,
            notes TEXT
        );
    """)
    cur.execute("""
        CREATE TABLE IF NOT EXISTS t2t_forward_log (
            id SERIAL PRIMARY KEY,
            channel_id INTEGER REFERENCES t2t_channels(id) ON DELETE CASCADE,
            original_msg_id INTEGER NOT NULL,
            filename TEXT,
            file_size BIGINT,
            forwarded_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            status TEXT DEFAULT 'forwarded',
            UNIQUE(channel_id, original_msg_id)
        );
    """)
    conn.commit()
    cur.close()

def t2t_add_channel(conn, link):
    cur = conn.cursor()
    try:
        cur.execute("INSERT INTO t2t_channels (channel_link) VALUES (%s) ON CONFLICT (channel_link) DO NOTHING RETURNING id", (link.strip(),))
        conn.commit()
        r = cur.fetchone()
        cur.close()
        return r[0] if r else None
    except Exception as e:
        conn.rollback()
        cur.close()
        log.error(f"Add channel error: {e}")
        return None

def t2t_fetch_next_channel(conn):
    cur = conn.cursor()
    # 1. First try to find a channel with status='pending'
    cur.execute("""
        SELECT id, channel_link, channel_id, channel_title, last_forwarded_msg_id,
               total_files_found, total_files_forwarded
        FROM t2t_channels WHERE status = 'pending'
        ORDER BY priority DESC, id ASC LIMIT 1
    """)
    row = cur.fetchone()
    if row:
        cur.close()
        return {"id": row[0], "link": row[1], "channel_id": row[2], "title": row[3],
                "last_msg_id": row[4], "found": row[5], "forwarded": row[6]}

    # 2. No pending channels — look for 'done' channels completed > 50 mins ago
    fifty_mins_ago = datetime.utcnow() - timedelta(minutes=50)
    cur.execute("""
        SELECT id, channel_link, channel_id, channel_title, last_forwarded_msg_id,
               total_files_found, total_files_forwarded
        FROM t2t_channels
        WHERE status = 'done'
          AND completed_at IS NOT NULL
          AND completed_at < %s
        ORDER BY completed_at ASC LIMIT 1
    """, (fifty_mins_ago,))
    row = cur.fetchone()
    cur.close()
    if not row:
        return None

    # 3. Reset this stale 'done' channel back to 'pending' for reprocessing.
    #    Preserve last_forwarded_msg_id so it resumes from where it left off.
    ch_id = row[0]
    log.info(f"  🔄 Auto-Recheck: Channel '{row[3] or row[1]}' (ID: {ch_id}) was done >50min ago. Resetting to pending.")
    t2t_update_channel(conn, ch_id, status="pending", completed_at=None)

    return {"id": row[0], "link": row[1], "channel_id": row[2], "title": row[3],
            "last_msg_id": row[4], "found": row[5], "forwarded": row[6]}

def t2t_update_channel(conn, ch_id, **kwargs):
    cur = conn.cursor()
    sets = []
    vals = []
    for k, v in kwargs.items():
        sets.append(f"{k} = %s")
        vals.append(v)
    vals.append(ch_id)
    cur.execute(f"UPDATE t2t_channels SET {', '.join(sets)} WHERE id = %s", vals)
    conn.commit()
    cur.close()

def t2t_log_forward(conn, ch_id, msg_id, filename, file_size, status="forwarded"):
    cur = conn.cursor()
    try:
        cur.execute("""INSERT INTO t2t_forward_log (channel_id, original_msg_id, filename, file_size, status)
            VALUES (%s,%s,%s,%s,%s) ON CONFLICT (channel_id, original_msg_id) DO NOTHING""",
            (ch_id, msg_id, filename, file_size, status))
        conn.commit()
    except Exception as e:
        conn.rollback()
        log.error(f"Log forward error: {e}")
    cur.close()

def t2t_is_already_forwarded(conn, ch_id, msg_id):
    cur = conn.cursor()
    cur.execute("SELECT 1 FROM t2t_forward_log WHERE channel_id=%s AND original_msg_id=%s", (ch_id, msg_id))
    r = cur.fetchone()
    cur.close()
    return r is not None

def t2t_get_all_channels(conn):
    cur = conn.cursor()
    cur.execute("SELECT id, channel_link, channel_title, status, total_files_forwarded, last_forwarded_msg_id FROM t2t_channels ORDER BY id")
    rows = cur.fetchall()
    cur.close()
    return rows

# ── Helpers ──
def is_video_file(message):
    """Check: is this a video document with an allowed mime type or extension?"""
    if not message.media or not isinstance(message.media, MessageMediaDocument):
        return False
    doc = message.media.document
    if not doc:
        return False
    mime = (doc.mime_type or "").lower()
    
    # Check mime type first
    if mime in ALLOWED_MIME_TYPES:
        return True
    
    # Check if mime starts with 'video/'
    if mime.startswith("video/"):
        return True
        
    # If mime type is weird (e.g. application/octet-stream), check filename extension
    for attr in doc.attributes:
        if isinstance(attr, DocumentAttributeFilename):
            name = attr.file_name.lower()
            if any(name.endswith(ext) for ext in ALLOWED_EXTENSIONS):
                return True
    
    # Also check if it has VideoAttribute (Telegram marks some videos this way)
    for attr in doc.attributes:
        if isinstance(attr, DocumentAttributeVideo):
            return True
                
    return False

def _contains_excluded_keyword(text):
    """Check if any excluded keyword appears in the text (case-insensitive)."""
    if not text:
        return False
    text_lower = text.lower()
    return any(kw in text_lower for kw in EXCLUDED_KEYWORDS)

def is_full_movie(message):
    """
    STRICT full-movie filter. Returns True ONLY if ALL conditions pass:
      1. Valid video mime type or extension
      2. File size >= MIN_FILE_SIZE
      3. Filename and caption do NOT contain excluded keywords
    """
    # Rule 1: MIME type / extension check
    if not is_video_file(message):
        # Only log if it's actually a document (ignore plain text messages to avoid spam)
        if message.media and isinstance(message.media, MessageMediaDocument):
            fname = get_filename(message)
            fsize = get_file_size(message)
            mime = ""
            if message.media.document:
                mime = message.media.document.mime_type or ""
            log.info(f"  ⏭️ SKIP (not video format): {fname} | mime: {mime} | size: {round(fsize/(1024*1024), 1)}MB")
        return False

    # Rule 2: Minimum file size
    fsize = get_file_size(message)
    if fsize < MIN_FILE_SIZE:
        fname = get_filename(message)
        size_mb = round(fsize / (1024*1024), 2) if fsize else 0
        log.info(f"  ⏭️ SKIP (too small): {fname} | {size_mb}MB < {MIN_FILE_SIZE//(1024*1024)}MB")
        return False

    # Rule 2.5: Duration check for files under 100MB
    if fsize < 100 * 1024 * 1024:
        duration = None
        if message.media and isinstance(message.media, MessageMediaDocument) and message.media.document:
            for attr in message.media.document.attributes:
                if isinstance(attr, DocumentAttributeVideo):
                    duration = attr.duration
                    break
        
        if duration is None or duration < 300:
            fname = get_filename(message)
            log.info(f"  ⏭️ Skipped: File under 100MB and duration < 5m (or unknown): {fname}")
            return False

    # Rule 3: Exclude junk keywords from filename and caption
    fname = get_filename(message)
    caption = message.text or message.message or ""
    if _contains_excluded_keyword(fname) or _contains_excluded_keyword(caption):
        log.info(f"  ⏭️ SKIP (excluded keyword in name/caption): {fname}")
        return False

    return True

def get_filename(message):
    if message.media and isinstance(message.media, MessageMediaDocument):
        doc = message.media.document
        if doc:
            for attr in doc.attributes:
                if isinstance(attr, DocumentAttributeFilename):
                    return attr.file_name
    return "unknown"

def get_file_size(message):
    if message.media and isinstance(message.media, MessageMediaDocument):
        doc = message.media.document
        if doc and doc.size:
            return doc.size
    return 0

async def human_delay(label=""):
    d = random.uniform(FILE_DELAY_MIN, FILE_DELAY_MAX)
    if label:
        log.info(f"  🕐 {label} — {d:.1f}s wait...")
    await asyncio.sleep(d)

async def safe_send_message(entity, text, **kwargs):
    global _last_msg_time
    now = asyncio.get_event_loop().time()
    elapsed = now - _last_msg_time
    if elapsed < MIN_MSG_GAP:
        w = MIN_MSG_GAP - elapsed + random.uniform(1, 4)
        await asyncio.sleep(w)
    for attempt in range(3):
        try:
            result = await client.send_message(entity, text, **kwargs)
            _last_msg_time = asyncio.get_event_loop().time()
            return result
        except errors.FloodWaitError as e:
            w = e.seconds + random.randint(5, 15)
            log.warning(f"  ⚠️ FLOOD WAIT: {w}s")
            await asyncio.sleep(w)
        except Exception as e:
            log.error(f"  ❌ Send error ({attempt+1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(random.uniform(10, 20))
            else:
                raise
    return None

async def safe_send_file(entity, file, **kwargs):
    """Send file WITHOUT the heavy MIN_MSG_GAP — burst-friendly."""
    for attempt in range(3):
        try:
            result = await client.send_file(entity, file, **kwargs)
            return result
        except errors.FloodWaitError as e:
            w = e.seconds + random.randint(5, 15)
            log.warning(f"  ⚠️ FLOOD WAIT on send_file: {w}s")
            await asyncio.sleep(w)
        except Exception as e:
            log.error(f"  ❌ send_file error ({attempt+1}/3): {e}")
            if attempt < 2:
                await asyncio.sleep(random.uniform(5, 10))
            else:
                return None
    return None

# ── Pause/Resume ──
async def trigger_pause(reason):
    global is_paused
    log.error(f"🛑 PAUSE: {reason}")
    is_paused = True
    if OWNER_ID:
        try:
            await client.send_message(OWNER_ID,
                f"🛑 **T2T Bot Paused!**\n**Reason:** `{reason}`\nSend `/resume` to continue.")
        except: pass

async def wait_for_resume():
    global _resume_event, is_paused
    if not is_paused:
        return
    if _resume_event is None:
        _resume_event = asyncio.Event()
    _resume_event.clear()
    log.info("  ⏸️ Waiting for /resume...")
    await _resume_event.wait()
    log.info("  ▶️ Resumed!")

def register_commands():
    @client.on(events.NewMessage(pattern=r'^/resume$', incoming=True))
    async def handle_resume(event):
        global is_paused, _resume_event
        if OWNER_ID and event.sender_id != OWNER_ID:
            return
        if not is_paused:
            await event.reply("✅ Bot is not paused.")
            return
        is_paused = False
        await event.reply("▶️ **Resumed!**")
        if _resume_event:
            _resume_event.set()

    @client.on(events.NewMessage(pattern=r'^/pause$', incoming=True))
    async def handle_pause(event):
        if OWNER_ID and event.sender_id != OWNER_ID:
            return
        await trigger_pause("Manual pause by owner")
        await event.reply("⏸️ Bot paused.")

    @client.on(events.NewMessage(pattern=r'^/addchannel\s+(.+)', incoming=True))
    async def handle_addchannel(event):
        if OWNER_ID and event.sender_id != OWNER_ID:
            return
        link = event.pattern_match.group(1).strip()
        conn = db_utils.get_db_connection()
        if not conn:
            await event.reply("❌ DB connection failed")
            return
        t2t_ensure_tables(conn)
        cid = t2t_add_channel(conn, link)
        db_utils.close_db_connection(conn)
        if cid:
            await event.reply(f"✅ Channel added (ID: {cid})\n`{link}`")
        else:
            await event.reply(f"⚠️ Channel already exists or error.\n`{link}`")

    @client.on(events.NewMessage(pattern=r'^/channels$', incoming=True))
    async def handle_channels(event):
        if OWNER_ID and event.sender_id != OWNER_ID:
            return
        conn = db_utils.get_db_connection()
        if not conn:
            await event.reply("❌ DB error")
            return
        t2t_ensure_tables(conn)
        rows = t2t_get_all_channels(conn)
        db_utils.close_db_connection(conn)
        if not rows:
            await event.reply("📭 No channels added yet.\nUse `/addchannel <link>`")
            return
        icons = {"pending": "⏳", "processing": "🔄", "done": "✅", "failed": "❌", "paused": "⏸️"}
        lines = ["📋 **T2T Channels:**\n"]
        for r in rows:
            cid, link, title, status, fwd, last_id = r
            icon = icons.get(status, "❓")
            name = title or link[:30]
            lines.append(f"{icon} `{cid}` | **{name}** | {status} | {fwd} files | last_msg: {last_id}")
        await event.reply("\n".join(lines))

    @client.on(events.NewMessage(pattern=r'^/skipchannel$', incoming=True))
    async def handle_skip(event):
        if OWNER_ID and event.sender_id != OWNER_ID:
            return
        conn = db_utils.get_db_connection()
        if not conn:
            await event.reply("❌ DB error")
            return
        ch = t2t_fetch_next_channel(conn)
        if not ch:
            await event.reply("No pending channel to skip.")
            db_utils.close_db_connection(conn)
            return
        t2t_update_channel(conn, ch["id"], status="paused", notes="Skipped by owner")
        db_utils.close_db_connection(conn)
        await event.reply(f"⏭️ Skipped channel: {ch['title'] or ch['link']}")

    @client.on(events.NewMessage(pattern=r'^/status$', incoming=True))
    async def handle_status(event):
        if OWNER_ID and event.sender_id != OWNER_ID:
            return
        status = "⏸️ PAUSED" if is_paused else "▶️ RUNNING"
        conn = db_utils.get_db_connection()
        if conn:
            t2t_ensure_tables(conn)
            cur = conn.cursor()
            cur.execute("SELECT COUNT(*) FROM t2t_channels WHERE status='pending'")
            pending = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM t2t_channels WHERE status='processing'")
            processing = cur.fetchone()[0]
            cur.execute("SELECT COUNT(*) FROM t2t_channels WHERE status='done'")
            done = cur.fetchone()[0]
            cur.close()
            db_utils.close_db_connection(conn)
            await event.reply(
                f"📊 **T2T Status**\n\n"
                f"State: {status}\n"
                f"Pending: {pending} | Processing: {processing} | Done: {done}")
        else:
            await event.reply(f"State: {status}\n❌ DB unavailable")

    log.info("  ✅ Owner commands registered")

# ── Core T2T Logic ──
async def t2t_forward_channel_files(conn, channel_data):
    ch_id = channel_data["id"]
    link = channel_data["link"]
    last_msg_id = channel_data["last_msg_id"] or 0

    # Resolve channel
    try:
        entity = await client.get_entity(link)
        title = getattr(entity, 'title', link)
        resolved_id = entity.id
        t2t_update_channel(conn, ch_id, channel_id=resolved_id, channel_title=title,
                           status="processing", started_at=datetime.utcnow())
        log.info(f"  ✅ Channel resolved: {title} (ID: {resolved_id})")
    except Exception as e:
        log.error(f"  ❌ Cannot access channel '{link}': {e}")
        t2t_update_channel(conn, ch_id, status="failed", notes=f"Access error: {str(e)[:200]}")
        return False, 0

    # Get FlimfyBoxBot entity
    try:
        flimfy_bot = await client.get_entity(FLIMFYBOX_BOT)
    except Exception as e:
        log.error(f"  ❌ Cannot resolve @{FLIMFYBOX_BOT}: {e}")
        t2t_update_channel(conn, ch_id, status="failed", notes=f"Bot resolve error: {e}")
        return False, 0

    # ── STEP 1: PRE-SCAN — check if channel has any valid files ──
    log.info(f"  📡 Pre-scanning channel for valid files (from msg_id > {last_msg_id})...")
    
    scan_count = 0
    msg_scanned = 0
    already_forwarded_count = 0
    skip_not_video = 0
    skip_too_small = 0
    skip_excluded = 0
    
    # First: RAW diagnostic — just count how many messages exist after min_id
    raw_msg_count = 0
    raw_doc_count = 0
    sample_files = []
    try:
        async for message in client.iter_messages(entity, reverse=True, min_id=last_msg_id, limit=50):
            raw_msg_count += 1
            if message.media and isinstance(message.media, MessageMediaDocument):
                raw_doc_count += 1
                fname = get_filename(message)
                fsize = get_file_size(message)
                mime = message.media.document.mime_type if message.media.document else "?"
                size_mb = round(fsize / (1024*1024), 1) if fsize else 0
                if len(sample_files) < 5:
                    sample_files.append(f"{fname} | {mime} | {size_mb}MB | msg_id:{message.id}")
    except Exception as e:
        log.error(f"  ❌ Raw diagnostic error: {e}")
    
    log.info(f"  🔬 RAW DIAGNOSTIC: {raw_msg_count} messages found after msg_id>{last_msg_id}, of which {raw_doc_count} are documents")
    for sf in sample_files:
        log.info(f"     📄 {sf}")
    
    # If NO messages found at all, try with min_id=0 (full rescan)
    effective_min_id = last_msg_id
    if raw_msg_count == 0 and last_msg_id > 0:
        log.warning(f"  ⚠️ 0 messages after msg_id>{last_msg_id}! Trying FULL RESCAN from msg_id=0...")
        effective_min_id = 0
        # Re-run raw diagnostic from 0
        try:
            async for message in client.iter_messages(entity, reverse=True, min_id=0, limit=50):
                raw_msg_count += 1
                if message.media and isinstance(message.media, MessageMediaDocument):
                    raw_doc_count += 1
                    fname = get_filename(message)
                    fsize = get_file_size(message)
                    mime = message.media.document.mime_type if message.media.document else "?"
                    size_mb = round(fsize / (1024*1024), 1) if fsize else 0
                    if len(sample_files) < 5:
                        sample_files.append(f"{fname} | {mime} | {size_mb}MB | msg_id:{message.id}")
        except Exception as e:
            log.error(f"  ❌ Full rescan diagnostic error: {e}")
        log.info(f"  🔬 FULL RESCAN: {raw_msg_count} messages found from start, {raw_doc_count} docs")
        for sf in sample_files:
            log.info(f"     📄 {sf}")
    
    # Now do the actual is_full_movie scan
    try:
        async for message in client.iter_messages(entity, reverse=True, min_id=effective_min_id, limit=2000):
            msg_scanned += 1
            
            # Log first 3 document messages for debugging regardless
            if message.media and isinstance(message.media, MessageMediaDocument) and msg_scanned <= 20:
                fname = get_filename(message)
                fsize = get_file_size(message)
                size_mb = round(fsize / (1024*1024), 1) if fsize else 0
                is_vid = is_video_file(message)
                log.info(f"  🔎 MSG#{message.id}: {fname} | {size_mb}MB | is_video={is_vid}")
            
            if is_full_movie(message):
                if t2t_is_already_forwarded(conn, ch_id, message.id):
                    already_forwarded_count += 1
                else:
                    scan_count += 1
                    if scan_count >= 3:
                        break
            else:
                # Count skip reasons
                if message.media and isinstance(message.media, MessageMediaDocument):
                    if not is_video_file(message):
                        skip_not_video += 1
                    elif get_file_size(message) < MIN_FILE_SIZE:
                        skip_too_small += 1
                    else:
                        skip_excluded += 1
    except Exception as e:
        log.error(f"  ❌ Pre-scan error: {e}")
        
    log.info(f"  🔍 Pre-scan result: {msg_scanned} msgs checked | {scan_count} NEW valid | {already_forwarded_count} already done")
    log.info(f"  📊 Skip reasons: not_video={skip_not_video} | too_small(<{MIN_FILE_SIZE//(1024*1024)}MB)={skip_too_small} | excluded_keyword={skip_excluded}")
    
    if scan_count == 0:
        log.info(f"  📭 No valid movie files found in channel. Skipping /superbatch.")
        t2t_update_channel(conn, ch_id, status="done", completed_at=datetime.utcnow(),
                           last_forwarded_msg_id=last_msg_id,
                           total_files_forwarded=(channel_data["forwarded"] or 0),
                           notes=f"No valid files. scanned={msg_scanned} skip_vid={skip_not_video} skip_size={skip_too_small} skip_kw={skip_excluded}")
        return True, 0
    
    log.info(f"  ✅ Pre-scan found {scan_count}+ valid files. Proceeding with /superbatch...")

    # Step 2: Send /superbatch
    log.info(f"  📤 Sending /superbatch to @{FLIMFYBOX_BOT}...")
    sb = await safe_send_message(flimfy_bot, "/superbatch")
    if not sb:
        log.error("  ❌ Failed to send /superbatch")
        t2t_update_channel(conn, ch_id, status="failed", notes="superbatch send failed")
        return False, 0
    await asyncio.sleep(random.uniform(5, 10))

    # Step 3: Iterate channel messages (oldest-first) — BURST BATCH MODE
    log.info(f"  📡 Forwarding files from channel (from msg_id > {effective_min_id})...")
    run_forwarded = 0          # Files forwarded THIS run (resets each cycle, capped at 100)
    total_forwarded = channel_data["forwarded"] or 0  # Lifetime counter
    batch_count = 0
    batch_size = random.randint(BATCH_MIN, BATCH_MAX)
    files_in_batch = 0
    hit_limit = False
    channel_exhausted = True   # True if we run out of messages naturally

    try:
        async for message in client.iter_messages(entity, reverse=True, min_id=effective_min_id):
            # Pause check
            if is_paused:
                log.info("  ⏸️ Paused mid-channel. Saving progress...")
                t2t_update_channel(conn, ch_id, last_forwarded_msg_id=last_msg_id,
                                   total_files_forwarded=total_forwarded, status="pending")
                await wait_for_resume()
                t2t_update_channel(conn, ch_id, status="processing")

            if not is_full_movie(message):
                continue

            # Dedup check
            if t2t_is_already_forwarded(conn, ch_id, message.id):
                continue

            fname = get_filename(message)
            fsize = get_file_size(message)
            size_mb = round(fsize / (1024*1024), 2) if fsize else 0

            # ── BURST SEND: tiny 1-2s delay between files within a batch ──
            log.info(f"  📁 [run:{run_forwarded+1}/{MAX_FILES_PER_CHANNEL}] {fname} ({size_mb} MB)")
            await asyncio.sleep(random.uniform(FILE_DELAY_MIN, FILE_DELAY_MAX))

            try:
                doc = message.media.document
                caption = message.text or message.message or ""
                sent = await safe_send_file(flimfy_bot, file=doc, caption=caption, force_document=False)
                if sent:
                    run_forwarded += 1
                    total_forwarded += 1
                    files_in_batch += 1
                    last_msg_id = message.id
                    t2t_log_forward(conn, ch_id, message.id, fname, fsize, "forwarded")
                    log.info(f"  ✅ Sent: {fname}")
                else:
                    t2t_log_forward(conn, ch_id, message.id, fname, fsize, "failed")
                    log.warning(f"  ❌ Failed: {fname}")
            except Exception as e:
                log.error(f"  ❌ Error: {fname}: {e}")
                t2t_log_forward(conn, ch_id, message.id, fname, fsize, "failed")

            # Save progress every 10 files
            if run_forwarded % 10 == 0:
                t2t_update_channel(conn, ch_id, last_forwarded_msg_id=last_msg_id,
                                   total_files_forwarded=total_forwarded)

            # ── BATCH PAUSE: after 10-20 files, take a 15-30s breather ──
            if files_in_batch >= batch_size:
                pause_time = random.uniform(BATCH_PAUSE_MIN, BATCH_PAUSE_MAX)
                batch_count += 1
                log.info(f"  🔄 Batch #{batch_count} done ({files_in_batch} files). Cooling {pause_time:.0f}s...")
                await asyncio.sleep(pause_time)
                files_in_batch = 0
                batch_size = random.randint(BATCH_MIN, BATCH_MAX)

            # ── HARD CAP: 100 files per run ──
            if run_forwarded >= MAX_FILES_PER_CHANNEL:
                log.info(f"  🛑 Hit {MAX_FILES_PER_CHANNEL}-file limit for this run.")
                hit_limit = True
                channel_exhausted = False
                break

        # If loop finished naturally without hitting limit
        if not hit_limit:
            channel_exhausted = True

    except errors.ChannelPrivateError:
        log.error(f"  ❌ Channel is private/inaccessible: {link}")
        t2t_update_channel(conn, ch_id, status="failed", notes="Channel private/inaccessible",
                           last_forwarded_msg_id=last_msg_id, total_files_forwarded=total_forwarded)
        return False, run_forwarded
    except Exception as e:
        log.error(f"  ❌ Channel iteration error: {e}")
        t2t_update_channel(conn, ch_id, status="failed", notes=f"Iteration error: {str(e)[:200]}",
                           last_forwarded_msg_id=last_msg_id, total_files_forwarded=total_forwarded)
        return False, run_forwarded

    # Step 3: Send /superdone ONLY if files were actually forwarded
    if run_forwarded > 0:
        log.info(f"  📤 Sending /superdone to @{FLIMFYBOX_BOT} ({run_forwarded} files forwarded)...")
        await asyncio.sleep(random.uniform(3, 6))
        await safe_send_message(flimfy_bot, "/superdone")
        await asyncio.sleep(random.uniform(2, 4))
    else:
        log.warning(f"  ⚠️ 0 files forwarded — SKIPPING /superdone (no need to trigger empty batch)")
        # Cancel superbatch since no files were sent
        await asyncio.sleep(random.uniform(1, 3))
        await safe_send_message(flimfy_bot, "/superdone")
        await asyncio.sleep(random.uniform(1, 2))

    # Update channel status
    if channel_exhausted:
        # All files in channel processed — mark done
        t2t_update_channel(conn, ch_id, status="done", completed_at=datetime.utcnow(),
                           last_forwarded_msg_id=last_msg_id, total_files_forwarded=total_forwarded)
        log.info(f"  🎉 Channel '{title}' FULLY DONE! {total_forwarded} total files forwarded.")
    else:
        # Hit 100-file limit — keep as pending for next cycle
        t2t_update_channel(conn, ch_id, status="pending",
                           last_forwarded_msg_id=last_msg_id, total_files_forwarded=total_forwarded)
        log.info(f"  ⏸️ Channel '{title}' paused at {total_forwarded} files. Will resume next cycle.")

    log.info(f"  📊 This run: {run_forwarded} files | Lifetime: {total_forwarded} files")
    return True, run_forwarded

# ── Main Pipeline (STRICT Clock Sync Mode) ──
def _seconds_until_next_hour():
    """Calculate seconds remaining until the start of the next hour."""
    now = datetime.now()
    seconds_past = now.minute * 60 + now.second
    remaining = 3600 - seconds_past
    if remaining <= 0:
        remaining = 3600
    return remaining

async def t2t_run_pipeline():
    global is_paused
    log.info(f"\n{'═'*58}")
    log.info(f"  🤖 T2T PIPELINE — STRICT Clock Sync Mode")
    log.info(f"  ⏰ Runs at the top of EVERY hour (XX:00)")
    log.info(f"  📦 Max {MAX_FILES_PER_CHANNEL} files per channel per run")
    log.info(f"  📦 Batch: {BATCH_MIN}-{BATCH_MAX} files, then {BATCH_PAUSE_MIN}-{BATCH_PAUSE_MAX}s pause")
    log.info(f"  📦 Min file size: {MIN_FILE_SIZE//(1024*1024)}MB")
    log.info(f"{'═'*58}")

    # ── Initial Clock Sync: wait until the first XX:00 ──
    now = datetime.now()
    if now.minute != 0:
        wait_secs = _seconds_until_next_hour()
        next_hour = (now + timedelta(seconds=wait_secs)).strftime("%H:00:00")
        log.info(f"  ⏰ Initial Clock Sync: Sleeping {wait_secs}s until {next_hour}...")
        await asyncio.sleep(wait_secs)

    run = 0
    while True:
        run += 1
        if is_paused:
            await wait_for_resume()

        # Re-establish MTProto connection if it dropped during sleep
        if not client.is_connected():
            log.warning("  🔌 Telethon disconnected — reconnecting...")
            await client.connect()
            log.info("  ✅ Telethon reconnected.")

        now = datetime.now()
        log.info(f"\n  🚀 [CLOCK-WISE] Hourly T2T run #{run} starting at {now.strftime('%H:%M:%S')}...")

        conn = db_utils.get_db_connection()
        if not conn:
            log.error("  ❌ DB connection failed!")
            await asyncio.sleep(_seconds_until_next_hour())
            continue

        t2t_ensure_tables(conn)
        
        # Loop over ALL pending/stale channels for this hour
        while True:
            channel = t2t_fetch_next_channel(conn)
            if not channel:
                log.info(f"  📭 All channels processed. No more pending channels.")
                break

            log.info(f"\n{'━'*55}")
            log.info(f"  🎯 Target: {channel['title'] or channel['link']}")
            log.info(f"  📊 Previously forwarded: {channel['forwarded']} | Resume from msg: {channel['last_msg_id']}")
            log.info(f"{'━'*55}")

            try:
                success, run_forwarded = await t2t_forward_channel_files(conn, channel)
            except Exception as e:
                log.error(f"  💥 Pipeline error: {e}")
                import traceback
                traceback.print_exc()
                success = False
                run_forwarded = 0

            if success:
                log.info(f"  ✅ Channel batch completed successfully.")
            else:
                log.info(f"  ⏩ Channel failed/skipped.")
                
            if run_forwarded > 0:
                log.info(f"  Forwarded {run_forwarded} files. Status marked 'done'. Resting for 1 hour to prevent FloodWait.")
                t2t_update_channel(conn, channel["id"], status="done", completed_at=datetime.utcnow())
                await asyncio.sleep(3600)
            else:
                log.info("  No new files found. Status marked 'done'. Fast skipping to next channel.")
                t2t_update_channel(conn, channel["id"], status="done", completed_at=datetime.utcnow())
                continue

        db_utils.close_db_connection(conn)

        # ── Promo: send once per hour (every 4th run) ──
        if run % 4 == 0:
            await send_promos_in_free_time()

        # ── STRICT Clock Sync: sleep until the NEXT hour starts ──
        wait_secs = _seconds_until_next_hour()
        next_hour = (datetime.now() + timedelta(seconds=wait_secs)).strftime("%H:%M:%S")
        log.info(f"  ⏰ Clock Sync: Next run in {wait_secs}s (at ~{next_hour}). Sleeping...")

        # Sleep in chunks for pause responsiveness
        slept = 0
        while slept < wait_secs:
            if is_paused:
                await wait_for_resume()
                break
            chunk = min(30, wait_secs - slept)
            await asyncio.sleep(chunk)
            slept += chunk

# ── Safety Check ──
async def safety_check():
    me = await client.get_me()
    log.info(f"  📱 Account: {me.first_name} (@{me.username or 'N/A'})")
    log.info(f"  🆔 ID: {me.id}")
    if hasattr(me, 'restricted') and me.restricted:
        log.error("  🚫 ACCOUNT RESTRICTED!")
        return False
    log.info("  ✅ Account healthy")
    return True

# ── Promo Bot Logic (Runs alongside scraper) ──
PROMO_GROUPS = [
    -1003731701537, -1004294114933, -1003139036639, -1003953915147, 
    -1003085027822, -1003760514520, -1003245429631, -1003247032511
]

def generate_promo_message():
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
    random.shuffle(lines)
    msg = f"{random.choice(headers)}\n\n"
    msg += "\n".join(lines[:random.randint(3, 5)])
    msg += f"\n\n{random.choice(footers)}"
    return msg

# Track which group gets promo next (round-robin style)
_promo_index = 0

async def send_promos_in_free_time():
    """
    Scraping khatam hone ke baad, free time me 1 random promo group me message bhejta hai.
    Har cycle me sirf 1 group — taaki account kabhi overloaded na lage.
    """
    global _promo_index
    
    if not PROMO_GROUPS:
        return
    
    # Check remaining time
    remaining = _seconds_until_next_run()
    if remaining < 120:  # 2 min se kam bacha hai toh mat bhejo
        log.info("  📣 [PROMO] Not enough free time (<2 min). Skipping this cycle.")
        return
    
    try:
        # Round-robin: har cycle me agla group
        group_id = PROMO_GROUPS[_promo_index % len(PROMO_GROUPS)]
        _promo_index += 1
        
        msg = generate_promo_message()
        
        # Thoda random delay before sending (30s-2min) — human jaisa
        pre_delay = random.randint(30, 120)
        log.info(f"  📣 [PROMO] Scraping done! Sending promo in {pre_delay}s...")
        await asyncio.sleep(pre_delay)
        
        log.info(f"  📣 [PROMO] Sending to group {group_id}...")
        await client.send_message(group_id, msg, link_preview=False)
        log.info(f"  ✅ [PROMO] Sent successfully to group {group_id}!")
        
    except errors.FloodWaitError as e:
        log.warning(f"  ⚠️ [PROMO] Flood wait {e.seconds}s. Will retry next cycle.")
    except errors.UserBannedInChannelError:
        log.warning(f"  ⚠️ [PROMO] Banned in group {group_id} — skipping.")
    except Exception as e:
        log.error(f"  ❌ [PROMO] Failed: {e}")


# ── Entry Point ──
async def start_t2t_worker():
    log.info("🤖 Starting T2T Userbot Worker...")
    await client.connect()

    if not await client.is_user_authorized():
        log.error("❌ Session expired ya invalid! Run: python qr_login.py")
        await client.disconnect()
        return

    healthy = await safety_check()
    if not healthy:
        await client.disconnect()
        return

    await asyncio.sleep(10)
    register_commands()

    # Ensure tables exist on startup
    conn = db_utils.get_db_connection()
    if conn:
        t2t_ensure_tables(conn)
        db_utils.close_db_connection(conn)

    log.info("  📣 Promo will run after each scraping cycle (sequential mode).")

    await t2t_run_pipeline()
    await client.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(start_t2t_worker())
    except KeyboardInterrupt:
        print("\n👋 T2T Userbot stopped.")
