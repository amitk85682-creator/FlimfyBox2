"""
Auto Movie Scraper — 1337x Torrent (via TMDB Trending Discovery)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Discovers trending movies from TMDB API, searches 1337x for magnet
links, enriches with metadata, and saves to PostgreSQL for the
userbot leech pipeline.

Architecture:
  TMDB Trending API → Movie Names
       ↓
  torrent_scraper.get_magnet_link(name) → Magnet URI
       ↓
  PostgreSQL: movies + pending_leech tables

Run:
  python auto_scraper.py                     # Ek baar + har 2 ghante
  python auto_scraper.py --once              # Sirf ek baar
  python auto_scraper.py --interval 60       # Har 60 min
  python auto_scraper.py --max-new 5         # Max 5 movies per run

Env vars:
  DATABASE_URL    — PostgreSQL connection string
  TMDB_API_KEY    — TMDB se trending movies + metadata fetch
"""


import os
import asyncio
import re
import gc
import time
import logging
import argparse
import json
from datetime import datetime
from typing import Optional, Dict, List, Tuple

print("--> 2. Ab db_utils load ho raha hai (Database connection try karega)...")
import db_utils
print("--> 3. Database successfully connect ho gaya!")

# ── Logging ─────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%H:%M:%S",
)
log = logging.getLogger("AutoScraper")

# ── Config ───────────────────────────────────────────────────────────────────
DATABASE_URL = os.environ.get("DATABASE_URL", "")
TMDB_KEY = os.environ.get("TMDB_API_KEY", "")
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
POST_CHANNEL_ID = os.environ.get("POST_CHANNEL_ID", "")


# ════════════════════════════════════════════════════════════════════════════
# TMDB TRENDING DISCOVERY
# ════════════════════════════════════════════════════════════════════════════

async def fetch_trending_movies(page: int = 1, time_window: str = "week") -> List[Dict]:
    """
    Fetch trending movies from TMDB API with curl_cffi and Proxy Fallback for ISP bypass.
    """
    if not TMDB_KEY:
        log.error("❌ TMDB_API_KEY not set! Cannot discover trending movies.")
        log.info("   Set it in .env: TMDB_API_KEY=your_key_here")
        return []

    import urllib.parse
    from curl_cffi import requests

    movies = []
    url = f"https://api.themoviedb.org/3/trending/movie/{time_window}?api_key={TMDB_KEY}&language=en-US&page={page}"
    data = None

    # 🚀 3-Retry Logic for TMDB Bypass
    for attempt in range(1, 4):
        try:
            log.info(f"  📡 Fetching TMDB (Page {page}, Attempt {attempt}/3)...")
            
            # 1. Primary Bypass
            async with requests.AsyncSession(impersonate="chrome") as session:
                resp = await session.get(url, timeout=15.0)
                if resp.status_code == 200:
                    data = resp.json()
                    break
                else:
                    log.error(f"TMDB direct error: HTTP {resp.status_code}")
            
        except Exception as e:
            log.warning(f"  ⚠️ Direct bypass failed (Attempt {attempt}): {e}")
            
            # 2. Fallback Proxy
            try:
                log.info(f"  🔄 Switching to Proxy Fallback (Attempt {attempt}/3)...")
                encoded_url = urllib.parse.quote(url, safe="")
                proxy_url = f"https://corsproxy.io/?url={encoded_url}"
                async with requests.AsyncSession(impersonate="chrome") as session:
                    resp = await session.get(proxy_url, timeout=20.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        break
                    else:
                        log.error(f"TMDB proxy error: HTTP {resp.status_code}")
            except Exception as proxy_e:
                log.error(f"  ❌ Proxy Fallback failed: {proxy_e}")

        if attempt < 3:
            log.info("     Retrying in 2s...")
            await asyncio.sleep(2)

    if not data:
        return []

    results = data.get("results", [])

    for m in results:
        title = m.get("title", "").strip()
        if not title:
            continue

        # Extract year from release_date
        release_date = m.get("release_date", "")
        year = int(release_date[:4]) if release_date and len(release_date) >= 4 else 0

        # Genre IDs → names mapping (TMDB genre IDs)
        genre_map = {
            28: "Action", 12: "Adventure", 16: "Animation",
            35: "Comedy", 80: "Crime", 99: "Documentary",
            18: "Drama", 10751: "Family", 14: "Fantasy",
            36: "History", 27: "Horror", 10402: "Music",
            9648: "Mystery", 10749: "Romance", 878: "Sci-Fi",
            10770: "TV Movie", 53: "Thriller", 10752: "War",
            37: "Western",
        }
        genre_ids = m.get("genre_ids", [])
        genres = ", ".join(genre_map.get(gid, "") for gid in genre_ids if gid in genre_map)

        # Poster
        poster_path = m.get("poster_path", "")
        poster_url = f"https://image.tmdb.org/t/p/w500{poster_path}" if poster_path else ""

        # Language → Category
        lang = m.get("original_language", "")
        if lang == "hi":
            category = "Bollywood"
        elif lang == "en":
            category = "Hollywood"
        elif lang in ("ta", "te", "ml", "kn"):
            category = "South"
        else:
            category = "Movies"

        movies.append({
            "tmdb_id": m.get("id"),
            "title": title,
            "year": year,
            "rating": str(round(m.get("vote_average", 0), 1)),
            "genre": genres,
            "description": m.get("overview", ""),
            "poster_url": poster_url,
            "category": category,
            "language": lang,
        })

    log.info(f"  ✅ TMDB page {page}: {len(results)} trending movies")

    return movies


async def enrich_tmdb_detail(tmdb_id: int) -> Dict:
    """
    Fetch detailed metadata for a movie from TMDB (cast, IMDb ID, etc.).
    Called only when we have a confirmed magnet link to save DB writes.
    """
    if not TMDB_KEY or not tmdb_id:
        return {}

    import urllib.parse
    from curl_cffi import requests

    url = f"https://api.themoviedb.org/3/movie/{tmdb_id}?api_key={TMDB_KEY}&append_to_response=credits"
    data = None
    
    # 🚀 3-Retry Logic for Detail Enrichment
    for attempt in range(1, 4):
        try:
            async with requests.AsyncSession(impersonate="chrome") as session:
                resp = await session.get(url, timeout=15.0)
                if resp.status_code == 200:
                    data = resp.json()
                    break
        except Exception as e:
            log.warning(f"  ⚠️ Detail Fetch failed (Attempt {attempt}): {e}")
            # Fallback Proxy
            try:
                encoded_url = urllib.parse.quote(url, safe="")
                proxy_url = f"https://corsproxy.io/?url={encoded_url}"
                async with requests.AsyncSession(impersonate="chrome") as session:
                    resp = await session.get(proxy_url, timeout=20.0)
                    if resp.status_code == 200:
                        data = resp.json()
                        break
            except:
                pass
        
        if attempt < 3:
            await asyncio.sleep(2)

    if not data:
        return {}

    cast = ", ".join(c["name"] for c in data.get("credits", {}).get("cast", [])[:5])
    imdb_id = data.get("imdb_id", "")

    return {
        "cast": cast,
        "imdb_id": imdb_id,
        "description": data.get("overview", ""),
    }


# ════════════════════════════════════════════════════════════════════════════
# DATABASE
# ════════════════════════════════════════════════════════════════════════════

def get_conn():
    conn = db_utils.get_db_connection()
    if not conn:
        raise RuntimeError("DATABASE_URL mismatch or Pool error!")
    return conn


def close_conn(conn):
    db_utils.close_db_connection(conn)


def _is_already_scraped(conn, movie_title: str, year: int) -> bool:
    """Check if we already have this movie in the database (by title+year) AND it has files."""
    cur = conn.cursor()
    try:
        # Check if movie exists AND has actual files associated with it
        cur.execute("""
            SELECT 1 FROM movies m
            JOIN movie_files mf ON m.id = mf.movie_id
            WHERE LOWER(m.title) = LOWER(%s) AND m.year = %s
            LIMIT 1
        """, (movie_title.strip(), year))
        if cur.fetchone():
            return True

        # Also check if it's currently in the pending queue downloading
        cur.execute(
            "SELECT 1 FROM pending_leech WHERE LOWER(movie_title) = LOWER(%s) LIMIT 1",
            (movie_title.strip(),),
        )
        return cur.fetchone() is not None
    except Exception:
        return False
    finally:
        cur.close()


def _save_movie_and_magnet(conn, movie: Dict, magnet: str, quality: str = "Best", file_size: str = "") -> bool:
    """
    Save movie metadata + magnet link to DB.
    1. INSERT INTO pending_leech (magnet link + tmdb_metadata for userbot to pick up)
    """
    cur = conn.cursor()
    try:
        title = movie.get("title", "Unknown").strip()
        movie_url = f"https://apibay.org/q.php?q={title.replace(' ', '+')}"

        # Ensure tmdb_metadata column exists
        try:
            cur.execute("ALTER TABLE pending_leech ADD COLUMN IF NOT EXISTS tmdb_metadata JSONB;")
            conn.commit()
        except Exception:
            conn.rollback()

        tmdb_metadata = json.dumps({
            "description": movie.get("description", ""),
            "year": movie.get("year", 0),
            "rating": movie.get("rating", "N/A"),
            "genre": movie.get("genre", ""),
            "poster_url": movie.get("poster_url", ""),
            "category": movie.get("category", "Movies"),
            "cast": movie.get("cast", ""),
            "language": movie.get("language", ""),
            "imdb_id": movie.get("imdb_id", ""),
            "movie_url": movie_url
        })

        # 2. Save magnet link to pending_leech
        filename = f"{title} [{quality}]"
        cur.execute("""
            INSERT INTO pending_leech (url, filename, file_size, movie_title, movie_id, quality, status, tmdb_metadata)
            VALUES (%s, %s, %s, %s, NULL, %s, 'pending', %s)
            ON CONFLICT (url) DO NOTHING
        """, (
            magnet,
            filename,
            file_size,
            title,
            quality,
            tmdb_metadata
        ))

        inserted = cur.rowcount > 0
        conn.commit()

        if inserted:
            log.info(f"  ✅ '{title}' → Magnet saved to pending_leech (delayed DB insert)")
        else:
            log.info(f"  ⏩ '{title}' → Magnet already exists in pending_leech")

        return inserted

    except Exception as e:
        conn.rollback()
        log.error(f"  ❌ DB save error for '{movie.get('title', '?')}': {e}")
        return False
    finally:
        cur.close()


# ════════════════════════════════════════════════════════════════════════════
# MAIN RUN
# ════════════════════════════════════════════════════════════════════════════

async def run_once(
    pages: int = 1,
    delay: float = 2.0,
    max_time_seconds: int = 300,
    max_new: int = None,
) -> dict:
    """
    Single scraping run:
      1. Fetch trending movies from TMDB
      2. For each, search 1337x for magnet link
      3. Enrich with TMDB metadata
      4. Save to DB (pending_leech)

    Args:
        pages: TMDB trending pages to fetch (20 movies/page)
        delay: Seconds between torrent searches (rate limiting)
        max_time_seconds: Max wall-clock time for this run
        max_new: Stop after saving this many NEW movies (None = unlimited)

    Returns:
        Stats dict: {"saved": N, "skipped": N, "failed": N}
    """
    # Lazy import — only loaded when actually scraping
    from tpb_scraper import search_tpb

    start_time = time.time()
    log.info(f"\n{'═' * 58}")
    log.info(f"  🚀 Auto Scraper started at {datetime.now().strftime('%d-%b-%Y %H:%M:%S')}")
    log.info(f"  Source: The Pirate Bay (TPB) | Discovery: TMDB Trending")
    log.info(f"  Pages: {pages} | Delay: {delay}s | Max New: {max_new or '∞'}")
    log.info(f"{'═' * 58}")

    stats = {"saved": 0, "skipped": 0, "failed": 0}

    # ── Step 1: Connect to DB and PRE-CHECK ──
    conn = get_conn()
    
    cur = conn.cursor()
    cur.execute("SELECT COUNT(*) FROM pending_leech WHERE status = 'pending'")
    count = cur.fetchone()[0]
    cur.close()
    
    if count > 20:
        log.info(f"  ⚠️ Queue is very full ({count} links). Skipping scraping to prevent overload.")
        close_conn(conn)
        return stats
    elif count > 0:
        log.info(f"  ℹ️ {count} links pending. Adding more to keep the pipeline moving...")

    # ── Step 2: Loop TMDB pages until we save at least 1 movie ──
    current_page = 1
    max_pages = max(pages, 5)  # Ensure we search up to 5 pages if needed
    
    while current_page <= max_pages and stats["saved"] == 0:
        log.info(f"\n  📡 Step 2: Fetching TMDB trending movies (Page {current_page})...")
        trending = await fetch_trending_movies(page=current_page)

        if not trending:
            log.warning(f"  ⚠️ No trending movies found on page {current_page}. Check TMDB_API_KEY.")
            break

        log.info(f"  📋 {len(trending)} trending movies fetched from TMDB page {current_page}")

        # ── Step 3: Process each movie ──
        for i, movie in enumerate(trending, 1):
            # Time limit check
            if time.time() - start_time > max_time_seconds:
                log.info(f"  ⏰ Time limit reached ({max_time_seconds}s). Stopping.")
                break

            # Max new limit check
            if max_new and stats["saved"] >= max_new:
                log.info(f"  ✅ Reached max_new limit ({max_new}). Stopping.")
                break

            title = movie["title"]
            year = movie["year"]

            log.info(f"\n  [{i}/{len(trending)}] 🎬 {title} ({year})")

            # Skip if already in DB
            if _is_already_scraped(conn, title, year):
                log.info(f"    ⏩ Already scraped, skipping.")
                stats["skipped"] += 1
                continue

            # ── Search TPB for magnet link ──
            # Build search query with year for better accuracy
            search_query = f"{title} {year}" if year else title
            log.info(f"    🔍 Searching TPB: '{search_query}'")

            try:
                tpb_results = await search_tpb(search_query)
            except Exception as e:
                log.error(f"    ❌ TPB search error: {e}")
                stats["failed"] += 1
                await asyncio.sleep(delay)
                continue

            if not tpb_results:
                log.info(f"    ⚠️ No magnet found on TPB for: {title}")
                stats["failed"] += 1
                await asyncio.sleep(delay)
                continue

            log.info(f"    🧲 Found {len(tpb_results)} qualities for '{title}'!")

            # ── Enrich with detailed TMDB metadata ──
            tmdb_id = movie.get("tmdb_id")
            if tmdb_id:
                detail = await enrich_tmdb_detail(tmdb_id)
                if detail:
                    movie["cast"] = detail.get("cast", movie.get("cast", ""))
                    movie["imdb_id"] = detail.get("imdb_id", "")
                    if detail.get("description"):
                        movie["description"] = detail["description"]

            # ── Save ALL qualities to DB ──
            saved_count = 0
            for res in tpb_results:
                # STRICT SIZE LIMIT: Skip if > 3.8GB (Leech bot limit)
                size_gb = res.get("size_gb", 0)
                if size_gb > 3.8:
                    log.debug(f"    ⏭️ Skipping '{title}' quality - Too large: {size_gb} GB")
                    continue

                quality = "1080p" # Default
                if size_gb < 1.2:
                    quality = "720p"
                elif size_gb < 0.6:
                    quality = "480p"
                elif size_gb > 2.5:
                    quality = "4K"
                    
                size_str = f"{size_gb} GB"
                saved = _save_movie_and_magnet(conn, movie, res['magnet_link'], quality=quality, file_size=size_str)
                if saved:
                    saved_count += 1
                    stats["saved"] += 1

            if saved_count > 0:
                log.info(f"  ✅ Movie Saved: Queued {saved_count} qualities.")
                # Removed "break" to allow multiple movies per run
            else:
                stats["skipped"] += 1

            # Periodic garbage collection (every 5 movies)
            if i % 5 == 0:
                gc.collect()

            # Rate limiting delay between torrent searches
            await asyncio.sleep(delay)

        if time.time() - start_time > max_time_seconds or (max_new and stats["saved"] >= max_new):
            break
            
        if stats["saved"] > 0:
            log.info("  ✅ Successfully saved movies. Stopping pagination.")
            break
            
        current_page += 1

    close_conn(conn)

    log.info(f"\n{'═' * 58}")
    log.info(f"  ✅ Saved: {stats['saved']}  ❌ Failed: {stats['failed']}  ⏭️ Skipped: {stats['skipped']}")
    log.info(f"  ⏱️ Duration: {int(time.time() - start_time)}s")
    log.info(f"{'═' * 58}\n")

    # Final GC
    gc.collect()

    return stats


async def run_loop(pages: int, delay: float, interval_min: int):
    """Infinite loop: run_once → sleep → repeat."""
    while True:
        try:
            await run_once(pages=pages, delay=delay)
        except Exception as e:
            log.error(f"Run failed: {e}")

        gc.collect()

        log.info(f"⏰ Next run in {interval_min} minutes...")
        await asyncio.sleep(interval_min * 60)


# ════════════════════════════════════════════════════════════════════════════
# ENTRY POINT
# ════════════════════════════════════════════════════════════════════════════

if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="Auto Torrent Scraper (1337x + TMDB)")
    ap.add_argument("--pages", type=int, default=1, help="TMDB trending pages (20 movies/page)")
    ap.add_argument("--delay", type=float, default=3.0, help="Delay between torrent searches (seconds)")
    ap.add_argument("--interval", type=int, default=120, help="Loop interval in minutes")
    ap.add_argument("--max-new", type=int, default=5, help="Max new movies to save per run")
    ap.add_argument("--once", action="store_true", help="Run once and exit")
    args = ap.parse_args()

    banner = f"""
╔══════════════════════════════════════════╗
║     🎬  Auto Torrent Scraper             ║
╠══════════════════════════════════════════╣
║  Source   : The Pirate Bay (TPB)        ║
║  Discovery: TMDB Trending API           ║
║  Pages    : {args.pages:<30}║
║  Delay    : {args.delay}s{' ' * 28}║
║  Interval : {'Once only' if args.once else f'Every {args.interval} min':<29}║
║  Max New  : {args.max_new or '∞':<30}║
║  TMDB Key : {'✅' if TMDB_KEY else '❌ Not set':<29}║
╚══════════════════════════════════════════╝
"""
    print(banner)

    if not TMDB_KEY:
        print("⚠️  WARNING: TMDB_API_KEY not set. Auto-discovery won't work.")
        print("   Get a free key at: https://www.themoviedb.org/settings/api\n")

    if args.once:
        asyncio.run(run_once(pages=args.pages, delay=args.delay, max_new=args.max_new))
    else:
        asyncio.run(run_loop(pages=args.pages, delay=args.delay, interval_min=args.interval))
