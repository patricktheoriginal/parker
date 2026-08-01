"""
zalo_inspect.py — list Zalo's buttons so Parker can find the call button.

Parker clicks Zalo's voice-call button via Windows UI Automation, matching the
button's accessibility name. If calling doesn't work, run this with a Zalo
conversation open and send the output back — we'll add your button's name.

Run (Windows, with a Zalo chat open):
    python tools/zalo_inspect.py
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    try:
        from pywinauto import Desktop
    except Exception as e:
        print(f"pywinauto not available: {e}")
        print("Install it:  pip install pywinauto")
        return

    win = None
    for w in Desktop(backend="uia").windows():
        try:
            if "zalo" in (w.window_text() or "").lower():
                win = w
                break
        except Exception:
            continue
    if win is None:
        print("Zalo window not found. Open Zalo and a conversation, then retry.")
        return

    print(f"Zalo window: {win.window_text()!r}\n")
    print("Buttons (name — these are what Parker matches for the call button):")
    n = 0
    for ctrl in win.descendants(control_type="Button"):
        try:
            name = ctrl.window_text()
            if name and name.strip():
                print(f"  - {name!r}")
                n += 1
        except Exception:
            continue
    if n == 0:
        print("  (no named buttons found — the call icon may be an image/menu item)")
    print("\nSend this list back so the voice-call button can be matched reliably.")


if __name__ == "__main__":
    main()
