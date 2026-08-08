"""
astar_route.py -- self-hosted A* shortest-path routing over the Vietnam
driving graph built by actions/osm_graph.py, as an alternative to the
OSRM/GraphHopper HTTP-based routes in actions/route_engine.py.

The algorithm is the standard A* / Dijkstra shared structure: a priority
queue of (distance_so_far, node) pairs, relaxing each neighbor edge and
pushing (distance_so_far + edge_weight + heuristic(neighbor), neighbor).
With heuristic(v) = 0 this degenerates to plain Dijkstra; here it's the
great-circle (haversine) travel-time estimate to the goal, which is
admissible (never overestimates, since no real road is shorter than a
straight line at max speed) so the result is still guaranteed shortest.

This is plain Python over a several-hundred-thousand-to-low-millions-node
graph -- expect single-digit seconds for a short urban route and up to
roughly a minute for a long cross-country one (e.g. Hanoi to Ho Chi Minh
City), versus OSRM/GraphHopper's sub-second contraction-hierarchy lookups.
That tradeoff is intentional: this engine has no external server
dependency at all, at the cost of being much slower on long routes.
route_engine.py tries this first and falls back to GraphHopper/OSRM if it
errors, times out, or the graph cache isn't built yet.
"""

import heapq
import math
import time

from actions.osm_graph import Graph, load_or_build_graph, graph_cache_exists

_EARTH_R_M = 6371000.0
# Used only for the A* heuristic (estimating remaining travel TIME from
# remaining straight-line DISTANCE) -- deliberately optimistic (faster than
# any real road) so the heuristic never overestimates and the result stays
# the true shortest path, not an approximation.
_HEURISTIC_SPEED_M_S = 90 * 1000.0 / 3600.0   # 90 km/h, motorway-speed

# Hard ceiling on search time so a pathological/disconnected query can't
# hang a route request forever -- route_engine.py falls back to
# GraphHopper/OSRM if this is hit.
_MAX_SEARCH_S = 90.0


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dlambda = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dlambda / 2) ** 2
    return 2 * _EARTH_R_M * math.asin(min(1.0, math.sqrt(a)))


class _SpatialIndex:
    """Buckets node ids into ~0.01-degree (~1km) grid cells for fast
    nearest-node lookup -- a full scan over a few million nodes per query
    would dominate total routing time otherwise. Not exact-nearest in
    pathological sparse-grid edge cases, but expands the search ring until
    it finds candidates, which is close enough for snapping a GPS/geocoded
    point to "a nearby road node"."""

    _CELL_DEG = 0.01

    def __init__(self, coords: dict[int, tuple[float, float]]):
        self._cells: dict[tuple[int, int], list[int]] = {}
        for node_id, (lat, lon) in coords.items():
            key = self._cell_key(lat, lon)
            self._cells.setdefault(key, []).append(node_id)
        self._coords = coords

    def _cell_key(self, lat: float, lon: float) -> tuple[int, int]:
        return (int(lat / self._CELL_DEG), int(lon / self._CELL_DEG))

    def nearest(self, lat: float, lon: float) -> int | None:
        cx, cy = self._cell_key(lat, lon)
        for radius in range(0, 20):   # expand outward until something's found
            candidates = []
            for dx in range(-radius, radius + 1):
                for dy in range(-radius, radius + 1):
                    if max(abs(dx), abs(dy)) != radius:
                        continue   # only the new outer ring each step
                    candidates.extend(self._cells.get((cx + dx, cy + dy), []))
            if candidates:
                best = min(candidates,
                          key=lambda n: _haversine_m(lat, lon, *self._coords[n]))
                return best
        return None


_GRAPH_STATE: dict = {"graph": None, "index": None}


def _get_graph_and_index():
    if _GRAPH_STATE["graph"] is None:
        graph = load_or_build_graph()
        _GRAPH_STATE["graph"] = graph
        _GRAPH_STATE["index"] = _SpatialIndex(graph.coords)
    return _GRAPH_STATE["graph"], _GRAPH_STATE["index"]


def astar_available() -> bool:
    """True if a graph is already cached (fast) -- used by route_engine.py
    to decide whether to even attempt this engine, since building the graph
    from scratch is way too slow to do inline on a route request."""
    return graph_cache_exists()


def find_route(origin: tuple[float, float], dest: tuple[float, float]) -> dict | None:
    """A* shortest (fastest, by estimated travel time) route between two
    (lat, lon) points using the cached Vietnam driving graph. Returns a dict
    matching route_engine.py's route schema (distance_m, duration_s, points,
    ...), or None if the graph isn't built yet, the points can't be snapped
    to the road network, or no path exists (e.g. an island with no bridge
    in the data)."""
    graph, index = _get_graph_and_index()

    start = index.nearest(*origin)
    goal = index.nearest(*dest)
    if start is None or goal is None:
        return None

    goal_lat, goal_lon = graph.coords[goal]

    def h(node_id: int) -> float:
        lat, lon = graph.coords[node_id]
        return _haversine_m(lat, lon, goal_lat, goal_lon) / _HEURISTIC_SPEED_M_S

    dist: dict[int, float] = {start: 0.0}
    came_from: dict[int, int] = {}
    visited: set[int] = set()
    pq: list[tuple[float, int]] = [(h(start), start)]

    t0 = time.monotonic()
    found = False
    while pq:
        if time.monotonic() - t0 > _MAX_SEARCH_S:
            return None   # let the caller fall back to GraphHopper/OSRM
        _, u = heapq.heappop(pq)
        if u in visited:
            continue
        visited.add(u)
        if u == goal:
            found = True
            break
        for v, w in graph.adj.get(u, ()):
            if v in visited:
                continue
            nd = dist[u] + w
            if nd < dist.get(v, math.inf):
                dist[v] = nd
                came_from[v] = u
                heapq.heappush(pq, (nd + h(v), v))

    if not found:
        return None

    # Reconstruct the node path, then convert to (lat, lon) points and total
    # distance (edge weights are travel TIME, so distance is recomputed
    # separately from consecutive point pairs rather than re-derived from
    # dist[goal], which is seconds not meters).
    path_nodes = [goal]
    node = goal
    while node in came_from:
        node = came_from[node]
        path_nodes.append(node)
    path_nodes.reverse()

    points = [list(graph.coords[n]) for n in path_nodes]
    distance_m = sum(
        _haversine_m(points[i][0], points[i][1], points[i + 1][0], points[i + 1][1])
        for i in range(len(points) - 1)
    )

    return {
        "distance_m": distance_m,
        "duration_s": dist[goal],
        "traffic_s": None,
        "turns": None,
        "summary": "A* (self-hosted)",
        "points": points,
        "by_class": {},
        "road_names": [],
    }
