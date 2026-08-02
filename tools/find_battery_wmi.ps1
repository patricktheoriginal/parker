# find_battery_wmi.ps1 — discover which Lenovo battery-control WMI classes
# your ThinkPad's BIOS/EC actually exposes, so Parker's charge-control feature
# can be wired up correctly for YOUR machine instead of guessed class names.
#
# Usage (PowerShell, no admin needed to just list):
#   powershell -ExecutionPolicy Bypass -File tools\find_battery_wmi.ps1

Write-Host "=== Lenovo/battery-related WMI classes in root\WMI ===" -ForegroundColor Cyan
$classes = Get-CimClass -Namespace root\WMI -ErrorAction SilentlyContinue |
    Where-Object { $_.CimClassName -match 'Batt|Charg|Conserv|Threshold' }

if (-not $classes) {
    Write-Host "No matching classes found in root\WMI." -ForegroundColor Yellow
} else {
    foreach ($c in $classes) {
        Write-Host "`nClass: $($c.CimClassName)" -ForegroundColor Green
        Write-Host "  Methods:"
        $c.CimClassMethods | ForEach-Object { Write-Host "    - $($_.Name)" }
        Write-Host "  Properties:"
        $c.CimClassProperties | ForEach-Object { Write-Host "    - $($_.Name) ($($_.CimType))" }
    }
}

Write-Host "`n=== Also checking root\CIMV2\power ===" -ForegroundColor Cyan
Get-CimClass -Namespace root\CIMV2\power -ErrorAction SilentlyContinue |
    Where-Object { $_.CimClassName -match 'Batt|Charg' } |
    ForEach-Object { Write-Host "  $($_.CimClassName)" }

Write-Host "`n=== Standard battery status (Win32_Battery) ===" -ForegroundColor Cyan
Get-CimInstance -ClassName Win32_Battery | Select-Object EstimatedChargeRemaining, BatteryStatus, Chemistry

Write-Host "`nDone. Copy everything above and send it back so the charge-control" -ForegroundColor Cyan
Write-Host "feature can be wired to the exact class/method names your machine has." -ForegroundColor Cyan
