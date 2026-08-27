"""Collector plugin interface.

A collector maps one data source (LibreNMS, phpIPAM, UniFi, ...) into the
shared model. Each module registers itself via @register; `available()`
returns only collectors whose settings are present, so a deployment simply
configures the sources it has.

Third-party collectors install as their own packages, no core edits: expose
the collector instance (or a zero-arg factory) through the
``patchbay.collectors`` entry-point group and it is discovered at startup —

    [project.entry-points."patchbay.collectors"]
    proxmox = "patchbay_proxmox:collector"

See docs/collectors.md for the authoring guide and the contract a collector
must honor (own your rows, prune what your source stops reporting, never
resolve conflicts — that's the normalizer's job).
"""

from __future__ import annotations

import sqlite3
from typing import Callable, Protocol

from ..config import Settings


class Collector(Protocol):
    name: str

    def configured(self, settings: Settings) -> bool: ...
    def collect(self, settings: Settings, conn: sqlite3.Connection) -> str:
        """Run one poll cycle; returns a short human-readable summary."""


_REGISTRY: dict[str, Collector] = {}


def register(collector: Collector) -> Collector:
    _REGISTRY[collector.name] = collector
    return collector


_discovered = False


def _discover_entry_points() -> None:
    """Load third-party collectors from the 'patchbay.collectors' entry-point
    group. A broken plugin is reported and skipped — it must not take the
    poll (or the built-in collectors) down with it."""
    import sys
    from importlib.metadata import entry_points

    for ep in entry_points(group="patchbay.collectors"):
        try:
            obj = ep.load()
            collector = obj() if callable(obj) and not hasattr(obj, "collect") else obj
            register(collector)
        except Exception as e:
            print(f"[warn] collector plugin {ep.name!r} failed to load: {e}",
                  file=sys.stderr)


def all_collectors() -> dict[str, Collector]:
    # import here so registration side effects run exactly once, lazily
    from . import librenms, opnsense, oxidized, pfsense, phpipam, unifi, vsphere  # noqa: F401

    global _discovered
    if not _discovered:
        _discovered = True
        _discover_entry_points()
    return dict(_REGISTRY)


def available(settings: Settings) -> dict[str, Collector]:
    return {n: c for n, c in all_collectors().items() if c.configured(settings)}
