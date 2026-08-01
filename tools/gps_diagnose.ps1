# gps_diagnose.ps1 — Diagnose Windows location access for Parker.
# Run in PowerShell:  powershell -ExecutionPolicy Bypass -File tools\gps_diagnose.ps1
# It tries BOTH location APIs and prints exactly what happens, so we can see
# why GPS is reported as off even when Location Services is enabled.

$ErrorActionPreference = 'Continue'
Write-Output "=== Parker GPS diagnostic ==="

# --- Method A: modern WinRT Geolocator (Windows.Devices.Geolocation) ---------
Write-Output ""
Write-Output "[A] WinRT Geolocator (Windows.Devices.Geolocation)"
try {
    $null = [Windows.Devices.Geolocation.Geolocator,Windows.Devices.Geolocation,ContentType=WindowsRuntime]
    $access = [Windows.Devices.Geolocation.Geolocator]::RequestAccessAsync()
    # Await the IAsyncOperation
    $null = [Windows.Foundation.IAsyncInfo]
    while ($access.Status -eq 0) { Start-Sleep -Milliseconds 100 }   # 0 = Started
    Write-Output ("    AccessStatus: " + $access.GetResults())

    $geo = New-Object Windows.Devices.Geolocation.Geolocator
    $geo.DesiredAccuracy = [Windows.Devices.Geolocation.PositionAccuracy]::High
    $op = $geo.GetGeopositionAsync()
    $spin = 0
    while ($op.Status -eq 0 -and $spin -lt 150) { Start-Sleep -Milliseconds 100; $spin++ }
    if ($op.Status -eq 1) {   # 1 = Completed
        $pos = $op.GetResults().Coordinate.Point.Position
        Write-Output ("    OK lat,lon = " + $pos.Latitude + "," + $pos.Longitude)
    } else {
        Write-Output ("    GetGeopositionAsync did not complete. Status=" + $op.Status)
    }
} catch {
    Write-Output ("    WinRT ERROR: " + $_.Exception.Message)
}

# --- Method B: legacy GeoCoordinateWatcher (System.Device.Location) -----------
Write-Output ""
Write-Output "[B] Legacy GeoCoordinateWatcher (System.Device.Location)"
try {
    Add-Type -AssemblyName System.Device
    $w = New-Object System.Device.Location.GeoCoordinateWatcher('High')
    $started = $w.TryStart($true, [TimeSpan]::FromSeconds(12))
    Write-Output ("    TryStart returned: " + $started + "  Status: " + $w.Status + "  Permission: " + $w.Permission)
    $c = $w.Position.Location
    if ($c.IsUnknown) { Write-Output "    Location is UNKNOWN (no fix)" }
    else { Write-Output ("    OK lat,lon = " + $c.Latitude + "," + $c.Longitude) }
} catch {
    Write-Output ("    Legacy ERROR: " + $_.Exception.Message)
}

Write-Output ""
Write-Output "=== end ==="
