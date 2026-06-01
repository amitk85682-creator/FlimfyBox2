@echo off
echo.
echo  ========================================
echo    Movie Auto-Scraper — Starting...
echo  ========================================
echo.

REM Env variables system se automatically aa jayenge (DATABASE_URL, TMDB_API_KEY, OMDB_API_KEY)
REM Kuch set karne ki zaroorat nahi — already environment mein hain

echo  DATABASE_URL : %DATABASE_URL%
echo  TMDB_API_KEY : %TMDB_API_KEY%
echo  OMDB_API_KEY : %OMDB_API_KEY%
echo.

echo Starting scraper (har 2 ghante mein chalega)...
python auto_scraper.py --pages 3 --delay 2 --interval 120

pause
