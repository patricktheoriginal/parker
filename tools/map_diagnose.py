"""
map_diagnose.py — Check why the 3D route map may not open in Parker.

Run from the project root:
    python tools/map_diagnose.py

Reports whether PyQt6-WebEngine is installed (needed to show the map INSIDE
Parker), builds a sample route map, and tries to open it.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


def main() -> None:
    print("=== Parker 3D map diagnostic ===\n")

    # 1. WebEngine?
    have_web = True
    try:
        from PyQt6.QtWebEngineWidgets import QWebEngineView  # noqa: F401
        print("[1] PyQt6-WebEngine installed ......... YES  (map shows inside Parker)")
    except Exception as e:
        have_web = False
        print("[1] PyQt6-WebEngine installed ......... NO   (map opens in a browser)")
        print(f"    -> {e}")
        print("    Fix:  pip install PyQt6-WebEngine")

    # 2. Build a sample map from a real route.
    print("\n[2] Building a sample route map…")
    try:
        import memory.config_manager as cm
        from actions.route_engine import compute_routes, render_map
        routes = compute_routes((21.0245, 105.8412), (20.2581, 105.9797),
                                "Hanoi", "Ninh Binh")
        if not routes:
            print("    Could not compute a route (no internet, or OSRM down).")
            return
        path = render_map(player=None)
        print(f"    Map written to: {path}")
    except Exception as e:
        print(f"    Failed to build the map: {e}")
        return

    # 3. Try to open it in the browser (proves the HTML/URL is valid).
    print("\n[3] Opening the map in your browser to verify it renders…")
    try:
        import webbrowser
        webbrowser.open(Path(path).as_uri())
        print("    Opened. If you see a map with a route line, the map works.")
    except Exception as e:
        print(f"    Could not open: {e}")

    if have_web:
        print("\nInside Parker, the map appears in the content panel below the HUD.")
    else:
        print("\nInstall PyQt6-WebEngine to see the map INSIDE Parker instead of a browser.")


if __name__ == "__main__":
    main()
