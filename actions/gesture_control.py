"""
gesture_control.py — camera hand-swipe gesture to play/pause music.

Runs a background thread that watches the webcam via MediaPipe Hands and
toggles play/pause when it sees a fast horizontal swipe (a hand crossing most
of the frame width within a short time window). OFF by default — the user
turns it on/off by voice ("turn on gesture control" / "turn off gesture
control") rather than it running continuously, to avoid needless CPU/battery
use and the camera light staying on all the time.

Requires: pip install mediapipe opencv-python (opencv-python is already a
Parker dependency). Fails soft with a clear message if mediapipe isn't
installed.
"""

import threading
import time

_STATE = {
    "thread": None,
    "running": False,
    "cap_index": 0,
}

# Swipe detection tuning.
_SWIPE_MIN_DX = 0.45      # hand must cross at least 45% of frame width
_SWIPE_MAX_S = 0.6        # ...within this many seconds to count as a swipe
_COOLDOWN_S = 1.5         # ignore further swipes for this long after one fires


def gesture_control(parameters: dict = None, player=None, session_memory=None) -> str:
    """Turn hand-swipe gesture control on or off. parameters: {"action": "on"|"off"}"""
    p = parameters or {}
    action = (p.get("action") or "").strip().lower()
    if action in ("on", "start", "enable", "activate", "true", "1"):
        return _start(player)
    if action in ("off", "stop", "disable", "deactivate", "false", "0"):
        return _stop(player)
    return ("Sir, say 'turn on gesture control' or 'turn off gesture control' — "
            "swiping your hand across the camera will then play/pause music.")


def _start(player=None) -> str:
    if _STATE["running"]:
        return "Gesture control is already on, sir."

    try:
        import cv2  # noqa: F401
    except Exception:
        return "Sir, OpenCV isn't installed — run: pip install opencv-python"
    try:
        import mediapipe  # noqa: F401
    except Exception:
        return "Sir, MediaPipe isn't installed — run: pip install mediapipe"

    _STATE["running"] = True
    t = threading.Thread(target=_run_loop, args=(player,), daemon=True)
    _STATE["thread"] = t
    t.start()
    return ("Gesture control is on, sir. Swipe your hand across the camera "
            "to play or pause music.")


def _stop(player=None) -> str:
    if not _STATE["running"]:
        return "Gesture control is already off, sir."
    _STATE["running"] = False
    # The loop thread checks _STATE["running"] each frame and exits + releases
    # the camera on its own; no need to join here (avoids blocking the caller
    # on a camera read that's mid-flight).
    return "Gesture control is off, sir."


def _log(player, msg: str):
    print(f"[Gesture] {msg}")
    if player:
        try:
            player.write_log(f"SYS: {msg}")
        except Exception:
            pass


def _run_loop(player) -> None:
    import cv2
    import mediapipe as mp

    from actions.computer_settings import media_playpause

    hands = mp.solutions.hands.Hands(
        model_complexity=0,        # lightest model — this only needs to
                                    # track one point smoothly, not draw a skeleton
        max_num_hands=1,
        min_detection_confidence=0.6,
        min_tracking_confidence=0.5,
    )

    cap = cv2.VideoCapture(_STATE["cap_index"])
    if not cap.isOpened():
        _log(player, "Couldn't open the camera for gesture control.")
        _STATE["running"] = False
        return

    _log(player, "Gesture control camera started.")

    # Track the wrist's x position over a short rolling window to detect a
    # fast, large horizontal movement (a swipe) without false-triggering on
    # normal small hand jitter.
    history: list[tuple[float, float]] = []   # (timestamp, x_normalized)
    last_trigger = 0.0

    try:
        while _STATE["running"]:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue

            frame = cv2.flip(frame, 1)  # mirror, so swipe direction feels natural
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            result = hands.process(rgb)

            now = time.monotonic()
            if result.multi_hand_landmarks:
                # Wrist landmark (index 0) is a stable single point to track.
                wrist = result.multi_hand_landmarks[0].landmark[0]
                history.append((now, wrist.x))
            # Drop samples older than the swipe detection window.
            history[:] = [(t, x) for t, x in history if now - t <= _SWIPE_MAX_S]

            if len(history) >= 2 and (now - last_trigger) > _COOLDOWN_S:
                xs = [x for _, x in history]
                dx = max(xs) - min(xs)
                if dx >= _SWIPE_MIN_DX:
                    last_trigger = now
                    history.clear()
                    try:
                        media_playpause()
                        _log(player, "Swipe detected — toggled play/pause.")
                    except Exception as e:
                        _log(player, f"Swipe detected but playback control failed: {e}")

            time.sleep(0.02)   # ~50 fps cap, plenty for gesture tracking
    except Exception as e:
        _log(player, f"Gesture control stopped due to an error: {e}")
    finally:
        cap.release()
        hands.close()
        _STATE["running"] = False
        _log(player, "Gesture control camera stopped.")
