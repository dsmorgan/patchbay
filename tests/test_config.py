"""Config parsing: malformed entries are skipped with warnings, never fatal;
DB-stored declarations fill in where the env file is silent."""

from patchbay.config import load_settings


def test_declarations_parse(clean_env):
    clean_env.setenv("PATCHBAY_LINKS", "sw1:1/0/1=host-a:eth0,sw1:1/0/2=tv")
    clean_env.setenv("PATCHBAY_CAPACITY", "sw1:1/0/16=3G,sw1:1/0/17=500M")
    clean_env.setenv("PATCHBAY_VLAN_FILTER", "sw1:1/0/11=1+24+73")
    clean_env.setenv("PATCHBAY_PANELS", r"Basement Panel:12=\[(\d+)\]")
    s = load_settings()
    assert s.links == [("sw1", "1/0/1", "host-a", "eth0"), ("sw1", "1/0/2", "tv", "?")]
    assert s.capacities == {("sw1", "1/0/16"): 3_000_000_000,
                            ("sw1", "1/0/17"): 500_000_000}
    assert s.vlan_filters == {("sw1", "1/0/11"): {1, 24, 73}}
    assert s.panels == [("Basement Panel", 12, r"\[(\d+)\]")]
    assert s.parse_warnings == ()


def test_malformed_entries_warn_not_crash(clean_env):
    # the colon-in-value-only case used to raise and kill every page
    clean_env.setenv("PATCHBAY_VLAN_FILTER", "sw1=1:24")
    clean_env.setenv("PATCHBAY_CAPACITY", "noport=3G,x:y=.G")
    clean_env.setenv("PATCHBAY_LINKS", "no-equals-here")
    s = load_settings()
    assert s.vlan_filters == {} and s.capacities == {} and s.links == []
    assert len(s.parse_warnings) == 4
    assert any("PATCHBAY_VLAN_FILTER" in w for w in s.parse_warnings)


def test_db_declarations_fill_env_silence(clean_env, tmp_path):
    import sqlite3

    from patchbay import db as pdb

    path = str(tmp_path / "test.db")
    c = sqlite3.connect(path)
    pdb.init(c)
    c.execute("INSERT INTO app_state (key, value) VALUES ('cfg:PATCHBAY_CAPACITY', "
              "'sw1:1/0/16=2.5G')")
    c.execute("INSERT INTO app_state (key, value) VALUES ('cfg:PATCHBAY_WAN_NAME', 'isp')")
    c.commit(); c.close()

    clean_env.setenv("PATCHBAY_WAN_NAME", "internet")  # env wins this one
    s = load_settings()
    assert s.capacities == {("sw1", "1/0/16"): 2_500_000_000}
    assert s.wan_name == "internet"
    assert s.declaration_sources == {"PATCHBAY_CAPACITY": "db",
                                     "PATCHBAY_WAN_NAME": "env"}


def test_auth_and_tls_defaults(clean_env):
    s = load_settings()
    assert s.auth_mode == "none" and s.tls_mode == "off"
    assert s.session_hours == 12


def test_unreadable_db_declarations_dont_prune(clean_env, tmp_path, monkeypatch):
    # a locked/corrupt DB must not present as "no declarations" — that would
    # let the poll delete declared cabling. Env declarations still load; the
    # readable flag goes false and a warning is raised.
    from patchbay import config as cfg

    def boom(_):
        raise cfg.DeclarationReadError("database is locked")

    monkeypatch.setattr(cfg, "_db_declarations", boom)
    clean_env.setenv("PATCHBAY_LINKS", "sw1:p1=nas:lan1")
    s = cfg.load_settings()
    assert s.links == [("sw1", "p1", "nas", "lan1")]  # env declarations intact
    assert s.declarations_readable is False
    assert any("could not read stored declarations" in w for w in s.parse_warnings)


def test_single_wan_needs_no_list_syntax(clean_env):
    clean_env.setenv("PATCHBAY_WAN_NAME", "Fiber")
    clean_env.setenv("PATCHBAY_WAN_PORT", "sw1:1/0/16")
    s = load_settings()
    assert s.wan_names == ("Fiber",) and s.wan_ports == (("sw1", "1/0/16"),)
    assert s.wan_pairing == (0,)
    assert s.wan_name == "Fiber" and s.wan_port == ("sw1", "1/0/16")
    assert s.parse_warnings == ()


def test_one_provider_over_several_ports(clean_env):
    # redundant circuits from one ISP: one cloud, two cables
    clean_env.setenv("PATCHBAY_WAN_NAME", "Fiber")
    clean_env.setenv("PATCHBAY_WAN_PORT", "sw1:1/0/16,sw1:1/0/17")
    s = load_settings()
    assert s.wan_names == ("Fiber",)
    assert s.wan_pairing == (0, 0)  # both ports land on the one provider
    assert s.parse_warnings == ()


def test_several_providers_pair_in_order(clean_env):
    clean_env.setenv("PATCHBAY_WAN_NAME", "Fiber,Cable")
    clean_env.setenv("PATCHBAY_WAN_PORT", "sw1:1/0/16,sw1:1/0/17,sw1:1/0/18")
    s = load_settings()
    assert s.wan_names == ("Fiber", "Cable")
    # the leftover third port joins the last provider named
    assert s.wan_pairing == (0, 1, 1)


def test_more_providers_than_ports_warns_and_drops(clean_env):
    clean_env.setenv("PATCHBAY_WAN_NAME", "Fiber,Cable,Starlink")
    clean_env.setenv("PATCHBAY_WAN_PORT", "sw1:1/0/16")
    s = load_settings()
    assert s.wan_names == ("Fiber",)      # only the one that can be placed
    assert any("Cable, Starlink" in w for w in s.parse_warnings)


def test_wan_defaults_and_malformed_port(clean_env):
    clean_env.setenv("PATCHBAY_WAN_PORT", "no-colon-here")
    s = load_settings()
    assert s.wan_names == ("internet",) and s.wan_ports == ()
    assert s.wan_port is None
    assert any("PATCHBAY_WAN_PORT" in w for w in s.parse_warnings)
