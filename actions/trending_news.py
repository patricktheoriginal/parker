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
import textwrap
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
            client = genai.Client(api_key=api_key)
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
def _speak_edge(text: str, voice: str = "vi-VN-HoaiMyNeural") -> bool:
    """Speak text using Edge TTS (online, high quality Vietnamese voice).
    Returns True if successful."""
    try:
        import edge_tts
        import io
        import sounddevice as sd
        import numpy as np

        async def _gen():
            communicate = edge_tts.Communicate(text, voice)
            audio_data = b""
            async for chunk in communicate.stream():
                if chunk["type"] == "audio":
                    audio_data += chunk["data"]
            return audio_data

        audio_bytes = asyncio.run(_gen())
        if not audio_bytes:
            return False

        # Save to temp WAV, then play.
        import tempfile
        with tempfile.NamedTemporaryFile(suffix=".mp3", delete=False) as f:
            f.write(audio_bytes)
            tmp_path = f.name

        # Use ffmpeg to convert mp3 to wav if available, otherwise use pyttsx3.
        try:
            wav_path = tmp_path.replace(".mp3", ".wav")
            subprocess.run(["ffmpeg", "-y", "-i", tmp_path, wav_path],
                           capture_output=True, timeout=10)
            import wave
            with wave.open(wav_path, "rb") as wf:
                sr = wf.getframerate()
                frames = wf.readframes(wf.getnframes())
            audio = np.frombuffer(frames, dtype=np.int16)
            sd.play(audio, sr)
            sd.wait()
            Path(tmp_path).unlink(missing_ok=True)
            Path(wav_path).unlink(missing_ok=True)
            return True
        except Exception:
            Path(tmp_path).unlink(missing_ok=True)
            return False
    except ImportError:
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

    pids = []
    for i, url in enumerate(urls):
        # Open each URL in Edge (or Chrome) as a separate process.
        try:
            # Try Edge first, then Chrome.
            for browser in ["msedge", "chrome", "msedge.exe", "chrome.exe"]:
                try:
                    proc = subprocess.Popen(
                        [browser, "--new-window", url],
                        stdout=subprocess.DEVNULL,
                        stderr=subprocess.DEVNULL,
                    )
                    pids.append(proc.pid)
                    break
                except FileNotFoundError:
                    continue
            time.sleep(0.8)  # Let each window appear.
        except Exception as e:
            print(f"[TrendingNews] Failed to open tab: {e}")

    # Snap each window into a quadrant using PowerShell + WinAPI.
    time.sleep(1.0)
    ps_snap = """
Add-Type @"
using System;
using System.Runtime.InteropServices;
public class SnapHelper {
    [DllImport("user32.dll")] public static extern bool SetWindowPos(
        IntPtr hWnd, IntPtr hWndInsertAfter, int X, int Y, int cx, int cy, uint uFlags);
    [DllImport("user32.dll")] public static extern bool GetWindowRect(
        IntPtr hWnd, out RECT lpRect);
    [DllImport("user32.dll")] public static extern IntPtr GetForegroundWindow();
    public struct RECT { public int Left, Top, Right, Bottom; }

    public static void SnapToQuadrant(int quadrant) {
        // Get screen size.
        var screen = System.Windows.Forms.Screen.PrimaryScreen.Bounds;
        int sw = screen.Width, sh = screen.Height;

        int x, y, w, h;
        int gap = 4; // Small gap between windows.

        switch (quadrant) {
            case 0: // Top-left
                x = 0; y = 0; w = sw / 2 - gap; h = sh / 2 - gap; break;
            case 1: // Top-right
                x = sw / 2 + gap; y = 0; w = sw / 2 - gap; h = sh / 2 - gap; break;
            case 2: // Bottom-left
                x = 0; y = sh / 2 + gap; w = sw / 2 - gap; h = sh / 2 - gap; break;
            default: // Bottom-right
                x = sw / 2 + gap; y = sh / 2 + gap; w = sw / 2 - gap; h = sh / 2 - gap; break;
        }

        IntPtr hWnd = GetForegroundWindow();
        SetWindowPos(hWnd, IntPtr.Zero, x, y, w, h, 0x0040); // SWP_SHOWWINDOW
    }
}
"@
Add-Type -AssemblyName System.Windows.Forms
"""
    for i in range(min(len(pids), 4)):
        try:
            # Focus each window and snap it.
            focus_ps = f"""
$proc = Get-Process -Id {pids[i]} -ErrorAction SilentlyContinue |
    Where-Object {{$_.MainWindowTitle -ne ''}} |
    Select-Object -First 1
if ($proc) {{
    $shell = New-Object -ComObject WScript.Shell
    $shell.AppActivate($proc.Id)
    Start-Sleep -Milliseconds 300
    [SnapHelper]::SnapToQuadrant({i})
}}
"""
            subprocess.Popen(
                ["powershell", "-NoProfile", "-Command", ps_snap + focus_ps],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            )
            time.sleep(0.5)
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

    # 3. Build URLs for the4 tabs (homepages of each source).
    urls = [feed[1].replace("/rss/", "/") for feed in _FEEDS]  # Convert RSS to homepage.

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
