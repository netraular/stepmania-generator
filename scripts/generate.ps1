# Generates a StepMania dance-single chart from a YouTube URL using AutoStepper.
#
# Usage:
#   powershell -ExecutionPolicy Bypass -File scripts/generate.ps1 `
#       -Url "https://www.youtube.com/watch?v=UnyLfqpyi94" `
#       -Title "Burn the House Down" -Artist "AJR"
#
# Optional params: -Duration (seconds, default 130), -Hard ($true/$false, default $true),
#                  -Python (path to python.exe)
param(
    [Parameter(Mandatory = $true)][string]$Url,
    [Parameter(Mandatory = $true)][string]$Title,
    [Parameter(Mandatory = $true)][string]$Artist,
    [int]$Duration = 130,
    [bool]$Hard = $true,
    [string]$Python = "python"
)
$ErrorActionPreference = "Stop"
$root = Split-Path -Parent $PSScriptRoot
$songsDir = Join-Path $root "songs"
$outputDir = Join-Path $root "output"
$jar = Join-Path $root "tools/AutoStepper/AutoStepper.jar"

New-Item -ItemType Directory -Force -Path $songsDir, $outputDir | Out-Null

if (-not (Test-Path $jar)) {
    Write-Host "AutoStepper not found. Running setup..."
    & (Join-Path $PSScriptRoot "setup-autostepper.ps1")
}

# Strip characters that confuse the generator / filesystem.
$clean = "$Title - $Artist" -replace "[\\/:*?""<>|']", ""
$mp3 = Join-Path $songsDir "$clean.mp3"

Write-Host "Downloading audio -> $mp3"
& $Python -m yt_dlp -x --audio-format mp3 --audio-quality 0 -o (Join-Path $songsDir "$clean.%(ext)s") $Url

Write-Host "Running AutoStepper (duration=$Duration, hard=$Hard)..."
Push-Location (Join-Path $root "tools/AutoStepper")
try {
    java -jar AutoStepper.jar input="$songsDir" output="$outputDir" duration=$Duration hard=$($Hard.ToString().ToLower())
}
finally {
    Pop-Location
}

Write-Host "Done. Chart folder is in: $outputDir"
Write-Host "Copy the generated folder into your StepMania 'Songs' directory to play."
