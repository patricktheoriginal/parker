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


def _list_devices(token) -> list:
    try:
        d = _http(f"{_API}/me/player/devices", headers=_api_headers(token))
        return d.get("devices", []) or []
    except Exception:
        return []


def _active_device(token) -> str | None:
    """A device id to play on, PREFERRING a genuinely active one. Falls back to
    any known device (Spotify remembers offline ones), so callers that need a
    live device should verify playback afterwards with _verify_playing()."""
    devs = _list_devices(token)
    active = next((x for x in devs if x.get("is_active")), None)
    return (active or (devs[0] if devs else {})).get("id")


def _has_active_device(token) -> bool:
    """True only if Spotify reports a device that is actually active/live."""
    return any(x.get("is_active") for x in _list_devices(token))


def _ensure_device(token) -> str | None:
    """Return a device id we can play on. A freshly-opened Spotify shows up in
    the device list but with is_active=false — that's fine: passing its
    device_id to the play endpoint activates it. So we only need a device to
    EXIST, not to already be active. Opens the app and waits for one to appear."""
    devs = _list_devices(token)
    if not devs:
        try:
            from actions.open_app import open_app
            open_app(parameters={"app_name": "Spotify"})
        except Exception:
            pass
        for _ in range(12):               # wait up to ~12s for a device to appear
            time.sleep(1)
            devs = _list_devices(token)
            if devs:
                break
    if not devs:
        print("[Spotify] no devices found at all")
        return None
    active = next((x for x in devs if x.get("is_active")), None)
    chosen = active or devs[0]
    print(f"[Spotify] devices={[d.get('name') for d in devs]} "
          f"chosen={chosen.get('name')} active={chosen.get('is_active')}")
    return chosen.get("id")


def _wake_device(token, device_id: str) -> None:
    """Transfer playback to (and thus activate) a specific device without
    starting a song, so a subsequent play targets a live device."""
    try:
        body = json.dumps({"device_ids": [device_id], "play": False}).encode()
        _http(f"{_API}/me/player", data=body, method="PUT",
              headers=_api_headers(token))
        time.sleep(1.0)
    except Exception as e:
        print(f"[Spotify] wake device failed (non-fatal): {e}")


def _verify_playing(token) -> bool:
    """Confirm playback actually started: /me/player must report is_playing.
    Spotify accepts a play request for an offline/stale device without error,
    so this guards against falsely reporting success."""
    for _ in range(4):
        time.sleep(0.8)
        try:
            d = _http(f"{_API}/me/player", headers=_api_headers(token))
        except Exception:
            d = {}
        if d and d.get("is_playing"):
            return True
    return False


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

    device = _ensure_device(token)
    if not device:
        return ("Sir, Spotify isn't running anywhere I can see. Open the Spotify "
                "app on this PC or your phone, then ask again.")
    _wake_device(token, device)

    # Start playback.
    try:
        body = json.dumps({"uris": [uri]}).encode()
        _http(f"{_API}/me/player/play?device_id={device}",
              data=body, method="PUT", headers=_api_headers(token))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return ("Sir, Spotify playback control needs a Premium account.")
        if e.code == 404:
            return ("Sir, Spotify has no active device. Open it and play any "
                    "song once, then ask again.")
        return f"Sir, I couldn't start playback: {e}"
    except Exception as e:
        return f"Sir, I couldn't start playback: {e}"

    if not _verify_playing(token):
        return ("Sir, I sent the play command but Spotify didn't start. Open "
                "Spotify, play any song once to wake the device, then ask again.")
    return f"Playing '{name}' by {artist} on Spotify."


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

    # Get a device to play on (opening Spotify if needed). We don't require it
    # to be already active — passing device_id to /play activates it.
    device = _ensure_device(token)
    if not device:
        return ("Sir, Spotify isn't running anywhere I can see. Open the Spotify "
                "app on this PC or your phone, then ask again.")
    _wake_device(token, device)           # transfer/activate the device first

    print(f"[Spotify] playing {len(uris[:100])} liked tracks on device {device}")
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
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "ignore")[:200]
        except Exception:
            pass
        print(f"[Spotify] play HTTP {e.code}: {detail}")
        if e.code == 403:
            return ("Sir, Spotify refused playback — this needs a Premium "
                    f"account. ({detail})" if detail else
                    "Sir, Spotify playback control needs a Premium account.")
        if e.code == 404:
            return ("Sir, Spotify has no active device to play on. Open Spotify "
                    "and play any song once, then ask again.")
        return f"Sir, I couldn't start playback: {e} {detail}"
    except Exception as e:
        return f"Sir, I couldn't start playback: {e}"

    # Confirm it really started before claiming success.
    if not _verify_playing(token):
        return ("Sir, I sent the play command but Spotify didn't start. Open "
                "Spotify, play any song once to wake the device, then ask again.")
    return f"Playing your Liked Songs on Spotify ({len(uris)} tracks, shuffled)."


def _open_spotify_uri(uri: str) -> bool:
    """Open a spotify: URI so the desktop app navigates straight to it. This is
    the reliable way to reach Liked Songs on a Free account (no shortcut for it).
    Returns True if we issued the open command."""
    import platform as _pf
    system = _pf.system()
    try:
        if system == "Windows":
            _subprocess_run(f'start "" "{uri}"', shell=True)
        elif system == "Darwin":
            _subprocess_run(["open", uri])
        else:
            _subprocess_run(["xdg-open", uri])
        return True
    except Exception as e:
        print(f"[Spotify] open URI failed: {e}")
        return False


def _subprocess_run(cmd, shell=False):
    import subprocess
    subprocess.Popen(cmd, shell=shell,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _play_liked_via_ui(shuffle: bool = True) -> str:
    """Free-account path: open Liked Songs via its spotify: URI, then press the
    Play button. Works without Premium since it drives the desktop app."""
    try:
        import pyautogui
    except Exception:
        return ("Sir, I can't control Spotify without pyautogui installed "
                "(pip install pyautogui).")
    try:
        # Make sure the app is running, then navigate to Liked Songs by URI.
        from actions.open_app import open_app
        open_app(parameters={"app_name": "Spotify"})
        time.sleep(2.0)
        if not _open_spotify_uri("spotify:collection:tracks"):
            return "Sir, I couldn't open your Liked Songs."
        time.sleep(3.5)                    # let the page load

        # Bring Spotify to the foreground so keystrokes land on it.
        _focus_spotify()
        time.sleep(0.5)

        if shuffle:
            # Ctrl+S toggles shuffle in the desktop app. (Best-effort — if it
            # was already on this turns it off, but most users leave it on.)
            try:
                pyautogui.hotkey("ctrl", "s")
                time.sleep(0.3)
            except Exception:
                pass

        # Play the list: Tab into the track list, then Enter on the first row.
        # Enter on a focused track starts playback of the whole list from there.
        pyautogui.press("tab")
        time.sleep(0.4)
        pyautogui.press("enter")
        time.sleep(0.6)
        # Fallback: Spotify's global "play/pause" is Space when a context is
        # loaded but nothing is playing yet.
        pyautogui.press("space")

        return ("I opened your Liked Songs and started playback. If it didn't "
                "start, just press the green Play button — some Spotify builds "
                "block simulated keys on the very first play.")
    except Exception as e:
        return f"Sir, I couldn't control Spotify: {e}"


def _focus_spotify() -> None:
    """Best-effort: bring the Spotify window to the foreground (Windows)."""
    import platform as _pf
    if _pf.system() != "Windows":
        return
    try:
        import subprocess
        # Use PowerShell to activate the Spotify window.
        ps = ('$p=Get-Process Spotify -ErrorAction SilentlyContinue | '
              'Where-Object {$_.MainWindowTitle} | Select-Object -First 1; '
              'if($p){ (New-Object -ComObject WScript.Shell).AppActivate('
              '$p.Id) }')
        subprocess.Popen(["powershell", "-NoProfile", "-Command", ps],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    except Exception:
        pass


def play_favorites(parameters: dict = None, player=None, session_memory=None) -> str:
    """Play the user's Liked Songs (favorites) on Spotify, shuffled.

    Playback control via the Web API needs Premium, so we try it first but fall
    back to driving the desktop app (which works on Free accounts)."""
    msg = _play_liked_via_api(shuffle=True)
    # None → API not configured. A 403/Premium message → API can't control a
    # Free account. In both cases, drive the desktop app instead.
    if msg is None or "Premium" in (msg or ""):
        msg = _play_liked_via_ui(shuffle=True)

    print(f"[Spotify] {msg}")
    if player:
        try:
            player.write_log(f"[spotify] {msg}")
        except Exception:
            pass
    return msg


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
    """Start playback of a playlist/album context on a device.
    Returns "" on confirmed success, or an error message."""
    device = _ensure_device(token)
    if not device:
        return ("Sir, Spotify isn't running anywhere I can see. Open the Spotify "
                "app on this PC or your phone, then ask again.")
    _wake_device(token, device)
    try:
        body = json.dumps({"context_uri": context_uri}).encode()
        _http(f"{_API}/me/player/play?device_id={device}",
              data=body, method="PUT", headers=_api_headers(token))
    except urllib.error.HTTPError as e:
        if e.code == 403:
            return "Sir, Spotify playback control needs a Premium account."
        if e.code == 404:
            return ("Sir, Spotify has no active device. Open it and play any "
                    "song once, then ask again.")
        return f"Sir, I couldn't start playback: {e}"
    except Exception as e:
        return f"Sir, I couldn't start playback: {e}"

    if not _verify_playing(token):
        return ("Sir, I sent the play command but Spotify didn't start. Open "
                "Spotify, play any song once to wake the device, then ask again.")
    return ""                              # empty = confirmed success


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
    if err and "Premium" in err:
        # Free account: open the playlist by URI in the app and press play.
        msg = _play_playlist_via_ui(pl)
    else:
        msg = err if err else f"Playing your playlist '{pl['name']}' on Spotify."
    print(f"[Spotify] {msg}")
    _log(player, f"[spotify] {msg}")
    return msg


def _play_playlist_via_ui(pl: dict) -> str:
    """Free-account path: open a playlist by its spotify: URI and press play."""
    try:
        import pyautogui
    except Exception:
        return "Sir, I can't control Spotify without pyautogui installed."
    try:
        from actions.open_app import open_app
        open_app(parameters={"app_name": "Spotify"})
        time.sleep(2.0)
        if not _open_spotify_uri(pl["uri"]):
            return f"Sir, I couldn't open the playlist '{pl['name']}'."
        time.sleep(3.5)
        _focus_spotify()
        time.sleep(0.5)
        pyautogui.press("tab")
        time.sleep(0.4)
        pyautogui.press("enter")
        time.sleep(0.6)
        pyautogui.press("space")
        return (f"I opened your playlist '{pl['name']}' and started playback. "
                f"If it didn't start, press the green Play button.")
    except Exception as e:
        return f"Sir, I couldn't control Spotify: {e}"


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
    # None → API not configured; "Premium" → Free account can't use API control.
    if msg is None or "Premium" in (msg or ""):
        msg = _play_via_ui(query)

    print(f"[Spotify] {msg}")
    if player:
        try:
            player.write_log(f"[spotify] {msg}")
        except Exception:
            pass
    return msg
