"""
spotify_cookies.py — save your Spotify web session cookies for SpotAPI.

SpotAPI reads your playlists / liked songs WITHOUT Spotify Premium by reusing a
logged-in web session. This helper grabs the cookies from a browser you're
already logged into at https://open.spotify.com and writes them where Parker
expects them.

Usage:
    pip install browser-cookie3
    python tools/spotify_cookies.py                 # auto-detect from all browsers
    python tools/spotify_cookies.py --browser chrome

It writes:  config/spotify_cookies.json
Keep that file private (it's your session — treat it like a password). It's
already covered by .gitignore.

⚠️ SpotAPI emulates Spotify's private web API (unofficial). Using it may violate
Spotify's Terms of Service and could get the account limited. You accepted that.
"""

import argparse
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "config" / "spotify_cookies.json"

# Cookies that actually matter for a Spotify web session.
WANTED = {"sp_dc", "sp_key", "sp_t", "sp_gaid", "sp_landing", "sp_m"}


def _from_browser_cookie3(browser: str | None) -> dict:
    import browser_cookie3 as bc3
    loaders = {
        "chrome": bc3.chrome, "edge": bc3.edge, "firefox": bc3.firefox,
        "brave": bc3.brave, "opera": bc3.opera, "chromium": bc3.chromium,
    }
    jars = []
    if browser:
        fn = loaders.get(browser.lower())
        if not fn:
            print(f"Unknown browser '{browser}'. Options: {', '.join(loaders)}")
            sys.exit(1)
        jars = [fn(domain_name="spotify.com")]
    else:
        # Try every browser; keep whatever we can read.
        for name, fn in loaders.items():
            try:
                jars.append(fn(domain_name="spotify.com"))
            except Exception:
                continue

    cookies = {}
    for jar in jars:
        for c in jar:
            if "spotify.com" in (c.domain or ""):
                cookies[c.name] = c.value
    return cookies


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", help="chrome/edge/firefox/brave/opera/chromium")
    args = ap.parse_args()

    try:
        cookies = _from_browser_cookie3(args.browser)
    except ImportError:
        print("Need browser-cookie3:  pip install browser-cookie3")
        sys.exit(1)
    except Exception as e:
        print(f"Couldn't read browser cookies: {e}")
        print("Make sure you're logged into https://open.spotify.com in that "
              "browser, and close it before running this.")
        sys.exit(1)

    have = {k: v for k, v in cookies.items() if k in WANTED}
    if "sp_dc" not in have:
        print("Didn't find the key 'sp_dc' cookie. Log into "
              "https://open.spotify.com in your browser first, then re-run.")
        print(f"(Found cookies: {list(cookies) or 'none'})")
        sys.exit(1)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"cookies": cookies}, indent=2))
    print(f"✓ Saved {len(cookies)} Spotify cookies to {OUT}")
    print("You can now ask Parker to list your playlists / liked songs.")


if __name__ == "__main__":
    main()
