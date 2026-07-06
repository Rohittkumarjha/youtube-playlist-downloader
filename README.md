# YouTube Playlist Downloader

A fast, interactive command-line tool to download YouTube playlists with a native folder picker, live progress bar, and pause / resume / cancel hotkeys.

## Features

- Prompts for playlist URL and how many videos to grab (`all` or a number)
- Opens a native OS **file-manager dialog** to pick the save folder
- Lists available **resolutions** detected from the playlist
- Live **progress bar** (percent, size, speed, ETA) per video
- Terminal **hotkeys** while downloading:
  - `p` → pause
  - `r` → resume
  - `c` → cancel
- **Super fast** downloads via parallel fragments (and `aria2c` when available)

---

## 1. Install Python

Make sure Python **3.8+** is installed:

```bash
python --version
```

If not installed, get it from [python.org/downloads](https://www.python.org/downloads/).
On Windows, tick **"Add Python to PATH"** in the installer.

---

## 2. Install Python dependencies

```bash
pip install -U "yt-dlp[default]" rich
```

- `yt-dlp` — the downloader
- `rich` — pretty terminal UI (panels, colors, progress bar)

Upgrade later with the same command.

---

## 2b. (Highly recommended) Install a JS runtime — Deno

YouTube now requires running a JavaScript challenge. Without a JS runtime,
yt-dlp fails with **"Sign in to confirm you're not a bot"** on many videos.
Install **Deno** (yt-dlp picks it up automatically):

### Linux / macOS
```bash
curl -fsSL https://deno.land/install.sh | sh
```

### Windows (PowerShell)
```powershell
irm https://deno.land/install.ps1 | iex
```

Verify:
```bash
deno --version
```

---

## 2c. Cookies (fixes "Sign in to confirm you're not a bot")

The script now auto-detects usable browser cookie databases and a local
`cookies.txt` file. If `cookies.txt` exists but is not in the required
**Netscape cookies.txt** format, the script rejects it before downloading.

For Codespaces / cloud terminals, browser cookies usually do not exist on
the server. Export cookies on your own computer instead:

1. Log in to YouTube in Chrome / Edge / Brave / Firefox.
2. Install **Get cookies.txt LOCALLY** or another exporter that supports
   **Netscape** format.
3. Open `youtube.com`, export cookies as `cookies.txt`.
4. Upload/put `cookies.txt` in this project folder.
5. Run:
   ```bash
   python youtube_playlist_downloader.py
   ```

Do **not** paste JSON cookies or browser DevTools output — yt-dlp needs
7 TAB-separated Netscape cookie columns.


---

## 3. Install FFmpeg (required for HD)

FFmpeg is needed to merge video + audio for any resolution above 360p.

### Windows
1. Download from [https://www.gyan.dev/ffmpeg/builds/](https://www.gyan.dev/ffmpeg/builds/) (pick **release full** build).
2. Extract, then add the `bin` folder to your **PATH** environment variable.
3. Verify:
   ```bash
   ffmpeg -version
   ```

Or via [Chocolatey](https://chocolatey.org/):
```bash
choco install ffmpeg
```

### macOS
```bash
brew install ffmpeg
```

### Linux (Debian / Ubuntu)
```bash
sudo apt update && sudo apt install ffmpeg
```

### Linux (Fedora)
```bash
sudo dnf install ffmpeg
```

---

## 4. (Optional) Install aria2c for maximum speed

`aria2c` enables 16-connection multi-threaded downloads. The script auto-detects it.

### Windows
```bash
choco install aria2
```
or download from [aria2 releases](https://github.com/aria2/aria2/releases) and add to PATH.

### macOS
```bash
brew install aria2
```

### Linux
```bash
sudo apt install aria2      # Debian / Ubuntu
sudo dnf install aria2      # Fedora
```

Verify:
```bash
aria2c --version
```

---

## 5. Run the downloader

```bash
python youtube_playlist_downloader.py
```

You'll be prompted for:
1. Playlist URL
2. Number of videos (`all` or a number)
3. Resolution (from the detected list)
4. Save folder (opens a file-picker window)

Then the download starts. Use `p` / `r` / `c` in the terminal to control it.

---

## Troubleshooting

| Problem | Fix |
|---|---|
| `yt-dlp is not installed` | Run `pip install "yt-dlp[default]"` |
| `Missing dependency 'rich'` | Run `pip install rich` |
| **`Sign in to confirm you're not a bot`** | Install **Deno** (step 2b) and use a valid Netscape-format `cookies.txt` from a browser where YouTube is logged in |
| `cookies.txt does not look like a Netscape format cookies file` | Re-export cookies with **Get cookies.txt LOCALLY** in **Netscape** format; do not use JSON/DevTools cookies |
| Downloads stuck at 360p | Install **FFmpeg** and make sure it's on PATH |
| Folder picker doesn't open | Install Tk: `sudo apt install python3-tk` (Linux). Falls back to typed path. |
| `HTTP Error 403 / 429` | Update yt-dlp: `pip install -U yt-dlp` |
| Slow downloads | Install **aria2c** (see step 4) |
| Cookies from browser fail with "database is locked" | Fully close the browser first, then retry |
| Hotkeys don't work | Run in a real terminal, not inside an IDE's read-only output pane |


---

## License

Personal use only. Respect YouTube's Terms of Service and creators' copyrights.
