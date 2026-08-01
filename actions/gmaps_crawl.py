"""
gmaps_crawl.py — get driving directions straight from Google Maps (no API key).

Opens the Google Maps directions page in a headless browser (Playwright) and
reads the route polyline from Google's internal directions response, plus the
primary route's ETA and distance from the page text. This gives Google's own
routing (with live traffic) without a paid Directions API key.

⚠️ Caveats:
  - Scraping Google Maps is against Google's Terms of Service and may lead to
    temporary IP blocks or CAPTCHAs. Use sparingly.
  - It's brittle: Google can change its page/response format at any time. The
    caller must fall back to OSRM when this returns nothing.
  - Requires Playwright with a browser installed (already used elsewhere).

Returns a list of route dicts compatible with route_engine:
  {distance_m, duration_s, traffic_s, turns, summary, points:[[lat,lon],…]}
"""

import re
from urllib.parse import quote


def _parse_duration_to_s(text: str) -> int | None:
    """'1 h 10 min' / '42 min' → seconds."""
    h = re.search(r"(\d+)\s*h", text)
    m = re.search(r"(\d+)\s*min", text)
    if not h and not m:
        return None
    secs = 0
    if h:
        secs += int(h.group(1)) * 3600
    if m:
        secs += int(m.group(1)) * 60
    return secs or None


def crawl_routes(o_label: str, d_label: str, timeout_ms: int = 40000) -> list:
    """Crawl Google Maps for driving directions. Returns route dicts or []."""
    try:
        from playwright.sync_api import sync_playwright
    except Exception as e:
        print(f"[GMapsCrawl] Playwright unavailable: {e}")
        return []

    url = ("https://www.google.com/maps/dir/"
           f"{quote(o_label)}/{quote(d_label)}/")
    bodies: list[str] = []

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(args=["--disable-blink-features=AutomationControlled"])
            page = browser.new_page(locale="en-US")

            def _on_resp(r):
                if "/maps/preview/directions" in r.url:
                    try:
                        bodies.append(r.text())
                    except Exception:
                        pass

            page.on("response", _on_resp)
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            if "consent" in page.url or "/sorry/" in page.url:
                browser.close()
                print("[GMapsCrawl] Hit consent/CAPTCHA page — aborting.")
                return []
            page.wait_for_timeout(8000)
            page_text = page.inner_text("body")
            browser.close()
    except Exception as e:
        print(f"[GMapsCrawl] crawl failed: {e}")
        return []

    # ── Polyline: pull ordered lat,lon pairs from the directions response ─────
    points: list[list[float]] = []
    if bodies:
        body = bodies[0]
        # Vietnam latitudes ~8–23, longitudes ~102–110.
        for lat_s, lon_s in re.findall(
                r"\[?(\d{1,2}\.\d{4,}),\s*(1[0-1]\d\.\d{4,})\]?", body):
            try:
                lat, lon = float(lat_s), float(lon_s)
                if 7.5 <= lat <= 24 and 101 <= lon <= 111:
                    points.append([lat, lon])
            except Exception:
                continue
        # De-duplicate consecutive repeats
        dedup = []
        for p in points:
            if not dedup or dedup[-1] != p:
                dedup.append(p)
        points = dedup

    if len(points) < 2:
        print("[GMapsCrawl] no polyline extracted.")
        return []

    # ── Primary route ETA + distance from the page text ──────────────────────
    dur_txt = re.search(r"(\d+\s*h\s*\d+\s*min|\d+\s*min)", page_text)
    # Prefer a km distance (the main route is always in km for city-to-city);
    # take the largest km value to avoid picking a short leg like "1.1 km".
    km_vals = [float(x) for x in re.findall(r"([\d.]+)\s*km\b", page_text)]
    dist_m = int(max(km_vals) * 1000) if km_vals else None
    dur_s = _parse_duration_to_s(dur_txt.group(1)) if dur_txt else None

    # Fall back to the polyline length if the page distance couldn't be read.
    if not dist_m and len(points) >= 2:
        import math
        total = 0.0
        for (a_lat, a_lon), (b_lat, b_lon) in zip(points, points[1:]):
            dlat = math.radians(b_lat - a_lat)
            dlon = math.radians(b_lon - a_lon)
            aa = (math.sin(dlat / 2) ** 2 +
                  math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) *
                  math.sin(dlon / 2) ** 2)
            total += 6371000 * 2 * math.asin(math.sqrt(aa))
        dist_m = int(total)

    if not dur_s or not dist_m:
        print("[GMapsCrawl] couldn't read ETA/distance.")
        return []

    return [{
        "distance_m": dist_m,
        "duration_s": dur_s,
        "traffic_s": dur_s,        # Google ETA already includes live traffic
        "turns": None,
        "summary": "Google Maps",
        "points": points,
    }]
