"""Chat names: the short handle a session and its chat agree on.

Every session gets one adjective-noun name (e.g. ``amber-falcon``). AgentClip
stamps it on every outbound EOM line and the model echoes it back on every
reply; a paste whose chat name is missing or different is not from this chat
and is dropped (see docs/design/protocol.md section 1.3).

The name is a handshake token, not a secret: it only has to be memorable for
the user, unambiguous when read aloud, and unlikely to collide with a chat the
user has open in another tab. 54 x 54 = 2,916 combinations is plenty for that.

Stdlib-only leaf: imports nothing from agentclip.
"""

from __future__ import annotations

import random
import re

# Lowercase, hyphen-free, unambiguous when read aloud (no near-homophones, no
# words that need spelling out). Both lists stay >= 40 entries.
ADJECTIVES: tuple[str, ...] = (
    "amber",
    "autumn",
    "bold",
    "brave",
    "brisk",
    "calm",
    "clever",
    "cobalt",
    "copper",
    "crimson",
    "curious",
    "dapper",
    "eager",
    "electric",
    "fearless",
    "fluent",
    "gentle",
    "gilded",
    "golden",
    "hidden",
    "humble",
    "indigo",
    "ivory",
    "jade",
    "jolly",
    "keen",
    "lucid",
    "lunar",
    "mellow",
    "merry",
    "mighty",
    "nimble",
    "noble",
    "olive",
    "patient",
    "plucky",
    "quiet",
    "rapid",
    "royal",
    "ruby",
    "rustic",
    "scarlet",
    "serene",
    "silver",
    "solar",
    "stormy",
    "sunny",
    "swift",
    "teal",
    "tidy",
    "velvet",
    "violet",
    "witty",
    "zesty",
)

NOUNS: tuple[str, ...] = (
    "anchor",
    "arrow",
    "badger",
    "beacon",
    "bison",
    "bramble",
    "canyon",
    "cedar",
    "comet",
    "compass",
    "coral",
    "crane",
    "delta",
    "dolphin",
    "ember",
    "falcon",
    "fern",
    "forge",
    "fox",
    "glacier",
    "harbor",
    "heron",
    "hollow",
    "ibis",
    "island",
    "jasper",
    "kestrel",
    "lantern",
    "ledger",
    "lynx",
    "meadow",
    "mesa",
    "moth",
    "otter",
    "panther",
    "pebble",
    "pilot",
    "prairie",
    "quartz",
    "quill",
    "raven",
    "reef",
    "ridge",
    "river",
    "sparrow",
    "spruce",
    "summit",
    "thistle",
    "tundra",
    "vale",
    "walrus",
    "willow",
    "wolf",
    "zephyr",
)

# What a well-formed chat name looks like on the wire. Kept permissive enough
# to accept a name a user typed themselves, strict enough that it can never
# contain whitespace (which would break `key=value` attribute splitting).
CHAT_NAME_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")


def generate_chat_name(rng: random.Random | None = None) -> str:
    """Return a fresh ``adjective-noun`` chat name.

    ``rng`` is injectable so tests can pin the name; the default draws from the
    module-level random state.
    """
    source = rng if rng is not None else random
    return f"{source.choice(ADJECTIVES)}-{source.choice(NOUNS)}"


def normalize_chat_name(name: str | None) -> str | None:
    """Comparison form for a chat name off the wire.

    Chat UIs love to wrap tokens in quotes or backticks and models drift on
    case, so both are stripped before the engine compares. Returns None for a
    value that is empty once cleaned.
    """
    if name is None:
        return None
    cleaned = name.strip().strip("`'\"").strip().casefold()
    return cleaned or None


__all__ = ["ADJECTIVES", "CHAT_NAME_RE", "NOUNS", "generate_chat_name", "normalize_chat_name"]
