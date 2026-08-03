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
  - Thumb_Up, held                                                    -> volume rises
    steadily while held, stopping automatically at 100% (holding
    past max just keeps it at 100, doesn't error or wrap)
  - Thumb_Down, held                                                  -> volume falls
    steadily while held, stopping automatically at 0% (holding past
    zero just keeps it at 0)

The swipe direction comes from tracking the wrist landmark's x position
over a short rolling window while an Open_Palm is held -- the gesture
classifier alone can't tell direction, only "hand is open", so direction
is layered on top of it.

Thumb_Up/Thumb_Down ramp volume at a fixed rate per second for as long as
the gesture is held (read the current system volume once via pycaw on
each ramp start, then step it every frame) rather than mapping hand shape
to an absolute level -- this matches the reference behavior of clamping
cleanly at 0/100 instead of continuing to compute an out-of-range target.

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

_MIN_CONFIDENCE = 0.45     # lowered from 0.6 -- a hand at distance/in motion
                            # scores lower even when it's genuinely the right
                            # gesture; the classifier's own confidence is
                            # already fairly conservative
_COOLDOWN_S = 1.2          # ignore further triggers for this long after one fires

# Swipe detection (Open_Palm + fast horizontal move).
_SWIPE_WINDOW_S = 0.8       # widened from 0.6 -- a fast swipe crosses the
                            # frame in well under this, but a wider window
                            # tolerates the classifier needing a couple of
                            # frames to reacquire Open_Palm after motion blur
_SWIPE_MIN_DX = 0.3         # lowered from 0.35 -- at distance a hand's real
                            # swipe covers less of the (wide) frame in
                            # normalized coordinates even for the same
                            # physical arm movement

# Fist-hold detection (Closed_Fist, deliberately NOT moving much, held briefly
# so a fist made in passing while gesturing something else doesn't fire).
_FIST_HOLD_S = 0.25

# Thumb_Up/Thumb_Down volume ramp (percent per second while held).
_VOLUME_RAMP_PCT_PER_S = 40.0
_VOLUME_UPDATE_INTERVAL_S = 0.1   # don't call volume_set() every single frame


def _get_current_volume() -> int:
    """Read the current system volume (0-100) via pycaw. Returns 50 (a
    neutral guess) if it can't be read, so a ramp still has somewhere
    sensible to start from instead of failing outright."""
    try:
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume
        devices = AudioUtilities.GetSpeakers()
        # Newer pycaw wraps the COM device in an AudioDevice object with no
        # .Activate() method -- it exposes the endpoint volume interface
        # directly as .EndpointVolume instead. Try that first; fall back to
        # the old Activate()+cast() path for older pycaw versions.
        vol = getattr(devices, "EndpointVolume", None)
        if vol is None:
            from ctypes import cast, POINTER
            from comtypes import CLSCTX_ALL
            interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
            vol = cast(interface, POINTER(IAudioEndpointVolume))
        return round(vol.GetMasterVolumeLevelScalar() * 100)
    except Exception as e:
        print(f"[Gesture] Couldn't read current volume, assuming 50: {e}")
        return 50


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
            "next track, left for previous track, thumbs up to raise the "
            "volume, thumbs down to lower it.")


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
            "an open hand right for the next track, left for the previous one, "
            "thumbs up for louder, thumbs down for quieter.")


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

    from actions.computer_settings import media_playpause, next_track, prev_track, volume_set

    # Shared mutable state the LIVE_STREAM callback writes into -- the
    # callback runs on MediaPipe's own thread, not this loop, so results are
    # picked up on the next frame we render rather than awaited directly.
    latest = {"gesture": None, "score": 0.0, "hand_seen": False, "wrist_x": None}
    _debug_logged = {"once": False}

    def _on_result(result, output_image, timestamp_ms):
        try:
            if result.gestures:
                top = result.gestures[0][0]   # best gesture for the first hand
                latest["gesture"] = top.category_name
                latest["score"] = top.score
                latest["hand_seen"] = True
                # hand_landmarks holds one list of 21 landmarks per detected
                # hand; landmark 0 is the wrist. Guard defensively -- this
                # field name has moved between MediaPipe releases, and a
                # silent AttributeError here would kill wrist tracking (and
                # therefore swipe direction) while gesture detection kept
                # working fine, which is hard to tell apart from "swipe
                # detection is broken" without this fallback + log.
                lm = getattr(result, "hand_landmarks", None)
                latest["wrist_x"] = lm[0][0].x if lm else None
                if not _debug_logged["once"]:
                    _debug_logged["once"] = True
                    print(f"[Gesture] debug: gesture={top.category_name} "
                          f"score={top.score:.2f} wrist_x={latest['wrist_x']} "
                          f"hand_landmarks_present={lm is not None}")
            else:
                latest["gesture"] = None
                latest["hand_seen"] = False
                latest["wrist_x"] = None
        except Exception as e:
            # Never let a callback error silently stop future callbacks.
            print(f"[Gesture] result callback error: {e}")
            latest["gesture"] = None
            latest["hand_seen"] = False
            latest["wrist_x"] = None

    options = mp_vision.GestureRecognizerOptions(
        base_options=mp_python.BaseOptions(model_asset_path=str(_MODEL_PATH)),
        running_mode=mp_vision.RunningMode.LIVE_STREAM,
        num_hands=1,
        # Lowered from 0.6/0.5 -- a hand a few meters from the camera only
        # covers a small patch of pixels, and the stricter defaults missed it
        # entirely at distance. min_tracking_confidence in particular governs
        # whether MediaPipe keeps following a hand between frames once found;
        # a fast-moving (motion-blurred) hand drops below a high threshold
        # and loses tracking, which is the other half of "fast swipes aren't
        # detected" (the gesture classifier also needs a clean detection each
        # time tracking is lost and has to re-run palm detection from scratch,
        # which is slower and more likely to miss a quick motion).
        min_hand_detection_confidence=0.4,
        min_tracking_confidence=0.3,
        min_hand_presence_confidence=0.4,
        result_callback=_on_result,
    )

    try:
        recognizer = mp_vision.GestureRecognizer.create_from_options(options)
    except Exception as e:
        _log(player, f"Couldn't start the gesture recognizer: {e}")
        _STATE["running"] = False
        return

    # Forcing 1280x720 @ 60fps (a previous change) backfired: many webcams
    # can't sustain that combination over USB and the driver responds by
    # degrading the actual image (blur/dark/dropped frames) rather than
    # failing outright, which made detection worse across the board, not
    # better. Let the camera open at its own default mode -- that's the
    # configuration the manufacturer validated as reliable -- and only ask
    # for 720p at whatever FPS the camera naturally provides at that
    # resolution, without forcing a specific frame rate.
    cap = cv2.VideoCapture(_STATE["cap_index"])
    if not cap.isOpened():
        _log(player, "Couldn't open the camera for gesture control.")
        recognizer.close()
        _STATE["running"] = False
        return

    default_w = int(cap.get(cv2.CAP_PROP_FRAME_WIDTH))
    default_h = int(cap.get(cv2.CAP_PROP_FRAME_HEIGHT))

    # Only step up to 720p if the camera's own default is meaningfully
    # smaller (e.g. 640x480) -- if it already defaults to 720p or higher,
    # leave it alone rather than re-requesting the same or a different mode.
    if default_w < 1280:
        cap.set(cv2.CAP_PROP_FRAME_WIDTH, 1280)
        cap.set(cv2.CAP_PROP_FRAME_HEIGHT, 720)
        # Verify the camera actually delivers a real frame at the new
        # resolution before keeping it -- some drivers report success on
        # cap.set() but then hand back garbage/black frames.
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

    last_trigger = 0.0
    flash_until = 0.0
    flash_label = ""
    start_time = time.monotonic()
    last_frame_time = start_time   # used by the volume ramp's dt calculation
    # CLAHE (contrast-limited adaptive histogram equalization) boosts local
    # contrast in dim/backlit frames, which is a common reason the palm
    # detector misses a hand that's visually a bit dark or low-contrast
    # against the background -- unlike forcing camera FPS/resolution (which
    # backfired by exceeding what the camera driver could sustain), this only
    # transforms the frame already captured and adapts per-frame, so it can't
    # make a good frame worse the way the earlier camera changes did.
    _clahe = cv2.createCLAHE(clipLimit=2.5, tileGridSize=(8, 8))

    # Rolling (timestamp, wrist_x) samples of ANY tracked hand position, used
    # to detect swipe direction/distance once we've also seen Open_Palm
    # recently. Tracking position independently of the per-frame gesture
    # label (rather than clearing history the instant a single frame isn't
    # classified as Open_Palm) matters because a fast swipe motion-blurs the
    # hand, and the classifier can drop out or flicker to a different label
    # for a frame or two mid-swipe -- clearing on every miss meant the
    # history almost never accumulated enough samples to cross the distance
    # threshold, and the swipe silently never fired.
    wrist_history: list[tuple[float, float]] = []
    last_open_palm_seen = 0.0
    # How long a fist has been continuously held, to avoid firing on a fist
    # that flashes past mid-gesture.
    fist_since: float | None = None

    # Volume ramp state: current ramped value (float, for smooth per-frame
    # stepping) and when it was last pushed to the OS, plus which direction
    # (if any) is currently held so a stray one-frame gesture flicker doesn't
    # restart the ramp from a freshly re-read system volume every time.
    volume_pct: float | None = None
    volume_direction: str | None = None   # "up" | "down" | None
    last_volume_push = 0.0
    last_volume_pushed_int: int | None = None

    try:
        while _STATE["running"]:
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue

            frame = cv2.flip(frame, 1)  # mirror, so it feels natural

            # Only apply CLAHE when the frame is actually dim -- on a
            # well-lit frame it does nothing useful and just costs CPU time
            # every single frame for no benefit.
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
            gesture = latest["gesture"]
            score = latest["score"]
            wrist_x = latest["wrist_x"]
            confident = score >= _MIN_CONFIDENCE
            cooling_down = (now - last_trigger) <= _COOLDOWN_S

            # --- Open_Palm swipe: track wrist x continuously whenever a hand
            # is visible (regardless of the per-frame gesture label -- see
            # the comment on wrist_history above), and require Open_Palm to
            # have been seen recently as confirmation this is a deliberate
            # palm swipe rather than incidental hand movement. ---
            if confident and gesture == "Open_Palm":
                last_open_palm_seen = now
            if wrist_x is not None:
                wrist_history.append((now, wrist_x))
            wrist_history[:] = [(t, x) for t, x in wrist_history if now - t <= _SWIPE_WINDOW_S]
            palm_recent = (now - last_open_palm_seen) <= _SWIPE_WINDOW_S

            if not cooling_down and palm_recent and len(wrist_history) >= 2:
                xs = [x for _, x in wrist_history]
                dx = xs[-1] - xs[0]   # signed: positive = moved right
                if abs(dx) >= _SWIPE_MIN_DX:
                    last_trigger = now
                    wrist_history.clear()
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

            # --- Thumb_Up/Thumb_Down: ramp volume up/down at a fixed rate
            # while held, clamping cleanly at 100/0 instead of continuing to
            # compute past the range. Fist/swipe cooldown doesn't apply here
            # -- this is a continuous hold-to-change control, not a discrete
            # one-shot trigger. ---
            wants_direction = None
            if confident and gesture == "Thumb_Up":
                wants_direction = "up"
            elif confident and gesture == "Thumb_Down":
                wants_direction = "down"

            if wants_direction:
                if volume_direction != wants_direction:
                    # Starting a fresh ramp (or switching direction) -- seed
                    # from the real current system volume so the first frame
                    # doesn't jump from a stale/unrelated value.
                    volume_direction = wants_direction
                    volume_pct = float(_get_current_volume())
                    last_frame_time = now
                else:
                    dt = now - last_frame_time
                    step = _VOLUME_RAMP_PCT_PER_S * dt
                    volume_pct += step if wants_direction == "up" else -step
                    volume_pct = max(0.0, min(100.0, volume_pct))
                last_frame_time = now

                if (now - last_volume_push) >= _VOLUME_UPDATE_INTERVAL_S:
                    rounded = round(volume_pct)
                    if rounded != last_volume_pushed_int:
                        last_volume_pushed_int = rounded
                        try:
                            volume_set(rounded)
                        except Exception as e:
                            _log(player, f"Volume control failed: {e}")
                    last_volume_push = now
                flash_label = f"VOLUME {round(volume_pct)}%"
                flash_until = now + 0.2
            else:
                volume_direction = None

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
