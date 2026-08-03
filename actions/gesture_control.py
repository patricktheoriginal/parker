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
  - Pointing_Up (index finger extended), held briefly on a window -> picks
    that window up; moving your hand while still holding Pointing_Up drags
    it around the screen in real time; dropping the gesture releases it in
    place. While a drag is active, every other gesture is suppressed (a
    dragging hand naturally passes through shapes that look like a fist or
    open palm, and those firing mid-drag would be a bad time to also
    trigger play/pause or a track skip). The picked-up window also becomes
    the target for a follow-up voice command (point_window, in
    computer_settings.py), e.g. "snap that window left" after a quick
    point without dragging.

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

# The window currently "pointed at" (Pointing_Up held), read by
# computer_settings.point_window() when the user issues a dock command by
# voice. Written only by the gesture loop thread, read from the main
# asyncio thread -- a single dict assignment is atomic enough in CPython
# that no lock is needed for this simple last-writer-wins case.
_POINTED = {"hwnd": None, "title": None, "at": 0.0}
_POINT_HOLD_S = 0.35     # how long Pointing_Up must be held before it counts
                          # as a deliberate point rather than a hand passing
                          # through that shape on the way to another gesture
_POINT_STALE_S = 10.0     # a point older than this no longer counts as "the"
                          # target -- avoids acting on a window pointed at
                          # long ago and forgotten about

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

# Re-check whether the frame needs CLAHE brightness boosting only every N
# frames -- room lighting doesn't change fast enough to need a fresh
# full-frame brightness measurement every single frame.
_LIGHT_CHECK_INTERVAL = 15


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


def get_pointed_window():
    """Returns (hwnd, title) of the window last pointed at (Pointing_Up held
    for _POINT_HOLD_S), or (None, None) if nothing was pointed at recently
    (older than _POINT_STALE_S) or gesture control isn't running. Used by
    computer_settings.point_window() to resolve "that window" to an actual
    HWND for a voice-issued dock command."""
    if time.monotonic() - _POINTED["at"] > _POINT_STALE_S:
        return None, None
    return _POINTED["hwnd"], _POINTED["title"]


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
            "volume, thumbs down to lower it, or hold your index finger up "
            "on a window to pick it up and drag it around, or point at it "
            "briefly and tell me to snap it left, right, or center.")


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
            "thumbs up for louder, thumbs down for quieter, or hold your "
            "index finger up on a window to pick it up and drag it, or point "
            "and tell me to snap it left, right, or center.")


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


def _window_under_point(norm_x: float, norm_y: float):
    """Maps a normalized (0-1, 0-1) fingertip position -- treating the whole
    camera frame as a trackpad over the whole virtual screen, no camera/head
    calibration needed -- to a real screen coordinate, then returns the
    (hwnd, title) of the top-level window under that point via Win32's
    WindowFromPoint. Returns (None, None) off Windows or if nothing usable
    is under the point (e.g. the desktop itself). Walks up to the top-level
    ancestor because WindowFromPoint can return a child control (e.g. a
    button) rather than the window we actually want to act on."""
    import platform
    if platform.system() != "Windows":
        return None, None
    try:
        import ctypes
        user32 = ctypes.windll.user32
        # ctypes defaults every windll function to a c_int return type, which
        # truncates 64-bit window handles on 64-bit Windows -- WindowFromPoint
        # and GetAncestor both return HWNDs and need the wider type declared
        # explicitly, or a handle above the 32-bit range comes back mangled
        # and every lookup after it (title, SetForegroundWindow, ...) fails
        # silently against the wrong (or a garbage) window.
        user32.WindowFromPoint.restype = ctypes.c_void_p
        user32.GetAncestor.restype = ctypes.c_void_p
        screen_w = user32.GetSystemMetrics(0)
        screen_h = user32.GetSystemMetrics(1)
        # Mirror horizontally to match the flipped preview frame (flip(1) is
        # applied before landmarks are read), so pointing right on-screen
        # from the user's point of view maps to the right side of the
        # display, not the left.
        px = int((1.0 - norm_x) * screen_w)
        py = int(norm_y * screen_h)
        px = max(0, min(screen_w - 1, px))
        py = max(0, min(screen_h - 1, py))

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        pt = POINT(px, py)
        hwnd = user32.WindowFromPoint(pt)
        if not hwnd:
            return None, None
        # Walk up to the top-level (desktop-owned) ancestor -- GA_ROOT = 2.
        root = user32.GetAncestor(hwnd, 2)
        if root:
            hwnd = root
        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return None, None
        buf = ctypes.create_unicode_buffer(length + 1)
        user32.GetWindowTextW(hwnd, buf, length + 1)
        title = buf.value.strip()
        if not title:
            return None, None
        return hwnd, title
    except Exception as e:
        print(f"[Gesture] window-under-point lookup failed: {e}")
        return None, None


def _move_window_by(hwnd, dx_px: int, dy_px: int) -> bool:
    """Moves a window by a pixel offset, keeping its current size -- used
    every frame while a Pointing_Up drag is in progress. Reads the window's
    current position fresh each call (rather than tracking it locally)
    since GetWindowRect is cheap and this avoids ever drifting out of sync
    with where the window actually is (e.g. if the user also nudged it some
    other way mid-drag). Returns False (and lets the caller drop the drag)
    if the window's gone -- e.g. it was closed while being dragged."""
    import ctypes

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    try:
        user32 = ctypes.windll.user32
        user32.IsWindow.restype = ctypes.c_int
        if not user32.IsWindow(hwnd):
            return False
        rect = RECT()
        if not user32.GetWindowRect(hwnd, ctypes.byref(rect)):
            return False
        w = rect.right - rect.left
        h = rect.bottom - rect.top
        SWP_NOSIZE = 0x0001
        SWP_NOZORDER = 0x0004
        SWP_NOACTIVATE = 0x0010
        user32.SetWindowPos(hwnd, 0, rect.left + dx_px, rect.top + dy_px, w, h,
                            SWP_NOSIZE | SWP_NOZORDER | SWP_NOACTIVATE)
        return True
    except Exception as e:
        print(f"[Gesture] drag move failed: {e}")
        return False


def _run_loop(player) -> None:
    import cv2
    import mediapipe as mp
    from mediapipe.tasks import python as mp_python
    from mediapipe.tasks.python import vision as mp_vision

    from actions.computer_settings import media_playpause, next_track, prev_track, volume_set

    # Shared mutable state the LIVE_STREAM callback writes into -- the
    # callback runs on MediaPipe's own thread, not this loop, so results are
    # picked up on the next frame we render rather than awaited directly.
    latest = {"gesture": None, "score": 0.0, "hand_seen": False, "wrist_x": None,
              "index_tip": None}
    _debug_logged = {"once": False}
    # Set right before each recognize_async() call, cleared by the callback
    # when that result comes back. If it's still True when the next frame is
    # ready, MediaPipe hasn't finished the previous frame yet -- skipping the
    # call in that case (instead of queuing more work on top) is what stops
    # the lag from building up over time rather than staying constant.
    _inference_pending = {"flag": False}

    def _on_result(result, output_image, timestamp_ms):
        _inference_pending["flag"] = False
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
                # Landmark 8 is the index fingertip -- used for Pointing_Up
                # targeting. Same defensive guard as wrist_x above.
                latest["index_tip"] = (lm[0][8].x, lm[0][8].y) if lm else None
                if not _debug_logged["once"]:
                    _debug_logged["once"] = True
                    print(f"[Gesture] debug: gesture={top.category_name} "
                          f"score={top.score:.2f} wrist_x={latest['wrist_x']} "
                          f"hand_landmarks_present={lm is not None}")
            else:
                latest["gesture"] = None
                latest["hand_seen"] = False
                latest["wrist_x"] = None
                latest["index_tip"] = None
        except Exception as e:
            # Never let a callback error silently stop future callbacks.
            print(f"[Gesture] result callback error: {e}")
            latest["gesture"] = None
            latest["hand_seen"] = False
            latest["wrist_x"] = None
            latest["index_tip"] = None

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
    #
    # The backend IS worth forcing though (unlike resolution/FPS): OpenCV's
    # default Windows backend (MSMF) is known to take 1-3+ seconds just to
    # initialize/warm up, especially on the first open after boot or after
    # the camera's been idle -- that's the "camera takes a while to come up"
    # delay. DirectShow (CAP_DSHOW) opens the same camera in its own default
    # mode just as validly, but starts up close to instantly. Falls back to
    # the platform default if DSHOW isn't available (e.g. non-Windows).
    import platform as _platform
    if _platform.system() == "Windows":
        cap = cv2.VideoCapture(_STATE["cap_index"], cv2.CAP_DSHOW)
    else:
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
    # Same idea for Pointing_Up -- how long it's been continuously held.
    point_since: float | None = None
    # Drag state: once Pointing_Up has been held past _POINT_HOLD_S on a
    # window, that window is "picked up" and follows the fingertip until
    # Pointing_Up is released (hand drops the gesture or leaves frame).
    # drag_last_tip is the previous frame's fingertip position, in *pixels*
    # (not normalized 0-1) -- normalized deltas would need re-scaling by
    # screen size every frame anyway, so pixels are computed once per frame
    # and reused directly as the SetWindowPos offset.
    drag_hwnd = None
    drag_last_tip_px: tuple[float, float] | None = None

    # Volume ramp state: current ramped value (float, for smooth per-frame
    # stepping) and when it was last pushed to the OS, plus which direction
    # (if any) is currently held so a stray one-frame gesture flicker doesn't
    # restart the ramp from a freshly re-read system volume every time.
    volume_pct: float | None = None
    volume_direction: str | None = None   # "up" | "down" | None
    last_volume_push = 0.0
    last_volume_pushed_int: int | None = None

    frame_count = 0
    needs_clahe = False   # re-checked every _LIGHT_CHECK_INTERVAL frames only
    target_frame_time = 1.0 / 30.0   # pace the loop to ~30fps of actual work

    # Screen size for mapping the fingertip's normalized position to real
    # pixel coordinates (used by both the point-and-hold target lookup and
    # the drag-move delta calculation). Windows only, matching
    # _window_under_point -- off Windows there's nothing to drag.
    screen_w, screen_h = 0, 0
    if _platform.system() == "Windows":
        try:
            import ctypes
            _user32 = ctypes.windll.user32
            screen_w = _user32.GetSystemMetrics(0)
            screen_h = _user32.GetSystemMetrics(1)
        except Exception:
            pass

    try:
        while _STATE["running"]:
            loop_start = time.monotonic()
            ok, frame = cap.read()
            if not ok:
                time.sleep(0.05)
                continue

            frame = cv2.flip(frame, 1)  # mirror, so it feels natural
            frame_count += 1

            # Room lighting doesn't change frame-to-frame -- re-measuring
            # brightness on every single frame (a full-frame cvtColor) was
            # pure wasted CPU. Check periodically instead.
            if frame_count % _LIGHT_CHECK_INTERVAL == 1:
                needs_clahe = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean() < 100

            if needs_clahe:
                lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
                l, a, b = cv2.split(lab)
                l = _clahe.apply(l)
                enhanced = cv2.merge((l, a, b))
                detect_frame = cv2.cvtColor(enhanced, cv2.COLOR_LAB2BGR)
            else:
                detect_frame = frame

            # Skip starting new inference while the previous frame's result
            # hasn't come back yet, instead of queuing work MediaPipe can't
            # keep up with -- that queuing is what made the camera get
            # progressively laggier over time rather than staying at a
            # steady (if imperfect) frame rate.
            if not _inference_pending["flag"]:
                rgb = cv2.cvtColor(detect_frame, cv2.COLOR_BGR2RGB)
                mp_image = mp.Image(image_format=mp.ImageFormat.SRGB, data=rgb)
                ts_ms = int((time.monotonic() - start_time) * 1000)
                _inference_pending["flag"] = True
                recognizer.recognize_async(mp_image, ts_ms)

            now = time.monotonic()
            gesture = latest["gesture"]
            score = latest["score"]
            wrist_x = latest["wrist_x"]
            confident = score >= _MIN_CONFIDENCE
            cooling_down = (now - last_trigger) <= _COOLDOWN_S

            # --- Pointing_Up: hold on a window to "pick it up", then move
            # your hand to drag it around; releasing the gesture (or losing
            # the hand) drops it in place. Handled before the other gestures
            # and, while a drag is active, suppresses them entirely -- a
            # dragging hand naturally passes through shapes that look like a
            # fist or open palm, and letting those fire mid-drag would
            # trigger play/pause or a track skip in the middle of moving a
            # window. ---
            dragging = drag_hwnd is not None
            if confident and gesture == "Pointing_Up" and latest["index_tip"] is not None:
                tip_x, tip_y = latest["index_tip"]
                tip_px = ((1.0 - tip_x) * screen_w, tip_y * screen_h)  # mirror
                                                                          # to match preview flip, same as _window_under_point
                if point_since is None:
                    point_since = now
                elif not dragging and (now - point_since) >= _POINT_HOLD_S:
                    # Hold duration crossed -- pick up whatever's under the
                    # fingertip right now and start dragging it.
                    hwnd, title = _window_under_point(tip_x, tip_y)
                    if hwnd:
                        drag_hwnd = hwnd
                        drag_last_tip_px = tip_px
                        _POINTED["hwnd"] = hwnd
                        _POINTED["title"] = title
                        _POINTED["at"] = now
                        flash_label = f"DRAGGING: {title[:30]}"
                        flash_until = now + 0.3
                        dragging = True
                elif dragging:
                    dx = tip_px[0] - drag_last_tip_px[0]
                    dy = tip_px[1] - drag_last_tip_px[1]
                    drag_last_tip_px = tip_px
                    if (dx or dy) and not _move_window_by(drag_hwnd, int(dx), int(dy)):
                        # Window's gone (e.g. closed mid-drag) -- drop it.
                        drag_hwnd = None
                        drag_last_tip_px = None
                        dragging = False
                    else:
                        flash_label = "DRAGGING"
                        flash_until = now + 0.2
            else:
                point_since = None
                if dragging:
                    _log(player, "Window drag released.")
                drag_hwnd = None
                drag_last_tip_px = None
                dragging = False

            if dragging:
                # Skip every other gesture this frame -- see comment above.
                elapsed = time.monotonic() - loop_start
                remaining = target_frame_time - elapsed
                if remaining > 0:
                    time.sleep(remaining)
                if show_preview:
                    h, w = frame.shape[:2]
                    cv2.putText(frame, flash_label, (10, 30),
                               cv2.FONT_HERSHEY_SIMPLEX, 0.7, (0, 255, 255), 2)
                    cv2.rectangle(frame, (0, 0), (w - 1, h - 1), (0, 255, 255), 6)
                    cv2.imshow(window_name, frame)
                    if cv2.waitKey(1) & 0xFF == ord("q"):
                        break
                continue

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

            # Pace to ~30fps of actual wall-clock time rather than always
            # sleeping a fixed 20ms on top of however long this iteration's
            # processing took -- if a frame took longer than the target
            # (e.g. CLAHE kicked in, or cvtColor was briefly slow), sleeping
            # a further fixed amount regardless just compounds the delay
            # frame after frame, which is what made the camera feel like it
            # was getting progressively laggier rather than staying steady.
            elapsed = time.monotonic() - loop_start
            remaining = target_frame_time - elapsed
            if remaining > 0:
                time.sleep(remaining)
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
