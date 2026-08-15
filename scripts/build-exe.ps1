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
    #
    # Neither extra is optional HERE even though both are optional for a
    # from-source install: the shipped exe bundles the OpenCV matcher backend
    # (architecture.md 6) and BOTH UI shells (gui.md 5), and PyInstaller can
    # only collect a package that is present in the environment it is pointed
    # at. Worse, leaving one off does not merely skip it - `uv sync` prunes to
    # exactly what was asked for, so a sync without these flags would UNINSTALL
    # opencv/numpy/pywebview from the shared .venv and then build a lean exe
    # without a word.
    Write-Step 'Syncing dependencies (uv sync --group build --extra cv --extra gui)'
    uv sync --group build --extra cv --extra gui
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed with exit code $LASTEXITCODE." }

    # And prove it before spending two minutes on a build that cannot be right.
    # A missing cv2 produces no build error at all: the exe starts, runs, and
    # silently gives every service the anchor search while the editor tells the
    # user their build does not include OpenCV. A missing pywebview is the same
    # shape one shell over: the exe starts, runs, and answers --gui with an
    # "install the gui extra" line a frozen user cannot act on.
    Write-Step 'Verifying the cv extra is importable'
    uv run --group build python -c "import cv2, numpy; print(f'cv2 {cv2.__version__}, numpy {numpy.__version__}')"
    if ($LASTEXITCODE -ne 0) {
        throw "The cv extra is not importable, so the exe would be built without the OpenCV matcher backend. Fix the environment and re-run."
    }
    Write-Step 'Verifying the gui extra is importable'
    uv run --group build python -c "from importlib.metadata import version; import webview; print('pywebview ' + version('pywebview'))"
    if ($LASTEXITCODE -ne 0) {
        throw "The gui extra is not importable, so the exe would be built without the GUI shell. Fix the environment and re-run."
    }

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

    # --- bundled-backend check -----------------------------------------------

    # --version proves the app imports; it says nothing about a backend that is
    # only ever imported inside a function on a poll tick. --list-matchers
    # actually imports each one and reports what happened, run against the exe
    # that was just built - so this catches both halves of the failure: cv2 not
    # collected at all, and cv2 collected but unable to load its DLLs out of a
    # onefile extraction directory. Neither is visible at runtime until
    # somebody opens the service editor and is told their build does not
    # include OpenCV, which is exactly the report this check exists to stop
    # shipping.
    Write-Step 'Verifying the OpenCV backend is bundled AND loads'
    $matchers = & $DistExe --list-matchers 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0 -or $matchers -match 'NOT AVAILABLE') {
        Write-Host $matchers
        throw "The frozen exe cannot run the OpenCV matcher, so every service would silently fall back to the anchor search. Check that the cv extra is installed and packaging/agentclip.spec's hiddenimports still name cv2/numpy."
    }
    $matchers.Trim() -split "`n" | ForEach-Object { Write-Host "    $($_.Trim())" -ForegroundColor Green }

    # --- bundled-shell check -------------------------------------------------

    # The same argument one shell over. --version proves the TUI's import tree;
    # it says nothing about a GUI whose every piece is reached lazily - the
    # package only on --gui, pywebview only inside a function, and its winforms
    # backend only through webview/guilib.py's per-platform pick. --gui-smoke
    # walks that whole chain against the exe that was just built: it imports
    # pywebview (which drags in clr and the .NET runtime), READS all three page
    # assets back through importlib.resources - the classic frozen-app failure,
    # where the files are in the archive but the resource reader cannot find
    # them under _MEIPASS - and reports the renderer WebView2 would give it.
    #
    # `renderer=missing` is not a failure: it describes this machine's WebView2
    # runtime, which a packaging check has no business being blocked on. Only a
    # non-zero exit means the freeze is wrong. It is printed so the state is
    # never merely assumed.
    Write-Step 'Verifying the GUI shell is bundled AND resolves its page'
    $gui = & $DistExe --gui-smoke 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0) {
        Write-Host $gui
        throw "The frozen exe cannot open the GUI shell, so --gui would tell the user to install an extra they cannot install into an exe. Check that the gui extra is installed and packaging/agentclip.spec still names webview.platforms.winforms and the gui assets."
    }
    Write-Host "    $($gui.Trim())" -ForegroundColor Green

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
