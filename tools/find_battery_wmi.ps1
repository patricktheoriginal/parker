# find_battery_wmi.ps1 — discover how Lenovo Vantage on THIS machine controls
# Battery Conservation Mode. root\WMI didn't expose a Lenovo class, so this
# checks the other places Vantage commonly uses: other WMI namespaces,
# registry keys, and the running Vantage services.
#
# Usage (PowerShell, no admin needed):
#   powershell -ExecutionPolicy Bypass -File tools\find_battery_wmi.ps1

Write-Host "=== 1. ALL WMI namespaces containing 'Lenovo' or 'IdeaPad' or 'ThinkPad' anywhere under ROOT ===" -ForegroundColor Cyan
$namespaces = Get-CimInstance -Namespace root -ClassName __Namespace -ErrorAction SilentlyContinue |
    Select-Object -ExpandProperty Name
foreach ($ns in $namespaces) {
    if ($ns -match 'Lenovo|WMI|CIMV2') {
        Write-Host "  root\$ns"
    }
}

Write-Host "`n=== 2. Searching ALL namespaces for classes with 'Lenovo' in the name ===" -ForegroundColor Cyan
function Search-Namespace($path, $depth) {
    if ($depth -gt 3) { return }
    try {
        $classes = Get-CimClass -Namespace $path -ErrorAction SilentlyContinue |
            Where-Object { $_.CimClassName -match 'Lenovo' }
        foreach ($c in $classes) {
            Write-Host "  FOUND: $path -> $($c.CimClassName)" -ForegroundColor Green
            $c.CimClassMethods | ForEach-Object { Write-Host "    method: $($_.Name)" }
        }
        $subs = Get-CimInstance -Namespace $path -ClassName __Namespace -ErrorAction SilentlyContinue |
            Select-Object -ExpandProperty Name
        foreach ($sub in $subs) {
            Search-Namespace "$path\$sub" ($depth + 1)
        }
    } catch {}
}
Search-Namespace "root" 0

Write-Host "`n=== 3. Registry: HKLM\SOFTWARE\Lenovo (conservation/charge/threshold keys) ===" -ForegroundColor Cyan
$paths = @(
    "HKLM:\SOFTWARE\Lenovo",
    "HKLM:\SOFTWARE\WOW6432Node\Lenovo"
)
foreach ($p in $paths) {
    if (Test-Path $p) {
        Write-Host "Scanning $p ..."
        Get-ChildItem -Path $p -Recurse -ErrorAction SilentlyContinue |
            Where-Object { $_.PSChildName -match 'Batt|Charg|Conserv|Threshold|Power' } |
            ForEach-Object { Write-Host "  $($_.PSPath -replace 'Microsoft.PowerShell.Core\\Registry::','')" }
    }
}

Write-Host "`n=== 4. Lenovo-related Windows services (running) ===" -ForegroundColor Cyan
Get-Service | Where-Object { $_.DisplayName -match 'Lenovo|Vantage' } |
    Select-Object Name, DisplayName, Status | Format-Table -AutoSize

Write-Host "`n=== 5. Lenovo-related scheduled tasks / exe paths (Program Files) ===" -ForegroundColor Cyan
Get-ChildItem "C:\Program Files\Lenovo" -Recurse -Include "*.exe" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match 'Charg|Batt|Conserv|Power' } |
    ForEach-Object { Write-Host "  $($_.FullName)" }
Get-ChildItem "C:\Program Files (x86)\Lenovo" -Recurse -Include "*.exe" -ErrorAction SilentlyContinue |
    Where-Object { $_.Name -match 'Charg|Batt|Conserv|Power' } |
    ForEach-Object { Write-Host "  $($_.FullName)" }

Write-Host "`nDone. Copy everything above and send it back." -ForegroundColor Cyan
