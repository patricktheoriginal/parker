"""
gesture_control.py -- camera hand-gesture control for music playback.

Uses MediaPipe's current Tasks API (GestureRecognizer), not the deprecated
`mp.solutions.hands` legacy API -- Tasks is Google's actively maintained
gesture pipeline with a purpose-built classifier instead of hand-rolled
landmark math, and is what MediaPipe recommends going forward.

Gestures:
  - Closed_Fist (make a fist, hold roughly still) -> play/pause
  - Open_Palm swiped RIGHT (open hand, fast horizontal move)          -> next track
  - Open_Palm swiped LEFT                                             -> previous track

The swipe direction comes from tracking the wrist landmark's x position
over a short rolling window while an Open_Palm is held -- the gesture
classifier alone can't tell direction, only "hand is open", so direction
is layered on top of it.

Runs a background thread that watches the webcam and shows a small preview
window (so it's visible the camera is on and actually seeing your hand).
OFF by default -- the user turns it on/off by voice ("turn on gesture
control" / "turn off gesture control") rather than it running continuously,
to avoid needless CPU/battery use and the camera light staying on all the
time.

Requires: pip install mediapipe opencv-python (opencv-python is already a
Parker dependency). MediaPipe officially supports Python 3.9-3.12 -- see
tools/setup_venv312.ps1 if your system Python is newer. The gesture model
(~ a few MB) downloads once to ~/.parker/mediapipe/ and is cached after
that. Fails soft with a clear message if anything isn't available.
"""

import threading
import time
import urllib.request
from pathlib import Path

_STATE = {
    "thread": None,
    "running": False,
    "cap_index": 0,
    "show_preview": True,
}

_MODEL_URL = ("https://storage.googleapis.com/mediapipe-models/"
             "gesture_recognizer/gesture_recognizer/float16/latest/"
             "gesture_recognizer.task")
_MODEL_PATH = Path.home() / ".parker" / "mediapipe" / "gesture_recognizer.task"

_MIN_CONFIDENCE = 0.6
_COOLDOWN_S = 1.2          # ignore further triggers for this long after one fires

# Swipe detection (Open_Palm + fast horizontal move).
_SWIPE_WINDOW_S = 0.6       # look at wrist movement over this recent window
_SWIPE_MIN_DX = 0.35        # must cross at least this much of the frame width

# Fist-hold detection (Closed_Fist, deliberately NOT moving much, held briefly
# so a fist made in passing while gesturing something else doesn't fire).
_FIST_HOLD_S = 0.25


def gesture_control(parameters: dict = None, player=None, session_memory=None) -> str:
    """Turn hand-gesture control on or off. parameters: {"action": "on"|"off"}"""
    p = parameters or {}
    action = (p.get("action") or "").strip().lower()
    if action in ("on", "start", "enable", "activate", "true", "1"):
        return _start(player)
    if action in ("off", "stop", "disable", "deactivate", "false", "0"):
        return _stop(player)
    return ("Sir, say 'turn on gesture control' or 'turn off gesture control'. "
            "Once on: make a fist to play/pause, swipe an open hand right for "
            "next track, left for previous track.")


def _ensure_model(player=None) -> bool:
    if _MODEL_PATH.exists():
        return True
    try:
        _log(player, "Downloading the gesture recognition model (one-time, "
                     "a few MB)...")
        _MODEL_PATH.parent.mkdir(parents=True, exist_ok=True)
        urllib.request.urlretrieve(_MODEL_URL, str(_MODEL_PATH))
        return True
    except Exception as e:
        _log(player, f"Couldn't download the gesture model: {e}")
        return False


def _start(player=None) -> str:
    if _STATE["running"]:
        return "Gesture control is already on, sir."

    try:
        import cv2  # noqa: F401
    except Exception:
        return "Sir, OpenCV isn't installed -- run: pip install opencv-python"
    try:
        import mediapipe  # noqa: F401
    except Exception:
        return ("Sir, MediaPipe isn't installed or isn't compatible with this "
                "Python version (MediaPipe supports Python 3.9-3.12). Run "
                "tools/setup_venv312.ps1, or: pip install mediapipe")

    if not _ensure_model(player):
        return "Sir, I couldn't get the gesture recognition model -- check your connection."

    _STATE["running"] = True
    t = threading.Thread(target=_run_loop, args=(player,), daemon=True)
    _STATE["thread"] = t
    t.start()
    return ("Gesture control is on, sir. Make a fist to play or pause, swipe "
            "an open hand right for the next track, left for the previous one.")


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
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision as mp_vision

    from actions.computer_settings import media_playpause, next_track, prev_track

    # Shared mutable state the LIVE_STREAM callback writes into -- the
    # callback runs on MediaPipe's own thread, not this loop, so results are
    # picked up on the next frame we render rather than awaited directly.
    latest = {"gesture": None, "score": 0.0, "hand_seen": False, "wrist_x": None}

    def _on_result(result, output_image, timestamp_ms):
        if result.gestures:
            top = result.gestures[0][0]   # best gesture for the first hand
            latest["gesture"] = top.category_name
            latest["score"] = top.score
            latest["hand_seen"] = True
            latest["wrist_x"] = result.hand_landmarks[0][0].x if result.hand_landmarks else None
        else:
            latest["gesture"] = None
            latest["hand_seen"] = False
            latest["wrist_x"] = None

    options = mp_vision.GestureRecognizerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(_MODEL_PATH)),
        running_mode=mp_vision.RunningMode.LIVE_STREAM,
        num_hands=1,
        min_hand_detection_confidence=0.6,
        min_tracking_confidence=0.5,
        result_callback=_on_result,
    )

    try:
        recognizer = mp_vision.GestureRecognizer.create_from_options(options)
    except Exception as e:
        _log(player, f"Couldn't start the gesture recognizer: {e}")
        _STATE["running"] = False
        return

    cap = cv2.VideoCapture(_STATE["cap_index"])
    if not cap.isOpened():
        _log(player, "Couldn't open the camera for gesture control.")
        recognizer.close()
        _STATE["running"] = False
        return

    _log(player, "Gesture control camera started.")

    # Small preview window with live status text, so it's visibly obvious the
    # camera is on and actually seeing your hand -- pure background tracking
    # with no feedback made it impossible to tell whether it was working.
    window_name = "Parker - Gesture Control (press Q or say 'turn off gesture control' to stop)"
    show_preview = _STATE.get("show_preview", True)

    last_trigger = 0.0
    flash_until = 0.0
    flash_label = ""
    start_time = time.monotonic()

    # Rolling (timestamp, wrist_x) samples while an Open_Palm is held, used to
    # detect swipe direction/distance.
    palm_history: list[tuple[float, float]] = []
    # How long a fist has been continuously held, to avoid firing on a fist
    # that flashes past mid-gesture.
    fist_since: float | None = None

    try:
        while _STATE["running"]:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue

            frame = cv2.flip(frame, 1)  # mirror, so it feels natural
            rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            ts_ms = int((time.monotonic() - start_time) * 1000)
            recognizer.recognize_async(mp_image, ts_ms)

            now = time.monotonic()
            gesture = latest["gesture"]
            score = latest["score"]
            wrist_x = latest["wrist_x"]
            confident = score >= _MIN_CONFIDENCE
            cooling_down = (now - last_trigger) <= _COOLDOWN_S

            # --- Open_Palm: track wrist x to detect a left/right swipe. ---
            if confident and gesture == "Open_Palm" and wrist_x is not None:
                palm_history.append((now, wrist_x))
            else:
                palm_history.clear()
            palm_history[:] = [(t, x) for t, x in palm_history if now - t <= _SWIPE_WINDOW_S]

            if not cooling_down and len(palm_history) >= 2:
                xs = [x for _, x in palm_history]
                dx = xs[-1] - xs[0]   # signed: positive = moved right
                if abs(dx) >= _SWIPE_MIN_DX:
                    last_trigger = now
                    palm_history.clear()
                    if dx > 0:
                        flash_label = "NEXT"
                        action_fn, action_name = next_track, "next track"
                    else:
                        flash_label = "PREVIOUS"
                        action_fn, action_name = prev_track, "previous track"
                    flash_until = now + 0.4
                    try:
                        action_fn()
                        _log(player, f"Swipe {flash_label.lower()} detected -- {action_name}.")
                    except Exception as e:
                        _log(player, f"Swipe detected but {action_name} failed: {e}")

            # --- Closed_Fist: play/pause after a brief deliberate hold. ---
            if confident and gesture == "Closed_Fist":
                if fist_since is None:
                    fist_since = now
                elif not cooling_down and (now - fist_since) >= _FIST_HOLD_S:
                    last_trigger = now
                    fist_since = None
                    flash_label = "PLAY/PAUSE"
                    flash_until = now + 0.4
                    try:
                        media_playpause()
                        _log(player, "Fist detected -- toggled play/pause.")
                    except Exception as e:
                        _log(player, f"Fist detected but playback control failed: {e}")
            else:
                fist_since = None

            if show_preview:
                h, w = frame.shape[:2]
                if latest["hand_seen"]:
                    label = f"{gesture or '...'} ({score:.2f})"
                    cv2.putText(frame, label, (10, 30),
                               cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 0), 2)
                else:
                    cv2.putText(frame, "no hand in view", (10, 30),
                               cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 0, 255), 2)
                if now < flash_until:
                    cv2.putText(frame, flash_label, (10, h - 20),
                               cv2.FONT_HERSHEY_SIMPLEX, 0.9, (0, 255, 0), 2)
                border = (0, 255, 0) if now < flash_until else (60, 60, 60)
                cv2.rectangle(frame, (0, 0), (w - 1, h - 1), border, 6)
                cv2.imshow(window_name, frame)
                # waitKey also pumps the window's event loop -- required for
                # imshow to actually render/update each frame.
                if cv2.waitKey(1) & 0xFF == ord("q"):
                    break

            time.sleep(0.02)   # ~50 fps cap, plenty for gesture tracking
    except Exception as e:
        _log(player, f"Gesture control stopped due to an error: {e}")
    finally:
        cap.release()
        if show_preview:
            try:
                cv2.destroyWindow(window_name)
            except Exception:
                pass
        recognizer.close()
        _STATE["running"] = False
        _log(player, "Gesture control camera stopped.")
