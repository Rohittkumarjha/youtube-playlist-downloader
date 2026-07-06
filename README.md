# YouTube Downloader

A fast, friendly command-line tool to download **single videos, multiple videos, or full playlists** from YouTube. It shows a live progress bar, lets you pause / resume / cancel with one key, and (on GitHub Codespaces) can even hand the finished files back to your PC as a single ZIP.

---

## Table of contents

1. [What it can do](#1-what-it-can-do)
2. [Install Python](#2-install-python)
3. [Install the Python packages](#3-install-the-python-packages)
4. [Install FFmpeg (needed for HD)](#4-install-ffmpeg-needed-for-hd)
5. [Install Deno (fixes “Sign in to confirm you're not a bot”)](#5-install-deno-fixes-sign-in-to-confirm-youre-not-a-bot)
6. [Optional: install aria2c for max speed](#6-optional-install-aria2c-for-max-speed)
7. [Get a `cookies.txt` from your browser](#7-get-a-cookiestxt-from-your-browser)
8. [Run the downloader](#8-run-the-downloader)
9. [Using it on GitHub Codespaces](#9-using-it-on-github-codespaces)
10. [Hotkeys during download](#10-hotkeys-during-download)
11. [Troubleshooting](#11-troubleshooting)

---

## 1. What it can do

- **Three modes** — pick at startup:
  1. **Single video** — one link → one file, direct download link.
  2. **Multiple videos** — enter how many, then paste them either **one by one** or **all at once** (comma, space, or newline separators are all fine). All videos get bundled into **one ZIP** at the end.
  3. **Playlist** — paste a playlist URL, choose how many videos, get a single ZIP.
- Choose the resolution from the list the script detects for you (or just pick “best”).
- Pick your save folder from a menu — **no typing paths**.
- Live progress bar with speed, size, and ETA.
- Hotkeys: **p** pause, **r** resume, **c** cancel.
- Auto-uses `aria2c` (16-connection downloader) if it's installed.
- On GitHub Codespaces: starts a mini web server and gives you a clickable link so the file/ZIP lands in your PC's Downloads folder.

---

## 2. Install Python

You need **Python 3.8 or newer**.

Check what you have:

```bash
python --version
```

If it's missing or too old, install from [python.org/downloads](https://www.python.org/downloads/).
**Windows users:** tick **“Add Python to PATH”** in the installer.

---

## 3. Install the Python packages

Open a terminal in the project folder and run:

```bash
pip install -U "yt-dlp[default]" rich
```

- `yt-dlp` — does the actual downloading.
- `rich` — the pretty terminal UI (panels, colors, progress bar).

Re-run the same command any time you want to upgrade.

---

## 4. Install FFmpeg (needed for HD)

FFmpeg merges the separate video and audio streams YouTube serves for anything above 360p. **Without it you're stuck at 360p.**

### Windows

1. Download a build from <https://www.gyan.dev/ffmpeg/builds/> (pick **release full**).
2. Extract the ZIP, then add the extracted `bin` folder to your **PATH** environment variable.
3. Verify in a new terminal:
   ```bash
   ffmpeg -version
   ```

Or with [Chocolatey](https://chocolatey.org/):

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

## 5. Install Deno (fixes “Sign in to confirm you're not a bot”)

YouTube runs a JavaScript challenge on many videos. If your machine has no JS runtime, yt-dlp fails with **“Sign in to confirm you're not a bot”**. Installing **Deno** solves this — yt-dlp finds and uses it automatically.

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

## 6. Optional: install aria2c for max speed

`aria2c` gives you 16-connection multi-threaded downloads. The script detects it automatically — no config needed.

- **Windows:** `choco install aria2` (or grab a build from [aria2 releases](https://github.com/aria2/aria2/releases) and add it to PATH)
- **macOS:** `brew install aria2`
- **Linux (Debian/Ubuntu):** `sudo apt install aria2`
- **Linux (Fedora):** `sudo dnf install aria2`

Verify: `aria2c --version`

---

## 7. Get a `cookies.txt` from your browser

YouTube blocks a lot of downloads unless yt-dlp can prove you're a logged-in user. You do that by exporting **your browser's cookies** into a file called `cookies.txt` and putting it next to the script.

> **Only export from a browser where you're already logged in to YouTube.** You never type your password anywhere — the extension just saves the cookie the browser already has.

### Step-by-step (works for Chrome, Edge, Brave, Firefox)

1. **Open your browser and sign in to YouTube.** Go to <https://www.youtube.com> and make sure your avatar appears in the top-right corner.

2. **Install a cookies exporter extension.** The one everyone uses is called **Get cookies.txt LOCALLY**:
   - Chrome / Edge / Brave: [Chrome Web Store link](https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc)
   - Firefox: search the Firefox add-ons store for **“cookies.txt”** (pick one that exports the **Netscape** format).

3. **Export the cookies.**
   - While you're on `youtube.com`, click the extension icon in the toolbar.
   - Click **Export** (or **Export As → cookies.txt**).
   - Your browser downloads a file named `cookies.txt`.

4. **Put `cookies.txt` in the project folder** — the same folder as `youtube_playlist_downloader.py`. That's it. Next time you run the script it will auto-detect the file and print:
   ```
   ✓ Auto-detected valid cookies file: cookies.txt
   ```

### Important rules

- The file **must** be in **Netscape format** — 7 tab-separated columns per line. The **Get cookies.txt LOCALLY** extension exports this format by default.
- Do **not** paste JSON cookies, DevTools output, or a screenshot. The script rejects anything that isn't real Netscape format.
- Cookies expire. If downloads suddenly start failing again with the “Sign in” error, just re-export a fresh `cookies.txt` and overwrite the old one.

### Alternative: let the script read cookies straight from your browser

If you run the script on the **same computer** where you use the browser, you can skip the export entirely. The script auto-detects installed browsers (Chrome, Edge, Brave, Firefox, Chromium, Safari on macOS) and can pull cookies directly from them. Just **close the browser fully first** so its cookie database isn't locked.

This option is **not** available on GitHub Codespaces / cloud terminals — there's no browser on the server, so you have to use the `cookies.txt` method above.

---

## 8. Run the downloader

From the project folder:

```bash
python youtube_playlist_downloader.py
```

The script walks you through everything, in this order:

1. **Mode** — pick `1` single video, `2` multiple videos, or `3` playlist.
2. **Cookies** — auto-picked from `cookies.txt` if present, otherwise you get a menu (browser / paste / skip).
3. **URL(s):**
   - **Single:** paste one link.
   - **Multiple:** enter how many videos → choose “one by one” or “paste all at once”. For paste-all-at-once you can separate links with **commas, spaces, or new lines** — all work.
   - **Playlist:** paste the playlist URL → choose how many videos to grab (or `all`).
4. **Resolution** — pick from the detected list, or `Best available`.
5. **Save folder** — a numbered menu of common locations (Downloads, Videos, project folder, `/tmp`…). You can also type a custom path.
6. **Download starts.** Watch the progress bar. Use `p` / `r` / `c` any time.

When it finishes:

- **Single video:** you get a **direct download link** to the file (no ZIP).
- **Multiple or Playlist:** everything is bundled into **one ZIP** and you get a single click-to-download link. The original loose files are then deleted so only the ZIP remains.

---

## 9. Using it on GitHub Codespaces

Codespaces runs in the cloud, so downloaded files land **inside the container**, not on your PC. The script handles this for you:

1. It detects you're on Codespaces and offers to start a **mini web server**.
2. Say **yes** — it will also try to auto-set the forwarded port to **Public** using the pre-installed `gh` CLI. If that fails, open the **PORTS** tab in VS Code, right-click the port, and set **Port Visibility → Public**.
3. When the download finishes, click the link the script prints — the file (or ZIP) downloads straight into your **PC's Downloads folder** via Chrome / your browser.
4. Press **Enter** in the terminal when you're done, and the server shuts down cleanly.

Because there's no browser on the Codespaces server, always use the [`cookies.txt`](#7-get-a-cookiestxt-from-your-browser) method for authentication.

---

## 10. Hotkeys during download

| Key | What it does |
|-----|--------------|
| `p` | Pause the current download |
| `r` | Resume after pausing |
| `c` | Cancel the whole run |

Hotkeys only work in a real terminal. IDE output panes that aren't interactive (e.g. a read-only “Output” view) can't send keys — open a real terminal tab instead.

---

## 11. Troubleshooting

| Problem | Fix |
|---|---|
| `yt-dlp is not installed` | `pip install "yt-dlp[default]"` |
| `Missing dependency 'rich'` | `pip install rich` |
| **`Sign in to confirm you're not a bot`** | Install **Deno** (step 5) **and** provide a valid `cookies.txt` (step 7). |
| `cookies.txt does not look like a Netscape format cookies file` | Re-export cookies with **Get cookies.txt LOCALLY** in **Netscape** format. Don't use JSON / DevTools cookies. |
| Downloads stuck at 360p | Install **FFmpeg** and make sure it's on your PATH (step 4). |
| Folder picker doesn't open | On Linux: `sudo apt install python3-tk`. Otherwise use the numbered menu that appears as a fallback. |
| `HTTP Error 403 / 429` | Update yt-dlp: `pip install -U yt-dlp`. |
| Slow downloads | Install **aria2c** (step 6). |
| Browser cookies fail with “database is locked” | Fully **close the browser** first, then re-run. |
| Hotkeys don't respond | Run in a real interactive terminal, not a read-only IDE output pane. |
| Codespaces link shows 404 | Set the port to **Public** in the **PORTS** tab (see [section 9](#9-using-it-on-github-codespaces)). |

---

## License

Personal use only. Respect YouTube's Terms of Service and the copyright of the creators whose videos you download.
