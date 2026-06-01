import requests
import json
import os

# तुम्हारी नई सेट की हुई API की
zenrows_key = os.environ.get('ZENROWS_API_KEY')
url = "https://fast-dl.org/dl/a9926f"

print("🚀 Sending precision sniper shot to ZenRows...")

# स्नाइपर शॉट: 4 सेकंड रुको, सीधा ID पर वार करो, फिर 12 सेकंड असली लिंक का इंतज़ार करो
js_instructions = [
    {"wait": 4000},
    {"evaluate": "let btn = document.getElementById('download-button'); if(btn){ btn.click(); }"},
    {"wait": 12000}
]

params = {
    "url": url,
    "apikey": zenrows_key,
    "js_render": "true",
    "antibot": "true",
    "js_instructions": json.dumps(js_instructions)
}

try:
    resp = requests.get("https://api.zenrows.com/v1/", params=params, timeout=60)

    if resp.status_code == 200:
        html = resp.text
        print(f"✅ Success! Received {len(html)} chars.")
        
        # HTML को सेव करना ताकि हम अपनी आँखों से देख सकें
        with open("final_bypass_test.html", "w", encoding="utf-8") as f:
            f.write(html)
            
        # सीधा चेक करना कि क्या हमें खज़ाना मिला
        if 'id="vd"' in html or 'googleusercontent' in html:
            print("🎉 BINGO! Cloudflare bypassed and final Google Direct Link FOUND in HTML!")
        else:
            print("❌ Still stuck. The exact button click didn't bypass it. Open 'final_bypass_test.html' to see what happened.")
    else:
        print(f"❌ Error {resp.status_code}: {resp.text}")

except Exception as e:
    print(f"🚨 Script crashed: {e}")