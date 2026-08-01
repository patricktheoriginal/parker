"""
test_offline_voice.py — Try Parker's OFFLINE VOICE loop standalone.

Speak to Parker with no internet: Mic → Whisper → local model → OS voice.

Run from the project root:
    python tools/test_offline_voice.py

Requirements:
    pip install -r requirements-offline.txt
    Ollama running with a model pulled (e.g. 'ollama pull llama3.2:3b')

Say "stop listening" or press Ctrl+C to quit.
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.offline_agent import offline_available, offline_respond, ensure_ollama_running
from core.offline_voice import OfflineVoice


def main() -> None:
    print("=== Parker offline VOICE test ===")
    if not ensure_ollama_running() or not offline_available():
        print("Ollama not reachable. Install from https://ollama.com, run 'ollama serve',")
        print("and pull a model: ollama pull llama3.2:3b")
        return

    stop = {"flag": False}

    def respond(text, history):
        if "stop listening" in text.lower():
            stop["flag"] = True
            return "Stopping offline voice. Goodbye, sir."
        return offline_respond(text, history=history)

    voice = OfflineVoice(
        respond_fn=respond,
        on_state=lambda s: None,
        on_log=lambda m: print(f"[voice] {m}"),
        whisper_model="base",
    )
    print("Loading models and starting microphone… (first run downloads Whisper ~75 MB)")
    voice.start()
    try:
        while voice.is_running() and not stop["flag"]:
            time.sleep(0.3)
    except KeyboardInterrupt:
        pass
    finally:
        voice.stop()
        print("Stopped.")


if __name__ == "__main__":
    main()
