"""
home_assistant.py — control smart-home devices via Home Assistant.

Uses the Home Assistant REST API (local, no cloud). Configure in
config/api_keys.json:
    "home_assistant_url":   "http://192.168.1.10:8123",
    "home_assistant_token": "<Long-Lived Access Token>"

Create the token in Home Assistant → your profile → Long-Lived Access Tokens.

Supports turning devices on/off, toggling, setting brightness, and reading
device state by name (e.g. "living room light").
"""

import json
import urllib.request

from memory.config_manager import load_api_keys


def _cfg():
    c = load_api_keys()
    url = (c.get("home_assistant_url") or "").rstrip("/")
    token = (c.get("home_assistant_token") or "").strip()
    return url, token


def _api(path: str, method: str = "GET", body: dict | None = None):
    url, token = _cfg()
    if not url or not token:
        raise RuntimeError("not_configured")
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(
        f"{url}/api/{path}", data=data, method=method,
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=10) as r:
        raw = r.read().decode("utf-8", "ignore")
        return json.loads(raw) if raw else {}


def _not_configured_msg() -> str:
    return ("Sir, Home Assistant isn't set up. Add 'home_assistant_url' and "
            "'home_assistant_token' (a Long-Lived Access Token) to "
            "config/api_keys.json.")


def _find_entity(name: str, domain_filter: tuple = ()) -> tuple[str, str] | None:
    """Match a spoken name to a Home Assistant entity_id. Returns
    (entity_id, friendly_name) or None."""
    try:
        states = _api("states")
    except Exception:
        return None
    name_l = name.lower().strip()
    best = None
    for s in states:
        eid = s.get("entity_id", "")
        domain = eid.split(".")[0]
        if domain_filter and domain not in domain_filter:
            continue
        friendly = (s.get("attributes", {}).get("friendly_name") or eid).lower()
        if name_l in friendly or name_l in eid.lower():
            # Prefer an exact-ish match (shorter friendly name).
            if best is None or len(friendly) < len(best[2]):
                best = (eid, s.get("attributes", {}).get("friendly_name") or eid, friendly)
    return (best[0], best[1]) if best else None


def home_control(parameters: dict, player=None, session_memory=None) -> str:
    """Control a smart-home device.

    parameters:
      - device: spoken device/area name, e.g. 'living room light'
      - action: 'on' | 'off' | 'toggle' | 'status'
      - brightness: optional 0-100 (for lights)
    """
    p = parameters or {}
    device = (p.get("device") or p.get("name") or p.get("entity") or "").strip()
    action = (p.get("action") or "toggle").lower().strip()
    brightness = p.get("brightness")

    url, token = _cfg()
    if not url or not token:
        return _not_configured_msg()
    if not device:
        return "Sir, which device should I control?"

    # Controllable domains for on/off/toggle.
    match = _find_entity(device, ("light", "switch", "fan", "climate",
                                  "media_player", "cover", "input_boolean"))
    if not match:
        return f"Sir, I couldn't find a device called '{device}' in Home Assistant."
    entity_id, friendly = match
    domain = entity_id.split(".")[0]

    # Status read
    if action in ("status", "state"):
        try:
            st = _api(f"states/{entity_id}")
            state = st.get("state", "unknown")
            return f"{friendly} is {state}."
        except Exception as e:
            return f"Sir, I couldn't read {friendly}: {e}"

    # On / off / toggle
    service = {"on": "turn_on", "off": "turn_off", "toggle": "toggle"}.get(action, "toggle")
    body = {"entity_id": entity_id}
    if service == "turn_on" and brightness is not None and domain == "light":
        try:
            body["brightness_pct"] = max(0, min(100, int(brightness)))
        except (TypeError, ValueError):
            pass
    try:
        _api(f"services/{domain}/{service}", method="POST", body=body)
        verb = {"turn_on": "turned on", "turn_off": "turned off", "toggle": "toggled"}[service]
        extra = (f" to {body['brightness_pct']}%"
                 if "brightness_pct" in body else "")
        msg = f"{friendly} {verb}{extra}."
    except Exception as e:
        msg = f"Sir, I couldn't control {friendly}: {e}"

    if player:
        try:
            player.write_log(f"[home] {msg}")
        except Exception:
            pass
    return msg


def home_list(parameters: dict = None, player=None, session_memory=None) -> str:
    """List controllable Home Assistant devices."""
    url, token = _cfg()
    if not url or not token:
        return _not_configured_msg()
    try:
        states = _api("states")
    except Exception as e:
        return f"Sir, I couldn't reach Home Assistant: {e}"
    controllable = ("light", "switch", "fan", "climate", "media_player", "cover")
    names = []
    for s in states:
        if s.get("entity_id", "").split(".")[0] in controllable:
            names.append(s.get("attributes", {}).get("friendly_name") or s["entity_id"])
    if not names:
        return "Sir, I found no controllable devices in Home Assistant."
    return "Home Assistant devices:\n" + "\n".join(f"  - {n}" for n in names[:25])
