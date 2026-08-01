"""
route_engine.py — multi-route planning with per-route analysis.

Produces several alternative driving routes between two Vietnam points, each
with distance, duration (with live traffic if a Google key is configured), and
a short analysis (fastest / shortest / fewest turns). Routes are cached so the
user can ask for a "different route" and cycle through the alternatives, and so
the 3D map view can render the currently-selected one.

Backends:
  - Google Directions API   (if 'google_maps_key' is set in config) — has traffic
  - OSRM (public server)     — free, no key, no live traffic

Coordinates are returned as [lat, lon] point lists for the map layer.
"""

import json
import time
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from memory.config_manager import load_api_keys

_OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
_GOOGLE_URL = "https://maps.googleapis.com/maps/api/directions/json"

# Last computed route set — shared with the map view and "different route".
_LAST = {"routes": [], "selected": 0, "origin": None, "dest": None,
         "o_label": "", "d_label": ""}


def _google_key() -> str:
    return (load_api_keys().get("google_maps_key") or "").strip()


def _http_json(url: str, timeout: int = 15) -> dict:
    req = Request(url, headers={"User-Agent": "Parker/1.0"})
    with urlopen(req, timeout=timeout) as r:
        return json.load(r)


def _decode_polyline(poly: str) -> list:
    """Decode a Google encoded polyline into [[lat, lon], …]."""
    points, index, lat, lng = [], 0, 0, 0
    while index < len(poly):
        for _coord in range(2):
            shift, result = 0, 0
            while True:
                b = ord(poly[index]) - 63
                index += 1
                result |= (b & 0x1F) << shift
                shift += 5
                if b < 0x20:
                    break
            d = ~(result >> 1) if (result & 1) else (result >> 1)
            if _coord == 0:
                lat += d
            else:
                lng += d
        points.append([lat / 1e5, lng / 1e5])
    return points


def _analyze(route: dict, all_routes: list) -> str:
    """Short human analysis of a route relative to the others."""
    tags = []
    durs = [r["duration_s"] for r in all_routes]
    dists = [r["distance_m"] for r in all_routes]
    if route["duration_s"] == min(durs):
        tags.append("fastest")
    if route["distance_m"] == min(dists):
        tags.append("shortest")
    if route.get("traffic_s") and route["traffic_s"] > route["duration_s"] * 1.15:
        tags.append("heavy traffic")
    turns = route.get("turns")
    if turns is not None and turns == min((r.get("turns") or 1e9) for r in all_routes):
        tags.append("simplest (fewest turns)")
    return ", ".join(tags) or "alternative"


def _fmt_dur(seconds: float) -> str:
    m = int(round(seconds / 60))
    h, m = divmod(m, 60)
    return (f"{h} h {m} min" if h else f"{m} min")


def _google_routes(o, d, depart_epoch: int | None) -> list:
    key = _google_key()
    if not key:
        return []
    params = {
        "origin": f"{o[0]},{o[1]}", "destination": f"{d[0]},{d[1]}",
        "alternatives": "true", "mode": "driving", "key": key,
    }
    if depart_epoch:
        params["departure_time"] = depart_epoch
        params["traffic_model"] = "best_guess"
    try:
        data = _http_json(f"{_GOOGLE_URL}?{urlencode(params)}")
    except Exception as e:
        print(f"[Route] Google Directions failed: {e}")
        return []
    if data.get("status") != "OK":
        print(f"[Route] Google Directions status: {data.get('status')}")
        return []

    routes = []
    for r in data.get("routes", []):
        leg = r["legs"][0]
        dur = leg["duration"]["value"]
        traffic = (leg.get("duration_in_traffic") or {}).get("value")
        turns = sum(len(s.get("steps", [])) for s in [leg])
        routes.append({
            "distance_m": leg["distance"]["value"],
            "duration_s": dur,
            "traffic_s": traffic,
            "turns": len(leg.get("steps", [])),
            "summary": r.get("summary", ""),
            "points": _decode_polyline(r["overview_polyline"]["points"]),
        })
    return routes


def _osrm_routes(o, d) -> list:
    url = (f"{_OSRM_URL}/{o[1]},{o[0]};{d[1]},{d[0]}"
           "?alternatives=3&overview=full&geometries=geojson&steps=false")
    try:
        data = _http_json(url)
        routes = []
        for r in data.get("routes", []):
            pts = [[lat, lon] for lon, lat in r["geometry"]["coordinates"]]
            routes.append({
                "distance_m": r["distance"],
                "duration_s": r["duration"],
                "traffic_s": None,
                "turns": len(r.get("legs", [{}])[0].get("steps", [])) or None,
                "summary": "",
                "points": pts,
            })
        return routes
    except Exception as e:
        print(f"[Route] OSRM failed: {e}")
        return []


def compute_routes(o: tuple, d: tuple, o_label: str, d_label: str,
                   depart_epoch: int | None = None) -> list:
    """Compute alternative routes and cache them. Returns the route list.

    Each route: {distance_m, duration_s, traffic_s, turns, summary, points,
                 analysis, label}.
    """
    routes = _google_routes(o, d, depart_epoch) or _osrm_routes(o, d)
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


def render_map(player=None) -> str | None:
    """Write the cached routes into the 3D map HTML and ask the UI to show it.

    Returns the temp HTML path, or None if there are no routes.
    """
    routes = _LAST["routes"]
    if not routes:
        return None
    import os
    import tempfile
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
    html = html.replace("<head>", "<head>\n" + inject, 1)

    out = Path(tempfile.gettempdir()) / "parker_route_map.html"
    out.write_text(html, encoding="utf-8")
    if player is not None:
        try:
            player.show_route_map(str(out))
        except Exception as e:
            print(f"[Route] could not show map: {e}")
    return str(out)


def describe(routes: list, selected: int = 0) -> str:
    """One-line-per-route English summary for the voice reply."""
    if not routes:
        return "No routes found."
    src = "with live traffic" if any(r.get("traffic_s") for r in routes) else "estimated"
    lines = [f"I found {len(routes)} route(s) ({src}):"]
    for i, r in enumerate(routes):
        mark = "→ " if i == selected else "  "
        name = f" via {r['summary']}" if r.get("summary") else ""
        lines.append(f"{mark}Route {i+1}{name}: {r['dist_km']:.0f} km, "
                     f"{r['eta_text']} — {r['analysis']}.")
    return "\n".join(lines)
