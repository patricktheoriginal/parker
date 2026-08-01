import webbrowser
from urllib.parse import quote_plus

# This assistant is Vietnam-focused: default to Vietnam and localize province/
# city lookups so results are for the right place (e.g. "Hue" → Hue, Vietnam).
_DEFAULT_LOCATION = "Vietnam"

# Common Vietnamese provinces/cities (lowercased, no diacritics) that should be
# pinned to Vietnam when the user names them without a country.
_VN_PLACES = {
    "hanoi", "ha noi", "ho chi minh", "ho chi minh city", "saigon", "sai gon", "hcmc",
    "da nang", "danang", "hue", "can tho", "hai phong", "haiphong", "nha trang",
    "da lat", "dalat", "vung tau", "sapa", "sa pa", "quy nhon", "vinh", "buon ma thuot",
    "phan thiet", "ha long", "halong", "ninh binh", "hoi an", "phu quoc", "bac ninh",
    "nam dinh", "thai nguyen", "thanh hoa", "nghe an", "quang ninh", "lao cai",
    "binh duong", "dong nai", "long an", "tien giang", "an giang", "kien giang",
    "ca mau", "soc trang", "tra vinh", "ben tre", "vinh long", "dong thap", "tay ninh",
    "ba ria", "lam dong", "dak lak", "gia lai", "kon tum", "quang nam", "quang ngai",
    "binh dinh", "phu yen", "khanh hoa", "ninh thuan", "binh thuan", "quang tri",
    "quang binh", "ha tinh", "hoa binh", "son la", "dien bien", "lai chau", "yen bai",
    "tuyen quang", "ha giang", "cao bang", "bac kan", "lang son", "bac giang",
    "phu tho", "vinh phuc", "hung yen", "hai duong", "thai binh", "ha nam",
}


def _localize_vn(city: str) -> str:
    """Append ', Vietnam' to a bare Vietnamese place name so results are correct."""
    key = city.lower().strip()
    if any(vn in key for vn in _VN_PLACES) and "vietnam" not in key and "viet nam" not in key:
        return f"{city}, Vietnam"
    return city


def weather_action(
    parameters: dict,
    player=None,
    session_memory=None,
) -> str:
    city     = parameters.get("city")
    when     = parameters.get("time", "today")

    # No city given → default to Vietnam instead of erroring out.
    if not city or not isinstance(city, str) or not city.strip():
        city = _DEFAULT_LOCATION

    city = _localize_vn(city.strip())
    when = (when or "today").strip()

    search_query  = f"weather in {city} {when}"
    url           = f"https://www.google.com/search?q={quote_plus(search_query)}"

    try:
        opened = webbrowser.open(url)
        if not opened:
            raise RuntimeError("webbrowser.open returned False")
    except Exception as e:
        msg = f"Sir, I couldn't open the browser for the weather report: {e}"
        _log(msg, player)
        return msg

    msg = f"Showing the weather for {city}, {when}, sir."
    _log(msg, player)

    if session_memory:
        try:
            session_memory.set_last_search(query=search_query, response=msg)
        except Exception:
            pass

    return msg


def _log(message: str, player=None) -> None:
    print(f"[Weather] {message}")
    if player:
        try:
            player.write_log(f"Parker: {message}")
        except Exception:
            pass


# ── Rain forecast (Open-Meteo — free, no API key needed) ─────────────────────
import json as _json
import urllib.request as _urlreq
import urllib.parse as _urlparse

_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
_VN_TZ = "Asia/Ho_Chi_Minh"


def _http_json(url: str, timeout: int = 12) -> dict:
    req = _urlreq.Request(url, headers={"User-Agent": "Parker/1.0"})
    with _urlreq.urlopen(req, timeout=timeout) as resp:
        return _json.load(resp)


# ── Current location (IP-based geolocation — free, no key, no permission) ─────
import time as _time

_LOC_CACHE: dict = {}          # cached result of the last successful lookup
_LOC_TTL = 900                 # re-check at most every 15 minutes


def current_location() -> dict | None:
    """Best-effort current location via IP geolocation.

    Returns {'lat', 'lon', 'city', 'region', 'country', 'label'} or None.
    No GPS/permission needed — accurate to the city level. Cached briefly so a
    burst of calls doesn't hammer the service.
    """
    now = _time.time()
    cached = _LOC_CACHE.get("data")
    if cached and (now - _LOC_CACHE.get("t", 0)) < _LOC_TTL:
        return cached

    # Primary: ip-api.com (reliable, generous). Fallback: ipapi.co.
    for url, keymap in (
        ("http://ip-api.com/json/?fields=status,city,regionName,country,lat,lon",
         {"lat": "lat", "lon": "lon", "city": "city", "region": "regionName", "country": "country"}),
        ("https://ipapi.co/json/",
         {"lat": "latitude", "lon": "longitude", "city": "city", "region": "region", "country": "country_name"}),
    ):
        try:
            d = _http_json(url, timeout=8)
            if d.get("status") == "fail":
                continue
            lat = d.get(keymap["lat"]); lon = d.get(keymap["lon"])
            if lat is None or lon is None:
                continue
            city = d.get(keymap["city"]) or ""
            region = d.get(keymap["region"]) or ""
            country = d.get(keymap["country"]) or ""
            label = ", ".join([p for p in (city, region, country) if p]) or "your location"
            result = {
                "lat": float(lat), "lon": float(lon),
                "city": city, "region": region, "country": country, "label": label,
            }
            _LOC_CACHE["data"] = result
            _LOC_CACHE["t"] = now
            return result
        except Exception:
            continue
    return None


def _geocode_search(name: str, count: int = 5) -> list[dict]:
    q = _urlparse.urlencode({"name": name, "count": count, "language": "en", "format": "json"})
    try:
        return _http_json(f"{_GEOCODE_URL}?{q}").get("results") or []
    except Exception:
        return []


def _geocode_vn(place: str) -> tuple[float, float, str] | None:
    """Resolve a place to (lat, lon, display_name), biased strongly to Vietnam.

    Tries the name as-is and a spaced variant (e.g. 'Sapa' -> 'Sa Pa'), and
    prefers any Vietnamese match before falling back to the top global hit.
    """
    variants = [place]
    # Insert spaces between run-together syllables people often type (Sapa, Danang)
    low = place.lower()
    _spaced = {"sapa": "Sa Pa", "danang": "Da Nang", "haiphong": "Hai Phong",
               "dalat": "Da Lat", "halong": "Ha Long", "hochiminh": "Ho Chi Minh"}
    if low in _spaced:
        variants.append(_spaced[low])

    candidates: list[dict] = []
    for name in variants:
        candidates.extend(_geocode_search(name))
        # Prefer a Vietnamese hit as soon as we find one
        vn = next((c for c in candidates if c.get("country_code") == "VN"), None)
        if vn:
            r = vn
            break
    else:
        if not candidates:
            return None
        # No VN match across variants — take the top global result
        r = candidates[0]
    name = r.get("name", place)
    admin = r.get("admin1", "")
    country = r.get("country", "")
    label = ", ".join([p for p in (name, admin if admin and admin != name else "", country) if p])
    return (r["latitude"], r["longitude"], label or name)


def rain_forecast(parameters: dict, player=None, session_memory=None) -> str:
    """Return a short English rain forecast for a Vietnamese city/province.

    Uses Open-Meteo (no API key). Defaults to Vietnam when no place is given.
    """
    place = (parameters.get("city") or parameters.get("place") or "").strip()
    days  = parameters.get("days", 3)
    try:
        days = max(1, min(7, int(days)))
    except (TypeError, ValueError):
        days = 3

    if not place:
        # No place given → use the device's actual current location.
        loc = current_location()
        if loc:
            lat, lon, label = loc["lat"], loc["lon"], loc["label"]
        else:
            lat, lon, label = _geocode_vn("Hanoi")
    else:
        geo = _geocode_vn(place)
        if not geo:
            msg = f"Sir, I couldn't locate '{place}' for a rain forecast."
            _log(msg, player)
            return msg
        lat, lon, label = geo

    params = _urlparse.urlencode({
        "latitude": lat, "longitude": lon,
        "daily": "precipitation_probability_max,precipitation_sum",
        "forecast_days": days, "timezone": _VN_TZ,
    })
    try:
        data = _http_json(f"{_FORECAST_URL}?{params}")
        daily = data["daily"]
        dates = daily["time"]
        probs = daily["precipitation_probability_max"]
        sums  = daily["precipitation_sum"]
    except Exception as e:
        msg = f"Sir, I couldn't fetch the rain forecast: {e}"
        _log(msg, player)
        return msg

    parts = [f"Rain forecast for {label}:"]
    for i in range(min(days, len(dates))):
        prob = probs[i] if probs[i] is not None else 0
        mm   = sums[i] if sums[i] is not None else 0
        when = "Today" if i == 0 else ("Tomorrow" if i == 1 else dates[i])
        if prob >= 70:
            desc = "heavy rain likely"
        elif prob >= 40:
            desc = "rain possible"
        elif prob >= 15:
            desc = "slight chance of rain"
        else:
            desc = "mostly dry"
        parts.append(f"{when}: {desc} — {prob}% chance, {mm:.1f} mm expected.")

    msg = " ".join(parts)
    _log(msg, player)
    if session_memory:
        try:
            session_memory.set_last_search(query=f"rain forecast {label}", response=msg)
        except Exception:
            pass
    return msg

def where_am_i(parameters: dict = None, player=None, session_memory=None) -> str:
    """Report the user's current approximate location (IP-based)."""
    loc = current_location()
    if not loc:
        msg = "Sir, I couldn't determine your current location right now."
        _log(msg, player)
        return msg
    msg = f"You appear to be in {loc['label']}, sir."
    _log(msg, player)
    return msg
