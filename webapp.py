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

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
except ImportError:
    print("yt-dlp is required. Install with:  pip install -U 'yt-dlp[default]'")
    sys.exit(1)

ROOT = Path.cwd()
DOWNLOADS_DIR = ROOT / "downloads"
COOKIES_DIR = ROOT / ".webapp_cookies"
DOWNLOADS_DIR.mkdir(exist_ok=True)
COOKIES_DIR.mkdir(exist_ok=True)

JOBS: dict[str, dict] = {}
JOBS_LOCK = threading.Lock()
MAX_LOG_LINES = 250


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
    if "Sign in to confirm" in msg or "not a bot" in msg:
        return "YouTube is asking for login/bot verification. Upload a fresh cookies.txt exported from the same browser where YouTube is signed in, then fetch again."
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
            elif status == "finished":
                j["current_pct"] = 100
                j["stage"] = "Merging / converting"
        if status == "finished":
            _job_log(job_id, "Download chunk finished. Running merge/conversion if needed…", "ok")
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

        if shutil.which("aria2c"):
            ydl_opts["external_downloader"] = "aria2c"
            ydl_opts["external_downloader_args"] = [
                "-x", "4", "-s", "4", "-k", "1M",
                "--max-tries=10", "--retry-wait=3",
                "--max-connection-per-server=4",
                "--console-log-level=warn", "--summary-interval=0",
                "--allow-overwrite=true", "--auto-file-renaming=false",
            ]
            _job_log(job_id, "aria2c detected: using faster external downloader with safe retry settings.")
        else:
            _job_log(job_id, "aria2c not found: using yt-dlp built-in downloader.", "warn")

        targets = job["urls"]
        _job_update(job_id, stage="Downloading", total_items=len(targets) if mode != "playlist" else job.get("total_items") or 1)
        _job_log(job_id, f"Sending {len(targets)} target(s) to yt-dlp…")
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
        if path.startswith("/downloads/"):
            return super().do_GET()
        self.send_error(404)

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
<html lang="en">
<head>
<meta charset="utf-8"/>
<meta name="viewport" content="width=device-width,initial-scale=1"/>
<title>YouTube Downloader</title>
<style>
:root {
  --bg:#0b0d16; --surface:#12152a; --surface2:#171b35; --line:#242a4a;
  --text:#eef1fb; --muted:#8b93b8; --soft:#c7d2fe; --accent:#14b8a6;
  --accent2:#38bdf8; --ok:#22c55e; --warn:#f59e0b; --err:#ef4444;
}
* { box-sizing:border-box; }
html, body { margin:0; padding:0; }
body { font-family:ui-sans-serif,system-ui,-apple-system,Segoe UI,Roboto,sans-serif; background:var(--bg); color:var(--text); min-height:100vh; -webkit-font-smoothing:antialiased; }
.shell { max-width:1120px; margin:0 auto; padding:20px 14px 60px; }
.hero { display:grid; grid-template-columns:minmax(0,1fr) auto; gap:12px; align-items:center; margin-bottom:16px; }
.brand { display:flex; gap:10px; align-items:center; min-width:0; }
.logo { width:38px; height:38px; display:grid; place-items:center; border-radius:9px; background:#e11d48; font-size:20px; flex-shrink:0; }
h1 { font-size:20px; margin:0; line-height:1.15; letter-spacing:-.01em; }
.sub { color:var(--muted); margin:2px 0 0; font-size:12px; }
.status-strip { display:flex; gap:6px; flex-wrap:wrap; justify-content:flex-end; }
.grid { display:grid; grid-template-columns:minmax(0,1fr) 340px; gap:14px; align-items:start; }
.card { background:var(--surface); border:1px solid var(--line); border-radius:12px; padding:16px; margin-bottom:12px; }
.card h2 { font-size:11px; margin:0 0 12px; color:var(--soft); text-transform:uppercase; letter-spacing:.1em; font-weight:600; }
.hint { font-size:12.5px; color:var(--muted); margin:0 0 10px; line-height:1.5; }
label { display:block; font-size:12px; color:var(--muted); margin:6px 0 4px; }
input[type=text], textarea, select { width:100%; background:#0a0d1e; color:var(--text); border:1px solid #2b3358; border-radius:8px; padding:10px 12px; font-size:14px; font-family:inherit; outline:none; }
textarea { resize:vertical; min-height:88px; line-height:1.5; }
input:focus, textarea:focus, select:focus { border-color:var(--accent2); box-shadow:0 0 0 3px rgba(56,189,248,.14); }
button { background:#0f766e; color:#ecfeff; border:1px solid #2dd4bf; padding:10px 14px; border-radius:8px; font-size:13.5px; font-weight:600; cursor:pointer; min-height:40px; }
button:hover:not(:disabled) { background:#0d9488; }
button:disabled { opacity:.5; cursor:not-allowed; }
button.ghost { background:transparent; border-color:#39426f; color:var(--text); }
button.secondary { background:#1e40af; border-color:#3b82f6; }
button.sm { padding:6px 10px; min-height:32px; font-size:12px; }
.row { display:flex; gap:8px; flex-wrap:wrap; align-items:center; }
.pill { display:inline-flex; align-items:center; gap:5px; padding:3px 9px; border-radius:999px; background:#1c2145; color:var(--muted); font-size:11.5px; border:1px solid rgba(255,255,255,.05); }
.pill.ok { background:rgba(34,197,94,.12); color:#86efac; border-color:rgba(34,197,94,.22); }
.pill.err { background:rgba(239,68,68,.12); color:#fca5a5; border-color:rgba(239,68,68,.22); }
.pill.warn { background:rgba(245,158,11,.12); color:#fcd34d; border-color:rgba(245,158,11,.22); }
.videos-toolbar { display:flex; gap:6px; align-items:center; flex-wrap:wrap; margin-bottom:8px; }
.videos-toolbar .count { color:var(--muted); font-size:12px; margin-left:auto; }
.videos { display:grid; grid-template-columns:repeat(auto-fill,minmax(240px,1fr)); gap:8px; max-height:360px; overflow:auto; padding:2px; }
.vid { background:#0a0d1e; border:1px solid #232a52; border-radius:9px; padding:8px; font-size:12px; display:flex; gap:8px; align-items:center; min-width:0; cursor:pointer; transition:border-color .15s; }
.vid:hover { border-color:#3b82f6; }
.vid.selected { border-color:#2dd4bf; background:#0d1732; }
.vid input[type=checkbox] { width:16px; height:16px; accent-color:#14b8a6; flex-shrink:0; cursor:pointer; }
.thumb, .vid img { width:56px; height:40px; object-fit:cover; border-radius:5px; background:#111827; flex-shrink:0; }
.vid .t { min-width:0; flex:1; overflow:hidden; }
.vid .t b { display:block; overflow:hidden; text-overflow:ellipsis; white-space:nowrap; font-weight:600; color:#eef2ff; font-size:12.5px; }
.vid .t span { color:var(--muted); font-size:11px; }
.progress { background:#0a0d1e; border-radius:999px; overflow:hidden; height:10px; border:1px solid #2b3358; }
.progress .bar { height:100%; background:linear-gradient(90deg,var(--accent),var(--accent2)); transition:width .25s ease; }
.hidden { display:none !important; }
.link-card { background:#0d1b2a; border:1px solid #24597a; border-radius:10px; padding:14px; margin-top:12px; }
.link-card a { color:#7dd3fc; word-break:break-all; font-weight:600; }
.err { color:#fca5a5; font-size:12.5px; margin-top:8px; line-height:1.4; }
.process { position:sticky; top:14px; }
.process-head { display:flex; align-items:center; justify-content:space-between; gap:10px; margin-bottom:10px; }
.stage { color:#e0f2fe; font-weight:600; font-size:13.5px; }
.logbox { height:340px; overflow:auto; background:#060917; border:1px solid #232a52; border-radius:8px; padding:8px; font-family:ui-monospace,SFMono-Regular,Consolas,monospace; font-size:11.5px; line-height:1.5; }
.logline { display:grid; grid-template-columns:56px 1fr; gap:8px; padding:2px 0; color:#cbd5e1; }
.logline .time { color:#64748b; }
.logline.ok .msg { color:#86efac; }
.logline.warn .msg { color:#fcd34d; }
.logline.error .msg { color:#fca5a5; }
.emptylog { color:#64748b; }
.quick { display:grid; grid-template-columns:repeat(3,1fr); gap:6px; margin-top:10px; }
.metric { background:#0a0d1e; border:1px solid #232a52; border-radius:8px; padding:8px 10px; }
.metric b { display:block; font-size:14px; }
.metric span { color:var(--muted); font-size:10.5px; }
.footer { text-align:center; color:var(--muted); font-size:11.5px; margin-top:18px; }
.file-input { color:var(--muted); font-size:12px; max-width:260px; }
@media (max-width:860px) {
  .grid { grid-template-columns:1fr; }
  .process { position:static; }
  .videos { max-height:300px; }
  .logbox { height:220px; }
  .quick { grid-template-columns:repeat(3,1fr); }
  h1 { font-size:18px; }
}
@media (max-width:480px) {
  .shell { padding:14px 10px 40px; }
  .card { padding:14px; }
  .videos { grid-template-columns:1fr; }
  .hero { grid-template-columns:1fr; }
  .status-strip { justify-content:flex-start; }
}
</style>
</head>
<body>
<div class="shell">
  <header class="hero">
    <div class="brand">
      <div class="logo">▶</div>
      <div>
        <h1>YouTube Downloader</h1>
        <p class="sub">Fast fetch, live backend process, video/audio download, ZIP for bulk items.</p>
      </div>
    </div>
    <div class="status-strip">
      <span class="pill ok">Local web app</span>
      <span class="pill" id="activeJobPill">Idle</span>
    </div>
  </header>

  <main class="grid">
    <section>
      <div class="card">
        <h2>1 · Cookies</h2>
        <p class="hint">Upload a fresh Netscape cookies.txt when YouTube asks for sign-in, bot verification, private videos, or age checks.</p>
        <div class="row">
          <input class="file-input" type="file" id="cookieFile" accept=".txt"/>
          <button id="btnUpload" class="ghost">Upload cookies</button>
          <span id="cookieStatus" class="pill warn">No cookies</span>
        </div>
      </div>

      <div class="card">
        <h2>2 · Paste link(s)</h2>
        <textarea id="url" placeholder="Paste one video, multiple videos, or playlist URL. For multiple links use comma, space, or new line."></textarea>
        <div class="row" style="margin-top:12px">
          <button id="btnFetch">Fetch videos</button>
          <button id="btnClear" class="ghost" type="button">Clear</button>
          <span id="fetchStatus" class="pill">Waiting</span>
        </div>
        <div id="fetchErr" class="err"></div>
      </div>

      <div class="card hidden" id="optionsCard">
        <h2>3 · Select items &amp; output</h2>
        <div id="meta" style="margin-bottom:10px"></div>
        <div class="videos-toolbar">
          <button type="button" class="ghost sm" id="btnSelectAll">Select all</button>
          <button type="button" class="ghost sm" id="btnSelectNone">Clear</button>
          <span class="count" id="selCount">0 selected</span>
        </div>
        <div class="videos" id="videos"></div>
        <div class="row" style="margin-top:14px">
          <div style="flex:1;min-width:160px">
            <label>Download type</label>
            <select id="mediaType">
              <option value="video">Video + Audio (mp4)</option>
              <option value="audio">Audio only (mp3)</option>
            </select>
          </div>
          <div style="flex:1;min-width:160px" id="resWrap">
            <label>Quality</label>
            <select id="resolution"></select>
          </div>
        </div>
        <div class="row" style="margin-top:14px">
          <button id="btnDownload" class="secondary">Start download</button>
        </div>
      </div>

      <div class="card hidden" id="progCard">
        <h2>4 · Download progress</h2>
        <div id="progText" class="stage" style="margin-bottom:8px">Starting…</div>
        <div class="progress"><div class="bar" id="progBar" style="width:0%"></div></div>
        <div id="progFile" style="font-size:12px;color:var(--muted);margin-top:8px"></div>
        <div id="results"></div>
      </div>
    </section>

    <aside class="card process">
      <div class="process-head">
        <h2 style="margin:0">Backend process</h2>
        <span class="pill" id="processState">Idle</span>
      </div>
      <div class="quick">
        <div class="metric"><b id="metricStage">—</b><span>Stage</span></div>
        <div class="metric"><b id="metricPct">0%</b><span>Progress</span></div>
        <div class="metric"><b id="metricEta">—</b><span>ETA</span></div>
      </div>
      <div style="height:10px"></div>
      <div class="logbox" id="logbox"><div class="emptylog">Backend messages will appear here while fetching/downloading.</div></div>
    </aside>
  </main>
  <div class="footer">Powered by yt-dlp. Keep this browser tab open while a job is running.</div>
</div>

<script>
const $ = s => document.querySelector(s);
let state = { cookies_file: "", probe: null, activeJob: "" };

async function api(path, opts={}) {
  const r = await fetch(path, opts);
  const j = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(j.error || ("HTTP "+r.status));
  return j;
}

function setBusy(label, kind="") {
  $("#activeJobPill").textContent = label;
  $("#processState").textContent = label;
  $("#activeJobPill").className = kind ? "pill " + kind : "pill";
  $("#processState").className = kind ? "pill " + kind : "pill";
}

function renderLogs(logs=[]) {
  const box = $("#logbox");
  if (!logs.length) {
    box.innerHTML = `<div class="emptylog">Waiting for backend messages…</div>`;
    return;
  }
  box.innerHTML = logs.map(l => `<div class="logline ${escapeHtml(l.level||"")}"><span class="time">${escapeHtml(l.time||"")}</span><span class="msg">${escapeHtml(l.message||"")}</span></div>`).join("");
  box.scrollTop = box.scrollHeight;
}

function renderProcess(j, pct) {
  const progress = Math.max(0, Math.min(100, pct ?? j.progress ?? j.current_pct ?? 0));
  $("#metricStage").textContent = j.stage || j.status || "—";
  $("#metricPct").textContent = progress.toFixed(0) + "%";
  $("#metricEta").textContent = fmtEta(j.eta);
  renderLogs(j.log || []);
}

$("#btnUpload").onclick = async () => {
  const f = $("#cookieFile").files[0];
  if (!f) { $("#cookieStatus").textContent = "Pick a file first"; return; }
  const fd = new FormData(); fd.append("file", f);
  $("#cookieStatus").textContent = "Uploading…";
  $("#cookieStatus").className = "pill warn";
  try {
    const j = await api("/api/cookies", { method:"POST", body: fd });
    state.cookies_file = j.cookies_file;
    $("#cookieStatus").textContent = "Loaded " + j.name;
    $("#cookieStatus").className = "pill ok";
  } catch (e) {
    $("#cookieStatus").textContent = e.message;
    $("#cookieStatus").className = "pill err";
  }
};

$("#btnClear").onclick = () => {
  $("#url").value = "";
  $("#fetchErr").textContent = "";
  $("#fetchStatus").textContent = "Waiting";
};

$("#btnFetch").onclick = async () => {
  const url = $("#url").value.trim();
  if (!url) return;
  $("#fetchErr").textContent = "";
  $("#optionsCard").classList.add("hidden");
  $("#fetchStatus").textContent = "Starting…";
  $("#fetchStatus").className = "pill warn";
  $("#btnFetch").disabled = true;
  setBusy("Fetching", "warn");
  renderLogs([{time:new Date().toLocaleTimeString(), level:"info", message:"Sending fetch job to backend…"}]);
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
    $("#fetchStatus").textContent = "Failed";
    $("#fetchStatus").className = "pill err";
    setBusy("Failed", "err");
  } finally {
    $("#btnFetch").disabled = false;
  }
};

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
      $("#fetchStatus").textContent = "Ready";
      $("#fetchStatus").className = "pill ok";
      setBusy("Ready", "ok");
      break;
    }
    if (j.status === "error") {
      $("#fetchErr").textContent = j.error || "Fetch failed";
      $("#fetchStatus").textContent = "Failed";
      $("#fetchStatus").className = "pill err";
      setBusy("Failed", "err");
      break;
    }
  }
}

function renderProbe(j) {
  $("#optionsCard").classList.remove("hidden");
  const modeLabel = j.mode === "playlist" ? "Playlist" : j.mode === "multiple" ? "Multiple videos" : "Single video";
  $("#meta").innerHTML = `<span class="pill ok">${modeLabel}</span> <span class="pill">${j.videos.length} item(s)</span> <b>${escapeHtml(j.title)}</b>`;
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
  res.innerHTML = `<option value="0">Best available</option>` + choices.map(r => `<option value="${r}">${r}p</option>`).join("");
  if (choices.includes(720)) res.value = "720";
}

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
$("#btnSelectAll").onclick = () => setAllChecked(true);
$("#btnSelectNone").onclick = () => setAllChecked(false);

function toPlaylistItems(indices) {
  // indices are 0-based; yt-dlp playlist_items is 1-based, supports "1,3,5-7"
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

$("#mediaType").onchange = () => {
  $("#resWrap").style.display = $("#mediaType").value === "audio" ? "none" : "";
};

$("#btnDownload").onclick = async () => {
  if (!state.probe) return;
  const p = state.probe;
  const idx = selectedIndices();
  if (!idx.length) { alert("Select at least one item to download."); return; }
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
  $("#progText").textContent = "Queued…";
  $("#progBar").style.width = "0%";
  $("#results").innerHTML = "";
  setBusy("Downloading", "warn");
  renderLogs([{time:new Date().toLocaleTimeString(), level:"info", message:"Sending download job to backend…"}]);
  try {
    const { job_id } = await api("/api/download", {
      method:"POST", headers:{"Content-Type":"application/json"},
      body: JSON.stringify(payload)
    });
    state.activeJob = job_id;
    pollJob(job_id);
  } catch (e) {
    $("#progText").textContent = "Error: " + e.message;
    setBusy("Failed", "err");
    $("#btnDownload").disabled = false;
  }
};

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
    $("#progText").textContent =
      j.status === "running"
        ? `${j.stage || "Downloading"} · ${overall.toFixed(1)}% · ${fmtSpeed(j.speed)} · ETA ${fmtEta(j.eta)}`
        : j.status === "queued" ? "Queued…"
        : j.status === "done"   ? "Complete"
        : j.status === "error"  ? (j.error||"Download failed") : j.status;
    $("#progFile").textContent = j.current_file ? "Current: " + j.current_file : "";
    if (j.status === "done") { renderLinks(j.links || []); setBusy("Complete", "ok"); break; }
    if (j.status === "error") { setBusy("Failed", "err"); $("#btnDownload").disabled = false; break; }
  }
}

function renderLinks(links) {
  if (!links.length) {
    $("#results").innerHTML = `<div class="err">No files were produced. Check the backend process log above for the exact reason.</div>`;
    return;
  }
  $("#results").innerHTML = `<div class="link-card">
    <div style="font-weight:700;margin-bottom:8px">Ready — click to save to your PC</div>
    ${links.map(l => `
      <div style="margin:8px 0">
        <a href="${l.url}" download>${escapeHtml(l.name)}</a>
        <span class="pill">${l.size_mb} MB</span>
        ${l.is_zip ? `<span class="pill ok">ZIP · ${l.file_count} files</span>` : ""}
      </div>`).join("")}
  </div>`;
  $("#btnDownload").disabled = false;
}

function escapeHtml(s) { return String(s ?? "").replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }
function fmtDur(s) { if(!s) return ""; s=Math.round(s); const h=Math.floor(s/3600), m=Math.floor((s%3600)/60), ss=s%60; return h ? `${h}:${m.toString().padStart(2,"0")}:${ss.toString().padStart(2,"0")}` : `${m}:${ss.toString().padStart(2,"0")}`; }
function fmtSpeed(b) { if(!b) return "—"; if(b>1e6) return (b/1e6).toFixed(1)+" MB/s"; if(b>1e3) return (b/1e3).toFixed(0)+" KB/s"; return b+" B/s"; }
function fmtEta(s) { if(!s) return "—"; if(s<60) return s+"s"; const m=Math.floor(s/60); return m+"m "+(s%60)+"s"; }
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
