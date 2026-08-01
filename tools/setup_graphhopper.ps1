# setup_graphhopper.ps1 — run a self-hosted, open-source GraphHopper routing
# server for Vietnam on Windows. FREE, no API key.
#
# Requirements: Java 17+ (JDK). Install Temurin: winget install EclipseAdoptium.Temurin.21.JDK
#
# Usage (PowerShell):
#   powershell -ExecutionPolicy Bypass -File tools\setup_graphhopper.ps1
#
# First run downloads the jar + Vietnam map and builds the routing graph
# (a few minutes, needs ~2-4 GB RAM). Server listens on http://localhost:8989.

$ErrorActionPreference = "Stop"
$dir = "$env:USERPROFILE\.parker_graphhopper"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Set-Location $dir

$jar  = "graphhopper-web-11.0.jar"
$conf = "config-example.yml"
$pbf  = "vietnam-latest.osm.pbf"

Write-Host "=== GraphHopper self-host setup (Vietnam) ==="
try { java -version *>&1 | Select-Object -First 1 } catch {
  Write-Host "Java not found. Install a JDK first:"
  Write-Host "  winget install EclipseAdoptium.Temurin.21.JDK"
  exit 1
}

if (-not (Test-Path $jar))  { Write-Host "Downloading GraphHopper…"; `
  Invoke-WebRequest "https://repo1.maven.org/maven2/com/graphhopper/graphhopper-web/11.0/graphhopper-web-11.0.jar" -OutFile $jar }
if (-not (Test-Path $conf)) { Write-Host "Downloading config…"; `
  Invoke-WebRequest "https://raw.githubusercontent.com/graphhopper/graphhopper/11.x/config-example.yml" -OutFile $conf }
if (-not (Test-Path $pbf))  { Write-Host "Downloading Vietnam map (~250 MB, one time)…"; `
  Invoke-WebRequest "https://download.geofabrik.de/asia/vietnam-latest.osm.pbf" -OutFile $pbf }

Write-Host ""
Write-Host "Starting GraphHopper on http://localhost:8989 (first run builds the graph)…"
java "-Xmx4g" "-Ddw.graphhopper.datareader.file=$pbf" -jar $jar server $conf
