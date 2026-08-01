"""
agent.py — Parker Remote Agent (run this on the macOS/Windows machine you want
Parker to reach).

It's a small local HTTP server, protected by a token (an AnyDesk-style
ID + password combined into a bearer token). Parker connects to it to:
  - browse / search / fetch files (so you can grab a file you left on this Mac)
  - read system status
  - run a shell command  (FULL CONTROL — see the security note)

⚠️  SECURITY — READ THIS:
  This agent, when exposed to the internet, is effectively remote control of
  this machine. Anyone with the token can read your files and run commands.
    • Use a LONG, random password (the agent refuses short ones).
    • Prefer LAN. To reach it over the internet, put it behind an authenticated
      tunnel (e.g. `cloudflared`/`ngrok`) rather than opening a router port.
    • Command execution is OFF by default — enable it explicitly.

Run:
    python remote_agent/agent.py            # prompts to create a password
    python remote_agent/agent.py --allow-exec   # also allow running commands
Then note the printed AGENT ID and PASSWORD and enter them in Parker.
"""

import argparse
import base64
import hashlib
import hmac
import json
import os
import platform
import secrets
import socket
import subprocess
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

CONFIG = Path.home() / ".parker_agent.json"
_STATE = {"token_hash": "", "allow_exec": False, "roots": []}


# ── Auth ─────────────────────────────────────────────────────────────────────
def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _load_or_create() -> None:
    if CONFIG.exists():
        try:
            data = json.loads(CONFIG.read_text())
            _STATE.update(data)
            return
        except Exception:
            pass
    # First run — create a password.
    print("=== Parker Remote Agent — first-time setup ===")
    agent_id = secrets.token_hex(3).upper()          # short human ID
    print(f"Agent ID: {agent_id}")
    pw = ""
    while len(pw) < 10:
        pw = input("Set a password (10+ chars, make it strong): ").strip()
        if len(pw) < 10:
            print("Too short — use at least 10 characters.")
    token = f"{agent_id}:{pw}"
    _STATE["token_hash"] = _hash_token(token)
    _STATE["agent_id"] = agent_id
    # Default allowed roots: home directory.
    _STATE["roots"] = [str(Path.home())]
    CONFIG.write_text(json.dumps(_STATE, indent=2))
    print(f"\nSaved to {CONFIG}")
    print("Give Parker these two:")
    print(f"    AGENT ID:  {agent_id}")
    print(f"    PASSWORD:  {pw}")


def _check_auth(headers) -> bool:
    tok = (headers.get("Authorization", "") or "").removeprefix("Bearer ").strip()
    if not tok or not _STATE.get("token_hash"):
        return False
    return hmac.compare_digest(_hash_token(tok), _STATE["token_hash"])


# ── File access (restricted to allowed roots) ────────────────────────────────
def _within_roots(p: Path) -> bool:
    rp = p.resolve()
    for root in _STATE.get("roots", []):
        try:
            rp.relative_to(Path(root).resolve())
            return True
        except ValueError:
            continue
    return False


def _resolve_path(path: str) -> Path:
    """Turn a user/LLM-supplied path into an absolute one under home.

    Handles: '' → home; '~/x' → home/x; absolute paths as-is; and relative
    paths like 'desktop', 'Documents/report.docx', or a bare name → home/…
    (case-insensitive for the common home folders)."""
    if not path:
        return Path.home()
    p = path.strip()
    if p.startswith("~"):
        return Path(p).expanduser()
    pp = Path(p)
    if pp.is_absolute():
        return pp
    # Relative → under home. Fix common case differences (desktop → Desktop).
    parts = pp.parts
    _home_folders = {"desktop": "Desktop", "documents": "Documents",
                     "downloads": "Downloads", "pictures": "Pictures",
                     "movies": "Movies", "music": "Music", "public": "Public",
                     "library": "Library"}
    if parts and parts[0].lower() in _home_folders:
        parts = (_home_folders[parts[0].lower()],) + parts[1:]
    return Path.home().joinpath(*parts)


def _list_dir(path: str) -> dict:
    p = _resolve_path(path)
    if not _within_roots(p) or not p.is_dir():
        return {"error": "not allowed or not a directory"}
    items = []
    for child in sorted(p.iterdir()):
        try:
            items.append({"name": child.name, "dir": child.is_dir(),
                          "size": child.stat().st_size if child.is_file() else 0})
        except Exception:
            continue
    return {"path": str(p), "items": items[:500]}


def _search(root: str, query: str) -> dict:
    base = _resolve_path(root)
    if not _within_roots(base):
        return {"error": "not allowed"}
    q = query.lower()
    hits = []
    for dirpath, _dirs, files in os.walk(base):
        for f in files:
            if q in f.lower():
                fp = Path(dirpath) / f
                hits.append(str(fp))
                if len(hits) >= 50:
                    return {"matches": hits}
    return {"matches": hits}


_MAX_TRANSFER = 500 * 1024 * 1024   # 500 MB cap for a single fetch


def _get_file(path: str) -> dict:
    """Fetch ANY file (image, zip/rar, pdf, docx, xlsx, …) as raw bytes, or a
    whole FOLDER zipped on the fly. Returns base64 of the bytes."""
    p = _resolve_path(path)
    if not _within_roots(p):
        return {"error": "not allowed"}

    # Folder → zip it in memory and return the zip.
    if p.is_dir():
        import io
        import zipfile
        buf = io.BytesIO()
        total = 0
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
            for f in p.rglob("*"):
                if not f.is_file():
                    continue
                try:
                    total += f.stat().st_size
                    if total > _MAX_TRANSFER:
                        return {"error": f"folder too large (> {_MAX_TRANSFER // (1024*1024)} MB)"}
                    zf.write(f, f.relative_to(p.parent))
                except Exception:
                    continue
        raw = buf.getvalue()
        return {"name": p.name + ".zip", "size": len(raw), "kind": "folder-zip",
                "b64": base64.b64encode(raw).decode()}

    if not p.is_file():
        return {"error": "not allowed or not a file"}
    if p.stat().st_size > _MAX_TRANSFER:
        return {"error": f"file too large (> {_MAX_TRANSFER // (1024*1024)} MB)"}
    # Works for any file type — images, archives, documents — it's raw bytes.
    data = base64.b64encode(p.read_bytes()).decode()
    return {"name": p.name, "size": p.stat().st_size, "kind": "file",
            "b64": data}


def _system_status() -> dict:
    try:
        import psutil
        return {"os": platform.platform(),
                "cpu_percent": psutil.cpu_percent(interval=0.3),
                "ram_percent": psutil.virtual_memory().percent,
                "hostname": socket.gethostname()}
    except Exception:
        return {"os": platform.platform(), "hostname": socket.gethostname()}


def _run_command(cmd: str) -> dict:
    if not _STATE.get("allow_exec"):
        return {"error": "command execution is disabled on this agent "
                         "(start it with --allow-exec to enable)."}
    try:
        r = subprocess.run(cmd, shell=True, capture_output=True, text=True,
                           timeout=30)
        return {"returncode": r.returncode,
                "stdout": (r.stdout or "")[:4000],
                "stderr": (r.stderr or "")[:2000]}
    except Exception as e:
        return {"error": str(e)}


class _Handler(BaseHTTPRequestHandler):
    def _send(self, obj, code=200):
        body = json.dumps(obj).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        if self.path != "/rpc":
            return self._send({"error": "not found"}, 404)
        if not _check_auth(self.headers):
            return self._send({"error": "unauthorized"}, 401)
        try:
            length = int(self.headers.get("Content-Length", 0))
            req = json.loads(self.rfile.read(length) or b"{}")
        except Exception:
            return self._send({"error": "bad request"}, 400)

        action = req.get("action")
        a = req.get("args", {})
        if action == "ping":       return self._send({"ok": True, "id": _STATE.get("agent_id")})
        if action == "list":       return self._send(_list_dir(a.get("path", "")))
        if action == "search":     return self._send(_search(a.get("root", ""), a.get("query", "")))
        if action == "get":        return self._send(_get_file(a.get("path", "")))
        if action == "status":     return self._send(_system_status())
        if action == "exec":       return self._send(_run_command(a.get("cmd", "")))
        return self._send({"error": f"unknown action: {action}"}, 400)

    def log_message(self, *a):
        pass


def _lan_ip() -> str:
    """Best-effort LAN IP without a reverse-DNS lookup (which fails on macOS
    '.local' hostnames)."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))     # doesn't send anything; just picks the iface
        return s.getsockname()[0]
    except Exception:
        return "127.0.0.1"
    finally:
        s.close()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8770)
    ap.add_argument("--allow-exec", action="store_true",
                    help="allow Parker to run shell commands (full control)")
    ap.add_argument("--reset", action="store_true", help="recreate the password")
    args = ap.parse_args()

    if args.reset and CONFIG.exists():
        CONFIG.unlink()
    _load_or_create()
    if args.allow_exec:
        _STATE["allow_exec"] = True
        CONFIG.write_text(json.dumps(_STATE, indent=2))

    ip = _lan_ip()
    print(f"\nParker Remote Agent listening on 0.0.0.0:{args.port}")
    print(f"  LAN address for Parker:  http://{ip}:{args.port}")
    print(f"  Command execution: {'ENABLED' if _STATE.get('allow_exec') else 'disabled'}")
    print("  For internet access, run a tunnel to this port (e.g. cloudflared).")
    ThreadingHTTPServer(("0.0.0.0", args.port), _Handler).serve_forever()


if __name__ == "__main__":
    main()
