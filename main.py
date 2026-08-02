import platform as _platform
import subprocess as _subprocess

# ── Nuclear: force CREATE_NO_WINDOW on EVERY subprocess call on Windows ───────
# This patches Popen itself, so no per-file flag is needed anywhere.
if _platform.system() == "Windows":
    _OrigPopen = _subprocess.Popen

    class _Popen(_OrigPopen):
        def __init__(self, args, **kw):
            kw["creationflags"] = kw.get("creationflags", 0) | _subprocess.CREATE_NO_WINDOW
            kw.pop("startupinfo", None)   # drop any stale/shared STARTUPINFO
            super().__init__(args, **kw)

    _subprocess.Popen = _Popen
# ─────────────────────────────────────────────────────────────────────────────

import asyncio
import re
import threading
import time
import json
import sys
import traceback
from datetime import datetime
from pathlib import Path

import sounddevice as sd
from google import genai
from google.genai import types
from ui import ParkerUI
from memory.memory_manager import (
    load_memory, update_memory, format_memory_for_prompt,
    save_session_summary, pop_last_session,
)

from actions.file_processor import file_processor
from actions.flight_finder     import flight_finder
from actions.open_app          import open_app
from actions.weather_report    import weather_action, rain_forecast, where_am_i, set_my_location
from actions.maps_route         import route_directions, different_route, analyze_route
from actions.nearby             import find_nearby, place_info
from actions.utilities          import (
    currency_convert, air_quality, wiki_lookup, crypto_price,
    unit_convert, calculate, lunar_date, day_briefing,
)
from actions.market_vn          import gold_price, fuel_price
from actions.vn_news            import vietnam_news
from actions.home_assistant     import home_control, home_list
from actions.spotify            import play_spotify, play_favorites, list_playlists, play_playlist, list_liked_songs, now_playing
from actions.remote_mac         import (
    remote_status, remote_list, remote_find, remote_get, remote_exec,
)
from actions.send_message      import send_message
from actions.make_call         import make_call
from actions.reminder          import reminder
from actions.computer_settings import computer_settings
from actions.screen_processor  import _capture_camera, _capture_screen
from actions.youtube_video     import youtube_video
from actions.desktop           import desktop_control
from actions.browser_control   import browser_control
from actions.file_controller   import file_controller
from actions.code_helper       import code_helper
from actions.dev_agent         import dev_agent
from actions.web_search        import web_search as web_search_action
from actions.trending_news     import trending_news
from actions.computer_control  import computer_control
from actions.game_updater      import game_updater
from actions.system_monitor    import SystemMonitor, get_system_status
from actions.battery_monitor   import BatteryMonitor, battery_status
from actions.proactive         import ProactiveEngine
from actions.background_monitor import (
    add_monitor, remove_monitor, list_monitors, check_all as monitor_check_all,
)
from actions.web_search        import _news as _fetch_news_sync
from memory.config_manager     import (
    get_brief_enabled, get_trending_schedule, save_trending_schedule,
)


def get_base_dir():
    if getattr(sys, "frozen", False):
        return Path(sys.executable).parent
    return Path(__file__).resolve().parent


BASE_DIR        = get_base_dir()
API_CONFIG_PATH = BASE_DIR / "config" / "api_keys.json"
PROMPT_PATH     = BASE_DIR / "core" / "prompt.txt"


def _cfg_value(key: str, default: str) -> str:
    """Read an optional string from config/api_keys.json, else the default.
    Lets you swap models without editing code — handy when Google retires a
    preview model. Run tools/list_models.py to see what your key supports."""
    try:
        with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
            v = (json.load(f).get(key) or "").strip()
            return v or default
    except Exception:
        return default


# Live (voice) model. Google retires preview audio models periodically; if you
# hit a 1007 "audio content type not supported", set "live_model" in
# config/api_keys.json to a current one from tools/list_models.py.
LIVE_MODEL = _cfg_value(
    "live_model", "models/gemini-2.5-flash-native-audio-latest")
# Text model for summaries / tool actions (override with "text_model" in config).
TEXT_MODEL = _cfg_value("text_model", "gemini-flash-latest")
# Offline mode settings (in config/api_keys.json).
# "whisper_model": "tiny"|"base"|"small"|"medium" (default: "small")
WHISPER_MODEL = _cfg_value("whisper_model", "small")
# "force_offline": true → start in offline mode (local Ollama) without trying
# the cloud first. Useful when your Gemini quota is exhausted. Set back to
# false once quota resets. Default: false.
FORCE_OFFLINE = _cfg_value("force_offline", "").lower() in ("1", "true", "yes")
CHANNELS            = 1
SEND_SAMPLE_RATE    = 16000
RECEIVE_SAMPLE_RATE = 24000
CHUNK_SIZE          = 1024

def _get_api_key() -> str:
    with open(API_CONFIG_PATH, "r", encoding="utf-8") as f:
        return json.load(f)["gemini_api_key"]


def _load_system_prompt() -> str:
    try:
        return PROMPT_PATH.read_text(encoding="utf-8")
    except Exception:
        return (
            "You are Parker, a professional AI assistant. "
            "Be concise, direct, and always use the provided tools to complete tasks. "
            "Never simulate or guess results — always call the appropriate tool."
        )

def _has_internet(timeout: float = 2.0) -> bool:
    """Fast active connectivity check — TCP-connect to a couple of well-known
    hosts. Returns True if any succeeds. Used to detect offline quickly instead
    of waiting for a Gemini request to time out."""
    import socket
    for host, port in (("generativelanguage.googleapis.com", 443),
                       ("8.8.8.8", 53), ("1.1.1.1", 53)):
        try:
            with socket.create_connection((host, port), timeout=timeout):
                return True
        except Exception:
            continue
    return False


_CTRL_RE = re.compile(r"<ctrl\d+>", re.IGNORECASE)

def _clean_transcript(text: str) -> str:    
    text = _CTRL_RE.sub("", text)
    text = re.sub(r"[\x00-\x08\x0b-\x1f]", "", text)
    return text.strip()

TOOL_DECLARATIONS = [
    {
        "name": "open_app",
        "description": (
            "Opens any application on the computer. "
            "Use this whenever the user asks to open, launch, or start any app, "
            "website, or program. Always call this tool — never just say you opened it."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "app_name": {
                    "type": "STRING",
                    "description": "Exact name of the application (e.g. 'WhatsApp', 'Chrome', 'Spotify')"
                }
            },
            "required": ["app_name"]
        }
    },
    {
        "name": "web_search",
        "description": (
            "Searches the web. Use for ANY question about current facts, events, prices, "
            "or topics — always prefer this over guessing. "
            "Modes: 'search' (default), 'news' (latest headlines on a topic), "
            "'research' (deep comprehensive answer), 'price' (product cost lookup), "
            "'compare' (side-by-side comparison of items). "
            "For news, default to Vietnam news unless the user names another place."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "Search query or topic. For general news, leave broad — it defaults to Vietnam."},
                "mode":   {"type": "STRING", "description": "search | news | research | price | compare"},
                "items":  {"type": "ARRAY",  "items": {"type": "STRING"}, "description": "Items to compare (compare mode)"},
                "aspect": {"type": "STRING", "description": "Comparison aspect: price | specs | reviews | features"},
            },
            "required": ["query"]
        }
    },
    {
        "name": "system_status",
        "description": (
            "Returns real-time system metrics: CPU usage, RAM, GPU load, CPU temperature, "
            "uptime, and process count. Use when the user asks about computer performance, "
            "temperature, memory, or resource usage."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
        "name": "battery_status",
        "description": (
            "Reports the current battery percentage and whether it's charging. "
            "Use when the user asks about battery level or charging state."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "battery_reminder",
        "description": (
            "Sets or clears voice reminders for battery thresholds. Windows has "
            "no software way to actually stop/start charging on this machine, "
            "so this REMINDS the user by voice instead of controlling the "
            "charger. Use when the user says things like: 'remind me to "
            "unplug at 80%', 'stop charging at 80' (sets a HIGH threshold — "
            "reminds to unplug when charging above it), 'remind me to charge "
            "when below 20%' (sets the LOW threshold — default is already "
            "20%), 'stop reminding me about charging' (clears reminders), "
            "'charge normally' (clears the high threshold). "
            "action: 'set_low', 'set_high', 'clear_low', 'clear_high', or "
            "'status' (report current thresholds). percent: the threshold "
            "number, required for set_low/set_high."
        ),
        "parameters": {"type": "OBJECT", "properties": {
            "action":  {"type": "STRING", "description": "set_low | set_high | clear_low | clear_high | status"},
            "percent": {"type": "INTEGER", "description": "Threshold percentage (0-100), for set_low/set_high."}
        }, "required": ["action"]},
    },
    {
        "name": "weather_report",
        "description": (
            "Gives the weather report to the user. Focused on Vietnam and its "
            "provinces/cities. If the user does not name a place, default to Vietnam."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "City or Vietnamese province name (e.g. Hanoi, Da Nang, Hue). Defaults to Vietnam if omitted."}
            },
            "required": []
        }
    },
    {
        "name": "rain_forecast",
        "description": (
            "Gives a rain forecast for Vietnam and its provinces/cities — chance of "
            "rain and expected rainfall for the next few days. Use this whenever the "
            "user asks about rain, whether it will rain, or the rain outlook. "
            "If no place is named, default to Vietnam."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "city": {"type": "STRING", "description": "Vietnamese city or province (e.g. Hanoi, Da Nang, Hue). Defaults to Vietnam if omitted."},
                "days": {"type": "INTEGER", "description": "How many days ahead (1-7, default 3)."}
            },
            "required": []
        }
    },
    {
        "name": "currency_convert",
        "description": "Converts money between currencies (e.g. USD to VND). Use for exchange rates.",
        "parameters": {"type": "OBJECT", "properties": {
            "amount": {"type": "NUMBER", "description": "Amount to convert (default 1)."},
            "from":   {"type": "STRING", "description": "Source currency code, e.g. USD."},
            "to":     {"type": "STRING", "description": "Target currency code, e.g. VND."}
        }, "required": ["from", "to"]},
    },
    {
        "name": "air_quality",
        "description": "Air quality (AQI, PM2.5) for a Vietnamese city, or the user's location if none given. Use when asked about pollution, air, or AQI.",
        "parameters": {"type": "OBJECT", "properties": {
            "city": {"type": "STRING", "description": "City name; defaults to current location."}
        }, "required": []},
    },
    {
        "name": "wiki_lookup",
        "description": "Looks up a short summary of a topic from Wikipedia. Use for 'what is X', 'who is X', general knowledge.",
        "parameters": {"type": "OBJECT", "properties": {
            "topic": {"type": "STRING", "description": "The topic/person/thing to look up."}
        }, "required": ["topic"]},
    },
    {
        "name": "crypto_price",
        "description": "Current price of a cryptocurrency in USD and VND (Bitcoin, Ethereum, etc.).",
        "parameters": {"type": "OBJECT", "properties": {
            "coin": {"type": "STRING", "description": "Coin name or symbol, e.g. bitcoin, btc, eth."}
        }, "required": ["coin"]},
    },
    {
        "name": "unit_convert",
        "description": "Converts units — length, weight, or temperature (e.g. km to miles, kg to lb, C to F).",
        "parameters": {"type": "OBJECT", "properties": {
            "value": {"type": "NUMBER", "description": "The number to convert."},
            "from":  {"type": "STRING", "description": "Source unit, e.g. km, kg, C."},
            "to":    {"type": "STRING", "description": "Target unit, e.g. miles, lb, F."}
        }, "required": ["value", "from", "to"]},
    },
    {
        "name": "calculate",
        "description": "Evaluates a basic arithmetic expression (+ - * / % and parentheses).",
        "parameters": {"type": "OBJECT", "properties": {
            "expression": {"type": "STRING", "description": "The arithmetic expression, e.g. '(15+27)*3'."}
        }, "required": ["expression"]},
    },
    {
        "name": "lunar_date",
        "description": "Converts a solar date to the Vietnamese lunar calendar date. Defaults to today.",
        "parameters": {"type": "OBJECT", "properties": {
            "date": {"type": "STRING", "description": "Solar date YYYY-MM-DD; defaults to today."}
        }, "required": []},
    },
    {
        "name": "day_briefing",
        "description": "A quick personal day summary combining date, lunar date, weather, air quality, and USD/VND for the user's location. Use for 'brief me', 'how's today', 'daily summary'.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "gold_price",
        "description": "Current SJC gold prices in Vietnam (buy/sell). Use when the user asks about gold price / giá vàng.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "fuel_price",
        "description": "Current Vietnam petrol/diesel prices (Petrolimex). Use for fuel/gas price / giá xăng dầu.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "vietnam_news",
        "description": "Latest hot Vietnamese headlines from VnExpress/Tuoi Tre/Thanh Nien, optionally by topic. Use when the user asks for Vietnam news or hot news.",
        "parameters": {"type": "OBJECT", "properties": {
            "topic": {"type": "STRING", "description": "Optional: latest (default), world, business, sports, tech, entertainment, law, health."}
        }, "required": []},
    },
    {
        "name": "home_control",
        "description": "Controls a smart-home device via Home Assistant — turn lights/switches/fans on or off, toggle, set brightness, or read status. Use when the user asks to turn on/off a device or light.",
        "parameters": {"type": "OBJECT", "properties": {
            "device":     {"type": "STRING", "description": "Device or area name, e.g. 'living room light', 'bedroom fan'."},
            "action":     {"type": "STRING", "description": "on | off | toggle | status."},
            "brightness": {"type": "INTEGER", "description": "Optional brightness 0-100 (lights)."}
        }, "required": ["device", "action"]},
    },
    {
        "name": "home_list",
        "description": "Lists the controllable smart-home devices in Home Assistant.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "play_spotify",
        "description": (
            "Plays a specific song, artist, or playlist on Spotify by searching "
            "for it and starting playback. Use when the user asks to play a named "
            "song/artist (e.g. 'play Shape of You', 'play Sơn Tùng on Spotify'). "
            "For plain pause/resume/next, use computer_settings music controls instead."
        ),
        "parameters": {"type": "OBJECT", "properties": {
            "query": {"type": "STRING", "description": "Song, artist, or playlist to play, e.g. 'Blinding Lights The Weeknd'."}
        }, "required": ["query"]},
    },
    {
        "name": "play_favorites",
        "description": (
            "Plays the user's Liked Songs (their favorites / saved tracks) on "
            "Spotify, shuffled. Use when the user asks to play their favorites, "
            "e.g. 'play my favorite', 'play my favorites', 'play my liked songs', "
            "'play my saved songs'. Takes no arguments."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "list_playlists",
        "description": (
            "Lists the user's Spotify playlists (the ones shown under 'All' when "
            "the app opens), numbered, and reads their names back. Use when the "
            "user asks 'what are my playlists', 'list my playlists', 'show my "
            "playlists', 'read my playlists'. After this, the user can say 'play "
            "the first one' or a playlist name to start it. Takes no arguments. "
            "Read the numbered names aloud to the user."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "play_playlist",
        "description": (
            "Plays one of the user's Spotify playlists, chosen either by NAME or "
            "by POSITION in the list just read out. Use when the user says 'play "
            "the first one', 'play the second one', 'play number 3', 'play the "
            "last one', or 'play my <playlist name> playlist'. Pass what they said "
            "as 'selector' (e.g. 'second', '3', or the playlist name)."
        ),
        "parameters": {"type": "OBJECT", "properties": {
            "selector": {"type": "STRING", "description": "Playlist name, or ordinal/position like 'first', 'second', '3', 'last'."}
        }, "required": ["selector"]},
    },
    {
        "name": "list_liked_songs",
        "description": (
            "Lists the user's Liked Songs (their saved/favorite tracks) by name "
            "and artist, and reads the latest ones back. Use when the user asks "
            "'what are my liked songs', 'list my liked songs', 'what songs do I "
            "like', 'read my favorites'. To actually PLAY them, use "
            "play_favorites instead. Takes no arguments."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "now_playing",
        "description": (
            "Reports the song and artist currently playing (or paused) on "
            "Spotify. Use when the user asks 'what's playing', 'what song is "
            "this', 'who sings this', 'what am I listening to'. For skip/"
            "pause/volume, use computer_settings instead. Takes no arguments."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "trending_news",
        "description": (
            "Shows the latest Vietnamese trending news from 4 major sources "
            "(VnExpress, TuoiTre, ThanhNien, DanTri) in a 4-panel grid layout "
            "on screen, reads AI-generated summaries of the top stories aloud "
            "via TTS, then closes all panels automatically. Use when the user "
            "asks for 'trending news', 'tin tuc noi bat', 'what's happening', "
            "'latest headlines', 'tin moi nhat', 'news update'. Takes no arguments."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "trending_news_schedule",
        "description": (
            "Sets or clears a daily automatic time for trending_news to run "
            "by itself (e.g. every morning at 7 AM). Use when the user says "
            "'read me the news every morning at 7', 'schedule trending news "
            "for 8am', 'stop the daily news', 'turn off scheduled news', or "
            "asks what the current schedule is. "
            "action: 'set', 'clear', or 'status'. hour: 0-23 (required for "
            "set). minute: 0-59 (optional, default 0)."
        ),
        "parameters": {"type": "OBJECT", "properties": {
            "action": {"type": "STRING", "description": "set | clear | status"},
            "hour":   {"type": "INTEGER", "description": "Hour in 24h format, 0-23."},
            "minute": {"type": "INTEGER", "description": "Minute, 0-59 (default 0)."}
        }, "required": ["action"]},
    },
    {
        "name": "microphone_control",
        "description": (
            "Turns Parker's MICROPHONE (command input / listening) on or off. This "
            "does NOT change speaker volume — Parker can still speak while its mic "
            "is off. Use ONLY when the user mentions the microphone specifically: "
            "'mute the microphone', 'deactivate mic', 'stop listening', 'turn the "
            "mic back on', 'activate microphone'. "
            "Do NOT use this for plain 'mute'/'unmute' (that's the speaker → use "
            "computer_settings mute/unmute)."
        ),
        "parameters": {"type": "OBJECT", "properties": {
            "action": {"type": "STRING", "description": "'mute'/'deactivate'/'off' to stop listening, or 'unmute'/'activate'/'on' to listen again."}
        }, "required": ["action"]},
    },
    {
        "name": "remote_status",
        "description": "Checks the connected remote machine (e.g. the user's Mac running the Parker Agent): reachable? OS, CPU, RAM. Use when the user asks about their other computer.",
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "remote_list",
        "description": "Lists files/folders in a directory on the remote machine. Use to browse the other computer. Empty path = its home folder.",
        "parameters": {"type": "OBJECT", "properties": {
            "path": {"type": "STRING", "description": "Folder path on the remote machine; empty for home."}
        }, "required": []},
    },
    {
        "name": "remote_find",
        "description": "Searches for a file by name on the remote machine (e.g. the user forgot a txt/word/excel on their Mac). Returns matching full paths.",
        "parameters": {"type": "OBJECT", "properties": {
            "query": {"type": "STRING", "description": "Part of the filename to search for, e.g. 'budget', 'report.docx'."},
            "root":  {"type": "STRING", "description": "Folder to search under; empty = home."}
        }, "required": ["query"]},
    },
    {
        "name": "remote_get",
        "description": "Fetches a FILE or a whole FOLDER from the remote machine to THIS computer (saved to Downloads/ParkerRemote). Works with any file type — images, zip/rar, pdf, word/excel, etc. A folder is zipped automatically. Use after remote_find to grab a forgotten file. Give the path (relative like 'desktop/report.docx' or a full path).",
        "parameters": {"type": "OBJECT", "properties": {
            "path": {"type": "STRING", "description": "Path of the file OR folder on the remote machine, e.g. 'desktop/abc.html', 'documents', or a full path."}
        }, "required": ["path"]},
    },
    {
        "name": "remote_exec",
        "description": "Runs a shell command on the remote machine and returns the output. Full remote control — only if the agent was started with command execution enabled. Use for advanced tasks on the other computer.",
        "parameters": {"type": "OBJECT", "properties": {
            "cmd": {"type": "STRING", "description": "The shell command to run on the remote machine."}
        }, "required": ["cmd"]},
    },
    {
        "name": "set_persona",
        "description": (
            "Changes Parker's speaking personality/style. Use when the user says "
            "things like 'switch to Rick', 'talk like Rick and Morty', 'be JARVIS', "
            "'talk like a pirate', 'go back to normal'. Personas: rick, jarvis, "
            "pirate, coach, professional, or empty/normal for default. Note: this "
            "changes the STYLE of speech (and reconnects), not the actual voice "
            "timbre — the exact copyrighted Rick voice isn't available."
        ),
        "parameters": {"type": "OBJECT", "properties": {
            "persona": {"type": "STRING", "description": "Persona name: rick | jarvis | pirate | coach | professional | normal."}
        }, "required": ["persona"]},
    },
    {
        "name": "set_voice",
        "description": (
            "Changes Parker's spoken VOICE to one of Gemini's prebuilt voices "
            "(Charon, Puck, Kore, Fenrir, Aoede, Zephyr, Leda, Orus). Use when the "
            "user asks to change the voice. Reconnects to apply."
        ),
        "parameters": {"type": "OBJECT", "properties": {
            "voice": {"type": "STRING", "description": "Voice name: Charon, Puck, Kore, Fenrir, Aoede, Zephyr, Leda, or Orus."}
        }, "required": ["voice"]},
    },
    {
        "name": "where_am_i",
        "description": (
            "Returns the user's current location. Uses a location the user set "
            "manually if available, otherwise GPS (Windows) or an approximate "
            "IP-based location. Use when the user asks where they are or 'where am I'."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "set_my_location",
        "description": (
            "Sets the user's current location manually. Use when the user states "
            "where they are, e.g. 'I'm in Da Nang', 'set my location to Hue', or "
            "'my location is Da Lat'. This is then used as the starting point for "
            "routes and the default place for weather. Pass an empty place to clear "
            "it and return to automatic detection."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "place": {"type": "STRING", "description": "The Vietnamese city or province the user is currently in. Empty to clear."}
            },
            "required": ["place"],
        },
    },
    {
        "name": "route_directions",
        "description": (
            "Plans driving routes between two places in Vietnam and shows a 3D map "
            "inside Parker with alternative routes and per-route analysis (fastest, "
            "shortest, traffic). Returns distance, driving time, depart/arrive time, "
            "and weather along the route. Use whenever the user asks how to get "
            "somewhere, directions, how far/long a drive is, or the best time to leave. "
            "Works with cities, provinces, landmarks, and specific street addresses. "
            "If the user does not give a starting point, leave 'origin' empty."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "destination": {"type": "STRING", "description": "Destination in Vietnam — a city, province, landmark, or a specific street address. Pass the full address exactly as the user said it."},
                "origin":      {"type": "STRING", "description": "Starting place or full address in Vietnam. Leave empty if the user did not specify one — it will use the user's current location automatically."},
                "depart_time": {"type": "STRING", "description": "Desired departure time, e.g. '7am', '15:30', or 'now'. Default is now."}
            },
            "required": ["destination"]
        }
    },
    {
        "name": "different_route",
        "description": (
            "Shows a DIFFERENT/alternative route for the trip just planned, cycling "
            "through the alternatives on the 3D map with analysis for each. Use when "
            "the user says 'different route', 'another way', 'show me alternatives', "
            "or 'is there a faster/shorter way'."
        ),
        "parameters": {"type": "OBJECT", "properties": {}},
    },
    {
        "name": "find_nearby",
        "description": (
            "Finds nearby places (like Google Maps) — cafes, restaurants, ATMs, gas "
            "stations, hotels, pharmacies, hospitals, supermarkets, etc. — around the "
            "user's current location, or around a named place. Shows them on the map "
            "with distances. Use when the user asks 'find X near me' or 'X nearby'."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "query":  {"type": "STRING", "description": "What to look for, e.g. 'cafe', 'ATM', 'gas station', 'restaurant'."},
                "near":   {"type": "STRING", "description": "Optional place to search around; defaults to the user's current location."},
                "radius": {"type": "INTEGER", "description": "Search radius in meters (200–5000, default 1500)."}
            },
            "required": ["query"]
        },
    },
    {
        "name": "place_info",
        "description": (
            "Gives information about a specific place — its address, type, and opening "
            "hours/phone/website if known (from map data). Use when the user asks about "
            "a particular place, its hours, or where it is. Coverage can be limited."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "place": {"type": "STRING", "description": "The place name or address."}
            },
            "required": ["place"]
        },
    },
    {
        "name": "analyze_route",
        "description": (
            "Gives an in-depth analysis of the current route: the road-type "
            "breakdown (expressway/cao tốc, national highway/quốc lộ, city roads), "
            "when it's congested (Vietnam rush hours), the estimated time now, and "
            "the worst-case time in heavy traffic. Use when the user asks about "
            "traffic, when it's jammed, whether the route is highway or expressway, "
            "or the longest/worst-case travel time. Requires a route to be planned first."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "depart_time": {"type": "STRING", "description": "Optional time to analyze for, e.g. '8am', '18:00'. Defaults to now."}
            },
            "required": []
        },
    },
    {
        "name": "send_message",
        "description": "Sends a text message via Zalo, Telegram, or another messaging app. Defaults to Zalo if no platform is given.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver":     {"type": "STRING", "description": "Recipient contact name"},
                "message_text": {"type": "STRING", "description": "The message to send"},
                "platform":     {"type": "STRING", "description": "Platform: Zalo (default), Telegram, Instagram, Signal, Discord, Messenger."}
            },
            "required": ["receiver", "message_text"]
        }
    },
    {
        "name": "make_call",
        "description": (
            "Dials a phone NUMBER via the default phone app (on Windows, Phone "
            "Link with the paired phone). Use when the user asks to call a phone "
            "number. Requires an actual phone number — app calls (e.g. Zalo) "
            "can't be started programmatically."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "receiver": {"type": "STRING", "description": "The phone number to dial."}
            },
            "required": ["receiver"]
        }
    },
    {
        "name": "reminder",
        "description": (
            "Manages timed reminders (OS notifications). "
            "action='set' (default) schedules a reminder — needs date, time, message, "
            "and optionally repeat='daily' or 'weekly'. "
            "action='list' lists current reminders. "
            "action='delete' cancels reminders matching the message text (or all if empty)."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":  {"type": "STRING", "description": "set (default) | list | delete"},
                "date":    {"type": "STRING", "description": "Date in YYYY-MM-DD format (for set)"},
                "time":    {"type": "STRING", "description": "Time in HH:MM format 24h (for set)"},
                "message": {"type": "STRING", "description": "Reminder text (for set); or text to match when deleting"},
                "repeat":  {"type": "STRING", "description": "Optional: 'daily' or 'weekly' for a repeating reminder"}
            },
            "required": []
        }
    },
    {
        "name": "youtube_video",
        "description": (
            "Controls YouTube. Use for: playing videos, summarizing a video's content, "
            "getting video info, or showing trending videos."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "play | summarize | get_info | trending (default: play)"},
                "query":  {"type": "STRING", "description": "Search query for play action"},
                "save":   {"type": "BOOLEAN", "description": "Save summary to Notepad (summarize only)"},
                "region": {"type": "STRING", "description": "Country code for trending e.g. TR, US"},
                "url":    {"type": "STRING", "description": "Video URL for get_info action"},
            },
            "required": []
        }
    },
    {
        "name": "screen_process",
        "description": (
            "Captures the screen or webcam image and lets you analyze it. "
            "MUST be called when user asks what is on screen, what you see, "
            "look at camera, analyze my screen, etc. "
            "You have NO visual ability without this tool. "
            "After the image is captured it is sent directly to you — describe what you see and answer the user's question. "
            "When using camera: the live view stays open until user says close it or calls close_camera."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "angle": {"type": "STRING", "description": "'screen' to capture display, 'camera' for webcam. Default: 'screen'"},
                "text":  {"type": "STRING", "description": "The question or instruction about the captured image"}
            },
            "required": ["text"]
        }
    },
    {
        "name": "close_camera",
        "description": (
            "Closes the live camera view shown on screen. "
            "Call when user says: close camera, stop camera, turn off camera, "
            "kamerayı kapat, kapat, creepy, etc."
        ),
        "parameters": {"type": "OBJECT", "properties": {}, "required": []}
    },
    {
        "name": "computer_settings",
        "description": (
            "Controls the computer: volume, brightness, window management, keyboard shortcuts, "
            "typing text on screen, closing apps, fullscreen, dark mode, WiFi, restart, shutdown, "
            "scrolling, tab management, zoom, screenshots, lock screen, refresh/reload page. "
            "Use for ANY single computer control command."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "The action to perform"},
                "description": {"type": "STRING", "description": "Natural language description of what to do"},
                "value":       {"type": "STRING", "description": "Optional value: volume level, text to type, etc."}
            },
            "required": []
        }
    },
    {
        "name": "browser_control",
        "description": (
            "Controls any web browser. Use for: opening websites, searching the web, "
            "clicking elements, filling forms, scrolling, screenshots, navigation, any web-based task. "
            "Simple open/search requests launch the user's own browser normally (their real profile "
            "and logged-in accounts); interactive actions (click, type, fill_form...) attach an "
            "automation browser. "
            "Always pass the 'browser' parameter when the user specifies a browser (e.g. 'open in Edge', "
            "'use Firefox', 'open Chrome'). Multiple browsers can run simultaneously."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "go_to | search | click | type | scroll | fill_form | smart_click | smart_type | get_text | get_url | press | new_tab | close_tab | screenshot | back | forward | reload | switch | list_browsers | close | close_all"},
                "browser":     {"type": "STRING", "description": "Target browser: chrome | edge | firefox | opera | operagx | brave | vivaldi | safari. Omit to use the currently active browser."},
                "url":         {"type": "STRING", "description": "URL for go_to / new_tab action"},
                "query":       {"type": "STRING", "description": "Search query for search action"},
                "engine":      {"type": "STRING", "description": "Search engine: google | bing | duckduckgo | yandex (default: google)"},
                "selector":    {"type": "STRING", "description": "CSS selector for click/type"},
                "text":        {"type": "STRING", "description": "Text to click or type"},
                "description": {"type": "STRING", "description": "Element description for smart_click/smart_type"},
                "direction":   {"type": "STRING", "description": "up | down for scroll"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount in pixels (default: 500)"},
                "key":         {"type": "STRING", "description": "Key name for press action (e.g. Enter, Escape, F5)"},
                "path":        {"type": "STRING", "description": "Save path for screenshot"},
                "incognito":   {"type": "BOOLEAN", "description": "Open in private/incognito mode"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "file_controller",
        "description": "Manages files and folders: list, create, delete, move, copy, rename, read, write, find, disk usage.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "list | create_file | create_folder | delete | move | copy | rename | read | write | find | largest | disk_usage | organize_desktop | info"},
                "path":        {"type": "STRING", "description": "File/folder path or shortcut: desktop, downloads, documents, home"},
                "destination": {"type": "STRING", "description": "Destination path for move/copy"},
                "new_name":    {"type": "STRING", "description": "New name for rename"},
                "content":     {"type": "STRING", "description": "Content for create_file/write"},
                "name":        {"type": "STRING", "description": "File name to search for"},
                "extension":   {"type": "STRING", "description": "File extension to search (e.g. .pdf)"},
                "count":       {"type": "INTEGER", "description": "Number of results for largest"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "desktop_control",
        "description": "Controls the desktop: wallpaper, organize, clean, list, stats.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {"type": "STRING", "description": "wallpaper | wallpaper_url | organize | clean | list | stats | task"},
                "path":   {"type": "STRING", "description": "Image path for wallpaper"},
                "url":    {"type": "STRING", "description": "Image URL for wallpaper_url"},
                "mode":   {"type": "STRING", "description": "by_type or by_date for organize"},
                "task":   {"type": "STRING", "description": "Natural language desktop task"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "code_helper",
        "description": "Writes, edits, explains, runs, or builds code files.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "write | edit | explain | run | build | auto (default: auto)"},
                "description": {"type": "STRING", "description": "What the code should do or what change to make"},
                "language":    {"type": "STRING", "description": "Programming language (default: python)"},
                "output_path": {"type": "STRING", "description": "Where to save the file"},
                "file_path":   {"type": "STRING", "description": "Path to existing file for edit/explain/run/build"},
                "code":        {"type": "STRING", "description": "Raw code string for explain"},
                "args":        {"type": "STRING", "description": "CLI arguments for run/build"},
                "timeout":     {"type": "INTEGER", "description": "Execution timeout in seconds (default: 30)"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "dev_agent",
        "description": "Builds complete multi-file projects from scratch: plans, writes files, installs deps, opens VSCode, runs and fixes errors.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "description":  {"type": "STRING", "description": "What the project should do"},
                "language":     {"type": "STRING", "description": "Programming language (default: python)"},
                "project_name": {"type": "STRING", "description": "Optional project folder name"},
                "timeout":      {"type": "INTEGER", "description": "Run timeout in seconds (default: 30)"},
            },
            "required": ["description"]
        }
    },
    {
        "name": "computer_control",
        "description": "Direct computer control: type, click, hotkeys, scroll, move mouse, screenshots, find elements on screen.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":      {"type": "STRING", "description": "type | smart_type | click | double_click | right_click | hotkey | press | scroll | move | copy | paste | screenshot | wait | clear_field | focus_window | screen_find | screen_click | random_data | user_data"},
                "text":        {"type": "STRING", "description": "Text to type or paste"},
                "x":           {"type": "INTEGER", "description": "X coordinate"},
                "y":           {"type": "INTEGER", "description": "Y coordinate"},
                "keys":        {"type": "STRING", "description": "Key combination e.g. 'ctrl+c'"},
                "key":         {"type": "STRING", "description": "Single key e.g. 'enter'"},
                "direction":   {"type": "STRING", "description": "up | down | left | right"},
                "amount":      {"type": "INTEGER", "description": "Scroll amount (default: 3)"},
                "seconds":     {"type": "NUMBER",  "description": "Seconds to wait"},
                "title":       {"type": "STRING",  "description": "Window title for focus_window"},
                "description": {"type": "STRING",  "description": "Element description for screen_find/screen_click"},
                "type":        {"type": "STRING",  "description": "Data type for random_data"},
                "field":       {"type": "STRING",  "description": "Field for user_data: name|email|city"},
                "clear_first": {"type": "BOOLEAN", "description": "Clear field before typing (default: true)"},
                "path":        {"type": "STRING",  "description": "Save path for screenshot"},
            },
            "required": ["action"]
        }
    },
    {
        "name": "game_updater",
        "description": (
            "THE ONLY tool for ANY Steam or Epic Games request. "
            "Use for: installing, downloading, updating games, listing installed games, "
            "checking download status, scheduling updates. "
            "ALWAYS call directly for any Steam/Epic/game request. "
            "NEVER use browser_control or web_search for Steam/Epic."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action":    {"type": "STRING",  "description": "update | install | list | download_status | schedule | cancel_schedule | schedule_status (default: update)"},
                "platform":  {"type": "STRING",  "description": "steam | epic | both (default: both)"},
                "game_name": {"type": "STRING",  "description": "Game name (partial match supported)"},
                "app_id":    {"type": "STRING",  "description": "Steam AppID for install (optional)"},
                "hour":      {"type": "INTEGER", "description": "Hour for scheduled update 0-23 (default: 3)"},
                "minute":    {"type": "INTEGER", "description": "Minute for scheduled update 0-59 (default: 0)"},
                "shutdown_when_done": {"type": "BOOLEAN", "description": "Shut down PC when download finishes"},
            },
            "required": []
        }
    },
    {
        "name": "flight_finder",
        "description": "Searches Google Flights and speaks the best options.",
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "origin":      {"type": "STRING",  "description": "Departure city or airport code"},
                "destination": {"type": "STRING",  "description": "Arrival city or airport code"},
                "date":        {"type": "STRING",  "description": "Departure date (any format)"},
                "return_date": {"type": "STRING",  "description": "Return date for round trips"},
                "passengers":  {"type": "INTEGER", "description": "Number of passengers (default: 1)"},
                "cabin":       {"type": "STRING",  "description": "economy | premium | business | first"},
                "save":        {"type": "BOOLEAN", "description": "Save results to Notepad"},
            },
            "required": ["origin", "destination", "date"]
        }
    },
    {
        "name": "manage_monitor",
        "description": (
            "Add, remove, or list background monitoring topics. "
            "Parker checks these topics once a day and alerts the user when there is a new development. "
            "Use 'add' when the user says 'monitor X', 'track X', 'follow X'. "
            "Use 'remove' when the user says 'stop monitoring X'. "
            "Use 'list' when the user asks what is being monitored. "
            "Do NOT add crypto, financial, or trading topics."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "action": {
                    "type":        "STRING",
                    "description": "add | remove | list",
                },
                "topic": {
                    "type":        "STRING",
                    "description": "Topic to monitor or stop monitoring (e.g. 'space exploration', 'AI news')",
                },
            },
            "required": ["action"],
        },
    },
    {
        "name": "shutdown_parker",
        "description": (
            "Shuts down the assistant completely. "
            "Call this when the user expresses intent to end the conversation, "
            "close the assistant, say goodbye, or stop Parker. "
            "The user can say this in ANY language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {},
        }
    },
    {
    "name": "file_processor",
    "description": (
        "Processes any file that the user has uploaded or dropped onto the interface. "
        "Use this when the user refers to an uploaded file and wants an action on it. "
        "Supports: images (describe/ocr/resize/compress/convert), "
        "PDFs (summarize/extract_text/to_word), "
        "Word docs & text files (summarize/fix/reformat/translate), "
        "CSV/Excel (analyze/stats/filter/sort/convert), "
        "JSON/XML (validate/format/analyze), "
        "code files (explain/review/fix/optimize/run/document/test), "
        "audio (transcribe/trim/convert/info), "
        "video (trim/extract_audio/extract_frame/compress/transcribe/info), "
        "archives (list/extract), "
        "presentations (summarize/extract_text). "
        "ALWAYS call this tool when a file has been uploaded and the user gives a command about it. "
        "If the user's command is ambiguous, pick the most logical action for that file type."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "file_path": {
                "type": "STRING",
                "description": "Full path to the uploaded file. Leave empty to use the currently uploaded file."
            },
            "action": {
                "type": "STRING",
                "description": (
                    "What to do with the file. Examples by type:\n"
                    "image: describe | ocr | resize | compress | convert | info\n"
                    "pdf: summarize | extract_text | to_word | info\n"
                    "docx/txt: summarize | fix | reformat | translate_hint | word_count | to_bullet\n"
                    "csv/excel: analyze | stats | filter | sort | convert | info\n"
                    "json: validate | format | analyze | to_csv\n"
                    "code: explain | review | fix | optimize | run | document | test\n"
                    "audio: transcribe | trim | convert | info\n"
                    "video: trim | extract_audio | extract_frame | compress | transcribe | info | convert\n"
                    "archive: list | extract\n"
                    "pptx: summarize | extract_text | analyze"
                )
            },
            "instruction": {
                "type": "STRING",
                "description": "Free-form instruction if action doesn't cover it. E.g. 'summarize this', 'find all email addresses'"
            },
            "format": {
                "type": "STRING",
                "description": "Target format for conversion. E.g. 'mp3', 'pdf', 'csv', 'png'"
            },
            "width":     {"type": "INTEGER", "description": "Target width for image resize"},
            "height":    {"type": "INTEGER", "description": "Target height for image resize"},
            "scale":     {"type": "NUMBER",  "description": "Scale factor for image resize (e.g. 0.5)"},
            "quality":   {"type": "INTEGER", "description": "Quality 1-100 for image/video compress"},
            "start":     {"type": "STRING",  "description": "Start time for trim: seconds or HH:MM:SS"},
            "end":       {"type": "STRING",  "description": "End time for trim: seconds or HH:MM:SS"},
            "timestamp": {"type": "STRING",  "description": "Timestamp for video frame extraction HH:MM:SS"},
            "column":    {"type": "STRING",  "description": "Column name for CSV filter/sort"},
            "value":     {"type": "STRING",  "description": "Filter value for CSV filter"},
            "condition": {"type": "STRING",  "description": "Filter condition: equals|contains|gt|lt"},
            "ascending": {"type": "BOOLEAN", "description": "Sort order for CSV sort (default: true)"},
            "save":      {"type": "BOOLEAN", "description": "Save result to file (default: true)"},
            "destination": {"type": "STRING", "description": "Output folder for archive extract"},
        },
        "required": []
    }
},
    {
        "name": "save_memory",
        "description": (
            "Save an important personal fact about the user to long-term memory. "
            "Call this silently whenever the user reveals something worth remembering: "
            "name, age, city, job, preferences, hobbies, relationships, projects, or future plans. "
            "Do NOT call for: weather, reminders, searches, or one-time commands. "
            "Do NOT announce that you are saving — just call it silently. "
            "Values must be in English regardless of the conversation language."
        ),
        "parameters": {
            "type": "OBJECT",
            "properties": {
                "category": {
                    "type": "STRING",
                    "description": (
                        "identity — name, age, birthday, city, job, language, nationality | "
                        "preferences — favorite food/color/music/film/game/sport, hobbies | "
                        "projects — active projects, goals, things being built | "
                        "relationships — friends, family, partner, colleagues | "
                        "wishes — future plans, things to buy, travel dreams | "
                        "notes — habits, schedule, anything else worth remembering"
                    )
                },
                "key":   {"type": "STRING", "description": "Short snake_case key (e.g. name, favorite_food, sister_name)"},
                "value": {"type": "STRING", "description": "Concise value in English (e.g. Fatih, pizza, older sister)"},
            },
            "required": ["category", "key", "value"]
        }
    },
]

# --- Plugin system ---


class ParkerLive:

    def __init__(self, ui: ParkerUI):
        self.ui             = ui
        self._asst_name     = "Parker"   # updated each session from config
        self._offline_history: list = []   # conversation memory for offline mode
        self._pending_reconnect = False    # reconnect to apply a new voice/persona
        self._reconnecting = False         # True during an intentional reconnect
        self._offline_voice = None         # OfflineVoice loop, active only offline
        self._announced_offline = False    # spoke the "offline mode" notice once
        self._was_online = False           # True once a cloud session has connected
        self.session              = None
        self.audio_in_queue       = None
        self.out_queue            = None
        self._loop                = None
        self._is_speaking         = False
        self._speaking_lock       = threading.Lock()
        self._phone_active        = False   # True while phone mic is streaming; pauses PC mic
        self._pending_vision       = None    # (img_bytes, mime_type, question, angle) to inject after tool response
        self._vision_cam_active    = False   # True if camera was opened for vision → auto-close after response
        self._vision_close_pending = False   # True after vision injected; next turn_complete closes camera
        self._vision_last_time     = 0.0     # monotonic time of last screen_process call (cooldown guard)
        self._vision_busy          = False   # True while a vision capture/inject cycle is in flight
        self._interrupted          = False   # True while draining audio after user interrupt
        self.ui.on_text_command   = self._on_text_command
        self.ui.on_remote_clicked = self._make_remote_key
        self.ui.on_interrupt      = self.interrupt
        self._turn_done_event: asyncio.Event | None = None
        self._dashboard     = None
        self._briefing_sent    = False          # morning briefing fires once per process
        self._trending_last_run_date = None     # date() of last auto trending-news run
        self._sys_monitor      = SystemMonitor()  # persistent cooldown state
        self._battery_monitor  = BatteryMonitor()  # voice reminders (can't control charging in software)
        self._proactive        = ProactiveEngine()
        self._last_user_speech = time.monotonic()  # updated on every user utterance
        self._session_log: list[str] = []          # conversation turns for end-of-session summary

    def _make_remote_key(self):
        """Called from Qt main thread when user presses Remote Control."""
        if self._dashboard is None:
            self.ui.write_log(
                "SYS: Dashboard unavailable. "
                "Run: pip install fastapi \"uvicorn[standard]\" cryptography"
            )
            return None
        key    = self._dashboard.new_key()
        url    = self._dashboard.get_url()
        manual = self._dashboard.get_manual_url()
        return url, key, f"{url}/auto-login?key={key}", manual

    def _on_text_command(self, text: str):
        # Online: send to the Gemini Live session as usual.
        if self._loop and self.session:
            asyncio.run_coroutine_threadsafe(
                self.session.send_client_content(
                    turns={"parts": [{"text": text}]},
                    turn_complete=True
                ),
                self._loop
            )
            return
        # Offline (no Gemini session): fall back to the local Ollama agent.
        self._handle_offline_text(text)

    def _start_offline_voice(self) -> None:
        """Start the offline voice loop (mic→Whisper→Ollama→TTS) if possible."""
        if self._offline_voice is not None and self._offline_voice.is_running():
            return
        try:
            from core.offline_agent import offline_available, offline_respond
            from core.offline_voice import OfflineVoice
        except Exception as e:
            print(f"[Offline] voice not available: {e}")
            return
        if not offline_available():
            return

        def _respond(text, history):
            reply = offline_respond(text, history=history)
            # keep the shared text history in sync so both paths remember
            return reply

        self._offline_voice = OfflineVoice(
            respond_fn=_respond,
            on_state=lambda s: self.ui.set_state(s),
            on_log=lambda m: self.ui.write_log(m if ":" in m else f"SYS: {m}"),
            whisper_model=WHISPER_MODEL,
        )
        self._offline_voice.start()
        self.ui.write_log("SYS: OFFLINE VOICE active — speak to Parker (local model).")

    def _stop_offline_voice(self) -> None:
        if self._offline_voice is not None:
            try:
                self._offline_voice.stop()
            except Exception:
                pass
            self._offline_voice = None

    def _handle_offline_text(self, text: str) -> None:
        """Answer a typed command with the local offline model, in a thread."""
        def _work():
            try:
                from core.offline_agent import offline_respond, offline_available
                if not offline_available():
                    self.ui.write_log(
                        "SYS: Offline mode unavailable — start Ollama (ollama serve) "
                        "and pull a model like llama3.2:3b.")
                    return
                self.ui.set_state("THINKING")
                self.ui.write_log("SYS: Offline (local model) — thinking…")
                reply = offline_respond(
                    text, history=self._offline_history,
                    log=lambda m: None,
                )
                self._offline_history.append({"role": "user", "content": text})
                self._offline_history.append({"role": "assistant", "content": reply})
                self._offline_history = self._offline_history[-12:]
                self.ui.write_log(f"{self._asst_name}: {reply}")
            except Exception as e:
                self.ui.write_log(f"ERR: Offline agent failed — {e}")
            finally:
                if not self.ui.muted:
                    self.ui.set_state("LISTENING")
        threading.Thread(target=_work, daemon=True).start()

    def set_speaking(self, value: bool):
        with self._speaking_lock:
            self._is_speaking = value
        if value:
            self.ui.set_state("SPEAKING")
        elif not self.ui.muted:
            self.ui.set_state("LISTENING")

    def interrupt(self) -> None:
        """Stop Parker mid-speech: drain queued audio and open mic immediately."""
        self._interrupted = True
        q = self.audio_in_queue
        if q:
            drained = 0
            while True:
                try:
                    q.get_nowait()
                    drained += 1
                except Exception:
                    break
            if drained:
                print(f"[PARKER] ✋ Interrupted — {drained} audio chunks discarded")
        self.set_speaking(False)
        if self._turn_done_event:
            self._turn_done_event.clear()
        self.ui.write_log("SYS: Interrupted — listening...")

    def speak(self, text: str):
        if not self._loop or not self.session:
            return
        asyncio.run_coroutine_threadsafe(
            self.session.send_client_content(
                turns={"parts": [{"text": text}]},
                turn_complete=True
            ),
            self._loop
        )

    def speak_error(self, tool_name: str, error: str):
        short = str(error)[:120]
        self.ui.write_log(f"ERR: {tool_name} — {short}")
        self.speak(f"Sir, {tool_name} encountered an error. {short}")

    def _build_config(self) -> types.LiveConnectConfig:
        from datetime import datetime

        # Load customization from config
        try:
            _cfg = json.loads(open(API_CONFIG_PATH, encoding="utf-8").read())
            self._asst_name = (_cfg.get("assistant_name") or "Parker").strip()
            _user_name = (_cfg.get("user_name") or "").strip()
        except Exception:
            self._asst_name = "Parker"
            _user_name = ""

        memory     = load_memory()
        mem_str    = format_memory_for_prompt(memory)
        sys_prompt = _load_system_prompt()

        now      = datetime.now()
        time_str = now.strftime("%A, %B %d, %Y — %I:%M %p")
        time_ctx = (
            f"[CURRENT DATE & TIME]\n"
            f"Right now it is: {time_str}\n"
            f"Use this to calculate exact times for reminders.\n\n"
        )

        # Identity injection — overrides any hardcoded name in prompt.txt
        _addr = (f"ADDRESS: Always call the user '{_user_name}'."
                 if _user_name
                 else "ADDRESS: Address the user as \"sir\".")
        identity_ctx = (
            f"[IDENTITY]\n"
            f"Your name is {self._asst_name}. "
            f"Always refer to yourself as {self._asst_name}.\n"
            f"LANGUAGE: ALWAYS respond in English only, regardless of the language "
            f"the user speaks. Never switch or mix languages.\n"
            f"{_addr}\n\n"
        )

        parts = [time_ctx, identity_ctx]
        if mem_str:
            parts.append(mem_str)

        # Persona (speaking style) and Gemini voice, from config.
        try:
            from memory.config_manager import get_voice, get_persona
            from actions.personas import persona_snippet
            voice_name = get_voice()
            persona = persona_snippet(get_persona())
            if persona:
                parts.append("[PERSONA]\n" + persona + "\n")
        except Exception:
            voice_name = "Charon"

        parts.append(sys_prompt)

        # Voice-activity detection tuned to catch soft/short speech and not cut
        # the user off. START_SENSITIVITY_HIGH triggers on quieter onsets;
        # a longer end-of-speech padding avoids clipping trailing words.
        try:
            _vad = types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(
                    start_of_speech_sensitivity=types.StartSensitivity.START_SENSITIVITY_HIGH,
                    end_of_speech_sensitivity=types.EndSensitivity.END_SENSITIVITY_LOW,
                    prefix_padding_ms=200,
                    silence_duration_ms=800,
                )
            )
        except Exception:
            _vad = None

        _cfg_kwargs = dict(
            response_modalities=["AUDIO"],
            output_audio_transcription={},
            input_audio_transcription={},
            system_instruction="\n".join(parts),
            tools=[{"function_declarations": TOOL_DECLARATIONS}],
            session_resumption=types.SessionResumptionConfig(),
            speech_config=types.SpeechConfig(
                voice_config=types.VoiceConfig(
                    prebuilt_voice_config=types.PrebuiltVoiceConfig(
                        voice_name=voice_name
                    )
                )
            ),
        )
        if _vad is not None:
            _cfg_kwargs["realtime_input_config"] = _vad
        return types.LiveConnectConfig(**_cfg_kwargs)

    async def _execute_tool(self, fc) -> types.FunctionResponse:
        name = fc.name
        args = dict(fc.args or {})

        print(f"[PARKER] 🔧 {name}  {args}")
        self.ui.set_state("THINKING")

        if name == "save_memory":
            category = args.get("category", "notes")
            key      = args.get("key", "")
            value    = args.get("value", "")
            if key and value:
                update_memory({category: {key: {"value": value}}})
                print(f"[Memory] 💾 save_memory: {category}/{key} = {value}")
            if not self.ui.muted:
                self.ui.set_state("LISTENING")
            return types.FunctionResponse(
                id=fc.id, name=name,
                response={"result": "ok", "silent": True}
            )

        loop   = asyncio.get_event_loop()
        result = "Done."

        try:
            if name == "open_app":
                r = await loop.run_in_executor(None, lambda: open_app(parameters=args, response=None, player=self.ui))
                result = r or f"Opened {args.get('app_name')}."

            elif name == "weather_report":
                r = await loop.run_in_executor(None, lambda: weather_action(parameters=args, player=self.ui))
                result = r or "Weather delivered."

            elif name == "rain_forecast":
                r = await loop.run_in_executor(None, lambda: rain_forecast(parameters=args, player=self.ui))
                result = r or "Rain forecast delivered."

            elif name == "currency_convert":
                result = await loop.run_in_executor(None, lambda: currency_convert(parameters=args, player=self.ui))

            elif name == "air_quality":
                result = await loop.run_in_executor(None, lambda: air_quality(parameters=args, player=self.ui))

            elif name == "wiki_lookup":
                result = await loop.run_in_executor(None, lambda: wiki_lookup(parameters=args, player=self.ui))

            elif name == "crypto_price":
                result = await loop.run_in_executor(None, lambda: crypto_price(parameters=args, player=self.ui))

            elif name == "unit_convert":
                result = await loop.run_in_executor(None, lambda: unit_convert(parameters=args, player=self.ui))

            elif name == "calculate":
                result = await loop.run_in_executor(None, lambda: calculate(parameters=args, player=self.ui))

            elif name == "lunar_date":
                result = await loop.run_in_executor(None, lambda: lunar_date(parameters=args, player=self.ui))

            elif name == "day_briefing":
                result = await loop.run_in_executor(None, lambda: day_briefing(parameters=args, player=self.ui))

            elif name == "gold_price":
                result = await loop.run_in_executor(None, lambda: gold_price(parameters=args, player=self.ui))

            elif name == "fuel_price":
                result = await loop.run_in_executor(None, lambda: fuel_price(parameters=args, player=self.ui))

            elif name == "vietnam_news":
                result = await loop.run_in_executor(None, lambda: vietnam_news(parameters=args, player=self.ui))

            elif name == "home_control":
                result = await loop.run_in_executor(None, lambda: home_control(parameters=args, player=self.ui))

            elif name == "home_list":
                result = await loop.run_in_executor(None, lambda: home_list(parameters=args, player=self.ui))

            elif name == "play_spotify":
                result = await loop.run_in_executor(None, lambda: play_spotify(parameters=args, player=self.ui))

            elif name == "play_favorites":
                result = await loop.run_in_executor(None, lambda: play_favorites(parameters=args, player=self.ui))

            elif name == "list_playlists":
                result = await loop.run_in_executor(None, lambda: list_playlists(parameters=args, player=self.ui))

            elif name == "play_playlist":
                result = await loop.run_in_executor(None, lambda: play_playlist(parameters=args, player=self.ui))

            elif name == "list_liked_songs":
                result = await loop.run_in_executor(None, lambda: list_liked_songs(parameters=args, player=self.ui))

            elif name == "now_playing":
                result = await loop.run_in_executor(None, lambda: now_playing(parameters=args, player=self.ui))

            elif name == "trending_news":
                # Hard timeout — fetch/summarize/TTS all touch the network with
                # no reliable upper bound of their own, and run_in_executor()
                # has no timeout, so a slow/blocked call would leave Parker
                # "thinking" forever. Bail out and report it instead.
                try:
                    result = await asyncio.wait_for(
                        loop.run_in_executor(None, lambda: trending_news(parameters=args, player=self.ui)),
                        timeout=90,
                    )
                except asyncio.TimeoutError:
                    result = ("Sir, the trending news feature is taking too long "
                              "(network or TTS issue) — I stopped waiting. Try again.")
                    self.ui.write_log("ERR: trending_news timed out after 90s.")

            elif name == "trending_news_schedule":
                act = (args.get("action") or "").lower().strip()
                if act == "set":
                    hour = args.get("hour")
                    minute = args.get("minute", 0)
                    if hour is None:
                        result = "Sir, what hour should I run it at (0-23)?"
                    else:
                        save_trending_schedule(True, int(hour), int(minute or 0))
                        result = (f"I'll read the trending news automatically every "
                                  f"day at {int(hour):02d}:{int(minute or 0):02d}.")
                elif act == "clear":
                    sched = get_trending_schedule()
                    save_trending_schedule(False, sched["hour"], sched["minute"])
                    result = "Scheduled trending news turned off."
                elif act == "status":
                    sched = get_trending_schedule()
                    if sched["enabled"]:
                        result = (f"Trending news is scheduled daily at "
                                  f"{sched['hour']:02d}:{sched['minute']:02d}.")
                    else:
                        result = "No trending news schedule is set."
                else:
                    result = "Sir, say 'set', 'clear', or 'status' for the news schedule."

            elif name == "remote_status":
                result = await loop.run_in_executor(None, lambda: remote_status(parameters=args, player=self.ui))
            elif name == "remote_list":
                result = await loop.run_in_executor(None, lambda: remote_list(parameters=args, player=self.ui))
            elif name == "remote_find":
                result = await loop.run_in_executor(None, lambda: remote_find(parameters=args, player=self.ui))
            elif name == "remote_get":
                result = await loop.run_in_executor(None, lambda: remote_get(parameters=args, player=self.ui))
            elif name == "remote_exec":
                result = await loop.run_in_executor(None, lambda: remote_exec(parameters=args, player=self.ui))

            elif name == "microphone_control":
                act = (args.get("action") or "").lower().strip()
                mute = act in ("mute", "deactivate", "off", "stop", "disable")
                self.ui.set_mic_muted(mute)
                result = ("Microphone deactivated — I've stopped listening, but I "
                          "can still speak." if mute else
                          "Microphone active — I'm listening again.")

            elif name == "set_persona":
                from actions.personas import resolve_persona, PERSONAS
                from memory.config_manager import save_setting
                key = resolve_persona(args.get("persona", ""))
                if key is None:
                    result = "Sir, I don't have that persona. Try Rick, JARVIS, pirate, coach, or normal."
                else:
                    save_setting("persona", key)
                    disp = PERSONAS.get(key, PERSONAS[""])[0]
                    self.ui.write_log(f"SYS: Persona → {disp}")
                    result = f"Switching to the {disp} persona now, sir."
                    self._pending_reconnect = True   # apply on reconnect

            elif name == "set_voice":
                from actions.personas import resolve_voice
                from memory.config_manager import save_setting
                v = resolve_voice(args.get("voice", ""))
                if v is None:
                    result = "Sir, that voice isn't available. Options: Charon, Puck, Kore, Fenrir, Aoede, Zephyr, Leda, Orus."
                else:
                    save_setting("voice", v)
                    self.ui.write_log(f"SYS: Voice → {v}")
                    result = f"Changing my voice to {v}, sir."
                    self._pending_reconnect = True

            elif name == "where_am_i":
                r = await loop.run_in_executor(None, lambda: where_am_i(parameters=args, player=self.ui))
                result = r or "Location delivered."

            elif name == "set_my_location":
                r = await loop.run_in_executor(None, lambda: set_my_location(parameters=args, player=self.ui))
                result = r or "Location set."

            elif name == "route_directions":
                r = await loop.run_in_executor(None, lambda: route_directions(parameters=args, player=self.ui))
                result = r or "Route delivered."

            elif name == "different_route":
                r = await loop.run_in_executor(None, lambda: different_route(parameters=args, player=self.ui))
                result = r or "Alternative route shown."

            elif name == "analyze_route":
                r = await loop.run_in_executor(None, lambda: analyze_route(parameters=args, player=self.ui))
                result = r or "Route analyzed."

            elif name == "find_nearby":
                r = await loop.run_in_executor(None, lambda: find_nearby(parameters=args, player=self.ui))
                result = r or "Places found."

            elif name == "place_info":
                r = await loop.run_in_executor(None, lambda: place_info(parameters=args, player=self.ui))
                result = r or "Place info delivered."

            elif name == "browser_control":
                r = await loop.run_in_executor(None, lambda: browser_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "file_controller":
                r = await loop.run_in_executor(None, lambda: file_controller(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "send_message":
                r = await loop.run_in_executor(None, lambda: send_message(parameters=args, response=None, player=self.ui, session_memory=None))
                result = r or f"Message sent to {args.get('receiver')}."

            elif name == "make_call":
                r = await loop.run_in_executor(None, lambda: make_call(parameters=args, response=None, player=self.ui))
                result = r or f"Calling {args.get('receiver')}."

            elif name == "reminder":
                r = await loop.run_in_executor(None, lambda: reminder(parameters=args, response=None, player=self.ui))
                result = r or "Reminder set."

            elif name == "youtube_video":
                r = await loop.run_in_executor(None, lambda: youtube_video(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "screen_process":
                import time as _t_mod
                _now = _t_mod.monotonic()
                _cooldown = 4.0  # seconds — covers echo window after speaking ends
                if self._vision_busy or (_now - self._vision_last_time) < _cooldown:
                    _wait = max(0, _cooldown - (_now - self._vision_last_time))
                    print(f"[Vision] ⏳ Cooldown active ({_wait:.1f}s remaining) — ignoring duplicate call")
                    result = "Vision is still processing the previous request. I will not call this again."
                else:
                    self._vision_busy      = True
                    self._vision_last_time = _now
                    angle     = args.get("angle", "screen").lower()
                    user_text = args.get("text", "What do you see?")
                    if angle == "camera":
                        img_b, mime_t = await loop.run_in_executor(None, _capture_camera)
                        self.ui.start_camera_stream()
                        self._vision_cam_active = True
                        print(f"[Vision] 📷 Camera: {len(img_b):,} bytes")
                        _stall = "camera"
                    else:
                        img_b, mime_t = await loop.run_in_executor(None, _capture_screen)
                        print(f"[Vision] 🖥️  Screen: {len(img_b):,} bytes")
                        _stall = "screen"
                    self._pending_vision = (img_b, mime_t, user_text, angle)
                    result = (
                        f"[VISION_ACTIVE] {_stall.capitalize()} captured. "
                        f"Immediately say ONE short natural sentence in English, "
                        f"telling them you are looking at their {_stall} right now. "
                        f"Do NOT describe or guess content — the actual image arrives in the NEXT message."
                    )

            elif name == "close_camera":
                self.ui.stop_camera_stream()
                result = "Camera closed."

            elif name == "computer_settings":
                r = await loop.run_in_executor(None, lambda: computer_settings(parameters=args, response=None, player=self.ui))
                result = r or "Done."

            elif name == "desktop_control":
                r = await loop.run_in_executor(None, lambda: desktop_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "code_helper":
                r = await loop.run_in_executor(None, lambda: code_helper(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "dev_agent":
                r = await loop.run_in_executor(None, lambda: dev_agent(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "web_search":
                r = await loop.run_in_executor(None, lambda: web_search_action(parameters=args, player=self.ui))
                result = r or "Done."
                # Mirror results to the on-screen content panel
                _mode = args.get("mode", "search")
                if r and not r.startswith("No results") and not r.startswith("Search failed"):
                    _query = args.get("query") or ", ".join(args.get("items", []))
                    _label = f"{_mode.upper()} — {_query[:38]}" if _query else _mode.upper()
                    self.ui.show_content(_label, r)
            elif name == "file_processor":
                if not args.get("file_path") and self.ui.current_file:
                    args["file_path"] = self.ui.current_file
                r = await loop.run_in_executor(
                    None,
                    lambda: file_processor(parameters=args, player=self.ui, speak=self.speak)
                )
                result = r or "Done."

            elif name == "computer_control":
                r = await loop.run_in_executor(None, lambda: computer_control(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "game_updater":
                r = await loop.run_in_executor(None, lambda: game_updater(parameters=args, player=self.ui, speak=self.speak))
                result = r or "Done."

            elif name == "flight_finder":
                r = await loop.run_in_executor(None, lambda: flight_finder(parameters=args, player=self.ui))
                result = r or "Done."

            elif name == "system_status":
                r = await loop.run_in_executor(None, get_system_status)
                result = str(r)

            elif name == "battery_status":
                result = await loop.run_in_executor(None, lambda: battery_status(parameters=args, player=self.ui))

            elif name == "battery_reminder":
                act = (args.get("action") or "").lower().strip()
                pct = args.get("percent")
                bm = self._battery_monitor
                if act == "set_low" and pct is not None:
                    bm.set_low(int(pct))
                    result = f"I'll remind you to plug in when the battery drops below {int(pct)}%."
                elif act == "set_high" and pct is not None:
                    bm.set_high(int(pct))
                    result = f"I'll remind you to unplug when the battery reaches {int(pct)}% while charging."
                elif act == "clear_low":
                    bm.set_low(None)
                    result = "Low-battery reminder cleared."
                elif act == "clear_high":
                    bm.set_high(None)
                    result = "Unplug reminder cleared — charging normally now."
                elif act == "status":
                    result = bm.status_text()
                else:
                    result = "Sir, tell me a threshold percent to set, or say 'status'/'clear'."

            elif name == "manage_monitor":
                action = args.get("action", "").lower().strip()
                topic  = args.get("topic", "").strip()
                if action == "add" and topic:
                    result = await asyncio.to_thread(add_monitor, topic)
                elif action == "remove" and topic:
                    result = await asyncio.to_thread(remove_monitor, topic)
                elif action == "list":
                    topics = await asyncio.to_thread(list_monitors)
                    result = ("Monitoring: " + ", ".join(topics)) if topics else "No topics are being monitored."
                else:
                    result = "Specify action (add/remove/list) and a topic."

            elif name == "shutdown_parker":
                self.ui.write_log("SYS: Shutdown requested.")
                async def _do_shutdown():
                    await self._save_session_summary()
                    if self.session:
                        try:
                            await self.session.send_client_content(
                                turns={"parts": [{"text": "Say a brief natural goodbye to the user."}]},
                                turn_complete=True,
                            )
                        except Exception:
                            pass
                    await asyncio.sleep(1.5)
                    import os as _os
                    _os._exit(0)
                asyncio.create_task(_do_shutdown())

            else:
                result = f"Unknown tool: {name}"

        except Exception as e:
            result = f"Tool '{name}' failed: {e}"
            traceback.print_exc()
            self.speak_error(name, e)

        if not self.ui.muted:
            self.ui.set_state("LISTENING")

        print(f"[PARKER] 📤 {name} → {str(result)[:80]}")
        return types.FunctionResponse(
            id=fc.id, name=name,
            response={"result": result}
        )

    async def _send_realtime(self):
        while True:
            msg = await self.out_queue.get()
            try:
                await self.session.send_realtime_input(media=msg)
            except Exception:
                # Session closed (e.g. an intentional reconnect for a voice/persona
                # change). Exit quietly instead of crashing the TaskGroup.
                if self._pending_reconnect or self._reconnecting:
                    return
                raise

    async def _listen_audio(self):
        print("[PARKER] 🎤 Mic started")
        loop = asyncio.get_event_loop()
        import numpy as _np

        # Adaptive gain: quietly boost weak mics so Gemini hears clearly, with a
        # hard clip-guard so we never distort. Gain adapts slowly toward a target
        # peak; if a boosted block would clip, we back off instead of overdriving.
        _target_peak = 0.5 * 32767.0   # aim for ~-6 dBFS peaks
        _max_gain = 6.0                # never amplify more than 6× (weak mics)
        _gain_state = {"g": 1.0}

        def callback(indata, frames, time_info, status):
            with self._speaking_lock:
                assistant_speaking = self._is_speaking
            # Skip while reconnecting (voice/persona change) — the sender isn't
            # draining the queue, so there's no point buffering mic audio.
            if (not assistant_speaking and not self.ui.muted
                    and not self._phone_active
                    and not self._reconnecting and not self._pending_reconnect):
                try:
                    samples = indata.reshape(-1).astype(_np.float32)
                    peak = float(_np.max(_np.abs(samples))) if samples.size else 0.0
                    if peak > 500.0:  # only adapt on real speech, ignore silence/hum
                        desired = _target_peak / peak
                        desired = max(1.0, min(_max_gain, desired))
                        # Smooth toward the desired gain so volume doesn't pump.
                        _gain_state["g"] += 0.1 * (desired - _gain_state["g"])
                    g = _gain_state["g"]
                    if g > 1.01:
                        boosted = samples * g
                        # Clip-guard: if the peak would exceed full scale, scale
                        # this block down just enough to stay clean.
                        bpeak = float(_np.max(_np.abs(boosted)))
                        if bpeak > 32767.0:
                            boosted *= 32767.0 / bpeak
                        data = boosted.astype(_np.int16).tobytes()
                    else:
                        data = indata.tobytes()
                except Exception:
                    data = indata.tobytes()  # never let audio math break the mic
                loop.call_soon_threadsafe(
                    self._enqueue_audio,
                    {"data": data, "mime_type": "audio/pcm"}
                )

        try:
            with sd.InputStream(
                samplerate=SEND_SAMPLE_RATE,
                channels=CHANNELS,
                dtype="int16",
                blocksize=CHUNK_SIZE,
                callback=callback,
            ):
                print("[PARKER] 🎤 Mic stream open")
                while True:
                    await asyncio.sleep(0.1)
        except Exception as e:
            print(f"[PARKER] ❌ Mic: {e}")
            raise

    def _enqueue_audio(self, msg):
        """Push a mic frame onto out_queue without ever raising. If the queue is
        full (sender stalled, e.g. during a voice/persona reconnect), drop the
        OLDEST frame and enqueue the newest — realtime audio must not back up or
        crash the event loop with QueueFull."""
        q = self.out_queue
        if q is None:
            return
        try:
            q.put_nowait(msg)
        except asyncio.QueueFull:
            try:
                q.get_nowait()      # discard stalest frame
            except Exception:
                pass
            try:
                q.put_nowait(msg)
            except Exception:
                pass

    async def _receive_audio(self):
        print("[PARKER] 👂 Recv started")
        out_buf, in_buf = [], []

        try:
            while True:
                async for response in self.session.receive():

                    if response.data:
                        if self._interrupted:
                            pass  # discard: interrupted
                        else:
                            if self._turn_done_event and self._turn_done_event.is_set():
                                self._turn_done_event.clear()
                            # Split into ~50 ms chunks so interrupt() stops audio within 50 ms
                            # (24000 Hz × 2 bytes/sample × 0.05 s = 2400 bytes per slice)
                            _audio_data = response.data
                            _SLICE = 2400
                            for _i in range(0, len(_audio_data), _SLICE):
                                self.audio_in_queue.put_nowait(_audio_data[_i : _i + _SLICE])

                    if response.server_content:
                        sc = response.server_content

                        if sc.output_transcription and sc.output_transcription.text:
                            txt = _clean_transcript(sc.output_transcription.text)
                            if txt and txt != (out_buf[-1] if out_buf else ""):
                                out_buf.append(txt)

                        if sc.input_transcription and sc.input_transcription.text:
                            txt = _clean_transcript(sc.input_transcription.text)
                            if txt:
                                in_buf.append(txt)
                                self._last_user_speech = time.monotonic()

                        if sc.turn_complete:
                            if self._turn_done_event:
                                self._turn_done_event.set()

                            # A voice/persona change was requested — reconnect now
                            # (after the confirmation was spoken) so the new config
                            # takes effect.
                            if self._pending_reconnect:
                                self._pending_reconnect = False
                                self._reconnecting = True
                                self._conn_backoff = 1
                                self.ui.write_log("SYS: Applying new voice/persona…")
                                try:
                                    await self.session.close()
                                except Exception:
                                    pass
                                return

                            # If this turn_complete ends an interrupted response, clear the
                            # flag and skip all further processing for that turn.
                            if self._interrupted:
                                self._interrupted = False
                                in_buf  = []
                                out_buf = []
                                continue

                            full_in = " ".join(in_buf).strip()
                            if full_in:
                                self.ui.write_log(f"You: {full_in}")
                                self._session_log.append(f"User: {full_in}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "user",
                                        "text": full_in,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            in_buf = []

                            full_out = " ".join(out_buf).strip()
                            if full_out:
                                self.ui.write_log(f"{self._asst_name}: {full_out}")
                                self._session_log.append(f"{self._asst_name}: {full_out}")
                                if self._dashboard:
                                    asyncio.create_task(self._dashboard.broadcast({
                                        "type": "log", "speaker": "parker",
                                        "text": full_out,
                                        "ts": datetime.now().isoformat(),
                                    }))
                            out_buf = []

                            # Vision injection: model finished tool-response turn → now send the image
                            if self._pending_vision and self.session:
                                import base64 as _b64
                                img_b, mime_t, question, angle = self._pending_vision
                                self._pending_vision = None
                                b64 = _b64.b64encode(img_b).decode("ascii")
                                print(f"[Vision] 📤 {len(img_b):,} bytes (angle={angle}) → main session")
                                await self.session.send_client_content(
                                    turns={"parts": [
                                        {"inline_data": {"mime_type": mime_t, "data": b64}},
                                        {"text": question},
                                    ]},
                                    turn_complete=True,
                                )
                                # Mark next turn_complete behaviour depending on angle
                                if self._vision_cam_active:
                                    # Camera: keep busy until Parker finishes speaking the answer
                                    self._vision_cam_active    = False
                                    self._vision_close_pending = True
                                else:
                                    # Screen-only: no camera to close; release busy flag now
                                    self._vision_busy = False
                            elif self._vision_close_pending:
                                # This turn_complete IS the vision answer — close camera + release busy flag
                                self._vision_close_pending = False
                                self._vision_busy = False
                                async def _cam_close():
                                    await asyncio.sleep(2.0)
                                    self.ui.stop_camera_stream()
                                asyncio.create_task(_cam_close())

                    if response.tool_call:
                        fn_responses = []
                        for fc in response.tool_call.function_calls:
                            print(f"[PARKER] 📞 {fc.name}")
                            fr = await self._execute_tool(fc)
                            fn_responses.append(fr)
                        await self.session.send_tool_response(
                            function_responses=fn_responses
                        )
        except Exception as e:
            print(f"[PARKER] ❌ Recv: {e}")
            traceback.print_exc()
            raise

    async def _play_audio(self):
        print("[PARKER] 🔊 Play started")

        stream = sd.RawOutputStream(
            samplerate=RECEIVE_SAMPLE_RATE,
            channels=CHANNELS,
            dtype="int16",
            blocksize=CHUNK_SIZE,
        )
        stream.start()

        try:
            while True:
                try:
                    chunk = await asyncio.wait_for(
                        self.audio_in_queue.get(),
                        timeout=0.1
                    )
                except asyncio.TimeoutError:
                    if (
                        self._turn_done_event
                        and self._turn_done_event.is_set()
                        and self.audio_in_queue.empty()
                    ):
                        self.set_speaking(False)
                        self._turn_done_event.clear()
                    continue

                self.set_speaking(True)

                # Batch all immediately-available chunks into one write to reduce
                # thread-pool round-trips (was one asyncio.to_thread per 50ms slice).
                # Cap at ~200 ms so interrupt() still stops audio within ~200 ms.
                batch = bytearray(chunk)
                while len(batch) < 9600:   # 9600 bytes ≈ 200 ms at 24 kHz / 16-bit mono
                    try:
                        batch.extend(self.audio_in_queue.get_nowait())
                    except asyncio.QueueEmpty:
                        break

                try:
                    await asyncio.to_thread(stream.write, bytes(batch))
                except (RuntimeError, asyncio.CancelledError):
                    break   # executor shutting down — exit cleanly
        except Exception as e:
            print(f"[PARKER] ❌ Play: {e}")
            raise
        finally:
            self.set_speaking(False)
            stream.stop()
            stream.close()

    # ── Morning briefing ────────────────────────────────────────────────────────

    async def _send_startup_briefing(self) -> None:
        """
        Two-phase briefing optimized for speed:
          Phase 1 — instant greeting (no tools) → speech starts in <1s
          Phase 2 — news pre-fetched in a background thread while Phase 1 plays,
                    delivered as ready text (no Gemini tool-call round-trip) and
                    shown on the UI content panel. Waits for turn_complete event
                    instead of a fixed sleep so there is no unnecessary gap.
        """
        memory   = load_memory()
        identity = memory.get("identity", {})

        def _val(k: str) -> str:
            e = identity.get(k, {})
            return (e.get("value", "") if isinstance(e, dict) else str(e)).strip()

        lang = _val("language")
        name = _val("name")
        time_str = datetime.now().strftime("%H:%M")

        # Start fetching news immediately — runs in parallel while phase 1 plays.
        # Focus on Vietnam news, which is this assistant's core news topic.
        loop = asyncio.get_event_loop()
        news_future = loop.run_in_executor(None, _fetch_news_sync, "Vietnam news today")

        await asyncio.sleep(0.3)
        if not self.session:
            return

        # ── Phase 1: instant greeting ─────────────────────────────────────────
        lang_clause = " Respond in English."
        name_clause = f" Address the user as {name}." if name else ""

        # Inject last session context if available — pop removes it so it's never repeated
        last = await asyncio.to_thread(pop_last_session)
        session_clause = ""
        if last:
            try:
                _delta = (datetime.now() - datetime.strptime(last["date"], "%Y-%m-%d")).days
                _when  = "earlier today" if _delta == 0 else ("yesterday" if _delta == 1 else f"{_delta} days ago")
            except Exception:
                _when = "last time"
            session_clause = (
                f" Also briefly and naturally mention that {_when}: {last['summary']}"
            )

        p1 = (
            f"Greet the user warmly, mention it is {time_str}, and say you are fetching today's Vietnam news now.{session_clause} "
            f"Keep it to 2 short sentences max. Do not call any tools.{lang_clause}{name_clause}"
        )

        # Clear the turn-done event so we can wait for Phase 1 to finish
        if self._turn_done_event:
            self._turn_done_event.clear()

        await self.session.send_client_content(
            turns={"parts": [{"text": p1}]},
            turn_complete=True,
        )
        self.ui.write_log("SYS: Briefing phase 1 (greeting) sent.")

        # ── Phase 2: fire as soon as Phase 1 audio is done ───────────────────
        async def _deliver_news():
            try:
                lang_str = " Respond in English."

                # Wait for news fetch (already running) and Phase 1 turn-complete
                # in parallel — whichever takes longer determines the wait time
                news_done   = asyncio.wrap_future(news_future)
                turn_waited = False
                if self._turn_done_event:
                    try:
                        await asyncio.wait_for(self._turn_done_event.wait(), timeout=6.0)
                        turn_waited = True
                    except asyncio.TimeoutError:
                        pass

                # Extra buffer: turn_complete fires when Gemini finishes *generating*
                # Phase 1, but audio may still be playing.  Waiting a beat here
                # prevents Phase 2 audio from arriving while Phase 1 is mid-sentence
                # (which sounds like a "repeated first response" to the user).
                if turn_waited:
                    await asyncio.sleep(0.8)
                else:
                    await asyncio.sleep(1.0)

                try:
                    news_text = await asyncio.wait_for(news_done, timeout=4.0)
                except Exception:
                    news_text = ""

                if not self.session:
                    return

                if news_text and len(news_text) > 60:
                    # Show on UI content panel immediately
                    self.ui.show_content("NEWS — top world news today", news_text)

                    p2 = (
                        f"[BRIEFING] Here are today's top news headlines:\n{news_text}\n\n"
                        "Pick ONE headline, summarise it in one sentence, then say the full list "
                        f"is displayed on screen. Do not call any tools.{lang_str}"
                    )
                else:
                    p2 = (
                        "News headlines could not be fetched right now. "
                        f"Let the user know briefly.{lang_str}"
                    )

                await self.session.send_client_content(
                    turns={"parts": [{"text": p2}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Briefing phase 2 (news) sent.")
            except Exception as e:
                print(f"[Briefing] Phase 2 error: {e}")
                self.ui.write_log(f"SYS: Briefing phase 2 failed: {e}")

        asyncio.create_task(_deliver_news())

    # ── Session memory ──────────────────────────────────────────────────────────

    async def _save_session_summary(self) -> None:
        """Summarise the current session in 1-2 sentences and save to long_term.json."""
        log = self._session_log
        if len(log) < 3:          # need at least one exchange to be worth saving
            return
        self._session_log = []    # reset immediately so the next session starts clean

        memory = load_memory()
        lang_entry = memory.get("identity", {}).get("language", {})
        lang = (lang_entry.get("value", "") if isinstance(lang_entry, dict) else str(lang_entry)).strip()
        lang = lang or "English"

        convo = "\n".join(log[-40:])   # cap at last 40 turns to stay within token budget
        prompt = (
            f"Summarize this conversation in 1-2 sentences in English. "
            "Focus on what the user accomplished or discussed. "
            "Output ONLY the summary text, nothing else:\n\n" + convo
        )
        try:
            from google import genai as _genai
            client = _genai.Client(api_key=_get_api_key())
            resp   = await asyncio.to_thread(
                client.models.generate_content,
                model=TEXT_MODEL,
                contents=prompt,
            )
            summary = (resp.text or "").strip()
            if summary:
                save_session_summary(summary, "English")
        except Exception as e:
            print(f"[Memory] ⚠️ Session summary failed: {e}")

    # ── System monitor ──────────────────────────────────────────────────────────

    async def _run_network_watchdog(self) -> None:
        """Detect a network drop quickly and force the offline transition,
        instead of waiting for a Gemini request to time out."""
        misses = 0
        while self.session is not None:
            await asyncio.sleep(5)
            online = await asyncio.to_thread(_has_internet)
            if online:
                misses = 0
                continue
            misses += 1
            # Require two consecutive misses (~10s) to avoid a false trip on a
            # momentary blip.
            if misses >= 2 and self.session is not None:
                self.ui.write_log("NET: Internet lost — switching to offline mode.")
                try:
                    await self.session.close()      # ends receive() → net-error path
                except Exception:
                    pass
                return

    async def _run_system_monitor(self) -> None:
        """Background task: voice alerts when metrics exceed thresholds."""
        while True:
            await asyncio.sleep(10)
            alert = await asyncio.to_thread(self._sys_monitor.check)
            if not alert or not self.session:
                continue
            # Don't interrupt an active conversation
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self.session.send_client_content(
                    turns={"parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send alert: {e}")

    async def _run_battery_monitor(self) -> None:
        """Background task: voice reminders to plug in / unplug at thresholds
        the user set. Windows exposes no software API to actually stop
        charging on this machine (Lenovo Vantage's Conservation Mode talks to
        an undocumented kernel driver), so this reminds instead of enforcing."""
        while True:
            await asyncio.sleep(30)
            alert = await asyncio.to_thread(self._battery_monitor.check)
            if not alert or not self.session:
                continue
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            try:
                await self.session.send_client_content(
                    turns={"parts": [{"text": alert}]},
                    turn_complete=True,
                )
            except Exception as e:
                print(f"[Monitor] ⚠️ Could not send battery alert: {e}")

    async def _run_trending_news_schedule(self) -> None:
        """Background task: automatically run the trending-news feature once a
        day at the configured time (config 'trending_news_schedule' —
        default disabled). Checks every minute; fires once per calendar day
        when the clock matches, regardless of how long Parker has been open."""
        from datetime import datetime as _dt
        while True:
            await asyncio.sleep(60)
            sched = get_trending_schedule()
            if not sched["enabled"]:
                continue
            now = _dt.now()
            today = now.date()
            if (self._trending_last_run_date == today
                    or now.hour != sched["hour"]
                    or now.minute != sched["minute"]):
                continue
            # Don't interrupt an active conversation — wait for a quiet moment
            # rather than skipping the day entirely.
            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking or (time.monotonic() - self._last_user_speech) < 10:
                continue
            self._trending_last_run_date = today
            self.ui.write_log("SYS: Scheduled trending news starting…")
            try:
                loop = asyncio.get_event_loop()
                await asyncio.wait_for(
                    loop.run_in_executor(
                        None, lambda: trending_news(parameters={}, player=self.ui)),
                    timeout=90,
                )
            except asyncio.TimeoutError:
                self.ui.write_log("ERR: Scheduled trending news timed out after 90s.")
            except Exception as e:
                print(f"[Monitor] ⚠️ Scheduled trending news failed: {e}")

    # ── Background monitor ──────────────────────────────────────────────────────

    async def _run_background_monitor(self) -> None:
        """Check user-configured topics once per day; speak alerts when new headlines appear."""
        await asyncio.sleep(300)          # wait 5 min after startup before first check
        while True:
            if self.session:
                # Don't interrupt if user spoke recently or Parker is mid-sentence
                with self._speaking_lock:
                    speaking = self._is_speaking
                recent_speech = (time.monotonic() - self._last_user_speech) < 30
                if not speaking and not recent_speech:
                    try:
                        alerts = await asyncio.to_thread(monitor_check_all)
                        memory = load_memory()
                        lang_e = memory.get("identity", {}).get("language", {})
                        lang   = (lang_e.get("value", "") if isinstance(lang_e, dict) else str(lang_e)).strip() or "English"
                        for alert in alerts:
                            msg = (
                                f"{alert}\n\n"
                                f"Inform the user about this development naturally in English. "
                                "One brief sentence only."
                            )
                            await self.session.send_client_content(
                                turns={"parts": [{"text": msg}]},
                                turn_complete=True,
                            )
                            self.ui.write_log(f"SYS: Monitor alert sent.")
                            await asyncio.sleep(6)   # gap between consecutive alerts
                    except Exception as e:
                        print(f"[Monitor] ⚠️ Background check error: {e}")
            await asyncio.sleep(1800)     # check every 30 minutes

    # ── Proactive mode ──────────────────────────────────────────────────────────

    async def _run_proactive_mode(self) -> None:
        """
        Background task: periodically checks if the user has been silent long enough,
        then hands time + memory context to Gemini so it can decide what (if anything)
        to say proactively. No hardcoded rules — Gemini makes the call.
        """
        while True:
            await asyncio.sleep(60)   # evaluate once per minute

            if not self.session:
                continue

            with self._speaking_lock:
                speaking = self._is_speaking
            if speaking:
                continue

            if not self._proactive.should_trigger(self._last_user_speech):
                continue

            self._proactive.mark_triggered()

            try:
                memory       = await asyncio.to_thread(load_memory)
                monitors     = await asyncio.to_thread(list_monitors)
                recent_turns = self._session_log[-8:] if self._session_log else []
                prompt = self._proactive.build_prompt(
                    memory       = memory,
                    monitors     = monitors or None,
                    recent_turns = recent_turns or None,
                )
                await self.session.send_client_content(
                    turns={"parts": [{"text": prompt}]},
                    turn_complete=True,
                )
                self.ui.write_log("SYS: Proactive check-in.")
            except Exception as e:
                print(f"[Proactive] ⚠️ {e}")

    # ── Phone audio relay ────────────────────────────────────────────────────────

    async def _relay_phone_audio(self) -> None:
        """Forward phone mic PCM chunks from dashboard queue into the Gemini Live session."""
        q = self._dashboard._phone_audio_queue
        while True:
            try:
                chunk = await asyncio.wait_for(q.get(), timeout=1.0)
            except asyncio.TimeoutError:
                # No audio for 1 s → phone mic inactive, give PC mic back
                self._phone_active = False
                continue
            self._phone_active = True   # phone is streaming — silence PC mic
            with self._speaking_lock:
                speaking = self._is_speaking
            if not speaking and not self.ui.muted:
                try:
                    self.out_queue.put_nowait(chunk)
                except asyncio.QueueFull:
                    pass

    def _on_phone_connected(self) -> None:
        self.ui.write_log("SYS: Phone connected via Remote Dashboard.")
        self.ui.notify_phone_connected()

    # ── dashboard command relay ─────────────────────────────────────────────

    async def _process_dashboard_commands(self) -> None:
        while True:
            try:
                text = await asyncio.wait_for(
                    self._dashboard._command_queue.get(), timeout=0.5
                )
                if not text:
                    continue
                # Wait up to 8s for session to become ready after a wake
                for _ in range(80):
                    if self.session:
                        break
                    await asyncio.sleep(0.1)
                if self.session:
                    await self.session.send_client_content(
                        turns={"parts": [{"text": text}]},
                        turn_complete=True,
                    )
                    self.ui.write_log(f"[Web]: {text}")
                else:
                    print(f"[Dashboard] Dropped command (no session): {text}")
            except asyncio.TimeoutError:
                pass
            except Exception as e:
                print(f"[Dashboard] Command error: {e}")
                await asyncio.sleep(0.5)

    # ── main loop ───────────────────────────────────────────────────────────

    async def run(self):
        self._loop = asyncio.get_event_loop()

        # Start dashboard (optional — needs: pip install fastapi "uvicorn[standard]" cryptography)
        try:
            from dashboard.server import DashboardServer
            self._dashboard = DashboardServer()
            self._dashboard.set_connect_callback(self._on_phone_connected)
            asyncio.create_task(self._dashboard.serve())
            # Runs for the whole lifetime, not just inside an active session
            asyncio.create_task(self._process_dashboard_commands())
        except Exception as e:
            print(f"[Dashboard] Disabled: {e}")
            self._dashboard = None

        while True:
            # ── FORCE_OFFLINE mode: skip the cloud entirely ──────────────
            if FORCE_OFFLINE and not self._reconnecting:
                self._start_offline_voice()
                self.ui.set_state("SLEEPING")
                self.ui.write_log("SYS: OFFLINE-ONLY mode (config force_offline=true).")
                while FORCE_OFFLINE and self._offline_voice and self._offline_voice.is_running():
                    await asyncio.sleep(5)
                if not FORCE_OFFLINE:
                    continue
                return

            try:
                print("[PARKER] Connecting...")
                self.ui.set_state("THINKING")
                config = self._build_config()

                # Fresh client on every reconnect — avoids stale HTTP session state
                client = genai.Client(
                    api_key=_get_api_key(),
                    http_options={"api_version": "v1beta"}
                )

                async with (
                    client.aio.live.connect(model=LIVE_MODEL, config=config) as session,
                    asyncio.TaskGroup() as tg,
                ):
                    self.session          = session
                    self.audio_in_queue   = asyncio.Queue()
                    self.out_queue        = asyncio.Queue(maxsize=200)
                    self._turn_done_event = asyncio.Event()

                    # Reset transient state that must not carry over from a previous session
                    self._pending_vision       = None
                    self._vision_cam_active    = False
                    self._vision_close_pending = False
                    self._vision_busy          = False
                    self._vision_last_time     = 0.0
                    self._interrupted          = False

                    print("[PARKER] Connected.")
                    self._stop_offline_voice()      # cloud is back — hand mic to Gemini
                    # Reset the offline-announcement latch so the next disconnect
                    # announces again.
                    self._announced_offline = False
                    self._was_online = True
                    self.ui.set_state("LISTENING")
                    self.ui.write_log("SYS: Parker online.")

                    if self._dashboard:
                        await self._dashboard.broadcast({"type": "status", "state": "active"})

                    tg.create_task(self._send_realtime())
                    tg.create_task(self._listen_audio())
                    tg.create_task(self._receive_audio())
                    tg.create_task(self._play_audio())
                    tg.create_task(self._run_system_monitor())
                    tg.create_task(self._run_battery_monitor())
                    tg.create_task(self._run_trending_news_schedule())
                    tg.create_task(self._run_background_monitor())
                    tg.create_task(self._run_proactive_mode())
                    tg.create_task(self._run_network_watchdog())
                    if self._dashboard:
                        tg.create_task(self._relay_phone_audio())

                    # Morning briefing — fires once per process launch (if enabled)
                    if not self._briefing_sent and get_brief_enabled():
                        self._briefing_sent = True
                        tg.create_task(self._send_startup_briefing())

            except KeyboardInterrupt:
                raise
            except SystemExit:
                raise
            except BaseException as e:
                # Catches both Exception and BaseExceptionGroup (Python 3.11+
                # TaskGroup raises BaseExceptionGroup when tasks are cancelled
                # externally, which `except Exception` would miss, letting the
                # exception escape the while-loop and causing asyncio.run() to
                # start shutdown — resulting in "executor after shutdown" errors).
                err_str = str(e)

                # Intentional reconnect (voice/persona change) closes the socket
                # cleanly — don't print a scary traceback for that; just reconnect
                # quickly (the `finally` below clears the session).
                _was_reconnect = self._reconnecting
                if _was_reconnect:
                    self._reconnecting = False
                    self._conn_backoff = 1
                    print("[PARKER] Applying new voice/persona — reconnecting…")
                else:
                    print(f"[PARKER] Error ({type(e).__name__}): {e}")
                    traceback.print_exc()

                # Invalid API key — stop hammering the API, prompt re-configuration
                if not _was_reconnect and ("API key not valid" in err_str or "1007" in err_str):
                    self.ui.write_log("ERR: API key invalid — please re-enter your key.")
                    self.ui.set_state("SLEEPING")
                    self.ui.prompt_reconfig()
                    while not self.ui._win._ready:
                        await asyncio.sleep(1)
                    print("[PARKER] New API key saved — reconnecting...")
                    _conn_backoff = 3
                    continue

                # Quota exhausted (429). Don't keep hammering the API — switch
                # to the offline voice immediately and tell the user by voice.
                if not _was_reconnect and (
                        "RESOURCE_EXHAUSTED" in err_str or "429" in err_str
                        or "quota" in err_str.lower()):
                    self.ui.write_log(
                        "ERR: Gemini quota exhausted (429) — switching to "
                        "offline mode. Check your plan/billing at "
                        "ai.google.dev/gemini-api/docs/rate-limits.")
                    self.set_speaking(False)
                    self._start_offline_voice()
                    if self._offline_voice and self._announced_offline is False:
                        self._announced_offline = True
                        notice = (
                            "My Gemini quota has been exhausted, sir. "
                            "I'm switching to the local model now. "
                            "I can still help you with everything offline. "
                            "The quota usually resets within a day.")
                        await asyncio.to_thread(self._offline_voice.announce, notice)
                    # Stay in this state until the user restarts or quota resets.
                    while True:
                        await asyncio.sleep(30)
                        if not self._offline_voice or not self._offline_voice.is_running():
                            break
                    continue

                # Google-side transient internal error (1011): not our bug and
                # not quota. It arrives as a ConnectionClosed, so the net-error
                # handling below backs off and reconnects; just note it clearly.
                if not _was_reconnect and "1011" in err_str:
                    self.ui.write_log(
                        "NET: Gemini internal error (1011) — reconnecting.")

                # Network / timeout errors — log clearly and back off. Also treat
                # it as a network error if an active connectivity check fails (the
                # watchdog may have closed the session cleanly on a drop).
                is_net_err = (not _was_reconnect) and any(k in err_str for k in (
                    "TimeoutError", "timed out", "getaddrinfo", "CancelledError",
                    "ConnectionRefusedError", "OSError", "Cannot connect",
                    "ConnectionClosed", "connection closed",
                ))
                if not _was_reconnect and not is_net_err and not await asyncio.to_thread(_has_internet):
                    is_net_err = True
                if is_net_err:
                    _conn_backoff = min(getattr(self, "_conn_backoff", 3) * 2, 60)
                    self._conn_backoff = _conn_backoff
                    self.ui.write_log(
                        f"NET: Can't reach Gemini — retrying in {_conn_backoff}s.")
                    # Offline fallback: if a local model is available, start the
                    # offline VOICE loop so the user can keep talking to Parker,
                    # and typed commands also route to the local model.
                    try:
                        from core.offline_agent import offline_available
                        if offline_available():
                            self.ui.write_log(
                                "SYS: OFFLINE MODE ready — local model in use "
                                "(cloud features like weather/news still need internet).")
                            # Announce the switch by voice, but only the first
                            # time we drop offline (not on every retry).
                            first_drop = not self._announced_offline
                            self._start_offline_voice()
                            if first_drop and self._offline_voice is not None:
                                self._announced_offline = True
                                if self._was_online:
                                    notice = ("Connection lost. Switching to offline mode, sir. "
                                              "I'll keep helping with the local model.")
                                else:
                                    notice = ("No internet connection. Starting in offline mode, sir. "
                                              "I'll use the local model.")
                                await asyncio.to_thread(self._offline_voice.announce, notice)
                    except Exception:
                        pass
                else:
                    self._conn_backoff = 3
            finally:
                self.session = None
                # Only save if there was a real conversation (≥3 turns)
                if len(self._session_log) >= 3:
                    asyncio.create_task(self._save_session_summary())

            self.set_speaking(False)
            self.ui.set_state("SLEEPING")

            if self._dashboard:
                await self._dashboard.broadcast({"type": "status", "state": "sleeping"})

            delay = getattr(self, "_conn_backoff", 3)
            print(f"[PARKER] Reconnecting in {delay}s...")
            await asyncio.sleep(delay)

def main():
    ui = ParkerUI("face.png")

    def runner():
        ui.wait_for_api_key()
        parker = ParkerLive(ui)
        try:
            asyncio.run(parker.run())
        except KeyboardInterrupt:
            print("\n🔴 Shutting down...")

    threading.Thread(target=runner, daemon=True).start()
    ui.root.mainloop()

if __name__ == "__main__":
    main()