"""
list_models.py — show which Gemini models YOUR API key can actually use.

Run this whenever Parker crashes with "model ... is no longer available" or
"audio content type not supported" — Google retires preview models, and this
prints the live/text models your key currently has access to, so you can pick
the right names for main.py.

Usage:
    python tools/list_models.py
"""

import json
import sys
import urllib.request
from pathlib import Path

CFG = Path(__file__).resolve().parent.parent / "config" / "api_keys.json"


def main() -> None:
    try:
        key = json.loads(CFG.read_text(encoding="utf-8"))["gemini_api_key"]
    except Exception as e:
        print(f"Couldn't read gemini_api_key from {CFG}: {e}")
        sys.exit(1)

    url = ("https://generativelanguage.googleapis.com/v1beta/models"
           f"?key={key}&pageSize=1000")
    try:
        data = json.loads(urllib.request.urlopen(url, timeout=20).read())
    except Exception as e:
        print(f"Request failed: {e}")
        sys.exit(1)

    models = data.get("models", [])

    print("=== LIVE models (voice — supportedGenerationMethods: "
          "bidiGenerateContent) ===")
    live = [m["name"] for m in models
            if "bidiGenerateContent" in m.get("supportedGenerationMethods", [])]
    for n in live:
        print("  ", n)
    if not live:
        print("   (none — your key may not have Live API access)")

    print("\n=== TEXT models (generateContent) ===")
    for m in models:
        if "generateContent" in m.get("supportedGenerationMethods", []):
            print("  ", m["name"])

    print(f"\nTotal models visible to this key: {len(models)}")
    print("\nPick a LIVE model for LIVE_MODEL in main.py, and a TEXT model for "
          "the gemini-2.5-flash references.")


if __name__ == "__main__":
    main()
