"""
spotify.py — play music on Spotify for Parker.

Two ways to play a song:
  1. Spotify Web API (preferred) — searches and plays the exact track on your
     active Spotify device. Requires Spotify PREMIUM and OAuth credentials in
     config/api_keys.json:
         "spotify_client_id":     "...",
         "spotify_client_secret": "...",
         "spotify_refresh_token": "..."   (get it with tools/spotify_auth.py)
  2. UI automation fallback — opens the Spotify app, types the query into
     search, and plays the top result (works without Premium/API, less reliable).
"""

import base64
import json
import time
import urllib.error
import urllib.parse
import urllib.request

from memory.config_manager import load_api_keys

_TOKEN_URL = "https://accounts.spotify.com/api/token"
_API = "https://api.spotify.com/v1"

# Cached access token (refreshes automatically).
_ACCESS = {"token": "", "expires": 0.0}


def _creds():
    c = load_api_keys()
    return (c.get("spotify_client_id", "").strip(),
            c.get("spotify_client_secret", "").strip(),
            c.get("spotify_refresh_token", "").strip())


def _http(url, data=None, headers=None, method="GET"):
    req = urllib.request.Request(url, data=data, method=method,
                                 headers=headers or {})
    with urllib.request.urlopen(req, timeout=12) as r:
        raw = r.read().decode("utf-8", "ignore")
        return json.loads(raw) if raw else {}


def _access_token() -> str | None:
    """Get a valid access token, refreshing via the refresh token if needed."""
    now = time.time()
    if _ACCESS["token"] and now < _ACCESS["expires"] - 30:
        return _ACCESS["token"]
    cid, secret, refresh = _creds()
    if not (cid and secret and refresh):
        return None
    try:
        auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        body = urllib.parse.urlencode({
            "grant_type": "refresh_token", "refresh_token": refresh}).encode()
        d = _http(_TOKEN_URL, data=body, method="POST", headers={
            "Authorization": f"Basic {auth}",
            "Content-Type": "application/x-www-form-urlencoded"})
        _ACCESS["token"] = d.get("access_token", "")
        _ACCESS["expires"] = now + d.get("expires_in", 3600)
        return _ACCESS["token"] or None
    except Exception as e:
        print(f"[Spotify] token refresh failed: {e}")
        return None


def _api_headers(token):
    return {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _active_device(token) -> str | None:
    try:
        d = _http(f"{_API}/me/player/devices", headers=_api_headers(token))
        devs = d.get("devices", [])
        active = next((x for x in devs if x.get("is_active")), None)
        return (active or (devs[0] if devs else {})).get("id")
    except Exception:
        return None


def _play_via_api(query: str) -> str | None:
    """Search + play a track through the Web API. Returns a message, or None to
    signal the API path is unavailable (fall back to UI automation)."""
    token = _access_token()
    if not token:
        return None                       # not configured → caller falls back

    # Search for the track.
    try:
        q = urllib.parse.urlencode({"q": query, "type": "track", "limit": 1})
        res = _http(f"{_API}/search?{q}", headers=_api_headers(token))
        items = res.get("tracks", {}).get("items", [])
        if not items:
            return f"Sir, I couldn't find '{query}' on Spotify."
        track = items[0]
        uri = track["uri"]
        name = track["name"]
        artist = ", ".join(a["name"] for a in track.get("artists", []))
    except Exception as e:
        print(f"[Spotify] search failed: {e}")
        return None

    # Find a device to play on.
    device = _active_device(token)
    if not device:
        # Try to open the desktop app so a device appears, then retry once.
        try:
            from actions.open_app import open_app
            open_app(parameters={"app_name": "Spotify"})
            time.sleep(3)
            device = _active_device(token)
        except Exception:
            pass
    if not device:
        return ("Sir, no active Spotify device. Open Spotify on this PC or your "
                "phone, then ask again.")

    # Start playback.
    try:
        body = json.dumps({"uris": [uri]}).encode()
        _http(f"{_API}/me/player/play?device_id={device}",
              data=body, method="PUT", headers=_api_headers(token))
        return f"Playing '{name}' by {artist} on Spotify."
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return ("Sir, Spotify playback control needs a Premium account.")
        return f"Sir, I couldn't start playback: {e}"
    except Exception as e:
        return f"Sir, I couldn't start playback: {e}"


def _play_via_ui(query: str) -> str:
    """Fallback: drive the Spotify desktop app — open, search, play top result."""
    try:
        import pyautogui
        import pyperclip
    except Exception:
        return "Sir, I can't control Spotify without pyautogui installed."
    try:
        from actions.open_app import open_app
        open_app(parameters={"app_name": "Spotify"})
        time.sleep(3.0)
        # Focus search (Ctrl+K / Ctrl+L both used across versions), type, enter.
        pyautogui.hotkey("ctrl", "k")
        time.sleep(0.8)
        try:
            pyperclip.copy(query)
            pyautogui.hotkey("ctrl", "v")
        except Exception:
            pyautogui.write(query, interval=0.03)
        time.sleep(1.5)
        pyautogui.press("enter")           # open top result / search page
        time.sleep(1.5)
        # Play: Spotify's shortcut to play the focused/first item varies; the
        # most reliable is to press Enter on the first track after a Tab.
        pyautogui.press("enter")
        return (f"I searched Spotify for '{query}' and started the top result. "
                f"If it didn't play, press Play — or set up the Spotify API for "
                f"reliable playback.")
    except Exception as e:
        return f"Sir, I couldn't control Spotify: {e}"


def play_spotify(parameters: dict, player=None, session_memory=None) -> str:
    """Play a song/artist/playlist on Spotify by name."""
    p = parameters or {}
    query = (p.get("query") or p.get("song") or p.get("track")
             or p.get("name") or "").strip()
    if not query:
        return "Sir, what song should I play?"

    msg = _play_via_api(query)
    if msg is None:                        # API not configured → UI fallback
        msg = _play_via_ui(query)

    print(f"[Spotify] {msg}")
    if player:
        try:
            player.write_log(f"[spotify] {msg}")
        except Exception:
            pass
    return msg
