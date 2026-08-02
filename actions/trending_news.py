"""
trending_news.py — Trending news feature for Parker.

When the user asks about "trending news" / "tin tức nổi bật", Parker:
1. Fetches latest headlines from 4 Vietnamese RSS feeds (VnExpress, TuoiTre,
   ThanhNien, DanTri).
2. Summarizes the top stories using the AI text model.
3. Opens 4 browser tabs (one per source) in Windows Snap Layout (2x2 grid).
4. Reads each summary aloud via TTS (Edge TTS online, Piper offline fallback).
5. After reading all summaries, closes all 4 tabs.

Requires:
- Internet (for RSS feeds + Edge TTS, or offline with Piper fallback).
- A browser (Edge/Chrome) for the tab display.
- On Windows, uses Snap Layout for 2x2 grid; on other OS, opens 4 tabs normally.
"""

import asyncio
import re
import subprocess
import threading
import time
import urllib.request
from pathlib import Path
from xml.etree import ElementTree

# ── RSS feeds ──────────────────────────────────────────────────────────────────
_FEEDS = [
    ("VnExpress",   "https://vnexpress.net/rss/tin-moi-nhat.rss"),
    ("TuoiTre",     "https://tuoitre.vn/rss/tin-moi-nhat.rss"),
    ("ThanhNien",   "https://thanhnien.vn/rss/home.rss"),
    ("DanTri",      "https://dantri.com.vn/rss/home.rss"),
]

# Real homepage URLs for the 4-panel display (RSS URLs aren't browsable pages).
_HOMEPAGES = {
    "VnExpress": "https://vnexpress.net/",
    "TuoiTre":   "https://tuoitre.vn/",
    "ThanhNien": "https://thanhnien.vn/",
    "DanTri":    "https://dantri.com.vn/",
}

# Max stories per feed to include in the summary.
_MAX_PER_FEED = 3


# ── Fetch RSS ──────────────────────────────────────────────────────────────────
def _fetch_feed(name: str, url: str, max_items: int = _MAX_PER_FEED) -> list[dict]:
    """Fetch and parse an RSS feed, returning [{title, link, summary}]."""
    try:
        req = urllib.request.Request(url, headers={
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                          "AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"
        })
        with urllib.request.urlopen(req, timeout=10) as resp:
            raw = resp.read().decode("utf-8", errors="ignore")
        root = ElementTree.fromstring(raw)
        items = []
        # RSS 2.0: //channel/item
        for item in root.findall(".//item")[:max_items]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = (item.findtext("description") or "").strip()
            # Strip HTML tags from description.
            desc = re.sub(r"<[^>]+>", "", desc).strip()
            if title:
                items.append({"title": title, "link": link, "summary": desc[:200]})
        return items
    except Exception as e:
        print(f"[TrendingNews] Failed to fetch {name}: {e}")
        return []


def _fetch_all() -> dict[str, list[dict]]:
    """Fetch headlines from all 4 feeds. Returns {source_name: [items]}."""
    result = {}
    for name, url in _FEEDS:
        items = _fetch_feed(name, url)
        if items:
            result[name] = items
    return result


# ── AI Summarize ───────────────────────────────────────────────────────────────
def _summarize(all_news: dict[str, list[dict]]) -> dict[str, str]:
    """Use the text model to create a short spoken summary per source.
    Returns {source_name: summary_text}."""
    summaries = {}

    # Build a combined prompt for all sources.
    combined_input = ""
    for source, items in all_news.items():
        combined_input += f"\n## {source}\n"
        for i, item in enumerate(items, 1):
            combined_input += f"{i}. {item['title']}. {item.get('summary', '')}\n"

    prompt = (
        "You are a news anchor. Given these Vietnamese news headlines, "
        "write a natural spoken summary for EACH source separately. "
        "For each source, write 2-3 sentences. Use English. "
        "Be concise and engaging. Format:\n"
        "SOURCE: <name>\n<summary>\n\n"
        "Do NOT add any headers or markdown. Just the summaries.\n\n"
        f"HEADLINES:\n{combined_input}"
    )

    # Try the text model (online or local).
    try:
        # Online path: use the configured text model.
        import json as _json
        config_path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
        api_key = _json.loads(config_path.read_text(encoding="utf-8")).get("gemini_api_key", "")
        if api_key:
            from google import genai
            client = genai.Client(
                api_key=api_key,
                http_options={"timeout": 20_000})  # ms — don't hang on a slow API
            resp = client.models.generate_content(
                model="gemini-flash-latest", contents=prompt)
            raw = (resp.text or "").strip()
        else:
            raise RuntimeError("no API key")
    except Exception:
        # Offline path: use Ollama local model.
        try:
            from core.llm_client import call_llm, ensure_ollama_running
            if ensure_ollama_running():
                messages = [{"role": "user", "content": prompt}]
                first = call_llm(messages, tools=None, timeout=60)
                raw = first.get("content") or ""
            else:
                raw = ""
        except Exception as e:
            print(f"[TrendingNews] Summarize failed: {e}")
            raw = ""

    # Parse the response into per-source summaries.
    if not raw:
        # Fallback: just concatenate the titles.
        for source, items in all_news.items():
            titles = "; ".join(item["title"] for item in items)
            summaries[source] = f"From {source}: {titles}"
        return summaries

    # Parse "SOURCE: <name>\n<summary>" blocks.
    blocks = re.split(r"(?i)source:\s*", raw)
    for block in blocks:
        block = block.strip()
        if not block:
            continue
        lines = block.split("\n", 1)
        source_name = lines[0].strip()
        summary_text = lines[1].strip() if len(lines) > 1 else source_name
        # Match to our known sources.
        for known in all_news:
            if known.lower() in source_name.lower() or source_name.lower() in known.lower():
                summaries[known] = summary_text
                break
    # Fill any missing sources with titles.
    for source, items in all_news.items():
        if source not in summaries:
            titles = "; ".join(item["title"] for item in items)
            summaries[source] = f"From {source}: {titles}"

    return summaries


# ── TTS ────────────────────────────────────────────────────────────────────────
# Network call to Microsoft's edge-tts service has no built-in timeout, so a
# slow/blocked connection (common issue in some regions/networks) would hang
# the whole feature indefinitely. Cap it hard.
_EDGE_TTS_TIMEOUT = 15


def _speak_edge(text: str, voice: str = "vi-VN-HoaiMyNeural") -> bool:
    """Speak text using Edge TTS (online, high quality Vietnamese voice).
    Returns True if successful. Never blocks longer than _EDGE_TTS_TIMEOUT for
    the network part."""
    try:
        import edge_tts
        import sounddevice as sd
        import numpy as np

        async def _gen():
            communicate = edge_tts.Communicate(text, voice)
            audio_data = b""
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_data += chunk["data"]
            return audio_data

        async def _gen_with_timeout():
            return await asyncio.wait_for(_gen(), timeout=_EDGE_TTS_TIMEOUT)

        try:
            audio_bytes = asyncio.run(_gen_with_timeout())
        except asyncio.TimeoutError:
            print(f"[TrendingNews] Edge TTS timed out after {_EDGE_TTS_TIMEOUT}s")
            return False
        if not audio_bytes:
            return False

        # Decode MP3 without shelling out to ffmpeg (which may not be
        # installed) — pydub uses audioop/ffmpeg too, so prefer a pure-Python
        # decode via soundfile/miniaudio if available; otherwise skip Edge TTS
        # cleanly instead of hanging on a missing external tool.
        try:
            import soundfile as sf
            import io as _io
            data, sr = sf.read(_io.BytesIO(audio_bytes), dtype="int16")
            sd.play(data, sr)
            sd.wait()
            return True
        except ImportError:
            print("[TrendingNews] soundfile not installed — skipping Edge TTS "
                  "playback (pip install soundfile for online Vietnamese TTS).")
            return False
        except Exception as e:
            print(f"[TrendingNews] Edge TTS decode/playback failed: {e}")
            return False
    except Exception as e:
        print(f"[TrendingNews] Edge TTS failed: {e}")
        return False


def _speak_piper(text: str) -> bool:
    """Speak text using Piper TTS (offline, neural voice)."""
    try:
        from piper import PiperVoice
        import io, wave
        import numpy as np
        import sounddevice as sd
        voice_dir = Path.home() / ".parker" / "piper"
        onnx = voice_dir / "en_US-ryan-medium.onnx"
        if not onnx.exists():
            return False
        voice = PiperVoice.load(str(onnx))
        buf = io.BytesIO()
        with wave.open(buf, "wb") as wf:
            voice.synthesize_wav(text, wf)
        buf.seek(0)
        with wave.open(buf, "rb") as wf:
            sr = wf.getframerate()
            frames = wf.readframes(wf.getnframes())
        audio = np.frombuffer(frames, dtype=np.int16)
        sd.play(audio, sr)
        sd.wait()
        return True
    except Exception as e:
        print(f"[TrendingNews] Piper TTS failed: {e}")
        return False


def _speak(text: str) -> None:
    """Speak text using the best available TTS engine."""
    if not text or not text.strip():
        return
    # Try Edge TTS first (online, high quality Vietnamese).
    if _speak_edge(text):
        return
    # Fallback to Piper (offline).
    _speak_piper(text)


def _find_windows_browser() -> str | None:
    """Find a real path to msedge.exe or chrome.exe on Windows. subprocess.Popen
    with a bare 'msedge'/'chrome' name only works if it's on PATH — which it
    usually ISN'T for these (they live under Program Files) — so that silently
    raised FileNotFoundError and no tabs ever opened. Search known install
    locations instead, same approach as open_app.py."""
    import os
    import shutil

    # If it happens to be on PATH, that's the easiest case.
    for name in ("msedge.exe", "msedge", "chrome.exe", "chrome"):
        found = shutil.which(name)
        if found:
            return found

    candidates = []
    pf = os.environ.get("ProgramFiles", r"C:\Program Files")
    pf86 = os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)")
    localapp = os.environ.get("LocalAppData", "")
    candidates += [
        Path(pf86) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(pf) / "Microsoft" / "Edge" / "Application" / "msedge.exe",
        Path(pf) / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(pf86) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    if localapp:
        candidates.append(
            Path(localapp) / "Google" / "Chrome" / "Application" / "chrome.exe")

    for c in candidates:
        if c.exists():
            return str(c)
    return None


# ── Tab Layout (Windows Snap) ─────────────────────────────────────────────────
def _open_tabs_snap_layout(urls: list[str]) -> list:
    """Open 4 browser tabs and snap them into a 2x2 grid on Windows.
    Returns list of PIDs for later cleanup."""
    import platform
    if platform.system() != "Windows":
        # On non-Windows, just open tabs normally.
        import webbrowser
        for url in urls:
            webbrowser.open(url)
        return []

    browser_path = _find_windows_browser()
    if not browser_path:
        print("[TrendingNews] No Edge/Chrome found — falling back to default "
              "browser via webbrowser.open (no snap layout).")
        import webbrowser
        for url in urls:
            webbrowser.open(url)
        return []

    # PowerShell helper: restore-if-maximized, then move+resize the CURRENT
    # foreground window into one of 4 screen quadrants. Loading
    # System.Windows.Forms before compiling SnapHelper matters — it's
    # referenced inside the C# code, and Add-Type throws if the assembly
    # isn't loaded first.
    _ps_helper = """
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class SnapHelper {
    [DllImport("user32.dll")] public static extern bool SetWindowPos(
        IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    [DllImport("user32.dll")] public static extern bool ShowWindow(
        IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool IsZoomed(IntPtr hWnd);
    const int SW_RESTORE = 9;

    public static void SnapToQuadrant(int quadrant) {
        var screen = System.Windows.Forms.Screen.PrimaryScreen.Bounds;
        int sw = screen.Width, sh = screen.Height;
        int x, y, w, h;
        int gap = 4;
        switch (quadrant) {
            case 0: x = 0; y = 0; w = sw / 2 - gap; h = sh / 2 - gap; break;
            case 1: x = sw / 2 + gap; y = 0; w = sw / 2 - gap; h = sh / 2 - gap; break;
            case 2: x = 0; y = sh / 2 + gap; w = sw / 2 - gap; h = sh / 2 - gap; break;
            default: x = sw / 2 + gap; y = sh / 2 + gap; w = sw / 2 - gap; h = sh / 2 - gap; break;
        }
        IntPtr hWnd = GetForegroundWindow();
        // A freshly-opened browser window is usually MAXIMIZED — SetWindowPos
        // silently does nothing to a maximized window.
        if (IsZoomed(hWnd)) { ShowWindow(hWnd, SW_RESTORE); }
        SetWindowPos(hWnd, IntPtr.Zero, x, y, w, h, 0x0040);
    }
}
"@
[SnapHelper]::SnapToQuadrant({quadrant})
"""

    pids = []
    for i, url in enumerate(urls[:4]):
        try:
            proc = subprocess.Popen(
                [browser_path, "--new-window", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            pids.append(proc.pid)
        except Exception as e:
            print(f"[TrendingNews] Failed to open tab {url}: {e}")
            continue

        # Snap it RIGHT AFTER opening, while it's still the foreground window.
        # (Relying on the launcher PID to find the window later doesn't work:
        # Chrome/Edge often hand off to an already-running instance and the
        # launcher process exits immediately, or spawns child processes, so
        # Get-Process -Id <launcher pid> finds no window at all — the snap
        # silently never fired.)
        time.sleep(1.2)  # let the window actually appear and gain focus
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command",
                 _ps_helper.replace("{quadrant}", str(i))],
                capture_output=True, timeout=10, text=True,
            )
            if r.returncode != 0:
                print(f"[TrendingNews] Snap PS error for tab {i}: {r.stderr[:300]}")
        except Exception as e:
            print(f"[TrendingNews] Snap failed for window {i}: {e}")

    return pids


def _close_tabs(pids: list) -> None:
    """Close browser tabs by PID."""
    for pid in pids:
        try:
            subprocess.run(["taskkill", "/F", "/PID", str(pid)],
                           capture_output=True, timeout=5)
        except Exception:
            pass


# ── Main entry point ──────────────────────────────────────────────────────────
def trending_news(parameters: dict = None, player=None,
                   session_memory=None) -> str:
    """Fetch trending Vietnamese news, summarize with AI, display in 4-panel
    layout, read summaries aloud, then close all tabs.

    This is a blocking operation (may take 15-30 seconds for fetch + TTS).
    """
    player = player  # unused but keeps signature consistent

    # 1. Fetch RSS from all 4 sources.
    if player:
        player.write_log("SYS: Fetching trending news from4 sources...")
    all_news = _fetch_all()
    if not all_news:
        return "Sir, I couldn't fetch any news feeds right now. Try again later."

    total = sum(len(v) for v in all_news.values())
    sources = ", ".join(all_news.keys())

    # 2. Summarize with AI.
    if player:
        player.write_log(f"SYS: Summarizing {total} stories from {sources}...")
    summaries = _summarize(all_news)

    # 3. Build URLs for the 4 tabs (real homepages, not RSS feed URLs).
    urls = [_HOMEPAGES[name] for name, _url in _FEEDS]

    # 4. Open 4 tabs in snap layout.
    if player:
        player.write_log("SYS: Opening4 news tabs in grid layout...")
    pids = _open_tabs_snap_layout(urls)

    # 5. Read summaries aloud (one by one).
    combined_text = ""
    for source in ["VnExpress", "TuoiTre", "ThanhNien", "DanTri"]:
        summary = summaries.get(source, f"No headlines from {source}.")
        combined_text += f"{source}. {summary} "

    if player:
        player.write_log(f"SYS: Reading {len(summaries)} news summaries...")

    # Run TTS in a thread so we don't block the UI.
    t = threading.Thread(target=_speak, args=(combined_text,), daemon=True)
    t.start()
    t.join(timeout=120)  # Max 2 minutes for all TTS.

    # 6. Close all tabs after reading.
    time.sleep(1.0)  # Brief pause before closing.
    if pids:
        _close_tabs(pids)

    if player:
        player.write_log("SYS: Trending news complete — tabs closed.")

    # Return a short summary for the voice reply.
    top_stories = []
    for source, items in all_news.items():
        if items:
            top_stories.append(f"{source}: {items[0]['title']}")
    reply = f"Trending news from {sources}. Top stories: " + "; ".join(top_stories[:4]) + "."
    return reply
