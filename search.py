import re

def search_file(filepath, term):
    with open(filepath, 'r', encoding='utf-8') as f:
        lines = f.readlines()
        for i, line in enumerate(lines):
            if term.lower() in line.lower():
                print(f"{i+1}: {line.strip()}")

print("Searching for HTML...")
search_file("main.py", "html")
print("Searching for Bollywood...")
search_file("main.py", "Bollywood")
