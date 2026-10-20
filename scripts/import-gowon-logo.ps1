# Extract the GOWON logo mark (base64 PNG embedded in the company homepage source)
# into public/brand/gowon-mark.png for aicc.gowon.co.kr. ASCII only on purpose.
$ErrorActionPreference = 'Stop'
$dev  = 'D:\Desktop\GOWON\Dev'
$site = Get-ChildItem -LiteralPath $dev -Directory | Where-Object { $_.Name -like '0. *' } | Select-Object -First 1
if (-not $site) { throw "homepage folder (0. ...) not found under $dev" }
$src  = Join-Path $site.FullName 'index.html'
$html = [IO.File]::ReadAllText($src)
$m = [regex]::Match($html, '\.brand-img\{\s*background-image:url\("data:image/png;base64,([A-Za-z0-9+/=]+)"\)')
if (-not $m.Success) { throw 'logo (.brand-img) not found in homepage source' }
$bytes = [Convert]::FromBase64String($m.Groups[1].Value)
$dir = Join-Path $PSScriptRoot '..\public\brand'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
$dst = Join-Path (Resolve-Path $dir) 'gowon-mark.png'
[IO.File]::WriteAllBytes($dst, $bytes)
Write-Host "saved: $dst ($($bytes.Length) bytes)"
