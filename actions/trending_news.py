"""
trending_news.py — Trending news feature for Parker.

When the user asks about "trending news" / "tin tức nổi bật", Parker fetches
the latest headlines from 4 Vietnamese RSS feeds (VnExpress, TuoiTre,
ThanhNien, DanTri), summarizes them with the text model, and returns the
summary as the tool result — Gemini Live then reads it aloud in the same
voice as the rest of the conversation (no separate TTS engine, no browser
tabs). This mirrors how the startup morning briefing delivers news.
"""

import json
import re
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
        for item in root.findall(".//item")[:max_items]:
            title = (item.findtext("title") or "").strip()
            link = (item.findtext("link") or "").strip()
            desc = (item.findtext("description") or "").strip()
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

    # Try the online text model first, fall back to a local Ollama model.
    raw = ""
    try:
        config_path = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"
        api_key = json.loads(config_path.read_text(encoding="utf-8")).get("gemini_api_key", "")
        if not api_key:
            raise RuntimeError("no API key")
        from google import genai
        client = genai.Client(
            api_key=api_key,
            http_options={"timeout": 20_000})  # ms — don't hang on a slow API
        resp = client.models.generate_content(
            model="gemini-flash-latest", contents=prompt)
        raw = (resp.text or "").strip()
    except Exception:
        try:
            from core.llm_client import call_llm, ensure_ollama_running
            if ensure_ollama_running():
                messages = [{"role": "user", "content": prompt}]
                first = call_llm(messages, tools=None, timeout=60)
                raw = first.get("content") or ""
        except Exception as e:
            print(f"[TrendingNews] Summarize failed: {e}")

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
        for known in all_news:
            if known.lower() in source_name.lower() or source_name.lower() in known.lower():
                summaries[known] = summary_text
                break
    for source, items in all_news.items():
        if source not in summaries:
            titles = "; ".join(item["title"] for item in items)
            summaries[source] = f"From {source}: {titles}"

    return summaries


# ── Main entry point ──────────────────────────────────────────────────────────
def trending_news(parameters: dict = None, player=None,
                   session_memory=None) -> str:
    """Fetch trending Vietnamese news and return an AI summary as text.
    Gemini Live speaks the returned text aloud — no separate TTS engine or
    browser tabs involved."""
    if player:
        player.write_log("SYS: Fetching trending news from 4 sources...")
    all_news = _fetch_all()
    if not all_news:
        return "Sir, I couldn't fetch any news feeds right now. Try again later."

    total = sum(len(v) for v in all_news.values())
    sources = ", ".join(all_news.keys())

    if player:
        player.write_log(f"SYS: Summarizing {total} stories from {sources}...")
    summaries = _summarize(all_news)

    parts = []
    for source in ["VnExpress", "TuoiTre", "ThanhNien", "DanTri"]:
        if source in summaries:
            parts.append(f"{source}: {summaries[source]}")

    if player:
        player.write_log("SYS: Trending news ready.")

    return f"Trending news from {sources}.\n\n" + "\n\n".join(parts)
