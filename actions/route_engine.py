"""
route_engine.py — multi-route planning with per-route analysis.

Produces several alternative driving routes between two Vietnam points, each
with distance, duration, and a short analysis (fastest / shortest / fewest
turns). Candidate routes come from ALL of (all free, open source, no API keys):
  - A* (self-hosted, actions/astar_route.py) — a driving graph built from
                  OpenStreetMap data, entirely offline once built; no server
                  or network dependency at all. Only used once its graph
                  cache exists (see tools/build_osm_graph.py) -- building it
                  from scratch takes minutes, too slow to do inline.
  - GraphHopper  (self-hosted open-source server at 'graphhopper_url', default
                  http://localhost:8989) — used if the local server is running
  - OSRM         (public server, no key) — always used
The results are merged and de-duplicated. Routes are cached so the user can ask
for a "different route" and cycle through the alternatives, and so the 3D map
view can render the currently-selected one.

To run a local GraphHopper for Vietnam: see tools/setup_graphhopper.sh.
To build the self-hosted A* graph: see tools/build_osm_graph.py.

Coordinates are returned as [lat, lon] point lists for the map layer.
"""

import json
from urllib.request import Request, urlopen

_OSRM_URL = "https://router.project-osrm.org/route/v1/driving"

# Last computed route set — shared with the map view and "different route".
_LAST = {"routes": [], "selected": 0, "origin": None, "dest": None,
         "o_label": "", "d_label": "", "places": []}


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


def _classify_road(name: str, ref: str) -> str:
    """Classify a road segment as 'expressway' | 'highway' | 'urban'."""
    n = (name or "").lower()
    r = (ref or "").upper()
    if "cao tốc" in n or r.startswith("CT"):
        return "expressway"          # đường cao tốc
    if "quốc lộ" in n or r.startswith("QL"):
        return "highway"             # quốc lộ
    if "tỉnh lộ" in n or r.startswith("DT") or r.startswith("ĐT"):
        return "provincial"          # tỉnh lộ
    return "urban"                   # đường nội đô / khác


def _osrm_routes(o, d) -> list:
    url = (f"{_OSRM_URL}/{o[1]},{o[0]};{d[1]},{d[0]}"
           "?alternatives=3&overview=full&geometries=geojson&steps=true")
    try:
        data = _http_json(url)
        routes = []
        for r in data.get("routes", []):
            pts = [[lat, lon] for lon, lat in r["geometry"]["coordinates"]]
            steps = [s for leg in r.get("legs", []) for s in leg.get("steps", [])]
            turns = len(steps)
            # Distance per road class (meters), from the step maneuvers.
            by_class: dict = {}
            names: list = []
            for s in steps:
                cls = _classify_road(s.get("name", ""), s.get("ref", ""))
                by_class[cls] = by_class.get(cls, 0.0) + float(s.get("distance", 0))
                nm = s.get("name") or ""
                if nm and nm not in names:
                    names.append(nm)
            routes.append({
                "distance_m": r["distance"],
                "duration_s": r["duration"],
                "traffic_s": None,
                "turns": turns or None,
                "summary": "OSRM",
                "points": pts,
                "by_class": by_class,        # meters per road type
                "road_names": names,         # ordered unique road names
            })
        return routes
    except Exception as e:
        print(f"[Route] OSRM failed: {e}")
        return []


def _graphhopper_url() -> str:
    """Base URL of a SELF-HOSTED GraphHopper server (open source, no API key).
    Defaults to http://localhost:8989. Configure with 'graphhopper_url'.
    Run the server with remote_agent/../tools/graphhopper — see the README."""
    try:
        from memory.config_manager import load_api_keys
        u = (load_api_keys().get("graphhopper_url") or "").strip()
    except Exception:
        u = ""
    return (u or "http://localhost:8989").rstrip("/")


def _graphhopper_routes(o, d) -> list:
    """Alternative routes from a self-hosted GraphHopper server (no API key).
    Returns [] if the local server isn't running or on error."""
    base = _graphhopper_url()
    from urllib.parse import urlencode
    params = [
        ("point", f"{o[0]},{o[1]}"), ("point", f"{d[0]},{d[1]}"),
        ("profile", "car"), ("points_encoded", "false"),
        ("algorithm", "alternative_route"),
        ("alternative_route.max_paths", "3"),
        ("ch.disable", "true"), ("instructions", "true"),
        # No 'key' — self-hosted GraphHopper needs none.
    ]
    url = f"{base}/route?" + urlencode(params)
    try:
        data = _http_json(url, timeout=15)
    except Exception:
        # Server not running / unreachable — silently fall back to OSRM.
        return []

    routes = []
    for path in data.get("paths", []):
        coords = path.get("points", {}).get("coordinates", [])  # [lon, lat]
        pts = [[c[1], c[0]] for c in coords]
        if len(pts) < 2:
            continue
        # Road-type breakdown from instructions (street_name / road_class).
        by_class: dict = {}
        names: list = []
        instr = path.get("instructions", [])
        for ins in instr:
            nm = ins.get("street_name", "") or ""
            dist = float(ins.get("distance", 0))
            cls = _classify_road(nm, ins.get("road_ref", "") or "")
            by_class[cls] = by_class.get(cls, 0.0) + dist
            if nm and nm not in names:
                names.append(nm)
        routes.append({
            "distance_m": path.get("distance", 0),
            "duration_s": path.get("time", 0) / 1000.0,   # GH gives ms
            "traffic_s": None,
            "turns": len(instr) or None,
            "summary": "GraphHopper",
            "points": pts,
            "by_class": by_class,
            "road_names": names,
        })
    return routes


def _dedupe_routes(routes: list) -> list:
    """Drop near-duplicate routes (same distance & duration within a tolerance)."""
    out = []
    for r in routes:
        dup = False
        for o in out:
            if (abs(r["distance_m"] - o["distance_m"]) < 500 and
                    abs(r["duration_s"] - o["duration_s"]) < 60):
                dup = True
                break
        if not dup:
            out.append(r)
    return out


def _astar_routes(o, d) -> list:
    """Self-hosted A* over a locally cached OSM driving graph (see
    actions/astar_route.py, actions/osm_graph.py) -- no external server or
    API dependency at all, unlike GraphHopper (needs a local server running)
    or OSRM (needs network access to a public server). Only attempted if the
    graph cache already exists: building it from the ~300MB Vietnam OSM
    extract takes minutes, far too slow to do inline on a route request --
    see tools/build_osm_graph.py to build it ahead of time. Much slower per
    query than GraphHopper/OSRM's contraction-hierarchy servers (seconds to
    up to ~90s for a long route vs. sub-second), so it's tried first only
    when ready, and route_engine still falls back to the HTTP engines below
    if it's not available, times out, or finds no path."""
    try:
        from actions.astar_route import astar_available, find_route
    except Exception as e:
        print(f"[Route] A* engine unavailable: {e}")
        return []
    if not astar_available():
        print("[Route] A* graph not built yet -- see tools/build_osm_graph.py. Using GraphHopper/OSRM.")
        return []
    try:
        r = find_route(o, d)
    except Exception as e:
        print(f"[Route] A* failed: {e}")
        return []
    if not r:
        print("[Route] A* found no path (falling back to GraphHopper/OSRM).")
        return []
    print(f"[Route] A*: 1 route ({r['distance_m']/1000:.1f} km, {r['duration_s']/60:.0f} min)")
    return [r]


def compute_routes(o: tuple, d: tuple, o_label: str, d_label: str,
                   depart_epoch: int | None = None) -> list:
    """Compute alternative routes from a self-hosted A* graph (if built),
    a self-hosted GraphHopper (if its local server is running), AND OSRM
    (always), merge + de-duplicate, and cache them.

    `depart_epoch` is accepted for API compatibility but unused (no live traffic).
    """
    routes: list = []
    # Self-hosted A* first (no external dependency at all) if the graph is
    # already built.
    try:
        routes.extend(_astar_routes(o, d))
    except Exception as e:
        print(f"[Route] A* engine errored: {e}")
    # GraphHopper next (self-hosted, no key) if the local server is running.
    try:
        gh = _graphhopper_routes(o, d)
        routes.extend(gh)
        if gh:
            print(f"[Route] GraphHopper: {len(gh)} route(s) from {_graphhopper_url()}")
        else:
            print(f"[Route] GraphHopper not available ({_graphhopper_url()}) — using OSRM.")
    except Exception as e:
        print(f"[Route] GraphHopper failed: {e}")
    # OSRM always (free) — adds coverage / a fallback.
    try:
        osrm = _osrm_routes(o, d)
        routes.extend(osrm)
        print(f"[Route] OSRM: {len(osrm)} route(s)")
    except Exception as e:
        print(f"[Route] OSRM failed: {e}")

    routes = _dedupe_routes(routes)

    for r in routes:
        r["analysis"] = _analyze(r, routes)
        r["dist_km"] = r["distance_m"] / 1000.0
        secs = r.get("traffic_s") or r["duration_s"]
        r["eta_text"] = _fmt_dur(secs)
    _LAST.update({"routes": routes, "selected": 0, "origin": o, "dest": d,
                  "o_label": o_label, "d_label": d_label, "places": []})
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


def set_places(center_label: str, places: list) -> None:
    """Cache a list of place pins to show on the map (nearby / place info).
    Each place: {name, lat, lon, dist, hours}. Clears any active route."""
    _LAST.update({"routes": [], "selected": 0, "places": places,
                  "o_label": center_label,
                  "d_label": f"{len(places)} place(s)"})


def build_map_html() -> str | None:
    """Return the full 3D map HTML (routes and/or place pins), as a string.
    Returns None if there's nothing to show."""
    routes = _LAST.get("routes") or []
    places = _LAST.get("places") or []
    if not routes and not places:
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
        "places": places,
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
            player.show_route_map(html)      # HTML string → UI writes a temp file + loads it
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


# ── Deep analysis: road types (real) + traffic estimate (Vietnam rush hours) ──

# Vietnam rush hours and how much they slow the URBAN portion of a trip.
# These are estimates (no live-traffic feed), tuned to typical VN city traffic.
_RUSH_WINDOWS = [
    ((7, 0), (9, 0),  "morning rush (7–9 AM)"),
    ((11, 0), (13, 0), "midday (11 AM–1 PM)"),
    ((16, 30), (19, 30), "evening rush (4:30–7:30 PM)"),
]


def _rush_factor(hour: float) -> tuple[float, str]:
    """Return (extra-delay multiplier for urban roads, label) at a given hour."""
    for (h1, m1), (h2, m2), label in _RUSH_WINDOWS:
        start, end = h1 + m1 / 60, h2 + m2 / 60
        if start <= hour < end:
            # Morning/evening rush are worst; midday milder.
            factor = 0.35 if "rush" in label else 0.15
            return factor, label
    if 22 <= hour or hour < 5:
        return -0.10, "late night (light traffic)"
    return 0.0, "off-peak"


def analyze_route_deep(depart_hour: float | None = None,
                       selected: int | None = None) -> str:
    """In-depth analysis of the selected route: real road-type breakdown from
    OSRM, plus an ESTIMATED traffic/rush-hour impact for Vietnam.

    depart_hour: 0–23.99; defaults to now.
    """
    routes = _LAST["routes"]
    if not routes:
        return "Ask me for a route first, then I can analyze it."
    idx = _LAST["selected"] if selected is None else selected
    r = routes[idx]

    total = r["distance_m"] or 1.0
    bc = r.get("by_class", {})
    exp = bc.get("expressway", 0)
    hwy = bc.get("highway", 0)
    prov = bc.get("provincial", 0)
    urban = bc.get("urban", 0) + (total - (exp + hwy + prov + bc.get("urban", 0)))
    urban = max(urban, 0)

    def pct(x):
        return round(100 * x / total)

    # Road-type sentence (real OSRM data)
    parts_types = []
    if exp:  parts_types.append(f"{pct(exp)}% expressway (cao tốc)")
    if hwy:  parts_types.append(f"{pct(hwy)}% national highway (quốc lộ)")
    if prov: parts_types.append(f"{pct(prov)}% provincial road")
    if urban: parts_types.append(f"{pct(urban)}% city/other roads")
    road_line = "Road types: " + ", ".join(parts_types) + "." if parts_types else ""

    # Traffic estimate (rush-hour affects the urban portion most)
    import datetime as _dt
    hour = depart_hour if depart_hour is not None else (
        _dt.datetime.now().hour + _dt.datetime.now().minute / 60)
    factor, label = _rush_factor(hour)
    base_s = r["duration_s"]
    urban_frac = urban / total
    # Rush hour mainly slows urban roads; expressways stay near free-flow.
    extra_s = base_s * urban_frac * max(factor, 0)
    worst_s = base_s + base_s * urban_frac * 0.6      # heaviest plausible delay

    congested = ""
    if factor > 0.2:
        congested = (f"Leaving now is in {label}; expect city sections to be "
                     f"congested. ")
    elif factor > 0:
        congested = f"Leaving now is {label} — light delays in town. "
    else:
        congested = f"Leaving now is {label}. "

    eta_now = _fmt_dur(base_s + extra_s)
    worst = _fmt_dur(worst_s)

    # When is it jammed? name the rush windows that overlap urban travel.
    jam_hint = ("Busiest times on the city parts: 7–9 AM and 4:30–7:30 PM. "
                if urban_frac > 0.15 else
                "Mostly highway/expressway, so rush hour has little effect. ")

    return (
        f"Route {idx+1} analysis — {r['dist_km']:.0f} km, normally {r['eta_text']}.\n"
        f"{road_line}\n"
        f"{congested}Estimated time now: about {eta_now}. Worst case in heavy "
        f"traffic: up to {worst}.\n"
        f"{jam_hint}"
        f"(Road types are from map data; traffic timing is an estimate — no live "
        f"traffic feed.)"
    )
