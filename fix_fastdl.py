"""
Clean fix: Simplify the anti-bot interception.
- Remove resolve_fastdl() form-submission logic (doesn't work with Turnstile)
- Simplify resolve_provider_link() to just: fetch_with_antibot -> parse -> extract link
- In extract_gdirect_movie/series: ALL provider URLs go through resolve_provider_link
  (no separate fast-dl path)
- Add fast-dl back to ANTIBOT_DOMAINS_RE
"""
with open('movie_scraper.py', 'r', encoding='utf-8') as f:
    content = f.read()

changes = 0

# 1. Fix ANTIBOT_DOMAINS_RE: add fast-dl back
old = 'r"(filepress|vcloud|dropgalaxy|hubcloud|gdflix)"'
new = 'r"(filepress|vcloud|dropgalaxy|hubcloud|gdflix|fast-dl)"'
if old in content:
    content = content.replace(old, new, 1)
    changes += 1
    print("1. Added fast-dl back to ANTIBOT_DOMAINS_RE")
else:
    # Maybe it already has fast-dl
    if 'fast-dl' in content.split('ANTIBOT_DOMAINS_RE')[1][:100]:
        print("1. SKIP: fast-dl already in ANTIBOT_DOMAINS_RE")
        changes += 1

# 2. Fix extract_gdirect_movie: remove fast-dl special case, just use resolve_provider_link
old_movie = '''    # Resolve fast-dl links via dedicated resolver (no API credits needed)
    if "fast-dl" in gdirect_url:
        return resolve_fastdl(gdirect_url)
    # Resolve other provider links via Anti-Bot APIs
    if resolve:
        return resolve_provider_link(gdirect_url)
    return gdirect_url'''
new_movie = '''    # Resolve provider link via Anti-Bot APIs (ZenRows/ScraperAPI)
    if resolve:
        return resolve_provider_link(gdirect_url)
    return gdirect_url'''
if old_movie in content:
    content = content.replace(old_movie, new_movie, 1)
    changes += 1
    print("2. Fixed extract_gdirect_movie: removed fast-dl special case")

# 3. Fix extract_gdirect_series: remove fast-dl special case
old_series = '''                # Resolve fast-dl via dedicated resolver, others via Anti-Bot
                if "fast-dl" in href:
                    final_url = resolve_fastdl(href)
                elif resolve:
                    final_url = resolve_provider_link(href)
                    time.sleep(1)'''
new_series = '''                # Resolve provider link via Anti-Bot APIs
                if resolve:
                    final_url = resolve_provider_link(href)
                    time.sleep(1)'''
if old_series in content:
    content = content.replace(old_series, new_series, 1)
    changes += 1
    print("3. Fixed extract_gdirect_series: removed fast-dl special case")

with open('movie_scraper.py', 'w', encoding='utf-8') as f:
    f.write(content)

print(f"\nDONE! Applied {changes}/3 changes.")
print("\nNow the flow is simple:")
print("  1. cloudscraper handles dotmovies + nexdrive (FREE)")
print("  2. extract_gdirect finds the provider URL")
print("  3. resolve_provider_link calls fetch_with_antibot(url)")
print("  4. ZenRows renders the page, returns HTML")
print("  5. BeautifulSoup parses HTML, extracts .mkv/.mp4 link")
print("  No form submission. No special cases. Just one clean interception.")
