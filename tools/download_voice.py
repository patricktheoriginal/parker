"""
download_voice.py — Download the Piper neural voice for Parker's offline mode.

Run once WHILE ONLINE:
    python tools/download_voice.py                 # default: en_US-ryan-medium
    python tools/download_voice.py en_GB-alan-medium

Downloads the voice into  ~/.parker/piper/  so Parker uses the natural Piper
voice offline instead of the robotic OS voice. Tries piper's own downloader
first, then falls back to a direct download from Hugging Face.
"""
import sys
import urllib.request
from pathlib import Path

VOICE = sys.argv[1] if len(sys.argv) > 1 else "en_US-ryan-medium"
DEST = Path.home() / ".parker" / "piper"

# Hugging Face path is  <lang>/<name>/<quality>/<full-name>.onnx
# e.g. en/en_US/ryan/medium/en_US-ryan-medium.onnx
_HF_BASE = "https://huggingface.co/rhasspy/piper-voices/resolve/main"


def _hf_url(voice: str, ext: str) -> str:
    # voice = en_US-ryan-medium  ->  lang_region=en_US, name=ryan, quality=medium
    lang_region, name, quality = voice.split("-", 2)
    lang = lang_region.split("_")[0]
    return f"{_HF_BASE}/{lang}/{lang_region}/{name}/{quality}/{voice}.onnx{ext}?download=true"


def _download(url: str, path: Path) -> None:
    print(f"    → {path.name}")
    req = urllib.request.Request(url, headers={"User-Agent": "Parker/1.0"})
    with urllib.request.urlopen(req, timeout=120) as r, open(path, "wb") as f:
        f.write(r.read())


def main() -> None:
    DEST.mkdir(parents=True, exist_ok=True)
    onnx = DEST / f"{VOICE}.onnx"
    cfg = DEST / f"{VOICE}.onnx.json"

    if onnx.exists() and cfg.exists():
        print(f"Voice '{VOICE}' already downloaded at {DEST}. Nothing to do.")
        return

    print(f"Downloading Piper voice '{VOICE}' to {DEST} …")

    # 1) Try piper's built-in downloader.
    try:
        from piper.download_voices import download_voice as _dl
        _dl(VOICE, DEST)
        if onnx.exists():
            print("Done (via piper downloader).")
            return
    except Exception as e:
        print(f"    piper downloader failed ({e}); trying direct download…")

    # 2) Fall back to a direct Hugging Face download.
    try:
        _download(_hf_url(VOICE, ""),      onnx)
        _download(_hf_url(VOICE, ".json"), cfg)
        print("Done (direct download).")
    except Exception as e:
        print(f"Failed to download voice: {e}")
        print("Check your internet connection, or try a different voice name.")
        sys.exit(1)


if __name__ == "__main__":
    main()
