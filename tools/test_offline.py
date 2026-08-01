"""
test_offline.py — Try Parker's offline brain (local Ollama model) directly.

Run from the project root:
    python tools/test_offline.py "What is my CPU usage?"
    python tools/test_offline.py            # interactive chat

Requirements:
  - Ollama installed and running (the script will try to start it)
  - A model pulled, e.g.:  ollama pull llama3.2:3b
"""
import sys
from pathlib import Path

# Allow running from anywhere
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.llm_client import list_ollama_models, pick_best_model, ensure_ollama_running
from core.offline_agent import offline_respond


def main() -> None:
    print("=== Parker offline test ===")
    if not ensure_ollama_running():
        print("Ollama is not reachable. Install it from https://ollama.com and run 'ollama serve'.")
        return
    models = list_ollama_models()
    print("Installed models:", models or "(none — run 'ollama pull llama3.2:3b')")
    print("Selected model  :", pick_best_model() or "(default)")
    if not models:
        return

    args = sys.argv[1:]
    if args:
        q = " ".join(args)
        print(f"\nYou: {q}")
        print("Parker:", offline_respond(q))
        return

    print("\nInteractive mode — type 'quit' to exit.")
    history: list = []
    while True:
        try:
            q = input("You: ").strip()
        except (EOFError, KeyboardInterrupt):
            break
        if q.lower() in ("quit", "exit", "q"):
            break
        if not q:
            continue
        reply = offline_respond(q, history=history)
        history.append({"role": "user", "content": q})
        history.append({"role": "assistant", "content": reply})
        history[:] = history[-12:]
        print("Parker:", reply)


if __name__ == "__main__":
    main()
