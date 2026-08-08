"""
build_osm_graph.py -- pre-builds the self-hosted A* driving graph for
Vietnam (see actions/osm_graph.py, actions/astar_route.py).

Run this once, ahead of time, so the first route request doesn't have to
download and parse the ~300MB Vietnam OSM extract inline (that alone can
take several minutes). After this finishes, route_engine.py automatically
picks up the cached graph and tries the self-hosted A* engine before
falling back to GraphHopper/OSRM.

Requires pyrosm, which is NOT in requirements.txt -- it pulls in
geopandas/shapely/pandas, which are only needed for this one-time build
step, not for normal Parker operation (routing itself just uses the
cached graph + numpy/heapq). Install it separately:

    pip install pyrosm

Usage:
    python tools/build_osm_graph.py
    python tools/build_osm_graph.py --region "Ho Chi Minh"   # smaller/faster
                                                              # test region

Expect several minutes and roughly 1-1.5GB of free disk space for the
whole-Vietnam build (the .osm.pbf download plus the parsed/cached graph).
"""

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from actions.osm_graph import load_or_build_graph, graph_cache_exists, _GRAPH_CACHE


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--region", default="Vietnam",
                        help="pyrosm region name (default: Vietnam). Use a "
                             "smaller region like 'Ho Chi Minh' for a faster "
                             "test build.")
    args = parser.parse_args()

    try:
        import pyrosm  # noqa: F401
    except ImportError:
        print("pyrosm isn't installed. Run: pip install pyrosm")
        sys.exit(1)

    if graph_cache_exists():
        print(f"A graph is already cached at {_GRAPH_CACHE} -- delete it "
             "first if you want to rebuild (e.g. for a different region).")
        sys.exit(0)

    graph = load_or_build_graph(args.region)
    print(f"Done. Cached to {_GRAPH_CACHE}")
    print(f"{len(graph.adj)} nodes, "
         f"{sum(len(edges) for edges in graph.adj.values())} directed edges.")


if __name__ == "__main__":
    main()
