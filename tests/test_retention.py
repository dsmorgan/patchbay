"""Tiered snapshot retention (#65): the spec parser and the pure keeper
selection, over synthetic timestamps — no filesystem except where the
wiring itself is under test."""

from __future__ import annotations

from datetime import date, datetime, timedelta

import pytest

from patchbay.retention import (DEFAULT_SPEC, KeepSpec, classify,
                                parse_keep_spec, select_keepers, stamp_of)


def name(ts: datetime, suffix: str = "") -> str:
    return ts.strftime(f"patchbay-%Y%m%d-%H%M%S{suffix}.html")


def nightly(start: date, end: date) -> list[datetime]:
    days = (end - start).days + 1
    return [datetime.combine(start + timedelta(days=i), datetime.min.time())
            .replace(hour=3, minute=30) for i in range(days)]


# --- parsing ---------------------------------------------------------------

def test_default_spec_parses_to_issue_tiers():
    assert parse_keep_spec(DEFAULT_SPEC) == KeepSpec(daily=30, monthly=12,
                                                     yearly=3, first=True)


def test_spec_terms_whitespace_and_case():
    spec = parse_keep_spec(" 7d , 4W,0m, 2y ,FIRST ")
    assert spec == KeepSpec(daily=7, weekly=4, monthly=0, yearly=2, first=True)
    assert str(spec) == "7,4w,0m,2y,first"


def test_bare_integer_is_daily_only():
    assert parse_keep_spec("30") == KeepSpec(daily=30)
    assert parse_keep_spec("0") == KeepSpec(daily=0)


@pytest.mark.parametrize("raw", ["30,", "thirty", "-5", "12x", "3y,first,2y",
                                 "30,10", "first,first", "1.5m", "12 m"])
def test_bad_specs_refuse(raw):
    with pytest.raises(ValueError):
        parse_keep_spec(raw)


def test_describe_reads_as_a_sentence():
    assert parse_keep_spec(DEFAULT_SPEC).describe() == (
        "the newest 30, the first of each of the last 12 months, "
        "the first of each of the last 3 years and the first ever")
    assert parse_keep_spec("0,3y").describe() == "every timestamped snapshot"
    assert parse_keep_spec("0m").describe() == (
        "the newest and the first of every month")


# --- selection -------------------------------------------------------------

def test_three_years_of_nightlies_pruned_daily():
    """The done-when: after years of nightly runs, each followed by a prune,
    the default spec leaves 30 nightlies, the first of each of the last 12
    months, the first of each of the last 3 years, and the first ever."""
    spec = parse_keep_spec(DEFAULT_SPEC)
    on_disk: set[str] = set()
    every = nightly(date(2022, 6, 15), date(2025, 12, 31))
    for ts in every:
        on_disk.add(name(ts))
        _, prune = classify(on_disk, spec)
        on_disk -= set(prune)

    def at(y, m, d):
        return name(datetime(y, m, d, 3, 30))

    dailies = {at(2025, 12, d) for d in range(2, 32)}
    monthlies = {at(2025, m, 1) for m in range(1, 13)}
    yearlies = {at(2023, 1, 1), at(2024, 1, 1), at(2025, 1, 1)}
    first = {at(2022, 6, 15)}
    assert on_disk == dailies | monthlies | yearlies | first
    assert len(on_disk) == 30 + 12 + 2 + 1   # 2025-01-01 is month and year

    kept, prune = classify(on_disk, spec)
    assert not prune                          # a settled directory is stable
    assert kept[at(2025, 1, 1)] == ("monthly", "yearly")
    assert kept[at(2022, 6, 15)] == ("first",)
    assert kept[at(2025, 12, 1)] == ("monthly",)   # just past the 30 dailies


def test_incremental_pruning_matches_one_shot_selection():
    """Earliest-of-period keepers are stable: pruning every night arrives at
    the same set as selecting once over the full history."""
    spec = parse_keep_spec("10,6w,5m,2y,first")
    every = nightly(date(2023, 2, 10), date(2025, 7, 4))
    survivors: set[datetime] = set()
    for ts in every:
        survivors.add(ts)
        survivors = set(select_keepers(survivors, spec))
    assert survivors == set(select_keepers(every, spec))


def test_monthly_keeper_is_known_the_day_it_is_taken():
    spec = parse_keep_spec("1,12m")
    jan = nightly(date(2025, 1, 1), date(2025, 1, 31))
    feb1 = datetime(2025, 2, 1, 3, 30)
    keep = select_keepers(jan + [feb1], spec)
    assert keep[feb1] == ("daily", "monthly")
    assert keep[jan[0]] == ("monthly",)
    assert set(keep) == {jan[0], feb1}


def test_periodic_windows_count_calendar_periods_back_from_newest():
    # gaps count against the window: a month with no snapshot is still one
    # of the last 3
    stamps = [datetime(2025, 1, 5), datetime(2025, 2, 5), datetime(2025, 4, 5)]
    keep = select_keepers(stamps, parse_keep_spec("1,3m"))
    assert set(keep) == {datetime(2025, 2, 5), datetime(2025, 4, 5)}


def test_weekly_tier_uses_monday_weeks():
    # 2025-03-02 is a Sunday, 03-03 a Monday
    stamps = [datetime(2025, 3, 1), datetime(2025, 3, 2), datetime(2025, 3, 3),
              datetime(2025, 3, 9), datetime(2025, 3, 10)]
    keep = select_keepers(stamps, parse_keep_spec("1,2w"))
    assert set(keep) == {datetime(2025, 3, 3), datetime(2025, 3, 10)}


def test_first_ever_survives_every_window():
    old = datetime(2019, 7, 4, 12, 0)
    recent = nightly(date(2025, 5, 1), date(2025, 5, 20))
    keep = select_keepers([old] + recent, parse_keep_spec("3,1y,first"))
    assert keep[old] == ("first",)
    assert set(keep) == {old, recent[0], *recent[-3:]}
    # without the term it goes
    assert old not in select_keepers([old] + recent, parse_keep_spec("3,1y"))


def test_bare_integer_keeps_todays_meaning():
    every = nightly(date(2024, 11, 1), date(2025, 2, 28))
    names = {name(ts) for ts in every}
    kept, prune = classify(names, parse_keep_spec("7"))
    assert sorted(kept) == sorted(names)[-7:]
    assert set(prune) == names - set(kept)


def test_zero_keeps_everything():
    every = nightly(date(2024, 1, 1), date(2025, 1, 1))
    assert set(select_keepers(every, parse_keep_spec("0"))) == set(every)
    # 0 in a periodic tier is that tier unlimited, not "off"
    keep = select_keepers(every, parse_keep_spec("1,0m"))
    assert len(keep) == 13   # every month-first; the newest is one of them


def test_spec_without_daily_still_keeps_the_newest():
    every = nightly(date(2025, 1, 1), date(2025, 1, 15))
    keep = select_keepers(every, parse_keep_spec("12m"))
    assert set(keep) == {every[0], every[-1]}


def test_selection_is_order_independent_and_empty_safe():
    every = nightly(date(2024, 12, 20), date(2025, 1, 10))
    spec = parse_keep_spec(DEFAULT_SPEC)
    assert select_keepers(list(reversed(every)), spec) == select_keepers(every, spec)
    assert select_keepers([], spec) == {}


def test_only_plain_timestamped_names_are_governed():
    """Alert copies (#66), latest, and strays are outside the tiers: never
    claimed, never pruned, however tight the spec."""
    every = nightly(date(2025, 1, 1), date(2025, 1, 10))
    alert = name(every[2], "-alert")
    names = {name(ts) for ts in every} | {
        alert, "patchbay-latest.html", "patchbay-20251399-000000.html",
        ".patchbay-20250101-033000.html.part", "notes.txt"}
    kept, prune = classify(names, parse_keep_spec("1"))
    assert len(prune) == 9 and all(stamp_of(n) for n in prune)
    assert alert not in prune and alert not in kept
    assert stamp_of(alert) is None
    assert stamp_of("patchbay-latest.html") is None


# --- wiring: config, write_snapshot, /snapshots, /ops ----------------------

def test_config_default_and_bad_spec_warning(clean_env):
    from patchbay.config import load_settings

    assert load_settings().snapshot_keep == parse_keep_spec(DEFAULT_SPEC)
    clean_env.setenv("PATCHBAY_SNAPSHOT_KEEP", "30,12months")
    s = load_settings()
    assert s.snapshot_keep is None
    assert any(w.startswith("PATCHBAY_SNAPSHOT_KEEP:") and "pruned" in w
               for w in s.parse_warnings)


def test_bad_spec_prunes_nothing_local_or_delivered(clean_env, tmp_path):
    from tests.test_web import seed

    seed(str(tmp_path / "test.db"))
    from patchbay.config import load_settings
    from patchbay.snapshot import write_snapshot

    local, off = tmp_path / "snaps", tmp_path / "offsite"
    clean_env.setenv("PATCHBAY_SNAPSHOT_DIR", str(local))
    clean_env.setenv("PATCHBAY_SNAPSHOT_DELIVER_DIR", str(off))
    clean_env.setenv("PATCHBAY_SNAPSHOT_KEEP", "1,first,first")
    old = [name(ts) for ts in nightly(date(2020, 1, 1), date(2020, 1, 20))]
    for d in (local, off):
        d.mkdir()
        for n in old:
            (d / n).write_text("old")
    path = write_snapshot(load_settings())
    for d in (local, off):
        assert {p.name for p in d.iterdir()} >= set(old) | {path.name}


def test_tiered_prune_applies_to_both_directories(clean_env, tmp_path):
    from tests.test_web import seed

    seed(str(tmp_path / "test.db"))
    from patchbay.config import load_settings
    from patchbay.snapshot import write_snapshot

    local, off = tmp_path / "snaps", tmp_path / "offsite"
    clean_env.setenv("PATCHBAY_SNAPSHOT_DIR", str(local))
    clean_env.setenv("PATCHBAY_SNAPSHOT_DELIVER_DIR", str(off))
    clean_env.setenv("PATCHBAY_SNAPSHOT_KEEP", "1,first")
    old = [name(ts) for ts in nightly(date(2020, 1, 1), date(2020, 1, 5))]
    alert = name(datetime(2020, 1, 3, 9, 15), "-alert")
    for d in (local, off):
        d.mkdir()
        for n in old + [alert]:
            (d / n).write_text("old")
    path = write_snapshot(load_settings())
    for d in (local, off):
        assert {p.name for p in d.iterdir()} == {
            old[0], path.name, alert, "patchbay-latest.html"}


def test_snapshots_page_shows_tiers_and_spec(clean_env, tmp_path):
    from fastapi.testclient import TestClient

    from tests.test_web import seed

    seed(str(tmp_path / "test.db"))
    from patchbay.web import app

    d = tmp_path / "snaps"
    d.mkdir()
    clean_env.setenv("PATCHBAY_SNAPSHOT_DIR", str(d))
    clean_env.setenv("PATCHBAY_SNAPSHOT_KEEP", "1,12m,first")
    for n in ("patchbay-20250101-033000.html", "patchbay-20250102-033000.html",
              "patchbay-20250103-033000.html", "patchbay-20250102-091500-alert.html"):
        (d / n).write_text("x")
    body = TestClient(app).get("/snapshots").text
    assert "PATCHBAY_SNAPSHOT_KEEP=1,12m,first" in body
    assert "the first of each of the last 12 months" in body
    row = body[body.index("patchbay-20250101-033000.html"):]
    assert row.index("monthly, first") < row.index("</tr>")
    row = body[body.index("patchbay-20250102-033000.html"):]
    assert row.index("pruned next snapshot") < row.index("</tr>")
    assert "-alert.html" not in body          # not a tiered snapshot

    clean_env.setenv("PATCHBAY_SNAPSHOT_KEEP", "1,12q")
    body = TestClient(app).get("/snapshots").text
    assert "pruning nothing until" in body and "12q" in body


def test_ops_shows_parsed_tiers_and_warning(clean_env, tmp_path):
    from fastapi.testclient import TestClient

    from tests.test_web import seed

    seed(str(tmp_path / "test.db"))
    from patchbay.web import app

    body = TestClient(app).get("/ops").text
    assert "30,12m,3y,first · keeps the newest 30" in body
    clean_env.setenv("PATCHBAY_SNAPSHOT_KEEP", "30,12q")
    body = TestClient(app).get("/ops").text
    assert "unparsed · pruning off" in body
    # the warning renders in the field's own block, not the page-top box
    field = body.split('class="dclwarn" data-var="PATCHBAY_SNAPSHOT_KEEP"', 1)[1]
    head, warn_block = field.split(">", 1)
    assert "hidden" not in head
    assert "no snapshot is pruned until it is" in warn_block.split("</div>\n", 1)[0]
    assert "Skipped configuration entries" not in body
