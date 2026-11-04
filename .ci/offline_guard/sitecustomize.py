"""Fail offline verification if Python attempts network access."""

import sys


def offline_network_guard(event, args):
    if event in {"socket.connect", "socket.connect_ex", "socket.getaddrinfo", "socket.bind"}:
        raise RuntimeError("Network access is prohibited during offline verification")


sys.addaudithook(offline_network_guard)
