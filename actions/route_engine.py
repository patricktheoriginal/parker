"""
route_engine.py — multi-route planning with per-route analysis (OSRM, free).

Produces several alternative driving routes between two Vietnam points, each
with distance, duration, and a short analysis (fastest / shortest / fewest
turns). Routes come from the public OSRM server — free, no API key. Routes are
cached so the user can ask for a "different route" and cycle through the
alternatives, and so the 3D map view can render the currently-selected one.

Coordinates are returned as [lat, lon] point lists for the map layer.
"""

import json
from urllib.request import Request, urlopen

_OSRM_URL = "https://router.project-osrm.org/route/v1/driving"

# Last computed route set — shared with the map view and "different route".
_LAST = {"routes": [], "selected": 0, "origin": None, "dest": None,
         "o_label": "", "d_label": ""}


def _http_json(url: str, timeout: int = 15) -> dict:
    req = Request(url, headers={"User-Agent": "Parker/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _analyze(route: dict, all_routes: list) -> str:
    """Short human analysis of a route relative to the others."""
    tags = []
    durs = [r["duration_s"] for r in all_routes]
    dists = [r["distance_m"] for r in all_routes]
    if route["duration_s"] == min(durs):
        tags.append("fastest")
    if route["distance_m"] == min(dists):
        tags.append("shortest")
    turns = route.get("turns")
    if turns is not None and len(all_routes) > 1 and \
       turns == min((r.get("turns") or 1e9) for r in all_routes):
        tags.append("simplest (fewest turns)")
    return ", ".join(tags) or "alternative"


def _fmt_dur(seconds: float) -> str:
    m = int(round(seconds / 60))
    h, m = divmod(m, 60)
    return (f"{h} h {m} min" if h else f"{m} min")


def _osrm_routes(o, d) -> list:
    url = (f"{_OSRM_URL}/{o[1]},{o[0]};{d[1]},{d[0]}"
           "?alternatives=3&overview=full&geometries=geojson&steps=true")
    try:
        data = _http_json(url)
        routes = []
        for r in data.get("routes", []):
            pts = [[lat, lon] for lon, lat in r["geometry"]["coordinates"]]
            turns = sum(len(leg.get("steps", [])) for leg in r.get("legs", []))
            routes.append({
                "distance_m": r["distance"],
                "duration_s": r["duration"],
                "traffic_s": None,
                "turns": turns or None,
                "summary": "",
                "points": pts,
            })
        return routes
    except Exception as e:
        print(f"[Route] OSRM failed: {e}")
        return []


def compute_routes(o: tuple, d: tuple, o_label: str, d_label: str,
                   depart_epoch: int | None = None) -> list:
    """Compute alternative routes (OSRM, free) and cache them.

    `depart_epoch` is accepted for API compatibility but unused (OSRM has no
    live traffic).
    """
    routes: list = []
    try:
        routes = _osrm_routes(o, d)
    except Exception as e:
        print(f"[Route] OSRM failed: {e}")

    for r in routes:
        r["analysis"] = _analyze(r, routes)
        r["dist_km"] = r["distance_m"] / 1000.0
        secs = r.get("traffic_s") or r["duration_s"]
        r["eta_text"] = _fmt_dur(secs)
    _LAST.update({"routes": routes, "selected": 0, "origin": o, "dest": d,
                  "o_label": o_label, "d_label": d_label})
    return routes


def get_last() -> dict:
    return _LAST


def select_next() -> int:
    """Advance to the next alternative route; returns the new index."""
    n = len(_LAST["routes"])
    if n <= 1:
        return _LAST["selected"]
    _LAST["selected"] = (_LAST["selected"] + 1) % n
    return _LAST["selected"]


def build_map_html() -> str | None:
    """Return the full 3D map HTML for the cached routes (with data injected),
    as a string — no file needed. Returns None if there are no routes."""
    routes = _LAST["routes"]
    if not routes:
        return None
    from pathlib import Path

    template = (Path(__file__).resolve().parent.parent
                / "dashboard" / "static" / "route_map.html")
    try:
        html = template.read_text(encoding="utf-8")
    except Exception as e:
        print(f"[Route] map template missing: {e}")
        return None

    data = {
        "o_label": _LAST["o_label"], "d_label": _LAST["d_label"],
        "selected": _LAST["selected"],
        "routes": [
            {"dist_km": r["dist_km"], "eta_text": r["eta_text"],
             "analysis": r["analysis"], "summary": r.get("summary", ""),
             "points": r["points"]}
            for r in routes
        ],
    }
    inject = f"<script>window.ROUTE_DATA = {json.dumps(data)};</script>"
    return html.replace("<head>", "<head>\n" + inject, 1)


def render_map(player=None) -> str | None:
    """Build the 3D map HTML and hand it straight to the UI (no temp file).

    Returns the HTML string, or None if there are no routes.
    """
    html = build_map_html()
    if html is None:
        return None
    if player is not None:
        try:
            player.show_route_map(html)      # HTML string, shown via setHtml
        except Exception as e:
            print(f"[Route] could not show map: {e}")
    return html


def describe(routes: list, selected: int = 0) -> str:
    """One-line-per-route English summary for the voice reply."""
    if not routes:
        return "No routes found."
    lines = [f"I found {len(routes)} route(s):"]
    for i, r in enumerate(routes):
        mark = "→ " if i == selected else "  "
        name = f" via {r['summary']}" if r.get("summary") else ""
        lines.append(f"{mark}Route {i+1}{name}: {r['dist_km']:.0f} km, "
                     f"{r['eta_text']} — {r['analysis']}.")
    return "\n".join(lines)
