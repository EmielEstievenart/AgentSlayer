#!/usr/bin/env bash
#
# Freeze AgentClip's THREE executables and drop them on your PATH.
#
#   agentclip          the full app - GUI shell, OpenCV backend
#   agentclip-engine   the engine half, the binary an SSH target runs
#                      (docs/design/remote-executor.md section 2.6)
#   agentclip-monitor  the monitor half, the standing binary that runs on the
#                      machine whose SCREEN shows the chat - a VM, or this PC in
#                      split mode (docs/design/ui-monitor.md 2.5, 6.5)
#
# The POSIX counterpart of scripts/build-exe.ps1, which builds the app and the
# monitor but never the engine, because Windows is where the app is DRIVEN.
# Linux is where it is HOSTED - so this one always builds the engine, and the
# two "only" flags below skip the full app entirely for the common case: a
# target or a VM that will never open a window and may not have the system
# libraries to.
#
# Usage:
#   scripts/build-exe.sh [--clean] [--engine-only] [--monitor-only]
#                        [--no-install] [--install-dir DIR] [--help]

set -euo pipefail

# Derive the repo root from THIS FILE, never from the caller's directory: the
# specs anchor their own paths to SPECPATH, but `uv sync` and `uv run` resolve
# the project from the CWD, so a run from anywhere else would sync some other
# environment (or none).
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
APP_SPEC="$ROOT/packaging/agentclip.spec"
ENGINE_SPEC="$ROOT/packaging/agentclip-engine.spec"
MONITOR_SPEC="$ROOT/packaging/agentclip-monitor.spec"

clean=0
engine_only=0
monitor_only=0
no_install=0
install_dir="${AGENTCLIP_INSTALL_DIR:-$HOME/.local/bin}"

usage() {
    sed -n '3,21p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --clean             delete build/ and dist/ before building
  --engine-only       build only agentclip-engine; skip the full app (and its
                      cv/gui extras, which a headless target need not install)
  --monitor-only      build only agentclip-monitor; skip the full app (and its
                      mcp extra, which a machine that only serves its screen
                      never runs a server for). It still syncs gui: the monitor
                      binary opens the Monitor UI since ui-monitor.md 9.1
  --no-install        build and smoke-test only; leave the binaries in dist/
  --install-dir DIR   where to copy them
                      (default: $AGENTCLIP_INSTALL_DIR, else ~/.local/bin)
  -h, --help          this text

Given together, --engine-only and --monitor-only build those two halves and
still skip the full app: they select halves, they are not exclusive modes.
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --clean) clean=1 ;;
        --engine-only) engine_only=1 ;;
        --monitor-only) monitor_only=1 ;;
        --no-install) no_install=1 ;;
        --install-dir)
            [ $# -ge 2 ] || { echo "--install-dir needs a directory" >&2; exit 2; }
            install_dir="$2"
            shift
            ;;
        --install-dir=*) install_dir="${1#--install-dir=}" ;;
        -h|--help) usage; exit 0 ;;
        *) echo "Unknown option: $1 (try --help)" >&2; exit 2 ;;
    esac
    shift
done

# What each run actually builds. Naming EITHER half drops the full app - that is
# what the "-only" in both flags means - and naming neither builds everything.
# The engine and the monitor are otherwise independent: they are opposite halves
# on opposite machines, and asking for both at once is a coherent thing to want
# (one box that is both an SSH target and the screen), so it is not an error.
build_app=1
build_engine=1
build_monitor=1
if [ "$engine_only" -eq 1 ] || [ "$monitor_only" -eq 1 ]; then
    build_app=0
    build_engine="$engine_only"
    build_monitor="$monitor_only"
fi

# Colour only when a human is looking; a CI log gets plain text.
if [ -t 1 ]; then
    C_STEP=$'\033[36m'; C_OK=$'\033[32m'; C_WARN=$'\033[33m'; C_ERR=$'\033[31m'; C_OFF=$'\033[0m'
else
    C_STEP=''; C_OK=''; C_WARN=''; C_ERR=''; C_OFF=''
fi

step() { printf '%s==> %s%s\n' "$C_STEP" "$1" "$C_OFF"; }
ok()   { printf '%s    %s%s\n' "$C_OK" "$1" "$C_OFF"; }
warn() { printf '%sWARNING: %s%s\n' "$C_WARN" "$1" "$C_OFF" >&2; }
die()  { printf '%sERROR: %s%s\n' "$C_ERR" "$1" "$C_OFF" >&2; exit 1; }

# PyInstaller names the artifact after EXE(name=...) plus the platform's
# executable suffix. Everything below asks for the path rather than assuming
# one, so a run under Git Bash or MSYS finds the .exe it actually produced
# instead of reporting a missing file it was never going to write.
dist_path() {
    if [ -f "$ROOT/dist/$1.exe" ] && [ ! -f "$ROOT/dist/$1" ]; then
        printf '%s\n' "$ROOT/dist/$1.exe"
    else
        printf '%s\n' "$ROOT/dist/$1"
    fi
}

size_of() {
    # Coreutils and BSD/macOS `stat` disagree on flags; `wc -c` agrees with both.
    awk -v bytes="$(wc -c < "$1")" 'BEGIN { printf "%.1f MB", bytes / 1048576 }'
}

# --- preflight ---------------------------------------------------------------

command -v uv >/dev/null 2>&1 ||
    die "uv is not on PATH. Install it from https://docs.astral.sh/uv/ and re-run."
if [ "$build_app" -eq 1 ] && [ ! -f "$APP_SPEC" ]; then
    die "Spec file not found at $APP_SPEC - is the repo checkout complete?"
fi
if [ "$build_engine" -eq 1 ] && [ ! -f "$ENGINE_SPEC" ]; then
    die "Spec file not found at $ENGINE_SPEC - is the repo checkout complete?"
fi
if [ "$build_monitor" -eq 1 ] && [ ! -f "$MONITOR_SPEC" ]; then
    die "Spec file not found at $MONITOR_SPEC - is the repo checkout complete?"
fi

cd "$ROOT"

# --- clean -------------------------------------------------------------------

if [ "$clean" -eq 1 ]; then
    step 'Cleaning build/ and dist/'
    rm -rf "$ROOT/build" "$ROOT/dist"
fi

# --- deps --------------------------------------------------------------------

# ONE sync, because `uv sync` prunes the environment to exactly what was asked
# for - a second sync with a different extra set would silently uninstall what
# the first one put there. Which extras depends on WHICH BINARIES this run
# builds, and that is the whole point of the "-only" flags: opencv and pywebview
# are heavy wheels whose Linux builds want system libraries a bare target may
# not have, and the engine binary imports neither
# (packaging/agentclip-engine.spec excludes them).
#
# `cv` covers the app AND the monitor - the monitor binary is where every
# template search actually RUNS (ui-monitor.md 2.5), so if anything it needs the
# backend more than the app does. `gui` covers them both too, since 9.1: the
# Monitor UI is a shell package and it ships in the MONITOR binary now, because
# it runs where the pixels are. Only that binary's --headless door opens no
# window, and it is one delegation inside the same entry point.
#
# Neither of those, nor `mcp`, is optional here even though all three are extras
# everywhere else: the engine binary exists to run MCP servers on the target
# (design section 2.7), and PyInstaller can only collect a package that is
# present in the environment it is pointed at.
#
# Not --no-default-groups: that would uninstall pytest/ruff/mypy and break the
# dev loop. Dev deps are kept out of the binaries by the specs' excludes.
#
# Spelled as if-blocks rather than `[ ... ] && extras+=(...)` one-liners: under
# `set -e` a trailing && list that evaluates false IS a failed command, so the
# terse form would abort the script on exactly the runs that need fewest extras.
extras=()
if [ "$build_app" -eq 1 ] || [ "$build_monitor" -eq 1 ]; then
    extras+=(--extra cv)
fi
if [ "$build_app" -eq 1 ] || [ "$build_monitor" -eq 1 ]; then
    extras+=(--extra gui)
fi
if [ "$build_app" -eq 1 ] || [ "$build_engine" -eq 1 ]; then
    extras+=(--extra mcp)
fi
step "Syncing dependencies (uv sync --group build ${extras[*]})"
uv sync --group build "${extras[@]}" || die "uv sync failed."

# And prove it before spending minutes on builds that cannot be right. Every one
# of these extras is reached by a LAZY, try/except-guarded import, so its absence
# produces no build error at all - just a binary that is quietly missing a
# feature and blames the user's install for it.
if [ "$build_app" -eq 1 ] || [ "$build_monitor" -eq 1 ]; then
    step 'Verifying the cv extra is importable'
    uv run --group build python -c \
        "import cv2, numpy; print(f'cv2 {cv2.__version__}, numpy {numpy.__version__}')" ||
        die "The cv extra is not importable, so the binaries would be built without the OpenCV matcher backend and every service would silently fall back to the anchor search. Fix the environment and re-run."
fi

if [ "$build_app" -eq 1 ] || [ "$build_monitor" -eq 1 ]; then
    step 'Verifying the gui extra is importable'
    uv run --group build python -c \
        "from importlib.metadata import version; import webview; print('pywebview ' + version('pywebview'))" ||
        die "The gui extra is not importable, so a binary would be built without its pywebview window - agentclip's DEFAULT shell, or agentclip-monitor's Monitor UI - and every launch would tell the user to install an extra they cannot install into a binary. Fix the environment and re-run."
fi

if [ "$build_app" -eq 1 ] || [ "$build_engine" -eq 1 ]; then
    step 'Verifying the mcp extra is importable'
    uv run --group build python -c \
        "from importlib.metadata import version; import mcp; print('mcp ' + version('mcp'))" ||
        die "The mcp extra is not importable, so agentclip-engine would be built without the MCP SDK and every server on the target would report missing_sdk - naming a fix that cannot be applied to a frozen binary. Fix the environment and re-run."
fi

# tkinter is the exception in this list: it is not an extra and not even a pip
# package, it is a stdlib module that many Linux distributions ship SEPARATELY
# from the interpreter. driver/screen/overlay.py imports it inside a function so
# a tk-less machine can still run the rest of the app, and
# packaging/agentclip-monitor.spec names it as a hidden import so the frozen
# build keeps the region picker - but PyInstaller's tkinter hook can only bundle
# a Tcl/Tk that EXISTS. Miss it and the build succeeds with a "module not found"
# buried in the log, and the failure lands on the monitor machine, which is the
# one machine where the region a user drags is actually drawn.
#
# Checked only when the monitor is built: the app carries the same picker, but
# adding a new fatal preflight to the long-standing app build is a bigger change
# than this is worth - a desktop that runs the GUI shell has tk anyway.
if [ "$build_monitor" -eq 1 ]; then
    step 'Verifying tkinter is importable (the --pick-region overlay)'
    if ! uv run --group build python -c "import tkinter; print('tkinter ' + str(tkinter.TkVersion))"; then
        case "$(uname -s)" in
            Darwin) hint="Install it with 'brew install python-tk' (or use a python.org interpreter, which bundles it)." ;;
            *)      hint="Install your distribution's tk package - 'sudo apt install python3-tk' on Debian/Ubuntu, 'sudo dnf install python3-tkinter' on Fedora - and re-run." ;;
        esac
        die "tkinter is not importable, so agentclip-monitor would be frozen without the --pick-region overlay's toolkit and the picker would fail on the machine that draws it. $hint"
    fi
fi

# --- build: the full app -----------------------------------------------------

installed=()

if [ "$build_app" -eq 1 ]; then
    step 'Building agentclip (this takes a minute or two)'
    uv run --group build pyinstaller --noconfirm "$APP_SPEC" || die "PyInstaller failed on $APP_SPEC."
    app_bin="$(dist_path agentclip)"
    [ -f "$app_bin" ] || die "PyInstaller reported success but $app_bin is missing."

    # --version answers from the import tree cli.py pulls at module level, so a
    # hidden import missed anywhere below that line fails here rather than on
    # somebody's desk.
    step 'Smoke-testing agentclip'
    version_out="$("$app_bin" --version 2>&1)" ||
        { printf '%s\n' "$version_out"; die "agentclip --version failed. Not installing a broken binary."; }
    [ -n "${version_out// /}" ] || die "agentclip --version printed nothing."
    ok "$version_out"

    # --version proves the app imports; it says nothing about a backend only
    # ever imported inside a function on a poll tick. --list-matchers actually
    # imports each one and reports what happened, run against the binary just
    # built - so this catches both halves: cv2 not collected at all, and cv2
    # collected but unable to load its shared objects out of a onefile
    # extraction directory.
    step 'Verifying the OpenCV backend is bundled AND loads'
    matchers_out="$("$app_bin" --list-matchers 2>&1)" ||
        { printf '%s\n' "$matchers_out"; die "agentclip --list-matchers failed."; }
    case "$matchers_out" in
        *"NOT AVAILABLE"*)
            printf '%s\n' "$matchers_out"
            die "The frozen agentclip cannot run the OpenCV matcher, so every service would silently fall back to the anchor search. Check that the cv extra is installed and packaging/agentclip.spec's hiddenimports still name cv2/numpy."
            ;;
    esac
    printf '%s\n' "$matchers_out" | while IFS= read -r line; do ok "$line"; done

    # The same argument one shell over. --gui-smoke imports pywebview, reads all
    # three page assets back through importlib.resources (the classic frozen-app
    # failure: the files are IN the archive but the resource reader cannot find
    # them under _MEIPASS) and prints the renderer it would get.
    #
    # It needs no display. cli.py's _gui_smoke imports `webview` - whose
    # __init__ does NOT call guilib.initialize(), so no toolkit is loaded - and
    # then short-circuits the renderer question to "n/a" off
    # platform.system() != "Windows". So on Linux a failure here is a real
    # freeze problem and is fatal, with ONE exception: if the message is
    # display- or toolkit-shaped, the environment has surprised us rather than
    # the build being wrong, and blocking a packaging check on a headless build
    # box is the mistake --gui-smoke was written to avoid in the first place. It
    # is reported very loudly instead of quietly passing.
    step 'Verifying the GUI shell is bundled AND resolves its page'
    if gui_out="$("$app_bin" --gui-smoke 2>&1)"; then
        ok "$gui_out"
    else
        printf '%s\n' "$gui_out"
        case "$gui_out" in
            *DISPLAY*|*display*|*GTK*|*Gtk*|*[Qq][Tt]5*|*[Qq][Tt]6*|*xcb*|*PyGObject*|*WebViewException*)
                warn "--gui-smoke failed in a way that looks like a missing display or GUI toolkit, NOT like a broken freeze. Continuing, but the GUI shell in this build is UNVERIFIED - re-run this check on a machine with a desktop session before trusting agentclip."
                ;;
            *)
                die "The frozen agentclip cannot open the GUI shell, so the default launch would tell the user to install an extra they cannot install into a binary. Check that the gui extra is installed and packaging/agentclip.spec still names the platform's webview backend and the gui assets."
                ;;
        esac
    fi

    ok "agentclip: $(size_of "$app_bin")"
    installed+=("$app_bin")
fi

# --- build: the engine half --------------------------------------------------

if [ "$build_engine" -eq 1 ]; then
    step 'Building agentclip-engine'
    uv run --group build pyinstaller --noconfirm "$ENGINE_SPEC" || die "PyInstaller failed on $ENGINE_SPEC."
    engine_bin="$(dist_path agentclip-engine)"
    [ -f "$engine_bin" ] || die "PyInstaller reported success but $engine_bin is missing."

    # --version is the engine's whole smoke test and it is a real one: argparse
    # runs the version action before the --project required-check, so this is the
    # one invocation that walks the entire module-level import tree - config, the
    # session factory, the server loop, the executor's tool registry - without
    # needing a project, a link peer, or a frame on stdout.
    step 'Smoke-testing agentclip-engine'
    engine_version="$("$engine_bin" --version 2>&1)" ||
        { printf '%s\n' "$engine_version"; die "agentclip-engine --version failed. Not installing a broken binary."; }
    case "$engine_version" in
        "agentclip-engine "*) ok "$engine_version" ;;
        *)
            printf '%s\n' "$engine_version"
            die "agentclip-engine --version answered something unexpected. On a target that stream is the protocol, so anything else on it is a problem."
            ;;
    esac
    ok "agentclip-engine: $(size_of "$engine_bin")"
    installed+=("$engine_bin")
fi

# --- build: the monitor half -------------------------------------------------

if [ "$build_monitor" -eq 1 ]; then
    step 'Building agentclip-monitor'
    uv run --group build pyinstaller --noconfirm "$MONITOR_SPEC" || die "PyInstaller failed on $MONITOR_SPEC."
    monitor_bin="$(dist_path agentclip-monitor)"
    [ -f "$monitor_bin" ] || die "PyInstaller reported success but $monitor_bin is missing."

    # The engine's argument, with the same argparse detail behind it: the version
    # action answers from inside parsing, before either door is chosen, so this
    # is an invocation that walks the whole module-level import tree - config,
    # the clipboard provider, LocalUIMonitor, the wire, the server loop, and
    # since 9.1 the Monitor UI dispatcher above them - without opening a window
    # or listening on anything. It needs no --port any more: the port is a Serve
    # panel field, and is required only under --headless.
    #
    # There is deliberately no --gui-smoke here, unlike agentclip. That check
    # lives in cli.py (`_gui_smoke`), which is off this binary's layering
    # allowance and is not in it to be called - so what proves a frozen Monitor
    # UI's pywebview collection is the app binary's own --gui-smoke, which
    # imports the same `webview`, the same backend and the same runtime out of
    # the same environment. Giving the monitor one of its own means moving that
    # function somewhere both binaries may import from; worth doing, and not in
    # this phase.
    step 'Smoke-testing agentclip-monitor'
    monitor_version="$("$monitor_bin" --version 2>&1)" ||
        { printf '%s\n' "$monitor_version"; die "agentclip-monitor --version failed. Not installing a broken binary."; }
    case "$monitor_version" in
        "agentclip-monitor "*) ok "$monitor_version" ;;
        *)
            printf '%s\n' "$monitor_version"
            die "agentclip-monitor --version answered something unexpected."
            ;;
    esac

    # And the app's bundled-backend check, for a sharper version of the same
    # reason: this binary is where every template search actually runs
    # (ui-monitor.md 2.5), and cv2 reaches it through a lazy, try/except-guarded
    # import - so a freeze that lost OpenCV, or kept it and cannot load its
    # shared objects out of a onefile extraction directory, raises nothing at
    # all. It just hands every service the anchor search on the one machine
    # doing the matching, where no service editor is open to complain about it.
    step 'Verifying the OpenCV backend is bundled AND loads in the monitor'
    monitor_matchers="$("$monitor_bin" --list-matchers 2>&1)" ||
        { printf '%s\n' "$monitor_matchers"; die "agentclip-monitor --list-matchers failed."; }
    case "$monitor_matchers" in
        *"NOT AVAILABLE"*)
            printf '%s\n' "$monitor_matchers"
            die "The frozen agentclip-monitor cannot run the OpenCV matcher, so every service would silently fall back to the anchor search on the machine that does the matching. Check that the cv extra is installed and packaging/agentclip-monitor.spec's hiddenimports still name cv2/numpy."
            ;;
    esac
    printf '%s\n' "$monitor_matchers" | while IFS= read -r line; do ok "$line"; done

    ok "agentclip-monitor: $(size_of "$monitor_bin")"
    installed+=("$monitor_bin")
fi

# --- install -----------------------------------------------------------------

if [ "$no_install" -eq 1 ]; then
    step "Built into $ROOT/dist. Skipping install (--no-install)."
    exit 0
fi

[ -d "$install_dir" ] || { step "Creating $install_dir"; mkdir -p "$install_dir"; }

for bin in "${installed[@]}"; do
    name="$(basename "$bin")"
    step "Installing to $install_dir/$name"
    # cp onto a RUNNING binary fails with ETXTBSY; say which one and why rather
    # than leaving the caller with an errno. The monitor is the likeliest one to
    # be running: it is a STANDING process (ui-monitor.md 2.8) that outlives
    # every brain that dials it, so it is up unless somebody ended it.
    cp -f "$bin" "$install_dir/$name" ||
        die "Could not overwrite $install_dir/$name - it is most likely running. Stop it and re-run."
    chmod +x "$install_dir/$name"
done

# --- report ------------------------------------------------------------------

echo
for bin in "${installed[@]}"; do
    printf '%sInstalled %s (%s) to %s%s\n' \
        "$C_OK" "$(basename "$bin")" "$(size_of "$bin")" "$install_dir" "$C_OFF"
done

install_real="$(cd "$install_dir" && pwd -P)"
on_path=0
IFS=':' read -r -a path_parts <<< "${PATH:-}"
for entry in "${path_parts[@]}"; do
    [ -n "$entry" ] && [ -d "$entry" ] || continue
    [ "$(cd "$entry" && pwd -P)" = "$install_real" ] || continue
    on_path=1
    break
done
[ "$on_path" -eq 1 ] ||
    warn "$install_dir is not on this shell's PATH. Add it to your shell profile, or open a new shell if you just did."

# Another agentclip earlier on PATH (e.g. a stale `uv tool install`) would
# silently win every invocation, so say so loudly rather than just listing. This
# matters more for the two halves than for the app: the master launches
# `agentclip-engine` BY NAME over an exec channel (design section 2.6) and a
# launcher on the monitor machine starts `agentclip-monitor` the same way, so
# whatever PATH resolves over there is what a remote session actually runs.
for bin in "${installed[@]}"; do
    name="$(basename "$bin")"
    target="$install_dir/$name"
    resolved="$(command -v "$name" 2>/dev/null || true)"
    if [ -z "$resolved" ]; then
        printf '%sOpen a new shell, then run: %s --version%s\n' "$C_WARN" "$name" "$C_OFF"
    elif [ "$(cd "$(dirname "$resolved")" && pwd -P)/$(basename "$resolved")" = \
           "$(cd "$(dirname "$target")" && pwd -P)/$(basename "$target")" ]; then
        ok "'$name' resolves to the binary just installed."
    else
        warn "'$name' resolves to $resolved - NOT the binary just installed."
        printf '%s  Something earlier on PATH is shadowing it. If it is a uv tool install, remove it with:%s\n' "$C_WARN" "$C_OFF"
        printf '%s      uv tool uninstall agentclip%s\n' "$C_WARN" "$C_OFF"
        printf '%s  Full resolution order:%s\n' "$C_WARN" "$C_OFF"
        # `command -v` answers with the winner only; `type -a` lists the rest.
        type -a "$name" 2>/dev/null | sed 's/^/      /' || true
    fi
done
