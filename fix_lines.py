"""Run this to fix the corrupted lines in movie_scraper.py"""
with open('movie_scraper.py', 'r', encoding='utf-8') as f:
    lines = f.readlines()

print(f"Original: {len(lines)} lines")

# Delete orphaned lines 439-447 (0-indexed: 438-446)
# Line 438 currently has corrupted content from a partial edit
# Line 439-446 are orphaned old code that should not be there
bad_start = 438  # 0-indexed
bad_end = 447     # 0-indexed, exclusive

# Show what we're removing
print("Removing these lines:")
for i in range(bad_start, bad_end):
    print(f"  {i+1}: {repr(lines[i][:80])}")

# Fix line 438: should just be "                return found_url\n"
lines[bad_start] = "                return found_url\n"

# Remove lines 439-446 (the orphaned old code)
del lines[bad_start+1:bad_end]

print(f"\nFixed: {len(lines)} lines")

with open('movie_scraper.py', 'w', encoding='utf-8') as f:
    f.writelines(lines)

print("DONE! movie_scraper.py has been fixed.")
