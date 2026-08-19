#!/usr/bin/env bash
#
# Freeze AgentClip's TWO executables and drop them on your PATH.
#
#   agentclip         the full app - TUI, GUI shell, OpenCV matcher backend
#   agentclip-engine  the engine half, the binary an SSH target runs
#                     (docs/design/remote-executor.md section 2.6)
#
# The POSIX counterpart of scripts/build-exe.ps1, which builds only the first of
# those because Windows is where the app is DRIVEN. Linux is where it is
# HOSTED - so this one always builds the engine, and `--engine-only` skips the
# full app entirely for the common case: a target machine that will never open
# a window and may not have the system libraries to.
#
# Usage:
#   scripts/build-exe.sh [--clean] [--engine-only] [--no-install]
#                        [--install-dir DIR] [--help]

set -euo pipefail

# Derive the repo root from THIS FILE, never from the caller's directory: the
# specs anchor their own paths to SPECPATH, but `uv sync` and `uv run` resolve
# the project from the CWD, so a run from anywhere else would sync some other
# environment (or none).
ROOT="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
APP_SPEC="$ROOT/packaging/agentclip.spec"
ENGINE_SPEC="$ROOT/packaging/agentclip-engine.spec"

clean=0
engine_only=0
no_install=0
install_dir="${AGENTCLIP_INSTALL_DIR:-$HOME/.local/bin}"

usage() {
    sed -n '3,18p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
    cat <<'EOF'

Options:
  --clean             delete build/ and dist/ before building
  --engine-only       build only agentclip-engine; skip the full app (and its
                      cv/gui extras, which a headless target need not install)
  --no-install        build and smoke-test only; leave the binaries in dist/
  --install-dir DIR   where to copy them
                      (default: $AGENTCLIP_INSTALL_DIR, else ~/.local/bin)
  -h, --help          this text
EOF
}

while [ $# -gt 0 ]; do
    case "$1" in
        --clean) clean=1 ;;
        --engine-only) engine_only=1 ;;
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
[ -f "$ENGINE_SPEC" ] ||
    die "Spec file not found at $ENGINE_SPEC - is the repo checkout complete?"
if [ "$engine_only" -eq 0 ] && [ ! -f "$APP_SPEC" ]; then
    die "Spec file not found at $APP_SPEC - is the repo checkout complete?"
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
# the first one put there. Which extras depends on the mode, and that is the
# whole point of --engine-only: opencv and pywebview are heavy wheels whose
# Linux builds want system libraries a bare target may not have, and the engine
# binary imports neither (packaging/agentclip-engine.spec excludes them).
#
# `mcp` is in BOTH sets and is not optional here even though it is an extra
# everywhere else: the engine binary exists to run MCP servers on the target
# (design section 2.7), and PyInstaller can only collect a package that is
# present in the environment it is pointed at.
#
# Not --no-default-groups: that would uninstall pytest/ruff/mypy and break the
# dev loop. Dev deps are kept out of the binaries by the specs' excludes.
if [ "$engine_only" -eq 1 ]; then
    extras=(--extra mcp)
else
    extras=(--extra cv --extra gui --extra mcp)
fi
step "Syncing dependencies (uv sync --group build ${extras[*]})"
uv sync --group build "${extras[@]}" || die "uv sync failed."

# And prove it before spending minutes on builds that cannot be right. Every one
# of these extras is reached by a LAZY, try/except-guarded import, so its absence
# produces no build error at all - just a binary that is quietly missing a
# feature and blames the user's install for it.
if [ "$engine_only" -eq 0 ]; then
    step 'Verifying the cv extra is importable'
    uv run --group build python -c \
        "import cv2, numpy; print(f'cv2 {cv2.__version__}, numpy {numpy.__version__}')" ||
        die "The cv extra is not importable, so agentclip would be built without the OpenCV matcher backend and every service would silently fall back to the anchor search. Fix the environment and re-run."

    step 'Verifying the gui extra is importable'
    uv run --group build python -c \
        "from importlib.metadata import version; import webview; print('pywebview ' + version('pywebview'))" ||
        die "The gui extra is not importable, so agentclip would be built without the GUI shell and --gui would tell the user to install an extra they cannot install into a binary. Fix the environment and re-run."
fi

step 'Verifying the mcp extra is importable'
uv run --group build python -c \
    "from importlib.metadata import version; import mcp; print('mcp ' + version('mcp'))" ||
    die "The mcp extra is not importable, so agentclip-engine would be built without the MCP SDK and every server on the target would report missing_sdk - naming a fix that cannot be applied to a frozen binary. Fix the environment and re-run."

# --- build: the full app -----------------------------------------------------

installed=()

if [ "$engine_only" -eq 0 ]; then
    step 'Building agentclip (this takes a minute or two)'
    uv run --group build pyinstaller --noconfirm "$APP_SPEC" || die "PyInstaller failed on $APP_SPEC."
    app_bin="$(dist_path agentclip)"
    [ -f "$app_bin" ] || die "PyInstaller reported success but $app_bin is missing."

    # cli.py imports agentclip.shell.tui.app, which transitively imports every
    # screen and widget. A missing hidden import fails here rather than the
    # first time a modal is opened.
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
                warn "--gui-smoke failed in a way that looks like a missing display or GUI toolkit, NOT like a broken freeze. Continuing, but the GUI shell in this build is UNVERIFIED - re-run this check on a machine with a desktop session before trusting agentclip --gui."
                ;;
            *)
                die "The frozen agentclip cannot open the GUI shell, so --gui would tell the user to install an extra they cannot install into a binary. Check that the gui extra is installed and packaging/agentclip.spec still names the platform's webview backend and the gui assets."
                ;;
        esac
    fi

    ok "agentclip: $(size_of "$app_bin")"
    installed+=("$app_bin")
fi

# --- build: the engine half --------------------------------------------------

step 'Building agentclip-engine'
uv run --group build pyinstaller --noconfirm "$ENGINE_SPEC" || die "PyInstaller failed on $ENGINE_SPEC."
engine_bin="$(dist_path agentclip-engine)"
[ -f "$engine_bin" ] || die "PyInstaller reported success but $engine_bin is missing."

# --version is the engine's whole smoke test and it is a real one: argparse runs
# the version action before the --project required-check, so this is the one
# invocation that walks the entire module-level import tree - config, the
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
    # than leaving the caller with an errno.
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
# matters more for the engine than for the app: the master launches
# `agentclip-engine` BY NAME over an exec channel (design section 2.6), so
# whatever PATH resolves on the target is what a remote session actually runs.
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
