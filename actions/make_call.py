"""
make_call.py — Start a phone/voice call for Parker.

Two ways to place a call:
  1. Zalo desktop (UI automation): open Zalo, find the contact, and start a
     voice call. Popular in Vietnam.
  2. tel: link: hand a phone number to the OS default handler (works when a
     dialer/soft-phone is configured, or a paired phone via Link to Windows).

No API keys required. Reuses the desktop-automation helpers from send_message.
"""

import re
import time
import webbrowser

from actions.send_message import _open_app, _search_in_app, _PYAUTOGUI

try:
    import pyautogui
except Exception:
    pyautogui = None


def _looks_like_number(s: str) -> bool:
    """True if the string is basically a phone number."""
    digits = re.sub(r"[^\d]", "", s)
    return len(digits) >= 6 and re.fullmatch(r"[\d\s\+\-\(\)\.]+", s.strip()) is not None


def _call_tel_link(number: str) -> str:
    """Hand a phone number to the OS default tel: handler."""
    cleaned = re.sub(r"[^\d\+]", "", number)
    try:
        webbrowser.open(f"tel:{cleaned}")
        return f"Dialing {number} on the default phone app."
    except Exception as e:
        return f"Could not start the call: {e}"


def _call_zalo(receiver: str) -> str:
    """Open Zalo, find the contact, and start a voice call."""
    if not _open_app("Zalo"):
        return "Could not open Zalo."
    time.sleep(1.0)
    _search_in_app(receiver)
    time.sleep(0.6)
    pyautogui.press("enter")      # open the top contact result
    time.sleep(1.2)
    # In Zalo, Ctrl+Shift+P starts a voice call with the open conversation.
    try:
        pyautogui.hotkey("ctrl", "shift", "p")
        time.sleep(0.4)
    except Exception:
        pass
    return f"Calling {receiver} on Zalo."


def make_call(parameters: dict, response=None, player=None, session_memory=None) -> str:
    """Place a call.

    parameters:
      - receiver: contact name OR phone number
      - platform: 'zalo' (default) or 'phone'/'tel' for a tel: link
    """
    params   = parameters or {}
    receiver = (params.get("receiver") or params.get("number") or params.get("contact") or "").strip()
    platform = (params.get("platform") or "").strip().lower()

    if not receiver:
        return "Sir, who should I call?"

    # A bare phone number → dial it directly regardless of platform.
    if _looks_like_number(receiver) and platform in ("", "phone", "tel", "dialer", "call"):
        result = _call_tel_link(receiver)
    elif "zalo" in platform or platform == "":
        if not _PYAUTOGUI:
            return "PyAutoGUI is not installed — cannot control Zalo."
        result = _call_zalo(receiver)
    elif platform in ("phone", "tel", "dialer"):
        result = _call_tel_link(receiver)
    else:
        # Unknown platform → try Zalo by name.
        if not _PYAUTOGUI:
            return "PyAutoGUI is not installed — cannot place the call."
        result = _call_zalo(receiver)

    print(f"[Call] {result}")
    if player:
        try:
            player.write_log(f"[call] {result}")
        except Exception:
            pass
    return result
