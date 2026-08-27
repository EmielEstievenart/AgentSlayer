"""The addresses a monitor can be dialled on, and the order they are listed in.

Every test here monkeypatches ``psutil.net_if_addrs``, because the property
under test is the SHAPE of the answer - which families survive, which addresses
are dropped, and what comes first - and a real machine's answer is different on
every machine. The one thing that is asserted against the real call is that it
does not explode.
"""

from __future__ import annotations

import socket

import psutil
import pytest

from agentclip.driver.monitor.interfaces import Interface, list_interfaces


class FakeAddr:
    """One row of ``psutil.net_if_addrs()``: family, address, and nothing else
    this module reads."""

    def __init__(self, family: object, address: str) -> None:
        self.family = family
        self.address = address


def answering(monkeypatch: pytest.MonkeyPatch, addresses: dict[str, list[FakeAddr]]) -> None:
    monkeypatch.setattr(psutil, "net_if_addrs", lambda: addresses)


def test_loopback_comes_first_and_ipv4_before_ipv6(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The default bind is 127.0.0.1 and the correct answer for split mode on
    one PC, so it must not be something a reader has to hunt for."""
    answering(
        monkeypatch,
        {
            "eth0": [
                FakeAddr(socket.AF_INET6, "2001:db8::5"),
                FakeAddr(socket.AF_INET, "192.168.56.10"),
            ],
            "lo": [
                FakeAddr(socket.AF_INET6, "::1"),
                FakeAddr(socket.AF_INET, "127.0.0.1"),
            ],
        },
    )
    assert list_interfaces() == [
        Interface("lo", "127.0.0.1", "ipv4", True),
        Interface("lo", "::1", "ipv6", True),
        Interface("eth0", "192.168.56.10", "ipv4", False),
        Interface("eth0", "2001:db8::5", "ipv6", False),
    ]


def test_link_local_ipv6_is_dropped(monkeypatch: pytest.MonkeyPatch) -> None:
    """``fe80::`` is per-link, needs a zone index to be usable at all, and is
    never the address somebody types into a dialog."""
    answering(
        monkeypatch,
        {
            "eth0": [
                FakeAddr(socket.AF_INET6, "fe80::1c2b:3d4e:5f60:7a8b%eth0"),
                FakeAddr(socket.AF_INET, "10.0.0.4"),
            ]
        },
    )
    assert list_interfaces() == [Interface("eth0", "10.0.0.4", "ipv4", False)]


def test_a_routable_ipv6_keeps_its_address_without_the_zone(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    answering(monkeypatch, {"eth0": [FakeAddr(socket.AF_INET6, "2001:db8::9%eth0")]})
    assert list_interfaces() == [Interface("eth0", "2001:db8::9", "ipv6", False)]


def test_mac_addresses_and_nonsense_are_not_interfaces(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """``AF_LINK``/``AF_PACKET`` rows are hardware addresses, which nobody dials."""
    link = getattr(socket, "AF_PACKET", None) or getattr(psutil, "AF_LINK", -1)
    answering(
        monkeypatch,
        {
            "eth0": [
                FakeAddr(link, "aa:bb:cc:dd:ee:ff"),
                FakeAddr(socket.AF_INET, "not an address"),
                FakeAddr(socket.AF_INET, ""),
                FakeAddr(socket.AF_INET, "10.0.0.4"),
            ]
        },
    )
    assert list_interfaces() == [Interface("eth0", "10.0.0.4", "ipv4", False)]


def test_the_order_is_total_so_the_list_does_not_reshuffle(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A list that reordered between paints would make the operator re-read it
    every time, so the sort has no ties: loopback, family, name, address."""
    answering(
        monkeypatch,
        {
            "wlan0": [FakeAddr(socket.AF_INET, "192.168.1.9")],
            "eth1": [FakeAddr(socket.AF_INET, "10.0.0.9"), FakeAddr(socket.AF_INET, "10.0.0.8")],
            "eth0": [FakeAddr(socket.AF_INET, "10.0.0.4")],
        },
    )
    names = [(row.name, row.address) for row in list_interfaces()]
    assert names == [
        ("eth0", "10.0.0.4"),
        ("eth1", "10.0.0.8"),
        ("eth1", "10.0.0.9"),
        ("wlan0", "192.168.1.9"),
    ]


def test_this_machine_answers_without_raising() -> None:
    """Read-only and harmless - no socket is opened and nothing is bound - so it
    is worth asking the real OS once that the psutil call is wired up right."""
    for row in list_interfaces():
        assert row.address
        assert row.family in ("ipv4", "ipv6")
