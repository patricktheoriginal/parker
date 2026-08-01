"""
spotify_cookies.py — save your Spotify web session cookie for SpotAPI.

SpotAPI reads your playlists / liked songs WITHOUT Spotify Premium by reusing a
logged-in web session. This helper stores the session cookie where Parker
expects it: config/spotify_cookies.json

Two ways to use it:

  1) Auto-read from your browser (needs browser-cookie3):
        pip install browser-cookie3
        python tools/spotify_cookies.py                  # try all browsers
        python tools/spotify_cookies.py --browser chrome # one browser

  2) Manual (most reliable — works even when browsers encrypt cookies):
        python tools/spotify_cookies.py --manual
     Then paste your 'sp_dc' cookie value. To find it:
        • Open https://open.spotify.com (logged in) in your browser
        • Press F12 → Application (Edge/Chrome) or Storage (Firefox)
        • Cookies → https://open.spotify.com → copy the VALUE of 'sp_dc'

Keep config/spotify_cookies.json private (it's your session — like a password).
It's already covered by .gitignore.

⚠️ SpotAPI emulates Spotify's private web API (unofficial). Using it may violate
Spotify's Terms of Service and could get the account limited. You accepted that.
"""

import argparse
import json
import sys
from pathlib import Path

OUT = Path(__file__).resolve().parent.parent / "config" / "spotify_cookies.json"

# Cookies that matter for a Spotify web session. sp_dc is the essential one.
WANTED = {"sp_dc", "sp_key", "sp_t", "sp_gaid", "sp_landing", "sp_m"}


def _from_browser_cookie3(browser: str | None) -> dict:
    """Read Spotify cookies from browsers. Reports per-browser failures instead
    of silently swallowing them (that's why you saw 'none')."""
    import browser_cookie3 as bc3
    loaders = {
        "chrome": bc3.chrome, "edge": bc3.edge, "firefox": bc3.firefox,
        "brave": bc3.brave, "opera": bc3.opera, "chromium": bc3.chromium,
    }

    if browser:
        fn = loaders.get(browser.lower())
        if not fn:
            print(f"Unknown browser '{browser}'. Options: {', '.join(loaders)}")
            sys.exit(1)
        targets = {browser.lower(): fn}
    else:
        targets = loaders

    cookies = {}
    any_ok = False
    for name, fn in targets.items():
        try:
            jar = fn(domain_name="spotify.com")
        except Exception as e:
            # Show WHY each browser failed (locked DB, app-bound encryption, etc.)
            print(f"  [{name}] couldn't read: {type(e).__name__}: {e}")
            continue
        found_here = 0
        for c in jar:
            if "spotify.com" in (c.domain or ""):
                cookies[c.name] = c.value
                found_here += 1
        if found_here:
            any_ok = True
            print(f"  [{name}] found {found_here} spotify.com cookie(s)")
        else:
            print(f"  [{name}] no spotify.com cookies (are you logged in there?)")

    if not any_ok:
        print("\nNo browser cookies could be read. On Windows, recent Chrome/Edge"
              "\nencrypt cookies (app-bound) and close-source tools can't decrypt"
              "\nthem. Use the manual method instead:\n"
              "    python tools/spotify_cookies.py --manual")
    return cookies


def _manual() -> tuple[dict, str]:
    """Ask the user to paste sp_dc (+ optionally sp_key) and their email.
    Returns (cookies_dict, identifier)."""
    print("Manual cookie entry.")
    print("Find it: open https://open.spotify.com (logged in) → F12 →")
    print("  Application/Storage → Cookies → https://open.spotify.com →")
    print("  copy the VALUE of the 'sp_dc' cookie, then paste below.\n")
    sp_dc = input("Paste sp_dc value: ").strip().strip('"').strip("'")
    if not sp_dc:
        print("Nothing pasted — aborting.")
        sys.exit(1)
    cookies = {"sp_dc": sp_dc}
    sp_key = input("Paste sp_key value (optional, press Enter to skip): ").strip()
    if sp_key:
        cookies["sp_key"] = sp_key.strip('"').strip("'")
    # SpotAPI needs an account identifier (your Spotify login email/username).
    identifier = input("Your Spotify account email (or username): ").strip()
    return cookies, identifier


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--browser", help="chrome/edge/firefox/brave/opera/chromium")
    ap.add_argument("--manual", action="store_true",
                    help="paste the sp_dc cookie by hand (most reliable)")
    args = ap.parse_args()

    identifier = ""
    if args.manual:
        cookies, identifier = _manual()
    else:
        try:
            print("Reading Spotify cookies from your browsers…")
            cookies = _from_browser_cookie3(args.browser)
        except ImportError:
            print("Need browser-cookie3:  pip install browser-cookie3")
            print("Or use the manual method:  python tools/spotify_cookies.py --manual")
            sys.exit(1)

    if "sp_dc" not in cookies:
        print("\nDidn't get the essential 'sp_dc' cookie.")
        print("Log into https://open.spotify.com first, or run with --manual.")
        print(f"(Cookies collected: {list(cookies) or 'none'})")
        sys.exit(1)

    # SpotAPI needs your account identifier (login email/username) alongside the
    # cookie. Ask for it if we don't have it yet.
    if not identifier:
        identifier = input("Your Spotify account email (or username): ").strip()
    if not identifier:
        print("An account email/username is required for SpotAPI. Aborting.")
        sys.exit(1)

    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps(
        {"identifier": identifier, "cookies": cookies}, indent=2))
    print(f"\n✓ Saved session ({identifier}, {len(cookies)} cookie(s)) to {OUT}")
    print("You can now ask Parker to list your playlists / liked songs.")


if __name__ == "__main__":
    main()
