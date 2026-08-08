"""
osm_graph.py -- builds and caches a driving-road graph of Vietnam from
OpenStreetMap data, for actions/astar_route.py's self-hosted A* router.

Pipeline: pyrosm downloads the Geofabrik Vietnam extract (~300MB .osm.pbf,
cached by pyrosm itself under its own data dir) once, then get_network()
extracts the drivable road network as a GeoDataFrame of edges. This module
converts that into a plain adjacency-list graph (dict of node_id -> list of
(neighbor_id, weight_seconds)) and pickles it to ~/.parker/osm/ so the
~1-2 million node/edge Vietnam network is only parsed from the GeoDataFrame
once, not on every Parker startup.

Building the graph the first time is slow (the .pbf download plus
GeoDataFrame parsing can take several minutes) -- see tools/build_osm_graph.py
for a standalone script to run this ahead of time instead of blocking the
first route request.
"""

import pickle
import time
from pathlib import Path

_CACHE_DIR = Path.home() / ".parker" / "osm"
_GRAPH_CACHE = _CACHE_DIR / "vietnam_driving_graph.pkl"

# Average speed (km/h) assumed per OSM highway tag, used to turn edge length
# (meters, which pyrosm precomputes) into a travel-time weight -- A* over
# pure distance would route through narrow alleys just as happily as a
# highway; weighting by an estimated travel time makes the shortest PATH
# and the fastest ROUTE the same thing, matching what OSRM/GraphHopper
# actually optimize for.
_SPEED_KMH = {
    "motorway": 90, "motorway_link": 60,
    "trunk": 70, "trunk_link": 50,
    "primary": 55, "primary_link": 40,
    "secondary": 45, "secondary_link": 35,
    "tertiary": 35, "tertiary_link": 30,
    "unclassified": 25, "residential": 25,
    "living_street": 15, "service": 15,
}
_DEFAULT_SPEED_KMH = 25.0

# Highway tags that aren't drivable -- excluded from the graph entirely.
_NON_DRIVING = {
    "footway", "path", "steps", "pedestrian", "cycleway", "bridleway",
    "corridor", "elevator", "platform", "proposed", "construction",
    "raceway", "track",
}


class Graph:
    """Plain adjacency-list road graph: node_id -> [(neighbor_id, weight_s)].
    Also keeps node_id -> (lat, lon) so astar_route.py can compute the
    heuristic and snap arbitrary (lat, lon) query points to the nearest node."""

    __slots__ = ("adj", "coords")

    def __init__(self):
        self.adj: dict[int, list[tuple[int, float]]] = {}
        self.coords: dict[int, tuple[float, float]] = {}

    def add_edge(self, u: int, v: int, weight_s: float, oneway: bool):
        self.adj.setdefault(u, []).append((v, weight_s))
        if not oneway:
            self.adj.setdefault(v, []).append((u, weight_s))
        # Ensure both endpoints exist in adj even if this is their only edge
        # in that direction, so lookups don't need a .get(..., []) everywhere.
        self.adj.setdefault(v, self.adj.get(v, []))


def _edge_weight_seconds(length_m: float, highway: str) -> float:
    speed = _SPEED_KMH.get(highway, _DEFAULT_SPEED_KMH)
    speed_m_s = speed * 1000.0 / 3600.0
    return length_m / speed_m_s


def _is_oneway(value) -> bool:
    # OSM 'oneway' values seen in practice: 'yes', '1', 'true' (forward),
    # '-1' (reverse -- pyrosm/osmium already normalize direction into u/v
    # order for -1 in most extracts, but treat it as oneway either way since
    # we don't reverse the edge here), everything else (None, 'no', '0') is
    # two-way.
    if value is None:
        return False
    s = str(value).strip().lower()
    return s in ("yes", "1", "true", "-1")


def build_graph(region: str = "Vietnam", force: bool = False,
                progress=print) -> Graph:
    """Downloads (if needed) the OSM extract for `region`, extracts the
    driving network, and returns a Graph. Does NOT use the on-disk pickle
    cache -- call load_or_build_graph() for that. Slow: expect several
    minutes on first run (download + parse of a few hundred MB)."""
    import pyrosm

    progress(f"[OSM] Fetching {region} OSM extract (cached by pyrosm after "
             f"the first download)...")
    fp = pyrosm.get_data(region)

    progress("[OSM] Parsing driving network from the extract (this is the "
             "slow part -- can take a few minutes for a whole country)...")
    osm = pyrosm.OSM(fp)
    nodes, edges = osm.get_network(network_type="driving", nodes=True)

    if edges is None or len(edges) == 0:
        raise RuntimeError(f"pyrosm returned no drivable edges for {region!r} "
                           "-- the extract may be empty or region name wrong.")

    graph = Graph()

    # Node coordinates. pyrosm's nodes GeoDataFrame has an 'id' column and a
    # geometry point -- read lat/lon from geometry.y/geometry.x rather than
    # assuming separate lat/lon columns exist, since that varies by version.
    progress(f"[OSM] Indexing {len(nodes)} nodes...")
    for row in nodes.itertuples(index=False):
        node_id = int(row.id)
        geom = row.geometry
        graph.coords[node_id] = (geom.y, geom.x)   # (lat, lon)

    # Edges. pyrosm's get_network(network_type=...) precomputes 'u'/'v' node
    # id columns and a 'length' column (meters) specifically for graph
    # building -- confirmed present across pyrosm's documented network
    # outputs. 'highway' and 'oneway' are raw OSM tag values and can be
    # missing/None on some ways, handled with .get()-style defaults below.
    progress(f"[OSM] Building graph from {len(edges)} edges...")
    skipped = 0
    for row in edges.itertuples(index=False):
        highway = getattr(row, "highway", None)
        if highway in _NON_DRIVING:
            skipped += 1
            continue
        u = getattr(row, "u", None)
        v = getattr(row, "v", None)
        length_m = getattr(row, "length", None)
        if u is None or v is None or not length_m or length_m <= 0:
            skipped += 1
            continue
        weight_s = _edge_weight_seconds(float(length_m), highway or "")
        graph.add_edge(int(u), int(v), weight_s, _is_oneway(getattr(row, "oneway", None)))

    progress(f"[OSM] Graph built: {len(graph.adj)} nodes with edges, "
             f"{skipped} non-drivable/invalid edges skipped.")
    return graph


def load_or_build_graph(region: str = "Vietnam", progress=print) -> Graph:
    """Returns the cached graph if present, else builds and caches it.
    This is the normal entry point -- build_graph() is only called directly
    by the standalone pre-build script (tools/build_osm_graph.py)."""
    if _GRAPH_CACHE.exists():
        progress(f"[OSM] Loading cached graph from {_GRAPH_CACHE}...")
        t0 = time.monotonic()
        with open(_GRAPH_CACHE, "rb") as f:
            graph = pickle.load(f)
        progress(f"[OSM] Loaded {len(graph.adj)} nodes in "
                 f"{time.monotonic() - t0:.1f}s.")
        return graph

    graph = build_graph(region, progress=progress)
    _CACHE_DIR.mkdir(parents=True, exist_ok=True)
    with open(_GRAPH_CACHE, "wb") as f:
        pickle.dump(graph, f, protocol=pickle.HIGHEST_PROTOCOL)
    progress(f"[OSM] Cached graph to {_GRAPH_CACHE} for future runs.")
    return graph


def graph_cache_exists() -> bool:
    return _GRAPH_CACHE.exists()
