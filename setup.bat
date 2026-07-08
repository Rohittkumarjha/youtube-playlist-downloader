@echo off
REM One-step setup + run for the YouTube downloader (Windows).
REM Installs EVERYTHING from README.md:
REM   - Python packages: yt-dlp, rich
REM   - System tools:    ffmpeg, aria2, deno
REM Uses winget (built into Windows 10/11). Run as a normal user; UAC will pop up if needed.
REM Usage: double-click this file, or run:  setup.bat

setlocal enableextensions

echo ==^> Checking Python...
where python >nul 2>nul
if errorlevel 1 (
  echo Python is required. Install from https://python.org  ^(tick "Add Python to PATH"^) and re-run.
  pause
  exit /b 1
)

echo ==^> Installing/updating Python packages ^(yt-dlp, rich^)...
python -m pip install --upgrade --quiet pip
python -m pip install --upgrade --quiet -r requirements.txt

echo ==^> ffmpeg ^(HD merges ^& mp3 conversion^)...
where ffmpeg >nul 2>nul
if errorlevel 1 (
  winget install --silent --accept-source-agreements --accept-package-agreements Gyan.FFmpeg
) else ( echo    already installed )

echo ==^> aria2c ^(optional, faster downloads^)...
where aria2c >nul 2>nul
if errorlevel 1 (
  winget install --silent --accept-source-agreements --accept-package-agreements aria2.aria2
) else ( echo    already installed )

echo ==^> Deno ^(fixes YouTube 'Sign in to confirm you're not a bot'^)...
where deno >nul 2>nul
if errorlevel 1 (
  winget install --silent --accept-source-agreements --accept-package-agreements DenoLand.Deno
) else ( echo    already installed )

echo.
echo ==^> Note: If ffmpeg/aria2c/deno were just installed, open a NEW terminal
echo     so they appear on PATH. Then run:  python webapp.py
echo.
echo ==^> Starting web app on http://localhost:8000  ^(Ctrl+C to stop^)
python webapp.py
pause
