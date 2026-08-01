"""
nearby.py — find nearby places and get place info (like Google Maps).

Uses OpenStreetMap data (free, no API key):
  - Overpass API for "nearby" points of interest around a location
  - Nominatim/Photon (via maps_route) to resolve a place name to coordinates

Note: OSM's coverage in Vietnam is thinner than Google — some places lack a
name, hours, or reviews. Results are best-effort.
"""

import json
from urllib.parse import urlencode
from urllib.request import Request, urlopen

from actions.weather_report import current_location

_OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",   # mirror if the first is busy
]
_UA = "Parker-Assistant/1.0 (Vietnam places)"

# Map friendly words → OSM amenity/shop/tourism tags.
_CATEGORY_TAGS = {
    "cafe": 'amenity=cafe', "coffee": 'amenity=cafe',
    "restaurant": 'amenity=restaurant', "food": 'amenity=restaurant',
    "atm": 'amenity=atm', "bank": 'amenity=bank',
    "gas": 'amenity=fuel', "fuel": 'amenity=fuel', "petrol": 'amenity=fuel',
    "hospital": 'amenity=hospital', "pharmacy": 'amenity=pharmacy',
    "hotel": 'tourism=hotel', "parking": 'amenity=parking',
    "supermarket": 'shop=supermarket', "market": 'amenity=marketplace',
    "school": 'amenity=school', "police": 'amenity=police',
    "bar": 'amenity=bar', "pub": 'amenity=pub',
    "convenience": 'shop=convenience', "store": 'shop=convenience',
    "bus": 'amenity=bus_station', "toilet": 'amenity=toilets',
    "cinema": 'amenity=cinema', "bakery": 'shop=bakery',
}


def _overpass(query: str, timeout: int = 25) -> dict:
    last_err = None
    for base in _OVERPASS_URLS:
        try:
            req = Request(base, data=urlencode({"data": query}).encode(),
                          headers={"User-Agent": _UA})
            with urlopen(req, timeout=timeout) as r:
                return json.load(r)
        except Exception as e:
            last_err = e
            continue
    raise RuntimeError(f"Overpass unavailable: {last_err}")


def _match_category(text: str) -> tuple[str, str]:
    """Return (osm_filter, label) for a free-text category."""
    t = (text or "").lower().strip()
    for key, tag in _CATEGORY_TAGS.items():
        if key in t:
            return tag, key
    # Fall back to a name search
    return f'name~"{text}",i', text


def _haversine_m(a_lat, a_lon, b_lat, b_lon) -> float:
    import math
    dlat = math.radians(b_lat - a_lat)
    dlon = math.radians(b_lon - a_lon)
    x = (math.sin(dlat / 2) ** 2 +
         math.cos(math.radians(a_lat)) * math.cos(math.radians(b_lat)) *
         math.sin(dlon / 2) ** 2)
    return 6371000 * 2 * math.asin(math.sqrt(x))


def find_nearby(parameters: dict, player=None, session_memory=None) -> str:
    """Find places of a category near the user's location (or a given place)."""
    params = parameters or {}
    what = (params.get("query") or params.get("category") or params.get("what") or "").strip()
    near = (params.get("near") or params.get("location") or "").strip()
    radius = int(params.get("radius", 1500) or 1500)
    radius = max(200, min(radius, 5000))

    if not what:
        return "Sir, what should I look for nearby — cafes, ATMs, restaurants…?"

    # Resolve the search center.
    if near:
        from actions.maps_route import _geocode_place
        geo = _geocode_place(near)
        if not geo:
            return f"Sir, I couldn't find '{near}'."
        lat, lon, center_label = geo[0], geo[1], geo[2]
    else:
        loc = current_location()
        if not loc:
            return ("Sir, I don't know your location. Tell me where you are, or "
                    "name a place to search near.")
        lat, lon, center_label = loc["lat"], loc["lon"], loc["label"]

    osm_filter, label = _match_category(what)
    query = (f"[out:json][timeout:20];"
             f"nwr[{osm_filter}](around:{radius},{lat},{lon});"
             f"out center 20;")
    try:
        data = _overpass(query)
    except Exception as e:
        return f"Sir, the places service is busy right now: {e}"

    items = []
    for el in data.get("elements", []):
        tags = el.get("tags", {})
        name = tags.get("name")
        if not name:
            continue
        p_lat = el.get("lat") or (el.get("center") or {}).get("lat")
        p_lon = el.get("lon") or (el.get("center") or {}).get("lon")
        if p_lat is None or p_lon is None:
            continue
        dist = _haversine_m(lat, lon, p_lat, p_lon)
        items.append({"name": name, "dist": dist,
                      "hours": tags.get("opening_hours", ""),
                      "lat": p_lat, "lon": p_lon})

    if not items:
        return (f"Sir, I couldn't find any {label} within {radius} m of "
                f"{center_label}. OSM coverage here may be limited.")

    items.sort(key=lambda x: x["dist"])
    items = items[:8]

    lines = [f"{label.title()} near {center_label}:"]
    for it in items:
        d = f"{it['dist']:.0f} m" if it["dist"] < 1000 else f"{it['dist']/1000:.1f} km"
        hrs = f" · {it['hours']}" if it["hours"] else ""
        lines.append(f"  - {it['name']} ({d}){hrs}")
    msg = "\n".join(lines)

    # Show them on the map (reuse the route map layer with points as markers).
    _show_places_on_map(center_label, items, player)

    if player:
        try:
            player.write_log(f"[places] {label} near {center_label}: {len(items)}")
        except Exception:
            pass
    return msg


def place_info(parameters: dict, player=None, session_memory=None) -> str:
    """Return info about a specific place (address, type, hours if known)."""
    params = parameters or {}
    name = (params.get("place") or params.get("query") or params.get("name") or "").strip()
    if not name:
        return "Sir, which place do you want information about?"

    from actions.maps_route import _geocode_place
    geo = _geocode_place(name)
    if not geo:
        return f"Sir, I couldn't find '{name}'."
    lat, lon, label = geo

    # Query OSM tags for the nearest matching feature.
    query = (f'[out:json][timeout:20];'
             f'nwr(around:80,{lat},{lon});out tags center 10;')
    tags = {}
    try:
        data = _overpass(query)
        # Prefer an element whose name matches the query.
        best = None
        for el in data.get("elements", []):
            t = el.get("tags", {})
            if not t.get("name"):
                continue
            if name.lower() in t["name"].lower():
                best = t
                break
            best = best or t
        tags = best or {}
    except Exception:
        tags = {}

    parts = [f"{label}"]
    typ = (tags.get("amenity") or tags.get("shop") or tags.get("tourism")
           or tags.get("office") or tags.get("leisure"))
    if typ:
        parts.append(f"Type: {typ.replace('_', ' ')}")
    if tags.get("opening_hours"):
        parts.append(f"Hours: {tags['opening_hours']}")
    if tags.get("phone") or tags.get("contact:phone"):
        parts.append(f"Phone: {tags.get('phone') or tags.get('contact:phone')}")
    if tags.get("website") or tags.get("contact:website"):
        parts.append(f"Website: {tags.get('website') or tags.get('contact:website')}")

    if len(parts) == 1:
        parts.append("(OSM has limited details for this place.)")

    msg = " · ".join(parts)
    # Show the single place on the map.
    _show_places_on_map(label, [{"name": name, "lat": lat, "lon": lon, "dist": 0,
                                 "hours": tags.get("opening_hours", "")}], player)
    if player:
        try:
            player.write_log(f"[place] {name}")
        except Exception:
            pass
    return msg


def _show_places_on_map(center_label: str, items: list, player) -> None:
    """Render each place as its own pin on the 3D satellite map."""
    if player is None or not items:
        return
    try:
        from actions.route_engine import set_places, build_map_html
        places = [{"name": it["name"], "lat": it["lat"], "lon": it["lon"],
                   "dist": it.get("dist", 0), "hours": it.get("hours", "")}
                  for it in items]
        set_places(center_label, places)
        html = build_map_html()
        if html:
            player.show_route_map(html)
    except Exception as e:
        print(f"[Nearby] map failed: {e}")
