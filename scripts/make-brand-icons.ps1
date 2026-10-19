# AICC Portal app icons (GOWON mark, blue) -> public/icon-192.png, icon-512.png, apple-touch-icon.png, favicon.ico
# Source: scripts/brand/icon.html (same mark as public/brand/aicc-app-icon.svg). Uses headless Edge (built into Windows).
# Usage (repo root):  powershell -ExecutionPolicy Bypass -File scripts/make-brand-icons.ps1
$ErrorActionPreference = 'Stop'
$root = Split-Path -Parent $PSScriptRoot
$pub = Join-Path $root 'public'
$brand = Join-Path $PSScriptRoot 'brand'

$browser = @(
  "${env:ProgramFiles(x86)}\Microsoft\Edge\Application\msedge.exe",
  "$env:ProgramFiles\Microsoft\Edge\Application\msedge.exe",
  "$env:ProgramFiles\Google\Chrome\Application\chrome.exe",
  "${env:ProgramFiles(x86)}\Google\Chrome\Application\chrome.exe"
) | Where-Object { Test-Path $_ } | Select-Object -First 1
if (-not $browser) { throw 'Edge/Chrome not found.' }
$tmpProfile = Join-Path $env:TEMP ('aicc-brand-' + [guid]::NewGuid())

function Shot([string]$html, [int]$w, [int]$h, [string]$out) {
  $url = 'file:///' + ((Join-Path $brand $html) -replace '\\', '/')
  $dir = Split-Path -Parent $out
  New-Item -ItemType Directory -Force -Path $dir | Out-Null
  if (Test-Path $out) { Remove-Item $out -Force }
  & $browser --headless=new --disable-gpu --hide-scrollbars --allow-file-access-from-files `
    "--user-data-dir=$tmpProfile" --virtual-time-budget=4000 "--window-size=$w,$h" "--screenshot=$out" $url 2>$null | Out-Null
  $i = 0; while (-not (Test-Path $out) -and $i -lt 40) { Start-Sleep -Milliseconds 250; $i++ }
  if (-not (Test-Path $out)) { throw "screenshot failed: $out" }
  Write-Host ("saved {0} ({1} KB)" -f ($out.Substring($root.Length + 1)), [math]::Round((Get-Item $out).Length / 1KB))
}

Shot 'icon.html' 192 192 (Join-Path $pub 'icon-192.png')
Shot 'icon.html' 512 512 (Join-Path $pub 'icon-512.png')
Shot 'icon.html' 180 180 (Join-Path $pub 'apple-touch-icon.png')
$png48 = Join-Path $env:TEMP 'aicc-favicon-48.png'
Shot 'icon.html' 48 48 $png48

# favicon.ico = ICO header(6) + dir entry(16) + one PNG. For clients that request /favicon.ico directly.
$bytes = [IO.File]::ReadAllBytes($png48)
$ico = Join-Path $pub 'favicon.ico'
$fs = [IO.File]::Create($ico); $bw = New-Object IO.BinaryWriter($fs)
$bw.Write([UInt16]0); $bw.Write([UInt16]1); $bw.Write([UInt16]1)
$bw.Write([Byte]48); $bw.Write([Byte]48); $bw.Write([Byte]0); $bw.Write([Byte]0)
$bw.Write([UInt16]1); $bw.Write([UInt16]32); $bw.Write([UInt32]$bytes.Length); $bw.Write([UInt32]22)
$bw.Write($bytes); $bw.Close(); $fs.Close()
Write-Host "saved public\favicon.ico"
Remove-Item $tmpProfile -Recurse -Force -ErrorAction SilentlyContinue
Write-Host 'done (browser tab icon may need a hard refresh)'
