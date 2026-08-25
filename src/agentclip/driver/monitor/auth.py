"""The monitor port's shared secret: where it lives, and how it is compared.

docs/design/ui-monitor.md §5 called auth on the monitor port an open point and
said the handshake "has room for a secret and does not use it". This is the
secret. The port is a channel to a machine's mouse, keyboard and clipboard, and
the deployment §5 describes - a VM on a host-only network - is exactly the one
where "loopback only" stops being a fence: the moment ``--bind`` is typed,
anything on that network could dial in and start clicking.

**A file, not a config key.** The token is a credential, so it lives on its own
at ``<config dir>/monitor-token`` with 0600 where the OS honours modes, rather
than inside ``config.toml`` next to settings a user pastes into a bug report.
The file IS the storage: there is no in-memory registry and no expiry, because
the monitor is a standing process (§2.8) and the operator reads the token off
its terminal once and puts it in the brain's dialog.

**32 hex characters.** 16 bytes from :func:`secrets.token_hex` - the same order
of magnitude as an SSH host key's fingerprint, short enough to be typed across
a VM console by hand, and hex so that a terminal font, a copy-paste and a
handwritten note all agree on what the character was.

**Compared with :func:`secrets.compare_digest`.** The monitor answers a hello in
under a millisecond and a comparison that returned early on the first wrong
character would leak the prefix one dial at a time. It costs nothing to not.
"""

from __future__ import annotations

import contextlib
import os
import secrets
import stat
import tempfile
from pathlib import Path

#: Bytes of entropy behind the token; the text form is twice this in hex.
TOKEN_BYTES = 16

#: How long a token reads as text - what a UI validates a pasted one against.
TOKEN_CHARS = TOKEN_BYTES * 2

#: The file's name inside the monitor's config directory. Named rather than
#: spelled inline, because the Serve panel tells the operator where to look.
TOKEN_FILE = "monitor-token"


def default_monitor_dir() -> Path:
    """``<user config dir>/agentclip/monitor`` - the monitor's own corner.

    Its own subdirectory rather than the config root, because everything the
    monitor persists is about THIS machine's screen (the token, the chat regions
    beside it in :mod:`~agentclip.driver.monitor.regions`) and none of it is
    part of the app's configuration. ``platformdirs`` is imported inside the
    function for the layering rule this package lives under
    (tests/test_layering.py): a lazy import is the allowed pattern, and it keeps
    the monitor's module graph a stdlib one.
    """
    import platformdirs

    return Path(platformdirs.user_config_dir("agentclip")) / "monitor"


def token_path(config_dir: Path) -> Path:
    """Where the token for ``config_dir`` is - whether or not it exists yet."""
    return Path(config_dir) / TOKEN_FILE


def new_token() -> str:
    """One fresh token, in memory. Persisted by nothing."""
    return secrets.token_hex(TOKEN_BYTES)


def load_or_create_token(config_dir: Path) -> str:
    """The token at ``config_dir``, minting and storing one on first use.

    Stable across restarts on purpose: a monitor that regenerated its secret on
    every launch would make the brain's saved connection wrong every time the VM
    rebooted, which is the one thing an operator does without thinking about it.
    :func:`regenerate_token` is how a compromised one is replaced, and it is a
    deliberate act.

    A file that exists but holds nothing usable (an empty one, or the leftovers
    of a half-written save) is replaced rather than trusted: an empty token that
    compared equal to an empty ``"token": ""`` would be an open port that looks
    authenticated.
    """
    path = token_path(config_dir)
    try:
        existing = path.read_text(encoding="utf-8").strip()
    except (OSError, UnicodeDecodeError):
        existing = ""
    if existing:
        return existing
    return _write_token(path, new_token())


def regenerate_token(config_dir: Path) -> str:
    """Mint a new token, store it, and return it. Every brain must be re-told."""
    return _write_token(token_path(config_dir), new_token())


def tokens_match(expected: str | None, offered: str | None) -> bool:
    """Does ``offered`` authorise a connection to a server holding ``expected``?

    ``expected is None`` is the no-token mode and accepts anything, including a
    client that offered one - the server, not the client, decides whether the
    port is guarded, and a brain that carries a token for a monitor that stopped
    requiring one should still connect.

    Otherwise it is a constant-time comparison, and a missing token is a
    mismatch rather than an error: "you sent none" and "you sent the wrong one"
    are the same refusal, said the same way.
    """
    if expected is None:
        return True
    if not offered:
        return False
    return secrets.compare_digest(expected, offered)


def _write_token(path: Path, token: str) -> str:
    """Store ``token`` at ``path``, 0600 where the OS has modes, atomically.

    The temporary file is created by :func:`tempfile.mkstemp`, which is 0600 on
    every platform that has modes - so the secret is never on disk world-
    readable, not even for the instant between write and chmod. The ``chmod``
    afterwards is for the umask-independent guarantee on the final name; on
    Windows it sets only the read-only bit and is harmless.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_name = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        # A filesystem with no modes at all; the content stands either way.
        with contextlib.suppress(OSError):
            os.chmod(tmp_name, stat.S_IRUSR | stat.S_IWUSR)
        os.replace(tmp_name, path)
    except BaseException:
        with contextlib.suppress(OSError):  # already replaced, or never created
            os.remove(tmp_name)
        raise
    return token
