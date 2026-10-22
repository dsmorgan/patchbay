"""Shared secret redaction (redact.py, #93): XML element rules and URL
userinfo masking. All values are fake."""

import pytest

from patchbay.redact import hide_userinfo, redact_xml

BLOB = "QUJD" * 60  # 240 chars of base64-looking content


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
    out, n = redact_xml(xml)
    assert secret not in out and n == 1
    assert "redacted:" in out


def test_redact_xml_leaves_ordinary_elements_alone():
    xml = ("<opnsense><system><hostname>fw1</hostname>"
           "<domain>example.net</domain></system>"
           "<interfaces><lan><if>em0</if><ipaddr>192.0.2.1</ipaddr>"
           "<descr><![CDATA[LAN <office>]]></descr></lan></interfaces>"
           "<filter><rule><descr>allow lan</descr></rule></filter></opnsense>")
    assert redact_xml(xml) == (xml, 0)


def test_redact_xml_is_idempotent_and_keeps_hashes():
    xml = "<a><Key>FAKE</Key><b><![CDATA[x]]></b></a>"
    once, n1 = redact_xml(xml)
    twice, n2 = redact_xml(once)
    assert once == twice and n1 == 1 and n2 == 0


def test_redact_xml_redacts_the_leaf_not_its_parent():
    out, _ = redact_xml("<authserver><name>dir</name><Key>FAKE</Key></authserver>")
    assert out.startswith("<authserver><name>dir</name><Key>redacted:")


@pytest.mark.parametrize("text, want", [
    ("http://admin:FAKEpw@192.0.2.5:8888", "http://***@192.0.2.5:8888"),
    ("Client error '401' for url 'http://u:FAKEpw@ox.example:8888/nodes.json'",
     "Client error '401' for url 'http://***@ox.example:8888/nodes.json'"),
    ("https://user@ox.example/", "https://***@ox.example/"),
    ("http://ox.example:8888", "http://ox.example:8888"),
    ("http://ox.example/a@b", "http://ox.example/a@b"),   # '@' in the path
    ("mail admin@example.net", "mail admin@example.net"),
    (None, None),
    ("", ""),
])
def test_hide_userinfo(text, want):
    assert hide_userinfo(text) == want
