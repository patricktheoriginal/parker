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


# ── Current location ─────────────────────────────────────────────────────────
# On Windows we use the real GPS/OS location (Windows Location Service via
# PowerShell's GeoCoordinateWatcher). On other OSes we use IP geolocation.
import time as _time
import platform as _platform
import subprocess as _subprocess

_LOC_CACHE: dict = {}          # cached result of the last successful lookup
_LOC_TTL = 900                 # re-check at most every 15 minutes
_OS_NAME = _platform.system()  # "Windows" | "Darwin" | "Linux"
# Last Windows GPS attempt status: 'ok' | 'denied' | 'no_fix' | 'error' | ''
_LAST_GPS_STATUS = ""


def _reverse_geocode(lat: float, lon: float) -> dict:
    """lat/lon → {'city','region','country','label'} via Nominatim reverse."""
    try:
        q = _urlparse.urlencode({
            "lat": lat, "lon": lon, "format": "json", "zoom": 12,
            "accept-language": "en",
        })
        req = _urlreq.Request(
            f"https://nominatim.openstreetmap.org/reverse?{q}",
            headers={"User-Agent": "Parker-Assistant/1.0"})
        with _urlreq.urlopen(req, timeout=10) as resp:
            addr = (_json.load(resp).get("address") or {})
        city = (addr.get("city") or addr.get("town") or addr.get("village")
                or addr.get("county") or "")
        region = addr.get("state") or ""
        country = addr.get("country") or ""
        label = ", ".join([p for p in (city, region, country) if p]) or "your location"
        return {"city": city, "region": region, "country": country, "label": label}
    except Exception:
        return {"city": "", "region": "", "country": "",
                "label": f"{lat:.4f}, {lon:.4f}"}


# PowerShell that tries the modern WinRT Geolocator first (works correctly with
# the Windows 10/11 Location Service and reports the real access status), then
# falls back to the legacy GeoCoordinateWatcher. It prints one of:
#   OK|<lat>|<lon>      – got a position
#   DENIED              – Location off or app not allowed
#   NOFIX               – Location on but no position yet
#   ERROR|<message>     – something else went wrong
_WIN_GPS_PS = r"""
$ErrorActionPreference = 'Stop'
$inv = [System.Globalization.CultureInfo]::InvariantCulture
function Out-Pos($lat,$lon){ Write-Output ("OK|" + $lat.ToString($inv) + "|" + $lon.ToString($inv)) }

# --- Method A: legacy GeoCoordinateWatcher (System.Device.Location) ---
# Tried first now, not as a fallback: it's a plain .NET Framework class with
# no WinRT projection involved, and it uses the same underlying Windows
# Location Provider (GPS/Wi-Fi/IP, whichever the OS picks) as the WinRT
# Geolocator -- so there's no accuracy tradeoff to trying this first, only
# less to go wrong. The WinRT path below it needs
# System.Runtime.WindowsRuntime loaded from the GAC by exact strong name,
# which is a brittle, undocumented reflection trick that can fail outright
# on some Windows builds ("Could not load file or assembly...") even with a
# fully installed, current .NET Framework -- when that happens the whole
# method is a dead end, whereas GeoCoordinateWatcher just works.
#
# TryStart() only waits for the *permission prompt*, not for an actual
# position fix -- reading .Position immediately after it returns is a common
# mistake that looks like "no GPS" when the receiver just hasn't reported in
# yet. Polling Status/Position in a loop is what actually waits for the fix,
# up to the real GPS cold-fix window (20-40s is typical).
try {
    Add-Type -AssemblyName System.Device
    $w = New-Object System.Device.Location.GeoCoordinateWatcher('High')
    $null = $w.TryStart($true, [TimeSpan]::FromSeconds(10))
    if ($w.Permission -eq [System.Device.Location.GeoPositionPermission]::Denied) {
        Write-Output "DENIED"; exit
    }
    $deadline = (Get-Date).AddSeconds(45)
    while ((Get-Date) -lt $deadline) {
        if ($w.Status -eq [System.Device.Location.GeoPositionStatus]::Ready -and
            -not $w.Position.Location.IsUnknown) {
            $c = $w.Position.Location
            Out-Pos $c.Latitude $c.Longitude; exit
        }
        if ($w.Permission -eq [System.Device.Location.GeoPositionPermission]::Denied) {
            Write-Output "DENIED"; exit
        }
        Start-Sleep -Milliseconds 500
    }
    # Timed out without a fix -- fall through to Method B rather than giving
    # up, in case the WinRT path (which can use a different provider under
    # the hood) does better on this machine.
} catch { }

# --- Method B: WinRT Geolocator ---
# On Windows PowerShell 5 (.NET Framework) the WinRT interop extension type is
# in System.Runtime.WindowsRuntime, which must be loaded explicitly. This can
# fail with a FileNotFoundException on some Windows builds even with .NET
# Framework fully installed and current -- if so, this whole method is
# skipped and Method A above is the real answer.
try {
    [System.Reflection.Assembly]::Load('System.Runtime.WindowsRuntime, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a') | Out-Null

    $asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                       $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } |
        Select-Object -First 1
    function Await($op, $resultType, $waitMs = 20000) {
        $m = $asTask.MakeGenericMethod($resultType)
        $t = $m.Invoke($null, @($op))
        $t.Wait($waitMs) | Out-Null
        return $t.Result
    }

    $null = [Windows.Devices.Geolocation.Geolocator,Windows.Devices.Geolocation,ContentType=WindowsRuntime]
    $accType = [Windows.Devices.Geolocation.GeolocationAccessStatus]
    $status  = Await ([Windows.Devices.Geolocation.Geolocator]::RequestAccessAsync()) $accType
    if ($status -ne [Windows.Devices.Geolocation.GeolocationAccessStatus]::Allowed) {
        Write-Output "DENIED"; exit
    }
    $geo = New-Object Windows.Devices.Geolocation.Geolocator
    $geo.DesiredAccuracy = [Windows.Devices.Geolocation.PositionAccuracy]::High
    $geo.DesiredAccuracyInMeters = 10
    $posType = [Windows.Devices.Geolocation.Geoposition]
    $pos = Await ($geo.GetGeopositionAsync()) $posType 45000
    if ($pos -ne $null) {
        $p = $pos.Coordinate.Point.Position
        Out-Pos $p.Latitude $p.Longitude; exit
    }
    Write-Output "NOFIX"; exit
} catch {
    Write-Output ("ERROR|" + $_.Exception.Message); exit
}
Write-Output "NOFIX"
"""


def _windows_gps() -> tuple[dict | None, str]:
    """Real device location on Windows via the OS Location Service.

    Returns (result_dict_or_None, status) where status is one of:
    'ok' | 'denied' | 'no_fix' | 'error'. Requires Location Services enabled
    and desktop apps allowed to access location.
    """
    if _OS_NAME != "Windows":
        return None, "error"
    try:
        r = _subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive",
             "-ExecutionPolicy", "Bypass", "-Command", _WIN_GPS_PS],
            # 100s: Method A (GeoCoordinateWatcher) can wait up to 45s for a
            # fix, and if it times out without one, Method B (WinRT) is
            # tried too and can wait up to another 45s -- worst case both
            # run their full timeout back to back. Generous headroom on top
            # for PowerShell/reflection startup overhead, so a real GPS cold
            # fix isn't cut off by the Python-side timeout before either
            # PS-side attempt even finishes.
            capture_output=True, text=True, timeout=100,
            creationflags=getattr(_subprocess, "CREATE_NO_WINDOW", 0),
        )
        out = (r.stdout or "").strip()
        line = out.splitlines()[-1].strip() if out else ""
        if line.startswith("OK|"):
            _, lat_s, lon_s = line.split("|", 2)
            lat, lon = float(lat_s), float(lon_s)
            info = _reverse_geocode(lat, lon)
            return {"lat": lat, "lon": lon, "source": "gps", **info}, "ok"
        if line == "DENIED":
            return None, "denied"
        if line == "NOFIX":
            return None, "no_fix"
        print(f"[Location] Windows GPS unexpected output: {out!r} err={r.stderr!r}")
        return None, "error"
    except Exception as e:
        print(f"[Location] Windows GPS failed: {e}")
        return None, "error"


def _ip_location() -> dict | None:
    """City-level location via IP geolocation (no GPS/permission)."""
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
            return {"lat": float(lat), "lon": float(lon), "source": "ip",
                    "city": city, "region": region, "country": country, "label": label}
        except Exception:
            continue
    return None


_PHONE_GPS_TTL = 600     # a phone fix is considered current for 10 minutes


def _phone_location() -> dict | None:
    """A live GPS fix pushed from the paired phone (accurate to ~meters)."""
    try:
        from memory.config_manager import get_phone_gps
    except Exception:
        return None
    g = get_phone_gps()
    if not g:
        return None
    if (_time.time() - float(g.get("ts", 0))) > _PHONE_GPS_TTL:
        return None            # stale — don't use an old phone fix
    lat, lon = float(g["lat"]), float(g["lon"])
    info = _reverse_geocode(lat, lon)
    return {"lat": lat, "lon": lon, "source": "phone_gps", **info}


def _manual_location() -> dict | None:
    """A location the user set explicitly ('I'm in Da Lat'), geocoded."""
    try:
        from memory.config_manager import get_manual_location
    except Exception:
        return None
    place = get_manual_location()
    if not place:
        return None
    geo = _geocode_vn(place)
    if not geo:
        return None
    lat, lon, label = geo
    return {"lat": lat, "lon": lon, "source": "manual",
            "city": place, "region": "", "country": "", "label": label}


def current_location(gps_required: bool = False) -> dict | None:
    """Current location, in priority order:
        1. A location the user set manually (most reliable — honours their intent)
        2. Real GPS on Windows (OS Location Service), if available
        3. IP-based city-level location

    Returns {'lat','lon','city','region','country','label','source'} or None.
    Cached briefly so a burst of calls doesn't repeat the lookup.
    """
    global _LAST_GPS_STATUS
    now = _time.time()

    # 1. A fresh GPS fix from the paired phone is the most accurate source.
    phone = _phone_location()
    if phone:
        return phone

    # 2. A location the user set manually ('I'm in Da Lat').
    manual = _manual_location()
    if manual:
        return manual

    cached = _LOC_CACHE.get("data")
    if cached and (now - _LOC_CACHE.get("t", 0)) < _LOC_TTL:
        if not (gps_required and cached.get("source") != "gps"):
            return cached

    result = None
    if _OS_NAME == "Windows":
        result, _LAST_GPS_STATUS = _windows_gps()
        # Only hard-fail when the user genuinely denied/disabled location.
        # If location is allowed but the device just can't get a fix (no_fix)
        # or the lookup errored, fall back to IP so the feature still works.
        if result is None and gps_required and _LAST_GPS_STATUS == "denied":
            return None            # caller inspects _LAST_GPS_STATUS for the message
    if result is None:
        result = _ip_location()

    if result:
        _LOC_CACHE["data"] = result
        _LOC_CACHE["t"] = now
    return result


def gps_error_message() -> str:
    """A user-facing message tailored to why the last Windows GPS lookup failed."""
    if _LAST_GPS_STATUS == "no_fix":
        return ("Sir, Location Services is on, but the GPS couldn't lock a position "
                "in time. A real GPS fix can take up to 30-40 seconds, especially "
                "indoors or right after startup -- try moving near a window for a "
                "clearer sky view, then ask again in a moment.")
    if _LAST_GPS_STATUS == "error":
        return ("Sir, I couldn't read your GPS location — the Windows location "
                "lookup failed. Make sure Location Services is on and try again.")
    # denied or unknown
    return ("Sir, I couldn't get your GPS location. Please enable Location "
            "Services in Windows Settings (Privacy & security → Location), turn on "
            "'Let apps access your location' and 'Let desktop apps access your "
            "location', then ask again.")


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
    """Report the user's current location (manual → GPS → IP)."""
    loc = current_location()
    if not loc:
        msg = ("Sir, I couldn't determine your location. You can tell me where "
               "you are — for example, say 'I'm in Da Nang'.")
        _log(msg, player)
        return msg
    src = loc.get("source")
    if src in ("manual", "gps", "phone_gps"):
        how = ""                       # precise source; state it plainly
    else:
        how = " (approximate, based on your network)"
    msg = f"You are in {loc['label']}{how}, sir."
    _log(msg, player)
    return msg


def set_my_location(parameters: dict = None, player=None, session_memory=None) -> str:
    """Set the user's current location manually (e.g. 'I'm in Da Lat').

    Pass parameters={'place': '<city/province>'} to set, or an empty place to
    clear it and go back to automatic detection.
    """
    from memory.config_manager import save_manual_location
    params = parameters or {}
    place = (params.get("place") or params.get("city") or params.get("location") or "").strip()

    if not place or place.lower() in ("clear", "reset", "auto", "automatic", "none"):
        save_manual_location("")
        _LOC_CACHE.clear()
        msg = "Sir, I've cleared your set location and will detect it automatically."
        _log(msg, player)
        return msg

    # Verify it resolves to a real place before saving.
    geo = _geocode_vn(place)
    if not geo:
        msg = f"Sir, I couldn't find '{place}'. Please give a Vietnamese city or province."
        _log(msg, player)
        return msg
    save_manual_location(place)
    _LOC_CACHE.clear()
    msg = f"Got it, sir — I'll treat your current location as {geo[2]}."
    _log(msg, player)
    return msg
