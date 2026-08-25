"""``agentclip-monitor`` - the Monitor, with its Monitor UI.

``docs/design/ui-monitor.md`` §9.1. This is the console script and the frozen
binary's entry point; in a checkout it is ``python -m agentclip.shell.monitor_ui``.
It is a **dispatcher and nothing else**, and it lives here rather than in
``driver/monitor/`` for one mechanical reason: the Driver may not import
pywebview (tests/test_layering.py says so about every module in it at once), and
the thing this opens is a window.

**One parser, two doors.** :func:`~agentclip.driver.monitor.__main__.build_arg_parser`
is imported from the Driver, unchanged and un-wrapped: the argument grammar is
the Driver's - ``agentclip-monitor --help`` says the same thing whichever half
answers it - and what belongs to the shell is only the choice of door.

* ``--headless`` delegates to :func:`agentclip.driver.monitor.__main__.main`
  **verbatim**, with the original ``argv``. That is the windowless server for a
  VM with no desktop: it imports no toolkit, it still needs ``--port``, and it
  is still what a frozen-binary smoke test drives.
* Anything else opens the Monitor UI over one
  :class:`~agentclip.driver.monitor.local.LocalUIMonitor` - the same one
  ``build_monitor`` assembles for the headless door, with the same config dir,
  the same profile root and the same real clipboard provider. The port is a
  field in the Serve panel rather than a required flag; naming ``--port`` on the
  command line pre-fills that panel and starts it.

``--version`` and ``--list-matchers`` answer from inside parsing, before either
door is chosen, which is what keeps both build smoke tests single-flag
invocations that reach no assembly and open no socket.
"""

from __future__ import annotations

import sys
from collections.abc import Sequence

from agentclip.driver.clip.base import select_provider
from agentclip.driver.monitor.__main__ import (
    build_arg_parser,
    config_dir,
    global_config_path,
    load_project_config,
    profile_root,
)
from agentclip.driver.monitor.__main__ import main as headless_main
from agentclip.shell.monitor_ui.window import run_monitor_ui

#: Said once, on stderr, when a run asks for a secret the panel does not read.
#: ``--token`` / ``--token-file`` answer "what does this PROCESS serve with",
#: which is a question the headless door has and the window does not: with a
#: panel the token is a row on screen with a Regenerate button beside it, and it
#: is the one in the config dir. Refusing the flag would break a launcher that
#: passes it to both halves; saying nothing would leave somebody wondering why
#: their secret is not the one on screen.
TOKEN_FLAG_IGNORED = (
    "agentclip-monitor: --token/--token-file is a --headless flag; the Monitor"
    " UI serves with the token in its config dir, which the Serve panel shows"
)


def main(argv: list[str] | None = None) -> int:
    parser = build_arg_parser()
    args = parser.parse_args(argv)
    if args.headless:
        # Verbatim, with the ORIGINAL argv rather than the namespace: the
        # windowless door owns its own validation (``--port`` is required there)
        # and its own error sentences, and re-deriving a command line from a
        # parsed namespace is how the two doors would start to disagree.
        return headless_main(argv)
    if args.token is not None or args.token_file is not None:
        print(TOKEN_FLAG_IGNORED, file=sys.stderr)
    config = load_project_config(args)
    return run_monitor_ui(
        config,
        config_dir=config_dir(args),
        profile_root=profile_root(args),
        global_config_path=global_config_path(args),
        # The real clipboard, which is the difference between this window and
        # the one the Chat UI opens beside itself: this process is the one a
        # Chat UI will ask to read and write it (ui-monitor.md §2.11), so the
        # Monitor owns it here rather than borrowing an app's.
        provider=select_provider(config.clipboard.provider),
        serve_at=None if args.port is None else (args.bind, args.port),
        no_token=args.no_token,
    )


__all__: Sequence[str] = ["TOKEN_FLAG_IGNORED", "main"]


if __name__ == "__main__":
    sys.exit(main())
