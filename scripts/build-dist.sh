#!/usr/bin/env bash
#
# Build all three AgentClip binaries into dist/ and stop there - no install.
#
# A one-liner over scripts/build-exe.sh for the release case: a clean build of
# agentclip, agentclip-engine and agentclip-monitor, left in dist/ for you to
# ship, and nothing copied onto PATH. Every flag and check is build-exe.sh's;
# this file only fixes the switches.
#
# Usage:
#   scripts/build-dist.sh

set -euo pipefail
HERE="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
"$HERE/build-exe.sh" --clean --no-install

echo "==> dist/ holds:"
ls -lh "$HERE/../dist"/agentclip* | awk '{printf "    %-24s %s\n", $NF, $5}' | sed 's|.*/||'
