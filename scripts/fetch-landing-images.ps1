# Landing photos (AI generated, Higgsfield) -> public/img/landing
# Usage (repo root):  powershell -ExecutionPolicy Bypass -File scripts/fetch-landing-images.ps1
# Run once, then commit public/img/landing. Existing files are skipped (-Force to re-download).
param([switch]$Force)
$ErrorActionPreference = 'Stop'
$base = 'https://d8j0ntlcm91z4.cloudfront.net/user_3ENsWC9Fimdubfa90AmLsdoIGVe/hf_20261019_'
$files = [ordered]@{
  'hero-agent.webp'   = '045954_be57cc51-6444-4ba1-83dd-2c3255a789bd_min.webp'
  'family-call.webp'  = '045954_44c1bd51-db74-4eb5-b444-a53924c5d730_min.webp'
  'ops-team.webp'     = '045954_8b17a715-eaf8-4a9d-acc5-c51e49147047_min.webp'
  'vars-phone.webp'   = '045955_89bdd272-48e7-440f-8dfd-eefabcc7bbf1_min.webp'
  'night-office.webp' = '045954_60bc6596-b9f7-4fe4-835a-6ad54e3e4408_min.webp'
  'welfare-call.webp' = '045953_a508f505-1f54-4cab-ad7c-868bc03517b0_min.webp'
}
$root = Split-Path -Parent $PSScriptRoot
$dir = Join-Path $root 'public/img/landing'
New-Item -ItemType Directory -Force -Path $dir | Out-Null
foreach ($name in $files.Keys) {
  $out = Join-Path $dir $name
  if ((Test-Path $out) -and -not $Force) { Write-Host "skip  $name"; continue }
  Invoke-WebRequest -Uri ($base + $files[$name]) -OutFile $out -UseBasicParsing
  Write-Host ("saved {0} ({1} KB)" -f $name, [math]::Round((Get-Item $out).Length / 1KB))
}
Write-Host "done -> $dir"
