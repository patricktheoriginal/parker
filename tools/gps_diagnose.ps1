# gps_diagnose.ps1 -- Diagnose Windows location access for Parker.
# Run:  powershell -ExecutionPolicy Bypass -File tools\gps_diagnose.ps1

$ErrorActionPreference = 'Continue'
Write-Output "=== Parker GPS diagnostic ==="

# On Windows PowerShell 5 (.NET Framework), the WinRT interop extension type
# lives in System.Runtime.WindowsRuntime, which must be loaded explicitly.
[System.Reflection.Assembly]::Load('System.Runtime.WindowsRuntime, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a') | Out-Null

# Helper: correctly await a WinRT IAsyncOperation[T] from PowerShell.
$script:asTask = [System.WindowsRuntimeSystemExtensions].GetMethods() |
    Where-Object { $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and
                   $_.GetParameters()[0].ParameterType.Name -eq 'IAsyncOperation`1' } |
    Select-Object -First 1
function Await($op, $resultType, $waitMs = 20000) {
    $m = $script:asTask.MakeGenericMethod($resultType)
    $t = $m.Invoke($null, @($op))
    $t.Wait($waitMs) | Out-Null
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
        $geo.DesiredAccuracy = [Windows.Devices.Geolocation.PositionAccuracy]::High
        $geo.DesiredAccuracyInMeters = 10
        $posType = [Windows.Devices.Geolocation.Geoposition]
        $pos = Await ($geo.GetGeopositionAsync()) $posType 45000
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
    $null = $w.TryStart($true, [TimeSpan]::FromSeconds(45))
    Write-Output ("    Status: " + $w.Status + "  Permission: " + $w.Permission)
    $c = $w.Position.Location
    if ($c.IsUnknown) { Write-Output "    Location is UNKNOWN (no fix)" }
    else { Write-Output ("    OK lat,lon = " + $c.Latitude + "," + $c.Longitude) }
} catch {
    Write-Output ("    Legacy ERROR: " + $_.Exception.Message)
}

Write-Output ""
Write-Output "=== end ==="
