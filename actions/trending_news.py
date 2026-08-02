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
    """Find a real path to msedge.exe or chrome.exe on Windows.

    Search real install locations FIRST, not PATH. Windows registers
    'msedge'/'msedge.exe' as an App Execution Alias under
    %LOCALAPPDATA%\\Microsoft\\WindowsApps — shutil.which() happily finds
    that stub, but launching it hands off to the real process via the
    Windows Apps mechanism and the launched PID has NO relation to the
    actual msedge.exe process that owns the window, breaking the
    Get-Process -Name match used for snapping/closing later. A direct
    Program Files path avoids that indirection entirely."""
    import os

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

    # Last resort: PATH (may be the WindowsApps stub, which is why this
    # comes after the direct paths above, not before).
    import shutil
    for name in ("msedge.exe", "msedge", "chrome.exe", "chrome"):
        found = shutil.which(name)
        if found:
            return found
    return None


# ── Tab Layout (Windows Snap) ─────────────────────────────────────────────────
def _open_tabs_snap_layout(urls: list[str]) -> list:
    """Open 4 browser tabs and snap them into a 2x2 grid on Windows.
    Returns the list of window handles (as strings) that were snapped, for
    _close_tabs() to close later. NOT launcher PIDs — see _close_tabs."""
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

    proc_name = Path(browser_path).stem  # "msedge" or "chrome"

    # PowerShell: find every top-level window belonging to the browser process
    # that has a real title and isn't in the list of already-snapped handles,
    # pick the NEWEST one (by process start time), restore-if-maximized, and
    # move+resize it into a quadrant. This avoids two failure modes we hit
    # before: (1) GetForegroundWindow() can grab the wrong window if focus
    # hasn't settled on the new tab yet, and (2) chasing the launcher's PID
    # doesn't work because Chrome/Edge often hand the URL to an already-
    # running instance and the launcher exits immediately.
    _ps_helper = """
Add-Type -AssemblyName System.Windows.Forms
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class SnapHelper {
    [DllImport("user32.dll")] public static extern bool SetWindowPos(
        IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
    [DllImport("user32.dll")] public static extern bool ShowWindow(
        IntPtr hWnd, int nCmdShow);
    [DllImport("user32.dll")] public static extern bool IsZoomed(IntPtr hWnd);
    const int SW_RESTORE = 9;

    public static bool SnapToQuadrant(IntPtr hWnd, int quadrant) {
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
        if (IsZoomed(hWnd)) { ShowWindow(hWnd, SW_RESTORE); }
        return SetWindowPos(hWnd, IntPtr.Zero, x, y, w, h, 0x0040);
    }
}
"@
$exclude = @({exclude})
$win = Get-Process -Name '{proc_name}' -ErrorAction SilentlyContinue |
    Where-Object {{ $_.MainWindowHandle -ne 0 -and $exclude -notcontains $_.MainWindowHandle.ToInt64() }} |
    Sort-Object StartTime -Descending |
    Select-Object -First 1
if ($win) {{
    $ok = [SnapHelper]::SnapToQuadrant($win.MainWindowHandle, {quadrant})
    Write-Output "HANDLE=$($win.MainWindowHandle.ToInt64()) TITLE=$($win.MainWindowTitle) OK=$ok"
}} else {{
    Write-Output "NOMATCH"
}}
"""

    snapped_handles: list[str] = []
    for i, url in enumerate(urls[:4]):
        try:
            subprocess.Popen(
                [browser_path, "--new-window", url],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        except Exception as e:
            print(f"[TrendingNews] Failed to open tab {url}: {e}")
            continue

        # Give the window time to actually appear (cold browser start is
        # much slower than opening a tab on an already-running instance).
        time.sleep(2.5 if i == 0 else 1.2)
        script = (_ps_helper
                  .replace("{proc_name}", proc_name)
                  .replace("{quadrant}", str(i))
                  .replace("{exclude}", ",".join(snapped_handles)))
        try:
            r = subprocess.run(
                ["powershell", "-NoProfile", "-Command", script],
                capture_output=True, timeout=10, text=True,
            )
            out = (r.stdout or "").strip()
            print(f"[TrendingNews] snap tab {i}: {out or r.stderr[:200]}")
            m = re.search(r"HANDLE=(\d+)", out)
            if m:
                snapped_handles.append(m.group(1))
        except Exception as e:
            print(f"[TrendingNews] Snap failed for window {i}: {e}")

    return snapped_handles


def _close_tabs(handles: list) -> None:
    """Close the browser windows we opened, by window handle (NOT the launcher
    PID — Chrome/Edge routinely hand off to an already-running instance and
    the launcher process exits immediately, so killing launcher PIDs closed
    nothing and tabs never auto-closed). Re-resolve each handle to its real
    owning process PID via PowerShell and stop that."""
    if not handles:
        return
    import platform
    if platform.system() != "Windows":
        return
    handle_list = ",".join(str(h) for h in handles)
    script = f"""
$handles = @({handle_list})
Get-Process | Where-Object {{
    $_.MainWindowHandle -ne 0 -and $handles -contains $_.MainWindowHandle.ToInt64()
}} | ForEach-Object {{
    Stop-Process -Id $_.Id -Force -ErrorAction SilentlyContinue
}}
"""
    try:
        subprocess.run(["powershell", "-NoProfile", "-Command", script],
                       capture_output=True, timeout=10, text=True)
    except Exception as e:
        print(f"[TrendingNews] Failed to close tabs: {e}")


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
    window_handles = _open_tabs_snap_layout(urls)

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
    if window_handles:
        _close_tabs(window_handles)

    if player:
        player.write_log("SYS: Trending news complete — tabs closed.")

    # Return a short summary for the voice reply.
    top_stories = []
    for source, items in all_news.items():
        if items:
            top_stories.append(f"{source}: {items[0]['title']}")
    reply = f"Trending news from {sources}. Top stories: " + "; ".join(top_stories[:4]) + "."
    return reply
