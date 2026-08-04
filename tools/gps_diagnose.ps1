# gps_diagnose.ps1 -- Diagnose Windows location access for Parker.
# Run:  powershell -ExecutionPolicy Bypass -File tools\gps_diagnose.ps1

$ErrorActionPreference = 'Continue'
Write-Output "=== Parker GPS diagnostic ==="

# --- [A] GeoCoordinateWatcher (System.Device.Location) ---
# Checked first: a plain .NET Framework class, no WinRT projection needed,
# and it uses the same underlying Windows Location Provider as the WinRT
# Geolocator below -- so there's no accuracy tradeoff to checking this one
# first, only less that can go wrong. TryStart() only waits for the
# permission prompt, not an actual position fix, so this polls
# Status/Position in a loop instead of reading immediately after TryStart
# (a common mistake that looks like "no GPS" when the receiver just hasn't
# reported in yet).
Write-Output ""
Write-Output "[A] GeoCoordinateWatcher (System.Device.Location)"
try {
    Add-Type -AssemblyName System.Device
    $w = New-Object System.Device.Location.GeoCoordinateWatcher('High')
    $null = $w.TryStart($true, [TimeSpan]::FromSeconds(10))
    Write-Output ("    Permission: " + $w.Permission)
    if ($w.Permission -eq [System.Device.Location.GeoPositionPermission]::Denied) {
        Write-Output "    Location access denied."
    } else {
        $deadline = (Get-Date).AddSeconds(45)
        $found = $false
        while ((Get-Date) -lt $deadline) {
            if ($w.Status -eq [System.Device.Location.GeoPositionStatus]::Ready -and
                -not $w.Position.Location.IsUnknown) {
                $c = $w.Position.Location
                Write-Output ("    OK lat,lon = " + $c.Latitude + "," + $c.Longitude)
                $found = $true
                break
            }
            Start-Sleep -Milliseconds 500
        }
        if (-not $found) {
            Write-Output ("    Status: " + $w.Status + " -- no fix within 45s (NOFIX).")
        }
    }
} catch {
    Write-Output ("    ERROR: " + $_.Exception.Message)
}

# --- [B] WinRT Geolocator (Windows.Devices.Geolocation) ---
# On Windows PowerShell 5 (.NET Framework), the WinRT interop extension type
# lives in System.Runtime.WindowsRuntime, which must be loaded explicitly by
# strong name. This is a brittle, undocumented reflection trick that can
# fail outright on some Windows builds ("Could not load file or assembly...")
# even with a fully installed, current .NET Framework -- wrapped in its own
# try/catch so that failure doesn't take down the rest of this script (it
# used to run unguarded at the top of the file, which is exactly what made
# the whole diagnostic abort instead of falling through to [A]/[B]).
Write-Output ""
Write-Output "[B] WinRT Geolocator (Windows.Devices.Geolocation)"
try {
    [System.Reflection.Assembly]::Load('System.Runtime.WindowsRuntime, Version=4.0.0.0, Culture=neutral, PublicKeyToken=b03f5f7f11d50a3a') | Out-Null

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
    Write-Output "    (This can fail on some Windows builds even with .NET Framework"
    Write-Output "     fully installed -- if [A] above got a fix, that's the one Parker uses.)"
}

Write-Output ""
Write-Output "=== end ==="
