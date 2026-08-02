"""
offline_agent.py — Parker's offline brain.

When the internet or the Gemini cloud is unavailable, Parker falls back to a
LOCAL model served by Ollama (e.g. llama3.2:3b) for understanding + tool use.

This module:
  1. Builds OpenAI/Ollama-style tool schemas from a curated set of Parker's
     actions (the ones that make sense offline or that degrade gracefully).
  2. Sends the user's message + tool schemas to the local model via
     core.llm_client.call_llm.
  3. Dispatches any tool call to the matching Python action and feeds the
     result back to the model for a final natural-language reply.

Note: tools that need the internet (weather, news, routes…) still require a
connection to return real data; offline they simply report they can't fetch it.
The always-available offline tools are the device-control ones.
"""

import json
import traceback

from core.llm_client import call_llm, ensure_ollama_running, get_llm_settings


# ── Action registry ──────────────────────────────────────────────────────────
# Each entry: name -> (callable, schema). The callable takes a dict of args and
# returns a string. Kept deliberately small and robust for small local models.

def _reg():
    from actions.weather_report import (
        weather_action, rain_forecast, where_am_i, set_my_location,
    )
    from actions.maps_route import route_directions
    from actions.web_search import web_search as web_search_action
    from actions.send_message import send_message
    from actions.make_call import make_call
    from actions.reminder import reminder
    from actions.open_app import open_app
    from actions.computer_settings import computer_settings
    from actions.system_monitor import get_system_status
    # Additional offline-capable tools (UI automation — no API needed).
    try:
        from actions.spotify import play_spotify, play_favorites
        _SPOTIFY_OK = True
    except Exception:
        _SPOTIFY_OK = False
    try:
        from actions.desktop import desktop_control
        _DESKTOP_OK = True
    except Exception:
        _DESKTOP_OK = False
    try:
        from actions.file_controller import file_controller
        _FILE_OK = True
    except Exception:
        _FILE_OK = False

    def _weather(a):   return weather_action(parameters=a)
    def _rain(a):      return rain_forecast(a)
    def _route(a):     return route_directions(a)
    def _where(a):     return where_am_i(a)
    def _setloc(a):    return set_my_location(a)
    def _news(a):      return web_search_action(parameters={"query": a.get("query", ""), "mode": "news"})
    def _search(a):    return web_search_action(parameters={"query": a.get("query", ""), "mode": a.get("mode", "search")})
    def _msg(a):       return send_message(parameters=a)
    def _call(a):      return make_call(a)
    def _remind(a):    return reminder(a)
    def _open(a):      return open_app(parameters=a)
    def _settings(a):  return computer_settings(parameters=a)
    def _status(a):
        try:
            d = get_system_status()
            return (f"CPU {d['cpu_percent']}%, RAM {d['ram_percent']}% "
                    f"({d['ram_used_gb']}/{d['ram_total_gb']} GB), "
                    f"GPU {d.get('gpu_percent')}%, temp {d.get('cpu_temp_c')}°C, "
                    f"uptime {d['uptime']}.")
        except Exception as e:
            return f"Could not read system status: {e}"
    def _spotify(a):    return play_spotify(parameters=a)
    def _fav(a):        return play_favorites(parameters=a)
    def _desktop(a):    return desktop_control(parameters=a)
    def _file(a):       return file_controller(parameters=a)

    def T(name, desc, props, required):
        return {"type": "function", "function": {
            "name": name, "description": desc,
            "parameters": {"type": "object", "properties": props, "required": required},
        }}

    S = {"type": "string"}
    tools = {
        "weather_report": (_weather, T("weather_report", "Weather for a Vietnamese city (default Vietnam).",
                            {"city": S}, [])),
        "rain_forecast": (_rain, T("rain_forecast", "Rain forecast for a Vietnamese place.",
                            {"city": S, "days": {"type": "integer"}}, [])),
        "route_directions": (_route, T("route_directions", "Driving route between two places in Vietnam.",
                            {"destination": S, "origin": S, "depart_time": S}, ["destination"])),
        "where_am_i": (_where, T("where_am_i", "The user's current location.", {}, [])),
        "set_my_location": (_setloc, T("set_my_location", "Set the user's current location.",
                            {"place": S}, ["place"])),
        "web_search": (_search, T("web_search", "Search the web.", {"query": S, "mode": S}, ["query"])),
        "news": (_news, T("news", "Latest Vietnam news headlines.", {"query": S}, [])),
        "send_message": (_msg, T("send_message", "Send a text message (Zalo/Telegram/...).",
                            {"receiver": S, "message_text": S, "platform": S}, ["receiver", "message_text"])),
        "make_call": (_call, T("make_call", "Call a contact (Zalo) or dial a number.",
                            {"receiver": S, "platform": S}, ["receiver"])),
        "reminder": (_remind, T("reminder", "Set/list/delete reminders.",
                            {"action": S, "date": S, "time": S, "message": S, "repeat": S}, [])),
        "open_app": (_open, T("open_app", "Open an application.", {"app_name": S}, ["app_name"])),
        "computer_settings": (_settings, T("computer_settings",
                            "Control the PC: volume, brightness, wifi, power, music play/pause/stop/next.",
                            {"action": S, "description": S, "value": S}, [])),
        "system_status": (_status, T("system_status", "CPU/RAM/GPU/temperature status.", {}, [])),
    }
    # Add optional tools that may not import on every machine.
    if _SPOTIFY_OK:
        tools["play_spotify"] = (_spotify, T("play_spotify",
            "Play a song/artist on Spotify by name.",
            {"query": S}, ["query"]))
        tools["play_favorites"] = (_fav, T("play_favorites",
            "Play the user's Liked Songs on Spotify (shuffled).", {}, []))
    if _DESKTOP_OK:
        tools["desktop_control"] = (_desktop, T("desktop_control",
            "Click/type/scroll on the desktop by description.",
            {"action": S, "description": S}, ["action", "description"]))
    if _FILE_OK:
        tools["file_controller"] = (_file, T("file_controller",
            "Copy/move/delete/list files and folders.",
            {"action": S, "source": S, "destination": S}, ["action", "source"]))
    return tools


_REGISTRY = None


def _registry():
    global _REGISTRY
    if _REGISTRY is None:
        _REGISTRY = _reg()
    return _REGISTRY


def _tool_schemas() -> list:
    return [schema for (_fn, schema) in _registry().values()]


def _dispatch(name: str, args: dict) -> str:
    entry = _registry().get(name)
    if not entry:
        return f"(unknown tool: {name})"
    fn, _schema = entry
    try:
        out = fn(args or {})
        return out if isinstance(out, str) else str(out)
    except Exception as e:
        traceback.print_exc()
        return f"Tool {name} failed: {e}"


_SYSTEM = (
    "You are Parker, a concise, professional voice assistant running OFFLINE on "
    "a local model. Always answer in English. Be direct and efficient. "
    "Use tools when the user asks for something a tool can do: "
    "weather, rain, routes, search, news, messages, calls, reminders, "
    "opening apps, controlling the PC (volume, brightness, music, settings), "
    "desktop control (click/type), file operations, or system status. "
    "For music: use play_spotify or play_favorites. "
    "If a tool fails or needs the internet, tell the user briefly. "
    "Keep replies to 1-3 short sentences. Never fabricate information — "
    "say you don't know if unsure."
)


def offline_available() -> bool:
    """True if a local Ollama server is reachable (or can be started)."""
    try:
        return ensure_ollama_running()
    except Exception:
        return False


def offline_respond(user_text: str, history: list | None = None,
                    log=None) -> str:
    """Run one offline turn: user_text -> (optional tool) -> final reply.

    `history` is an optional list of prior {role, content} messages.
    `log` is an optional callable(str) for status lines.
    Returns the assistant's final text reply.
    """
    def _log(m):
        print(f"[Offline] {m}")
        if log:
            try: log(m)
            except Exception: pass

    if not user_text or not user_text.strip():
        return ""

    if not ensure_ollama_running():
        return ("Offline mode needs Ollama running. Install it from ollama.com "
                "and make sure a model like llama3.2:3b is pulled.")

    _url, model = get_llm_settings()
    _log(f"Using local model: {model}")

    messages = [{"role": "system", "content": _SYSTEM}]
    if history:
        messages.extend(history[-8:])
    messages.append({"role": "user", "content": user_text.strip()})

    try:
        first = call_llm(messages, tools=_tool_schemas(), timeout=120)
    except Exception as e:
        return f"Offline model error: {e}"

    tool_calls = first.get("tool_calls") or []
    if not tool_calls:
        return first.get("content") or "…"

    # Execute tool calls, then ask the model to summarise the results.
    messages.append({"role": "assistant", "content": first.get("content", ""),
                     "tool_calls": tool_calls})
    for tc in tool_calls:
        fn = tc.get("function", {})
        name = fn.get("name", "")
        args = fn.get("arguments", {})
        if isinstance(args, str):
            try: args = json.loads(args)
            except Exception: args = {}
        _log(f"tool: {name} {args}")
        result = _dispatch(name, args)
        messages.append({"role": "tool", "name": name, "content": result[:1500]})

    raw_results = "; ".join(m["content"] for m in messages if m.get("role") == "tool")
    try:
        final = call_llm(messages, tools=None, timeout=120)
        # Some models (e.g. reasoning models) return empty content after tools —
        # fall back to the raw tool result so the user still gets the answer.
        return final.get("content") or raw_results or "Done."
    except Exception:
        return raw_results or "Done."
