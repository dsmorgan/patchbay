"""Shared secret redaction (redact.py, #93): XML element rules and URL
userinfo masking. All values are fake."""

import ast
import hashlib
import inspect
import textwrap

import pytest

from patchbay import redact
from patchbay.redact import hide_userinfo, redact_xml, strip_stamps

BLOB = "QUJD" * 60  # 240 chars of base64-looking content
KEY = b"test-key"


@pytest.mark.parametrize("xml, secret", [
    ("<Key> <![CDATA[FAKE-a]]></Key>", "FAKE-a"),                  # ws + CDATA
    ("<Key><![CDATA[FAKE-b]]> </Key>", "FAKE-b"),                  # CDATA + ws
    ("<Key>FAKE-c<![CDATA[FAKE-d]]></Key>", "FAKE-"),              # text + CDATA
    ("<Key><![CDATA[FAKE-e]]>FAKE-f<![CDATA[x]]></Key>", "FAKE-"),  # interleaved
    ("<password><![CDATA[FAKE-<lt>-g]]></password>", "FAKE-"),     # '<' inside
    ("<password><!-- note -->FAKE-h</password>", "FAKE-h"),        # comment
    ('<privkey type="rsa">\n FAKE-i \n</privkey>', "FAKE-i"),      # attrs + ws
    (f"<widget>\n  <![CDATA[{BLOB}]]>\n</widget>", BLOB),          # blob in CDATA
    (f"<widget>{BLOB[:120]}<![CDATA[{BLOB[:120]}]]></widget>", BLOB[:120]),
    ("<widget><![CDATA[-----BEGIN EC PRIVATE KEY-----\nFAKEPEM\n"
     "-----END EC PRIVATE KEY-----]]></widget>", "FAKEPEM"),        # PEM in CDATA
    ("<widget> -----BEGIN OpenVPN Static key V1-----\nFAKEOVPN </widget>",
     "FAKEOVPN"),
])
def test_redact_xml_handles_any_mix_of_text_whitespace_and_cdata(xml, secret):
    out, n = redact_xml(xml, KEY)
    assert secret not in out and n == 1
    assert "redacted:" in out
    out, n = redact_xml(xml)                 # keyless: the constant
    assert secret not in out and n == 1
    assert ">redacted</" in out and "redacted:" not in out


@pytest.mark.parametrize("xml", [
    # CDATA or comments on both sides of a secret child: a body that could
    # run past its own terminator let the parent's match swallow the child,
    # and the scan resumed after it, so the child was never redacted
    "<item><![CDATA[a]]><Key>FAKE-s</Key><![CDATA[b]]></item>",
    "<item><!-- a --><Key>FAKE-s</Key><!-- b --></item>",
    "<item><![CDATA[a]]><Key>FAKE-s</Key><!-- b --></item>",
    "<descr><![CDATA[x]]></descr><Key>FAKE-s</Key><descr><![CDATA[y]]></descr>",
    "<descr><!-- x --></descr><Key>FAKE-s</Key><descr><!-- y --></descr>",
    "<a><![CDATA[x]]><b/><![CDATA[y]]></a><Key>FAKE-s</Key>",
])
def test_redact_xml_never_spans_into_a_sibling(xml):
    out, n = redact_xml(xml, KEY)
    assert "FAKE-s" not in out and n == 1, out


def test_redact_xml_leaves_ordinary_elements_alone():
    xml = ("<opnsense><system><hostname>fw1</hostname>"
           "<domain>example.net</domain></system>"
           "<interfaces><lan><if>em0</if><ipaddr>192.0.2.1</ipaddr>"
           "<descr><![CDATA[LAN <office>]]></descr></lan></interfaces>"
           "<filter><rule><descr>allow lan</descr></rule></filter></opnsense>")
    assert redact_xml(xml) == (xml, 0)


@pytest.mark.parametrize("key", [KEY, None])
def test_redact_xml_is_idempotent_and_keeps_stamps(key):
    xml = "<a><Key>FAKE</Key><b><![CDATA[x]]></b></a>"
    once, n1 = redact_xml(xml, key)
    twice, n2 = redact_xml(once, key)
    assert once == twice and n1 == 1 and n2 == 0


def test_redact_xml_redacts_the_leaf_not_its_parent():
    out, _ = redact_xml("<authserver><name>dir</name><Key>FAKE</Key></authserver>", KEY)
    assert out.startswith("<authserver><name>dir</name><Key>redacted:")


def test_stamps_are_keyed_and_rekey_continues_legacy_ones():
    legacy = hashlib.sha256(b"FAKE").hexdigest()[:8]
    s = redact.stamp("FAKE", KEY)
    assert legacy not in s and s != redact.stamp("FAKE", b"other-key")
    assert s == "redacted:" + redact.rekey(legacy, KEY)


def test_strip_stamps():
    assert strip_stamps("<Key>redacted:0123abcd</Key>") == "<Key>redacted</Key>"


# REDACTION_VERSION -> fingerprint of the rules it names. A rule change that
# moves the fingerprint must bump REDACTION_VERSION (so db.init re-redacts
# stored history) and add the new pair here.
RULE_FINGERPRINTS = {2: "b05e56ab5e2a5d35"}


def _code(fn) -> str:
    """A function's logic without its docstring, comments, or layout, so
    only a behavior change moves the fingerprint."""
    node = ast.parse(textwrap.dedent(inspect.getsource(fn))).body[0]
    if ast.get_docstring(node) is not None:
        node.body = node.body[1:]
    return ast.dump(node)


def test_rule_change_bumps_redaction_version():
    patterns = (redact._SECRET_NAME_RE, redact._ELEMENT_RE, redact._UNWRAP_RE,
                redact._LONG_BLOB_RE, redact._ALREADY, redact.STAMP_RE)
    parts = [repr(redact.SECRET_TAG_WORDS), repr(redact.SECRET_TAG_NAMES),
             redact.PLACEHOLDER,
             *(f"{p.pattern}/{int(p.flags)}" for p in patterns),
             *(_code(f) for f in (redact._is_secret, redact.stamp,
                                  redact.rekey, redact.redact_xml))]
    got = hashlib.sha256("\n".join(parts).encode()).hexdigest()[:16]
    assert RULE_FINGERPRINTS.get(redact.REDACTION_VERSION) == got, (
        f"redaction rules changed (fingerprint {got}): bump "
        "redact.REDACTION_VERSION and record the new fingerprint here")


@pytest.mark.parametrize("text, want", [
    ("http://admin:FAKEpw@192.0.2.5:8888", "http://***@192.0.2.5:8888"),
    ("Client error '401' for url 'http://u:FAKEpw@ox.example:8888/nodes.json'",
     "Client error '401' for url 'http://***@ox.example:8888/nodes.json'"),
    ("https://user@ox.example/", "https://***@ox.example/"),
    ("http://ox.example:8888", "http://ox.example:8888"),
    ("http://ox.example/a@b", "http://ox.example/a@b"),   # '@' in the path
    ("http://u:FAKE@pw@ox.example/a@b", "http://***@ox.example/a@b"),  # last '@'
    ("url 'http://u:p@ss@ox.example:8888?q=a@b'", "url 'http://***@ox.example:8888?q=a@b'"),
    ("mail admin@example.net", "mail admin@example.net"),
    (None, None),
    ("", ""),
])
def test_hide_userinfo(text, want):
    assert hide_userinfo(text) == want


def test_pfsense_notification_tokens_under_api_are_redacted():
    """pfSense keeps Telegram and Slack tokens at notifications/*/api; the
    name is matched whole, so <apinger> and friends are left alone."""
    xml = ("<notifications><telegram><api>FAKE-tg-token</api>"
           "<chatid>12345</chatid></telegram>"
           "<slack><api>FAKE-slack-token</api></slack></notifications>"
           "<apinger><alarm>none</alarm></apinger>")
    out, n = redact_xml(xml, KEY)
    assert "FAKE-tg-token" not in out and "FAKE-slack-token" not in out and n == 2
    assert "<chatid>12345</chatid>" in out and "<alarm>none</alarm>" in out


@pytest.mark.parametrize("text", [
    "<a><![CDATA[" * 20000,                    # unclosed CDATA openers
    "<a><!--" * 30000,                         # unclosed comment openers
    ("<a>" + "x" * 50 + "<![CDATA[") * 10000,
    "<a><![CDATA[x" * 20000 + "]]></a>",       # one far closer
])
def test_redact_xml_stays_linear_on_unclosed_openers(text):
    """Each unclosed opener used to rescan to the end of the text: 20,000
    of them took close to a minute."""
    import time
    t = time.perf_counter()
    redact_xml(text, KEY)
    assert time.perf_counter() - t < 1.0


def test_hide_userinfo_stays_linear():
    import time
    t = time.perf_counter()
    hide_userinfo("a." * 200000 + "b+" * 100000)
    assert time.perf_counter() - t < 1.0
