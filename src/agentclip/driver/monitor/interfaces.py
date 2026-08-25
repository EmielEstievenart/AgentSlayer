"""The addresses this machine can be dialled on - for the Serve panel's list.

A monitor is started by somebody sitting at the VM, and the next thing that has
to happen is a person on the OTHER machine typing an address. "Which of these is
the host-only network?" is the question that stalls that, and it is answered by
showing the addresses rather than by asking the operator to run ``ip addr``
through a console window.

**Read-only, and nothing decides anything from it.** The bind address is
whatever ``--bind`` said; this list exists to be shown. That is why it is a
plain list of records with no notion of "recommended": which interface is the
private network is a fact about the operator's LAN that this process cannot
know.

Three filters, and each one exists because of what a list is FOR:

* **Link-local IPv6 is dropped.** ``fe80::...`` is per-link, needs a zone index
  to be usable at all, and is never the address somebody types into a dialog.
* **Loopback comes first**, because ``127.0.0.1`` is the default bind and the
  correct answer for split mode on one PC - the case a reader should not have
  to hunt for.
* **The order is total and deterministic.** A list that reshuffled between
  paints would make the operator re-read it every time, so the sort has no ties:
  loopback, then family, then interface name, then address.

``psutil`` is imported inside the function, which is this package's layering
rule (tests/test_layering.py) and also its honesty rule: a monitor whose build
lost the dependency should still poll, serve and click. It answers with an empty
list and a log line instead.
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from dataclasses import dataclass
from typing import Literal

_log = logging.getLogger(__name__)

Family = Literal["ipv4", "ipv6"]


@dataclass(frozen=True, slots=True)
class Interface:
    """One address one network interface answers on.

    An interface with both an IPv4 and an IPv6 address is TWO of these: the
    thing a person types is an address, not an adapter, so the address is what
    a row is.
    """

    name: str
    address: str
    family: Family
    loopback: bool


def list_interfaces() -> list[Interface]:
    """Every usable address on this machine, loopback first.

    Deterministic: loopback before the rest, IPv4 before IPv6, then by interface
    name and address. Link-local IPv6 and every non-IP family (a MAC address
    under ``AF_LINK``/``AF_PACKET``) are left out.
    """
    try:
        import psutil
    except ImportError:  # pragma: no cover - psutil is a core dependency
        _log.warning("psutil is not installed; cannot list this machine's addresses")
        return []
    found: list[Interface] = []
    for name, addresses in psutil.net_if_addrs().items():
        for entry in addresses:
            interface = _interface(name, entry.family, entry.address)
            if interface is not None:
                found.append(interface)
    return sorted(found, key=_order)


def _interface(name: str, family: object, address: object) -> Interface | None:
    if family == socket.AF_INET:
        kind: Family = "ipv4"
    elif family == socket.AF_INET6:
        kind = "ipv6"
    else:  # AF_LINK / AF_PACKET: a MAC address, which nobody dials
        return None
    if not isinstance(address, str) or not address:
        return None
    # psutil hands IPv6 back with the zone index attached ("fe80::1%eth0"). The
    # scope is part of the link-local problem rather than part of the address,
    # so it is stripped before parsing and the address is dropped below anyway.
    plain = address.split("%", 1)[0]
    try:
        parsed = ipaddress.ip_address(plain)
    except ValueError:
        return None
    if parsed.is_link_local and kind == "ipv6":
        return None
    return Interface(name=name, address=plain, family=kind, loopback=parsed.is_loopback)


def _order(interface: Interface) -> tuple[int, int, str, str]:
    return (
        0 if interface.loopback else 1,
        0 if interface.family == "ipv4" else 1,
        interface.name,
        interface.address,
    )
