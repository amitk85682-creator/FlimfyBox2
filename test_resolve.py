"""
Quick test: resolve_redirection ko test karo
hblinks → HubCloud Page 4 → Page 5 final download links
"""
import re
import requests
from bs4 import BeautifulSoup
from urllib.parse import urlparse

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}
SESSION = requests.Session()
SESSION.headers.update(HEADERS)

_SKIP_DOMAINS = re.compile(r"(t\.me|telegram|youtube|tutorial|how[-.]to)", re.I)
_FINAL_BTN_RE = re.compile(
    r"(FSLv?2?\s*Server|FSL\s*Server|ZipDisk|PixelServer\s*:?\s*\d*|"
    r"Download\s*\[|Direct\s*Download|High\s*Speed|Fast\s*Server|"
    r"Server\s*:\s*10|Mirror\s*\d*|Server\s*\d+)",
    re.I,
)

def _abs(href, base):
    if href.startswith("http"): return href
    p = urlparse(base)
    return f"{p.scheme}://{p.netloc}{href}" if href.startswith("/") else f"{p.scheme}://{p.netloc}/{href}"


# ── TEST 1: Fetch HubCloud Page 4 and see what we get ──
print("=" * 60)
print("TEST: HubCloud Page 4 → Page 5 flow")
print("=" * 60)

# This is a known HubCloud URL from the browser test
hubcloud_url = "https://hubcloud.foo/drive/iijtchhamiii5u3"

print(f"\n[Step 1] Fetching Page 4: {hubcloud_url}")
r = SESSION.get(hubcloud_url, timeout=20, allow_redirects=True)
print(f"  Status: {r.status_code}")
print(f"  Final URL: {r.url}")

soup = BeautifulSoup(r.text, "html.parser")

# Check for "Generate Direct Download Link" button
gen_btn = soup.find("a", string=re.compile(r"Generate Direct Download Link", re.I))
print(f"\n[Step 2] 'Generate Direct Download Link' button: {'FOUND' if gen_btn else 'NOT FOUND'}")
if gen_btn:
    print(f"  href: {gen_btn.get('href', 'NO HREF')}")
    print(f"  class: {gen_btn.get('class', 'NO CLASS')}")

# Also check btn-success
btn_success = soup.find("a", class_=re.compile(r"btn-success|btn-primary", re.I))
print(f"\n[Step 3] btn-success/btn-primary: {'FOUND' if btn_success else 'NOT FOUND'}")
if btn_success:
    print(f"  text: {btn_success.get_text(strip=True)}")
    print(f"  href: {btn_success.get('href', 'NO HREF')}")

# Check all links on page
print(f"\n[Step 4] All links on page:")
for a in soup.find_all("a", href=True):
    text = a.get_text(strip=True)[:50]
    href = a.get("href", "")[:80]
    if text:
        print(f"  [{text}] → {href}")

# Check script tags for tokens
print(f"\n[Step 5] Script tags scan:")
for script in soup.find_all("script"):
    js = script.string or ""
    if "token" in js.lower() or "download" in js.lower():
        print(f"  Script with token/download found: {js[:200]}")

# Check forms
print(f"\n[Step 6] Forms on page:")
for form in soup.find_all("form"):
    print(f"  action: {form.get('action', 'NONE')}")
    for inp in form.find_all("input"):
        print(f"    input name={inp.get('name')} value={inp.get('value', '')[:50]}")

# ── If Generate button found, click it ──
if gen_btn and gen_btn.get("href"):
    next_url = _abs(gen_btn["href"], r.url)
    print(f"\n{'=' * 60}")
    print(f"[Step 7] Following Generate button → {next_url}")
    print(f"{'=' * 60}")
    
    r2 = SESSION.get(next_url, timeout=20, allow_redirects=True)
    print(f"  Status: {r2.status_code}")
    print(f"  Final URL: {r2.url}")
    
    soup2 = BeautifulSoup(r2.text, "html.parser")
    
    # Parse Page 5 buttons
    print(f"\n[Step 8] Page 5 download buttons:")
    found_any = False
    for btn in soup2.find_all("a", href=True):
        href = btn["href"].strip()
        if not href or href == "#" or _SKIP_DOMAINS.search(href):
            continue
        btn_text = btn.get_text(" ", strip=True)
        if _FINAL_BTN_RE.search(btn_text):
            found_any = True
            print(f"  ✅ [{btn_text}]")
            print(f"     → {href[:100]}")
    
    if not found_any:
        print("  ❌ No download buttons found!")
        print(f"\n  All links on Page 5:")
        for a in soup2.find_all("a", href=True):
            text = a.get_text(strip=True)[:60]
            href = a.get("href", "")[:80]
            if text:
                print(f"    [{text}] → {href}")

elif btn_success and btn_success.get("href"):
    next_url = _abs(btn_success["href"], r.url)
    print(f"\n[Step 7b] Following btn-success → {next_url}")
    r2 = SESSION.get(next_url, timeout=20, allow_redirects=True)
    print(f"  Status: {r2.status_code}")
    print(f"  Final URL: {r2.url}")
else:
    print("\n❌ No generate button found at all!")

print("\n" + "=" * 60)
print("TEST COMPLETE")
print("=" * 60)
