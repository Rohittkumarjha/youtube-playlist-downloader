#!/usr/bin/env python3
"""
YouTube Playlist Downloader — fast, interactive, pretty.

Features:
  * Native folder picker for save location.
  * Cookie support (browser or cookies.txt) to bypass
    "Sign in to confirm you're not a bot".
  * Rich terminal UI: panels, colored prompts, live progress bar.
  * Hotkeys during download:  p = pause   r = resume   c = cancel
  * Parallel fragment downloads + aria2c auto-detect for max speed.

Install:
    pip install "yt-dlp[default]" rich
    # optional but recommended:
    #   ffmpeg  (required for >360p, merges video+audio)
    #   aria2c  (multi-connection downloader)
    #   deno    (JS runtime yt-dlp uses for YouTube; fixes many 'Sign in' errors)
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
import time
from pathlib import Path

# ---------- pretty UI (rich) ----------
try:
    from rich.console import Console
    from rich.panel import Panel
    from rich.prompt import Prompt, IntPrompt
    from rich.table import Table
    from rich.text import Text
    from rich.align import Align
    from rich.rule import Rule
    from rich.progress import (
        Progress, BarColumn, TextColumn, TimeRemainingColumn,
        TransferSpeedColumn, DownloadColumn, SpinnerColumn,
    )
except ImportError:
    print("Missing dependency 'rich'. Install with:\n    pip install rich")
    sys.exit(1)

try:
    from yt_dlp import YoutubeDL
    from yt_dlp.utils import DownloadError
except ImportError:
    print("yt-dlp is not installed. Install it with:\n    pip install \"yt-dlp[default]\"")
    sys.exit(1)


console = Console()


# ---------- shared control state ----------
class Controller:
    def __init__(self) -> None:
        self.paused = False
        self.cancelled = False


CTRL = Controller()


class CancelledByUser(Exception):
    pass


# ---------- hotkey listener ----------
def _read_key_windows() -> None:
    import msvcrt
    while not CTRL.cancelled:
        if msvcrt.kbhit():
            ch = msvcrt.getch().decode(errors="ignore").lower()
            _handle_key(ch)
        time.sleep(0.05)


def _read_key_unix() -> None:
    import termios, tty, select
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
        console.print("[yellow]⏸  paused[/]  press [bold]r[/] to resume, [bold]c[/] to cancel")
    elif ch == "r" and CTRL.paused:
        CTRL.paused = False
        console.print("[green]▶  resumed[/]")
    elif ch == "c":
        CTRL.cancelled = True
        CTRL.paused = False
        console.print("[red]■  cancel requested…[/]")


def start_hotkey_listener() -> None:
    target = _read_key_windows if os.name == "nt" else _read_key_unix
    threading.Thread(target=target, daemon=True).start()


# ---------- banner ----------
BANNER = r"""
 __     __         _____      _        _____                       _                 _           
 \ \   / /        |_   _|    | |      |  __ \                     | |               | |          
  \ \_/ /___  _   _ | | _   _| |__    | |  | | _____      ___ __ | | ___   __ _  __| | ___ _ __ 
   \   // _ \| | | || || | | | '_ \   | |  | |/ _ \ \ /\ / / '_ \| |/ _ \ / _` |/ _` |/ _ \ '__|
    | || (_) | |_| || || |_| | |_) |  | |__| | (_) \ V  V /| | | | | (_) | (_| | (_| |  __/ |   
    |_| \___/ \__,_|___|\__,_|_.__/   |_____/ \___/ \_/\_/ |_| |_|_|\___/ \__,_|\__,_|\___|_|   
"""


def show_banner() -> None:
    console.print(Align.center(Text(BANNER, style="bold magenta")))
    console.print(Align.center(Text("Playlist Downloader • fast • pretty • hotkeys", style="cyan")))
    console.print(Rule(style="magenta"))


# ---------- prompts ----------
def ask_mode() -> str:
    """Ask user what they want to download: single, multiple, or playlist."""
    console.print(Panel.fit(
        "[bold]What do you want to download?[/]\n"
        "  [bold]1[/] Single video\n"
        "  [bold]2[/] Multiple videos (not from a playlist)\n"
        "  [bold]3[/] Playlist",
        title="🎯 Mode", border_style="cyan",
    ))
    choice = Prompt.ask("[bold cyan]▸ Choose[/]", choices=["1", "2", "3"], default="3")
    return {"1": "single", "2": "multiple", "3": "playlist"}[choice]


def ask_media_type() -> str:
    """Ask if the user wants video+audio or audio-only (mp3)."""
    console.print(Panel.fit(
        "[bold]What kind of file do you want?[/]\n"
        "  [bold]1[/] Video + Audio (mp4)\n"
        "  [bold]2[/] Audio only (mp3)",
        title="🎧 Media type", border_style="cyan",
    ))
    choice = Prompt.ask("[bold cyan]▸ Choose[/]", choices=["1", "2"], default="1")
    return "audio" if choice == "2" else "video"



def ask_playlist_url() -> str:
    while True:
        url = Prompt.ask("[bold cyan]▸ YouTube playlist link[/]").strip()
        if url:
            return url
        console.print("[red]Please enter a valid URL.[/]")


def ask_single_url() -> str:
    while True:
        url = Prompt.ask("[bold cyan]▸ YouTube video link[/]").strip()
        if url:
            return url
        console.print("[red]Please enter a valid URL.[/]")


def ask_multiple_urls() -> list[str]:
    """Ask how many videos, then collect that many URLs.

    Sub-mode 1: paste them one-by-one (press Enter after each).
    Sub-mode 2: paste all at once separated by comma, space, or newline.
    """
    while True:
        raw = Prompt.ask("[bold cyan]▸ How many videos?[/]").strip()
        if raw.isdigit() and int(raw) >= 1:
            count = int(raw)
            break
        console.print("[red]Enter a positive number.[/]")

    console.print(Panel.fit(
        "[bold]How do you want to paste the links?[/]\n"
        "  [bold]1[/] One by one (paste a link, press Enter, repeat)\n"
        "  [bold]2[/] All at once (separated by comma, space, or newline)",
        border_style="cyan", title="🔗 Input style",
    ))
    style = Prompt.ask("[bold cyan]▸ Choose[/]", choices=["1", "2"], default="1")

    urls: list[str] = []
    if style == "1":
        for i in range(1, count + 1):
            while True:
                url = Prompt.ask(f"[bold cyan]▸ Link {i}/{count}[/]").strip()
                if url:
                    urls.append(url)
                    break
                console.print("[red]Empty — try again.[/]")
    else:
        console.print(f"[dim]Paste all {count} links (comma / space / newline separated).[/]")
        console.print("[dim]End with an empty line:[/]")
        buf: list[str] = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line == "":
                if buf:
                    break
                continue
            buf.append(line)
        import re
        tokens = [t.strip() for t in re.split(r"[\s,]+", "\n".join(buf)) if t.strip()]
        if len(tokens) < count:
            console.print(f"[yellow]Got {len(tokens)} links, expected {count}. Using what was pasted.[/]")
        elif len(tokens) > count:
            console.print(f"[yellow]Got {len(tokens)} links, using first {count}.[/]")
            tokens = tokens[:count]
        urls = tokens

    if not urls:
        console.print("[red]No links provided.[/]")
        sys.exit(1)
    return urls


def ask_video_count(total: int) -> int:
    while True:
        raw = Prompt.ask(
            f"[bold cyan]▸ How many videos to download?[/] [dim](all or 1-{total})[/]",
            default="all",
        ).strip().lower()
        if raw in ("all", "a", ""):
            return total
        if raw.isdigit():
            n = int(raw)
            if 1 <= n <= total:
                return n
        console.print("[red]Invalid input, try again.[/]")



def _has_chromium_cookie_db(root: Path) -> bool:
    if not root.exists():
        return False
    candidates = [
        root / "Default" / "Network" / "Cookies",
        root / "Default" / "Cookies",
    ]
    candidates.extend(root.glob("Profile */Network/Cookies"))
    candidates.extend(root.glob("Profile */Cookies"))
    return any(p.is_file() for p in candidates)


def _has_firefox_cookie_db(root: Path) -> bool:
    return root.exists() and any(p.is_file() for p in root.glob("*/cookies.sqlite"))


def _detect_browsers() -> list[tuple[str, str]]:
    """Return list of (browser_key, label) whose real cookie DB exists."""
    home = Path.home()
    if sys.platform == "darwin":
        candidates: dict[str, tuple[Path, str]] = {
            "chrome":   (home / "Library/Application Support/Google/Chrome", "chromium"),
            "chromium": (home / "Library/Application Support/Chromium", "chromium"),
            "brave":    (home / "Library/Application Support/BraveSoftware/Brave-Browser", "chromium"),
            "edge":     (home / "Library/Application Support/Microsoft Edge", "chromium"),
            "firefox":  (home / "Library/Application Support/Firefox/Profiles", "firefox"),
            "safari":   (home / "Library/Cookies/Cookies.binarycookies", "file"),
        }
    elif os.name == "nt":
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData/Local"))
        appdata = Path(os.environ.get("APPDATA", home / "AppData/Roaming"))
        candidates = {
            "chrome":   (local / "Google/Chrome/User Data", "chromium"),
            "chromium": (local / "Chromium/User Data", "chromium"),
            "brave":    (local / "BraveSoftware/Brave-Browser/User Data", "chromium"),
            "edge":     (local / "Microsoft/Edge/User Data", "chromium"),
            "firefox":  (appdata / "Mozilla/Firefox/Profiles", "firefox"),
        }
    else:  # linux
        candidates = {
            "chrome":   (home / ".config/google-chrome", "chromium"),
            "chromium": (home / ".config/chromium", "chromium"),
            "brave":    (home / ".config/BraveSoftware/Brave-Browser", "chromium"),
            "edge":     (home / ".config/microsoft-edge", "chromium"),
            "firefox":  (home / ".mozilla/firefox", "firefox"),
        }
    labels = {"chrome": "Chrome", "firefox": "Firefox", "edge": "Edge",
              "brave": "Brave", "safari": "Safari", "chromium": "Chromium"}
    found = []
    for key, (path, kind) in candidates.items():
        if (
            (kind == "chromium" and _has_chromium_cookie_db(path))
            or (kind == "firefox" and _has_firefox_cookie_db(path))
            or (kind == "file" and path.is_file())
        ):
            found.append((key, labels[key]))
    return found


def _validate_netscape_cookie_text(text: str) -> tuple[bool, str]:
    """Validate the tab-separated Netscape cookies.txt format yt-dlp requires."""
    stripped = text.lstrip()
    if not stripped:
        return False, "file is empty"
    if stripped.startswith("{") or stripped.startswith("["):
        return False, "this looks like JSON, not Netscape cookies.txt"

    valid_cookie_rows = 0
    for raw_line in text.splitlines():
        line = raw_line.strip("\n\r")
        if not line.strip() or (line.startswith("#") and not line.startswith("#HttpOnly_")):
            continue
        parts = line.split("\t")
        if len(parts) != 7:
            return False, "cookie rows must have 7 TAB-separated columns"
        if parts[1].upper() not in {"TRUE", "FALSE"} or parts[3].upper() not in {"TRUE", "FALSE"}:
            return False, "cookie TRUE/FALSE columns are invalid"
        valid_cookie_rows += 1

    if valid_cookie_rows == 0:
        return False, "no cookie rows found"
    return True, "ok"


def _validate_netscape_cookie_file(path: Path) -> tuple[bool, str]:
    try:
        return _validate_netscape_cookie_text(path.read_text(encoding="utf-8", errors="replace"))
    except OSError as e:
        return False, str(e)


def _find_local_cookies_txt() -> Path | None:
    """Look for a cookies.txt near the script (auto-pickup)."""
    for name in ("cookies.txt", "youtube.cookies.txt", "yt-cookies.txt"):
        for base in (Path.cwd(), Path(__file__).resolve().parent):
            p = base / name
            if p.is_file():
                return p
    return None


def ask_cookie_source() -> dict:
    """Return yt-dlp options that provide cookies (or {} to skip).

    Auto-detects installed browsers and any local cookies.txt so the user
    isn't offered options that will crash (e.g. Chrome in a Codespace).
    """
    # 1. Auto-pickup only valid cookies.txt next to the script.
    local = _find_local_cookies_txt()
    if local:
        ok, reason = _validate_netscape_cookie_file(local)
        if not ok:
            console.print(Panel(
                f"[yellow]Found:[/] [cyan]{local}[/]\n"
                f"[red]Not using it:[/] {reason}\n\n"
                "Export again as [bold]Netscape cookies.txt[/] using a browser extension like\n"
                "[bold]Get cookies.txt LOCALLY[/], then upload/replace this file.",
                border_style="yellow", title="🍪 Invalid cookies.txt",
            ))
        else:
            console.print(Panel.fit(
                f"[green]✓ Auto-detected valid cookies file:[/] [cyan]{local}[/]",
                border_style="green", title="🍪 Cookies",
            ))
            return {"cookiefile": str(local)}

    # 2. Auto-use browser cookies only when exactly one real cookie DB exists.
    browsers = _detect_browsers()
    if len(browsers) == 1:
        key, label = browsers[0]
        console.print(Panel.fit(
            f"[green]✓ Auto-detected browser cookies:[/] [cyan]{label}[/]",
            border_style="green", title="🍪 Cookies",
        ))
        return {"cookiesfrombrowser": (key,)}

    console.print(Panel.fit(
        "[bold]YouTube often blocks with[/] [red]'Sign in to confirm you're not a bot'[/].\n"
        "Pick a detected browser, provide a valid [bold]Netscape cookies.txt[/], or skip.",
        title="🍪 Cookies", border_style="yellow",
    ))

    table = Table(show_header=False, box=None, padding=(0, 2))
    options: list[tuple[str, dict]] = []  # (label, ydl_opts)
    idx = 1
    for key, label in browsers:
        table.add_row(f"[bold]{idx}[/]", f"{label}  [green](detected)[/]")
        options.append((label, {"cookiesfrombrowser": (key,)}))
        idx += 1
    if not browsers:
        table.add_row("", "[dim]No installed browsers detected on this machine.[/]")
    table.add_row(f"[bold]{idx}[/]", "Path to cookies.txt")
    txt_choice = str(idx); idx += 1
    table.add_row(f"[bold]{idx}[/]", "Paste cookies.txt contents (multi-line, end with blank line)")
    paste_choice = str(idx); idx += 1
    table.add_row(f"[bold]{idx}[/]", "Skip (may fail on YouTube)")
    skip_choice = str(idx)
    console.print(table)

    choices = [str(i) for i in range(1, idx + 1)]
    choice = Prompt.ask("[bold cyan]▸ Cookie source[/]", choices=choices, default=skip_choice)

    if choice == skip_choice:
        return {}

    if choice == txt_choice:
        path = Prompt.ask("[bold cyan]▸ Path to cookies.txt[/]").strip().strip('"').strip("'")
        cookie_path = Path(path)
        if path and cookie_path.is_file():
            ok, reason = _validate_netscape_cookie_file(cookie_path)
            if ok:
                return {"cookiefile": str(cookie_path)}
            console.print(f"[red]Invalid cookies.txt:[/] {reason}")
        else:
            console.print("[red]File not found.[/]")
        console.print("[yellow]Continuing without cookies may fail on YouTube.[/]")
        return {}

    if choice == paste_choice:
        console.print("[dim]Paste cookies.txt below. End with an empty line:[/]")
        lines = []
        while True:
            try:
                line = input()
            except EOFError:
                break
            if line == "":
                break
            lines.append(line)
        if not lines:
            console.print("[red]Nothing pasted — continuing without cookies.[/]")
            return {}
        ok, reason = _validate_netscape_cookie_text("\n".join(lines) + "\n")
        if not ok:
            console.print(f"[red]Invalid paste:[/] {reason}")
            console.print("[yellow]Use a Netscape cookies.txt export, not copied browser DevTools/JSON cookies.[/]")
            return {}
        out = Path.cwd() / "cookies.txt"
        out.write_text("\n".join(lines) + "\n", encoding="utf-8")
        console.print(f"[green]✓ Saved to[/] [cyan]{out}[/]")
        return {"cookiefile": str(out)}

    # Browser option
    _, opts = options[int(choice) - 1]
    return opts


def _is_codespaces() -> bool:
    return bool(os.environ.get("CODESPACES") or os.environ.get("CODESPACE_NAME"))


def _has_display() -> bool:
    if os.name == "nt" or sys.platform == "darwin":
        return True
    return bool(os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _try_native_picker() -> str | None:
    try:
        import tkinter as tk
        from tkinter import filedialog
        root = tk.Tk()
        root.withdraw()
        root.attributes("-topmost", True)
        console.print("[dim]Opening folder picker…[/]")
        chosen = filedialog.askdirectory(title="Select download folder")
        root.destroy()
        return chosen or None
    except Exception:
        return None


def ask_save_folder(default_name: str) -> str:
    """Pick a folder via menu (no path typing needed).

    On desktop with a display we still try the native picker first.
    On Codespaces / headless servers we show a numbered menu of common
    locations and let the user pick.
    """
    chosen = None
    if _has_display() and not _is_codespaces():
        chosen = _try_native_picker()

    if not chosen:
        home = Path.home()
        cwd = Path.cwd()
        candidates: list[tuple[str, Path]] = []
        seen: set[str] = set()

        def add(label: str, path: Path) -> None:
            key = str(path)
            if key in seen:
                return
            seen.add(key)
            candidates.append((label, path))

        add("Project downloads/",     cwd / "downloads")
        add("Home ~/Downloads",       home / "Downloads")
        add("Home ~/Videos",          home / "Videos")
        add("Home ~/Music",           home / "Music")
        add("Home directory",         home)
        add("Current folder",         cwd)
        add("System /tmp",            Path("/tmp") / "yt-downloads")

        console.print(Panel.fit(
            "[bold]Pick a folder[/] (no typing needed).\n"
            + ("[dim]On Codespaces, files save inside the container — "
               "see the auto-download step next.[/]" if _is_codespaces() else ""),
            title="📁 Save location", border_style="cyan",
        ))
        table = Table(show_header=False, box=None, padding=(0, 2))
        for i, (label, path) in enumerate(candidates, 1):
            exists = "[green](exists)[/]" if path.exists() else "[dim](will create)[/]"
            table.add_row(f"[bold]{i}[/]", f"{label}  [dim]{path}[/]  {exists}")
        table.add_row(f"[bold]{len(candidates) + 1}[/]", "Type a custom path")
        console.print(table)

        choices = [str(i) for i in range(1, len(candidates) + 2)]
        pick = Prompt.ask("[bold cyan]▸ Choose[/]", choices=choices, default="1")
        if int(pick) == len(candidates) + 1:
            chosen = Prompt.ask("[bold cyan]▸ Custom folder path[/]",
                                default=str(cwd / "downloads")).strip()
        else:
            chosen = str(candidates[int(pick) - 1][1])

    safe = "".join(c for c in default_name if c.isalnum() or c in " -_").strip() or "playlist"
    out = os.path.join(chosen, safe)
    os.makedirs(out, exist_ok=True)
    return out


# ---------- Codespaces auto-download to browser ----------
class BrowserBridge:
    """Serves the downloads folder over HTTP so you can grab files from your
    local browser when running on Codespaces / a remote container."""
    def __init__(self) -> None:
        self.enabled = False
        self.port = 0
        self.root = ""
        self.base_url = ""
        self._server = None
        self._thread = None
        self.files: list[str] = []  # final downloaded file paths

    def start(self, root_dir: str, port: int = 8000) -> None:
        import http.server, socketserver, socket, functools
        # find a free port starting at requested one
        for p in range(port, port + 20):
            try:
                with socket.socket() as s:
                    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
                    s.bind(("0.0.0.0", p))
                self.port = p
                break
            except OSError:
                continue
        else:
            raise RuntimeError("no free port")

        base_handler = functools.partial(http.server.SimpleHTTPRequestHandler,
                                         directory=root_dir)

        class QuietHandler(base_handler.func):  # type: ignore[misc]
            def log_message(self, *a, **k): pass
            def handle_one_request(self):
                try:
                    super().handle_one_request()
                except (BrokenPipeError, ConnectionResetError):
                    # Browser cancelled / paused the download — normal, ignore.
                    self.close_connection = True
            def copyfile(self, src, dst):
                try:
                    super().copyfile(src, dst)
                except (BrokenPipeError, ConnectionResetError):
                    pass

        class ReuseServer(socketserver.ThreadingTCPServer):
            allow_reuse_address = True
            daemon_threads = True

        server = ReuseServer(("0.0.0.0", self.port), QuietHandler)
        self._server = server
        self._thread = threading.Thread(target=server.serve_forever, daemon=True)
        self._thread.start()

        cs_name = os.environ.get("CODESPACE_NAME")
        domain = os.environ.get("GITHUB_CODESPACES_PORT_FORWARDING_DOMAIN", "app.github.dev")
        if cs_name:
            self.base_url = f"https://{cs_name}-{self.port}.{domain}"
        else:
            self.base_url = f"http://localhost:{self.port}"
        self.root = root_dir
        self.enabled = True

    def url_for(self, filepath: str) -> str:
        try:
            rel = os.path.relpath(filepath, self.root).replace(os.sep, "/")
        except ValueError:
            rel = os.path.basename(filepath)
        from urllib.parse import quote
        return f"{self.base_url}/{quote(rel)}"


BRIDGE = BrowserBridge()


def ask_browser_download(out_dir: str) -> None:
    """Offer to expose downloads over HTTP so the browser can save them locally."""
    if not _is_codespaces():
        return
    console.print(Panel.fit(
        "You're on [bold]Codespaces[/] — files save inside the container, not your PC.\n"
        "I can start a mini web server so each finished video shows a "
        "[bold]clickable link[/] that downloads it to your PC's Downloads folder.",
        title="🌐 Auto-download to your PC", border_style="cyan",
    ))
    if Prompt.ask("[bold cyan]▸ Enable auto-download links?[/]",
                  choices=["y", "n"], default="y") != "y":
        return
    try:
        # Serve the PARENT of out_dir so URLs stay short and playlist folder is browsable
        # Serve from project root so links like /downloads/... AND /NCS.../ both work.
        BRIDGE.start(os.getcwd(), port=8000)
        # Try to auto-set the port to Public via the gh CLI (pre-installed in Codespaces).
        # Without this the URL returns 404 unless the same browser is signed into GitHub.
        made_public = False
        cs_name = os.environ.get("CODESPACE_NAME")
        if cs_name and shutil.which("gh"):
            import subprocess
            try:
                subprocess.run(
                    ["gh", "codespace", "ports", "visibility",
                     f"{BRIDGE.port}:public", "-c", cs_name],
                    capture_output=True, timeout=15, check=True,
                )
                made_public = True
            except Exception:
                made_public = False
        visibility_line = (
            "[green]✓ Port set to Public — links work in any browser.[/]"
            if made_public else
            f"[yellow]⚠ Port is Private.[/] Open the [bold]PORTS[/] tab, right-click "
            f"port {BRIDGE.port} → [bold]Port Visibility → Public[/], or run:\n"
            f"  [cyan]gh codespace ports visibility {BRIDGE.port}:public -c $CODESPACE_NAME[/]"
        )
        console.print(Panel.fit(
            f"[green]✓ Server running:[/] [cyan]{BRIDGE.base_url}[/]\n"
            f"{visibility_line}",
            title="🌐 Browser bridge ready", border_style="green",
        ))
    except Exception as e:
        console.print(f"[yellow]Could not start browser bridge:[/] {e}")


def collect_available_resolutions(entries) -> list[int]:
    heights = set()
    for entry in entries:
        if not entry:
            continue
        for f in entry.get("formats", []) or []:
            h = f.get("height")
            if h and f.get("vcodec") and f.get("vcodec") != "none":
                heights.add(h)
    return sorted(heights)


def _looks_like_bot_challenge(error: Exception | str) -> bool:
    msg = str(error).lower()
    return any(needle in msg for needle in (
        "sign in to confirm",
        "not a bot",
        "use --cookies-from-browser",
        "use --cookies",
    ))


def _print_cookie_help(title: str = "YouTube needs cookies") -> None:
    console.print(Panel(
        "YouTube is asking for a logged-in browser session.\n\n"
        "[bold]Fix:[/] export cookies from the browser where YouTube is logged in:\n"
        "  1. Install the [bold]Get cookies.txt LOCALLY[/] extension.\n"
        "  2. Open youtube.com while logged in.\n"
        "  3. Export as [bold]Netscape cookies.txt[/].\n"
        "  4. Put it here as [cyan]cookies.txt[/] and run again.\n\n"
        "[dim]A server/Codespace cannot create YouTube login cookies by itself.[/]",
        border_style="red", title=f"⚠ {title}",
    ))


def ask_resolution(available: list[int]) -> int:
    if not available:
        console.print("[yellow]No resolutions detected — using best available.[/]")
        return 0
    table = Table(title="Available Resolutions", header_style="bold magenta", border_style="magenta")
    table.add_column("#", style="bold cyan", justify="right")
    table.add_column("Resolution", style="green")
    for i, h in enumerate(available, 1):
        table.add_row(str(i), f"{h}p")
    table.add_row(str(len(available) + 1), "Best available ⭐")
    console.print(table)
    while True:
        raw = Prompt.ask(
            "[bold cyan]▸ Select resolution[/]",
            default=str(len(available) + 1),
        ).strip()
        if raw.isdigit():
            idx = int(raw)
            if 1 <= idx <= len(available):
                return available[idx - 1]
            if idx == len(available) + 1:
                return 0
        console.print("[red]Invalid choice.[/]")


# ---------- progress bar ----------
class ProgressState:
    def __init__(self) -> None:
        self.progress: Progress | None = None
        self.task_id = None
        self.current_file = ""


PSTATE = ProgressState()


def progress_hook(d: dict) -> None:
    while CTRL.paused and not CTRL.cancelled:
        time.sleep(0.2)
    if CTRL.cancelled:
        raise CancelledByUser()

    if not PSTATE.progress:
        return

    status = d.get("status")
    if status == "downloading":
        total = d.get("total_bytes") or d.get("total_bytes_estimate") or 0
        done = d.get("downloaded_bytes") or 0
        speed = d.get("speed") or 0
        filename = os.path.basename(d.get("filename") or "")
        if filename and filename != PSTATE.current_file:
            PSTATE.current_file = filename
            short = filename if len(filename) < 60 else filename[:57] + "…"
            PSTATE.progress.update(PSTATE.task_id, description=f"[cyan]{short}")
        if total:
            PSTATE.progress.update(
                PSTATE.task_id, completed=done, total=total, speed=speed,
            )
    elif status == "finished":
        PSTATE.progress.update(PSTATE.task_id, description="[green]merging…")
        # NOTE: don't print a download link here — 'filename' is a temporary
        # fragment (.f398.mp4 / .f774.webm) that gets deleted after merging.
        # We print the link from postprocessor_hook once the final .mp4 exists.


def postprocessor_hook(d: dict) -> None:
    """Fires after each postprocessor. Collects the final on-disk file path
    so we can bundle everything into a single ZIP at the end."""
    if not BRIDGE.enabled:
        return
    if d.get("status") != "finished":
        return
    pp = d.get("postprocessor") or ""
    info = d.get("info_dict") or {}
    fp = info.get("filepath") or info.get("_filename")
    if not fp or not os.path.exists(fp):
        return
    if pp and pp not in ("MoveFiles", "Merger", "FFmpegVideoConvertor",
                         "FFmpegVideoRemuxer", "FFmpegMerger"):
        return
    if fp in BRIDGE.files:
        return
    BRIDGE.files.append(fp)
    name = os.path.basename(fp)
    console.print(f"  [green]✓[/] {name}")



# ---------- main ----------
def main() -> None:
    show_banner()

    mode = ask_mode()  # "single" | "multiple" | "playlist"
    media_type = ask_media_type()  # "video" | "audio"
    cookie_opts = ask_cookie_source()

    # -------- gather URLs + a display title based on mode --------
    if mode == "playlist":
        playlist_url = ask_playlist_url()

        def _extract(opts_extra):
            with YoutubeDL({"quiet": True, "extract_flat": True, "skip_download": True, **opts_extra}) as ydl:
                return ydl.extract_info(playlist_url, download=False)

        with console.status("[cyan]Fetching playlist info…[/]", spinner="dots"):
            try:
                info = _extract(cookie_opts)
            except Exception as e:
                msg = str(e)
                if cookie_opts and "cookie" in msg.lower():
                    console.print(f"[yellow]⚠ Cookie load failed:[/] {msg.splitlines()[0]}")
                    console.print("[yellow]Retrying without cookies…[/]")
                    cookie_opts = {}
                    info = _extract(cookie_opts)
                else:
                    raise

        entries = info.get("entries") or []
        if not entries:
            console.print("[red]No videos found. Is this a valid playlist URL?[/]")
            sys.exit(1)

        total = len(entries)
        title = info.get("title") or "playlist"
        console.print(Panel.fit(
            f"[bold white]{title}[/]\n[dim]{total} videos found[/]",
            border_style="green", title="📃 Playlist",
        ))
        count = ask_video_count(total)
        selected = entries[:count]

        first_url = selected[0].get("url") or selected[0].get("webpage_url")
        if first_url and not first_url.startswith("http"):
            first_url = f"https://www.youtube.com/watch?v={first_url}"
        download_targets = [playlist_url]
    else:
        if mode == "single":
            urls = [ask_single_url()]
            title = "video"
        else:
            urls = ask_multiple_urls()
            title = "videos"
        count = len(urls)
        first_url = urls[0]
        download_targets = urls

    chosen = 0
    if media_type == "video":
        console.print("[cyan]Probing available resolutions…[/]")
        resolutions: list[int] = []
        try:
            with YoutubeDL({"quiet": True, "skip_download": True, **cookie_opts}) as ydl:
                probe = ydl.extract_info(first_url, download=False)
            resolutions = collect_available_resolutions([probe])
        except DownloadError as e:
            if _looks_like_bot_challenge(e):
                _print_cookie_help("Format probe blocked")
                return
            console.print(Panel(
                f"[red]Could not probe formats:[/]\n{e}\n\n"
                "[yellow]Tip:[/] pick a browser cookie source if you skipped it, "
                "and install [bold]deno[/] so yt-dlp can run YouTube's JS challenge.",
                border_style="red", title="⚠ Format probe failed",
            ))
            if not Prompt.ask("Continue with 'best available' anyway?", choices=["y", "n"], default="y") == "y":
                return
        chosen = ask_resolution(resolutions)

    out_dir = ask_save_folder(title)
    ask_browser_download(out_dir)

    if media_type == "audio":
        fmt = "bestaudio/best"
    elif chosen == 0:
        fmt = "bestvideo+bestaudio/best"
    else:
        fmt = f"bestvideo[height<={chosen}]+bestaudio/best[height<={chosen}]"

    if mode == "playlist":
        outtmpl = os.path.join(out_dir, "%(playlist_index)s - %(title)s.%(ext)s")
    else:
        outtmpl = os.path.join(out_dir, "%(title)s.%(ext)s")

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
        "progress_hooks": [progress_hook],
        "postprocessor_hooks": [postprocessor_hook],
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
        ydl_opts["playlist_items"] = f"1-{count}"
    else:
        ydl_opts["noplaylist"] = True

    # aria2c: YouTube's CDN rate-limits high per-host connection counts
    # (503 errors + aria2c exit 29). Keep it modest and retry aggressively
    # so a single failed fragment doesn't leave you with video-but-no-audio.
    if shutil.which("aria2c"):
        ydl_opts["external_downloader"] = "aria2c"
        ydl_opts["external_downloader_args"] = [
            "-x", "4", "-s", "4", "-k", "1M",
            "--max-tries=10", "--retry-wait=3",
            "--max-connection-per-server=4",
            "--console-log-level=warn", "--summary-interval=0",
            "--allow-overwrite=true", "--auto-file-renaming=false",
        ]
        console.print("[green]✓ aria2c detected[/] — using multi-connection downloader.")



    quality_label = "audio (mp3)" if media_type == "audio" else f"{chosen}p" if chosen else "best"
    console.print(Panel.fit(
        f"[bold]Saving to:[/] [green]{out_dir}[/]\n"
        f"[bold]Mode:[/] {mode}   [bold]Type:[/] {media_type}   [bold]Items:[/] {count}   "
        f"[bold]Quality:[/] {quality_label}   "
        f"[bold]Cookies:[/] {'yes' if cookie_opts else 'no'}\n"
        "[dim]Hotkeys:[/]  [bold]p[/] pause   [bold]r[/] resume   [bold]c[/] cancel",
        border_style="cyan", title="⬇ Download",
    ))

    start_hotkey_listener()

    PSTATE.progress = Progress(
        SpinnerColumn(style="magenta"),
        TextColumn("{task.description}"),
        BarColumn(bar_width=None, complete_style="green", finished_style="bright_green"),
        TextColumn("[bold]{task.percentage:>5.1f}%[/]"),
        DownloadColumn(),
        TransferSpeedColumn(),
        TimeRemainingColumn(),
        console=console,
        transient=False,
        expand=True,
    )

    try:
        with PSTATE.progress:
            PSTATE.task_id = PSTATE.progress.add_task("[cyan]starting…", total=1)
            with YoutubeDL(ydl_opts) as ydl:
                ydl.download(download_targets)
    except CancelledByUser:
        console.print("[red]■ Download cancelled.[/]")
        return
    except DownloadError as e:
        if _looks_like_bot_challenge(e):
            _print_cookie_help("Download blocked")
            return
        console.print(f"[red]Download error:[/] {e}")
        return

    if CTRL.cancelled:
        console.print("[red]■ Cancelled.[/]")
    else:
        console.print(Panel.fit(
            f"[bold green]✓ All done![/]\nSaved to [cyan]{out_dir}[/]",
            border_style="green",
        ))

    if BRIDGE.enabled:
        files = [f for f in BRIDGE.files if os.path.exists(f)]
        if not files and os.path.isdir(out_dir):
            for root, _, fnames in os.walk(out_dir):
                for fn in fnames:
                    if not fn.endswith((".part", ".ytdl")):
                        files.append(os.path.join(root, fn))

        if mode == "single":
            # No ZIP for a single video — direct link only.
            if files:
                lines = []
                for fp in files:
                    size_mb = os.path.getsize(fp) / (1024 * 1024)
                    link = BRIDGE.url_for(fp)
                    lines.append(
                        f"[white]{os.path.basename(fp)}[/] [dim]({size_mb:.1f} MB)[/]\n"
                        f"  [cyan][link={link}]{link}[/link][/]"
                    )
                console.print(Panel.fit(
                    "[bold]⬇ Click to download to your PC:[/]\n" + "\n".join(lines),
                    title="🎁 Your video", border_style="green",
                ))
            else:
                console.print("[yellow]No file found to link.[/]")
        else:
            # Playlist / multiple: bundle into one ZIP.
            if files:
                import zipfile
                zip_name = os.path.basename(out_dir.rstrip(os.sep)) or "downloads"
                zip_path = os.path.join(os.path.dirname(out_dir) or ".", f"{zip_name}.zip")
                try:
                    with console.status(f"[cyan]Packing {len(files)} file(s) into ZIP…[/]", spinner="dots"):
                        with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_STORED, allowZip64=True) as zf:
                            for fp in files:
                                arc = os.path.relpath(fp, os.path.dirname(out_dir) or ".")
                                zf.write(fp, arcname=arc)
                    zip_link = BRIDGE.url_for(zip_path)
                    size_mb = os.path.getsize(zip_path) / (1024 * 1024)

                    deleted = 0
                    for fp in files:
                        try:
                            os.remove(fp); deleted += 1
                        except OSError:
                            pass
                    if os.path.isdir(out_dir):
                        for root, dirs, _ in os.walk(out_dir, topdown=False):
                            for d in dirs:
                                try: os.rmdir(os.path.join(root, d))
                                except OSError: pass
                        try: os.rmdir(out_dir)
                        except OSError: pass

                    console.print(Panel.fit(
                        f"[bold green]📦 ZIP ready:[/] [white]{os.path.basename(zip_path)}[/] "
                        f"[dim]({size_mb:.1f} MB, {len(files)} files)[/]\n"
                        f"[dim]🗑  Deleted {deleted} original file(s) — only the ZIP remains.[/]\n"
                        f"[bold]⬇ Click to download all at once:[/]\n"
                        f"  [cyan][link={zip_link}]{zip_link}[/link][/]",
                        title="🎁 One-click download", border_style="green",
                    ))
                except Exception as e:
                    console.print(f"[yellow]⚠ Could not build ZIP:[/] {e}")
            else:
                console.print("[yellow]No files to zip.[/]")

        console.print(Panel.fit(
            f"[bold]🌐 Server running:[/] [cyan]{BRIDGE.base_url}[/]\n"
            "Click the link(s) above to save to your PC.\n\n"
            "[yellow]Press Enter (or Ctrl+C) to stop the server and exit.[/]",
            title="⏳ Waiting — server open", border_style="cyan",
        ))
        try:
            input()
        except (KeyboardInterrupt, EOFError):
            pass
        console.print("[dim]Shutting down server…[/]")




if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        CTRL.cancelled = True
        console.print("\n[red]Interrupted.[/]")
