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