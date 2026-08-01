"""
spotify_auth.py — one-time Spotify authorization to get a refresh token.

Do this once (needs a Spotify PREMIUM account for playback):

1. Go to https://developer.spotify.com/dashboard → Create app → choose "Web API".
2. In the app settings, add this Redirect URI EXACTLY:
       http://127.0.0.1:8888/callback
3. Copy the app's Client ID and Client Secret.
4. Run this script and paste them when asked:
       python tools/spotify_auth.py
5. It opens your browser to log in / authorize. After you approve, it prints a
   refresh_token. Put these three into config/api_keys.json:
       "spotify_client_id":     "...",
       "spotify_client_secret": "...",
       "spotify_refresh_token": "..."

Then Parker can search and play songs on Spotify.
"""
import base64
import json
import sys
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, HTTPServer

REDIRECT = "http://127.0.0.1:8888/callback"
SCOPES = "user-read-playback-state user-modify-playback-state"
_code = {"value": None}


class _Handler(BaseHTTPRequestHandler):
    def do_GET(self):
        q = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(q.query)
        _code["value"] = (params.get("code") or [None])[0]
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        self.wfile.write("<h2>Spotify authorized. You can close this tab.</h2>"
                         .encode())

    def log_message(self, *a):
        pass


def main() -> None:
    print("=== Spotify authorization ===")
    cid = input("Client ID: ").strip()
    secret = input("Client Secret: ").strip()
    if not cid or not secret:
        print("Client ID and Secret are required.")
        return

    auth_url = "https://accounts.spotify.com/authorize?" + urllib.parse.urlencode({
        "client_id": cid, "response_type": "code", "redirect_uri": REDIRECT,
        "scope": SCOPES})
    print("\nOpening your browser to authorize… (log in and click Agree)")
    webbrowser.open(auth_url)

    server = HTTPServer(("127.0.0.1", 8888), _Handler)
    print("Waiting for authorization on http://127.0.0.1:8888/callback …")
    while _code["value"] is None:
        server.handle_request()

    # Exchange the code for tokens.
    token_body = urllib.parse.urlencode({
        "grant_type": "authorization_code", "code": _code["value"],
        "redirect_uri": REDIRECT}).encode()
    auth = base64.b64encode(f"{cid}:{secret}".encode()).decode()
    req = urllib.request.Request(
        "https://accounts.spotify.com/api/token", data=token_body, method="POST",
        headers={"Authorization": f"Basic {auth}",
                 "Content-Type": "application/x-www-form-urlencoded"})
    try:
        d = json.load(urllib.request.urlopen(req, timeout=15))
    except Exception as e:
        print(f"Token exchange failed: {e}")
        return

    refresh = d.get("refresh_token")
    if not refresh:
        print("No refresh token returned:", d)
        return

    print("\n=== SUCCESS — add these to config/api_keys.json ===")
    print(json.dumps({
        "spotify_client_id": cid,
        "spotify_client_secret": secret,
        "spotify_refresh_token": refresh,
    }, indent=4))


if __name__ == "__main__":
    main()
