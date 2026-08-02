# watch_battery_toggle.ps1 — capture what changes when you toggle Battery
# Conservation Mode inside the Lenovo Vantage app.
#
# WMI probing didn't reveal a settable class, so instead we snapshot the
# registry + LENOVO_UTILITY_DATA WMI instances, wait for you to toggle the
# setting in Vantage's UI, then snapshot again and diff — showing exactly
# what Vantage wrote.
#
# Usage:
#   1. Close Lenovo Vantage if it's open, then run this script.
#   2. When it says "BEFORE snapshot taken", open Vantage, go to the Battery
#      Conservation Mode toggle, but DON'T toggle it yet.
#   3. Press Enter in this console when ready.
#   4. NOW toggle Conservation Mode ON in Vantage.
#   5. Press Enter again in this console.
#   6. It prints everything that changed.

function Snapshot {
    $reg = @()
    foreach ($p in @("HKLM:\SOFTWARE\Lenovo", "HKLM:\SOFTWARE\WOW6432Node\Lenovo",
                     "HKCU:\SOFTWARE\Lenovo")) {
        if (Test-Path $p) {
            Get-ChildItem -Path $p -Recurse -ErrorAction SilentlyContinue | ForEach-Object {
                $key = $_.PSPath
                try {
                    $props = Get-ItemProperty -Path $key -ErrorAction SilentlyContinue
                    foreach ($prop in $props.PSObject.Properties) {
                        if ($prop.Name -notmatch '^PS') {
                            $reg += [PSCustomObject]@{
                                Key = ($key -replace 'Microsoft.PowerShell.Core\\Registry::','')
                                Name = $prop.Name
                                Value = "$($prop.Value)"
                            }
                        }
                    }
                } catch {}
            }
        }
    }
    $wmi = Get-CimInstance -Namespace root\WMI -ClassName LENOVO_UTILITY_DATA -ErrorAction SilentlyContinue |
        Select-Object *
    return @{ Reg = $reg; Wmi = $wmi }
}

Write-Host "Taking BEFORE snapshot..." -ForegroundColor Cyan
$before = Snapshot
Write-Host "BEFORE snapshot taken ($($before.Reg.Count) registry values seen)." -ForegroundColor Green
Write-Host "`nNow open Lenovo Vantage, navigate to the Battery Conservation Mode" -ForegroundColor Yellow
Write-Host "setting, but do NOT toggle it yet. Press Enter here when ready..." -ForegroundColor Yellow
Read-Host

Write-Host "`nNOW toggle Battery Conservation Mode ON in Vantage. Press Enter here once done..." -ForegroundColor Yellow
Read-Host

Write-Host "Taking AFTER snapshot..." -ForegroundColor Cyan
$after = Snapshot

Write-Host "`n=== Registry differences ===" -ForegroundColor Cyan
$beforeSet = $before.Reg | ForEach-Object { "$($_.Key)|$($_.Name)|$($_.Value)" }
$afterSet  = $after.Reg  | ForEach-Object { "$($_.Key)|$($_.Name)|$($_.Value)" }
$diff = Compare-Object $beforeSet $afterSet
if ($diff) {
    foreach ($d in $diff) {
        $marker = if ($d.SideIndicator -eq '=>') { "NEW/CHANGED" } else { "REMOVED/OLD" }
        Write-Host "  [$marker] $($d.InputObject)"
    }
} else {
    Write-Host "  (no registry changes detected under HKLM/HKCU Lenovo keys)"
}

Write-Host "`n=== LENOVO_UTILITY_DATA WMI instances AFTER ===" -ForegroundColor Cyan
if ($after.Wmi) {
    $after.Wmi | Format-List *
} else {
    Write-Host "  (no instances)"
}

Write-Host "`nDone. Copy everything above (especially registry diffs) and send it back." -ForegroundColor Cyan
