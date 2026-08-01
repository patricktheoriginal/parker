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

# Continue on native-command stderr (java -version writes there); we handle
# download failures explicitly.
$ErrorActionPreference = "Continue"
$dir = "$env:USERPROFILE\.parker_graphhopper"
New-Item -ItemType Directory -Force -Path $dir | Out-Null
Set-Location $dir

$jar  = "graphhopper-web-11.0.jar"
$conf = "config-example.yml"
$pbf  = "vietnam-latest.osm.pbf"

Write-Host "=== GraphHopper self-host setup (Vietnam) ==="

# Find java.exe even if it isn't on PATH (Temurin/Microsoft OpenJDK install to
# Program Files but don't always add themselves to PATH).
function Find-Java {
  # 1) PATH
  $cmd = Get-Command java -ErrorAction SilentlyContinue
  if ($cmd) { return $cmd.Source }
  # 2) JAVA_HOME
  if ($env:JAVA_HOME -and (Test-Path "$env:JAVA_HOME\bin\java.exe")) {
    return "$env:JAVA_HOME\bin\java.exe"
  }
  # 3) Common install dirs (newest first)
  $roots = @(
    "$env:ProgramFiles\Eclipse Adoptium",
    "$env:ProgramFiles\Microsoft\jdk*",
    "$env:ProgramFiles\Java",
    "${env:ProgramFiles(x86)}\Eclipse Adoptium"
  )
  foreach ($root in $roots) {
    $found = Get-ChildItem -Path $root -Recurse -Filter java.exe -ErrorAction SilentlyContinue |
             Where-Object { $_.FullName -match '\\bin\\java.exe$' } |
             Sort-Object FullName -Descending | Select-Object -First 1
    if ($found) { return $found.FullName }
  }
  return $null
}

$JAVA = Find-Java
if (-not $JAVA) {
  Write-Host "Java not found. Install a JDK, then run this again:"
  Write-Host "  winget install EclipseAdoptium.Temurin.21.JDK"
  Write-Host "(If you already installed it, open a NEW terminal, or it will still be found here.)"
  exit 1
}
Write-Host "Java: $JAVA"

if (-not (Test-Path $jar))  { Write-Host "Downloading GraphHopper…"; `
  Invoke-WebRequest "https://repo1.maven.org/maven2/com/graphhopper/graphhopper-web/11.0/graphhopper-web-11.0.jar" -OutFile $jar }
if (-not (Test-Path $conf)) { Write-Host "Downloading config…"; `
  Invoke-WebRequest "https://raw.githubusercontent.com/graphhopper/graphhopper/11.x/config-example.yml" -OutFile $conf }
if (-not (Test-Path $pbf))  { Write-Host "Downloading Vietnam map (~250 MB, one time)…"; `
  Invoke-WebRequest "https://download.geofabrik.de/asia/vietnam-latest.osm.pbf" -OutFile $pbf }

Write-Host ""
Write-Host "Starting GraphHopper on http://localhost:8989 (first run builds the graph)…"
& $JAVA "-Xmx4g" "-Ddw.graphhopper.datareader.file=$pbf" -jar $jar server $conf
