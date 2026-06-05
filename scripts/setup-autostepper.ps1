# Downloads AutoStepper (phr00t) into ./tools/AutoStepper
# Usage: powershell -ExecutionPolicy Bypass -File scripts/setup-autostepper.ps1
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$toolsDir = Join-Path $root "tools"
$zip = Join-Path $toolsDir "AutoStepper-Java-v1.7.zip"
$dest = Join-Path $toolsDir "AutoStepper"

New-Item -ItemType Directory -Force -Path $toolsDir | Out-Null

if (Test-Path (Join-Path $dest "AutoStepper.jar")) {
    Write-Host "AutoStepper already present at $dest"
    return
}

$url = "https://github.com/phr00t/AutoStepper/raw/master/dist/AutoStepper-Java-v1.7.zip"
Write-Host "Downloading AutoStepper v1.7..."
Invoke-WebRequest -Uri $url -OutFile $zip
Write-Host "Extracting..."
Expand-Archive -Path $zip -DestinationPath $dest -Force
Remove-Item $zip
Write-Host "Done. AutoStepper.jar is at $dest"
