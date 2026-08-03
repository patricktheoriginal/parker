"""
gesture_control.py -- camera hand-gesture control for music playback.

Uses MediaPipe's current Tasks API (GestureRecognizer), not the deprecated
`mp.solutions.hands` legacy API -- Tasks is Google's actively maintained
gesture pipeline. Its hand_landmarks output (21 points per hand) is used
directly for continuous tracking rather than relying only on its canned
gesture labels, since the controls here are motion/shape-based rather than
static poses.

Controls (all relative to the hand's OWN size/orientation, so they work at
any distance from the camera or hand size):
  - Twist the wrist right/left (forearm stays roughly still, the hand
    rotates -- like turning a doorknob) -> next / previous track.
    Measured as the angle between the wrist->middle-finger-base vector
    (the hand's own "up" axis) and the wrist->thumb-base vector; a fast
    change in that angle is a twist, independent of where the hand is
    positioned in frame.
  - Gradually opening the hand (fingers extending) -> volume goes up,
    tracking how open the hand is in real time.
    Gradually closing the hand (fingers curling into a fist)
                                        -> volume goes down, same way.
    Measured as total fingertip-to-wrist distance, normalized by the
    hand's own palm size (wrist-to-middle-finger-base distance) so it
    doesn't depend on how far the hand is from the camera.

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

import math
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

# Landmark indices (MediaPipe Hands 21-point model).
_WRIST = 0
_THUMB_BASE = 2
_MIDDLE_BASE = 9
_FINGERTIPS = (4, 8, 12, 16, 20)   # thumb, index, middle, ring, pinky tips

_MIN_HAND_CONF = 0.4
_MIN_TRACK_CONF = 0.3
_MIN_PRESENCE_CONF = 0.4

# --- Wrist twist (next/previous) ---
_TWIST_WINDOW_S = 0.5       # look at angle change over this recent window
_TWIST_MIN_DELTA_DEG = 35   # angle must swing at least this many degrees
_TWIST_COOLDOWN_S = 1.0     # ignore further twists for this long after one fires

# --- Openness -> volume ---
# Openness is normalized fingertip-to-wrist distance (divided by palm size),
# roughly 1.0 for a relaxed open hand and much smaller for a closed fist --
# calibrated generously since exact values vary by hand shape/camera angle.
_OPENNESS_MIN = 1.0     # at/below this -> treated as fully closed (0% contribution)
_OPENNESS_MAX = 2.2     # at/above this -> treated as fully open (100% contribution)
_VOLUME_SMOOTHING = 0.25   # 0..1, higher = follows the hand faster/less smooth
_VOLUME_UPDATE_MIN_INTERVAL_S = 0.15   # don't spam volume_set faster than this


def gesture_control(parameters: dict = None, player=None, session_memory=None) -> str:
    """Turn hand-gesture control on or off. parameters: {"action": "on"|"off"}"""
    p = parameters or {}
    action = (p.get("action") or "").strip().lower()
    if action in ("on", "start", "enable", "activate", "true", "1"):
        return _start(player)
    if action in ("off", "stop", "disable", "deactivate", "false", "0"):
        return _stop(player)
    return ("Sir, say 'turn on gesture control' or 'turn off gesture control'. "
            "Once on: twist your wrist right for next track, left for "
            "previous; open your hand to raise volume, close it to lower.")


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
    return ("Gesture control is on, sir. Twist your wrist right for next "
            "track, left for previous. Open your hand to raise volume, "
            "close it to lower.")


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


def _hand_angle_deg(lm) -> float:
    """Angle (degrees) of the wrist->thumb-base vector relative to the
    wrist->middle-finger-base vector -- the hand's own "up" axis. This
    rotates as the wrist twists, independent of where the hand is
    positioned or how the arm is angled, because it's measured relative to
    the hand's own geometry rather than the frame."""
    wx, wy = lm[_WRIST].x, lm[_WRIST].y
    mx, my = lm[_MIDDLE_BASE].x, lm[_MIDDLE_BASE].y
    tx, ty = lm[_THUMB_BASE].x, lm[_THUMB_BASE].y
    # Angle of the "up" axis and the thumb vector, both from the wrist.
    up_angle = math.atan2(my - wy, mx - wx)
    thumb_angle = math.atan2(ty - wy, tx - wx)
    delta = math.degrees(thumb_angle - up_angle)
    # Normalize to [-180, 180] so it doesn't wrap around discontinuously.
    while delta > 180:
        delta -= 360
    while delta < -180:
        delta += 360
    return delta


def _hand_openness(lm) -> float:
    """Sum of fingertip-to-wrist distances, normalized by palm size
    (wrist-to-middle-finger-base distance) so the value doesn't depend on
    how far the hand is from the camera or how large the hand is."""
    wx, wy = lm[_WRIST].x, lm[_WRIST].y
    mx, my = lm[_MIDDLE_BASE].x, lm[_MIDDLE_BASE].y
    palm_size = math.hypot(mx - wx, my - wy)
    if palm_size < 1e-6:
        return 0.0
    total = 0.0
    for idx in _FINGERTIPS:
        fx, fy = lm[idx].x, lm[idx].y
        total += math.hypot(fx - wx, fy - wy)
    return (total / len(_FINGERTIPS)) / palm_size


def _run_loop(player) -> None:
    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision as mp_vision

    from actions.computer_settings import next_track, prev_track, volume_set

    # Shared mutable state the LIVE_STREAM callback writes into -- the
    # callback runs on MediaPipe's own thread, not this loop, so results are
    # picked up on the next frame we render rather than awaited directly.
    latest = {"hand_seen": False, "angle": None, "openness": None}
    _debug_logged = {"once": False}

    def _on_result(result, output_image, timestamp_ms):
        try:
            lm = getattr(result, "hand_landmarks", None)
            if lm:
                hand = lm[0]
                latest["hand_seen"] = True
                latest["angle"] = _hand_angle_deg(hand)
                latest["openness"] = _hand_openness(hand)
                if not _debug_logged["once"]:
                    _debug_logged["once"] = True
                    print(f"[Gesture] debug: angle={latest['angle']:.1f} "
                          f"openness={latest['openness']:.2f}")
            else:
                latest["hand_seen"] = False
                latest["angle"] = None
                latest["openness"] = None
        except Exception as e:
            # Never let a callback error silently stop future callbacks.
            print(f"[Gesture] result callback error: {e}")
            latest["hand_seen"] = False
            latest["angle"] = None
            latest["openness"] = None

    options = mp_vision.GestureRecognizerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(_MODEL_PATH)),
        running_mode=mp_vision.RunningMode.LIVE_STREAM,
        num_hands=1,
        min_hand_detection_confidence=_MIN_HAND_CONF,
        min_tracking_confidence=_MIN_TRACK_CONF,
        min_hand_presence_confidence=_MIN_PRESENCE_CONF,
        result_callback=_on_result,
    )

    try:
        recognizer = mp_vision.GestureRecognizer.create_from_options(options)
    except Exception as e:
        _log(player, f"Couldn't start the gesture recognizer: {e}")
        _STATE["running"] = False
        return

    # Open the camera at its own default mode -- that's the configuration
    # the manufacturer validated as reliable. Forcing a specific resolution/
    # FPS previously backfired by exceeding what some webcams can sustain
    # over USB, degrading the actual image instead of failing outright.
    cap = cv2.VideoCapture(_STATE["cap_index"])
    if not cap.isOpened():
        _log(player, "Couldn't open the camera for gesture control.")
        recognizer.close()
        _STATE["running"] = False
        return

    default_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    default_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    if default_w < 1280:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        ok, test_frame = cap.read()
        if not ok or test_frame is None or test_frame.mean() < 5:
            _log(player, "720p request produced a bad frame -- reverting to "
                         "the camera's default resolution.")
            cap.set(cv2.CAP_PROP_FRAME_WIDTH, default_w)
            cap.set(cv2.CAP_PROP_FRAME_HEIGHT, default_h)

    actual_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    actual_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))
    actual_fps = cap.get(cv2.CAP_PROP_FPS)
    _log(player, f"Gesture control camera started ({actual_w}x{actual_h} "
                 f"@ {actual_fps:.0f}fps).")

    # Small preview window with live status text, so it's visibly obvious the
    # camera is on and actually seeing your hand -- pure background tracking
    # with no feedback made it impossible to tell whether it was working.
    window_name = "Parker - Gesture Control (press Q or say 'turn off gesture control' to stop)"
    show_preview = _STATE.get("show_preview", True)

    # CLAHE (contrast-limited adaptive histogram equalization) boosts local
    # contrast in dim/backlit frames -- only applied when the frame is
    # actually dim, so it costs nothing when lighting is already fine.
    _clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))

    flash_until = 0.0
    flash_label = ""
    start_time = time.monotonic()

    # Rolling (timestamp, angle) samples for twist detection.
    angle_history: list[tuple[float, float]] = []
    last_twist = 0.0

    # Volume state: smoothed openness (to avoid jittery volume changes from
    # per-frame landmark noise) and the last percent we actually applied.
    smoothed_openness: float | None = None
    last_volume_pct: int | None = None
    last_volume_update = 0.0

    try:
        while _STATE["running"]:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue

            frame = cv2.flip(frame, 1)  # mirror, so it feels natural

            gray_mean = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean()
            if gray_mean < 100:
                lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
                l, a, b = cv2.split(lab)
                l = _clahe.apply(l)
                enhanced = cv2.merge((l, a, b))
                detect_frame = cv2.cvtColor(enhanced, cv2.COLOR_LAB2BGR)
            else:
                detect_frame = frame

            rgb = cv2.cvtColor(detect_frame, cv2.COLOR_BGR2RGB)
            mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
            ts_ms = int((time.monotonic() - start_time) * 1000)
            recognizer.recognize_async(mp_image, ts_ms)

            now = time.monotonic()
            hand_seen = latest["hand_seen"]
            angle = latest["angle"]
            openness = latest["openness"]

            # --- Wrist twist: track hand-relative angle over a short window,
            # fire on a fast large swing in either direction. ---
            if hand_seen and angle is not None:
                angle_history.append((now, angle))
            angle_history[:] = [(t, a) for t, a in angle_history if now - t <= _TWIST_WINDOW_S]

            if (now - last_twist) > _TWIST_COOLDOWN_S and len(angle_history) >= 2:
                angles = [a for _, a in angle_history]
                d_angle = angles[-1] - angles[0]
                if abs(d_angle) >= _TWIST_MIN_DELTA_DEG:
                    last_twist = now
                    angle_history.clear()
                    if d_angle > 0:
                        flash_label = "NEXT"
                        action_fn, action_name = next_track, "next track"
                    else:
                        flash_label = "PREVIOUS"
                        action_fn, action_name = prev_track, "previous track"
                    flash_until = now + 0.4
                    try:
                        action_fn()
                        _log(player, f"Wrist twist detected -- {action_name}.")
                    except Exception as e:
                        _log(player, f"Twist detected but {action_name} failed: {e}")

            # --- Openness -> volume: smooth the raw value, then map to 0-100
            # and only push volume_set() when it actually changes and not too
            # often (avoids hammering the OS volume API every frame). ---
            if hand_seen and openness is not None:
                if smoothed_openness is None:
                    smoothed_openness = openness
                else:
                    smoothed_openness += _VOLUME_SMOOTHING * (openness - smoothed_openness)

                frac = (smoothed_openness - _OPENNESS_MIN) / (_OPENNESS_MAX - _OPENNESS_MIN)
                frac = max(0.0, min(1.0, frac))
                pct = round(frac * 100)

                if (pct != last_volume_pct
                        and (now - last_volume_update) >= _VOLUME_UPDATE_MIN_INTERVAL_S):
                    last_volume_pct = pct
                    last_volume_update = now
                    try:
                        volume_set(pct)
                    except Exception as e:
                        _log(player, f"Volume control failed: {e}")
            else:
                smoothed_openness = None

            if show_preview:
                h, w = frame.shape[:2]
                if hand_seen:
                    vol_text = f"vol {last_volume_pct}%" if last_volume_pct is not None else "vol ..."
                    label = f"angle {angle:.0f} deg | {vol_text}"
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
