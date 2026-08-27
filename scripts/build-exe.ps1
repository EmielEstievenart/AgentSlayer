<#
.SYNOPSIS
    Freeze AgentClip's THREE Windows executables and drop them on your PATH.

.DESCRIPTION
    Builds PyInstaller onefile binaries, smoke-tests each frozen artifact, then
    copies them into a folder that is already on PATH:

      agentclip.exe          the full app - the Chat UI window (a brain: it
                             carries no screen half since ui-monitor.md 10)
      agentclip-engine.exe   the engine half, the binary an SSH target runs
                             (docs/design/remote-executor.md section 2.6)
      agentclip-monitor.exe  the standing monitor, the binary that runs on the
                             machine whose SCREEN shows the chat - a VM, or this
                             PC in split mode (docs/design/ui-monitor.md 2.5, 6.5)

    The Windows counterpart of scripts/build-exe.sh, which builds the same three
    for a POSIX box. Windows is where the app is DRIVEN and where the pixels
    usually are, so a plain run builds everything; the two "Only" switches below
    skip the full app entirely for the case each is named after - a target or a
    VM that will never open a window and need not carry the extras for one.

.PARAMETER InstallDir
    Where to copy the exes. Defaults to $env:AGENTCLIP_INSTALL_DIR, else
    "$HOME\Documents\PATH". The folder is created if it does not exist.

.PARAMETER EngineOnly
    Build only agentclip-engine.exe; skip the full app (and its cv/gui extras,
    which a headless target need not install). The mirror of build-exe.sh's
    --engine-only.

.PARAMETER MonitorOnly
    Build only agentclip-monitor.exe; skip the full app (and its mcp extra,
    which a machine that only serves its screen never runs a server for). It
    does NOT skip gui any more: since docs/design/ui-monitor.md 9.1 the monitor
    binary opens the Monitor UI - the service editor, the region picker and the
    Serve panel - and only its --headless door runs without a toolkit. The
    mirror of build-exe.sh's --monitor-only.

    Given together, -EngineOnly and -MonitorOnly build those two halves and
    still skip the full app: they select halves, they are not exclusive modes.

.PARAMETER NoInstall
    Build and smoke-test only; leave the exes in dist\ and skip the copy.

.PARAMETER Clean
    Delete build\ and dist\ before building.

.EXAMPLE
    .\scripts\build-exe.ps1
.EXAMPLE
    .\scripts\build-exe.ps1 -Clean
.EXAMPLE
    .\scripts\build-exe.ps1 -EngineOnly
.EXAMPLE
    .\scripts\build-exe.ps1 -MonitorOnly
.EXAMPLE
    .\scripts\build-exe.ps1 -InstallDir D:\tools
#>
[CmdletBinding()]
param(
    [string] $InstallDir,
    [switch] $EngineOnly,
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
$EngineSpec     = Join-Path $Root 'packaging\agentclip-engine.spec'
$MonitorSpec    = Join-Path $Root 'packaging\agentclip-monitor.spec'
$DistExe        = Join-Path $Root 'dist\agentclip.exe'
$DistEngineExe  = Join-Path $Root 'dist\agentclip-engine.exe'
$DistMonitorExe = Join-Path $Root 'dist\agentclip-monitor.exe'

# What each run actually builds. Naming EITHER half drops the full app - that is
# what the "Only" in both switches means - and naming neither builds everything.
# The engine and the monitor are otherwise independent: they are opposite halves
# on opposite machines, and asking for both at once is a coherent thing to want
# (one box that is both an SSH target and the screen), so it is not an error.
$BuildApp     = -not ($EngineOnly -or $MonitorOnly)
$BuildEngine  = $EngineOnly -or $BuildApp
$BuildMonitor = $MonitorOnly -or $BuildApp

function Write-Step { param([string] $Message) Write-Host "==> $Message" -ForegroundColor Cyan }
function Get-SizeMb { param([string] $Path) [math]::Round((Get-Item $Path).Length / 1MB, 1) }

# --- preflight ---------------------------------------------------------------

if (-not (Get-Command uv -ErrorAction SilentlyContinue)) {
    throw "uv is not on PATH. Install it from https://docs.astral.sh/uv/ and re-run."
}
if ($BuildApp -and -not (Test-Path $Spec)) {
    throw "Spec file not found at $Spec - is the repo checkout complete?"
}
if ($BuildEngine -and -not (Test-Path $EngineSpec)) {
    throw "Spec file not found at $EngineSpec - is the repo checkout complete?"
}
if ($BuildMonitor -and -not (Test-Path $MonitorSpec)) {
    throw "Spec file not found at $MonitorSpec - is the repo checkout complete?"
}

Push-Location $Root
try {
    # --- clean ---------------------------------------------------------------

    if ($Clean) {
        Write-Step 'Cleaning build\ and dist\'
        foreach ($dir in 'build', 'dist') {
            $path = Join-Path $Root $dir
            if (-not (Test-Path $path)) { continue }
            try {
                Remove-Item $path -Recurse -Force -ErrorAction Stop
            } catch {
                # The folder itself is somebody's current directory (a terminal
                # or an Explorer window sitting in dist\). Its CONTENTS can still
                # go, and that is all a clean build needs.
                Write-Host "    $dir\ is held open by another process - emptying it instead"
                Get-ChildItem $path -Force | Remove-Item -Recurse -Force
            }
        }
    }

    # --- deps ----------------------------------------------------------------

    # ONE sync, because `uv sync` prunes the environment to exactly what was
    # asked for - a second sync with a different extra set would silently
    # uninstall what the first one put there. Which extras depends on WHICH EXES
    # this run builds, and that is the whole point of the two "Only" switches:
    # the engine exe imports neither pywebview nor OpenCV
    # (packaging/agentclip-engine.spec excludes both), while the app and the
    # monitor each open a pywebview window and the monitor alone bundles the
    # matcher backend.
    #
    # Not --no-default-groups: that would uninstall pytest/ruff/mypy and break
    # the dev loop. Dev deps are kept out of the binaries by the specs' excludes.
    #
    # None of the three is optional HERE even though all three are extras
    # everywhere else. `cv` covers the MONITOR alone: the monitor exe is where
    # every template search runs, and since ui-monitor.md 10 the app exe carries
    # no matcher backend at all (packaging/agentclip.spec excludes cv2/numpy).
    # `gui` covers the app AND the monitor, since ui-monitor.md 9.1: the monitor
    # binary IS the Monitor UI, and only --headless runs without a toolkit. `mcp` covers the app AND the engine: the
    # engine exe exists to run MCP servers on the target
    # (docs/design/remote-executor.md 2.7). And PyInstaller can only collect a
    # package that is present in the environment it is pointed at. Worse,
    # leaving an extra off does not merely skip it - `uv sync` prunes to exactly
    # what was asked for, so a sync without these flags would UNINSTALL
    # opencv/numpy/pywebview/mcp from the shared .venv and then build lean exes
    # without a word.
    $extras = @()
    if ($BuildMonitor)               { $extras += @('--extra', 'cv') }
    if ($BuildApp -or $BuildMonitor) { $extras += @('--extra', 'gui') }
    if ($BuildApp -or $BuildEngine)  { $extras += @('--extra', 'mcp') }
    Write-Step "Syncing dependencies (uv sync --group build $($extras -join ' '))"
    uv sync --group build @extras
    if ($LASTEXITCODE -ne 0) { throw "uv sync failed with exit code $LASTEXITCODE." }

    # And prove it before spending two minutes on a build that cannot be right.
    # A missing cv2 produces no build error at all: the monitor exe starts, runs,
    # and silently gives every service the anchor search while the editor tells
    # the user their build does not include OpenCV. A missing pywebview is the
    # same shape one shell over: the exe starts, runs, and answers a plain launch
    # with an "install the gui extra" line a frozen user cannot act on.
    if ($BuildMonitor) {
        Write-Step 'Verifying the cv extra is importable'
        uv run --group build python -c "import cv2, numpy; print(f'cv2 {cv2.__version__}, numpy {numpy.__version__}')"
        if ($LASTEXITCODE -ne 0) {
            throw "The cv extra is not importable, so agentclip-monitor would be built without the OpenCV matcher backend. Fix the environment and re-run."
        }
    }
    if ($BuildApp -or $BuildMonitor) {
        Write-Step 'Verifying the gui extra is importable'
        uv run --group build python -c "from importlib.metadata import version; import webview; print('pywebview ' + version('pywebview'))"
        if ($LASTEXITCODE -ne 0) {
            throw "The gui extra is not importable, so the exe would be built without its pywebview window (the Chat UI, or the Monitor UI). Fix the environment and re-run."
        }
    }
    # The engine's counterpart, and the same silent shape one layer down: every
    # use of the SDK in executor/mcp/client.py is a function-body import, so a
    # missing `mcp` produces no build error at all - just a frozen engine whose
    # every server on the target reports missing_sdk and names a fix (install the
    # extra) that cannot be applied to a binary with no environment to install
    # into.
    if ($BuildApp -or $BuildEngine) {
        Write-Step 'Verifying the mcp extra is importable'
        uv run --group build python -c "from importlib.metadata import version; import mcp; print('mcp ' + version('mcp'))"
        if ($LASTEXITCODE -ne 0) {
            throw "The mcp extra is not importable, so agentclip-engine would be built without the MCP SDK and every server on the target would report missing_sdk - naming a fix that cannot be applied to a frozen binary. Fix the environment and re-run."
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

        # --version answers from the import tree cli.py pulls at module level -
        # config, the engine link, the executor seam - so a hidden import missed
        # anywhere below that line fails here, at build time, instead of on
        # somebody's desk.
        Write-Step 'Smoke-testing the frozen binary'
        $version = & $DistExe --version 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0 -or -not $version.Trim()) {
            Write-Host $version
            throw "Smoke test failed (exit code $LASTEXITCODE). Not installing the broken exe."
        }
        Write-Host "    $($version.Trim())" -ForegroundColor Green

        # No --list-matchers here, and it is not an omission: since
        # docs/design/ui-monitor.md 10 the Chat UI hosts no monitor and runs no
        # matcher - packaging/agentclip.spec EXCLUDES cv2/numpy, and the CLI has
        # no such flag any more. The backend is checked against
        # agentclip-monitor.exe below, on the binary that actually does the
        # matching.

        # --- bundled-shell check ---------------------------------------------

        # The sharper half, because --version proves only what is imported at
        # module level and the shell is not: every piece of it is reached lazily -
        # the package only when the window opens, pywebview only inside a
        # function, and its winforms backend only through webview/guilib.py's
        # per-platform pick. --gui-smoke
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

    # --- build: the engine half ----------------------------------------------

    if ($BuildEngine) {
        Write-Step 'Building agentclip-engine.exe'
        uv run --group build pyinstaller --noconfirm $EngineSpec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE." }

        if (-not (Test-Path $DistEngineExe)) {
            throw "PyInstaller reported success but $DistEngineExe is missing."
        }

        # --version is the engine's whole smoke test, and it is a real one:
        # argparse runs the version action before the --project required-check,
        # so this is the one invocation that walks the entire module-level import
        # tree - config, the session factory, the server loop, the executor's
        # tool registry - without needing a project, a link peer, or a single
        # frame on stdout.
        #
        # The ANSWER is checked, not just the exit code. On a target that stream
        # IS the protocol (packaging/agentclip-engine.spec: stdin carries frames
        # in, stdout carries them out, which is also why that spec keeps
        # console=True), so anything unexpected on it is a problem worth failing
        # here for rather than meeting later as a handshake error over an SSH
        # exec channel.
        Write-Step 'Smoke-testing agentclip-engine.exe'
        $engineVersion = & $DistEngineExe --version 2>&1 | Out-String
        if ($LASTEXITCODE -ne 0 -or -not $engineVersion.Trim().StartsWith('agentclip-engine ')) {
            Write-Host $engineVersion
            throw "agentclip-engine --version failed or answered something unexpected (exit code $LASTEXITCODE). Not installing the broken exe."
        }
        Write-Host "    $($engineVersion.Trim())" -ForegroundColor Green

        # No --list-matchers here, and that is not an omission: the engine half
        # touches no screen - it is handed frames - and its spec EXCLUDES cv2 and
        # numpy on purpose, so asking this binary about matcher backends would be
        # asking it about a half it deliberately does not have.
        Write-Host "    agentclip-engine.exe: $(Get-SizeMb $DistEngineExe) MB" -ForegroundColor Green
        $installed += $DistEngineExe
    }

    # --- build: the monitor half ---------------------------------------------

    if ($BuildMonitor) {
        Write-Step 'Building agentclip-monitor.exe'
        uv run --group build pyinstaller --noconfirm $MonitorSpec
        if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed with exit code $LASTEXITCODE." }

        if (-not (Test-Path $DistMonitorExe)) {
            throw "PyInstaller reported success but $DistMonitorExe is missing."
        }

        # --version is one of the two invocations that is neither a run nor a usage
        # error, and it is a real check: the version action answers from inside
        # parsing, before either door is chosen, so this walks the entire
        # module-level import tree - config, the clipboard provider,
        # LocalUIMonitor, the wire, the server loop, and since 9.1 the Monitor UI
        # dispatcher above them - without opening a window or listening on
        # anything. It needs no --port: the port is a Serve panel field now and
        # is required only under --headless.
        #
        # There is deliberately no --gui-smoke here, unlike agentclip.exe. That
        # check lives in cli.py (`_gui_smoke`), which is off this binary's
        # layering allowance and is not in it to be called - so what a frozen
        # Monitor UI's pywebview collection is proven by is the app binary's own
        # --gui-smoke, which imports the same `webview`, the same backend and the
        # same runtime out of the same environment. Giving the monitor one of its
        # own means moving that function into a package both binaries may import;
        # worth doing, and not in this phase.
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
    }

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
    # every one of [project.scripts] on PATH, so either half can be shadowed on
    # its own - and both halves are started BY NAME on the machine they run on
    # (the master launches agentclip-engine over an SSH exec channel, a launcher
    # on the monitor machine starts agentclip-monitor), so whatever PATH resolves
    # over there is what actually serves the screen or hosts the session.
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
