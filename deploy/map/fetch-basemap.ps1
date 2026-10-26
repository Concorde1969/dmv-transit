<#
.SYNOPSIS
  Download the regional basemap for local testing (Windows). The server uses
  deploy/map/fetch-basemap.sh instead.

.DESCRIPTION
  Cuts the DC / Maryland / Virginia region out of Protomaps' free daily world build
  (OpenStreetMap data) into data\map\dmv.pmtiles, using the pinned pmtiles tool from
  github.com/protomaps/go-pmtiles. Only the region's tiles are downloaded.

.EXAMPLE
  powershell -ExecutionPolicy Bypass -File deploy\map\fetch-basemap.ps1
#>
param(
    [string]$Build = '20261026',
    [switch]$DryRun
)
$ErrorActionPreference = 'Stop'

$Version = '1.31.2'
$Bbox = '-79.3,37.4,-75.3,40.1'
$MaxZoom = 15
$Root = (Resolve-Path (Join-Path $PSScriptRoot '..\..')).Path
$Dir = Join-Path $Root 'data\map'
$ToolDir = Join-Path $Dir '.tool'
$Tool = Join-Path $ToolDir 'pmtiles.exe'
New-Item -ItemType Directory -Force -Path $ToolDir | Out-Null

if (-not (Test-Path $Tool)) {
    $arch = if ($env:PROCESSOR_ARCHITECTURE -eq 'ARM64') { 'arm64' } else { 'x86_64' }
    $url = "https://github.com/protomaps/go-pmtiles/releases/download/v$Version/go-pmtiles_${Version}_Windows_$arch.zip"
    Write-Host "Downloading the pmtiles tool ($url)"
    $zip = Join-Path $ToolDir 'pmtiles.zip'
    Invoke-WebRequest -Uri $url -OutFile $zip -UseBasicParsing
    Expand-Archive -Path $zip -DestinationPath $ToolDir -Force
    Remove-Item $zip
}

$src = "https://build.protomaps.com/$Build.pmtiles"
$out = Join-Path $Dir 'dmv.pmtiles'
$args = @('extract', $src, "$out.new", "--bbox=$Bbox", "--maxzoom=$MaxZoom")
if ($DryRun) { $args += '--dry-run' }
& $Tool @args
if ($LASTEXITCODE -ne 0) { throw "pmtiles extract failed ($LASTEXITCODE)" }
if (-not $DryRun) {
    Move-Item -Force "$out.new" $out
    Write-Host ("Saved {0} ({1:N0} MB)" -f $out, ((Get-Item $out).Length / 1MB))
}
