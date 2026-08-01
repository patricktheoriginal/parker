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


def _play_liked_via_api(shuffle: bool = True) -> str | None:
    """Play the user's Liked Songs. The Web API has no context URI for the
    Liked-Songs collection, so we fetch the saved tracks and play their URIs.
    Returns a message, or None if the API path is unavailable (→ UI fallback)."""
    token = _access_token()
    if not token:
        return None                       # not configured → caller falls back

    # Fetch saved (liked) tracks — up to a few hundred so a session has variety.
    uris = []
    try:
        url = f"{_API}/me/tracks?limit=50"
        for _ in range(4):                # up to 200 tracks
            res = _http(url, headers=_api_headers(token))
            for it in res.get("items", []):
                tr = it.get("track") or {}
                if tr.get("uri"):
                    uris.append(tr["uri"])
            url = res.get("next")
            if not url:
                break
    except Exception as e:
        print(f"[Spotify] fetch liked failed: {e}")
        return None
    if not uris:
        return "Sir, you have no Liked Songs on Spotify yet."

    if shuffle:
        import random
        random.shuffle(uris)

    # Find a device to play on (open the app if needed).
    device = _active_device(token)
    if not device:
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

    # Turn on shuffle on the player too (best-effort), then play the URIs.
    try:
        if shuffle:
            try:
                _http(f"{_API}/me/player/shuffle?state=true&device_id={device}",
                      method="PUT", headers=_api_headers(token))
            except Exception:
                pass
        # The play endpoint accepts at most 100 URIs at a time.
        body = json.dumps({"uris": uris[:100]}).encode()
        _http(f"{_API}/me/player/play?device_id={device}",
              data=body, method="PUT", headers=_api_headers(token))
        return f"Playing your Liked Songs on Spotify ({len(uris)} tracks, shuffled)."
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return "Sir, Spotify playback control needs a Premium account."
        return f"Sir, I couldn't start playback: {e}"
    except Exception as e:
        return f"Sir, I couldn't start playback: {e}"


def _play_liked_via_ui() -> str:
    """Fallback: open Spotify and navigate to Liked Songs, then play."""
    try:
        import pyautogui
    except Exception:
        return "Sir, I can't control Spotify without pyautogui installed."
    try:
        from actions.open_app import open_app
        open_app(parameters={"app_name": "Spotify"})
        time.sleep(3.0)
        # Ctrl+Shift+K jumps to the Liked Songs page in the desktop app.
        pyautogui.hotkey("ctrl", "shift", "k")
        time.sleep(1.5)
        pyautogui.press("enter")           # play the list
        return ("I opened your Liked Songs on Spotify and started playback. "
                "If it didn't play, press Play — or set up the Spotify API for "
                "reliable control.")
    except Exception as e:
        return f"Sir, I couldn't control Spotify: {e}"


def play_favorites(parameters: dict = None, player=None, session_memory=None) -> str:
    """Play the user's Liked Songs (favorites) on Spotify, shuffled."""
    msg = _play_liked_via_api(shuffle=True)
    if msg is None:                        # API not configured → UI fallback
        msg = _play_liked_via_ui()

    print(f"[Spotify] {msg}")
    if player:
        try:
            player.write_log(f"[spotify] {msg}")
        except Exception:
            pass
    return msg


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


# Remembers the last listed playlists so "play the second one" works after
# "list my playlists". Each entry: {"name","uri","id"}.
_LAST_PLAYLISTS: list[dict] = []

# Spoken ordinals → 1-based index, for "play the first/second one".
_ORDINALS = {
    "first": 1, "1st": 1, "one": 1,
    "second": 2, "2nd": 2, "two": 2,
    "third": 3, "3rd": 3, "three": 3,
    "fourth": 4, "4th": 4, "four": 4,
    "fifth": 5, "5th": 5, "five": 5,
    "sixth": 6, "6th": 6, "six": 6,
    "seventh": 7, "7th": 7, "seven": 7,
    "eighth": 8, "8th": 8, "eight": 8,
    "ninth": 9, "9th": 9, "nine": 9,
    "tenth": 10, "10th": 10, "ten": 10,
    "last": -1,
}


def _fetch_playlists(token) -> list[dict]:
    """Return the user's playlists as [{name, uri, id}] in Spotify's own order —
    the same order shown under 'All' when the app opens."""
    out = []
    try:
        url = f"{_API}/me/playlists?limit=50"
        for _ in range(4):                # up to 200 playlists
            res = _http(url, headers=_api_headers(token))
            for it in res.get("items", []):
                if it and it.get("uri"):
                    out.append({"name": it.get("name", "Untitled"),
                                "uri": it["uri"], "id": it.get("id", "")})
            url = res.get("next")
            if not url:
                break
    except Exception as e:
        print(f"[Spotify] fetch playlists failed: {e}")
    return out


def list_playlists(parameters: dict = None, player=None, session_memory=None) -> str:
    """List the user's Spotify playlists (as shown under 'All'), numbered, and
    write each to the Activity Log. Remembers the order so the user can then say
    'play the first one' or 'play <name>'."""
    global _LAST_PLAYLISTS
    token = _access_token()
    if not token:
        return ("Sir, listing playlists needs the Spotify Web API configured "
                "(client id/secret/refresh token in config/api_keys.json).")
    pls = _fetch_playlists(token)
    if not pls:
        return "Sir, I found no playlists on your Spotify account."
    _LAST_PLAYLISTS = pls

    _log(player, f"SYS: 🎵 {len(pls)} Spotify playlist(s):")
    for i, pl in enumerate(pls, 1):
        _log(player, f"  {i}. {pl['name']}")

    # Read the first ~10 names back so Parker can speak them.
    top = pls[:10]
    spoken = "; ".join(f"{i}. {pl['name']}" for i, pl in enumerate(top, 1))
    more = f" …and {len(pls) - 10} more" if len(pls) > 10 else ""
    return (f"You have {len(pls)} playlists. Here they are: {spoken}{more}. "
            f"Say 'play the first one', 'play number 3', or the playlist name.")


def _resolve_playlist(selector: str) -> dict | None:
    """Map a selector ('second', '3', or a name) to a remembered playlist."""
    sel = (selector or "").strip().lower()
    if not sel or not _LAST_PLAYLISTS:
        return None
    # Ordinal word or plain number.
    idx = None
    sel_clean = sel.replace("the ", "").replace("number ", "").replace("#", "").strip()
    if sel_clean in _ORDINALS:
        idx = _ORDINALS[sel_clean]
    elif sel_clean.isdigit():
        idx = int(sel_clean)
    if idx is not None:
        if idx == -1:
            return _LAST_PLAYLISTS[-1]
        if 1 <= idx <= len(_LAST_PLAYLISTS):
            return _LAST_PLAYLISTS[idx - 1]
        return None
    # Otherwise match by name (exact, then substring).
    for pl in _LAST_PLAYLISTS:
        if pl["name"].lower() == sel:
            return pl
    for pl in _LAST_PLAYLISTS:
        if sel in pl["name"].lower():
            return pl
    return None


def _play_context(token, context_uri: str) -> str:
    """Start playback of a playlist/album context on the active device."""
    device = _active_device(token)
    if not device:
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
    try:
        body = json.dumps({"context_uri": context_uri}).encode()
        _http(f"{_API}/me/player/play?device_id={device}",
              data=body, method="PUT", headers=_api_headers(token))
        return ""                          # empty = success (caller adds name)
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return "Sir, Spotify playback control needs a Premium account."
        return f"Sir, I couldn't start playback: {e}"
    except Exception as e:
        return f"Sir, I couldn't start playback: {e}"


def play_playlist(parameters: dict = None, player=None, session_memory=None) -> str:
    """Play a playlist chosen by name or by position ('the second one'). Lists
    playlists first if none have been listed yet this session."""
    global _LAST_PLAYLISTS
    p = parameters or {}
    selector = (p.get("selector") or p.get("name") or p.get("query")
                or p.get("index") or "").strip()
    if not selector:
        return "Sir, which playlist? Say a name, or 'the first one'."

    token = _access_token()
    if not token:
        return ("Sir, playing a playlist by name needs the Spotify Web API "
                "configured in config/api_keys.json.")

    # Populate the list on demand so 'play the second one' works even before an
    # explicit 'list playlists'.
    if not _LAST_PLAYLISTS:
        _LAST_PLAYLISTS = _fetch_playlists(token)
    if not _LAST_PLAYLISTS:
        return "Sir, I found no playlists on your Spotify account."

    pl = _resolve_playlist(selector)
    if not pl:
        return (f"Sir, I couldn't find a playlist matching '{selector}'. "
                f"Ask me to list your playlists first.")

    err = _play_context(token, pl["uri"])
    msg = err if err else f"Playing your playlist '{pl['name']}' on Spotify."
    print(f"[Spotify] {msg}")
    _log(player, f"[spotify] {msg}")
    return msg


def _log(player, msg: str):
    if player:
        try:
            player.write_log(msg)
        except Exception:
            pass


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
