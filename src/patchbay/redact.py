"""Secret redaction shared by the firewall config history, the snapshot
scrubber, and the UI.

`redact_xml` covers element-shaped secrets (config.xml from OPNsense and
pfSense, whether pulled over the API or backed up by Oxidized): the content
of every secret-bearing element is replaced with a short hash of itself, so
a rotated key still shows as a change but no secret is ever stored. It is
idempotent: an element already holding `redacted:<hash>` is left alone, so
stored history can be re-redacted under newer rules without its existing
hashes moving.

`hide_userinfo` masks the `user:password@` part of any URL in a string, for
source URLs and the error text that echoes them.
"""

from __future__ import annotations

import hashlib
import re

# Bump when the element rules below change: db.init re-applies redact_xml to
# every stored config revision once per version (#93).
REDACTION_VERSION = 2

# matched as substrings of the tag NAME, without regard to case, not exact
# tags: plugins invent names like <rocommunity>, <sharedsecret>,
# <varusersfreeradiuspassword>, <otp_seed>, <ldap_bindpw>, and the IPsec
# pre-shared-key model stores its secret as <Key>. A false positive still
# diffs as a change (the hash moves), so err toward matching.
SECRET_TAG_WORDS = ("prv", "crt", "cert", "pass", "pw", "secret", "psk",
                    "shared", "private", "key", "seed", "otp", "community",
                    "auth", "token", "hash", "credential", "tls")

_ATTRS = r"(\s[^>]*)?"                       # optional attributes, kept
_CONTENT = r"(<!\[CDATA\[.*?\]\]>|[^<]+)"    # text or one CDATA section
_SECRET_TAG_RE = re.compile(
    r"<([a-z0-9_-]*(?:" + "|".join(SECRET_TAG_WORDS) + r")[a-z0-9_-]*)"
    + _ATTRS + ">" + _CONTENT + r"</\1\s*>", re.I | re.S)
# PEM material under any tag name: a key or certificate block pasted as-is
_PEM_ELEMENT_RE = re.compile(
    r"<([a-z0-9_-]+)" + _ATTRS + r">([^<]*-----BEGIN [^<]*)</\1\s*>", re.I)
# catch-all for key material hiding under tags the list doesn't know: any
# element whose content is one long base64-ish run gets the same treatment
# (a real ACME account key surfaced under a bare <key> before this existed;
# unknown plugins will invent more names, so shape catches what names miss)
_LONG_BLOB_RE = re.compile(
    r"<([a-z0-9_-]+)" + _ATTRS + r">([A-Za-z0-9+/=\s]{200,})</\1\s*>", re.I)
_ALREADY = re.compile(r"redacted:[0-9a-f]{8}")


def _digest(content: str) -> str:
    return "redacted:" + hashlib.sha256(content.encode()).hexdigest()[:8]


def redact_xml(text: str) -> tuple[str, int]:
    """Replace the content of secret-bearing XML elements with a hash of
    itself. Returns (redacted text, number of elements newly redacted)."""
    count = 0

    def sub(m: re.Match) -> str:
        nonlocal count
        tag, attrs, content = m.group(1), m.group(2) or "", m.group(3)
        if _ALREADY.fullmatch(content):
            return m.group(0)
        count += 1
        return f"<{tag}{attrs}>{_digest(content)}</{tag}>"

    for pat in (_SECRET_TAG_RE, _PEM_ELEMENT_RE, _LONG_BLOB_RE):
        text = pat.sub(sub, text)
    return text, count


# scheme://userinfo@ — the userinfo stops at the first '/', whitespace, or
# quote, so an '@' later in a path or message is not mistaken for one
_USERINFO_RE = re.compile(r"([a-z][a-z0-9+.-]*://)[^/\s@'\"<>]+@", re.I)


def hide_userinfo(text: str | None) -> str | None:
    """Mask the credentials of every URL in `text` (`http://u:p@h/` becomes
    `http://***@h/`). Host and path stay readable."""
    if not text:
        return text
    return _USERINFO_RE.sub(r"\1***@", text)
