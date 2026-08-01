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


def _find_call_button() -> tuple[int, int] | None:
    """Locate Zalo's voice-call button by image, if a template is provided.

    Put a small screenshot of the call button at config/zalo_call_button.png to
    enable reliable clicking. Returns (x, y) center or None.
    """
    try:
        from pathlib import Path
        import pyautogui as _pg
        tmpl = Path(__file__).resolve().parent.parent / "config" / "zalo_call_button.png"
        if not tmpl.exists():
            return None
        box = _pg.locateOnScreen(str(tmpl), confidence=0.8)
        if box:
            return _pg.center(box)
    except Exception:
        pass
    return None


def _call_zalo(receiver: str) -> str:
    """Open Zalo, find the contact, open the conversation, and start a voice
    call by clicking the call button (or trying known shortcuts)."""
    if not _open_app("Zalo"):
        return "Could not open Zalo."
    time.sleep(1.2)
    _search_in_app(receiver)
    time.sleep(0.8)
    pyautogui.press("enter")      # open the top contact result
    time.sleep(1.3)

    # 1) Best: click the call button located by image template (if provided).
    btn = _find_call_button()
    if btn:
        try:
            pyautogui.click(btn)
            time.sleep(0.4)
            return f"Calling {receiver} on Zalo."
        except Exception:
            pass

    # 2) Click the voice-call icon area — in Zalo it sits in the top-right of the
    #    chat header. Click a position relative to the screen's top-right.
    try:
        import pyautogui as _pg
        w, _h = _pg.size()
        # The phone (voice) icon is left of the video-call icon in the header.
        pyautogui.click(w - 150, 92)
        time.sleep(0.5)
        return (f"I opened {receiver} in Zalo and tried to start a voice call. "
                f"If it didn't dial, the call button position differs on your "
                f"Zalo — I can calibrate it if you save a screenshot of the "
                f"button to config/zalo_call_button.png.")
    except Exception:
        pass

    # 3) Last resort: try known keyboard shortcuts across Zalo versions.
    for combo in (("ctrl", "shift", "p"), ("ctrl", "shift", "c")):
        try:
            pyautogui.hotkey(*combo)
            time.sleep(0.3)
        except Exception:
            pass
    return (f"I opened {receiver} in Zalo. I couldn't confirm the call started — "
            f"please tap the call button, or save a screenshot of it to "
            f"config/zalo_call_button.png so I can click it next time.")


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
