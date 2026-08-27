"""Environment-driven configuration.

All credentials and site specifics come from environment variables, typically
loaded from a site .env file (see the private site-config repo). The env file
path comes from PATCHBAY_ENV, defaulting to ./.env.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass

from dotenv import load_dotenv

# Declarations editable from the /ops page. Stored in the DB (app_state,
# key 'cfg:<VAR>') in the exact .env syntax; per key, an env value wins and
# renders the UI field read-only. Everything else (credentials, serving,
# source URLs) is env-file-only, forever.
DECLARATION_VARS = (
    "PATCHBAY_ALIASES", "PATCHBAY_UNMANAGED", "PATCHBAY_LINKS",
    "PATCHBAY_RELATED", "PATCHBAY_VLAN_FILTER", "PATCHBAY_CAPACITY",
    "PATCHBAY_PANELS", "PATCHBAY_WAN_NAME", "PATCHBAY_WAN_PORT",
)


class DeclarationReadError(Exception):
    """The DB exists but its declarations couldn't be read (locked, corrupt).
    Distinct from 'no declarations' so callers that prune on absence can
    refuse to act on a false empty — an undeclared link gets deleted."""


def _db_declarations(db_path: str) -> dict[str, str]:
    """UI-stored declaration strings. {} genuinely means none stored; a read
    failure (locked/corrupt DB) raises rather than masquerading as empty."""
    if not os.path.exists(db_path):
        return {}
    import sqlite3

    try:
        conn = sqlite3.connect(db_path, timeout=10)
        try:
            names = {r[1] for r in conn.execute("PRAGMA table_info(app_state)")}
            if "key" not in names:
                return {}  # table not created yet — legitimately empty
            rows = conn.execute(
                "SELECT key, value FROM app_state WHERE key LIKE 'cfg:%'").fetchall()
        finally:
            conn.close()
        return {k[4:]: v for k, v in rows}
    except sqlite3.Error as e:
        raise DeclarationReadError(str(e)) from e


@dataclass(frozen=True)
class Settings:
    db_path: str
    # TLS verification for API calls: True, False, or a CA bundle path.
    # PATCHBAY_TLS_VERIFY=1 (default) | 0 | /path/to/ca.pem
    tls_verify: bool | str
    # Site-provided identity aliases, e.g. a chassis serial no source maps:
    # PATCHBAY_ALIASES="ABC123456=core1,oldname=newname"
    aliases: dict[str, str]
    # Declared unmanaged switches (ports the operator knows feed one), shown
    # even when too few MACs are live to infer them:
    # PATCHBAY_UNMANAGED="core1:1/0/12,edge1:ethernet1/1/5"
    unmanaged: list[tuple[str, str]]
    # Operator-declared cabling facts that no protocol reveals:
    # PATCHBAY_LINKS="edge1:ethernet1/2/2=vmhost2:vmnic3"
    links: list[tuple[str, str, str, str]]
    # Internet cloud nodes and where they physically land. Both accept a
    # comma-separated list, paired by position:
    #   one name, several ports  -> redundant links to one provider (one cloud
    #                               node, one edge per port)
    #   several names and ports  -> paired in order; leftover ports join the
    #                               last provider named
    #   more names than ports    -> the extra names have nowhere to attach and
    #                               are dropped with a parse warning
    # A single provider with a single port is the common case and needs no
    # list syntax: PATCHBAY_WAN_NAME="Fiber", PATCHBAY_WAN_PORT="sw1:1/0/16".
    wan_names: tuple[str, ...]
    wan_ports: tuple[tuple[str, str], ...]
    # Which cloud node each port lands on: index into wan_names, per wan_ports
    # entry. Empty when no ports are declared (the cloud hangs off the firewall).
    wan_pairing: tuple[int, ...]
    # Out-of-band / component pairs: "this box is part of that box"
    # (BMC/CIMC/iDRAC and the server it manages):
    # PATCHBAY_RELATED="cimc1=vmhost1"
    related: list[tuple[str, str]]
    # Trunk ports with a restricted VLAN list. Elsewhere every VLAN is assumed
    # to pass (trunks flood by default; we can't read allowed-lists via SNMP):
    # PATCHBAY_VLAN_FILTER="core1:1/0/11=1+24+73,edge1:ethernet1/2/2=1+73"
    vlan_filters: dict[tuple[str, str], set[int]]
    # Real capacity where the service is below the port speed (a 10G port
    # carrying a 3G circuit): PATCHBAY_CAPACITY="core1:1/0/16=3G". Load
    # math divides by the capacity, and the topology label shows both:
    # "10G (3G)". Values take a G or M suffix ("3G", "500M", "2.5G").
    capacities: dict[tuple[str, str], int]
    # Patch panels as name:size=regex; the regex's first capture group is the
    # panel position a port description claims. Distinct prefixes keep panels
    # apart ("attic:16=\[(\d+)\],rack:12=r(\d+)") — collision avoidance is the
    # operator's job. Names may contain spaces; size 0 = size by the highest
    # position seen. Unset = one implicit panel matching [n], unsized.
    panels: list[tuple[str, int, str]]
    # LibreNMS
    librenms_url: str | None
    librenms_token: str | None
    # Oxidized REST API (config history), e.g. http://oxidized:8888
    oxidized_url: str | None
    # phpIPAM
    ipam_url: str | None
    ipam_app_id: str | None
    ipam_token: str | None
    # UniFi (self-hosted Network app: local admin + cookie auth)
    unifi_url: str | None
    unifi_user: str | None
    unifi_pass: str | None
    # OPNsense
    opnsense_host: str | None
    opnsense_api_key: str | None
    opnsense_api_secret: str | None
    # pfSense (pfSense REST API package — pfrest)
    pfsense_host: str | None
    pfsense_api_key: str | None
    pfsense_api_secret: str | None
    # vSphere
    vsphere_host: str | None
    vsphere_user: str | None
    vsphere_pass: str | None
    # Per-source verify override (VSPHERE_TLS_VERIFY): vCenter often serves a
    # VMCA self-signed cert, so it can relax alone without weakening the rest.
    vsphere_tls_verify: bool | str
    # --- serving the UI itself ---
    # PATCHBAY_TLS=off (default) | direct. "direct" serves HTTPS from
    # PATCHBAY_TLS_CERT / PATCHBAY_TLS_KEY (PEM paths) and picks up renewed
    # files without a manual restart — any ACME client that drops files works
    # (Certwarden pull scripts, certbot deploy hooks, ...). "off" is for plain
    # HTTP or a TLS-terminating reverse proxy in front.
    tls_mode: str
    tls_cert: str | None
    tls_key: str | None
    # PATCHBAY_AUTH=none (default) | password | oidc.
    # password: one shared secret — PATCHBAY_PASSWORD_HASH (from
    #   `patchbay hash-password`) or, discouraged, PATCHBAY_PASSWORD plain.
    # oidc: authorization-code flow against any OAuth2/OIDC provider.
    auth_mode: str
    password_hash: str | None
    password_plain: str | None
    # Session cookie signing secret; auto-generated and persisted in the DB
    # when unset. Set it explicitly to share sessions across replicas.
    session_secret: str | None
    session_hours: float
    # Generic OIDC provider description (the glidepath pattern: endpoints +
    # claim path, no vendor-specific code).
    oidc_client_id: str | None
    oidc_client_secret: str | None
    oidc_auth_url: str | None
    oidc_token_url: str | None
    oidc_userinfo_url: str | None      # preferred identity source when set
    oidc_scopes: str                   # default "openid email profile"
    oidc_identity_path: str            # dot-path into userinfo/id_token claims
    # Exact redirect URL registered at the provider; derived from the request
    # (honoring X-Forwarded-Proto) when unset.
    oidc_redirect_url: str | None
    # Comma-separated identities allowed in; empty = any authenticated one.
    oidc_allowed: frozenset[str]
    # Entries the parsers skipped as malformed. Silent drops are dangerous for
    # declarations (an undeclared link gets *deleted*), so poll prints these
    # and /ops shows them.
    parse_warnings: tuple[str, ...]
    # Where each declaration came from: 'env' (read-only in the UI), 'db'
    # (editable on /ops), or absent when unset everywhere.
    declaration_sources: dict[str, str]
    # False when DB-stored declarations couldn't be read (locked/corrupt): the
    # env-only view is still usable for rendering, but the poll must NOT prune
    # undeclared links on it — a false empty would delete real cabling.
    declarations_readable: bool
    # Break-glass snapshots: output directory (default: snapshots/ beside the
    # DB) and how many timestamped files to keep (0 = keep everything).
    snapshot_dir: str
    snapshot_keep: int
    # Optional second destination the finished snapshot is copied to — a
    # mounted share on the box that syncs off-site. Kept separate from
    # snapshot_dir so a delivery failure never costs you the snapshot.
    snapshot_deliver_dir: str | None
    # "HH:MM" local time for the poller to write one snapshot a day; unset
    # means on-demand only.
    snapshot_at: str | None

    @property
    def wan_name(self) -> str:
        """The first provider — the whole answer for a single-WAN site."""
        return self.wan_names[0] if self.wan_names else "internet"

    @property
    def wan_port(self) -> tuple[str, str] | None:
        return self.wan_ports[0] if self.wan_ports else None


def load_settings() -> Settings:
    load_dotenv(os.environ.get("PATCHBAY_ENV", ".env"))
    _env = os.environ.get
    declarations_readable = True
    try:
        stored = _db_declarations(_env("PATCHBAY_DB", "patchbay.db"))
    except DeclarationReadError:
        stored = {}
        declarations_readable = False
    declaration_sources: dict[str, str] = {}

    def env(var: str, default: str | None = None) -> str | None:
        v = _env(var)
        if var in DECLARATION_VARS:
            if v is not None:
                declaration_sources[var] = "env"
            elif var in stored:
                declaration_sources[var] = "db"
                v = stored[var]
        return v if v is not None else default

    def parse_verify(raw: str) -> bool | str:
        if raw in ("1", "true", "yes"):
            return True
        if raw in ("0", "false", "no"):
            return False
        return raw  # CA bundle path (e.g. the homelab CA)

    tls_verify = parse_verify(env("PATCHBAY_TLS_VERIFY", "1"))
    # malformed entries are skipped, never fatal — but recorded, because a
    # silently dropped declaration can *delete* data (an undeclared link is
    # pruned on the next poll)
    warnings: list[str] = []

    def warn(var: str, spec: str) -> None:
        warnings.append(f"{var}: skipped malformed entry {spec.strip()!r}")

    if not declarations_readable:
        warnings.append("could not read stored declarations from the database "
                        "(locked or corrupt) — using env-file declarations only; "
                        "the poll will not prune links this cycle")

    aliases = {}
    for pair in (env("PATCHBAY_ALIASES") or "").split(","):
        if "=" in pair:
            alias, canonical = pair.split("=", 1)
            aliases[alias.strip()] = canonical.strip()
        elif pair.strip():
            warn("PATCHBAY_ALIASES", pair)
    unmanaged = []
    for spec in (env("PATCHBAY_UNMANAGED") or "").split(","):
        if ":" in spec:
            dev, iface = spec.split(":", 1)
            unmanaged.append((dev.strip(), iface.strip()))
        elif spec.strip():
            warn("PATCHBAY_UNMANAGED", spec)
    links = []
    for spec in (env("PATCHBAY_LINKS") or "").split(","):
        a, _, b = spec.partition("=")
        if b and ":" in a:
            ad, ai = a.strip().split(":", 1)
            # far side may be just a name — a host whose own port is
            # unknowable or uninteresting ("edge1:e1/1/24=basement-tv")
            bd, bi = (b.strip().split(":", 1) if ":" in b else (b.strip(), "?"))
            links.append((ad, ai, bd, bi))
        elif spec.strip():
            warn("PATCHBAY_LINKS", spec)
    related = []
    for spec in (env("PATCHBAY_RELATED") or "").split(","):
        if "=" in spec:
            comp, owner = spec.split("=", 1)
            related.append((comp.strip(), owner.strip()))
        elif spec.strip():
            warn("PATCHBAY_RELATED", spec)
    vlan_filters = {}
    for spec in (env("PATCHBAY_VLAN_FILTER") or "").split(","):
        port, _, vids = spec.partition("=")
        if vids and ":" in port:  # the colon must be in the port half
            dev, iface = port.strip().split(":", 1)
            vlan_filters[(dev, iface)] = {int(v) for v in vids.split("+") if v.strip().isdigit()}
        elif spec.strip():
            warn("PATCHBAY_VLAN_FILTER", spec)
    capacities = {}
    for spec in (env("PATCHBAY_CAPACITY") or "").split(","):
        port, _, cap = spec.partition("=")
        m = re.fullmatch(r"(\d+(?:\.\d+)?)([GgMm])", cap.strip())
        if m and ":" in port:
            dev, iface = port.strip().split(":", 1)
            capacities[(dev, iface)] = int(
                float(m.group(1)) * (1e9 if m.group(2) in "Gg" else 1e6))
        elif spec.strip():
            warn("PATCHBAY_CAPACITY", spec)
    wan_names = tuple(
        n.strip() for n in (env("PATCHBAY_WAN_NAME", "internet") or "").split(",")
        if n.strip()) or ("internet",)
    wan_ports: list[tuple[str, str]] = []
    for spec in (env("PATCHBAY_WAN_PORT") or "").split(","):
        if ":" in spec:
            dev, iface = spec.split(":", 1)
            wan_ports.append((dev.strip(), iface.strip()))
        elif spec.strip():
            warn("PATCHBAY_WAN_PORT", spec)
    # a provider needs somewhere to land; with no ports declared the cloud
    # hangs off the firewall, and only one of them can
    usable = wan_names[:max(len(wan_ports), 1)]
    if len(wan_names) > len(usable):
        warnings.append(
            f"PATCHBAY_WAN_NAME: {', '.join(wan_names[len(usable):])} has no "
            f"matching PATCHBAY_WAN_PORT entry and is not shown — name one "
            f"port per provider")
    # extra ports past the last named provider are redundant links to it
    wan_pairing = tuple(min(i, len(usable) - 1) for i in range(len(wan_ports)))

    panels = []
    # split only at commas that start a new name:size= entry — the regex
    # itself may contain commas (e.g. \d{1,2}); names may contain spaces
    for spec in re.split(r",(?=[^=,]+:\d+=)", env("PATCHBAY_PANELS") or ""):
        head, _, pattern = spec.partition("=")
        if pattern and ":" in head:
            pname, size = head.rsplit(":", 1)
            try:
                re.compile(pattern)
                panels.append((pname.strip(), int(size), pattern))
                continue
            except (re.error, ValueError):
                pass
        if spec.strip():
            warn("PATCHBAY_PANELS", spec)
    return Settings(
        db_path=env("PATCHBAY_DB", "patchbay.db"),
        tls_verify=tls_verify,
        aliases=aliases,
        unmanaged=unmanaged,
        links=links,
        wan_names=usable,
        wan_ports=tuple(wan_ports),
        wan_pairing=wan_pairing,
        related=related,
        vlan_filters=vlan_filters,
        capacities=capacities,
        panels=panels,
        librenms_url=env("LIBRENMS_URL"),
        librenms_token=env("LIBRENMS_TOKEN"),
        oxidized_url=env("OXIDIZED_URL"),
        ipam_url=env("IPAM_URL"),
        ipam_app_id=env("IPAM_APP_ID"),
        ipam_token=env("IPAM_TOKEN"),
        unifi_url=env("UNIFI_URL"),
        unifi_user=env("UNIFI_USER"),
        unifi_pass=env("UNIFI_PASS"),
        opnsense_host=env("OPNSENSE_HOST"),
        opnsense_api_key=env("OPNSENSE_API_KEY"),
        opnsense_api_secret=env("OPNSENSE_API_SECRET"),
        pfsense_host=env("PFSENSE_HOST"),
        pfsense_api_key=env("PFSENSE_API_KEY"),
        pfsense_api_secret=env("PFSENSE_API_SECRET"),
        vsphere_host=env("VSPHERE_HOST"),
        vsphere_user=env("VSPHERE_USER"),
        vsphere_pass=env("VSPHERE_PASS"),
        vsphere_tls_verify=(parse_verify(env("VSPHERE_TLS_VERIFY", ""))
                            if env("VSPHERE_TLS_VERIFY") else tls_verify),
        tls_mode=(env("PATCHBAY_TLS", "off") or "off").strip().lower(),
        tls_cert=env("PATCHBAY_TLS_CERT"),
        tls_key=env("PATCHBAY_TLS_KEY"),
        auth_mode=(env("PATCHBAY_AUTH", "none") or "none").strip().lower(),
        password_hash=env("PATCHBAY_PASSWORD_HASH"),
        password_plain=env("PATCHBAY_PASSWORD"),
        session_secret=env("PATCHBAY_SESSION_SECRET"),
        session_hours=float(env("PATCHBAY_SESSION_HOURS", "12") or 12),
        oidc_client_id=env("PATCHBAY_OIDC_CLIENT_ID"),
        oidc_client_secret=env("PATCHBAY_OIDC_CLIENT_SECRET"),
        oidc_auth_url=env("PATCHBAY_OIDC_AUTH_URL"),
        oidc_token_url=env("PATCHBAY_OIDC_TOKEN_URL"),
        oidc_userinfo_url=env("PATCHBAY_OIDC_USERINFO_URL"),
        oidc_scopes=env("PATCHBAY_OIDC_SCOPES", "openid email profile") or "openid email profile",
        oidc_identity_path=env("PATCHBAY_OIDC_IDENTITY_PATH", "email") or "email",
        oidc_redirect_url=env("PATCHBAY_OIDC_REDIRECT_URL"),
        oidc_allowed=frozenset(
            s.strip().lower() for s in (env("PATCHBAY_OIDC_ALLOWED") or "").split(",")
            if s.strip()),
        parse_warnings=tuple(warnings),
        declaration_sources=declaration_sources,
        declarations_readable=declarations_readable,
        snapshot_dir=(env("PATCHBAY_SNAPSHOT_DIR")
                      or os.path.join(os.path.dirname(env("PATCHBAY_DB", "patchbay.db"))
                                      or ".", "snapshots")),
        snapshot_keep=int(env("PATCHBAY_SNAPSHOT_KEEP", "30") or 30),
        snapshot_deliver_dir=env("PATCHBAY_SNAPSHOT_DELIVER_DIR") or None,
        snapshot_at=env("PATCHBAY_SNAPSHOT_AT") or None,
    )
