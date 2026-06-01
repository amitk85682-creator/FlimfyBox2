"""
Torrent Magnet Link Scraper — 1337x (x1337x.ws)
━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━
Extracts magnet links from 1337x torrent site with Cloudflare bypass.

Architecture: 3-Layer Fallback
  Layer 1: curl_cffi   — TLS fingerprint spoofing (~5MB RAM)
  Layer 2: cloudscraper — JS challenge solver (~8MB RAM)
  Layer 3: Playwright   — Full headless browser, resource-blocked (~80MB RAM)

Optimized for Render Free Tier (512MB RAM strict limit).

Usage:
  from torrent_scraper import get_magnet_link
  magnet = await get_magnet_link("Inception 2010")
"""

import re
import gc
import logging
import asyncio
from typing import Optional, List, Dict
from urllib.parse import quote

log = logging.getLogger("TorrentScraper")

# ── Constants ────────────────────────────────────────────────────────────────
BASE_URL = "https://x1337x.ws"
SEARCH_URL = BASE_URL + "/search/{query}/{page}/"

# Realistic browser headers for all HTTP-based layers
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;"
        "q=0.9,image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}


# ════════════════════════════════════════════════════════════════════════════
# LAYER 1: curl_cffi (Lightest — TLS fingerprint spoofing)
# ════════════════════════════════════════════════════════════════════════════

async def _fetch_curl_cffi(url: str, timeout: int = 20) -> Optional[str]:
    """
    Uses curl_cffi to make requests with a Chrome-impersonated TLS fingerprint.
    This fools Cloudflare's JA3/JA4 fingerprint checks without running JS.
    RAM: ~5MB overhead — the lightest possible bypass.
    """
    try:
        from curl_cffi.requests import AsyncSession

        async with AsyncSession(impersonate="chrome124", timeout=30) as session:
            response = await session.get(
                url,
                headers=HEADERS,
                timeout=timeout,
                allow_redirects=True,
            )

            # Cloudflare block detection: look for challenge markers
            if response.status_code == 403:
                log.warning(f"[curl_cffi] 403 Forbidden — Cloudflare blocked: {url[:60]}")
                return None
            if response.status_code != 200:
                log.warning(f"[curl_cffi] HTTP {response.status_code}: {url[:60]}")
                return None

            html = response.text

            # Detect Cloudflare challenge page (JS challenge, not actual content)
            if _is_cloudflare_challenge(html):
                log.warning("[curl_cffi] Cloudflare JS challenge detected — escalating.")
                return None

            return html

    except ImportError:
        log.warning("[curl_cffi] Not installed. Skipping Layer 1.")
        return None
    except Exception as e:
        log.error(f"[curl_cffi] Error: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# LAYER 2: cloudscraper (Medium — solves basic JS challenges)
# ════════════════════════════════════════════════════════════════════════════

async def _fetch_cloudscraper(url: str, timeout: int = 20) -> Optional[str]:
    """
    Uses cloudscraper which solves Cloudflare's "I'm Under Attack" JS challenges.
    It rewrites and evaluates the challenge JS locally — no browser needed.
    RAM: ~8MB overhead.
    Runs in a thread executor since cloudscraper is synchronous.
    """
    try:
        import cloudscraper

        def _sync_fetch():
            scraper = cloudscraper.create_scraper(
                browser={
                    "browser": "chrome",
                    "platform": "windows",
                    "desktop": True,
                },
                delay=5,  # Wait 5s for Cloudflare's "checking your browser" timer
            )
            response = scraper.get(url, headers=HEADERS, timeout=timeout)
            if response.status_code != 200:
                return None
            html = response.text
            if _is_cloudflare_challenge(html):
                return None
            return html

        # Run synchronous cloudscraper in a thread to avoid blocking the event loop
        loop = asyncio.get_event_loop()
        html = await loop.run_in_executor(None, _sync_fetch)
        return html

    except ImportError:
        log.warning("[cloudscraper] Not installed. Skipping Layer 2.")
        return None
    except Exception as e:
        log.error(f"[cloudscraper] Error: {e}")
        return None


# ════════════════════════════════════════════════════════════════════════════
# LAYER 3: Playwright (Heaviest — real browser, last resort)
# ════════════════════════════════════════════════════════════════════════════

async def _fetch_playwright(url: str, timeout: int = 30000) -> Optional[str]:
    """
    Full Playwright headless browser with stealth patches.
    STRICT memory optimization:
      - Blocks ALL images, CSS, fonts, media, and non-essential resources
      - Uses a single browser context, closed immediately after use
      - Runs with --disable-gpu, --no-sandbox, reduced memory args
    RAM: ~80-120MB, but freed immediately after use.
    """
    browser = None
    try:
        from playwright.async_api import async_playwright

        # Try importing stealth plugin for undetected navigation
        try:
            from playwright_stealth import stealth_async
            has_stealth = True
        except ImportError:
            has_stealth = False
            log.warning("[Playwright] playwright-stealth not installed — running without stealth.")

        pw = await async_playwright().start()

        browser = await pw.chromium.launch(
            headless=True,
            args=[
                "--disable-gpu",
                "--disable-dev-shm-usage",  # Prevents /dev/shm overflow on low-RAM
                "--no-sandbox",
                "--disable-setuid-sandbox",
                "--disable-extensions",
                "--disable-background-networking",
                "--disable-sync",
                "--disable-translate",
                "--metrics-recording-only",
                "--mute-audio",
                "--no-first-run",
                "--safebrowsing-disable-auto-update",
                "--single-process",  # Reduces memory by running in one process
                "--js-flags=--max-old-space-size=128",  # Limit V8 heap
            ],
        )

        context = await browser.new_context(
            user_agent=HEADERS["User-Agent"],
            viewport={"width": 1280, "height": 720},
            locale="en-US",
        )

        page = await context.new_page()

        # Apply stealth patches if available
        if has_stealth:
            await stealth_async(page)

        # ── BLOCK ALL non-HTML resources to save RAM ──
        async def block_resources(route, request):
            blocked_types = {"image", "stylesheet", "font", "media", "websocket", "other"}
            if request.resource_type in blocked_types:
                await route.abort()
            else:
                await route.continue_()

        await page.route("**/*", block_resources)

        # Also block known heavy domains (analytics, ads, trackers)
        heavy_domains = [
            "*google-analytics*", "*doubleclick*", "*facebook*",
            "*googlesyndication*", "*googletagmanager*",
            "*.png", "*.jpg", "*.jpeg", "*.gif", "*.webp",
            "*.css", "*.woff*", "*.ttf", "*.svg",
        ]
        for pattern in heavy_domains:
            await page.route(pattern, lambda route: route.abort())

        # Navigate and wait for Cloudflare to auto-resolve
        await page.goto(url, wait_until="domcontentloaded", timeout=timeout)

        # Wait for Cloudflare challenge to auto-complete (up to 15s)
        for _ in range(15):
            content = await page.content()
            if not _is_cloudflare_challenge(content):
                break
            await asyncio.sleep(1)

        html = await page.content()

        # Aggressive cleanup
        await page.close()
        await context.close()
        await browser.close()
        await pw.stop()
        browser = None

        if _is_cloudflare_challenge(html):
            log.error("[Playwright] Cloudflare challenge could not be bypassed.")
            return None

        return html

    except ImportError:
        log.warning("[Playwright] Not installed. Layer 3 unavailable.")
        return None
    except Exception as e:
        log.error(f"[Playwright] Error: {e}")
        return None
    finally:
        # Ensure browser is always closed to free RAM
        if browser:
            try:
                await browser.close()
            except Exception:
                pass
        gc.collect()


# ════════════════════════════════════════════════════════════════════════════
# UNIFIED FETCH — Cascading Fallback
# ════════════════════════════════════════════════════════════════════════════

async def _fetch_page(url: str) -> Optional[str]:
    """
    Master fetch function with 3-layer cascade:
      1. curl_cffi  (fastest, lightest)
      2. cloudscraper (medium)
      3. Playwright (last resort, heaviest)
    Returns raw HTML string or None.
    """
    log.info(f"🌐 Fetching: {url[:80]}")

    # Layer 1: curl_cffi
    log.debug("[Fetch] Trying Layer 1: curl_cffi...")
    html = await _fetch_curl_cffi(url)
    if html:
        log.info("✅ Layer 1 (curl_cffi) succeeded.")
        return html

    # Layer 2: cloudscraper
    log.info("[Fetch] Layer 1 failed. Trying Layer 2: cloudscraper...")
    html = await _fetch_cloudscraper(url)
    if html:
        log.info("✅ Layer 2 (cloudscraper) succeeded.")
        return html

    # Layer 3: Playwright (heavy)
    log.warning("[Fetch] Layer 2 failed. Trying Layer 3: Playwright (heavy)...")
    html = await _fetch_playwright(url)
    if html:
        log.info("✅ Layer 3 (Playwright) succeeded.")
        return html

    log.error(f"❌ All 3 layers failed for: {url[:80]}")
    return None


# ════════════════════════════════════════════════════════════════════════════
# CLOUDFLARE DETECTION
# ════════════════════════════════════════════════════════════════════════════

def _is_cloudflare_challenge(html: str) -> bool:
    """Detects if the HTML is a Cloudflare challenge page, not real content."""
    if not html:
        return True
    cf_markers = [
        "Performing security verification",
        "cf-challenge-running",
        "Just a moment...",
        "challenge-platform",
        "cf_chl_opt",
        "turnstile",
        "Checking if the site connection is secure",
        "_cf_chl",
    ]
    # Check for multiple markers (single marker could be a false positive)
    hits = sum(1 for marker in cf_markers if marker.lower() in html.lower())
    return hits >= 2


# ════════════════════════════════════════════════════════════════════════════
# HTML PARSING — Search Results
# ════════════════════════════════════════════════════════════════════════════

def _parse_search_results(html: str) -> List[Dict]:
    """
    Parses the 1337x search results page.

    DOM Structure:
      <tbody>
        <tr>
          <td class="coll-1 name">
            <a href="/sub/.../">icon</a>
            <a href="/torrent/12345/Movie-Name/">Movie Title</a>  ← We want this
          </td>
          <td class="coll-2 seeds">150</td>     ← Seeders
          <td class="coll-3 leeches">20</td>    ← Leechers
          <td class="coll-date">Oct. 5th '23</td>
          <td class="coll-4 size mob-uploader">1.4 GB</td>  ← Size
          <td class="coll-5 uploader mob-user">
        </tr>
      </tbody>

    Returns list of dicts: [{name, url, seeds, leeches, size}, ...]
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")
    results = []

    tbody = soup.find("tbody")
    if not tbody:
        log.warning("No <tbody> found — possibly no results or blocked page.")
        return results

    for tr in tbody.find_all("tr"):
        try:
            # Title & URL: second <a> inside td.coll-1
            td_name = tr.find("td", class_=re.compile(r"coll-1\s+name"))
            if not td_name:
                continue

            links = td_name.find_all("a")
            if len(links) < 2:
                continue

            # The second <a> has the actual torrent detail link
            detail_link = links[1]
            name = detail_link.get_text(strip=True)
            href = detail_link.get("href", "")

            # Seeders
            td_seeds = tr.find("td", class_=re.compile(r"coll-2|seeds"))
            seeds = 0
            if td_seeds:
                try:
                    seeds = int(td_seeds.get_text(strip=True).replace(",", ""))
                except ValueError:
                    seeds = 0

            # Leechers
            td_leeches = tr.find("td", class_=re.compile(r"coll-3|leeches"))
            leeches = 0
            if td_leeches:
                try:
                    leeches = int(td_leeches.get_text(strip=True).replace(",", ""))
                except ValueError:
                    leeches = 0

            # Size
            td_size = tr.find("td", class_=re.compile(r"coll-4|size"))
            size = ""
            if td_size:
                # Size text often has a nested <span> with secondary size,
                # just grab the first text node
                size_text = td_size.get_text(separator=" ", strip=True)
                # Clean up — "1.4 GB1.4 GB" → "1.4 GB"
                size_match = re.match(r"([\d.]+\s*[KMGT]?B)", size_text, re.I)
                size = size_match.group(1) if size_match else size_text

            # Build full URL
            full_url = BASE_URL + href if href.startswith("/") else href

            results.append({
                "name": name,
                "url": full_url,
                "seeds": seeds,
                "leeches": leeches,
                "size": size,
            })

        except Exception as e:
            log.debug(f"Error parsing row: {e}")
            continue

    # Sort by seeders (descending) — healthiest torrents first
    results.sort(key=lambda x: x["seeds"], reverse=True)

    log.info(f"📋 Found {len(results)} results, top seeded: "
             f"{results[0]['name'][:50] if results else 'N/A'} "
             f"({results[0]['seeds']}S)" if results else "📋 No results found.")
    return results


# ════════════════════════════════════════════════════════════════════════════
# HTML PARSING — Detail Page (Magnet Extraction)
# ════════════════════════════════════════════════════════════════════════════

def _extract_magnet_from_detail(html: str) -> Optional[str]:
    """
    Extracts the magnet link from a torrent detail page.

    DOM Structure:
      <a href="magnet:?xt=urn:btih:HASH&dn=Name&tr=tracker..."
         class="..."
         onclick="...">Magnet Download</a>

    We look for any <a> tag where href starts with 'magnet:?xt=urn:btih:'.
    """
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, "html.parser")

    # Method 1: Direct <a> tag search
    magnet_tag = soup.find("a", href=re.compile(r"^magnet:\?xt=urn:btih:", re.I))
    if magnet_tag:
        magnet = magnet_tag["href"]
        log.info(f"🧲 Magnet found (len={len(magnet)}, hash={magnet[20:60]}...)")
        return magnet

    # Method 2: Regex fallback on raw HTML (in case BS4 misses it)
    magnet_match = re.search(r'href=["\']?(magnet:\?xt=urn:btih:[^"\'\s>]+)', html, re.I)
    if magnet_match:
        magnet = magnet_match.group(1)
        log.info(f"🧲 Magnet found via regex (len={len(magnet)})")
        return magnet

    log.warning("⚠️ No magnet link found on detail page.")
    return None


# ════════════════════════════════════════════════════════════════════════════
# PUBLIC API
# ════════════════════════════════════════════════════════════════════════════

async def search_torrents(
    movie_name: str,
    page: int = 1,
) -> List[Dict]:
    """
    Search 1337x for a movie/show name and return parsed results.

    Args:
        movie_name: The search query (e.g. "Inception 2010")
        page: Results page number (default 1)

    Returns:
        List of dicts with keys: name, url, seeds, leeches, size
        Sorted by seeders (descending).
    """
    # URL-encode the movie name for the search path
    query = quote(movie_name, safe="")
    search_url = SEARCH_URL.format(query=query, page=page)

    html = await _fetch_page(search_url)
    if not html:
        log.error(f"Failed to fetch search page for: {movie_name}")
        return []

    results = _parse_search_results(html)
    return results


async def get_magnet_from_url(torrent_url: str) -> Optional[str]:
    """
    Fetch a specific torrent detail page and extract the magnet link.

    Args:
        torrent_url: Full URL to the torrent detail page.

    Returns:
        Magnet link string, or None if extraction fails.
    """
    html = await _fetch_page(torrent_url)
    if not html:
        log.error(f"Failed to fetch detail page: {torrent_url[:60]}")
        return None

    magnet = _extract_magnet_from_detail(html)

    # Force garbage collection after processing large HTML
    del html
    gc.collect()

    return magnet


async def get_magnet_link(
    movie_name: str,
    min_seeds: int = 0,
    preferred_quality: Optional[str] = None,
    max_size_gb: Optional[float] = None,
    result_index: int = 0,
) -> Optional[str]:
    """
    Main entry point: Search 1337x for a movie and return the magnet link.

    Flow:
      1. Search 1337x for `movie_name`
      2. Filter results by criteria (seeds, quality, size)
      3. Pick the best match (or `result_index`-th result)
      4. Fetch the detail page
      5. Extract and return the magnet link

    Args:
        movie_name:        Search query (e.g. "Oppenheimer 2023 1080p")
        min_seeds:         Minimum seeders threshold (default: 0)
        preferred_quality: Quality filter keyword, e.g. "1080p", "720p", "2160p"
        max_size_gb:       Maximum torrent size in GB (e.g. 2.5)
        result_index:      Which result to pick (0 = first/best, after filtering)

    Returns:
        Magnet link string, or None if no suitable torrent found.
    """
    log.info(f"🎬 Searching 1337x for: '{movie_name}'")

    try:
        # Step 1: Search
        results = await search_torrents(movie_name)
        if not results:
            log.warning(f"No results found for: {movie_name}")
            return None

        # Step 2: Filter by criteria
        filtered = results

        # Filter: minimum seeders
        if min_seeds > 0:
            filtered = [r for r in filtered if r["seeds"] >= min_seeds]
            if not filtered:
                log.warning(f"No results with >= {min_seeds} seeds. "
                            f"Best available: {results[0]['seeds']} seeds.")
                filtered = results  # Fall back to unfiltered

        # Filter: preferred quality (e.g. "1080p")
        if preferred_quality:
            quality_filtered = [
                r for r in filtered
                if preferred_quality.lower() in r["name"].lower()
            ]
            if quality_filtered:
                filtered = quality_filtered
            else:
                log.info(f"No results matching quality '{preferred_quality}'. "
                         f"Using best available.")

        # Filter: max size
        if max_size_gb is not None:
            size_filtered = []
            for r in filtered:
                size_gb = _parse_size_to_gb(r.get("size", ""))
                if size_gb is not None and size_gb <= max_size_gb:
                    size_filtered.append(r)
            if size_filtered:
                filtered = size_filtered
            else:
                log.info(f"No results under {max_size_gb}GB. Using best available.")

        # Step 3: Pick result
        if result_index >= len(filtered):
            log.warning(f"result_index={result_index} out of range "
                        f"(only {len(filtered)} results). Using index 0.")
            result_index = 0

        chosen = filtered[result_index]
        log.info(f"🎯 Selected: '{chosen['name'][:60]}' "
                 f"| Seeds: {chosen['seeds']} | Size: {chosen['size']}")

        # Step 4 & 5: Fetch detail page and extract magnet
        magnet = await get_magnet_from_url(chosen["url"])

        if magnet:
            log.info(f"✅ Magnet link extracted successfully for: {movie_name}")
        else:
            log.error(f"❌ Failed to extract magnet from detail page: {chosen['url']}")

        return magnet

    except Exception as e:
        log.error(f"❌ get_magnet_link() error: {e}", exc_info=True)
        return None
    finally:
        gc.collect()


# ════════════════════════════════════════════════════════════════════════════
# UTILITIES
# ════════════════════════════════════════════════════════════════════════════

def _parse_size_to_gb(size_str: str) -> Optional[float]:
    """Parses size strings like '1.4 GB', '850 MB', '4.2 TB' into GB float."""
    if not size_str:
        return None
    match = re.match(r"([\d.]+)\s*(KB|MB|GB|TB)", size_str.strip(), re.I)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).upper()
    multipliers = {"KB": 1 / (1024 * 1024), "MB": 1 / 1024, "GB": 1, "TB": 1024}
    return value * multipliers.get(unit, 1)


# ════════════════════════════════════════════════════════════════════════════
# STANDALONE TEST
# ════════════════════════════════════════════════════════════════════════════

async def _test():
    """Quick standalone test."""
    logging.basicConfig(
        level=logging.DEBUG,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    movie = "Inception 2010"
    print(f"\n{'═' * 60}")
    print(f"  🧪 Testing Torrent Scraper: '{movie}'")
    print(f"{'═' * 60}\n")

    # Test search
    print("── Step 1: Search ──")
    results = await search_torrents(movie)
    if results:
        for i, r in enumerate(results[:5]):
            print(f"  [{i}] {r['name'][:55]}  |  S:{r['seeds']}  L:{r['leeches']}  |  {r['size']}")
    else:
        print("  ❌ No results found!")
        return

    # Test magnet extraction
    print(f"\n── Step 2: Extract Magnet (from result #0) ──")
    magnet = await get_magnet_link(movie)
    if magnet:
        print(f"  ✅ Magnet: {magnet[:80]}...")
    else:
        print("  ❌ Failed to extract magnet link.")

    print(f"\n{'═' * 60}")


if __name__ == "__main__":
    asyncio.run(_test())
