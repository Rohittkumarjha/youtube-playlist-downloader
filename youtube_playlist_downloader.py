#!/usr/bin/env python3
"""
YouTube Playlist Downloader (fast + interactive)
------------------------------------------------
Features:
    * Prompts for playlist URL and how many videos to grab.
    * Opens a native file-manager dialog to pick the save folder.
    * Lists available resolutions detected from the first video.
    * Live progress bar (percent, speed, ETA) per video.
    * Terminal hotkeys while downloading:
          p = pause    r = resume    c = cancel
    * Uses parallel fragment downloads (and aria2c if installed) for
      maximum speed.

Install:
    pip install yt-dlp
    # optional but much faster:
    #   - ffmpeg on PATH  (required for >360p, merges video+audio)
    #   - aria2c on PATH  (multi-connection downloader)
"""

import os
import shutil
import sys
import threading
import time

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
except ImportError:
    print("yt-dlp is not installed. Install it with:\n    pip install yt-dlp")
    sys.exit(1)


# ---------- shared control state (hotkeys) ----------
class Controller:
    def __init__(self) -> None:
        self.paused = False
        self.cancelled = False


CTRL = Controller()


class CancelledByUser(Exception):
    pass


# ---------- terminal hotkey listener ----------
def _read_key_windows():
    import msvcrt
    while not CTRL.cancelled:
        if msvcrt.kbhit():
            ch = msvcrt.getch().decode(errors="ignore").lower()
            _handle_key(ch)
        time.sleep(0.05)


def _read_key_unix():
    import termios
    import tty
    import select
    fd = sys.stdin.fileno()
    old = termios.tcgetattr(fd)
    try:
        tty.setcbreak(fd)
        while not CTRL.cancelled:
            r, _, _ = select.select([sys.stdin], [], [], 0.1)
            if r:
                ch = sys.stdin.read(1).lower()
                _handle_key(ch)
    finally:
        termios.tcsetattr(fd, termios.TCSADRAIN, old)


def _handle_key(ch: str) -> None:
    if ch == "p" and not CTRL.paused:
        CTRL.paused = True
        print("\n[paused]  press 'r' to resume, 'c' to cancel")
    elif ch == "r" and CTRL.paused:
        CTRL.paused = False
        print("[resumed]")
    elif ch == "c":
        CTRL.cancelled = True
        CTRL.paused = False
        print("\n[cancel requested] finishing current fragment...")


def start_hotkey_listener() -> None:
    target = _read_key_windows if os.name == "nt" else _read_key_unix
    t = threading.Thread(target=target, daemon=True)
    t.start()


# ---------- prompts ----------
def ask_playlist_url() -> str:
    while True:
        url = input("Enter YouTube playlist link: ").strip()
        if url:
            return url
        print("Please enter a valid URL.")


def ask_video_count(total: int) -> int:
    while True:
        raw = input(
            f"How many videos to download? (Type 'all' or a number 1-{total}): "
        ).strip().lower()
        if raw in ("all", "a", ""):
            return total
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= total:
                return n
        print("Invalid input, try again.")


def ask_save_folder(default_name: str) -> str:
    """Open a native folder picker; fall back to typed input if unavailable."""
    chosen = None
    try:
        import tkinter as tk
        from tkinter import filedialog

        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        print("Opening folder picker...")
        chosen = filedialog.askdirectory(title="Select download folder")
        root.destroy()
    except Exception:
        pass

    if not chosen:
        chosen = input(
            f"Enter save folder path [default: {os.path.join(os.getcwd(), 'downloads')}]: "
        ).strip() or os.path.join(os.getcwd(), "downloads")

    safe = "".join(c for c in default_name if c.isalnum() or c in " -_").strip() or "playlist"
    out = os.path.join(chosen, safe)
    os.makedirs(out, exist_ok=True)
    return out


def collect_available_resolutions(entries) -> list:
    heights = set()
    for entry in entries:
        if not entry:
            continue
        for f in entry.get("formats", []) or []:
            h = f.get("height")
            if h and f.get("vcodec") and f.get("vcodec") != "none":
                heights.add(h)
    return sorted(heights)


def ask_resolution(available: list) -> int:
    if not available:
        return 0
    print("\nAvailable resolutions:")
    for i, h in enumerate(available, 1):
        print(f"  {i}. {h}p")
    print(f"  {len(available) + 1}. Best available")
    while True:
        raw = input("Select resolution (number): ").strip()
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(available):
                return available[idx - 1]
            if idx == len(available) + 1:
                return 0
        print("Invalid choice, try again.")


# ---------- progress bar ----------
def _fmt_bytes(n):
    if not n:
        return "?"
    for unit in ("B", "KB", "MB", "GB"):
        if n < 1024:
            return f"{n:.1f}{unit}"
        n /= 1024
    return f"{n:.1f}TB"


def progress_hook(d: dict) -> None:
    # honor pause: block inside the hook so yt-dlp waits between chunks
    while CTRL.paused and not CTRL.cancelled:
        time.sleep(0.2)
    if CTRL.cancelled:
        raise CancelledByUser()

    if d["status"] == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        speed = d.get("speed") or 0
        eta = d.get("eta") or 0
        pct = (done / total * 100) if total else 0
        bar_len = 30
        filled = int(bar_len * pct / 100)
        bar = "█" * filled + "░" * (bar_len - filled)
        sys.stdout.write(
            f"\r  [{bar}] {pct:5.1f}%  "
            f"{_fmt_bytes(done)}/{_fmt_bytes(total)}  "
            f"{_fmt_bytes(speed)}/s  ETA {eta}s   "
        )
        sys.stdout.flush()
    elif d["status"] == "finished":
        sys.stdout.write("\r  [done] merging/processing...                                  \n")
        sys.stdout.flush()


# ---------- main ----------
def main() -> None:
    playlist_url = ask_playlist_url()

    print("\nFetching playlist info...")
    with YoutubeDL({"quiet": True, "extract_flat": True, "skip_download": True}) as ydl:
        info = ydl.extract_info(playlist_url, download=False)

    entries = info.get("entries") or []
    if not entries:
        print("No videos found. Is this a valid playlist URL?")
        sys.exit(1)

    total = len(entries)
    title = info.get("title") or "playlist"
    print(f"Playlist: {title}  |  {total} videos found.")

    count = ask_video_count(total)
    selected = entries[:count]

    print("Probing available resolutions...")
    first_url = selected[0].get("url") or selected[0].get("webpage_url")
    if first_url and not first_url.startswith("http"):
        first_url = f"https://www.youtube.com/watch?v={first_url}"
    with YoutubeDL({"quiet": True, "skip_download": True}) as ydl:
        probe = ydl.extract_info(first_url, download=False)
    resolutions = collect_available_resolutions([probe])
    chosen = ask_resolution(resolutions)

    out_dir = ask_save_folder(title)

    if chosen == 0:
        fmt = "bestvideo+bestaudio/best"
    else:
        fmt = f"bestvideo[height<={chosen}]+bestaudio/best[height<={chosen}]"

    ydl_opts = {
        "format": fmt,
        "outtmpl": os.path.join(out_dir, "%(playlist_index)s - %(title)s.%(ext)s"),
        "playlist_items": f"1-{count}",
        "merge_output_format": "mp4",
        "ignoreerrors": True,
        "noprogress": True,          # we draw our own bar
        "quiet": True,
        "no_warnings": True,
        "concurrent_fragment_downloads": 16,   # parallel fragments = faster
        "retries": 10,
        "fragment_retries": 10,
        "progress_hooks": [progress_hook],
    }

    # use aria2c when available for multi-connection speed
    if shutil.which("aria2c"):
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = [
            "-x", "16", "-s", "16", "-k", "1M", "--summary-interval=0",
        ]
        print("[aria2c detected] using multi-connection downloader.")

    print(f"\nSaving to: {out_dir}")
    print("Hotkeys:  p = pause    r = resume    c = cancel\n")

    start_hotkey_listener()

    try:
        with YoutubeDL(ydl_opts) as ydl:
            ydl.download([playlist_url])
    except CancelledByUser:
        print("\nDownload cancelled.")
        return
    except DownloadError as e:
        print(f"\nDownload error: {e}")
        return

    if CTRL.cancelled:
        print("\nCancelled.")
    else:
        print("\nAll done!")


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        CTRL.cancelled = True
        print("\nInterrupted.")
