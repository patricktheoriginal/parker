"""
maps_route.py — Vietnam driving routes for Parker.

Given an origin and destination (both assumed to be in Vietnam), this:
  1. Geocodes both places (Open-Meteo geocoding, biased to Vietnam)
  2. Gets the fastest driving route from OSRM (free, no API key) —
     distance, duration, and the road geometry
  3. Samples a few points along the route and fetches weather for each
     (Open-Meteo), so Parker can describe conditions along the way
  4. Computes a concrete depart/arrive time
  5. Opens the route on Google Maps in the browser (2D directions the user
     can tilt/rotate into 3D inside Maps)

No API keys required.
"""

import json
import math
import re
import time
import webbrowser
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen

# Reuse the province-level geocoder from the weather module as a fallback,
# plus the shared IP-based current-location lookup.
from actions.weather_report import (
    _geocode_vn as _geocode_province, _http_json, _VN_TZ, current_location,
)

_OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"
_PHOTON_URL = "https://photon.komoot.io/api/"
# Nominatim's usage policy requires an identifying User-Agent.
_UA = "Parker-Assistant/1.0 (Vietnam route planner)"


def _photon_one(query: str) -> tuple[float, float, str] | None:
    """Photon geocoder (OSM-based) — ranks landmarks/POIs and diacritic names
    better than Nominatim, matching Google Maps more closely. Biased to Vietnam."""
    if not query or not query.strip():
        return None
    q = urlencode({"q": query.strip(), "limit": 1, "lang": "en",
                   "lat": 16.0, "lon": 106.0})   # bias toward Vietnam
    try:
        req = Request(f"{_PHOTON_URL}?{q}", headers={"User-Agent": _UA})
        with urlopen(req, timeout=12) as resp:
            data = json.load(resp)
        feats = data.get("features", [])
        if not feats:
            return None
        f = feats[0]
        pr = f.get("properties", {})
        if (pr.get("countrycode") or "").upper() not in ("VN", ""):
            return None            # keep results inside Vietnam
        lon, lat = f["geometry"]["coordinates"]
        label = ", ".join([p for p in (
            pr.get("name"), pr.get("street"),
            pr.get("city") or pr.get("district") or pr.get("county"),
            pr.get("country")) if p][:3])
        return (float(lat), float(lon), label or query)
    except Exception:
        return None


def _nominatim_one(query: str) -> tuple[float, float, str] | None:
    """Single Nominatim lookup restricted to Vietnam."""
    if not query or not query.strip():
        return None
    q = urlencode({
        "q": query.strip(), "format": "json", "limit": 1,
        "countrycodes": "vn", "accept-language": "en", "addressdetails": 0,
    })
    try:
        req = Request(f"{_NOMINATIM_URL}?{q}", headers={"User-Agent": _UA})
        with urlopen(req, timeout=12) as resp:
            data = json.load(resp)
        if data:
            top = data[0]
            name = top.get("display_name", query)
            parts = [p.strip() for p in name.split(",") if p.strip()]
            short = ", ".join(parts[:3])
            return (float(top["lat"]), float(top["lon"]), short or query)
    except Exception:
        pass
    return None


def _address_variants(place: str) -> list[str]:
    """Progressively simpler forms of an address so a too-specific street
    number that OSM doesn't have still resolves to the street / ward / city.

    e.g. "2 Hai Trieu, Ben Nghe, District 1, HCMC" ->
         [full, "Hai Trieu, Ben Nghe, District 1, HCMC",
          "Ben Nghe, District 1, HCMC", "District 1, HCMC", "HCMC"]
    """
    place = place.strip()
    variants = [place]
    parts = [p.strip() for p in place.split(",") if p.strip()]
    # Drop a leading house number token (e.g. "2 Hai Trieu" -> "Hai Trieu")
    if parts and re.match(r"^\d+\S*\s+\S", parts[0]):
        stripped = re.sub(r"^\d+\S*\s*", "", parts[0]).strip()
        variants.append(", ".join([stripped] + parts[1:]))
    # Drop leading comma-parts one at a time (street -> ward -> district -> city)
    for i in range(1, len(parts)):
        variants.append(", ".join(parts[i:]))
    # De-dupe while preserving order
    seen, out = set(), []
    for v in variants:
        if v and v not in seen:
            seen.add(v)
            out.append(v)
    return out


def _geocode_place(place: str) -> tuple[float, float, str] | None:
    """Resolve any Vietnam place — landmarks, airports, markets, districts,
    diacritic names, AND specific street addresses — to (lat, lon, label).

    Tries the full address first, then progressively simpler forms (dropping the
    house number, then leading address parts) so a precise address that OSM lacks
    still snaps to the right street/ward/city. Falls back to the province
    geocoder as a last resort.
    """
    if not place or not place.strip():
        return None

    has_house_number = bool(re.match(r"^\s*\d+\S*\s+\S", place.strip()))

    # For a specific street address (starts with a house number), Nominatim is
    # usually more precise; otherwise Photon ranks landmarks/POIs/named places
    # much better and closer to Google Maps.
    if not has_house_number:
        g = _photon_one(place)
        if g:
            return g

    variants = _address_variants(place)
    for i, v in enumerate(variants):
        g = _nominatim_one(v)
        if g:
            return g
        if i < len(variants) - 1:
            time.sleep(1.1)   # respect Nominatim's ~1 req/sec policy

    # Address with a house number that Nominatim couldn't place → let Photon try.
    if has_house_number:
        g = _photon_one(place)
        if g:
            return g

    # Last resort: province/city geocoder (Open-Meteo)
    return _geocode_province(place)


def _fmt_duration(seconds: float) -> str:
    total_min = int(round(seconds / 60))
    h, m = divmod(total_min, 60)
    if h and m:
        return f"{h} h {m} min"
    if h:
        return f"{h} h"
    return f"{m} min"


def _parse_depart_time(when: str) -> datetime:
    """Parse a rough departure time like '7am', '15:30', 'now' → datetime today.

    Falls back to now. Only handles simple, common forms — the LLM passes a
    clean HH:MM or an hour when it can.
    """
    now = datetime.now()
    if not when:
        return now
    w = when.strip().lower()
    if w in ("now", "immediately", "right now"):
        return now
    # HH:MM
    try:
        if ":" in w:
            hh, mm = w.split(":")[0:2]
            mm = "".join(ch for ch in mm if ch.isdigit()) or "0"
            dt = now.replace(hour=int(hh) % 24, minute=int(mm) % 60,
                             second=0, microsecond=0)
            return dt if dt >= now else dt + timedelta(days=1)
    except Exception:
        pass
    # "7am" / "7 am" / "7pm" / "7"
    try:
        digits = "".join(ch for ch in w if ch.isdigit())
        if digits:
            hour = int(digits) % 24
            if "pm" in w and hour < 12:
                hour += 12
            if "am" in w and hour == 12:
                hour = 0
            dt = now.replace(hour=hour, minute=0, second=0, microsecond=0)
            return dt if dt >= now else dt + timedelta(days=1)
    except Exception:
        pass
    return now


def _weather_summary(lat: float, lon: float) -> str:
    """One-line current weather at a point along the route."""
    params = urlencode({
        "latitude": lat, "longitude": lon,
        "current": "temperature_2m,precipitation,weather_code",
        "timezone": _VN_TZ,
    })
    try:
        cur = _http_json(f"{_FORECAST_URL}?{params}")["current"]
        temp = cur.get("temperature_2m")
        precip = cur.get("precipitation", 0) or 0
        rain = "rain" if precip > 0.1 else "dry"
        return f"{round(temp)}°C, {rain}"
    except Exception:
        return "weather unavailable"


def _sample_points(coords: list, n: int = 3) -> list:
    """Pick ~n evenly spaced points along the route (excluding exact endpoints)."""
    if len(coords) <= 2:
        return coords
    step = len(coords) / (n + 1)
    idxs = sorted({int(step * (i + 1)) for i in range(n)})
    idxs = [i for i in idxs if 0 < i < len(coords)]
    return [coords[i] for i in idxs]


def _maps_endpoint(text: str, geo) -> str:
    """Prefer the place TEXT (Google resolves names/addresses better than raw
    coordinates); fall back to coordinates only if we have no text."""
    t = (text or "").strip()
    if t:
        if "vietnam" not in t.lower() and "viet nam" not in t.lower():
            t = f"{t}, Vietnam"
        return t
    if geo:
        return f"{geo[0]},{geo[1]}"
    return t


def _open_maps_by_text(origin: str, dest: str, o, d, player) -> str:
    """Open Google Maps directions using text for any endpoint we couldn't
    geocode. Google Maps resolves specific addresses reliably even when the
    free geocoder can't, so the user still gets their route."""
    gmaps = ("https://www.google.com/maps/dir/?api=1"
             f"&origin={quote_plus(_maps_endpoint(origin, o))}"
             f"&destination={quote_plus(_maps_endpoint(dest, d))}"
             "&travelmode=driving")
    opened = False
    try:
        opened = webbrowser.open(gmaps)
    except Exception:
        opened = False

    o_name = o[2] if o else origin
    d_name = d[2] if d else dest
    if opened:
        msg = (f"I've opened driving directions from {o_name} to {d_name} on "
               f"Google Maps. I couldn't compute the exact drive time and "
               f"along-route weather for that specific address, but Maps will "
               f"show the fastest route and timing.")
    else:
        msg = f"Open directions here: {gmaps}"
    _log(msg, player)
    if player is not None:
        try:
            player.show_content(
                "Route",
                f"ROUTE — {o_name} → {d_name}\n\n"
                f"(Opened on Google Maps for a specific address.)\n\n{gmaps}",
            )
        except Exception:
            pass
    return msg


def different_route(parameters: dict = None, player=None, session_memory=None) -> str:
    """Cycle to the next alternative of the last computed route and re-show the
    3D map with per-route analysis."""
    from actions.route_engine import get_last, select_next, render_map, describe
    last = get_last()
    if not last["routes"]:
        return "Sir, ask me for a route first, then I can show alternatives."
    if len(last["routes"]) <= 1:
        return "That was the only route I could find for this trip, sir."
    idx = select_next()
    render_map(player)
    r = last["routes"][idx]
    _log(f"Switched to route {idx+1}: {r['dist_km']:.0f} km, {r['eta_text']} — {r['analysis']}.", player)
    return (f"Showing route {idx+1} of {len(last['routes'])}: "
            f"{r['dist_km']:.0f} km, {r['eta_text']}, {r['analysis']}.\n"
            + describe(last["routes"], idx))


def analyze_route(parameters: dict = None, player=None, session_memory=None) -> str:
    """In-depth analysis of the current route: road types (expressway/highway/
    city), plus an estimated rush-hour/traffic impact and worst-case time."""
    from actions.route_engine import analyze_route_deep, get_last
    if not get_last()["routes"]:
        return "Sir, ask me for a route first, then I can analyze it in depth."
    params = parameters or {}
    depart_hour = None
    when = (params.get("depart_time") or params.get("time") or "").strip()
    if when:
        dt = _parse_depart_time(when)
        depart_hour = dt.hour + dt.minute / 60
    msg = analyze_route_deep(depart_hour=depart_hour)
    _log(msg, player)
    return msg


def route_directions(parameters: dict, player=None, session_memory=None) -> str:
    """Plan a Vietnam driving route: compute alternative routes with analysis,
    show a 3D map, and give timing + along-route weather."""
    # "different route" / "another way" → cycle the last result.
    _action = (parameters.get("action") or "").strip().lower()
    if _action in ("different", "different_route", "alternative", "another", "next"):
        return different_route(parameters, player, session_memory)

    dest = (parameters.get("destination") or parameters.get("to") or "").strip()
    origin = (parameters.get("origin") or parameters.get("from") or "").strip()
    depart = (parameters.get("depart_time") or parameters.get("time") or "").strip()

    if not dest:
        msg = "Sir, please tell me the destination for the route."
        _log(msg, player)
        return msg

    # No origin given → use the current location: a manually set location if the
    # user gave one, else GPS (Windows), else an approximate IP-based location.
    o = None
    if not origin:
        loc = current_location()
        if loc:
            o = (loc["lat"], loc["lon"], loc["label"])
            origin = loc["label"]
        else:
            msg = ("Sir, I couldn't determine your starting point. Tell me where "
                   "you are — for example, 'I'm in Da Nang' — then ask again.")
            _log(msg, player)
            return msg

    if o is None:
        o = _geocode_place(origin)
        time.sleep(1.1)   # Nominatim usage policy: ~1 request/second
    d = _geocode_place(dest)

    # If we couldn't pin exact coordinates (e.g. a very specific address OSM
    # doesn't have), still open Google Maps using the raw text — Google resolves
    # addresses well — instead of failing with "unable to find".
    if not o or not d:
        return _open_maps_by_text(origin, dest, o, d, player)

    o_lat, o_lon, o_label = o
    d_lat, d_lon, d_label = d

    # OSRM expects lon,lat;lon,lat
    osrm = (f"{_OSRM_URL}/{o_lon},{o_lat};{d_lon},{d_lat}"
            f"?overview=simplified&geometries=geojson&alternatives=false")
    try:
        data = _http_json(osrm)
        route = data["routes"][0]
        dist_km = route["distance"] / 1000.0
        dur_s = route["duration"]
        coords = route["geometry"]["coordinates"]  # [lon, lat] pairs
    except Exception as e:
        msg = f"Sir, I couldn't compute the route: {e}"
        _log(msg, player)
        return msg

    depart_dt = _parse_depart_time(depart)
    arrive_dt = depart_dt + timedelta(seconds=dur_s)

    # Weather along the route: sample a few midpoints
    along = []
    for lon, lat in _sample_points(coords, n=3):
        along.append(_weather_summary(lat, lon))

    # ── Alternative routes + 3D map ──────────────────────────────────────────
    routes_summary = ""
    try:
        from actions.route_engine import compute_routes, render_map, describe
        depart_epoch = int(depart_dt.timestamp()) if depart_dt else None
        alts = compute_routes((o_lat, o_lon), (d_lat, d_lon),
                              o_label, d_label, depart_epoch)
        if alts:
            render_map(player)               # show the 3D route map in Parker
            routes_summary = describe(alts, 0)
    except Exception as e:
        print(f"[Route] alternatives/map failed: {e}")

    # Google Maps resolves place names/addresses better than raw coordinates, so
    # pass the exact text the user asked for (biased to Vietnam). This makes the
    # Google Maps route match what was requested instead of an OSM point that may
    # be slightly off.
    def _gmaps_place(text: str, lat: float, lon: float) -> str:
        t = (text or "").strip()
        if not t:
            return f"{lat},{lon}"
        if "vietnam" not in t.lower() and "viet nam" not in t.lower():
            t = f"{t}, Vietnam"
        return t

    gmaps = ("https://www.google.com/maps/dir/?api=1"
             f"&origin={quote_plus(_gmaps_place(origin, o_lat, o_lon))}"
             f"&destination={quote_plus(_gmaps_place(dest, d_lat, d_lon))}"
             "&travelmode=driving")
    opened = False
    try:
        opened = webbrowser.open(gmaps)
    except Exception:
        opened = False

    # Build a natural English summary for the voice reply
    parts = [
        f"Fastest route from {o_label} to {d_label}:",
        f"about {dist_km:.0f} km, {_fmt_duration(dur_s)} driving.",
        f"Leaving at {depart_dt.strftime('%H:%M')}, you'd arrive around {arrive_dt.strftime('%H:%M')}.",
    ]
    if along:
        parts.append("Weather along the way: " + "; ".join(along) + ".")
    parts.append("I've opened the 3D route map.")
    if routes_summary and "route(s)" in routes_summary:
        # Mention alternatives count and that the user can ask for a different one.
        parts.append("Say 'different route' to see alternatives.")

    msg = " ".join(parts)
    if routes_summary:
        msg = msg + "\n" + routes_summary
    _log(msg, player)

    # Show it on the content panel too, if available
    if player is not None:
        try:
            panel = (
                f"ROUTE — {o_label} → {d_label}\n\n"
                f"Distance : {dist_km:.1f} km\n"
                f"Drive    : {_fmt_duration(dur_s)}\n"
                f"Depart   : {depart_dt.strftime('%a %H:%M')}\n"
                f"Arrive   : {arrive_dt.strftime('%a %H:%M')}\n\n"
                f"Weather along route:\n  - " + "\n  - ".join(along) + "\n\n"
                f"Google Maps:\n{gmaps}"
            )
            player.show_content("Route", panel)
        except Exception:
            pass

    if session_memory:
        try:
            session_memory.set_last_search(query=f"route {o_label} to {d_label}", response=msg)
        except Exception:
            pass

    return msg


def _log(message: str, player=None) -> None:
    print(f"[Maps] {message}")
    if player:
        try:
            player.write_log(f"Parker: {message}")
        except Exception:
            pass
