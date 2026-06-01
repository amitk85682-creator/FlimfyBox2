from bs4 import BeautifulSoup
import re

print("🔍 Inspecting 'final_bypass_test.html' for the button...\n")

try:
    with open("final_bypass_test.html", "r", encoding="utf-8") as f:
        html = f.read()
        soup = BeautifulSoup(html, 'html.parser')
        
        # 'verify' शब्द ढूँढ रहे हैं
        elements = soup.find_all(string=re.compile("verify", re.IGNORECASE))
        
        if not elements:
            print("❌ 'verify' नाम का कोई बटन नहीं मिला!")
            if "Cloudflare" in html or "cloudflare" in html:
                print("⚠️ Cloudflare का Captcha/Checking पेज फंसा हुआ है।")
        else:
            print("🎯 'Click to Verify' Button Found! Here are the details:")
            for text in elements:
                parent = text.parent
                print(f"👉 Tag: <{parent.name}> | ID: '{parent.get('id')}' | Class: {parent.get('class')} | Text: '{text.strip()}'")
                
except Exception as e:
    print(f"Error: {e}")