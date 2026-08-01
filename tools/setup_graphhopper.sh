#!/bin/bash
# setup_graphhopper.sh — run a self-hosted, open-source GraphHopper routing
# server for Vietnam. FREE, no API key.
#
# Requirements: Java 17+ (GraphHopper 11 wants Java 25; 17+ usually works).
#   macOS:   brew install openjdk
#   Windows: install Temurin/Adoptium JDK, or use setup_graphhopper.ps1
#
# Usage:
#   bash tools/setup_graphhopper.sh          # download + build + run
# First run builds the routing graph from the Vietnam map (a few minutes,
# needs ~2-4 GB RAM). Later runs start fast. Server listens on :8989.
#
# Then in Parker config/api_keys.json (only if not localhost):
#   "graphhopper_url": "http://localhost:8989"

set -e
DIR="$HOME/.parker_graphhopper"
mkdir -p "$DIR"
cd "$DIR"

JAR="graphhopper-web-11.0.jar"
CONF="config-example.yml"
# Vietnam OpenStreetMap extract (Geofabrik) — ~250 MB.
PBF="vietnam-latest.osm.pbf"
PBF_URL="https://download.geofabrik.de/asia/vietnam-latest.osm.pbf"

echo "=== GraphHopper self-host setup (Vietnam) ==="

command -v java >/dev/null 2>&1 || {
  echo "Java not found. Install a JDK 17+ first:"
  echo "  macOS:   brew install openjdk   (then follow brew's PATH note)"
  echo "  Linux:   sudo apt install default-jdk"
  exit 1
}
echo "Java: $(java -version 2>&1 | head -1)"

[ -f "$JAR" ]  || { echo "Downloading GraphHopper…"; \
  curl -L -o "$JAR" "https://repo1.maven.org/maven2/com/graphhopper/graphhopper-web/11.0/graphhopper-web-11.0.jar"; }
[ -f "$CONF" ] || { echo "Downloading config…"; \
  curl -L -o "$CONF" "https://raw.githubusercontent.com/graphhopper/graphhopper/11.x/config-example.yml"; }
[ -f "$PBF" ]  || { echo "Downloading Vietnam map (~250 MB, one time)…"; \
  curl -L -o "$PBF" "$PBF_URL"; }

echo ""
echo "Starting GraphHopper on http://localhost:8989 (first run builds the graph)…"
exec java -Xmx4g -Ddw.graphhopper.datareader.file="$PBF" -jar "$JAR" server "$CONF"
