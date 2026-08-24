<#
.SYNOPSIS
    Freeze AgentClip's Windows executables and drop them on your PATH.

.DESCRIPTION
    Builds PyInstaller onefile binaries, smoke-tests each frozen artifact, then
    copies them into a folder that is already on PATH:

      agentclip.exe          the full app - GUI shell, deprecated TUI, OpenCV backend
      agentclip-monitor.exe  the standing monitor, the binary that runs on the
                             machine whose SCREEN shows the chat - a VM, or this
                             PC in split mode (docs/design/ui-monitor.md 2.5, 6.5)

    The engine half (agentclip-engine) is NOT built here: it runs on an SSH
    target, which is a Linux box, so scripts/build-exe.sh owns it. Windows is
    where the app is DRIVEN and where the pixels usually are, so it owns these
    two.

.PARAMETER InstallDir
    Where to copy the exes. Defaults to $env:AGENTCLIP_INSTALL_DIR, else
    "$HOME\Documents\PATH". The folder is created if it does not exist.

.PARAMETER MonitorOnly
    Build only agentclip-monitor.exe; skip the full app (and its `gui` extra,
    which a machine that only serves its screen need not install). The mirror of
    build-exe.sh's --engine-only.

.PARAMETER NoInstall
    Build and smoke-test only; leave the exes in dist\ and skip the copy.

.PARAMETER Clean
    Delete build\ and dist\ before building.

.EXAMPLE
    .\scripts\build-exe.ps1
.EXAMPLE
    .\scripts\build-exe.ps1 -Clean
.EXAMPLE
    .\scripts\build-exe.ps1 -MonitorOnly
.EXAMPLE
    .\scripts\build-exe.ps1 -InstallDir D:\tools
#>
[CmdletBinding()]
param(
    [string] $InstallDir,
    [switch] $MonitorOnly,
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

$Root           = Split-Path -Parent $PSScriptRoot
$Spec           = Join-Path $Root 'packaging\agentclip.spec'
$MonitorSpec    = Join-Path $Root 'packaging\agentclip-monitor.spec'
$DistExe        = Join-Path $Root 'dist\agentclip.exe'
$DistMonitorExe = Join-Path $Root 'dist\agentclip-monitor.exe'

$BuildApp = -not $MonitorOnly

function Write-Step { param([string] $Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Get-SizeMb { param([string] $Path) [math]::Round((Get-Item $Path).Length / 1MB, 1) }

# --- preflight ---------------------------------------------------------------

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is not on PATH. Install it from https://docs.astral.sh/uv/ and re-run."
}
if ($BuildApp -and -not (Test-Path $Spec)) {
    throw "Spec file not found at $Spec - is the repo checkout complete?"
}
if (-not (Test-Path $MonitorSpec)) {
    throw "Spec file not found at $MonitorSpec - is the repo checkout complete?"
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

    # ONE sync, because `uv sync` prunes the environment to exactly what was
    # asked for - a second sync with a different extra set would silently
    # uninstall what the first one put there. Which extras depends on the mode,
    # and that is the whole point of -MonitorOnly: pywebview is only reached by
    # the app's GUI shell, and packaging/agentclip-monitor.spec excludes it.
    #
    # Not --no-default-groups: that would uninstall pytest/ruff/mypy and break
    # the dev loop. Dev deps are kept out of the binaries by the specs' excludes.
    #
    # `cv` is in BOTH sets and is not optional HERE even though it is optional
    # for a from-source install: both exes bundle the OpenCV matcher backend
    # (architecture.md 6; the monitor exe is where every template search
    # actually RUNS), and PyInstaller can only collect a package that is present
    # in the environment it is pointed at. Worse, leaving an extra off does not
    # merely skip it - `uv sync` prunes to exactly what was asked for, so a sync
    # without these flags would UNINSTALL opencv/numpy/pywebview from the shared
    # .venv and then build lean exes without a word.
    $extras = if ($BuildApp) { @('--extra', 'cv', '--extra', 'gui') } else { @('--extra', 'cv') }
    Write-Step "Syncing dependencies (uv sync --group build $($extras -join ' '))"
    uv sync --group build @extras
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed with exit code $LASTEXITCODE." }

    # And prove it before spending two minutes on a build that cannot be right.
    # A missing cv2 produces no build error at all: the exe starts, runs, and
    # silently gives every service the anchor search while the editor tells the
    # user their build does not include OpenCV. A missing pywebview is the same
    # shape one shell over: the exe starts, runs, and answers a plain launch -
    # the GUI is the default shell now - with an "install the gui extra" line a
    # frozen user cannot act on.
    Write-Step 'Verifying the cv extra is importable'
    uv run --group build python -c "import cv2, numpy; print(f'cv2 {cv2.__version__}, numpy {numpy.__version__}')"
    if ($LASTEXITCODE -ne 0) {
        throw "The cv extra is not importable, so the exe would be built without the OpenCV matcher backend. Fix the environment and re-run."
    }
    if ($BuildApp) {
        Write-Step 'Verifying the gui extra is importable'
        uv run --group build python -c "from importlib.metadata import version; import webview; print('pywebview ' + version('pywebview'))"
        if ($LASTEXITCODE -ne 0) {
            throw "The gui extra is not importable, so the exe would be built without the GUI shell. Fix the environment and re-run."
        }
    }

    $installed = @()

    # --- build: the full app -------------------------------------------------

    if ($BuildApp) {
        Write-Step 'Building agentclip.exe (this takes a minute or two)'
        uv run --group build pyinstaller --noconfirm $Spec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE." }

        if (-not (Test-Path $DistExe)) {
            throw "PyInstaller reported success but $DistExe is missing."
        }

        # --- smoke test ------------------------------------------------------

        # cli.py imports agentclip.shell.tui.app (the deprecated --tui shell) at
        # module level, which transitively imports every screen and widget. A
        # missing hidden import fails here, at build time, instead of the first
        # time a modal is opened.
        Write-Step 'Smoke-testing the frozen binary'
        $version = & $DistExe --version 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0 -or -not $version.Trim()) {
            Write-Host $version
            throw "Smoke test failed (exit code $LASTEXITCODE). Not installing the broken exe."
        }
        Write-Host "    $($version.Trim())" -ForegroundColor Green

        # --- bundled-backend check -------------------------------------------

        # --version proves the app imports; it says nothing about a backend that
        # is only ever imported inside a function on a poll tick. --list-matchers
        # actually imports each one and reports what happened, run against the
        # exe that was just built - so this catches both halves of the failure:
        # cv2 not collected at all, and cv2 collected but unable to load its DLLs
        # out of a onefile extraction directory. Neither is visible at runtime
        # until somebody opens the service editor and is told their build does
        # not include OpenCV, which is exactly the report this check exists to
        # stop shipping.
        Write-Step 'Verifying the OpenCV backend is bundled AND loads'
        $matchers = & $DistExe --list-matchers 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0 -or $matchers -match 'NOT AVAILABLE') {
            Write-Host $matchers
            throw "The frozen exe cannot run the OpenCV matcher, so every service would silently fall back to the anchor search. Check that the cv extra is installed and packaging/agentclip.spec's hiddenimports still name cv2/numpy."
        }
        $matchers.Trim() -split "`n" | ForEach-Object { Write-Host "    $($_.Trim())" -ForegroundColor Green }

        # --- bundled-shell check ---------------------------------------------

        # The same argument one shell over, and the sharper half now that the GUI
        # is what a bare `agentclip` opens: --version proves the TUI's import tree
        # (that one is module-level), and says nothing about a GUI whose every piece
        # is reached lazily - the package only on a GUI launch, pywebview only
        # inside a function, and its winforms backend only through
        # webview/guilib.py's per-platform pick. --gui-smoke
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
            throw "The frozen exe cannot open the GUI shell, so the default launch would tell the user to install an extra they cannot install into an exe. Check that the gui extra is installed and packaging/agentclip.spec still names webview.platforms.winforms and the gui assets."
        }
        Write-Host "    $($gui.Trim())" -ForegroundColor Green

        Write-Host "    agentclip.exe: $(Get-SizeMb $DistExe) MB" -ForegroundColor Green
        $installed += $DistExe
    }

    # --- build: the monitor half ---------------------------------------------

    Write-Step 'Building agentclip-monitor.exe'
    uv run --group build pyinstaller --noconfirm $MonitorSpec
    if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE." }

    if (-not (Test-Path $DistMonitorExe)) {
        throw "PyInstaller reported success but $DistMonitorExe is missing."
    }

    # --version is one of the two invocations that is neither a run nor a usage
    # error, and it is a real check: argparse runs the version action before the
    # required --port check, so this walks the entire module-level import tree -
    # config, the clipboard provider, LocalUIMonitor, the wire, the server loop -
    # without listening on anything.
    Write-Step 'Smoke-testing agentclip-monitor.exe'
    $monitorVersion = & $DistMonitorExe --version 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0 -or -not $monitorVersion.Trim().StartsWith('agentclip-monitor ')) {
        Write-Host $monitorVersion
        throw "agentclip-monitor --version failed or answered something unexpected (exit code $LASTEXITCODE). Not installing the broken exe."
    }
    Write-Host "    $($monitorVersion.Trim())" -ForegroundColor Green

    # And the same bundled-backend check as the app, for a sharper version of the
    # same reason: the monitor binary is where every template search actually
    # runs (ui-monitor.md 2.5), and cv2 gets there through a lazy,
    # try/except-guarded import - so a freeze that lost OpenCV, or kept it and
    # cannot load its DLLs out of a onefile extraction directory, raises nothing
    # at all. It just hands every service the anchor search on the one machine
    # doing the matching, where no service editor is open to complain about it.
    Write-Step 'Verifying the OpenCV backend is bundled AND loads in the monitor'
    $monitorMatchers = & $DistMonitorExe --list-matchers 2>&1 | Out-String
    if ($LASTEXITCODE -ne 0 -or $monitorMatchers -match 'NOT AVAILABLE') {
        Write-Host $monitorMatchers
        throw "The frozen agentclip-monitor cannot run the OpenCV matcher, so every service would silently fall back to the anchor search on the machine that does the matching. Check that the cv extra is installed and packaging/agentclip-monitor.spec's hiddenimports still name cv2/numpy."
    }
    $monitorMatchers.Trim() -split "`n" | ForEach-Object { Write-Host "    $($_.Trim())" -ForegroundColor Green }

    Write-Host "    agentclip-monitor.exe: $(Get-SizeMb $DistMonitorExe) MB" -ForegroundColor Green
    $installed += $DistMonitorExe

    if ($NoInstall) {
        Write-Step "Built into $(Join-Path $Root 'dist'). Skipping install (-NoInstall)."
        return
    }

    # --- install -------------------------------------------------------------

    if (-not (Test-Path $InstallDir)) {
        Write-Step "Creating $InstallDir"
        New-Item -ItemType Directory -Path $InstallDir -Force | Out-Null
    }

    foreach ($bin in $installed) {
        $name   = Split-Path -Leaf $bin
        $target = Join-Path $InstallDir $name
        Write-Step "Installing to $target"
        try {
            Copy-Item $bin $target -Force
        } catch [System.IO.IOException] {
            throw "Could not overwrite $target - it is most likely running. Close any open AgentClip (the monitor is a STANDING process - end it with Ctrl+C) and re-run."
        }
    }

    # --- report --------------------------------------------------------------

    Write-Host ''
    foreach ($bin in $installed) {
        Write-Host "Installed $(Split-Path -Leaf $bin) ($(Get-SizeMb $bin) MB) to $InstallDir" -ForegroundColor Green
    }

    $onPath = ($env:Path -split ';' | Where-Object { $_ -and (Test-Path $_) -and
        ((Resolve-Path $_).Path.TrimEnd('\') -eq (Resolve-Path $InstallDir).Path.TrimEnd('\')) })
    if (-not $onPath) {
        Write-Warning "$InstallDir is not on this shell's PATH. Add it, or open a new shell if you just did."
    }

    # Another agentclip earlier on PATH (e.g. a stale `uv tool install`) would
    # silently win every invocation, so say so loudly rather than just listing.
    # Asked of each name separately: `uv tool install agentclip` puts a shim for
    # every one of [project.scripts] on PATH, so agentclip-monitor.exe can be
    # shadowed on its own - and a launcher on the monitor machine starts it BY
    # NAME, so whatever PATH resolves is what actually serves the screen.
    foreach ($bin in $installed) {
        $name     = Split-Path -Leaf $bin
        $target   = Join-Path $InstallDir $name
        $resolved = @(& where.exe $name 2>$null)
        if (-not $resolved) {
            Write-Host "Open a new shell, then run: $([IO.Path]::GetFileNameWithoutExtension($name)) --version" -ForegroundColor Yellow
        } elseif ($resolved[0].TrimEnd('\') -ieq $target.TrimEnd('\')) {
            Write-Host "'$name' resolves to the exe just installed." -ForegroundColor Green
        } else {
            Write-Warning "'$name' resolves to $($resolved[0]) - NOT the exe just installed."
            Write-Host '  Something earlier on PATH is shadowing it. If it is a uv tool install, remove it with:' -ForegroundColor Yellow
            Write-Host '      uv tool uninstall agentclip' -ForegroundColor Yellow
            Write-Host '  Full resolution order:' -ForegroundColor Yellow
            $resolved | ForEach-Object { Write-Host "      $_" }
        }
    }
}
finally {
    Pop-Location
}
