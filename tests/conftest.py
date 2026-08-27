"""Shared fixtures. Every test runs against throwaway in-memory/tmp SQLite —
no network, no site data. Fixture payloads are fully synthetic (RFC 5737
addresses, fake names); see CONTRIBUTING.md.
"""

from __future__ import annotations

import os
import sqlite3

import pytest

from patchbay import db as pdb


@pytest.fixture()
def conn():
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA foreign_keys = ON")
    pdb.init(c)
    yield c
    c.close()


@pytest.fixture()
def clean_env(tmp_path, monkeypatch):
    """A hermetic environment: no site .env, throwaway DB, and every
    PATCHBAY_*/source variable cleared so ambient shell state can't leak in."""
    for var in list(os.environ):
        if var.startswith(("PATCHBAY_", "LIBRENMS_", "IPAM_", "UNIFI_",
                           "OPNSENSE_", "PFSENSE_", "VSPHERE_", "OXIDIZED_")):
            monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("PATCHBAY_ENV", os.devnull)
    monkeypatch.setenv("PATCHBAY_DB", str(tmp_path / "test.db"))
    return monkeypatch
