# gps_diagnose.ps1 — Diagnose Windows location access for Parker.
# Run:  powershell -ExecutionPolicy Bypass -File tools\gps_diagnose.ps1

$ErrorActionPreference = 'Continue'
Write-Output "=== Parker GPS diagnostic ==="

# Helper: correctly await a WinRT IAsyncOperation[T] from PowerShell.
function Await($op, $resultType) {
    $task = [System.WindowsRuntimeSystemExtensions].GetMethods() |
        Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                       $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } |
        Select-Object -First 1
    $task = $task.MakeGenericMethod($resultType)
    $t = $task.Invoke($null, @($op))
    $t.Wait(15000) | Out-Null
    return $t.Result
}

Write-Output ""
Write-Output "[A] WinRT Geolocator (Windows.Devices.Geolocation)"
try {
    $null = [Windows.Devices.Geolocation.Geolocator,Windows.Devices.Geolocation,ContentType=WindowsRuntime]
    $accessType  = [Windows.Devices.Geolocation.GeolocationAccessStatus]
    $accessOp    = [Windows.Devices.Geolocation.Geolocator]::RequestAccessAsync()
    $access      = Await $accessOp $accessType
    Write-Output ("    AccessStatus: " + $access)

    if ($access -eq [Windows.Devices.Geolocation.GeolocationAccessStatus]::Allowed) {
        $geo = New-Object Windows.Devices.Geolocation.Geolocator
        $geo.DesiredAccuracyInMeters = 100
        $posType = [Windows.Devices.Geolocation.Geoposition]
        $pos = Await ($geo.GetGeopositionAsync()) $posType
        if ($pos -ne $null) {
            $p = $pos.Coordinate.Point.Position
            Write-Output ("    OK lat,lon = " + $p.Latitude + "," + $p.Longitude)
        } else {
            Write-Output "    No position returned (NOFIX)."
        }
    }
} catch {
    Write-Output ("    WinRT ERROR: " + $_.Exception.Message)
}

Write-Output ""
Write-Output "[B] Legacy GeoCoordinateWatcher (System.Device.Location)"
try {
    Add-Type -AssemblyName System.Device
    $w = New-Object System.Device.Location.GeoCoordinateWatcher('High')
    $null = $w.TryStart($true, [TimeSpan]::FromSeconds(15))
    Write-Output ("    Status: " + $w.Status + "  Permission: " + $w.Permission)
    $c = $w.Position.Location
    if ($c.IsUnknown) { Write-Output "    Location is UNKNOWN (no fix)" }
    else { Write-Output ("    OK lat,lon = " + $c.Latitude + "," + $c.Longitude) }
} catch {
    Write-Output ("    Legacy ERROR: " + $_.Exception.Message)
}

Write-Output ""
Write-Output "=== end ==="
