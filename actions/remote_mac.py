"""
remote_mac.py — Parker client for a remote machine running the Parker Agent.

Lets Parker reach another computer (e.g. your Mac) that's running
remote_agent/agent.py: browse/search/fetch files, read status, and (if the
agent allows it) run commands.

Configure in config/api_keys.json:
    "remote_agent_url":   "http://192.168.1.187:8770",   (or a tunnel URL)
    "remote_agent_token": "AGENTID:password"

Fetched files are saved to ~/Downloads/ParkerRemote/ on THIS machine.
"""

import base64
import json
from pathlib import Path
from urllib.request import Request, urlopen

from memory.config_manager import load_api_keys


def _cfg():
    c = load_api_keys()
    return (c.get("remote_agent_url", "").rstrip("/"),
            c.get("remote_agent_token", "").strip())


def _rpc(action: str, args: dict | None = None, timeout: int = 20) -> dict:
    url, token = _cfg()
    if not url or not token:
        return {"error": "not_configured"}
    body = json.dumps({"action": action, "args": args or {}}).encode()
    # A browser-like User-Agent — Cloudflare returns 403 for the default
    # 'Python-urllib' agent, treating it as a bot.
    req = Request(f"{url}/rpc", data=body, method="POST", headers={
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Parker/1.0"})
    try:
        with urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "ignore"))
    except Exception as e:
        return {"error": str(e)}


def _not_configured() -> str:
    return ("Sir, the remote machine isn't set up. Run remote_agent/agent.py on "
            "that computer, then add 'remote_agent_url' and 'remote_agent_token' "
            "to config/api_keys.json.")


def remote_status(parameters: dict = None, player=None, session_memory=None) -> str:
    url, token = _cfg()
    if not url or not token:
        return _not_configured()
    d = _rpc("ping")
    if d.get("error"):
        return f"Sir, I can't reach the remote machine: {d['error']}"
    s = _rpc("status")
    if s.get("error"):
        return f"Connected to agent {d.get('id')}, but status failed: {s['error']}"
    return (f"Remote machine (agent {d.get('id')}): {s.get('os')}, "
            f"CPU {s.get('cpu_percent')}%, RAM {s.get('ram_percent')}%, "
            f"host {s.get('hostname')}.")


def remote_list(parameters: dict, player=None, session_memory=None) -> str:
    url, token = _cfg()
    if not url or not token:
        return _not_configured()
    path = (parameters or {}).get("path", "").strip()
    d = _rpc("list", {"path": path})
    if d.get("error"):
        return f"Sir, I couldn't list that folder: {d['error']}"
    items = d.get("items", [])
    where = d.get("path", path or "home")
    if not items:
        _log(player, f"SYS: {where} is empty.")
        return f"{where} is empty."

    # Folders first, then files — write each entry to the Activity Log.
    folders = [it for it in items if it["dir"]]
    files = [it for it in items if not it["dir"]]
    _log(player, f"SYS: 📂 {where}  —  {len(folders)} folder(s), {len(files)} file(s)")
    for it in folders:
        _log(player, f"  📁 {it['name']}/")
    for it in files:
        _log(player, f"  📄 {it['name']}  ({_fmt_size(it['size'])})")

    # Short summary back to the model (it doesn't need the whole dump).
    names = [f"{it['name']}/" if it["dir"] else it["name"] for it in items[:12]]
    more = f" and {len(items) - 12} more" if len(items) > 12 else ""
    return (f"{where} has {len(folders)} folder(s) and {len(files)} file(s). "
            f"Listed them in the activity log. Top items: " + ", ".join(names) + more + ".")


def remote_find(parameters: dict, player=None, session_memory=None) -> str:
    url, token = _cfg()
    if not url or not token:
        return _not_configured()
    p = parameters or {}
    query = (p.get("query") or p.get("name") or "").strip()
    root = (p.get("root") or "").strip()
    if not query:
        return "Sir, what filename should I search for on the remote machine?"
    d = _rpc("search", {"root": root, "query": query}, timeout=40)
    if d.get("error"):
        return f"Sir, the search failed: {d['error']}"
    matches = d.get("matches", [])
    if not matches:
        return f"Sir, I found no files matching '{query}' on the remote machine."
    # Write each match to the Activity Log.
    _log(player, f"SYS: 🔎 {len(matches)} match(es) for '{query}':")
    for m in matches[:40]:
        _log(player, f"  📄 {m}")
    top = matches[:8]
    more = f" and {len(matches) - 8} more" if len(matches) > 8 else ""
    return (f"Found {len(matches)} file(s) matching '{query}', listed in the "
            f"activity log. Examples: " + ", ".join(top) + more +
            ". Say 'get <path>' to fetch one.")


def _fmt_size(n: int) -> str:
    if n >= 1024 * 1024:
        return f"{n / (1024*1024):.1f} MB"
    if n >= 1024:
        return f"{n / 1024:.0f} KB"
    return f"{n} B"


def _log(player, msg: str):
    if player:
        try:
            player.write_log(msg)
        except Exception:
            pass


def remote_get(parameters: dict, player=None, session_memory=None) -> str:
    from urllib.parse import quote
    url, token = _cfg()
    if not url or not token:
        return _not_configured()
    path = (parameters or {}).get("path", "").strip()
    if not path:
        return "Sir, which file or folder should I fetch? (use 'find' first if unsure)."

    _log(player, f"SYS: Fetching '{path}' from the Mac…")
    dl_url = f"{url}/download?path={quote(path)}"
    req = Request(dl_url, method="GET", headers={
        "Authorization": f"Bearer {token}",
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) Parker/1.0"})

    try:
        resp = urlopen(req, timeout=300)
    except Exception as e:
        return f"Sir, I couldn't fetch that: {e}"

    # Error responses come back as JSON on 4xx.
    ctype = resp.headers.get("Content-Type", "")
    if "application/json" in ctype:
        try:
            err = json.loads(resp.read().decode("utf-8", "ignore"))
            return f"Sir, I couldn't fetch that: {err.get('error', 'unknown error')}"
        except Exception:
            return "Sir, I couldn't fetch that."

    name = resp.headers.get("X-Filename", "download.bin")
    kind = resp.headers.get("X-Kind", "file")
    total = int(resp.headers.get("Content-Length", 0) or 0)

    dest_dir = Path.home() / "Downloads" / "ParkerRemote"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest = dest_dir / name

    got = 0
    last_pct = -10
    try:
        with open(dest, "wb") as f:
            while True:
                chunk = resp.read(256 * 1024)
                if not chunk:
                    break
                f.write(chunk)
                got += len(chunk)
                if total:
                    pct = int(got * 100 / total)
                    # Log every ~10% so the log isn't spammed.
                    if pct >= last_pct + 10 or pct == 100:
                        last_pct = pct
                        mb = got / (1024 * 1024)
                        _log(player, f"SYS: Downloading '{name}' — {pct}% "
                                     f"({mb:.1f} MB)")
    except Exception as e:
        return f"Sir, the download failed partway: {e}"

    what = "folder (zipped)" if kind == "folder-zip" else "file"
    size_mb = got / (1024 * 1024)
    size_str = f"{size_mb:.1f} MB" if size_mb >= 1 else f"{got:,} bytes"
    _log(player, f"SYS: Saved '{name}' → {dest}")
    return (f"Got the {what} '{name}' ({size_str}) from the remote machine. "
            f"Saved to {dest}.")


def remote_exec(parameters: dict, player=None, session_memory=None) -> str:
    url, token = _cfg()
    if not url or not token:
        return _not_configured()
    cmd = (parameters or {}).get("cmd", "").strip()
    if not cmd:
        return "Sir, what command should I run on the remote machine?"
    d = _rpc("exec", {"cmd": cmd}, timeout=40)
    if d.get("error"):
        return f"Sir, I couldn't run that: {d['error']}"
    out = (d.get("stdout") or "").strip()
    err = (d.get("stderr") or "").strip()
    msg = f"Command finished (exit {d.get('returncode')})."
    if out:
        msg += f"\nOutput:\n{out[:1500]}"
    if err:
        msg += f"\nErrors:\n{err[:500]}"
    return msg
