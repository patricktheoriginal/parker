"""
spotify_data.py — read Spotify library data (playlists, liked songs) WITHOUT
Premium, via SpotAPI (an unofficial wrapper over Spotify's private web API).

This module only READS data. Actual playback is still driven through the
desktop app (see spotify.py), because playback control needs Premium.

Setup:
    pip install spotapi browser-cookie3
    python tools/spotify_cookies.py        # save your web-session cookies

Everything here fails soft: if SpotAPI isn't installed or the session is
missing/expired, the functions return (None, reason) and the caller falls back.

⚠️ SpotAPI is unofficial and may violate Spotify's Terms of Service.
"""

import json
from pathlib import Path

_COOKIES = Path(__file__).resolve().parent.parent / "config" / "spotify_cookies.json"

# Cache the logged-in client so we don't rebuild it every call.
_CLIENT = {"obj": None}


def _load_cookies() -> dict | None:
    try:
        data = json.loads(_COOKIES.read_text())
        c = data.get("cookies") or {}
        return c if c.get("sp_dc") else None
    except Exception:
        return None


def _client():
    """Return a logged-in SpotAPI client, or raise with a clear message."""
    if _CLIENT["obj"] is not None:
        return _CLIENT["obj"]

    cookies = _load_cookies()
    if not cookies:
        raise RuntimeError(
            "no Spotify session — run: python tools/spotify_cookies.py")

    try:
        import spotapi  # noqa: F401
    except Exception:
        raise RuntimeError("SpotAPI not installed — run: pip install spotapi")

    # SpotAPI's login surface has shifted across versions; try the known ways to
    # build an authenticated session from raw cookies.
    from spotapi import Login  # type: ignore
    obj = None
    errors = []

    # 1) Login.from_cookies({...})
    for attempt in ("from_cookies", "from_saver"):
        fn = getattr(Login, attempt, None)
        if fn is None:
            continue
        try:
            if attempt == "from_cookies":
                obj = fn(cookies)
            else:
                # Build a saver seeded with our cookies.
                try:
                    from spotapi import JSONSaver  # type: ignore
                    saver = JSONSaver(path=str(_COOKIES))
                    obj = fn(saver)
                except Exception as e:
                    errors.append(f"{attempt}: {e}")
                    continue
            break
        except Exception as e:
            errors.append(f"{attempt}: {e}")

    # 2) Fall back to constructing Login/session directly with cookies.
    if obj is None:
        try:
            obj = Login(cookies=cookies)  # type: ignore
        except Exception as e:
            errors.append(f"Login(cookies=): {e}")

    if obj is None:
        raise RuntimeError("SpotAPI login failed (" + "; ".join(errors) + ")")

    _CLIENT["obj"] = obj
    return obj


def _first_method(obj, names):
    for n in names:
        m = getattr(obj, n, None)
        if callable(m):
            return m
    return None


def get_playlists() -> tuple[list | None, str]:
    """Return ([{name, uri, id}], "") or (None, reason)."""
    try:
        client = _client()
    except Exception as e:
        return None, str(e)

    try:
        from spotapi import User  # type: ignore
        user = User(client)
        fn = _first_method(user, ("get_playlists", "playlists", "get_all_playlists"))
        if fn is None:
            return None, "SpotAPI User has no playlists method on this version"
        raw = fn()
        items = _extract_items(raw)
        out = []
        for it in items:
            name = it.get("name") or it.get("title") or "Untitled"
            uri = it.get("uri") or ""
            pid = it.get("id") or (uri.split(":")[-1] if uri else "")
            if not uri and pid:
                uri = f"spotify:playlist:{pid}"
            if uri or pid:
                out.append({"name": name, "uri": uri, "id": pid})
        return out, ""
    except Exception as e:
        return None, f"couldn't read playlists: {e}"


def get_liked_songs(limit: int = 50) -> tuple[list | None, str]:
    """Return ([{name, artist, uri}], "") or (None, reason)."""
    try:
        client = _client()
    except Exception as e:
        return None, str(e)

    try:
        from spotapi import User  # type: ignore
        user = User(client)
        fn = _first_method(user, ("get_liked_songs", "liked_songs",
                                  "get_saved_tracks", "saved_tracks"))
        if fn is None:
            return None, "SpotAPI User has no liked-songs method on this version"
        try:
            raw = fn(limit=limit)
        except TypeError:
            raw = fn()
        items = _extract_items(raw)
        out = []
        for it in items:
            tr = it.get("track") or it
            name = tr.get("name") or "Unknown"
            artist = _artist_str(tr)
            uri = tr.get("uri") or ""
            out.append({"name": name, "artist": artist, "uri": uri})
            if len(out) >= limit:
                break
        return out, ""
    except Exception as e:
        return None, f"couldn't read liked songs: {e}"


def _artist_str(track: dict) -> str:
    arts = track.get("artists") or []
    if isinstance(arts, list):
        names = []
        for a in arts:
            if isinstance(a, dict):
                names.append(a.get("name", ""))
            elif isinstance(a, str):
                names.append(a)
        return ", ".join(n for n in names if n)
    return track.get("artist", "")


def _extract_items(raw) -> list:
    """SpotAPI returns lists directly, dicts with 'items', or a generator that
    yields pages. Normalise to a flat list of dicts."""
    if raw is None:
        return []
    if isinstance(raw, dict):
        return raw.get("items") or raw.get("tracks") or raw.get("playlists") or []
    if isinstance(raw, list):
        return raw
    # Generator / iterator of pages or items.
    items = []
    try:
        for page in raw:
            if isinstance(page, dict) and ("items" in page or "tracks" in page):
                items.extend(page.get("items") or page.get("tracks") or [])
            elif isinstance(page, list):
                items.extend(page)
            else:
                items.append(page)
            if len(items) >= 500:
                break
    except Exception:
        pass
    return items
