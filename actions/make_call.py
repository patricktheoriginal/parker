"""
make_call.py — place a phone call for Parker.

Calls a phone number via the OS default tel: handler. On Windows 11 with Phone
Link set as the handler for tel:, this dials through your paired phone
automatically.

Note: Zalo (and other closed apps) provide NO programmatic way to start a voice
call, so Parker calls real phone numbers only. Use send_message to message a
Zalo contact.
"""

import re
import webbrowser


def _looks_like_number(s: str) -> bool:
    """True if the string is basically a phone number."""
    digits = re.sub(r"[^\d]", "", s)
    return len(digits) >= 6 and re.fullmatch(r"[\d\s\+\-\(\)\.]+", s.strip()) is not None


def make_call(parameters: dict, response=None, player=None, session_memory=None) -> str:
    """Dial a phone number via the OS default phone app (tel:).

    parameters:
      - receiver / number: the phone number to call
    """
    params = parameters or {}
    number = (params.get("receiver") or params.get("number")
              or params.get("contact") or "").strip()

    if not number:
        return "Sir, what phone number should I call?"

    if not _looks_like_number(number):
        return (f"Sir, I can only place calls to a phone number, and '{number}' "
                f"isn't one. Please give me the number to dial.")

    cleaned = re.sub(r"[^\d+]", "", number)
    try:
        webbrowser.open(f"tel:{cleaned}")
        result = (f"Dialing {number} on the default phone app. "
                  f"(On Windows, this uses Phone Link with your paired phone.)")
    except Exception as e:
        result = f"Sir, I couldn't start the call: {e}"

    print(f"[Call] {result}")
    if player:
        try:
            player.write_log(f"[call] {result}")
        except Exception:
            pass
    return result
