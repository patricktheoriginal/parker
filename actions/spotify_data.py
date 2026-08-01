"""
spotify_data.py — read Spotify library data (playlists, liked songs) WITHOUT
Premium, via SpotAPI (an unofficial wrapper over Spotify's private web API).

Reads ONLY. Playback is still driven through the desktop app (see spotify.py),
because playback control needs Premium.

Setup:
    pip install spotapi
    python tools/spotify_cookies.py --manual   # save sp_dc + your email

config/spotify_cookies.json looks like:
    { "identifier": "you@email.com",
      "cookies": { "sp_dc": "...", "sp_key": "..." } }

Everything fails soft: if SpotAPI isn't installed or the session is
missing/expired, functions return (None, reason) and the caller falls back.

⚠️ SpotAPI is unofficial and may violate Spotify's Terms of Service.
"""

import json
from pathlib import Path

_COOKIES = Path(__file__).resolve().parent.parent / "config" / "spotify_cookies.json"

# Cache the logged-in Login object so we don't rebuild it every call.
_LOGIN = {"obj": None}


def _load_session() -> tuple[str | None, dict | None]:
    """Return (identifier, cookies_dict) from the saved file, or (None, None)."""
    try:
        data = json.loads(_COOKIES.read_text())
    except Exception:
        return None, None
    cookies = data.get("cookies") or {}
    identifier = (data.get("identifier") or "").strip()
    if not cookies.get("sp_dc"):
        return None, None
    return (identifier or None), cookies


def _login():
    """Build (and cache) a logged-in SpotAPI Login from the saved cookies.
    Raises RuntimeError with a clear message on any problem."""
    if _LOGIN["obj"] is not None:
        return _LOGIN["obj"]

    identifier, cookies = _load_session()
    if not cookies:
        raise RuntimeError(
            "no Spotify session — run: python tools/spotify_cookies.py --manual")
    if not identifier:
        raise RuntimeError(
            "missing your Spotify email — re-run tools/spotify_cookies.py "
            "--manual and enter your account email when asked")

    try:
        from spotapi import Login, Config  # type: ignore
    except Exception:
        raise RuntimeError("SpotAPI not installed — run: pip install spotapi")

    # A no-op logger keeps SpotAPI quiet; no CAPTCHA solver needed for cookies.
    try:
        from spotapi import NoopLogger  # type: ignore
        cfg = Config(solver=None, logger=NoopLogger())
    except Exception:
        # Older/newer builds may not export NoopLogger the same way.
        try:
            from spotapi.utils.logger import NoopLogger  # type: ignore
            cfg = Config(solver=None, logger=NoopLogger())
        except Exception as e:
            raise RuntimeError(f"couldn't build SpotAPI Config: {e}")

    dump = {"identifier": identifier, "cookies": cookies}
    try:
        login = Login.from_cookies(dump, cfg)
    except Exception as e:
        raise RuntimeError(f"SpotAPI login from cookies failed: {e}")

    _LOGIN["obj"] = login
    return login


def _private_playlist():
    from spotapi import PrivatePlaylist  # type: ignore
    return PrivatePlaylist(_login())


def get_playlists() -> tuple[list | None, str]:
    """Return ([{name, uri, id}], "") or (None, reason)."""
    try:
        pp = _private_playlist()
    except Exception as e:
        return None, str(e)

    try:
        lib = pp.get_library(50)
    except TypeError:
        try:
            lib = pp.get_library()
        except Exception as e:
            return None, f"couldn't read playlists: {e}"
    except Exception as e:
        return None, f"couldn't read playlists: {e}"

    items = _library_items(lib)
    out = []
    for it in items:
        # Library items look like {"uri": "spotify:playlist:..", "name": ..} or
        # nest the playlist under "item"/"data".
        node = it.get("item") or it.get("data") or it
        name = node.get("name") or "Untitled"
        uri = node.get("uri") or ""
        pid = node.get("id") or (uri.split(":")[-1] if uri else "")
        if not uri and pid:
            uri = f"spotify:playlist:{pid}"
        # Only keep actual playlists (skip folders/other library entries).
        if "playlist" in uri or (uri == "" and pid):
            out.append({"name": name, "uri": uri, "id": pid})
    if not out and items:
        return None, "got a library response but found no playlists in it"
    return out, ""


def get_liked_songs(limit: int = 50) -> tuple[list | None, str]:
    """Return ([{name, artist, uri}], "") or (None, reason)."""
    try:
        pp = _private_playlist()
    except Exception as e:
        return None, str(e)

    tracks = []
    try:
        # Preferred: paginated generator of saved-track pages.
        gen = getattr(pp, "paginate_saved_tracks", None)
        if callable(gen):
            for page in gen():
                tracks.extend(_track_items(page))
                if len(tracks) >= limit:
                    break
        else:
            info = pp.get_saved_tracks_info(limit)
            tracks = _track_items(info)
    except Exception as e:
        return None, f"couldn't read liked songs: {e}"

    out = []
    for it in tracks[:limit]:
        node = it.get("item") or it.get("track") or it.get("data") or it
        name = node.get("name") or "Unknown"
        out.append({"name": name, "artist": _artist_str(node),
                    "uri": node.get("uri", "")})
    if not out:
        return None, "no liked songs found"
    return out, ""


# ── Response shape helpers (SpotAPI nests things under GraphQL-ish keys) ──────
def _library_items(lib) -> list:
    if lib is None:
        return []
    if isinstance(lib, list):
        return lib
    if isinstance(lib, dict):
        # Try common shapes: {"items": [...]}, or nested under data→me→library.
        if "items" in lib and isinstance(lib["items"], list):
            return lib["items"]
        data = lib.get("data") or {}
        for path in (("me", "libraryV3", "items"),
                     ("me", "library", "items"),
                     ("libraryV3", "items")):
            node = data
            ok = True
            for key in path:
                if isinstance(node, dict) and key in node:
                    node = node[key]
                else:
                    ok = False
                    break
            if ok and isinstance(node, list):
                return node
    return []


def _track_items(page) -> list:
    if page is None:
        return []
    if isinstance(page, list):
        return page
    if isinstance(page, dict):
        if "items" in page and isinstance(page["items"], list):
            return page["items"]
        data = page.get("data") or {}
        for path in (("me", "library", "tracks", "items"),
                     ("tracks", "items")):
            node = data
            ok = True
            for key in path:
                if isinstance(node, dict) and key in node:
                    node = node[key]
                else:
                    ok = False
                    break
            if ok and isinstance(node, list):
                return node
    return []


def _artist_str(node: dict) -> str:
    arts = node.get("artists")
    # Spotify GraphQL: artists = {"items": [{"profile": {"name": ..}}, ..]}
    if isinstance(arts, dict):
        arts = arts.get("items", [])
    names = []
    if isinstance(arts, list):
        for a in arts:
            if isinstance(a, dict):
                names.append(a.get("name")
                             or (a.get("profile") or {}).get("name", ""))
            elif isinstance(a, str):
                names.append(a)
    return ", ".join(n for n in names if n) or node.get("artist", "")
