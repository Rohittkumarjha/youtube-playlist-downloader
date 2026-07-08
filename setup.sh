#!/usr/bin/env bash
# One-step setup + run for the YouTube downloader.
# Installs EVERYTHING listed in README.md:
#   - Python packages: yt-dlp, rich
#   - System tools:    ffmpeg, aria2, deno, python3-tk
# Usage:  bash setup.sh
set -e

echo "==> Checking Python…"
command -v python3 >/dev/null 2>&1 || { echo "Python 3 is required. Install it and re-run."; exit 1; }

# ---------- pick a package manager for system tools ----------
PM=""
SUDO=""
if   command -v apt-get >/dev/null 2>&1; then PM="apt";    command -v sudo >/dev/null 2>&1 && SUDO="sudo"
elif command -v dnf     >/dev/null 2>&1; then PM="dnf";    command -v sudo >/dev/null 2>&1 && SUDO="sudo"
elif command -v pacman  >/dev/null 2>&1; then PM="pacman"; command -v sudo >/dev/null 2>&1 && SUDO="sudo"
elif command -v brew    >/dev/null 2>&1; then PM="brew"
fi

install_pkg() {
  # $1 = friendly name, rest = package names per manager: apt|dnf|pacman|brew
  local name="$1"; shift
  local apt_p="$1" dnf_p="$2" pac_p="$3" brew_p="$4"
  case "$PM" in
    apt)    $SUDO apt-get install -y -qq "$apt_p" ;;
    dnf)    $SUDO dnf install -y -q  "$dnf_p" ;;
    pacman) $SUDO pacman -S --noconfirm --needed "$pac_p" ;;
    brew)   brew install "$brew_p" ;;
    *)      echo "   No supported package manager — install $name manually."; return 1 ;;
  esac
}

echo "==> Updating pip & installing Python packages (yt-dlp, rich)…"
python3 -m pip install --upgrade --quiet pip
python3 -m pip install --upgrade --quiet -r requirements.txt

if [ -n "$PM" ] && [ "$PM" = "apt" ]; then
  echo "==> Refreshing apt index…"
  $SUDO apt-get update -qq || true
fi

echo "==> ffmpeg (HD merges & mp3 conversion)…"
if ! command -v ffmpeg >/dev/null 2>&1; then
  install_pkg ffmpeg ffmpeg ffmpeg ffmpeg ffmpeg || true
else echo "   already installed"; fi

echo "==> aria2c (optional, faster multi-connection downloads)…"
if ! command -v aria2c >/dev/null 2>&1; then
  install_pkg aria2 aria2 aria2 aria2 aria2 || true
else echo "   already installed"; fi

echo "==> python3-tk (folder-picker GUI in CLI script)…"
if ! python3 -c "import tkinter" >/dev/null 2>&1; then
  install_pkg python3-tk python3-tk python3-tkinter tk python-tk || true
else echo "   already installed"; fi

echo "==> Deno (fixes YouTube 'Sign in to confirm you're not a bot')…"
if ! command -v deno >/dev/null 2>&1; then
  if [ "$PM" = "brew" ]; then
    brew install deno || true
  else
    curl -fsSL https://deno.land/install.sh | sh >/dev/null || echo "   Deno install failed — install manually from https://deno.land"
    # add to PATH for this session
    export PATH="$HOME/.deno/bin:$PATH"
    # persist for future shells
    grep -q 'deno/bin' "$HOME/.bashrc" 2>/dev/null || echo 'export PATH="$HOME/.deno/bin:$PATH"' >> "$HOME/.bashrc"
  fi
else echo "   already installed"; fi

echo ""
echo "==> Versions:"
python3 --version 2>&1 | sed 's/^/   /'
python3 -c "import yt_dlp; print('   yt-dlp', yt_dlp.version.__version__)" 2>/dev/null || true
command -v ffmpeg  >/dev/null && echo "   ffmpeg  $(ffmpeg  -version | head -1 | awk '{print $3}')"  || echo "   ffmpeg  MISSING"
command -v aria2c  >/dev/null && echo "   aria2c  $(aria2c  --version | head -1 | awk '{print $3}')"  || echo "   aria2c  (skipped)"
command -v deno    >/dev/null && echo "   deno    $(deno --version | head -1 | awk '{print $2}')"    || echo "   deno    (skipped)"

echo ""
echo "==> Starting web app on http://localhost:8000  (Ctrl+C to stop)"
exec python3 webapp.py
