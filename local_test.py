from bs4 import BeautifulSoup
import re

print("🔍 Inspecting local 'fastdl_page.html'...\n")

try:
    with open('fastdl_page.html', 'r', encoding='utf-8') as f:
        html = f.read()
        
    soup = BeautifulSoup(html, 'html.parser')
    
    print("--- ALL EXTRACTED LINKS ON THE PAGE ---")
    for a in soup.find_all('a', href=True):
        href = a['href']
        text = a.text.strip().replace('\n', ' ')
        print(f"Text: '{text}' | Link: {href}")
        
    print("\n--- CHECKING FOR GOOGLE USER CONTENT ---")
    google_links = [a['href'] for a in soup.find_all('a', href=True) if 'googleusercontent' in a['href']]
    if google_links:
        print(f"✅ FOUND GOOGLE LINK: {google_links[0]}")
    else:
        print("❌ NO GOOGLE LINK FOUND. Cloudflare is still blocking, or the HTML structure changed.")

except FileNotFoundError:
    print("❌ fastdl_page.html nahi mili. Pehle scraper run karke file save hone do.")