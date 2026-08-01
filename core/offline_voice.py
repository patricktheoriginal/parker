"""
offline_voice.py — fully offline voice loop for Parker.

Mic → energy VAD → Whisper STT → offline agent (Ollama) → pyttsx3 TTS → speaker.

No internet needed once the Whisper model is cached. Runs in its own thread and
is started by main.py only while Parker is offline (no Gemini session). Stops
cleanly when the cloud session comes back.

Dependencies (installed via requirements-offline.txt):
  - faster-whisper   (STT)
  - pyttsx3          (offline TTS via the OS voice)
  - sounddevice, numpy  (already required)
"""

import threading
import time
import queue

import numpy as np

SAMPLE_RATE = 16000          # Whisper expects 16 kHz mono
_FRAME_MS   = 30
_FRAME      = SAMPLE_RATE * _FRAME_MS // 1000     # samples per frame


import os
from pathlib import Path

# A natural male neural voice (Piper) that's closer to the online 'Charon' feel.
_PIPER_VOICE = os.environ.get("PARKER_PIPER_VOICE", "en_US-ryan-medium")
_PIPER_DIR = Path.home() / ".parker" / "piper"


class _TTS:
    """Offline text-to-speech.

    Prefers Piper (neural, natural male voice — the closest offline match to the
    online Charon voice). Falls back to pyttsx3 (OS voice) if Piper or its voice
    model isn't available.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._piper = None          # PiperVoice, or False if unavailable
        self._engine = None         # pyttsx3 fallback

    # ── Piper (preferred) ────────────────────────────────────────────────────
    def _load_piper(self):
        if self._piper is not None:
            return self._piper
        try:
            from piper import PiperVoice
        except Exception:
            print("[OfflineVoice] piper-tts not installed → using the robotic OS "
                  "voice. Install it:  pip install piper-tts")
            self._piper = False
            return False
        onnx = _PIPER_DIR / f"{_PIPER_VOICE}.onnx"
        if not onnx.exists():
            print(f"[OfflineVoice] Piper voice not downloaded at {onnx} → using OS "
                  f"voice. Run:  python tools/setup_offline.py")
            self._piper = False
            return False
        try:
            self._piper = PiperVoice.load(str(onnx))
            print(f"[OfflineVoice] Using Piper neural voice: {_PIPER_VOICE}")
            return self._piper
        except Exception as e:
            print(f"[OfflineVoice] Piper failed to load ({e}); using OS voice.")
            self._piper = False
            return False

    def _speak_piper(self, text: str) -> bool:
        voice = self._load_piper()
        if not voice:
            return False
        try:
            import io, wave
            import numpy as _np
            import sounddevice as _sd
            buf = io.BytesIO()
            with wave.open(buf, "wb") as wf:
                voice.synthesize_wav(text, wf)
            buf.seek(0)
            with wave.open(buf, "rb") as wf:
                sr = wf.getframerate()
                frames = wf.readframes(wf.getnframes())
            audio = _np.frombuffer(frames, dtype=_np.int16)
            _sd.play(audio, sr)
            _sd.wait()
            return True
        except Exception as e:
            print(f"[OfflineVoice] Piper playback failed ({e}); using OS voice.")
            return False

    # ── pyttsx3 (fallback) ───────────────────────────────────────────────────
    def _ensure_engine(self):
        if self._engine is None:
            import pyttsx3
            self._engine = pyttsx3.init()
            self._engine.setProperty("rate", 178)
            # Prefer a male voice if the OS offers one.
            try:
                for v in self._engine.getProperty("voices"):
                    if "male" in (getattr(v, "gender", "") or "").lower() or \
                       any(k in v.name.lower() for k in ("david", "mark", "james", "male")):
                        self._engine.setProperty("voice", v.id)
                        break
            except Exception:
                pass

    def _speak_pyttsx3(self, text: str):
        try:
            self._ensure_engine()
            self._engine.say(text)
            self._engine.runAndWait()
        except Exception as e:
            print(f"[OfflineVoice] TTS error: {e}")

    def speak(self, text: str):
        if not text or not text.strip():
            return
        with self._lock:
            if not self._speak_piper(text):
                self._speak_pyttsx3(text)


class OfflineVoice:
    """Background offline voice assistant loop."""

    def __init__(self, respond_fn, on_state=None, on_log=None,
                 whisper_model: str = "base"):
        """
        respond_fn(text, history) -> str   : the offline brain (offline_respond)
        on_state(state:str)                : optional UI state callback
        on_log(msg:str)                    : optional UI log callback
        """
        self._respond = respond_fn
        self._on_state = on_state or (lambda s: None)
        self._on_log = on_log or (lambda m: None)
        self._whisper_name = whisper_model

        self._stt = None
        self._tts = _TTS()
        self._history: list = []

        self._running = False
        self._speaking = False          # true while Parker is talking (mute mic)
        self._thread = None

        # VAD tuning
        self._silence_ms = 700          # end of utterance after this much silence
        self._min_speech_ms = 300       # ignore blips shorter than this
        self._energy_thresh = 0.010     # RMS threshold for "speech" (auto-calibrated)

    def announce(self, text: str) -> None:
        """Speak a one-off line (e.g. 'switching to offline mode') with the
        offline TTS, muting the mic while it plays."""
        was_speaking = self._speaking
        self._speaking = True
        try:
            self._on_state("SPEAKING")
            self._tts.speak(text)
        except Exception as e:
            print(f"[OfflineVoice] announce failed: {e}")
        finally:
            self._speaking = was_speaking
            if self._running:
                self._on_state("LISTENING")

    # ── lifecycle ────────────────────────────────────────────────────────────
    def start(self):
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._running = False

    def is_running(self) -> bool:
        return self._running and self._thread is not None and self._thread.is_alive()

    # ── main loop ────────────────────────────────────────────────────────────
    def _load_stt(self) -> bool:
        try:
            from core.stt import WhisperSTT
            self._on_log(f"Loading Whisper '{self._whisper_name}' (offline STT)…")
            self._stt = WhisperSTT(self._whisper_name, language="en")
            return True
        except Exception as e:
            msg = str(e).lower()
            # The Whisper model isn't cached yet and we're offline → can't download.
            if any(k in msg for k in ("getaddrinfo", "connect", "internet",
                                       "hub", "snapshot", "offline", "resolve")):
                self._on_log(
                    "Offline voice needs the Whisper model, which isn't downloaded "
                    "yet. Connect to the internet once and run: "
                    "python -c \"from faster_whisper import WhisperModel; "
                    "WhisperModel('base')\"  — then it works fully offline.")
            else:
                self._on_log(f"Offline STT unavailable: {e}")
            return False

    def _run(self):
        if not self._load_stt():
            self._running = False
            return

        try:
            import sounddevice as sd
        except Exception as e:
            self._on_log(f"Microphone unavailable: {e}")
            self._running = False
            return

        frames_q: "queue.Queue[np.ndarray]" = queue.Queue()

        def _cb(indata, _frames, _t, _status):
            if not self._speaking:                # ignore mic while Parker talks
                frames_q.put(indata[:, 0].copy())

        self._on_log("Offline voice ready — speak now.")
        self._on_state("LISTENING")

        speech: list = []
        silence_ms = 0
        speaking = False

        try:
            with sd.InputStream(samplerate=SAMPLE_RATE, channels=1,
                                blocksize=_FRAME, dtype="float32", callback=_cb):
                while self._running:
                    try:
                        frame = frames_q.get(timeout=0.5)
                    except queue.Empty:
                        continue

                    rms = float(np.sqrt(np.mean(frame ** 2)) + 1e-9)
                    is_speech = rms > self._energy_thresh

                    if is_speech:
                        speech.append(frame)
                        silence_ms = 0
                        if not speaking:
                            speaking = True
                            self._on_state("LISTENING")
                    elif speaking:
                        speech.append(frame)
                        silence_ms += _FRAME_MS
                        if silence_ms >= self._silence_ms:
                            # End of utterance → process
                            audio = np.concatenate(speech)
                            speech, silence_ms, speaking = [], 0, False
                            dur_ms = len(audio) / SAMPLE_RATE * 1000
                            if dur_ms >= self._min_speech_ms:
                                self._process(audio)
        except Exception as e:
            msg = str(e)
            if "device" in msg.lower() or "invalid" in msg.lower():
                self._on_log("No microphone available for offline voice — "
                             "you can still TYPE commands to the local model.")
            else:
                self._on_log(f"Offline voice loop error: {e}")
        finally:
            self._running = False
            self._on_state("SLEEPING")

    def _process(self, audio: np.ndarray):
        self._on_state("THINKING")
        try:
            text = self._stt.transcribe(audio).strip()
        except Exception as e:
            self._on_log(f"STT error: {e}")
            self._on_state("LISTENING")
            return
        if not text or len(text) < 2:
            self._on_state("LISTENING")
            return

        self._on_log(f"You (offline): {text}")
        try:
            reply = self._respond(text, self._history)
        except Exception as e:
            reply = f"Offline error: {e}"
        self._history.append({"role": "user", "content": text})
        self._history.append({"role": "assistant", "content": reply})
        self._history[:] = self._history[-12:]

        self._on_log(f"Parker (offline): {reply}")
        self._speaking = True
        self._on_state("SPEAKING")
        try:
            self._tts.speak(reply)
        finally:
            self._speaking = False
            self._on_state("LISTENING")
