"""Pytest plugin that fails the run if anything tries to reach the network.

Enable with `pytest -p tests.no_network`. Loopback stays open because asyncio needs
it for its own event-loop plumbing on Windows; anything else raises.

The suite is supposed to be entirely offline -- fixtures, not the internet -- and
this is how that claim gets checked rather than assumed.
"""

from __future__ import annotations

import socket

_LOOPBACK = {"127.0.0.1", "::1", "localhost", "", None}


class NetworkAccessAttempted(RuntimeError):
    """A test tried to open a socket to something other than loopback."""


def _is_loopback(address) -> bool:
    if isinstance(address, tuple) and address:
        return address[0] in _LOOPBACK
    return False


def pytest_configure(config) -> None:  # noqa: ARG001
    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_getaddrinfo = socket.getaddrinfo

    def connect(self, address, *args, **kwargs):
        if not _is_loopback(address):
            raise NetworkAccessAttempted(f"outbound connection to {address!r}")
        return real_connect(self, address, *args, **kwargs)

    def connect_ex(self, address, *args, **kwargs):
        if not _is_loopback(address):
            raise NetworkAccessAttempted(f"outbound connection to {address!r}")
        return real_connect_ex(self, address, *args, **kwargs)

    def getaddrinfo(host, *args, **kwargs):
        if host not in _LOOPBACK:
            raise NetworkAccessAttempted(f"DNS lookup for {host!r}")
        return real_getaddrinfo(host, *args, **kwargs)

    socket.socket.connect = connect
    socket.socket.connect_ex = connect_ex
    socket.getaddrinfo = getaddrinfo
