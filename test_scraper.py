"""
Quick test — DotMovies scraper test
Ye script The Boys ki files nikalne ki koshish karega.
"""
import sys
sys.path.insert(0, ".")

from movie_scraper import (
    scrape_detail, crawl_series_links,
    crawl_movie_links, get_listing_links, safe_get,
    extract_quality_nexdrive_urls, extract_gdirect_series,
    is_series_page, SESSION, log
)

def test_listing():
    """Test: homepage se movie links nikalo"""
    print("\n" + "=" * 60)
    print("TEST 1: Listing Page")
    print("=" * 60)
    links = get_listing_links(page=1)
    print(f"Found {len(links)} links on page 1:")
    for i, link in enumerate(links[:5], 1):
        print(f"  {i}. {link}")
    if len(links) > 5:
        print(f"  ... and {len(links) - 5} more")
    return links


def test_the_boys():
    """Test: The Boys series ki files nikalo"""
    print("\n" + "=" * 60)
    print("TEST 2: The Boys Series (G-Direct extraction)")
    print("=" * 60)
    
    # Step 1: Search for The Boys
    # Use the first listing page link that has "the-boys" in it
    links = get_listing_links(page=1)
    boys_url = None
    for link in links:
        if "the-boys" in link.lower() or "boys" in link.lower():
            boys_url = link
            break
    
    if not boys_url:
        print("❌ 'The Boys' not found on page 1, trying page 2...")
        links = get_listing_links(page=2)
        for link in links:
            if "the-boys" in link.lower() or "boys" in link.lower():
                boys_url = link
                break
    
    if not boys_url:
        print("❌ 'The Boys' not found! Trying direct URL...")
        boys_url = "https://dotmovies.broker/the-boys-2026/"
    
    print(f"📺 Found: {boys_url}")
    
    # Step 2: Scrape detail page
    data = scrape_detail(boys_url)
    if not data:
        print("❌ scrape_detail returned None")
        return
    
    print(f"\n🎬 Title: {data['title']}")
    print(f"📅 Year: {data['year']}")
    print(f"📂 Category: {data['category']}")
    print(f"🌐 Language: {data.get('language', 'N/A')}")
    print(f"🎭 Genre: {data.get('genre', 'N/A')}")
    
    print(f"\n📥 Download Links ({len(data['qualities'])} total):")
    for q in sorted(data["qualities"].keys()):
        info = data["qualities"][q]
        url = info["url"] if isinstance(info, dict) else info
        size = info.get("size", "") if isinstance(info, dict) else ""
        print(f"  {q}: {url[:70]}  [{size}]")


def test_movie():
    """Test: Ek regular movie ki files nikalo"""
    print("\n" + "=" * 60)
    print("TEST 3: Regular Movie (G-Direct extraction)")
    print("=" * 60)
    
    links = get_listing_links(page=1)
    movie_url = None
    # Find something that's NOT a series
    skip_words = ["season", "episode", "ep-", "boys"]
    for link in links:
        link_lower = link.lower()
        if not any(w in link_lower for w in skip_words):
            movie_url = link
            break
    
    if not movie_url and links:
        movie_url = links[0]
    
    if not movie_url:
        print("❌ No movie found on page 1!")
        return
    
    print(f"🎬 Testing: {movie_url}")
    
    data = scrape_detail(movie_url)
    if not data:
        print("❌ scrape_detail returned None")
        return
    
    print(f"\n🎬 Title: {data['title']}")
    print(f"📅 Year: {data['year']}")
    print(f"📂 Category: {data['category']}")
    
    print(f"\n📥 Download Links ({len(data['qualities'])} total):")
    for q in sorted(data["qualities"].keys()):
        info = data["qualities"][q]
        url = info["url"] if isinstance(info, dict) else info
        size = info.get("size", "") if isinstance(info, dict) else ""
        print(f"  {q}: {url[:70]}  [{size}]")


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--test", choices=["listing", "boys", "movie", "all"], default="all")
    args = ap.parse_args()
    
    if args.test in ("listing", "all"):
        test_listing()
    if args.test in ("boys", "all"):
        test_the_boys()
    if args.test in ("movie", "all"):
        test_movie()
    
    print("\n✅ Tests complete!")
