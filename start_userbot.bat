@echo off
echo ══════════════════════════════════════════════
echo    🤖 Telegram Userbot Starter
echo ══════════════════════════════════════════════
echo.

REM Install dependencies if not already installed
echo 📦 Installing/Checking Dependencies...
pip install telethon qrcode >nul 2>&1
echo ✅ Dependencies ready!
echo.

REM Check if USERBOT_SESSION exists in .env
findstr /C:"USERBOT_SESSION=" .env >nul 2>&1
if %ERRORLEVEL% NEQ 0 (
    echo ⚠️  No session found! Starting QR Login...
    echo.
    python qr_login.py
    if %ERRORLEVEL% NEQ 0 (
        echo ❌ QR Login failed!
        pause
        exit /b 1
    )
)

REM Run the userbot
echo 🚀 Starting Userbot...
echo.
python t2t_userbot.py %*

echo.
pause
