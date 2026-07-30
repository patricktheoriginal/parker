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
import webbrowser
from datetime import datetime, timedelta
from urllib.parse import quote_plus, urlencode
from urllib.request import Request, urlopen

# Reuse the Vietnam geocoding logic already used by the weather module so
# provinces like "Sapa" resolve correctly to Vietnam.
from actions.weather_report import _geocode_vn, _http_json, _VN_TZ

_OSRM_URL = "https://router.project-osrm.org/route/v1/driving"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"


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


def route_directions(parameters: dict, player=None, session_memory=None) -> str:
    """Plan a Vietnam driving route with timing and along-the-route weather,
    then open it on Google Maps."""
    dest = (parameters.get("destination") or parameters.get("to") or "").strip()
    origin = (parameters.get("origin") or parameters.get("from") or "").strip()
    depart = (parameters.get("depart_time") or parameters.get("time") or "").strip()

    if not dest:
        msg = "Sir, please tell me the destination for the route."
        _log(msg, player)
        return msg
    if not origin:
        # No origin given — default to Hanoi so we can still plan a route.
        origin = "Hanoi"

    o = _geocode_vn(origin)
    d = _geocode_vn(dest)
    if not o:
        msg = f"Sir, I couldn't locate the origin '{origin}'."
        _log(msg, player)
        return msg
    if not d:
        msg = f"Sir, I couldn't locate the destination '{dest}'."
        _log(msg, player)
        return msg

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

    # Open Google Maps driving directions (user can tilt into 3D inside Maps)
    gmaps = ("https://www.google.com/maps/dir/?api=1"
             f"&origin={quote_plus(o_label)}"
             f"&destination={quote_plus(d_label)}"
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
    if opened:
        parts.append("I've opened the route on Google Maps.")
    else:
        parts.append(f"Open it here: {gmaps}")

    msg = " ".join(parts)
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
