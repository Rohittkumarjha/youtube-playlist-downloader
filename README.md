# YouTube Downloader

Download **single videos, multiple videos, or full playlists** from YouTube — either through a clean **web UI** (`webapp.py`) or the classic **CLI** (`youtube_playlist_downloader.py`). Live progress, pause/resume/cancel, auto‑ZIP for playlists, and light/dark theme in the web UI.

---

## Table of contents

1. [What it can do](#1-what-it-can-do)
2. [One‑step setup (recommended)](#2-one-step-setup-recommended)
3. [Manual install](#3-manual-install)
   - [Python](#31-python)
   - [Python packages](#32-python-packages)
   - [FFmpeg (needed for HD)](#33-ffmpeg-needed-for-hd)
   - [Deno or Node (fixes “Sign in to confirm you're not a bot” + n‑challenge)](#34-deno-or-node-fixes-sign-in--n-challenge)
   - [aria2c (optional, faster)](#35-aria2c-optional-faster)
4. [Get a `cookies.txt` from your browser](#4-get-a-cookiestxt-from-your-browser)
5. [Run it](#5-run-it)
   - [Web app](#51-web-app-webapppy)
   - [CLI](#52-cli-youtube_playlist_downloaderpy)
6. [Using it on GitHub Codespaces](#6-using-it-on-github-codespaces)
7. [Hotkeys (CLI)](#7-hotkeys-cli)
8. [Troubleshooting](#8-troubleshooting)

---

## 1. What it can do

- **Web UI** (`webapp.py`) — minimal light/dark themed browser interface, drag‑in cookies, live progress %, speed & ETA, one‑click download of finished files or playlist ZIP.
- **CLI** (`youtube_playlist_downloader.py`) — 3 modes: single video, multiple videos, full playlist.
- Auto‑detects best resolution list from YouTube.
- Bundles multi‑file jobs into a **single ZIP**.
- Uses `aria2c` (16 connections) automatically for playlists when available.
- Codespaces‑aware: serves finished files/ZIP over a forwarded port so downloads land in your PC's Downloads folder.

---

## 2. One‑step setup (recommended)

Two helper scripts install **everything** — Python packages, ffmpeg, aria2, deno, tkinter — and then launch the web app.

### Linux / macOS

```bash
bash setup.sh
```

### Windows (10 / 11)

Double‑click `setup.bat`, or in a terminal:

```bat
setup.bat
```

That's it. When it finishes it starts the web app at <http://localhost:8000>.

> If any system tool was newly installed on Windows, open a **new** terminal window afterwards so `ffmpeg` / `aria2c` / `deno` appear on `PATH`, then run `python webapp.py`.

---

## 3. Manual install

Skip this section if `setup.sh` / `setup.bat` worked.

### 3.1 Python

Python **3.8+**. Check: `python --version`. Install from [python.org/downloads](https://www.python.org/downloads/). **Windows:** tick “Add Python to PATH”.

### 3.2 Python packages

```bash
pip install -U -r requirements.txt
```

That installs:

- `yt-dlp[default]` — the actual downloader
- `yt-dlp-ejs` — plugin that runs YouTube's JS challenge (needed for many videos)
- `rich` — pretty terminal UI

### 3.3 FFmpeg (needed for HD)

Without ffmpeg you're stuck at 360p (yt‑dlp can't merge the separate video+audio streams YouTube serves for higher qualities).

- **Windows:** `choco install ffmpeg` or grab a build from <https://www.gyan.dev/ffmpeg/builds/> and add `bin/` to PATH.
- **macOS:** `brew install ffmpeg`
- **Debian/Ubuntu:** `sudo apt install ffmpeg`
- **Fedora:** `sudo dnf install ffmpeg`

Verify: `ffmpeg -version`.

### 3.4 Deno or Node (fixes “Sign in” + n‑challenge)

YouTube runs a JavaScript challenge (`n challenge`) that yt‑dlp needs a real JS runtime to solve. Without one you get errors like **“Sign in to confirm you're not a bot”** or **“Requested format is not available”** even with valid cookies. Install **Deno** (recommended) or Node.js.

**Linux / macOS:**

```bash
curl -fsSL https://deno.land/install.sh | sh
export PATH="$HOME/.deno/bin:$PATH"
```

**Windows (PowerShell):**

```powershell
irm https://deno.land/install.ps1 | iex
```

Verify: `deno --version` (or `node --version`).

### 3.5 aria2c (optional, faster)

Multi‑connection downloader, auto‑used by the app when present.

- **Windows:** `choco install aria2`
- **macOS:** `brew install aria2`
- **Debian/Ubuntu:** `sudo apt install aria2`
- **Fedora:** `sudo dnf install aria2`

---

## 4. Get a `cookies.txt` from your browser

YouTube blocks a lot of downloads unless yt‑dlp can prove you're logged in. You do that by exporting your browser cookies to a `cookies.txt` file.

> You never type your YouTube password anywhere — the extension just exports the cookie your browser already has.

### Step‑by‑step (Chrome / Edge / Brave / Firefox)

1. **Sign in to YouTube** in your browser at <https://www.youtube.com>. Your avatar should show top‑right.
2. **Install the exporter extension — “Get cookies.txt LOCALLY”:**
   - Chrome / Edge / Brave: <https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc>
   - Firefox: search the add‑ons store for **“cookies.txt”** (pick one that exports **Netscape** format).
3. **Export the cookies.** While on `youtube.com`, click the extension icon → **Export** (or **Export As → cookies.txt**). Your browser saves `cookies.txt`.
4. **Use it:**
   - **Web app:** open `webapp.py` in your browser, drop `cookies.txt` into the **Cookies** card, hit **Upload**.
   - **CLI:** put `cookies.txt` next to `youtube_playlist_downloader.py` — it's auto‑detected.

### Rules

- File must be **Netscape format** (7 tab‑separated columns per line). The extension above exports this by default.
- Don't paste JSON cookies, DevTools output, or screenshots — they'll be rejected.
- **Cookies expire.** If downloads suddenly fail again with the “Sign in” error, re‑export a fresh `cookies.txt` and overwrite the old one.

### CLI alternative: read cookies straight from your browser

If you run the **CLI** on the **same computer** as your browser, you can skip the export — the script can pull cookies directly from Chrome, Edge, Brave, Firefox, Chromium, or Safari (macOS). **Close the browser fully first** so its cookie DB isn't locked. Not available on Codespaces (no browser on the server).

---

## 5. Run it

### 5.1 Web app (`webapp.py`)

```bash
python webapp.py
```

Then open <http://localhost:8000>. Workflow:

1. **Cookies** → upload your `cookies.txt` (or use the button to browse).
2. **URL** → paste a video or playlist link, hit **Fetch**.
3. Pick **resolution** + **format** (mp4 / mp3).
4. Hit **Download**. Watch the live progress bar (%, speed, ETA).
5. When done, click the download link. Playlists arrive as one **ZIP**.

Extras in the web UI:
- 🌙 / ☀ toggle top‑right — switches between **light** and **dark** theme, remembers your choice.
- Cookies card has inline steps + a direct link to the exporter extension.
- Real error messages (with fix suggestions) instead of the raw yt‑dlp trace.

### 5.2 CLI (`youtube_playlist_downloader.py`)

```bash
python youtube_playlist_downloader.py
```

Guided prompts:

1. **Mode** — `1` single, `2` multiple, `3` playlist.
2. **Cookies** — auto‑picked from `cookies.txt`, or a menu (browser / paste / skip).
3. **URL(s)** — for multiple, paste one‑by‑one or all at once (comma/space/newline separated).
4. **Resolution** — pick from detected list or “Best available”.
5. **Save folder** — numbered menu of common locations, or type a custom path.
6. **Download** — live progress; `p`/`r`/`c` to pause/resume/cancel.

Single video → direct file. Multiple / playlist → one ZIP.

---

## 6. Using it on GitHub Codespaces

Files land inside the container, not on your PC. Both the web app and the CLI handle this:

- **Web app:** just forward port **8000**. In the VS Code **PORTS** tab, right‑click the port → **Port Visibility → Public** if the link 404s.
- **CLI:** it asks to start a mini web server and tries to auto‑set the port to Public via `gh`. Same fallback if that fails.

Always use the [`cookies.txt`](#4-get-a-cookiestxt-from-your-browser) method on Codespaces — there's no browser on the server.

Quick Codespaces setup:

```bash
bash setup.sh
```

---

## 7. Hotkeys (CLI)

| Key | Action |
|-----|--------|
| `p` | Pause current download |
| `r` | Resume |
| `c` | Cancel the whole run |

Only work in a real interactive terminal — not in read‑only IDE output panes.

---

## 8. Troubleshooting

| Problem | Fix |
|---|---|
| `yt-dlp is required` on startup | `pip install -U -r requirements.txt` (or run `setup.sh` / `setup.bat`). |
| `Missing dependency 'rich'` | Same as above. |
| **`Sign in to confirm you're not a bot`** | Install **Deno** (§3.4) **and** upload a fresh `cookies.txt` (§4). |
| **`Requested format is not available`** / **`n challenge`** errors | Same fix — install Deno/Node **and** make sure `yt-dlp-ejs` is installed (it is in `requirements.txt`). |
| `cookies.txt does not look like a Netscape format cookies file` | Re‑export with **Get cookies.txt LOCALLY** in **Netscape** format. |
| Stuck at 360p | Install **FFmpeg** (§3.3) and make sure it's on `PATH`. |
| Web UI progress stuck at 0% | Usually one of the above — check the log panel under the progress bar for the real error. |
| CLI folder picker doesn't open | Linux: `sudo apt install python3-tk`. Otherwise use the numbered menu fallback. |
| `HTTP Error 403 / 429` | Update yt‑dlp: `pip install -U "yt-dlp[default]"`. |
| Slow downloads | Install **aria2c** (§3.5). |
| Browser cookies fail with “database is locked” | Fully close the browser first, then retry. |
| Codespaces link shows 404 | Set the forwarded port to **Public** in the **PORTS** tab. |
| Live‑stream URL fails | Live streams have no pre‑merged mp4 until the stream ends — wait for the VOD. |

---

## License

Personal use only. Respect YouTube's Terms of Service and the copyright of the creators whose videos you download.
