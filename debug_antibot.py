"""Debug: Test ZenRows with wait_for parameter for fast-dl.org"""
import os
from antibot_fetcher import fetch_with_antibot

os.environ.setdefault("ZENROWS_API_KEY", "e26c0797421fa6811b7e7da578c67e7c665690b3")
os.environ.setdefault("SCRAPERAPI_KEY", "ab7dc55da9cd62918f8888deb038f3a1")

url = "https://fast-dl.org/dl/a9926f"
print(f"Fetching with wait_for='a#vd': {url}")
print("This will wait up to 15s for Turnstile to solve...\n")

html = fetch_with_antibot(url, wait=15000, wait_for="a#vd")
if html:
    from bs4 import BeautifulSoup
    import re
    soup = BeautifulSoup(html, "html.parser")
    
    print(f"TITLE: {soup.title.string if soup.title else 'None'}")
    
    # Check for vd tag (the download link)
    vd = soup.find("a", id="vd")
    if vd:
        print(f"\nSUCCESS! <a id='vd'> found!")
        print(f"  href = {vd.get('href', 'none')[:100]}")
    else:
        print("\n<a id='vd'> NOT found. Checking what we got...")
        # Check forms
        forms = soup.find_all("form")
        print(f"  Forms: {len(forms)}")
        # Check all links
        for a in soup.find_all("a", href=True):
            print(f"  Link: {a['href'][:100]}  text={a.get_text(strip=True)[:30]}")
        # Check for Turnstile
        cf = soup.find(attrs={"class": re.compile(r"cf-turnstile|turnstile", re.I)})
        print(f"  Turnstile: {'STILL PRESENT' if cf else 'NOT FOUND'}")
        
    with open("debug_fastdl_v2.html", "w", encoding="utf-8") as f:
        f.write(html)
    print(f"\nSaved {len(html)} chars to debug_fastdl_v2.html")
else:
    print("FAILED to fetch")
