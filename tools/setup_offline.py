"""
setup_offline.py — Prepare Parker for fully offline use.

Run this ONCE while you have internet. It downloads and caches everything the
offline mode needs, so afterwards Parker works with no connection.

    python tools/setup_offline.py

Steps:
  1. Check faster-whisper + pyttsx3 are installed.
  2. Download the Whisper speech model (~75 MB for 'base').
  3. Check Ollama is running and a model is pulled.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    print("=== Parker offline setup ===\n")

    # 1. Dependencies
    missing = []
    try:
        import faster_whisper  # noqa: F401
    except Exception:
        missing.append("faster-whisper")
    try:
        import pyttsx3  # noqa: F401
    except Exception:
        missing.append("pyttsx3")
    have_piper = True
    try:
        import piper  # noqa: F401
    except Exception:
        have_piper = False
    if missing:
        print("Missing packages:", ", ".join(missing))
        print("Install them with:\n    pip install -r requirements-offline.txt\n")
        return
    print("[1/4] faster-whisper and pyttsx3 installed ✓"
          + ("" if have_piper else "  (piper-tts not installed — will use OS voice)"))

    # 1b. Piper neural voice (natural male voice, closer to online Charon)
    if have_piper:
        print("[2/4] Downloading Piper voice 'en_US-ryan-medium' (one-time)…")
        try:
            from pathlib import Path as _P
            from piper.download_voices import download_voice
            d = _P.home() / ".parker" / "piper"
            d.mkdir(parents=True, exist_ok=True)
            if not (d / "en_US-ryan-medium.onnx").exists():
                download_voice("en_US-ryan-medium", d)
            print("      Piper voice cached ✓  (natural offline voice ready)")
        except Exception as e:
            print(f"      Piper voice download failed ({e}); offline will use the OS voice.")
    else:
        print("[2/4] Piper not installed — offline will use the OS voice (pyttsx3).")

    # 2. Whisper model
    model = sys.argv[1] if len(sys.argv) > 1 else "base"
    print(f"[3/4] Downloading Whisper '{model}' speech model (one-time)…")
    try:
        from faster_whisper import WhisperModel
        WhisperModel(model)
        print(f"      Whisper '{model}' cached ✓  (offline STT ready)")
    except Exception as e:
        print(f"      Failed to download Whisper model: {e}")
        print("      Make sure you have internet for this one-time download.")
        return

    # 3. Ollama + model
    print("[4/4] Checking the local language model (Ollama)…")
    try:
        from core.llm_client import ensure_ollama_running, list_ollama_models, pick_best_model
        if not ensure_ollama_running():
            print("      Ollama is not running. Install it from https://ollama.com,")
            print("      then run: ollama pull llama3.2:3b")
            return
        models = list_ollama_models()
        if not models:
            print("      No Ollama models found. Pull one with: ollama pull llama3.2:3b")
            return
        print(f"      Ollama models: {models}")
        print(f"      Parker will use: {pick_best_model()} ✓")
    except Exception as e:
        print(f"      Ollama check failed: {e}")
        return

    print("\nAll set! Parker can now run offline (text and voice).")
    print("Test it with:  python tools/test_offline_voice.py")


if __name__ == "__main__":
    main()
