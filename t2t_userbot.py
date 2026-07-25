"""
T2T Userbot — Telegram-to-Telegram Channel Forwarder
Forwards MKV/MP4 files from target channels to @FlimfyBoxBot PM.
"""
import os, re, sys, random, asyncio, logging, json, unicodedata
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
    from telethon.errors import MessageNotModifiedError
except ImportError:
    print("pip install telethon")
    sys.exit(1)

try:
    import aiohttp
except ImportError:
    aiohttp = None

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

# ── FindBatchID Config ──
TMDB_API_KEY = os.environ.get("TMDB_API_KEY", "9fa44f5e9fbd41415df930ce5b81c4d7")
TARGET_BOT = "Searchmovie4u_bot"
FILE_COLLECT_TIMEOUT = 15        # Seconds to wait for new files before assuming batch done
SEASON_COOLDOWN_MIN = 10         # Delay between seasons (min)
SEASON_COOLDOWN_MAX = 20         # Delay between seasons (max)
BUTTON_CLICK_DELAY_MIN = 3       # Delay before clicking a button (min)
BUTTON_CLICK_DELAY_MAX = 6       # Delay before clicking a button (max)
SEARCH_MSG_DELAY_MIN = 2         # Delay before sending search query (min)
SEARCH_MSG_DELAY_MAX = 4         # Delay before sending search query (max)
_findbatch_running = False       # Guard against concurrent /findbatchid runs

# Robust regex to detect "Episode 1" / "E01" / "S01E01" etc.
EPISODE_1_PATTERN = re.compile(
    r'(?i)'
    r'(?:'
        r'S\d{1,2}\s*E\s*0*1(?!\d)'    # S01E01, S1E1, S01 E 01
        r'|(?<!\d)E\s*0*1(?!\d)'         # E01, E1 (not part of larger number)
        r'|Ep\s*\.?\s*0*1(?!\d)'         # Ep01, Ep.01, Ep 1
        r'|Episode\s*0*1(?!\d)'          # Episode 01, Episode 1
        r'|\b0*1\s*of\s*\d+'             # "1 of 10", "01 of 12"
    r')'
)

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

# ══════════════════════════════════════════════════════════════════════════════
# 🎯 FindBatchID — TMDB Metadata + Target Bot Scraping + Auto Forward
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_tmdb_metadata(imdb_id: str) -> dict:
    """
    Fetch metadata from TMDB using an IMDb ID.
    Returns: {"title": str, "year": int, "media_type": "movie"|"tv", "seasons": int}
    """
    if not TMDB_API_KEY:
        log.error("  ❌ TMDB_API_KEY not set!")
        return None

    find_url = f"https://api.themoviedb.org/3/find/{imdb_id}?api_key={TMDB_API_KEY}&external_source=imdb_id"

    try:
        if aiohttp:
            async with aiohttp.ClientSession() as session:
                async with session.get(find_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                    if resp.status != 200:
                        log.error(f"  ❌ TMDB /find error: HTTP {resp.status}")
                        return None
                    data = await resp.json()
        else:
            import requests as _req
            r = _req.get(find_url, timeout=15)
            if r.status_code != 200:
                log.error(f"  ❌ TMDB /find error: HTTP {r.status_code}")
                return None
            data = r.json()
    except Exception as e:
        log.error(f"  ❌ TMDB fetch error: {e}")
        return None

    # Check movie results first, then TV
    movie_results = data.get("movie_results", [])
    tv_results = data.get("tv_results", [])

    if movie_results:
        m = movie_results[0]
        title = m.get("title", "")
        year_str = str(m.get("release_date", ""))[:4]
        year = int(year_str) if year_str.isdigit() else 0
        return {"title": title, "year": year, "media_type": "movie", "seasons": 0}

    elif tv_results:
        tv = tv_results[0]
        title = tv.get("name", "")
        year_str = str(tv.get("first_air_date", ""))[:4]
        year = int(year_str) if year_str.isdigit() else 0
        tmdb_id = tv.get("id")

        # Fetch season count from /tv/{id}
        seasons = 1
        if tmdb_id:
            try:
                tv_url = f"https://api.themoviedb.org/3/tv/{tmdb_id}?api_key={TMDB_API_KEY}"
                if aiohttp:
                    async with aiohttp.ClientSession() as session:
                        async with session.get(tv_url, timeout=aiohttp.ClientTimeout(total=15)) as resp:
                            if resp.status == 200:
                                tv_data = await resp.json()
                                all_seasons = tv_data.get("seasons", [])
                                # Filter out Season 0 (Specials)
                                seasons = len([s for s in all_seasons if s.get("season_number", 0) > 0])
                                if seasons == 0:
                                    seasons = tv_data.get("number_of_seasons", 1)
                else:
                    import requests as _req
                    r = _req.get(tv_url, timeout=15)
                    if r.status_code == 200:
                        tv_data = r.json()
                        all_seasons = tv_data.get("seasons", [])
                        seasons = len([s for s in all_seasons if s.get("season_number", 0) > 0])
                        if seasons == 0:
                            seasons = tv_data.get("number_of_seasons", 1)
            except Exception as e:
                log.warning(f"  ⚠️ Could not fetch season count: {e}. Defaulting to 1.")
                seasons = 1

        return {"title": title, "year": year, "media_type": "tv", "seasons": max(seasons, 1)}

    else:
        log.error(f"  ❌ TMDB: No results for IMDb ID '{imdb_id}'")
        return None


def _normalize_text(text: str) -> str:
    """Lowercase, replace common separators with spaces, strip non-alphanum."""
    if not text:
        return ""
    text = text.lower()
    text = re.sub(r'[._\-\+]', ' ', text)   # separators → space
    text = re.sub(r'[^a-z0-9\s]', '', text)  # strip special chars
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def verify_file_matches(text: str, expected_title: str, expected_year: int) -> bool:
    """
    Verify that a filename/caption matches the expected movie/series.
    - Title: ≥70% token overlap required.
    - Year: If a year is present in the file text, it must match ±1.
    Returns True if the file is a plausible match.
    """
    if not text or not expected_title:
        return False

    norm_text = _normalize_text(text)
    norm_title = _normalize_text(expected_title)

    # Token overlap check
    title_tokens = set(norm_title.split())
    text_tokens = set(norm_text.split())

    if not title_tokens:
        return False

    overlap = title_tokens & text_tokens
    overlap_ratio = len(overlap) / len(title_tokens)

    if overlap_ratio < 0.70:
        log.info(f"  ⏭️ TITLE MISMATCH: '{expected_title}' vs file text (overlap: {overlap_ratio:.0%})")
        return False

    # Year check (±1 tolerance)
    if expected_year and expected_year > 1900:
        years_in_text = re.findall(r'\b((?:19|20)\d{2})\b', norm_text)
        if years_in_text:
            # Check if ANY extracted year is within ±1
            year_match = any(abs(int(y) - expected_year) <= 1 for y in years_in_text)
            if not year_match:
                log.info(f"  ⏭️ YEAR MISMATCH: expected ~{expected_year}, found {years_in_text} in '{text[:60]}'")
                return False

    return True


async def safe_fuzzy_click(message, target_text):
    """
    Manually iterates over buttons, normalizes fancy fonts, and clicks the target button safely.
    """
    if not message.buttons:
        return False
        
    for row in message.buttons:
        for btn in row:
            # Normalize fancy fonts (e.g. 𝗔𝗟𝗟 𝗙𝗜𝗟𝗘𝗦 -> all files)
            norm_text = unicodedata.normalize('NFKC', btn.text).lower()
            
            if target_text.lower() in norm_text:
                # Found the right button! Now click it with retry logic.
                for attempt in range(3):
                    try:
                        await btn.click()
                        return True
                    except errors.FloodWaitError as e:
                        await asyncio.sleep(e.seconds + random.uniform(3, 6))
                    except errors.MessageNotModifiedError:
                        return True  # Button already processed
                    except Exception as e:
                        if attempt < 2:
                            await asyncio.sleep(random.uniform(2, 5))
                        else:
                            return False
                return False
                
    return False # Button not found


async def findbatchid_scrape(imdb_id: str, event):
    """
    Core orchestration for /findbatchid.
    Scrapes files from @Searchmovie4u_bot and forwards to @FlimfyBoxBot.
    """
    global _findbatch_running
    if _findbatch_running:
        await event.reply("⚠️ Another /findbatchid is already running. Please wait.")
        return

    _findbatch_running = True
    total_forwarded = 0
    status_msg = await event.reply(f"⏳ Fetching metadata for `{imdb_id}` from TMDB...")

    try:
        # ══════════════════════════════════════════════════
        # PHASE 1: INITIALIZATION
        # ══════════════════════════════════════════════════
        meta = await fetch_tmdb_metadata(imdb_id)
        if not meta:
            await status_msg.edit(f"❌ TMDB: No data found for `{imdb_id}`. Check the IMDb ID.")
            return

        title = meta["title"]
        year = meta["year"]
        media_type = meta["media_type"]
        seasons = meta["seasons"]

        type_label = "🎬 Movie" if media_type == "movie" else f"📺 TV Show ({seasons} season{'s' if seasons > 1 else ''})"
        await status_msg.edit(
            f"✅ **Metadata Fetched!**\n\n"
            f"🎬 **Title:** `{title}`\n"
            f"📅 **Year:** {year}\n"
            f"🏷️ **Type:** {type_label}\n\n"
            f"⏳ Sending `/batchid {imdb_id}` to @{FLIMFYBOX_BOT}..."
        )
        log.info(f"  🎯 FindBatchID: {title} ({year}) — {type_label}")

        # Resolve entities
        try:
            flimfy_bot = await client.get_entity(FLIMFYBOX_BOT)
        except Exception as e:
            await status_msg.edit(f"❌ Cannot resolve @{FLIMFYBOX_BOT}: {e}")
            return

        try:
            target_bot = await client.get_entity(TARGET_BOT)
        except Exception as e:
            await status_msg.edit(f"❌ Cannot resolve @{TARGET_BOT}: {e}")
            return

        # Send /batchid to FlimfyBoxBot
        await asyncio.sleep(random.uniform(SEARCH_MSG_DELAY_MIN, SEARCH_MSG_DELAY_MAX))
        await safe_send_message(flimfy_bot, f"/batchid {imdb_id}")
        log.info(f"  📤 Sent /batchid {imdb_id} to @{FLIMFYBOX_BOT}")
        await asyncio.sleep(random.uniform(5, 8))  # Let FlimfyBoxBot process

        # ══════════════════════════════════════════════════
        # PHASE 2: SEASON LOOP
        # ══════════════════════════════════════════════════
        if media_type == "movie":
            season_range = [0]  # Single iteration, no season tag
        else:
            season_range = list(range(1, seasons + 1))

        for season_num in season_range:
            if not _findbatch_running:
                log.info("  🛑 Scraping stopped by user before new season.")
                break
                
            if media_type == "movie":
                search_query = f"{title} {year}" if year else title
                season_label = "Movie"
            else:
                search_query = f"{title} s{season_num:02d}"
                season_label = f"Season {season_num}"

            log.info(f"\n  {'━'*50}")
            log.info(f"  📡 {season_label}: Searching '{search_query}' on @{TARGET_BOT}")
            log.info(f"  {'━'*50}")

            await status_msg.edit(
                f"📡 **{season_label}** — Searching on @{TARGET_BOT}...\n"
                f"🔍 Query: `{search_query}`"
            )

            # ── Setup hybrid message queue ──
            file_queue = asyncio.Queue()
            _collector_active = True

            @client.on(events.NewMessage(from_users=target_bot.id))
            async def _file_collector(evt):
                if _collector_active:
                    await file_queue.put(evt.message)

            # Send search query
            await asyncio.sleep(random.uniform(SEARCH_MSG_DELAY_MIN, SEARCH_MSG_DELAY_MAX))
            try:
                await safe_send_message(target_bot, search_query)
            except Exception as e:
                log.error(f"  ❌ Failed to send search query: {e}")
                client.remove_event_handler(_file_collector)
                continue

            # Wait for menu/response from target bot to be sent and edited
            await asyncio.sleep(4)
            
            menu_msg = None
            async for msg in client.iter_messages(target_bot, limit=3):
                if msg.buttons:
                    menu_msg = msg
                    break
            
            if not menu_msg:
                log.warning(f"  ⚠️ Target bot response has no buttons after 4s. Skipping {season_label}.")
                _collector_active = False
                client.remove_event_handler(_file_collector)
                continue
                
            log.info(f"  📩 Got menu response from @{TARGET_BOT} (msg_id: {menu_msg.id})")
            
            # Clear out the file queue of the initial unedited text messages
            while not file_queue.empty():
                try:
                    file_queue.get_nowait()
                except asyncio.QueueEmpty:
                    break

            # ── PAGINATION LOOP ──
            season_forwarded = 0
            found_ep1 = False
            page_num = 0
            files_in_burst = 0
            burst_size = random.randint(BATCH_MIN, BATCH_MAX)

            while True:
                if not _findbatch_running:
                    log.info(f"  🛑 Scraping stopped by user before page {page_num + 1}.")
                    break
                    
                page_num += 1
                log.info(f"  📄 {season_label} — Page {page_num}: Clicking 'All Files'...")

                # Find and click "All Files" button
                await asyncio.sleep(random.uniform(BUTTON_CLICK_DELAY_MIN, BUTTON_CLICK_DELAY_MAX))
                click_result = await safe_fuzzy_click(menu_msg, "all files")
                
                if not click_result:
                    # Try broader match
                    click_result = await safe_fuzzy_click(menu_msg, "all")
                    
                if not click_result:
                    log.error(f"  ❌ Failed to click 'All Files'. Aborting {season_label}.")
                    break

                log.info(f"  ✅ Clicked 'All Files'. Waiting for files to drop...")

                # ── Collect files with timeout ──
                page_files = []
                page_found_ep1 = False
                consecutive_text_msgs = 0

                while True:
                    if not _findbatch_running:
                        log.info("  🛑 File collection stopped by user.")
                        break
                        
                    try:
                        msg = await asyncio.wait_for(file_queue.get(), timeout=FILE_COLLECT_TIMEOUT)
                    except asyncio.TimeoutError:
                        log.info(f"  ⏱️ No new message for {FILE_COLLECT_TIMEOUT}s. Batch done.")
                        break

                    # Skip plain text messages (promos, ads, etc.)
                    if not msg.media or not isinstance(msg.media, MessageMediaDocument):
                        consecutive_text_msgs += 1
                        if msg.reply_markup:
                            # This might be a new menu/navigation message — save for later
                            menu_msg = msg
                            log.info(f"  📋 Got updated menu message (msg_id: {msg.id})")
                        elif consecutive_text_msgs <= 3:
                            log.info(f"  ⏭️ Skipping text message: '{(msg.text or '')[:50]}'")
                        continue

                    consecutive_text_msgs = 0

                    # Check if it's a video file
                    if not is_video_file(msg):
                        fname = get_filename(msg)
                        log.info(f"  ⏭️ Not a video file: {fname}")
                        continue

                    fname = get_filename(msg)
                    caption = msg.text or msg.message or ""
                    check_text = caption if caption else fname

                    # Verify name & year match
                    if not verify_file_matches(check_text, title, year):
                        log.info(f"  ⏭️ VERIFICATION FAILED: {fname}")
                        continue

                    # ✅ Valid file — forward to FlimfyBoxBot
                    page_files.append(msg)

                    # Burst delay
                    await asyncio.sleep(random.uniform(FILE_DELAY_MIN, FILE_DELAY_MAX))

                    try:
                        doc = msg.media.document
                        fwd_caption = caption
                        sent = await safe_send_file(flimfy_bot, file=doc, caption=fwd_caption, force_document=False)
                        if sent:
                            season_forwarded += 1
                            total_forwarded += 1
                            files_in_burst += 1
                            fsize_mb = round(get_file_size(msg) / (1024*1024), 1)
                            log.info(f"  ✅ [{total_forwarded}] Forwarded: {fname} ({fsize_mb}MB)")
                        else:
                            log.warning(f"  ❌ Forward failed: {fname}")
                    except Exception as e:
                        log.error(f"  ❌ Forward error for {fname}: {e}")

                    # Burst batch pause
                    if files_in_burst >= burst_size:
                        pause_time = random.uniform(BATCH_PAUSE_MIN, BATCH_PAUSE_MAX)
                        log.info(f"  🔄 Burst pause ({files_in_burst} files). Cooling {pause_time:.0f}s...")
                        await asyncio.sleep(pause_time)
                        files_in_burst = 0
                        burst_size = random.randint(BATCH_MIN, BATCH_MAX)

                    # Check for Episode 1
                    if EPISODE_1_PATTERN.search(check_text):
                        log.info(f"  🎯 EPISODE 1 DETECTED in: {fname}")
                        page_found_ep1 = True

                # End of file collection for this page
                log.info(f"  📊 Page {page_num}: {len(page_files)} files forwarded")

                if page_found_ep1:
                    found_ep1 = True

                # ── STOP or NEXT? ──
                if found_ep1:
                    log.info(f"  ✅ Episode 1 found! {season_label} is complete.")
                    break

                # Check for "Next" button and click it
                log.info(f"  ➡️ Clicking 'Next' for page {page_num + 1}...")
                await asyncio.sleep(random.uniform(BUTTON_CLICK_DELAY_MIN, BUTTON_CLICK_DELAY_MAX))
                
                click_result = await safe_fuzzy_click(menu_msg, "next")
                if not click_result:
                    click_result = await safe_fuzzy_click(menu_msg, "➡")
                    
                if not click_result:
                    log.info(f"  📌 No 'Next' button found. {season_label} is complete.")
                    break

                # After clicking Next, wait for updated menu
                await asyncio.sleep(4)
                
                updated_menu = None
                async for msg in client.iter_messages(target_bot, limit=3):
                    if msg.buttons:
                        updated_menu = msg
                        break
                
                if updated_menu:
                    menu_msg = updated_menu
                else:
                    log.warning(f"  ⚠️ Could not refresh menu message. Ending {season_label}.")
                    break
                
                # Clear out the file queue of any intermediate text messages
                temp_files = []
                while not file_queue.empty():
                    try:
                        q_msg = file_queue.get_nowait()
                        if q_msg.media and isinstance(q_msg.media, MessageMediaDocument):
                            temp_files.append(q_msg)
                    except asyncio.QueueEmpty:
                        break
                for f in temp_files:
                    await file_queue.put(f)

            # ── End of pagination loop for this season ──
            _collector_active = False
            client.remove_event_handler(_file_collector)

            log.info(f"  🎉 {season_label} done! {season_forwarded} files forwarded.")

            # Update admin
            await status_msg.edit(
                f"✅ **{season_label} Complete!**\n"
                f"📁 Files forwarded: {season_forwarded}\n"
                f"📊 Total so far: {total_forwarded}"
            )

            # ── CLEANUP: Clear chat with target bot ──
            log.info(f"  🧹 Clearing chat history with @{TARGET_BOT}...")
            try:
                msg_ids_to_delete = []
                async for msg in client.iter_messages(target_bot, limit=200):
                    msg_ids_to_delete.append(msg.id)
                if msg_ids_to_delete:
                    # Delete in batches of 100 (Telegram limit)
                    for i in range(0, len(msg_ids_to_delete), 100):
                        batch = msg_ids_to_delete[i:i+100]
                        await client.delete_messages(target_bot, batch)
                        await asyncio.sleep(1)
                log.info(f"  ✅ Chat cleared ({len(msg_ids_to_delete)} messages deleted)")
            except Exception as e:
                log.warning(f"  ⚠️ Chat cleanup failed (non-critical): {e}")

            # Inter-season cooldown
            if season_num < season_range[-1]:
                cooldown = random.uniform(SEASON_COOLDOWN_MIN, SEASON_COOLDOWN_MAX)
                log.info(f"  💤 Season cooldown: {cooldown:.0f}s before next season...")
                await asyncio.sleep(cooldown)

        # ══════════════════════════════════════════════════
        # PHASE 3: FINALIZATION
        # ══════════════════════════════════════════════════
        log.info(f"\n  📤 Sending /done to @{FLIMFYBOX_BOT}...")
        await asyncio.sleep(random.uniform(SEARCH_MSG_DELAY_MIN, SEARCH_MSG_DELAY_MAX))
        await safe_send_message(flimfy_bot, "/done")
        log.info(f"  ✅ Sent /done to @{FLIMFYBOX_BOT}")

        if not _findbatch_running:
            final_report = (
                f"🛑 **FindBatchID Stopped by Admin!**\n\n"
                f"🎬 **Title:** `{title}`\n"
                f"📁 **Files Forwarded Before Stop:** {total_forwarded}\n"
                f"✅ `/done` sent to @{FLIMFYBOX_BOT} to close batch."
            )
        else:
            # Final report
            final_report = (
                f"🎉 **FindBatchID Complete!**\n\n"
                f"🎬 **Title:** `{title}`\n"
                f"📅 **Year:** {year}\n"
                f"🏷️ **Type:** {type_label}\n\n"
                f"📁 **Total Files Forwarded:** {total_forwarded}\n"
                f"✅ `/done` sent to @{FLIMFYBOX_BOT}"
            )
        
        await status_msg.edit(final_report)
        log.info(f"  🎉 FindBatchID END: {total_forwarded} files forwarded for '{title}'")

    except Exception as e:
        log.error(f"  💥 FindBatchID error: {e}")
        import traceback
        traceback.print_exc()
        try:
            await status_msg.edit(f"❌ FindBatchID Error: {e}")
        except:
            pass
    finally:
        _findbatch_running = False


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

    # ── /findbatchid Command ──
    @client.on(events.NewMessage(pattern=r'^/findbatchid\s+(tt\d+)', outgoing=True))
    async def handle_findbatchid(event):
        if OWNER_ID and event.sender_id != OWNER_ID:
            return
        imdb_id = event.pattern_match.group(1).strip()
        log.info(f"  🎯 /findbatchid triggered: {imdb_id}")
        # Run scraping in background so event handler returns quickly
        asyncio.create_task(findbatchid_scrape(imdb_id, event))

    # ── /stopfindbatch Command ──
    @client.on(events.NewMessage(pattern=r'^/stopfindbatch$', outgoing=True))
    async def handle_stopfindbatch(event):
        global _findbatch_running
        if OWNER_ID and event.sender_id != OWNER_ID:
            return
        if _findbatch_running:
            _findbatch_running = False
            await event.reply("🛑 FindBatchID will stop after the current page completes.")
        else:
            await event.reply("ℹ️ No FindBatchID process is running.")

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
    
    effective_min_id = last_msg_id
    
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

        # ── Promo: DISABLED — competitor reports se account restrict ho raha tha ──
        # await send_promos_in_free_time()  # ⛔ DISABLED

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

# ── Promo Bot Logic — ⛔ COMPLETELY DISABLED ──
# Competitor channels report karke account restrict karwa rahe the.
# Promo system permanently band kar diya gaya hai.
# Agar future mein chahiye toh Git history se restore karo.


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

    log.info("  ⛔ Promo system is DISABLED. Only scraping will run.")

    await t2t_run_pipeline()
    await client.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(start_t2t_worker())
    except KeyboardInterrupt:
        print("\n👋 T2T Userbot stopped.")
