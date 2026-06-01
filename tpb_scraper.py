import aiohttp
import asyncio
import urllib.parse
import logging

# Configure logging for the test script
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
log = logging.getLogger(__name__)

MAX_SIZE_BYTES = 4080218931  # 3.8GB

async def search_tpb(query: str):
    """
    Searches The Pirate Bay API for torrents.
    Returns the top 3 seeded torrents that are under the 3.8GB size limit.
    """
    formatted_query = urllib.parse.quote(query)
    url = f"https://apibay.org/q.php?q={formatted_query}&cat=200"
    
    results = []
    
    try:
        from curl_cffi import requests
        data = None
        
        try:
            async with requests.AsyncSession(impersonate="chrome") as session:
                response = await session.get(url, timeout=15)
                if response.status_code == 200:
                    data = response.json()
                else:
                    log.error(f"TPB API returned status {response.status_code}")
        except Exception as e:
            log.warning(f"TPB API direct request failed: {e}")
            
        if not data:
            log.info("Trying TPB via AllOrigins proxy...")
            proxy_url = f"https://api.allorigins.win/raw?url={urllib.parse.quote(url)}"
            try:
                async with requests.AsyncSession(impersonate="chrome") as session:
                    response = await session.get(proxy_url, timeout=20)
                    if response.status_code == 200:
                        data = response.json()
                    else:
                        log.error(f"TPB API proxy returned status {response.status_code}")
                        return []
            except Exception as e:
                log.error(f"TPB API proxy request failed: {e}")
                return []
                
        # Check if API returned empty result 
        # (TPB sometimes returns [{'id': '0', 'name': 'No results returned', ...}])
        if not data or (len(data) == 1 and data[0].get('id') == '0'):
            log.info(f"No results found for: {query}")
            return []
        
        # Sort data by seeders in descending order
        try:
            data.sort(key=lambda x: int(x.get('seeders', 0)), reverse=True)
        except ValueError:
            pass # Keep original order if parsing seeders fails
        
        excluded_keywords = ['epub', 'pdf', 'mobi', 'audiobook', 'mp3', 'flac']

        for item in data:
            name = item.get('name')
            info_hash = item.get('info_hash')
            size_str = item.get('size')
            seeders = item.get('seeders')
            
            if not name or not info_hash or not size_str:
                continue
            
            # Filter out torrents with low seeders (15 or fewer)
            seeders_count = int(seeders) if str(seeders).isdigit() else 0
            if seeders_count <= 15:
                log.debug(f"Skipping '{name}' - Low seeders: {seeders_count}")
                continue
            
            # Filter out non-video content
            name_lower = name.lower()
            if any(kw in name_lower for kw in excluded_keywords):
                log.debug(f"Skipping '{name}' - Contains excluded keyword")
                continue
                
            try:
                size_bytes = int(size_str)
            except ValueError:
                continue
                
            # Filter out anything larger than the maximum allowed size (3.8GB)
            if size_bytes > MAX_SIZE_BYTES:
                log.debug(f"Skipping '{name}' - Size too large: {size_bytes / (1024**3):.2f} GB")
                continue
                
            # Calculate size in GB for the output
            size_gb = round(size_bytes / (1024**3), 2)
            
            # Construct magnet link manually
            encoded_name = urllib.parse.quote(name)
            magnet_link = f"magnet:?xt=urn:btih:{info_hash}&dn={encoded_name}&tr=udp%3A%2F%2Ftracker.opentrackr.org%3A1337%2Fannounce"
            
            results.append({
                'title': name,
                'magnet_link': magnet_link,
                'size_gb': size_gb,
                'seeders': int(seeders) if str(seeders).isdigit() else 0
            })
                
        return results

    except Exception as e:
        log.error(f"Error searching TPB: {e}")
        return []

if __name__ == "__main__":
    # Test the scraper directly
    query = "Project Hail Mary"
    print(f"Testing TPB scraper with query: '{query}'")
    
    results = asyncio.run(search_tpb(query))
    
    print(f"\nFound {len(results)} results:")
    for i, res in enumerate(results, 1):
        print(f"\nResult {i}:")
        print(f"Title: {res['title']}")
        print(f"Size: {res['size_gb']} GB")
        print(f"Seeders: {res['seeders']}")
        print(f"Magnet: {res['magnet_link'][:80]}...")

    # Database insertion
    try:
        import db_utils
        conn = db_utils.get_db_connection()
        if conn:
            cur = conn.cursor()
            for res in results:
                # Map to pending_leech table columns which is what userbot reads
                cur.execute("""
                    INSERT INTO pending_leech (url, filename, file_size, movie_title, quality, status)
                    VALUES (%s, %s, %s, %s, %s, 'pending')
                    ON CONFLICT (url) DO NOTHING
                """, (
                    res['magnet_link'],
                    res['title'],
                    str(res['size_gb']) + " GB",
                    res['title'],
                    "1080p",
                ))
                if cur.rowcount > 0:
                    print(f"✅ Successfully inserted '{res['title']}' into pending_leeches queue!")
                else:
                    print(f"⏩ '{res['title']}' already exists in pending_leeches queue.")
            conn.commit()
            cur.close()
            db_utils.close_db_connection(conn)
        else:
            print("❌ Failed to connect to database!")
    except Exception as e:
        print(f"❌ Database insertion error: {e}")
