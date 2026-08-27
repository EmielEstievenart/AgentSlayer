<#
.SYNOPSIS
    Build all three AgentClip exes into dist\ and stop there - no install.

.DESCRIPTION
    A one-liner over scripts\build-exe.ps1 for the release case: a clean
    build of agentclip.exe, agentclip-engine.exe and agentclip-monitor.exe,
    left in dist\ for you to ship, and nothing copied onto PATH. Every flag
    and check is build-exe.ps1's; this file only fixes the switches.

.EXAMPLE
    .\scripts\build-dist.ps1
#>
[CmdletBinding()]
param()

$ErrorActionPreference = 'Stop'
& (Join-Path $PSScriptRoot 'build-exe.ps1') -Clean -NoInstall
if ($LASTEXITCODE -ne 0) { exit $LASTEXITCODE }

$dist = Join-Path (Split-Path $PSScriptRoot -Parent) 'dist'
Write-Host "==> dist\ holds:" -ForegroundColor Cyan
Get-ChildItem $dist -Filter 'agentclip*.exe' | ForEach-Object {
    Write-Host ("    {0,-24} {1,6:N1} MB" -f $_.Name, ($_.Length / 1MB)) -ForegroundColor Green
}
