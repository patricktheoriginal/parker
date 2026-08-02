import json
import sys
from pathlib import Path

def get_base_dir() -> Path:
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent.parent

BASE_DIR    = get_base_dir()
CONFIG_DIR  = BASE_DIR / "config"
CONFIG_FILE = CONFIG_DIR / "api_keys.json"

def ensure_config_dir() -> None:
    CONFIG_DIR.mkdir(parents=True, exist_ok=True)

def config_exists() -> bool:
    return CONFIG_FILE.exists()

def save_api_keys(gemini_api_key: str) -> None:
    ensure_config_dir()

    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}

    data["gemini_api_key"] = gemini_api_key.strip()

    CONFIG_FILE.write_text(
        json.dumps(data, indent=2),
        encoding="utf-8"
    )

def load_api_keys() -> dict:
    if not CONFIG_FILE.exists():
        return {}
    try:
        return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        print(f"❌ Failed to load api_keys.json: {e}")
        return {}

def get_gemini_key() -> str | None:
    return load_api_keys().get("gemini_api_key")

def is_configured() -> bool:
    key = get_gemini_key()
    return bool(key and len(key) > 15)


def get_assistant_name() -> str:
    """Return the configured assistant name, or 'Parker' if not set."""
    return load_api_keys().get("assistant_name", "Parker") or "Parker"


def get_user_name() -> str:
    """Return the configured user name for addressing."""
    return load_api_keys().get("user_name", "")


def save_manual_location(place: str) -> None:
    """Persist a user-set current location (a place name string)."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    place = (place or "").strip()
    if place:
        data["manual_location"] = place
    else:
        data.pop("manual_location", None)
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_manual_location() -> str:
    """Return the user-set current location, or '' if none."""
    return (load_api_keys().get("manual_location") or "").strip()


def get_voice() -> str:
    """Gemini prebuilt voice name (default 'Charon')."""
    return (load_api_keys().get("voice") or "Charon").strip() or "Charon"


def get_persona() -> str:
    """Persona/personality key (default '')."""
    return (load_api_keys().get("persona") or "").strip().lower()


def save_setting(key: str, value: str) -> None:
    """Persist a single config setting."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    if value:
        data[key] = value
    else:
        data.pop(key, None)
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def save_phone_gps(lat: float, lon: float, ts: float) -> None:
    """Persist a GPS fix pushed from the paired phone (lat, lon, unix ts)."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["phone_gps"] = {"lat": float(lat), "lon": float(lon), "ts": float(ts)}
    CONFIG_FILE.write_text(json.dumps(data, indent=2), encoding="utf-8")


def get_phone_gps() -> dict | None:
    """Return the last phone GPS fix {'lat','lon','ts'} or None."""
    g = load_api_keys().get("phone_gps")
    if isinstance(g, dict) and "lat" in g and "lon" in g:
        return g
    return None


def save_assistant_config(assistant_name: str, user_name: str) -> None:
    """Persist assistant name and user name to config."""
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["assistant_name"] = assistant_name.strip() or "Parker"
    data["user_name"] = user_name.strip()
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_brief_enabled() -> bool:
    return load_api_keys().get("morning_brief_enabled", True)


def save_brief_enabled(enabled: bool) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["morning_brief_enabled"] = enabled
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")


def get_trending_schedule() -> dict:
    """Returns {"enabled": bool, "hour": int, "minute": int} for the daily
    automatic trending-news read-aloud. Default: disabled, 07:00."""
    d = load_api_keys().get("trending_news_schedule", {})
    return {
        "enabled": bool(d.get("enabled", False)),
        "hour":    int(d.get("hour", 7)),
        "minute":  int(d.get("minute", 0)),
    }


def save_trending_schedule(enabled: bool, hour: int = 7, minute: int = 0) -> None:
    ensure_config_dir()
    data: dict = {}
    if CONFIG_FILE.exists():
        try:
            data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            data = {}
    data["trending_news_schedule"] = {
        "enabled": enabled,
        "hour": max(0, min(23, hour)),
        "minute": max(0, min(59, minute)),
    }
    CONFIG_FILE.write_text(json.dumps(data, indent=4), encoding="utf-8")