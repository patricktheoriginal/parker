# find_battery_wmi.ps1 — deep-dive into Lenovo's generic capability WMI
# interface (LENOVO_UTILITY_DATA / LENOVO_UTILITY_EVENT in root\WMI), which is
# how newer Lenovo Vantage builds expose battery conservation mode instead of
# a dedicated Lenovo_Battery* class.
#
# Usage (PowerShell, no admin needed):
#   powershell -ExecutionPolicy Bypass -File tools\find_battery_wmi.ps1

Write-Host "=== Full detail: LENOVO_UTILITY_DATA ===" -ForegroundColor Cyan
$cls = Get-CimClass -Namespace root\WMI -ClassName LENOVO_UTILITY_DATA -ErrorAction SilentlyContinue
if ($cls) {
    Write-Host "Methods:" -ForegroundColor Green
    foreach ($m in $cls.CimClassMethods) {
        Write-Host "  $($m.Name)("
        foreach ($p in $m.Parameters) {
            Write-Host "      $($p.CimType) $($p.Name)"
        }
        Write-Host "  )"
    }
    Write-Host "Properties:" -ForegroundColor Green
    $cls.CimClassProperties | ForEach-Object { Write-Host "  $($_.Name) ($($_.CimType))" }
} else {
    Write-Host "Class not found." -ForegroundColor Yellow
}

Write-Host "`n=== Full detail: LENOVO_UTILITY_EVENT ===" -ForegroundColor Cyan
$cls2 = Get-CimClass -Namespace root\WMI -ClassName LENOVO_UTILITY_EVENT -ErrorAction SilentlyContinue
if ($cls2) {
    Write-Host "Methods:" -ForegroundColor Green
    foreach ($m in $cls2.CimClassMethods) {
        Write-Host "  $($m.Name)("
        foreach ($p in $m.Parameters) {
            Write-Host "      $($p.CimType) $($p.Name)"
        }
        Write-Host "  )"
    }
    Write-Host "Properties:" -ForegroundColor Green
    $cls2.CimClassProperties | ForEach-Object { Write-Host "  $($_.Name) ($($_.CimType))" }
} else {
    Write-Host "Class not found." -ForegroundColor Yellow
}

Write-Host "`n=== Existing instances of LENOVO_UTILITY_DATA (current values) ===" -ForegroundColor Cyan
Get-CimInstance -Namespace root\WMI -ClassName LENOVO_UTILITY_DATA -ErrorAction SilentlyContinue |
    Format-List *

Write-Host "`n=== ALL other classes in root\WMI starting with LENOVO (case-insensitive, full list) ===" -ForegroundColor Cyan
Get-CimClass -Namespace root\WMI -ErrorAction SilentlyContinue |
    Where-Object { $_.CimClassName -like '*LENOVO*' -or $_.CimClassName -like '*Ideapad*' -or $_.CimClassName -like '*ThinkPad*' } |
    ForEach-Object { Write-Host "  $($_.CimClassName)" }

Write-Host "`n=== Try calling GetIfSupportOrVersion for common battery-related IDs ===" -ForegroundColor Cyan
# Lenovo's capability IDs aren't publicly documented, but conservation mode /
# battery threshold related IDs commonly seen in the wild start around these
# ranges. We probe a small safe set (read-only query, no Set calls) to see
# which ones respond as 'supported' on this machine.
if ($cls) {
    $idsToTry = @(
        "BatteryChargeThreshold", "ConservationMode", "BatteryConservation",
        "ChargeThreshold", "AlwaysOnUSB", "RapidCharge"
    )
    foreach ($id in $idsToTry) {
        try {
            $result = Invoke-CimMethod -Namespace root\WMI -ClassName LENOVO_UTILITY_DATA `
                -MethodName GetIfSupportOrVersion -Arguments @{ IDString = $id } -ErrorAction Stop
            Write-Host "  $id -> $($result | Out-String)"
        } catch {
            Write-Host "  $id -> error: $($_.Exception.Message)"
        }
    }
}

Write-Host "`nDone. Copy everything above and send it back." -ForegroundColor Cyan
