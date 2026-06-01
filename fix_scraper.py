"""Fix corrupted lines in movie_scraper.py"""
import re

with open('movie_scraper.py', 'r', encoding='utf-8') as f:
    content = f.read()

# Find and replace the corrupted resolve_provider_link function
# Locate from "def resolve_provider_link" to the next section header
old_func_pattern = r'def resolve_provider_link\(provider_url: str\).*?(?=\n# [═])'
 
new_func = '''def resolve_provider_link(provider_url: str) -> str:
    """
    Resolve a provider URL (filepress.app, vcloud.ac, dropgalaxy.life, fast-dl.org)
    to the final direct download link using Anti-Bot APIs.

    Uses fetch_with_antibot() -> ZenRows (primary) -> ScraperAPI (fallback)
    for Cloudflare Turnstile bypass.

    Falls back to original URL if resolution fails.
    """
    if not provider_url:
        return provider_url

    # Only use anti-bot for known provider domains with Cloudflare Turnstile
    if not ANTIBOT_DOMAINS_RE.search(provider_url):
        log.info(f"      INFO: Non-provider domain, skipping anti-bot: {provider_url[:60]}")
        return provider_url

    log.info(f"      ANTIBOT: Resolving provider link: {provider_url[:60]}")

    html = fetch_with_antibot(provider_url)
    if not html:
        log.warning(f"      FALLBACK: Anti-bot fetch failed, returning original URL")
        return provider_url

    soup = BeautifulSoup(html, "html.parser")

    # Strategy 1: <a id="vd"> tag (fast-dl.org pattern)
    vd_tag = soup.find("a", id="vd")
    if vd_tag and vd_tag.get("href", "").startswith("http"):
        direct = vd_tag["href"].strip()
        log.info(f"      OK: Direct link (vd tag): {direct[:60]}")
        return direct

    # Strategy 2: Direct file links (.mkv, .mp4, etc.)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if re.search(r"\\.(mkv|mp4|avi|zip|rar)", href, re.I):
            log.info(f"      OK: Direct file link: {href[:60]}")
            return href

    # Strategy 3: CDN links (googleusercontent, googleapis, etc.)
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if re.search(r"(googleusercontent|googleapis|drive\\.google|cdn)", href, re.I):
            log.info(f"      OK: CDN link: {href[:60]}")
            return href

    # Strategy 4: Download button with substantial URL
    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        text = a.get_text(" ", strip=True).lower()
        if ("download" in text and href.startswith("http") and len(href) > 30
                and not SKIP_RE.search(href)):
            log.info(f"      OK: Download button link: {href[:60]}")
            return href

    # Strategy 5: JavaScript-embedded URLs
    for script in soup.find_all("script"):
        js = script.string or ""
        for m in re.finditer(r"https?://\\S{20,}", js):
            found_url = m.group(0).rstrip("\\'\\\";,)")
            if re.search(r"(googleusercontent|googleapis|cdn|\\.mkv|\\.mp4)", found_url, re.I):
                log.info(f"      OK: URL from script: {found_url[:60]}")
                return found_url

    log.warning(f"      WARN: Could not extract direct link, returning original URL")
    return provider_url

'''

match = re.search(old_func_pattern, content, re.DOTALL)
if match:
    content = content[:match.start()] + new_func + content[match.end():]
    with open('movie_scraper.py', 'w', encoding='utf-8') as f:
        f.write(content)
    print("SUCCESS: resolve_provider_link function replaced cleanly")
else:
    print("ERROR: Could not find the function pattern")
    # Debug: show what's around that area
    idx = content.find('resolve_provider_link')
    if idx >= 0:
        print(f"Found at index {idx}")
        snippet = content[idx:idx+200]
        print(f"Snippet: {repr(snippet[:200])}")
    else:
        print("Function name not found at all!")
