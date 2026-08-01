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
    if not items:
        return f"{d.get('path')} is empty."
    lines = [f"Contents of {d.get('path')}:"]
    for it in items[:40]:
        tag = "📁" if it["dir"] else "📄"
        size = "" if it["dir"] else f" ({it['size']:,} B)"
        lines.append(f"  {tag} {it['name']}{size}")
    return "\n".join(lines)


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
    lines = [f"Found {len(matches)} file(s) matching '{query}':"]
    lines += [f"  - {m}" for m in matches[:20]]
    lines.append("Say 'get <full path>' to fetch one.")
    return "\n".join(lines)


def remote_get(parameters: dict, player=None, session_memory=None) -> str:
    url, token = _cfg()
    if not url or not token:
        return _not_configured()
    path = (parameters or {}).get("path", "").strip()
    if not path:
        return "Sir, which file should I fetch? Give the full path (use 'find' first)."
    d = _rpc("get", {"path": path}, timeout=60)
    if d.get("error"):
        return f"Sir, I couldn't fetch that file: {d['error']}"
    try:
        dest_dir = Path.home() / "Downloads" / "ParkerRemote"
        dest_dir.mkdir(parents=True, exist_ok=True)
        dest = dest_dir / d["name"]
        dest.write_bytes(base64.b64decode(d["b64"]))
    except Exception as e:
        return f"Sir, I fetched it but couldn't save it: {e}"
    return (f"Got '{d['name']}' ({d['size']:,} bytes) from the remote machine. "
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
