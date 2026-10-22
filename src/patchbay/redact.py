"""Secret redaction shared by the firewall config history, the snapshot
scrubber, and the UI.

`redact_xml` covers element-shaped secrets (config.xml from OPNsense and
pfSense, whether pulled over the API or backed up by Oxidized): the content
of every secret-bearing element is replaced. With a key (the stored firewall
history), the replacement is a short keyed stamp of the content, so a
rotated key still shows as a change but no secret is ever stored, and a
reader of the history who lacks the per-install key can't test guesses
against a stamp. Without a key (the snapshot, which leaves the host), it is
a constant. Redaction is idempotent: an element already holding a
placeholder is left alone, so stored history can be re-redacted under newer
rules without its existing stamps moving.

A regex, not an XML parser, on purpose: parsing and re-serializing would
drop comments and CDATA and normalize quoting and whitespace, so every
stored revision would diff in full against the ones before it; and Oxidized
text is often not XML at all.

`hide_userinfo` masks the `user:password@` part of any URL in a string, for
source URLs and the error text that echoes them.
"""

from __future__ import annotations

import hashlib
import hmac
import re

# Bump when the rules below change: db.init re-applies redact_xml to every
# stored config revision once per version (#93). tests/test_redact.py pins a
# fingerprint of the rules to this number, so a rule change without a bump
# fails there. 2: case-insensitive names, pw/otp/tls, attributes, CDATA and
# comments, PEM by shape, keyed stamps.
REDACTION_VERSION = 2

# matched as substrings of the tag NAME, without regard to case, not exact
# tags: plugins invent names like <rocommunity>, <sharedsecret>,
# <varusersfreeradiuspassword>, <otp_seed>, <ldap_bindpw>, and the IPsec
# pre-shared-key model stores its secret as <Key>. A false positive still
# diffs as a change (the stamp moves), so err toward matching.
SECRET_TAG_WORDS = ("prv", "crt", "cert", "pass", "pw", "secret", "psk",
                    "shared", "private", "key", "seed", "otp", "community",
                    "auth", "token", "hash", "credential", "tls")
# names too short or common to match as substrings, matched whole: pfSense
# keeps its Telegram and Slack notification tokens in <api>
# (notifications/telegram/api, notifications/slack/api)
SECRET_TAG_NAMES = ("api",)

_SECRET_NAME_RE = re.compile(
    "|".join(SECRET_TAG_WORDS) + "|^(?:" + "|".join(SECRET_TAG_NAMES) + ")$", re.I)
# a leaf element: optional attributes (kept), and content that is any mix of
# text, whitespace, CDATA sections, and comments (either may hold '<'). Their
# bodies are tempered: they cannot run past their own terminator, so a match
# never spans into a sibling element and hides it from the scan; nor past
# the next opener of their own kind, so a run of unclosed openers costs one
# pass over the text, not one pass per opener. The quantifiers are
# possessive (no backtracking into a body that already failed). Only leaves
# match, so a secret is redacted where it sits, never its parent.
_CDATA_BODY = r"(?:(?!\]\]>|<!\[CDATA\[).)*+"
_CDATA = r"<!\[CDATA\[" + _CDATA_BODY + r"\]\]>"
_COMMENT = r"<!--(?:(?!-->|<!--).)*+-->"
_ELEMENT_RE = re.compile(
    r"<([a-z0-9_-]+)(\s[^>]*)?>"
    r"((?:" + _CDATA + "|" + _COMMENT + r"|[^<])++)</\1\s*>",
    re.I | re.S)
_UNWRAP_RE = re.compile(
    r"<!\[CDATA\[(" + _CDATA_BODY + r")\]\]>|" + _COMMENT, re.S)
# catch-all for key material hiding under tags the list doesn't know: any
# element whose content is one long base64-ish run gets the same treatment
# (a real ACME account key surfaced under a bare <key> before this existed;
# unknown plugins will invent more names, so shape catches what names miss),
# and so does a PEM block pasted under any name
_LONG_BLOB_RE = re.compile(r"[A-Za-z0-9+/=\s]{200,}")

PLACEHOLDER = "redacted"
_ALREADY = re.compile(r"redacted(?::[0-9a-f]{8})?")   # a stamp or the constant
STAMP_RE = re.compile(r"redacted:([0-9a-f]{8})")


def rekey(legacy: str, key: bytes) -> str:
    """Keyed stamp digits from unkeyed ones. Revisions stored before #93
    hold `redacted:` + sha256(content)[:8]; keying that same value lets the
    upgrade convert them in place, and they still equal what a new poll
    stamps for unchanged content."""
    return hmac.new(key, legacy.encode(), hashlib.sha256).hexdigest()[:8]


def stamp(content: str, key: bytes) -> str:
    return "redacted:" + rekey(hashlib.sha256(content.encode()).hexdigest()[:8], key)


def _is_secret(tag: str, content: str) -> bool:
    if _SECRET_NAME_RE.search(tag):
        return True
    inner = _UNWRAP_RE.sub(lambda m: m.group(1) or "", content)
    return "-----BEGIN " in inner or bool(_LONG_BLOB_RE.fullmatch(inner))


def redact_xml(text: str, key: bytes | None = None) -> tuple[str, int]:
    """Replace the content of secret-bearing XML elements with a keyed stamp
    (`key` given) or the constant placeholder. Returns (redacted text,
    number of elements newly redacted)."""
    count = 0

    def sub(m: re.Match) -> str:
        nonlocal count
        tag, attrs, content = m.group(1), m.group(2) or "", m.group(3)
        if _ALREADY.fullmatch(content) or not _is_secret(tag, content):
            return m.group(0)
        count += 1
        out = stamp(content, key) if key else PLACEHOLDER
        return f"<{tag}{attrs}>{out}</{tag}>"

    return _ELEMENT_RE.sub(sub, text), count


def strip_stamps(text: str) -> str:
    """Stamps down to the constant placeholder, for text leaving the host:
    an 8-hex stamp of a low-entropy secret is a guessing oracle."""
    return STAMP_RE.sub(PLACEHOLDER, text)


# scheme://userinfo@ — the userinfo runs to the LAST '@' before the first
# '/', whitespace, or quote: httpx accepts an unencoded '@', '?', or '#' in a
# password, so stopping earlier would show its tail. The cost is a query or
# fragment holding an '@' with no path before it, which is masked too —
# over-masking, never a leak. The lookbehind starts a scheme only at the head
# of its run, so a long run of scheme characters is scanned once, not once
# per position.
_USERINFO_RE = re.compile(
    r"(?<![a-z0-9+.-])([a-z][a-z0-9+.-]*://)[^/\s'\"<>]*@", re.I)


def hide_userinfo(text: str | None) -> str | None:
    """Mask the credentials of every URL in `text` (`http://u:p@h/` becomes
    `http://***@h/`). Host and path stay readable."""
    if not text:
        return text
    return _USERINFO_RE.sub(r"\1***@", text)
