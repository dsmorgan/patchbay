"""Port-name classification, shared by the web UI and the normalizer."""

from __future__ import annotations

import re

_PORT_KINDS: list[tuple[str, re.Pattern[str]]] = [
    ("lag", re.compile(r"^(lag[ _]?\d|po\d|port-channel|trk\d|lg\d)", re.I)),
    ("vlan", re.compile(r"^(vlan|ve[ _]?\d|vl\d|.*\.\d+$)", re.I)),
    # hypervisor kernel/guest ports: they carry addresses and their MACs are
    # learned by switches, but they ride a vSwitch — no cable ends on one, so
    # they can't be a link's far end (see normalize._place_endpoints_and_infer)
    ("kernel", re.compile(r"^(vmk\d|vswif\d|Network adapter )", re.I)),
    ("virtual", re.compile(r"^(lo\d?|loopback|cpu|enc\d|pflog|pfsync|gif\d|gre\d|wg\d|ovpn|tun|tap)", re.I)),
]


def port_kind(name: str) -> str:
    for kind, pat in _PORT_KINDS:
        if pat.match(name or ""):
            return kind
    return "physical"
