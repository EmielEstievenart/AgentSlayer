<#
.SYNOPSIS
    Freeze AgentClip into a single agentclip.exe and drop it on your PATH.

.DESCRIPTION
    Builds a PyInstaller onefile binary from packaging/agentclip.spec, smoke-tests
    the frozen exe, then copies it into a folder that is already on PATH.

.PARAMETER InstallDir
    Where to copy agentclip.exe. Defaults to $env:AGENTCLIP_INSTALL_DIR, else
    "$HOME\Documents\PATH". The folder is created if it does not exist.

.PARAMETER NoInstall
    Build and smoke-test only; leave the exe in dist\ and skip the copy.

.PARAMETER Clean
    Delete build\ and dist\ before building.

.EXAMPLE
    .\scripts\build-exe.ps1
.EXAMPLE
    .\scripts\build-exe.ps1 -Clean
.EXAMPLE
    .\scripts\build-exe.ps1 -InstallDir D:\tools
#>
[CmdletBinding()]
param(
    [string] $InstallDir,
    [switch] $NoInstall,
    [switch] $Clean
)

$ErrorActionPreference = 'Stop'
Set-StrictMode -Version Latest

if (-not $InstallDir) {
    $InstallDir = if ($env:AGENTCLIP_INSTALL_DIR) {
        $env:AGENTCLIP_INSTALL_DIR
    } else {
        Join-Path $HOME 'Documents\PATH'
    }
}

$Root    = Split-Path -Parent $PSScriptRoot
$Spec    = Join-Path $Root 'packaging\agentclip.spec'
$DistExe = Join-Path $Root 'dist\agentclip.exe'

function Write-Step { param([string] $Message) Write-Host "==> $Message" -ForegroundColor Cyan }

# --- preflight ---------------------------------------------------------------

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is not on PATH. Install it from https://docs.astral.sh/uv/ and re-run."
}
if (-not (Test-Path $Spec)) {
    throw "Spec file not found at $Spec - is the repo checkout complete?"
}

Push-Location $Root
try {
    # --- clean ---------------------------------------------------------------

    if ($Clean) {
        Write-Step 'Cleaning build\ and dist\'
        foreach ($dir in 'build', 'dist') {
            $path = Join-Path $Root $dir
            if (Test-Path $path) { Remove-Item $path -Recurse -Force }
        }
    }

    # --- deps ----------------------------------------------------------------

    # Not --no-default-groups: that would uninstall pytest/ruff/mypy and break
    # the dev loop. Dev deps are kept out of the binary by the spec's excludes.
    Write-Step 'Syncing dependencies (uv sync --group build)'
    uv sync --group build
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed with exit code $LASTEXITCODE." }

    # --- build ---------------------------------------------------------------

    Write-Step 'Building onefile executable (this takes a minute or two)'
    uv run --group build pyinstaller --noconfirm $Spec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE." }

    if (-not (Test-Path $DistExe)) {
        throw "PyInstaller reported success but $DistExe is missing."
    }

    # --- smoke test ----------------------------------------------------------

    # cli.py imports agentclip.tui.app, which transitively imports every screen
    # and widget. A missing hidden import fails here, at build time, instead of
    # the first time a modal is opened.
    Write-Step 'Smoke-testing the frozen binary'
    $version = & $DistExe --version 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0 -or -not $version.Trim()) {
        Write-Host $version
        throw "Smoke test failed (exit code $LASTEXITCODE). Not installing the broken exe."
    }
    Write-Host "    $($version.Trim())" -ForegroundColor Green

    $sizeMb = [math]::Round((Get-Item $DistExe).Length / 1MB, 1)

    if ($NoInstall) {
        Write-Step "Built $DistExe ($sizeMb MB). Skipping install (-NoInstall)."
        return
    }

    # --- install -------------------------------------------------------------

    if (-not (Test-Path $InstallDir)) {
        Write-Step "Creating $InstallDir"
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    }

    $target = Join-Path $InstallDir 'agentclip.exe'
    Write-Step "Installing to $target"
    try {
        Copy-Item $DistExe $target -Force
    } catch [System.IO.IOException] {
        throw "Could not overwrite $target - it is most likely running. Close any open AgentClip and re-run."
    }

    # --- report --------------------------------------------------------------

    Write-Host ''
    Write-Host "Installed agentclip.exe ($sizeMb MB) to $InstallDir" -ForegroundColor Green

    $onPath = ($env:Path -split ';' | Where-Object { $_ -and (Test-Path $_) -and
        ((Resolve-Path $_).Path.TrimEnd('\') -eq (Resolve-Path $InstallDir).Path.TrimEnd('\')) })
    if (-not $onPath) {
        Write-Warning "$InstallDir is not on this shell's PATH. Add it, or open a new shell if you just did."
    }

    # Another agentclip earlier on PATH (e.g. a stale `uv tool install`) would
    # silently win every invocation, so say so loudly rather than just listing.
    $resolved = @(& where.exe agentclip 2>$null)
    if (-not $resolved) {
        Write-Host "Open a new shell, then run: agentclip --version" -ForegroundColor Yellow
    } elseif ($resolved[0].TrimEnd('\') -ieq $target.TrimEnd('\')) {
        Write-Host "'agentclip' resolves to the exe just installed." -ForegroundColor Green
    } else {
        Write-Warning "'agentclip' resolves to $($resolved[0]) - NOT the exe just installed."
        Write-Host '  Something earlier on PATH is shadowing it. If it is a uv tool install, remove it with:' -ForegroundColor Yellow
        Write-Host '      uv tool uninstall agentclip' -ForegroundColor Yellow
        Write-Host '  Full resolution order:' -ForegroundColor Yellow
        $resolved | ForEach-Object { Write-Host "      $_" }
    }
}
finally {
    Pop-Location
}
