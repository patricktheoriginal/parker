"""
voice_diagnose.py — Check which OFFLINE voice Parker will actually use.

Run from the project root:
    python tools/voice_diagnose.py

It reports whether Piper (natural neural voice) is available or whether Parker
is falling back to the robotic OS voice, and writes a sample WAV you can play.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

PIPER_VOICE = "en_US-ryan-medium"
PIPER_DIR = Path.home() / ".parker" / "piper"


def main() -> None:
    print("=== Parker offline voice diagnostic ===\n")

    # 1. Is piper-tts installed?
    try:
        from piper import PiperVoice
        print("[1] piper-tts installed ................ YES")
    except Exception as e:
        print("[1] piper-tts installed ................ NO")
        print(f"    -> {e}")
        print("    Fix:  pip install piper-tts")
        print("\nParker will use the robotic OS voice until this is fixed.")
        return

    # 2. Is the voice model downloaded?
    onnx = PIPER_DIR / f"{PIPER_VOICE}.onnx"
    if onnx.exists():
        print(f"[2] Voice model downloaded ............ YES  ({onnx})")
    else:
        print(f"[2] Voice model downloaded ............ NO   (expected {onnx})")
        print("    Fix:  python tools/setup_offline.py   (needs internet once)")
        print("\nParker will use the robotic OS voice until the model is downloaded.")
        return

    # 3. Can Piper load and synthesize?
    try:
        import wave
        voice = PiperVoice.load(str(onnx))
        out = Path("voice_sample_windows.wav")
        with wave.open(str(out), "wb") as wf:
            voice.synthesize_wav(
                "Good evening sir. This is Parker, offline, using the Piper neural voice.",
                wf,
            )
        print(f"[3] Piper synthesis .................... OK   (wrote {out})")
        print(f"\nParker WILL use the natural Piper voice ({PIPER_VOICE}).")
        print(f"Play '{out}' to hear it — it should match the sample you heard earlier.")
    except Exception as e:
        print(f"[3] Piper synthesis .................... FAILED")
        print(f"    -> {e}")
        print("\nParker will fall back to the robotic OS voice.")


if __name__ == "__main__":
    main()
