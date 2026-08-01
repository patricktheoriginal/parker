"""
calibrate_zalo_call.py — Teach Parker where Zalo's voice-call button is.

Zalo has no public API to start a call, so Parker clicks the call button on the
Zalo desktop window. Different Zalo versions/themes put it in slightly different
spots, so this saves a screenshot of YOUR call button for reliable clicking.

How to use:
  1. Open Zalo and open any conversation so the call (phone) icon is visible in
     the top-right of the chat header.
  2. Run:  python tools/calibrate_zalo_call.py
  3. Move your mouse over the VOICE-CALL (phone) icon and wait for the countdown.
  4. It saves a small crop around the cursor to config/zalo_call_button.png.

After this, "call <name>" will click that button.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    try:
        import pyautogui
    except Exception as e:
        print(f"pyautogui not available: {e}")
        return

    print("=== Calibrate Zalo call button ===")
    print("Open a Zalo conversation. Then hover your mouse over the VOICE-CALL")
    print("(phone) icon in the top-right of the chat header.\n")
    for i in range(6, 0, -1):
        x, y = pyautogui.position()
        print(f"  Capturing in {i}s… (cursor at {x},{y})", end="\r")
        time.sleep(1)
    print()

    x, y = pyautogui.position()
    # Crop a small box around the cursor (the button is ~36px).
    half = 24
    region = (max(0, x - half), max(0, y - half), half * 2, half * 2)
    out = Path(__file__).resolve().parent.parent / "config" / "zalo_call_button.png"
    out.parent.mkdir(parents=True, exist_ok=True)
    try:
        img = pyautogui.screenshot(region=region)
        img.save(str(out))
        print(f"Saved call-button image to: {out}")
        print("Parker will now click this button when you say 'call <name>'.")
    except Exception as e:
        print(f"Failed to capture: {e}")


if __name__ == "__main__":
    main()
