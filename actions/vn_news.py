"""
vn_news.py — hot Vietnamese news from major VN newspaper RSS feeds.

Reads the latest-headlines RSS from VnExpress, Tuoi Tre, and Thanh Nien —
reliable, fast, and truly Vietnamese (better than a generic web search).
Free, no API key.
"""

import re
import urllib.request

_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Parker/1.0"}

# Topic → RSS URL per source. 'latest' is the default.
_FEEDS = {
    "latest": [
        ("VnExpress", "https://vnexpress.net/rss/tin-moi-nhat.rss"),
        ("Tuoi Tre", "https://tuoitre.vn/rss/tin-moi-nhat.rss"),
        ("Thanh Nien", "https://thanhnien.vn/rss/home.rss"),
    ],
    "world": [("VnExpress", "https://vnexpress.net/rss/the-gioi.rss")],
    "business": [("VnExpress", "https://vnexpress.net/rss/kinh-doanh.rss")],
    "sports": [("VnExpress", "https://vnexpress.net/rss/the-thao.rss")],
    "tech": [("VnExpress", "https://vnexpress.net/rss/so-hoa.rss")],
    "entertainment": [("VnExpress", "https://vnexpress.net/rss/giai-tri.rss")],
    "law": [("VnExpress", "https://vnexpress.net/rss/phap-luat.rss")],
    "health": [("VnExpress", "https://vnexpress.net/rss/suc-khoe.rss")],
}

_TOPIC_ALIASES = {
    "the gioi": "world", "kinh doanh": "business", "kinh te": "business",
    "the thao": "sports", "cong nghe": "tech", "giai tri": "entertainment",
    "phap luat": "law", "suc khoe": "health",
}


def _get(url: str, timeout: int = 15) -> str:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _titles(xml: str) -> list[str]:
    items = re.findall(r"<item>(.*?)</item>", xml, re.DOTALL)
    out = []
    for it in items:
        m = re.search(r"<title>(?:<!\[CDATA\[)?(.*?)(?:\]\]>)?</title>", it, re.DOTALL)
        if m:
            title = re.sub(r"\s+", " ", m.group(1)).strip()
            if title:
                out.append(title)
    return out


def vietnam_news(parameters: dict = None, player=None, session_memory=None) -> str:
    """Return the latest hot Vietnamese headlines (optionally by topic)."""
    p = parameters or {}
    topic = (p.get("topic") or p.get("category") or "latest").lower().strip()
    topic = _TOPIC_ALIASES.get(topic, topic)
    if topic not in _FEEDS:
        topic = "latest"

    headlines: list[str] = []
    for source, url in _FEEDS[topic]:
        try:
            titles = _titles(_get(url))
            for t in titles[:5]:
                headlines.append(f"[{source}] {t}")
            if len(headlines) >= 8:
                break
        except Exception:
            continue

    if not headlines:
        return "Sir, I couldn't fetch Vietnamese news right now."

    label = "Vietnam news" if topic == "latest" else f"Vietnam {topic} news"
    msg = f"Top {label}:\n" + "\n".join(f"  - {h}" for h in headlines[:8])
    if player:
        try:
            player.write_log(f"Parker: {label} — {len(headlines)} headlines")
        except Exception:
            pass
    return msg
