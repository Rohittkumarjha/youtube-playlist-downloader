"""
YouTube Downloader — Web UI (no separate server deploy needed).

Run:
    python webapp.py

Then open the URL it prints (works on localhost and GitHub Codespaces).
Everything happens in the browser:
    1. (Optional) Upload your cookies.txt
    2. Paste one link, many links (comma / newline separated), or a playlist URL
    3. Pick Video / Audio + quality
    4. Click Download — watch progress live
    5. Click the final link(s) / ZIP to save to your PC

Uses only Python stdlib + yt-dlp (already required by the CLI script).
"""
from __future__ import annotations

import http.server
import io
import json
import os
import re
import shutil
import socketserver
import sys
import threading
import time
import traceback
import uuid
import zipfile
from pathlib import Path
from urllib.parse import unquote, urlparse

def _auto_install(pkgs: list[str]) -> None:
    import subprocess
    print(f"[setup] Installing missing packages: {', '.join(pkgs)} …")
    cmd = [sys.executable, "-m", "pip", "install", "-U", "--quiet", *pkgs]
    try:
        subprocess.check_call(cmd)
    except subprocess.CalledProcessError:
        print(f"[setup] pip install failed. Please run manually:\n  {' '.join(cmd)}")
        sys.exit(1)


try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
except ImportError:
    _auto_install(["yt-dlp[default]"])
    from yt_dlp import YoutubeDL  # type: ignore
    from yt_dlp.utils import DownloadError  # type: ignore

# yt-dlp-ejs ships the JS "n-challenge" solver YouTube now requires.
# Without it you get: "Sign in to confirm you're not a bot" / "Requested format is not available".
try:
    import yt_dlp_ejs  # type: ignore  # noqa: F401
    _HAS_EJS = True
except ImportError:
    try:
        _auto_install(["yt-dlp-ejs"])
        import yt_dlp_ejs  # type: ignore  # noqa: F401
        _HAS_EJS = True
    except Exception:
        _HAS_EJS = False
        print("[setup] Warning: yt-dlp-ejs not installed. YouTube may block downloads.")
        print("        Install manually:  pip install -U yt-dlp-ejs")

# ffmpeg is required for merging video+audio and audio conversion.
if not shutil.which("ffmpeg"):
    print("[setup] Warning: ffmpeg not found on PATH.")
    print("        Video+audio merges and mp3 conversion will fail without it.")
    print("        Install:  Debian/Ubuntu:  sudo apt-get install -y ffmpeg")
    print("                  macOS (brew):   brew install ffmpeg")
    print("                  Windows:        winget install Gyan.FFmpeg")

# Deno is the JS runtime yt-dlp-ejs uses to solve YouTube's n-challenge.
_HAS_DENO = bool(shutil.which("deno") or shutil.which("node"))
if not _HAS_DENO:
    print("[setup] Warning: neither 'deno' nor 'node' found on PATH.")
    print("        YouTube's bot-check solver needs one of them.")
    print("        Install deno:  curl -fsSL https://deno.land/install.sh | sh")
    print("                       (or)  sudo apt-get install -y nodejs")



ROOT = Path.cwd()
DOWNLOADS_DIR = ROOT / "downloads"
COOKIES_DIR = ROOT / ".webapp_cookies"
DOWNLOADS_DIR.mkdir(exist_ok=True)
COOKIES_DIR.mkdir(exist_ok=True)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
MAX_LOG_LINES = 250


def _list_downloaded_files() -> list[dict]:
    """List every file inside DOWNLOADS_DIR (recursive) with metadata for the UI library."""
    out: list[dict] = []
    if not DOWNLOADS_DIR.exists():
        return out
    for fp in sorted(DOWNLOADS_DIR.rglob("*"), key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True):
        if not fp.is_file():
            continue
        try:
            st = fp.stat()
            rel = fp.relative_to(DOWNLOADS_DIR).as_posix()
            out.append({
                "name": fp.name,
                "path": rel,
                "url": "/downloads/" + rel,
                "size_mb": round(st.st_size / (1024 * 1024), 2),
                "mtime": int(st.st_mtime),
                "is_zip": fp.suffix.lower() == ".zip",
                "kind": "audio" if fp.suffix.lower() in {".mp3", ".m4a", ".opus", ".wav"} else ("zip" if fp.suffix.lower()==".zip" else "video"),
            })
        except OSError:
            continue
    return out


# ---------------- helpers ----------------
def _split_urls(text: str) -> list[str]:
    parts = re.split(r"[\s,]+", text.strip())
    return [p for p in parts if p.startswith("http")]


def _is_playlist_url(url: str) -> bool:
    try:
        q = urlparse(url).query
        return "list=" in q
    except Exception:
        return False


def _cookie_opts_for(job: dict) -> dict:
    cf = job.get("cookies_file")
    if cf and os.path.exists(cf):
        return {"cookiefile": cf}
    return {}


def _job_log(job_id: str, message: str, level: str = "info") -> None:
    """Store backend activity so the browser can show a live process log."""
    line = {
        "time": time.strftime("%H:%M:%S"),
        "level": level,
        "message": str(message).strip(),
    }
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if not job:
            return
        logs = job.setdefault("log", [])
        logs.append(line)
        if len(logs) > MAX_LOG_LINES:
            del logs[:-MAX_LOG_LINES]
        job["last_message"] = line["message"]


def _job_update(job_id: str, **values) -> None:
    with JOBS_LOCK:
        job = JOBS.get(job_id)
        if job:
            job.update(values)


class YtdlpJobLogger:
    def __init__(self, job_id: str):
        self.job_id = job_id

    def debug(self, msg):
        text = str(msg)
        if text.startswith("[debug]"):
            return
        if text:
            _job_log(self.job_id, text, "debug")

    def warning(self, msg):
        _job_log(self.job_id, msg, "warn")

    def error(self, msg):
        _job_log(self.job_id, msg, "error")


def _friendly_error(error: Exception) -> str:
    msg = str(error)
    missing = []
    if not _HAS_EJS: missing.append("pip install -U yt-dlp-ejs")
    if not _HAS_DENO: missing.append("install deno  (curl -fsSL https://deno.land/install.sh | sh)")
    fix_hint = ("  Missing on this machine:\n    - " + "\n    - ".join(missing)) if missing else ""

    if "n challenge" in msg or "JavaScript runtime" in msg or "challenge solver" in msg:
        return ("YouTube's bot-check ('n-challenge') could not be solved. "
                "You need both the yt-dlp EJS solver plugin AND a JS runtime (deno or node).\n"
                + (fix_hint or "  Then upgrade yt-dlp:  pip install -U 'yt-dlp[default]' yt-dlp-ejs"))
    if "Requested format is not available" in msg or "Only images are available" in msg:
        return ("YouTube returned no playable formats — usually because the bot-check solver is missing. "
                "Install the EJS solver + a JS runtime, then try again.\n"
                + (fix_hint or "  pip install -U 'yt-dlp[default]' yt-dlp-ejs   and install deno or node"))
    if "Sign in to confirm" in msg or "not a bot" in msg:
        return ("YouTube is asking for login/bot verification. Two things fix this:\n"
                "  1) Upload a fresh cookies.txt from a signed-in browser (see step 1).\n"
                "  2) Install the JS solver so yt-dlp can pass the n-challenge:\n"
                "     pip install -U 'yt-dlp[default]' yt-dlp-ejs   and install deno or node.\n"
                + fix_hint)
    if "HTTP Error 429" in msg or "Too Many Requests" in msg:
        return "YouTube is rate-limiting this connection. Wait a few minutes, use fresh cookies, then try again."
    if "HTTP Error 403" in msg:
        return "YouTube blocked this request. Refresh cookies.txt and try again."
    if "HTTP Error 404" in msg:
        return "Video not found or private. Check the link."
    return msg or "Something went wrong."


def _base_url(host_header: str) -> str:
    # Respect Codespaces forwarded host if present
    return f"https://{host_header}" if "app.github.dev" in host_header else f"http://{host_header}"


# ---------------- probing ----------------
def probe_url(url: str, cookies_file: str | None, limit: int = 200, job_id: str | None = None) -> dict:
    opts = {
        "quiet": True,
        "skip_download": True,
        "extract_flat": "in_playlist",
        "socket_timeout": 20,
        "retries": 3,
        "extractor_retries": 3,
        "ignoreerrors": False,
    }
    if cookies_file:
        opts["cookiefile"] = cookies_file
    if job_id:
        opts["logger"] = YtdlpJobLogger(job_id)

    if job_id:
        _job_log(job_id, "Connecting to YouTube and reading metadata…")
    with YoutubeDL(opts) as ydl:
        info = ydl.extract_info(url, download=False)

    videos = []
    is_playlist = False
    title = info.get("title") or "video"

    if info.get("_type") == "playlist" or info.get("entries"):
        is_playlist = True
        for e in (info.get("entries") or [])[:limit]:
            if not e:
                continue
            vid_url = e.get("url") or e.get("webpage_url") or ""
            if vid_url and not vid_url.startswith("http"):
                vid_url = f"https://www.youtube.com/watch?v={vid_url}"
            videos.append({
                "title": e.get("title") or "(untitled)",
                "url": vid_url,
                "duration": e.get("duration"),
                "thumbnail": (e.get("thumbnails") or [{}])[-1].get("url") if e.get("thumbnails") else None,
            })
    else:
        videos.append({
            "title": title,
            "url": info.get("webpage_url") or url,
            "duration": info.get("duration"),
            "thumbnail": info.get("thumbnail"),
        })

    # Keep fetch fast: use formats already returned for single videos, and defaults for playlists.
    resolutions = []
    for f in info.get("formats") or []:
        h = f.get("height")
        if h and f.get("vcodec") != "none":
            resolutions.append(h)
    resolutions = sorted({r for r in resolutions if r}) or [144, 240, 360, 480, 720, 1080]
    if job_id:
        _job_log(job_id, f"Found {len(videos)} item(s). Quality options are ready.", "ok")

    return {"title": title, "is_playlist": is_playlist, "videos": videos, "resolutions": resolutions}


def run_probe(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        url_text = job.get("url_text", "")
        cookies_file = job.get("cookies_file") or None
    try:
        _job_update(job_id, status="fetching", stage="Parsing links", progress=8)
        _job_log(job_id, "Started fetch job.")
        urls = _split_urls(url_text)
        if not urls:
            raise ValueError("Please paste at least one YouTube URL.")

        has_cookies = bool(cookies_file and os.path.exists(cookies_file))
        _job_log(job_id, f"Cookies: {'loaded' if has_cookies else 'not uploaded'}.")
        _job_update(job_id, progress=18, stage="Detecting mode")

        if len(urls) > 1:
            # Fast path: do not validate every URL during fetch. yt-dlp validates during download.
            _job_log(job_id, f"Multiple mode detected with {len(urls)} links. Skipping slow per-video probing.", "ok")
            info = {
                "title": "Multiple videos",
                "is_playlist": False,
                "videos": [{"title": u, "url": u, "duration": None, "thumbnail": None} for u in urls],
                "resolutions": [144, 240, 360, 480, 720, 1080],
                "mode": "multiple",
                "urls": urls,
            }
        elif _is_playlist_url(urls[0]):
            _job_update(job_id, progress=32, stage="Reading playlist")
            _job_log(job_id, "Playlist mode detected. Fetching playlist items…")
            info = probe_url(urls[0], cookies_file, job_id=job_id)
            info["mode"] = "playlist"
            info["urls"] = urls
        else:
            _job_update(job_id, progress=32, stage="Reading video")
            _job_log(job_id, "Single video mode detected. Fetching video details…")
            info = probe_url(urls[0], cookies_file, job_id=job_id)
            info["mode"] = "single"
            info["urls"] = urls

        _job_update(job_id, status="probe_done", stage="Ready", progress=100, probe=info)
        _job_log(job_id, "Fetch complete. You can choose type, quality, and start download.", "ok")
    except Exception as e:
        traceback.print_exc()
        message = _friendly_error(e)
        _job_update(job_id, status="error", stage="Fetch failed", error=message, progress=100)
        _job_log(job_id, message, "error")


# ---------------- download worker ----------------
def _make_progress_hook(job_id: str):
    def hook(d: dict) -> None:
        with JOBS_LOCK:
            j = JOBS.get(job_id)
            if not j:
                return
            status = d.get("status")
            if status == "downloading":
                total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
                done = d.get("downloaded_bytes") or 0
                j["current_file"] = os.path.basename(d.get("filename", ""))
                j["speed"] = d.get("speed") or 0
                j["eta"] = d.get("eta") or 0
                j["current_pct"] = (done / total * 100) if total else 0
                j["last_tick"] = time.time()
            elif status == "finished":
                j["current_pct"] = 0
                j["completed_items"] = int(j.get("completed_items") or 0) + 1
                j["stage"] = "Merging / converting"
                j["last_tick"] = time.time()
        if status == "finished":
            _job_log(job_id, "File finished. Running merge/conversion if needed…", "ok")
    return hook


def _make_postprocessor_hook(job_id: str):
    def hook(d: dict) -> None:
        pp = d.get("postprocessor") or "postprocess"
        status = d.get("status")
        if status == "started":
            _job_update(job_id, stage=f"Running {pp}")
            _job_log(job_id, f"Started {pp}…")
        elif status == "finished":
            _job_log(job_id, f"Finished {pp}.", "ok")
    return hook


def run_download(job_id: str) -> None:
    with JOBS_LOCK:
        job = JOBS[job_id]
        job["status"] = "running"
        job["stage"] = "Preparing download"
        job["progress"] = 0

    try:
        _job_log(job_id, "Download job started.")
        out_dir = DOWNLOADS_DIR / job["folder_name"]
        out_dir.mkdir(parents=True, exist_ok=True)
        _job_log(job_id, f"Output folder ready: downloads/{job['folder_name']}")

        mode = job["mode"]  # single | multiple | playlist
        media_type = job["media_type"]  # video | audio
        chosen = int(job.get("resolution") or 0)
        cookie_opts = _cookie_opts_for(job)
        _job_log(job_id, f"Mode: {mode}. Type: {'audio mp3' if media_type == 'audio' else 'video mp4'}.")

        if media_type == "audio":
            fmt = "bestaudio/best"
        elif chosen == 0:
            fmt = "bestvideo+bestaudio/best"
        else:
            fmt = f"bestvideo[height<={chosen}]+bestaudio/best[height<={chosen}]"
        _job_log(job_id, f"Selected yt-dlp format: {fmt}")

        if mode == "playlist":
            outtmpl = str(out_dir / "%(playlist_index)s - %(title)s.%(ext)s")
        else:
            outtmpl = str(out_dir / "%(title)s.%(ext)s")

        ydl_opts = {
            "format": fmt,
            "outtmpl": outtmpl,
            "ignoreerrors": True,
            "noprogress": True,
            "quiet": True,
            "no_warnings": True,
            "concurrent_fragment_downloads": 8,
            "retries": 20,
            "fragment_retries": 20,
            "extractor_retries": 5,
            "socket_timeout": 30,
            "progress_hooks": [_make_progress_hook(job_id)],
            "postprocessor_hooks": [_make_postprocessor_hook(job_id)],
            "logger": YtdlpJobLogger(job_id),
            **cookie_opts,
        }
        if media_type == "audio":
            ydl_opts["postprocessors"] = [{
                "key": "FFmpegExtractAudio",
                "preferredcodec": "mp3",
                "preferredquality": "192",
            }]
        else:
            ydl_opts["merge_output_format"] = "mp4"

        if mode == "playlist":
            items = (job.get("playlist_items") or "").strip()
            if items:
                ydl_opts["playlist_items"] = items
                _job_log(job_id, f"Playlist items filter: {items}")
        else:
            ydl_opts["noplaylist"] = True

        targets = job["urls"]
        # aria2c gives huge speed boost on many files but does NOT stream per-chunk
        # progress back to yt-dlp's hooks. Use it only when there are multiple files
        # so single-video jobs still show a moving progress bar.
        planned_items = len(targets)
        if mode == "playlist":
            pi = (job.get("playlist_items") or "").strip()
            if pi:
                count = 0
                for part in pi.split(","):
                    part = part.strip()
                    if not part: continue
                    if "-" in part:
                        a, b = part.split("-", 1)
                        if a.isdigit() and b.isdigit():
                            count += max(1, int(b) - int(a) + 1)
                        else:
                            count += 1
                    else:
                        count += 1
                planned_items = max(1, count)
            else:
                planned_items = job.get("total_items") or 1
        if shutil.which("aria2c") and planned_items > 1:
            ydl_opts["external_downloader"] = "aria2c"
            ydl_opts["external_downloader_args"] = [
                "-x", "4", "-s", "4", "-k", "1M",
                "--max-tries=10", "--retry-wait=3",
                "--max-connection-per-server=4",
                "--console-log-level=warn", "--summary-interval=0",
                "--allow-overwrite=true", "--auto-file-renaming=false",
            ]
            _job_log(job_id, "aria2c detected: using faster external downloader for multi-file job.")
        else:
            if shutil.which("aria2c"):
                _job_log(job_id, "Using yt-dlp built-in downloader so live progress can stream.", "ok")
            else:
                _job_log(job_id, "aria2c not found: using yt-dlp built-in downloader.", "warn")

        _job_update(job_id, stage="Downloading", total_items=planned_items)
        _job_log(job_id, f"Sending {len(targets)} target(s) to yt-dlp… ({planned_items} file(s) expected)")
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download(targets)

        # collect files
        _job_update(job_id, stage="Collecting files")
        _job_log(job_id, "Scanning downloaded files…")
        files = []
        for root, _, fnames in os.walk(out_dir):
            for fn in fnames:
                if not fn.endswith((".part", ".ytdl")):
                    files.append(os.path.join(root, fn))
        files.sort()

        result_links: list[dict] = []
        if mode == "single" or len(files) <= 1:
            _job_log(job_id, "Single output ready. ZIP is not needed.", "ok")
            for fp in files:
                rel = os.path.relpath(fp, ROOT).replace(os.sep, "/")
                result_links.append({
                    "name": os.path.basename(fp),
                    "size_mb": round(os.path.getsize(fp) / (1024 * 1024), 1),
                    "url": "/" + rel,
                })
        else:
            # Zip everything, delete originals.
            _job_update(job_id, stage="Creating ZIP")
            _job_log(job_id, f"Creating ZIP with {len(files)} file(s)…")
            zip_name = f"{job['folder_name']}.zip"
            zip_path = DOWNLOADS_DIR / zip_name
            with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
                for fp in files:
                    zf.write(fp, arcname=os.path.relpath(fp, DOWNLOADS_DIR))
            for fp in files:
                try: os.remove(fp)
                except OSError: pass
            try: shutil.rmtree(out_dir)
            except OSError: pass
            _job_log(job_id, "ZIP created and original video files deleted.", "ok")
            rel = os.path.relpath(zip_path, ROOT).replace(os.sep, "/")
            result_links.append({
                "name": zip_name,
                "size_mb": round(zip_path.stat().st_size / (1024 * 1024), 1),
                "url": "/" + rel,
                "is_zip": True,
                "file_count": len(files),
            })

        with JOBS_LOCK:
            JOBS[job_id]["status"] = "done"
            JOBS[job_id]["stage"] = "Complete"
            JOBS[job_id]["current_pct"] = 100
            JOBS[job_id]["links"] = result_links
        _job_log(job_id, "All done. Download link is ready.", "ok")

    except Exception as e:
        traceback.print_exc()
        message = _friendly_error(e)
        with JOBS_LOCK:
            JOBS[job_id]["status"] = "error"
            JOBS[job_id]["stage"] = "Failed"
            JOBS[job_id]["error"] = message
        _job_log(job_id, message, "error")


# ---------------- HTTP handler ----------------
INDEX_HTML = None  # loaded on first request from below


class Handler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, *a, **k):
        pass

    # ---- CORS (so a remotely-hosted index.html can call this backend) ----
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, DELETE, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.send_header("Access-Control-Max-Age", "86400")

    def end_headers(self):
        self._cors()
        super().end_headers()

    def do_OPTIONS(self):
        self.send_response(204)
        self.end_headers()

    # ---- utilities ----
    def _send_json(self, code: int, payload: dict) -> None:
        body = json.dumps(payload).encode("utf-8")
        try:
            self.send_response(code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            # Browser stopped waiting / refreshed. Background jobs keep running.
            return


    def _read_json(self) -> dict:
        n = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(n) if n else b"{}"
        try:
            return json.loads(raw.decode("utf-8") or "{}")
        except Exception:
            return {}

    # ---- routes ----
    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/" or path == "/index.html":
            body = INDEX_HTML.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if path.startswith("/api/job/"):
            jid = path.rsplit("/", 1)[-1]
            with JOBS_LOCK:
                j = JOBS.get(jid)
                out = dict(j) if j else None
            if not out:
                return self._send_json(404, {"error": "unknown job"})
            out.pop("cookies_file", None)
            return self._send_json(200, out)
        if path == "/api/files":
            return self._send_json(200, {"files": _list_downloaded_files()})
        if path.startswith("/downloads/"):
            return super().do_GET()
        self.send_error(404)

    def do_DELETE(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/files":
                data = self._read_json()
                rel = (data.get("path") or "").strip().lstrip("/")
                if not rel:
                    return self._send_json(400, {"error": "Missing path"})
                target = (DOWNLOADS_DIR / rel).resolve()
                try:
                    target.relative_to(DOWNLOADS_DIR.resolve())
                except ValueError:
                    return self._send_json(400, {"error": "Invalid path"})
                if not target.exists():
                    return self._send_json(404, {"error": "Not found"})
                if target.is_dir():
                    shutil.rmtree(target)
                else:
                    target.unlink()
                    # clean up now-empty parent folder
                    try:
                        if target.parent != DOWNLOADS_DIR and not any(target.parent.iterdir()):
                            target.parent.rmdir()
                    except Exception:
                        pass
                return self._send_json(200, {"ok": True, "files": _list_downloaded_files()})
            self.send_error(404)
        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    def do_POST(self):
        path = urlparse(self.path).path
        try:
            if path == "/api/cookies":
                return self._upload_cookies()
            if path == "/api/probe":
                data = self._read_json()
                url_text = data.get("url", "")
                if not _split_urls(url_text):
                    return self._send_json(400, {"error": "Please paste at least one YouTube URL."})
                jid = uuid.uuid4().hex[:12]
                with JOBS_LOCK:
                    JOBS[jid] = {
                        "id": jid,
                        "kind": "probe",
                        "status": "queued",
                        "stage": "Queued",
                        "progress": 0,
                        "url_text": url_text,
                        "cookies_file": data.get("cookies_file") or "",
                        "log": [],
                    }
                threading.Thread(target=run_probe, args=(jid,), daemon=True).start()
                return self._send_json(202, {"job_id": jid})
            if path == "/api/download":
                data = self._read_json()
                jid = uuid.uuid4().hex[:12]
                folder = re.sub(r"[^\w\-\. ]+", "_", (data.get("title") or "download"))[:80].strip() or f"job_{jid}"
                folder = f"{folder}_{jid[:4]}"
                with JOBS_LOCK:
                    JOBS[jid] = {
                        "id": jid,
                        "status": "queued",
                        "kind": "download",
                        "stage": "Queued",
                        "mode": data.get("mode") or "single",
                        "media_type": data.get("media_type") or "video",
                        "resolution": data.get("resolution") or 0,
                        "urls": data.get("urls") or [],
                        "playlist_items": data.get("playlist_items") or "",
                        "folder_name": folder,
                        "cookies_file": data.get("cookies_file") or "",
                        "current_file": "",
                        "current_pct": 0,
                        "completed_items": 0,
                        "total_items": data.get("total_items") or len(data.get("urls") or []) or 1,
                        "log": [],
                    }
                threading.Thread(target=run_download, args=(jid,), daemon=True).start()
                return self._send_json(202, {"job_id": jid})
            self.send_error(404)
        except Exception as e:
            traceback.print_exc()
            self._send_json(500, {"error": str(e)})

    def _upload_cookies(self):
        n = int(self.headers.get("Content-Length") or 0)
        if n <= 0 or n > 5 * 1024 * 1024:
            return self._send_json(400, {"error": "Cookie file must be under 5 MB."})
        body = self.rfile.read(n)
        # Extract raw file part from a very simple multipart/form-data
        ctype = self.headers.get("Content-Type", "")
        if "multipart/form-data" in ctype and "boundary=" in ctype:
            boundary = ctype.split("boundary=", 1)[1].encode()
            parts = body.split(b"--" + boundary)
            file_bytes = None
            for p in parts:
                if b"filename=" in p:
                    header_end = p.find(b"\r\n\r\n")
                    if header_end == -1:
                        continue
                    file_bytes = p[header_end + 4:].rstrip(b"\r\n-")
                    break
            if not file_bytes:
                return self._send_json(400, {"error": "No file found in upload."})
        else:
            file_bytes = body  # raw upload fallback

        text = file_bytes.decode("utf-8", errors="ignore")
        first_lines = "\n".join(text.splitlines()[:3])
        if "Netscape HTTP Cookie File" not in first_lines and "\t" not in text:
            return self._send_json(400, {"error": "This doesn't look like a Netscape cookies.txt file."})

        cookie_path = COOKIES_DIR / f"c_{uuid.uuid4().hex[:10]}.txt"
        cookie_path.write_bytes(file_bytes)
        return self._send_json(200, {"cookies_file": str(cookie_path), "name": cookie_path.name})


class ReuseServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


INDEX_HTML = r"""<!doctype html>
<html lang="en" data-theme="dark">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>ytdl — hacker terminal</title>
<link rel="preconnect" href="https://fonts.googleapis.com">
<link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
<link href="https://fonts.googleapis.com/css2?family=JetBrains+Mono:wght@400;500;600;700&family=Inter:wght@400;500;600;700&display=swap" rel="stylesheet">
<style>
:root {
  --bg:#05080a; --bg2:#0a1014; --panel:#0e1519cc; --panel2:#111a20cc;
  --line:#1e2a33; --line2:#2a3a45;
  --green:#39ff14; --green2:#0aff7a; --dim:#5fb37a; --muted:#8aa89a;
  --text:#e6f1ea; --soft:#c7e6d3;
  --amber:#ffb454; --red:#ff5670; --cyan:#5efcff; --violet:#a48bff;
  --glow:0 0 12px rgba(57,255,20,.35);
  --mono:"JetBrains Mono", ui-monospace, "SF Mono", Menlo, Consolas, monospace;
  --sans:"Inter", ui-sans-serif, system-ui, -apple-system, "Segoe UI", Roboto, sans-serif;
  --radius:14px;
}
html[data-theme="light"] {
  --bg:#f4f7f6; --bg2:#e8efeb; --panel:#ffffffcc; --panel2:#f6faf7cc;
  --line:#d3ded6; --line2:#b3c4b8;
  --green:#0a8a3a; --green2:#0aa055; --dim:#3d6a4d; --muted:#556b5c;
  --text:#0b1a12; --soft:#183a24; --amber:#8a5a00; --red:#b3261e;
  --glow:0 0 0 rgba(0,0,0,0);
}
* { box-sizing:border-box; }
html, body { margin:0; padding:0; }
body {
  font-family:var(--sans);
  background:var(--bg); color:var(--text);
  min-height:100vh;
  -webkit-font-smoothing:antialiased; text-rendering:optimizeLegibility;
  position:relative; overflow-x:hidden;
}

/* ---------- HACKER MATRIX BACKGROUND (canvas) ---------- */
#matrix {
  position:fixed; inset:0; z-index:0; pointer-events:none;
  opacity:.28; filter:blur(.3px);
}
html[data-theme="light"] #matrix { opacity:.10; }
/* soft ambient glows in front of matrix but behind content */
body::before {
  content:""; position:fixed; inset:0; pointer-events:none; z-index:1;
  background:
    radial-gradient(900px 500px at 12% -10%, rgba(57,255,20,.10), transparent 55%),
    radial-gradient(700px 450px at 110% 8%, rgba(94,252,255,.06), transparent 60%),
    linear-gradient(180deg, transparent, rgba(0,0,0,.35));
}

.shell { max-width:1180px; margin:0 auto; padding:26px 20px 60px; position:relative; z-index:5; }

/* ---------- header ---------- */
.banner {
  border:1px solid var(--line2);
  background:linear-gradient(180deg, var(--panel), var(--panel2));
  backdrop-filter:blur(10px) saturate(1.1); -webkit-backdrop-filter:blur(10px) saturate(1.1);
  border-radius:var(--radius); padding:18px 20px; margin-bottom:20px;
  box-shadow:0 10px 40px -20px rgba(0,0,0,.6), inset 0 1px 0 rgba(255,255,255,.04);
}
.banner-top { display:flex; gap:14px; align-items:center; justify-content:space-between; flex-wrap:wrap; }
.brand { display:flex; align-items:center; gap:12px; }
.logo {
  width:38px; height:38px; border-radius:10px;
  background:linear-gradient(135deg, var(--green), var(--cyan));
  display:grid; place-items:center; color:#001208; font-family:var(--mono); font-weight:700;
  box-shadow:0 0 20px rgba(57,255,20,.35), inset 0 0 12px rgba(255,255,255,.25);
}
.brand h1 { margin:0; font-size:18px; letter-spacing:.02em; font-weight:700; }
.brand h1 span { color:var(--green); }
.brand p  { margin:2px 0 0; font-size:12px; color:var(--muted); font-family:var(--mono); }
.status-strip { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }

/* ---------- pills ---------- */
.pill {
  display:inline-flex; align-items:center; gap:6px;
  padding:4px 10px; border-radius:999px;
  color:var(--muted); font-size:11px; font-weight:600;
  border:1px solid var(--line2); background:rgba(255,255,255,.02);
  font-family:var(--mono); text-transform:uppercase; letter-spacing:.06em;
}
.pill.ok   { color:var(--green); border-color:rgba(57,255,20,.45); background:rgba(57,255,20,.08); }
.pill.warn { color:var(--amber); border-color:rgba(255,180,84,.5); background:rgba(255,180,84,.08); }
.pill.err  { color:var(--red);   border-color:rgba(255,86,112,.5); background:rgba(255,86,112,.08); }
.pill::before { content:"●"; font-size:8px; }

/* ---------- layout ---------- */
.grid { display:grid; grid-template-columns:minmax(0,1fr) 380px; gap:18px; align-items:start; }
.card {
  background:linear-gradient(180deg, var(--panel), var(--panel2));
  backdrop-filter:blur(14px) saturate(1.1); -webkit-backdrop-filter:blur(14px) saturate(1.1);
  border:1px solid var(--line2);
  border-radius:var(--radius); padding:18px 20px 20px; margin-bottom:16px;
  box-shadow:0 12px 40px -24px rgba(0,0,0,.7), inset 0 1px 0 rgba(255,255,255,.04);
  transition:transform .2s ease, box-shadow .25s ease;
}
.card:hover { box-shadow:0 16px 50px -22px rgba(0,0,0,.75), inset 0 1px 0 rgba(255,255,255,.06); }
.card h2 {
  font-family:var(--mono);
  font-size:12px; margin:0 0 14px; color:var(--green);
  letter-spacing:.06em; font-weight:700; text-transform:uppercase;
  padding-bottom:10px; border-bottom:1px dashed var(--line2);
}
.card h2::before { content:"$ "; color:var(--dim); }
.hint { font-size:13px; color:var(--muted); margin:0 0 12px; line-height:1.6; }
.hint a { color:var(--cyan); text-decoration:none; border-bottom:1px dashed rgba(94,252,255,.4); }
.hint a:hover { color:var(--green); border-color:var(--green); }
label { display:block; font-family:var(--mono); font-size:11px; color:var(--dim); margin:10px 0 6px; text-transform:uppercase; letter-spacing:.08em; }

input[type=text], textarea, select {
  width:100%; background:rgba(0,0,0,.35); color:var(--soft);
  border:1px solid var(--line2); border-radius:10px;
  padding:11px 13px; font-size:13.5px; font-family:var(--mono); outline:none;
  transition:border-color .15s, box-shadow .15s, background .15s;
  caret-color:var(--green);
}
textarea { resize:vertical; min-height:92px; line-height:1.55; }
input:focus, textarea:focus, select:focus {
  border-color:var(--green); background:rgba(0,0,0,.5);
  box-shadow:0 0 0 3px rgba(57,255,20,.14);
}
html[data-theme="light"] input, html[data-theme="light"] textarea, html[data-theme="light"] select { background:#fff; color:var(--text); }

button {
  background:transparent; color:var(--green); border:1px solid rgba(57,255,20,.55);
  padding:10px 16px; border-radius:10px; font-size:12.5px; font-weight:600;
  cursor:pointer; min-height:38px; letter-spacing:.04em;
  font-family:var(--mono); transition:all .18s ease; position:relative;
}
button:hover:not(:disabled) { background:rgba(57,255,20,.12); box-shadow:var(--glow); transform:translateY(-1px); }
button:active:not(:disabled) { transform:translateY(0); }
button:disabled { opacity:.35; cursor:not-allowed; }
button.ghost { color:var(--muted); border-color:var(--line2); }
button.ghost:hover:not(:disabled) { color:var(--green); border-color:var(--green); }
button.primary {
  background:linear-gradient(135deg, rgba(57,255,20,.22), rgba(94,252,255,.14));
  color:#eaffe6; border-color:rgba(57,255,20,.6);
  box-shadow:0 6px 20px -8px rgba(57,255,20,.5);
}
button.primary:hover:not(:disabled) { box-shadow:0 10px 30px -8px rgba(57,255,20,.55), var(--glow); }
button.sm { padding:6px 11px; min-height:30px; font-size:11.5px; }

.row { display:flex; gap:10px; flex-wrap:wrap; align-items:center; }

/* ---------- cookie drag & drop ---------- */
.drop {
  border:1.5px dashed var(--line2); border-radius:12px;
  padding:20px; text-align:center; cursor:pointer;
  background:rgba(0,0,0,.2); transition:all .2s ease;
  font-family:var(--mono); color:var(--muted); font-size:13px;
}
.drop:hover { border-color:var(--green); color:var(--soft); background:rgba(57,255,20,.05); }
.drop.dragover { border-color:var(--green); background:rgba(57,255,20,.12); color:var(--green); transform:scale(1.01); box-shadow:var(--glow); }
.drop .drop-icon { font-size:26px; color:var(--green); margin-bottom:8px; }
.drop .drop-hint { display:block; font-size:11px; color:var(--dim); margin-top:6px; }
.drop input[type=file] { display:none; }
.steps { margin:10px 0 0; padding-left:0; list-style:none; font-size:12.5px; color:var(--muted); line-height:1.8; font-family:var(--mono); }
.steps li::before { content:"› "; color:var(--green); }
.steps b { color:var(--soft); }

/* ---------- videos ---------- */
.videos-toolbar { display:flex; gap:8px; align-items:center; flex-wrap:wrap; margin-bottom:12px; }
.videos-toolbar .count { color:var(--dim); font-size:11.5px; margin-left:auto; font-family:var(--mono); }
.videos { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:10px; max-height:380px; overflow:auto; padding:4px; }
.vid {
  background:rgba(0,0,0,.28); border:1px solid var(--line);
  border-radius:10px; padding:9px; font-size:12px;
  display:flex; gap:10px; align-items:center; min-width:0; cursor:pointer;
  transition:all .18s ease;
}
.vid:hover { border-color:var(--dim); transform:translateY(-1px); }
.vid.selected { border-color:rgba(57,255,20,.6); box-shadow:0 0 0 1px rgba(57,255,20,.25); background:rgba(57,255,20,.05); }
.vid input[type=checkbox] { width:16px; height:16px; accent-color:var(--green); flex-shrink:0; }
.thumb, .vid img { width:60px; height:44px; object-fit:cover; border-radius:6px; background:#001a0d; flex-shrink:0; }
.vid .t { min-width:0; flex:1; overflow:hidden; }
.vid .t b { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:600; color:var(--soft); font-size:12.5px; }
.vid .t span { color:var(--dim); font-size:11px; font-family:var(--mono); }

/* ---------- progress / stdout ---------- */
.progress { background:rgba(0,0,0,.4); border-radius:999px; overflow:hidden; height:12px; border:1px solid var(--line2); position:relative; }
.progress .bar {
  height:100%; background:linear-gradient(90deg, var(--green2), var(--green), var(--cyan));
  transition:width .3s ease; box-shadow:var(--glow);
  background-size:200% 100%; animation:barshift 2s linear infinite;
}
@keyframes barshift { from { background-position:0 0; } to { background-position:200% 0; } }
.hidden { display:none !important; }

.link-card { background:rgba(0,0,0,.28); border:1px dashed rgba(57,255,20,.6); border-radius:12px; padding:14px; margin-top:12px; }
.link-card a { color:var(--cyan); word-break:break-all; font-weight:600; text-decoration:none; }
.link-card a:hover { color:var(--green); }
.err { color:var(--red); font-size:12.5px; margin-top:8px; line-height:1.5; font-family:var(--mono); }

.process { position:sticky; top:20px; }
.process-head { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:12px; }
.stage { color:var(--soft); font-weight:600; font-size:13px; font-family:var(--mono); }
.logbox {
  height:340px; overflow:auto; background:rgba(0,0,0,.55);
  border:1px solid var(--line2); border-radius:10px; padding:12px;
  font-family:var(--mono); font-size:11.5px; line-height:1.65;
}
.logline { display:grid; grid-template-columns:60px 1fr; gap:8px; padding:1px 0; color:var(--soft); }
.logline .time { color:var(--dim); }
.logline .msg::before { content:"$ "; color:var(--dim); }
.logline.ok    .msg { color:var(--green); }
.logline.warn  .msg { color:var(--amber); }
.logline.error .msg { color:var(--red); }
.emptylog { color:var(--dim); font-style:italic; }
.emptylog::before { content:"// "; }

.quick { display:grid; grid-template-columns:repeat(3,1fr); gap:10px; }
.metric { background:rgba(0,0,0,.3); border:1px solid var(--line2); border-radius:10px; padding:10px 12px; }
.metric b { display:block; font-size:18px; font-weight:700; color:var(--green); font-family:var(--mono); }
.metric span { color:var(--dim); font-size:10px; text-transform:uppercase; letter-spacing:.1em; margin-top:2px; display:block; }

.footer { text-align:center; color:var(--dim); font-size:11.5px; margin-top:26px; font-family:var(--mono); }

.lib-grid { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:12px; }
.lib-item { background:rgba(0,0,0,.3); border:1px solid var(--line2); border-radius:10px; padding:12px; display:flex; flex-direction:column; gap:8px; transition:all .18s ease; }
.lib-item:hover { border-color:rgba(57,255,20,.5); transform:translateY(-2px); }
.lib-item .lib-name { font-weight:600; font-size:12.5px; word-break:break-all; color:var(--soft); font-family:var(--mono); }
.lib-item .lib-actions { display:flex; gap:6px; margin-top:2px; }
.lib-item a.lib-dl { flex:1; text-align:center; text-decoration:none; padding:7px 10px; border-radius:8px; border:1px solid rgba(57,255,20,.55); color:var(--green); font-size:11.5px; font-weight:600; font-family:var(--mono); text-transform:uppercase; letter-spacing:.05em; }
.lib-item a.lib-dl:hover { background:rgba(57,255,20,.12); }
.lib-item button.lib-del { color:var(--red); border-color:rgba(255,86,112,.5); }
.lib-item button.lib-del:hover:not(:disabled) { background:rgba(255,86,112,.12); }

code { background:rgba(0,0,0,.35); border:1px solid var(--line2); padding:1px 6px; border-radius:5px; font-size:11.5px; font-family:var(--mono); color:var(--cyan); }

/* ---------- PROCESSING OVERLAY ---------- */
body.busy .shell > *:not(.overlay) { filter:blur(6px) saturate(.85); pointer-events:none; user-select:none; transition:filter .3s ease; }
.overlay {
  position:fixed; inset:0; z-index:50; display:none;
  align-items:center; justify-content:center; padding:20px;
  background:radial-gradient(ellipse at center, rgba(0,10,4,.55), rgba(0,0,0,.85));
  backdrop-filter:blur(6px); -webkit-backdrop-filter:blur(6px);
  animation:fadein .3s ease;
}
body.busy .overlay { display:flex; }
@keyframes fadein { from { opacity:0; } to { opacity:1; } }
.overlay .box {
  width:min(720px, 100%);
  background:linear-gradient(180deg, rgba(14,21,25,.95), rgba(10,15,18,.98));
  border:1px solid rgba(57,255,20,.4);
  border-radius:16px; padding:22px 24px;
  box-shadow:0 30px 100px -20px rgba(0,0,0,.9), 0 0 60px -10px rgba(57,255,20,.35);
  animation:popin .35s cubic-bezier(.2,.9,.3,1.2);
}
@keyframes popin { from { transform:translateY(16px) scale(.96); opacity:0; } to { transform:none; opacity:1; } }
.overlay .ov-head { display:flex; align-items:center; gap:12px; margin-bottom:14px; }
.overlay .spinner {
  width:34px; height:34px; border-radius:50%;
  border:3px solid rgba(57,255,20,.18);
  border-top-color:var(--green);
  animation:spin 1s linear infinite;
  box-shadow:0 0 18px rgba(57,255,20,.35);
}
@keyframes spin { to { transform:rotate(360deg); } }
.overlay h3 { margin:0; font-size:16px; font-family:var(--mono); color:var(--green); letter-spacing:.05em; }
.overlay h3 span { color:var(--muted); font-weight:400; margin-left:6px; font-size:12.5px; }
.overlay .ov-meta { display:flex; gap:10px; flex-wrap:wrap; margin:6px 0 14px; font-family:var(--mono); font-size:12px; color:var(--muted); }
.overlay .ov-meta b { color:var(--green); }
.overlay .progress { height:14px; margin-bottom:14px; }
.overlay .ov-log {
  height:220px; overflow:auto; background:#000; border:1px solid var(--line2); border-radius:10px;
  padding:12px; font-family:var(--mono); font-size:11.5px; line-height:1.6; color:var(--soft);
}
.overlay .ov-log .logline .msg::before { content:"$ "; color:var(--dim); }
.overlay .ov-footer { display:flex; gap:10px; align-items:center; justify-content:space-between; margin-top:14px; }
.overlay .ov-footer small { color:var(--muted); font-family:var(--mono); font-size:11px; }

@media (max-width:860px) {
  .grid { grid-template-columns:1fr; }
  .process { position:static; }
  .videos { max-height:300px; }
  .logbox { height:220px; }
  .overlay .ov-log { height:160px; }
}
@media (max-width:480px) {
  .shell { padding:16px 12px 40px; }
  .card { padding:15px; }
  .videos { grid-template-columns:1fr; }
}
</style>
</head>
<body>
<canvas id="matrix"></canvas>
<div class="shell">

  <div class="banner">
    <div class="banner-top">
      <div class="brand">
        <div class="logo">▶</div>
        <div>
          <h1>ytdl<span>://</span> downloader</h1>
          <p>paste · probe · extract · package</p>
        </div>
      </div>
      <div class="status-strip">
        <span class="pill ok">session live</span>
        <span class="pill" id="activeJobPill">idle</span>
        <button id="btnTheme" class="ghost sm" type="button" title="Toggle theme">◐ theme</button>
      </div>
    </div>
  </div>

  <main class="grid">
    <section>
      <div class="card">
        <h2>01 · cookies (optional)</h2>
        <p class="hint">Bypass "Sign in to confirm you're not a bot" — export cookies from a signed-in browser session and drop the file here.</p>
        <label class="drop" id="cookieDrop" for="cookieFile">
          <div class="drop-icon">⇪</div>
          <div id="dropText"><b style="color:var(--soft)">drag &amp; drop</b> your <code>cookies.txt</code> here, or click to browse</div>
          <span class="drop-hint">Netscape / Mozilla format · exported from browser extension</span>
          <input type="file" id="cookieFile" accept=".txt"/>
        </label>
        <div class="row" style="margin-top:10px">
          <span id="cookieStatus" class="pill warn">no cookies</span>
          <button id="btnClearCookies" class="ghost sm" type="button">clear</button>
        </div>
        <ol class="steps">
          <li>Install ext: <a href="https://chromewebstore.google.com/detail/get-cookiestxt-locally/cclelndahbckbenkjhflpdbgdldlbecc" target="_blank" rel="noopener"><b>Get cookies.txt LOCALLY</b></a></li>
          <li>Open <a href="https://www.youtube.com" target="_blank" rel="noopener">youtube.com</a> (signed in) → click ext → Export</li>
          <li>Drop the exported file above</li>
        </ol>
      </div>

      <div class="card">
        <h2>02 · paste link(s)</h2>
        <textarea id="url" placeholder="paste video / playlist url(s) — separate multiple by comma, space or newline"></textarea>
        <div class="row" style="margin-top:12px">
          <button id="btnFetch" class="primary" type="button">▶ fetch</button>
          <button id="btnClear" class="ghost" type="button">clear</button>
          <span id="fetchStatus" class="pill">waiting</span>
        </div>
        <div id="fetchErr" class="err"></div>
      </div>

      <div class="card hidden" id="optionsCard">
        <h2>03 · select items &amp; format</h2>
        <div id="meta" style="margin-bottom:10px"></div>
        <div class="videos-toolbar">
          <button type="button" class="ghost sm" id="btnSelectAll">select all</button>
          <button type="button" class="ghost sm" id="btnSelectNone">clear</button>
          <span class="count" id="selCount">0 selected</span>
        </div>
        <div class="videos" id="videos"></div>
        <div class="row" style="margin-top:16px">
          <div style="flex:1;min-width:160px">
            <label>output type</label>
            <select id="mediaType">
              <option value="video">video + audio (mp4)</option>
              <option value="audio">audio only (mp3)</option>
            </select>
          </div>
          <div style="flex:1;min-width:160px" id="resWrap">
            <label>quality</label>
            <select id="resolution"></select>
          </div>
        </div>
        <div class="row" style="margin-top:16px">
          <button id="btnDownload" class="primary" type="button">▼ initiate download</button>
        </div>
      </div>

      <div class="card hidden" id="progCard">
        <h2>04 · download progress</h2>
        <div id="progText" class="stage" style="margin-bottom:10px">starting…</div>
        <div class="progress"><div class="bar" id="progBar" style="width:0%"></div></div>
        <div id="progFile" style="font-size:12px;color:var(--dim);margin-top:10px;font-family:var(--mono)"></div>
        <div id="results"></div>
      </div>
    </section>

    <aside class="card process">
      <div class="process-head">
        <h2 style="margin:0;border:none;padding:0">stdout · backend</h2>
        <span class="pill" id="processState">idle</span>
      </div>
      <div class="quick">
        <div class="metric"><b id="metricStage">—</b><span>stage</span></div>
        <div class="metric"><b id="metricPct">0%</b><span>progress</span></div>
        <div class="metric"><b id="metricEta">—</b><span>eta</span></div>
      </div>
      <div style="height:12px"></div>
      <div class="logbox" id="logbox"><div class="emptylog">awaiting stdout stream from yt-dlp process…</div></div>
    </aside>

  </main>

  <section class="card" id="libraryCard">
    <div class="process-head">
      <h2 style="margin:0;border:none;padding:0">05 · downloaded files</h2>
      <div class="row" style="gap:6px">
        <span class="pill" id="libCount">0 files</span>
        <button class="ghost sm" id="btnRefreshLib" type="button">refresh</button>
      </div>
    </div>
    <div id="libraryList" class="lib-grid"><div class="emptylog">vault empty. completed downloads will materialize here.</div></div>
  </section>

  <div class="footer">powered by yt-dlp · keep tab open while jobs run</div>

  <!-- PROCESSING OVERLAY -->
  <div class="overlay" id="overlay" aria-hidden="true">
    <div class="box">
      <div class="ov-head">
        <div class="spinner" id="ovSpinner"></div>
        <div>
          <h3 id="ovTitle">working<span id="ovSub"> · initializing</span></h3>
        </div>
      </div>
      <div class="ov-meta">
        <span>stage: <b id="ovStage">—</b></span>
        <span>eta: <b id="ovEta">—</b></span>
        <span>speed: <b id="ovSpeed">—</b></span>
        <span>file: <b id="ovFile">—</b></span>
      </div>
      <div class="progress"><div class="bar" id="ovBar" style="width:0%"></div></div>
      <div class="ov-log" id="ovLog"><div class="emptylog">connecting to backend…</div></div>
      <div class="ov-footer">
        <small>live process stream — window unblurs on completion</small>
        <button class="ghost sm" id="ovMinimize" type="button">minimize</button>
      </div>
    </div>
  </div>
</div>

<script>
(function(){
  "use strict";
  const $ = s => document.querySelector(s);
  const state = { cookies_file: "", probe: null, activeJob: "", minimized:false };

  function escapeHtml(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
  function fmtDur(s) { if(!s) return ""; s=Math.round(s); const h=Math.floor(s/3600), m=Math.floor((s%3600)/60), ss=s%60; return h ? `${h}:${m.toString().padStart(2,"0")}:${ss.toString().padStart(2,"0")}` : `${m}:${ss.toString().padStart(2,"0")}`; }
  function fmtSpeed(b) { if(!b) return "—"; if(b>1e6) return (b/1e6).toFixed(1)+" MB/s"; if(b>1e3) return (b/1e3).toFixed(0)+" KB/s"; return b+" B/s"; }
  function fmtEta(s) { if(!s) return "—"; if(s<60) return s+"s"; const m=Math.floor(s/60); return m+"m "+(s%60)+"s"; }

  async function api(path, opts={}) {
    const r = await fetch(path, opts);
    const j = await r.json().catch(() => ({}));
    if (!r.ok) throw new Error(j.error || ("HTTP "+r.status));
    return j;
  }

  // ---------- matrix rain background ----------
  (function matrix(){
    const c = $("#matrix"); if(!c) return;
    const ctx = c.getContext("2d");
    let w, h, cols, drops, fontSize = 15;
    const chars = "01アイウエオカキクケコサシスセソタチツテトナニヌネノABCDEF{}<>/_$#@";
    function resize() {
      w = c.width = window.innerWidth;
      h = c.height = window.innerHeight;
      cols = Math.floor(w / fontSize);
      drops = new Array(cols).fill(0).map(() => Math.random() * -50);
    }
    resize(); window.addEventListener("resize", resize);
    function draw() {
      ctx.fillStyle = "rgba(5,8,10,0.08)";
      ctx.fillRect(0, 0, w, h);
      ctx.font = fontSize + "px JetBrains Mono, monospace";
      for (let i=0; i<cols; i++) {
        const ch = chars[Math.floor(Math.random()*chars.length)];
        const y = drops[i] * fontSize;
        // head
        ctx.fillStyle = "rgba(180,255,200,0.9)";
        ctx.fillText(ch, i*fontSize, y);
        // trail
        ctx.fillStyle = "rgba(57,255,20,0.55)";
        ctx.fillText(ch, i*fontSize, y);
        if (y > h && Math.random() > 0.975) drops[i] = 0;
        drops[i] += 1;
      }
    }
    setInterval(draw, 55);
  })();

  // ---------- busy overlay ----------
  function setBusyMode(on, title="working", sub="") {
    document.body.classList.toggle("busy", !!on && !state.minimized);
    if (on) {
      $("#overlay").setAttribute("aria-hidden","false");
      $("#ovTitle").firstChild.nodeValue = title;
      $("#ovSub").textContent = sub ? " · " + sub : "";
    } else {
      state.minimized = false;
      $("#overlay").setAttribute("aria-hidden","true");
    }
  }

  function setBusy(label, kind="") {
    const L = label.toLowerCase();
    $("#activeJobPill").textContent = L;
    $("#processState").textContent = L;
    $("#activeJobPill").className = kind ? "pill " + kind : "pill";
    $("#processState").className = kind ? "pill " + kind : "pill";
  }

  function renderLogs(logs=[]) {
    const html = !logs.length
      ? `<div class="emptylog">awaiting stdout…</div>`
      : logs.map(l => `<div class="logline ${escapeHtml(l.level||"")}"><span class="time">${escapeHtml(l.time||"")}</span><span class="msg">${escapeHtml(l.message||"")}</span></div>`).join("");
    const box = $("#logbox"); box.innerHTML = html; box.scrollTop = box.scrollHeight;
    const ov = $("#ovLog"); if (ov) { ov.innerHTML = html; ov.scrollTop = ov.scrollHeight; }
  }

  function renderProcess(j, pct) {
    const progress = Math.max(0, Math.min(100, pct ?? j.progress ?? j.current_pct ?? 0));
    $("#metricStage").textContent = j.stage || j.status || "—";
    $("#metricPct").textContent = progress.toFixed(0) + "%";
    $("#metricEta").textContent = fmtEta(j.eta);
    $("#ovStage").textContent = j.stage || j.status || "—";
    $("#ovEta").textContent = fmtEta(j.eta);
    $("#ovSpeed").textContent = fmtSpeed(j.speed);
    $("#ovFile").textContent = (j.current_file || "—").split("/").pop() || "—";
    $("#ovBar").style.width = progress.toFixed(1) + "%";
    $("#ovSub").textContent = " · " + progress.toFixed(0) + "%";
    renderLogs(j.log || []);
  }

  // ---- theme ----
  function applyTheme(t) {
    document.documentElement.setAttribute("data-theme", t);
    try { localStorage.setItem("ytdl_theme", t); } catch(_){}
    const btn = $("#btnTheme"); if (btn) btn.textContent = t === "dark" ? "◐ light" : "◐ dark";
  }
  (function initTheme(){
    let saved = "dark";
    try { saved = localStorage.getItem("ytdl_theme") || "dark"; } catch(_){}
    applyTheme(saved);
  })();

  // ---- selection helpers ----
  function selectedIndices() {
    return Array.from($("#videos").querySelectorAll('input[type=checkbox]'))
      .map((cb, i) => cb.checked ? i : -1).filter(i => i >= 0);
  }
  function updateSelCount() {
    const n = selectedIndices().length;
    const total = state.probe ? state.probe.videos.length : 0;
    $("#selCount").textContent = `${n} / ${total} selected`;
  }
  function setAllChecked(v) {
    $("#videos").querySelectorAll('input[type=checkbox]').forEach(cb => {
      cb.checked = v; cb.closest(".vid").classList.toggle("selected", v);
    });
    updateSelCount();
  }
  function toPlaylistItems(indices) {
    if (!indices.length) return "";
    const nums = indices.map(i => i + 1).sort((a,b)=>a-b);
    const out = []; let start = nums[0], prev = nums[0];
    for (let i = 1; i < nums.length; i++) {
      if (nums[i] === prev + 1) { prev = nums[i]; continue; }
      out.push(start === prev ? `${start}` : `${start}-${prev}`);
      start = prev = nums[i];
    }
    out.push(start === prev ? `${start}` : `${start}-${prev}`);
    return out.join(",");
  }

  function renderProbe(j) {
    $("#optionsCard").classList.remove("hidden");
    const modeLabel = j.mode === "playlist" ? "playlist" : j.mode === "multiple" ? "multi" : "single";
    $("#meta").innerHTML = `<span class="pill ok">${modeLabel}</span> <span class="pill">${j.videos.length} item(s)</span> <b style="color:var(--soft);margin-left:6px">${escapeHtml(j.title)}</b>`;
    $("#videos").innerHTML = j.videos.map((v, i) => `
      <label class="vid selected" data-idx="${i}">
        <input type="checkbox" checked data-idx="${i}"/>
        ${v.thumbnail ? `<img src="${escapeHtml(v.thumbnail)}" onerror="this.replaceWith(Object.assign(document.createElement('div'),{className:'thumb'}))"/>` : `<div class="thumb"></div>`}
        <div class="t"><b>${escapeHtml(v.title)}</b><span>${v.duration ? fmtDur(v.duration) : escapeHtml(v.url || "")}</span></div>
      </label>`).join("");
    $("#videos").querySelectorAll('input[type=checkbox]').forEach(cb => {
      cb.addEventListener("change", () => {
        cb.closest(".vid").classList.toggle("selected", cb.checked);
        updateSelCount();
      });
    });
    updateSelCount();
    const res = $("#resolution");
    const choices = (j.resolutions || []).filter(r => r <= 4320).sort((a,b)=>a-b);
    res.innerHTML = `<option value="0">best available</option>` + choices.map(r => `<option value="${r}">${r}p</option>`).join("");
    if (choices.includes(720)) res.value = "720";
  }

  async function pollProbe(id) {
    while (true) {
      await new Promise(r => setTimeout(r, 750));
      let j;
      try { j = await api("/api/job/" + id); } catch { continue; }
      renderProcess(j, j.progress || 0);
      $("#fetchStatus").textContent = `${j.stage || j.status} · ${Math.round(j.progress || 0)}%`;
      if (j.status === "probe_done") {
        state.probe = j.probe;
        renderProbe(j.probe);
        $("#fetchStatus").textContent = "ready";
        $("#fetchStatus").className = "pill ok";
        setBusy("ready", "ok");
        setBusyMode(false);
        break;
      }
      if (j.status === "error") {
        $("#fetchErr").textContent = j.error || "Fetch failed";
        $("#fetchStatus").textContent = "failed";
        $("#fetchStatus").className = "pill err";
        setBusy("failed", "err");
        setBusyMode(false);
        break;
      }
    }
  }

  async function pollJob(id) {
    while (true) {
      await new Promise(r => setTimeout(r, 800));
      let j;
      try { j = await api("/api/job/" + id); } catch { continue; }
      const totalItems = Math.max(1, j.total_items || 1);
      const done = Math.max(0, j.completed_items || 0);
      const overall = j.status === "done" ? 100 : Math.min(99, ((done + (j.current_pct||0)/100) / totalItems) * 100);
      $("#progBar").style.width = overall.toFixed(1) + "%";
      renderProcess(j, overall);
      const filesTxt = totalItems > 1 ? ` · file ${Math.min(done+1, totalItems)}/${totalItems}` : "";
      $("#progText").textContent =
        j.status === "running"
          ? `${j.stage || "downloading"} · ${overall.toFixed(1)}%${filesTxt} · ${fmtSpeed(j.speed)} · eta ${fmtEta(j.eta)}`
          : j.status === "queued" ? "queued…"
          : j.status === "done"   ? "complete ✓"
          : j.status === "error"  ? (j.error||"download failed") : j.status;
      $("#progFile").textContent = j.current_file ? "> " + j.current_file : "";
      if (j.status === "done") { renderLinks(j.links || []); setBusy("complete", "ok"); setBusyMode(false); break; }
      if (j.status === "error") { setBusy("failed", "err"); setBusyMode(false); $("#btnDownload").disabled = false; break; }
    }
  }

  function renderLinks(links) {
    if (!links.length) {
      $("#results").innerHTML = `<div class="err">// no files produced. inspect stdout above for cause.</div>`;
      return;
    }
    $("#results").innerHTML = `<div class="link-card">
      <div style="font-weight:700;margin-bottom:8px;color:var(--green)">▼ transfer complete — click to save</div>
      ${links.map(l => `
        <div style="margin:8px 0">
          <a href="${l.url}" download>${escapeHtml(l.name)}</a>
          <span class="pill">${l.size_mb} MB</span>
          ${l.is_zip ? `<span class="pill ok">zip · ${l.file_count} files</span>` : ""}
        </div>`).join("")}
    </div>`;
    $("#btnDownload").disabled = false;
    refreshLibrary();
  }

  async function refreshLibrary() {
    try {
      const j = await api("/api/files");
      renderLibrary(j.files || []);
    } catch (e) {
      $("#libraryList").innerHTML = `<div class="err">${escapeHtml(e.message)}</div>`;
    }
  }
  function renderLibrary(files) {
    $("#libCount").textContent = files.length + " file" + (files.length===1?"":"s");
    if (!files.length) {
      $("#libraryList").innerHTML = `<div class="emptylog">vault empty. completed downloads will materialize here.</div>`;
      return;
    }
    $("#libraryList").innerHTML = files.map(f => `
      <div class="lib-item" data-path="${escapeHtml(f.path)}">
        <div class="lib-name">${escapeHtml(f.name)}</div>
        <div class="row" style="gap:6px">
          <span class="pill ${f.is_zip?'ok':''}">${escapeHtml(f.kind)}</span>
          <span class="pill">${f.size_mb} MB</span>
        </div>
        <div class="lib-actions">
          <a class="lib-dl" href="${f.url}" download>download</a>
          <button class="ghost sm lib-del" type="button" data-path="${escapeHtml(f.path)}">rm</button>
        </div>
      </div>`).join("");
    $("#libraryList").querySelectorAll(".lib-del").forEach(btn => {
      btn.onclick = async () => {
        const p = btn.getAttribute("data-path");
        if (!confirm("rm -f " + p + " ?")) return;
        btn.disabled = true;
        try {
          const j = await fetch("/api/files", {
            method:"DELETE", headers:{"Content-Type":"application/json"},
            body: JSON.stringify({ path: p })
          }).then(r => r.json());
          if (j.error) throw new Error(j.error);
          renderLibrary(j.files || []);
        } catch (e) { alert("delete failed: " + e.message); btn.disabled = false; }
      };
    });
  }

  // ---------- cookie upload (drag + click) ----------
  async function uploadCookieFile(f) {
    if (!f) return;
    const fd = new FormData(); fd.append("file", f);
    $("#cookieStatus").textContent = "uploading…";
    $("#cookieStatus").className = "pill warn";
    try {
      const j = await api("/api/cookies", { method:"POST", body: fd });
      state.cookies_file = j.cookies_file;
      $("#cookieStatus").textContent = "loaded · " + j.name;
      $("#cookieStatus").className = "pill ok";
      $("#dropText").innerHTML = `<b style="color:var(--green)">✓ ${escapeHtml(j.name)}</b> loaded — drop another to replace`;
    } catch (e) {
      $("#cookieStatus").textContent = e.message;
      $("#cookieStatus").className = "pill err";
    }
  }

  function bind() {
    const btnTheme = $("#btnTheme");
    if (btnTheme) btnTheme.onclick = () => applyTheme(document.documentElement.getAttribute("data-theme") === "dark" ? "light" : "dark");

    // cookie drop zone
    const drop = $("#cookieDrop");
    const fileInput = $("#cookieFile");
    fileInput.addEventListener("change", () => { if (fileInput.files[0]) uploadCookieFile(fileInput.files[0]); });
    ["dragenter","dragover"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); e.stopPropagation(); drop.classList.add("dragover"); }));
    ["dragleave","drop"].forEach(ev => drop.addEventListener(ev, e => { e.preventDefault(); e.stopPropagation(); drop.classList.remove("dragover"); }));
    drop.addEventListener("drop", e => {
      const f = e.dataTransfer && e.dataTransfer.files && e.dataTransfer.files[0];
      if (f) uploadCookieFile(f);
    });
    $("#btnClearCookies").onclick = () => {
      state.cookies_file = ""; fileInput.value = "";
      $("#cookieStatus").textContent = "no cookies"; $("#cookieStatus").className = "pill warn";
      $("#dropText").innerHTML = `<b style="color:var(--soft)">drag &amp; drop</b> your <code>cookies.txt</code> here, or click to browse`;
    };

    $("#btnClear").onclick = () => {
      $("#url").value = "";
      $("#fetchErr").textContent = "";
      $("#fetchStatus").textContent = "waiting";
      $("#fetchStatus").className = "pill";
    };

    $("#btnFetch").onclick = async () => {
      const url = $("#url").value.trim();
      if (!url) { $("#fetchErr").textContent = "// paste a url first"; return; }
      $("#fetchErr").textContent = "";
      $("#optionsCard").classList.add("hidden");
      $("#fetchStatus").textContent = "starting…";
      $("#fetchStatus").className = "pill warn";
      $("#btnFetch").disabled = true;
      setBusy("fetching", "warn");
      setBusyMode(true, "fetching metadata", "probing yt-dlp");
      renderLogs([{time:new Date().toLocaleTimeString(), level:"info", message:"dispatching fetch job → backend"}]);
      try {
        const { job_id } = await api("/api/probe", {
          method:"POST",
          headers:{"Content-Type":"application/json"},
          body: JSON.stringify({ url, cookies_file: state.cookies_file })
        });
        state.activeJob = job_id;
        await pollProbe(job_id);
      } catch (e) {
        $("#fetchErr").textContent = e.message;
        $("#fetchStatus").textContent = "failed";
        $("#fetchStatus").className = "pill err";
        setBusy("failed", "err");
        setBusyMode(false);
      } finally {
        $("#btnFetch").disabled = false;
      }
    };

    $("#btnSelectAll").onclick = () => setAllChecked(true);
    $("#btnSelectNone").onclick = () => setAllChecked(false);

    $("#mediaType").onchange = () => {
      $("#resWrap").style.display = $("#mediaType").value === "audio" ? "none" : "";
    };

    $("#btnDownload").onclick = async () => {
      if (!state.probe) return;
      const p = state.probe;
      const idx = selectedIndices();
      if (!idx.length) { alert("select at least one item to download."); return; }
      let urls, playlist_items = "";
      if (p.mode === "playlist") {
        urls = [p.urls[0]];
        playlist_items = toPlaylistItems(idx);
      } else {
        urls = idx.map(i => p.videos[i].url).filter(Boolean);
      }
      const payload = {
        mode: p.mode,
        media_type: $("#mediaType").value,
        resolution: parseInt($("#resolution").value || "0", 10),
        urls, playlist_items,
        total_items: idx.length,
        title: p.title,
        cookies_file: state.cookies_file,
      };
      $("#btnDownload").disabled = true;
      $("#progCard").classList.remove("hidden");
      $("#progText").textContent = "queued…";
      $("#progBar").style.width = "0%";
      $("#results").innerHTML = "";
      setBusy("downloading", "warn");
      setBusyMode(true, "downloading", `${idx.length} item(s)`);
      renderLogs([{time:new Date().toLocaleTimeString(), level:"info", message:"dispatching download job → backend"}]);
      try {
        const { job_id } = await api("/api/download", {
          method:"POST", headers:{"Content-Type":"application/json"},
          body: JSON.stringify(payload)
        });
        state.activeJob = job_id;
        pollJob(job_id);
      } catch (e) {
        $("#progText").textContent = "error: " + e.message;
        setBusy("failed", "err"); setBusyMode(false);
        $("#btnDownload").disabled = false;
      }
    };

    $("#ovMinimize").onclick = () => {
      state.minimized = true;
      document.body.classList.remove("busy");
      $("#overlay").setAttribute("aria-hidden","true");
    };

    $("#btnRefreshLib").onclick = refreshLibrary;
    refreshLibrary();
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
</script>

</body>
</html>
"""


def main():
    port = int(os.environ.get("PORT") or 8000)
    os.chdir(ROOT)  # so /downloads/* is served relative to ROOT
    server = ReuseServer(("0.0.0.0", port), Handler)
    host = f"localhost:{port}"
    cs_domain = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN")
    cs_name = os.environ.get("CODESPACE_NAME")
    if cs_domain and cs_name:
        host = f"{cs_name}-{port}.{cs_domain}"
        print(f"\n  🌐 Open in browser:  https://{host}\n")
    else:
        print(f"\n  🌐 Open in browser:  http://{host}\n")
    print("  (Ctrl+C to stop)\n")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down…")
        server.shutdown()


if __name__ == "__main__":
    main()
