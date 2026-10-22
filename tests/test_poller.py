"""`patchbay poller`: the supervisor loop stops on SIGTERM without waiting out
a sleep or killing a poll (#111)."""

import os
import signal
import subprocess
import sys
import threading
import time

import pytest

from patchbay import cli


@pytest.fixture()
def handlers():
    saved = {s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT)}
    yield
    for s, h in saved.items():
        signal.signal(s, h)


class FakePoll:
    """A stand-in for the `patchbay poll` child: runs until released."""

    instances: list = []

    def __init__(self, *a, **kw):
        self.released = threading.Event()
        self.terminated = False
        self.signals: list = []
        FakePoll.instances.append(self)

    def wait(self):
        self.released.wait(10)

    def poll(self):
        return 0 if self.released.is_set() else None

    def send_signal(self, sig):
        self.signals.append(sig)   # a leased poll ignores it: keeps running

    def terminate(self):
        self.terminated = True
        self.released.set()


@pytest.fixture()
def fake_polls(monkeypatch, clean_env, handlers):
    FakePoll.instances = []
    monkeypatch.setattr(cli.subprocess, "Popen", FakePoll)
    clean_env.setenv("PATCHBAY_POLL_INTERVAL", "3600")
    return FakePoll


def after(delay, fn):
    t = threading.Timer(delay, fn)
    t.start()
    return t


def term():
    os.kill(os.getpid(), signal.SIGTERM)


def run_poller(args=None):
    started = time.monotonic()
    rc = cli.main(["poller", *(args or [])])
    return rc, time.monotonic() - started


def test_stop_during_sleep_exits_at_once(fake_polls):
    after(0.2, lambda: fake_polls.instances[0].released.set())  # poll ends
    after(0.5, term)                                            # then stop
    rc, took = run_poller()
    assert rc == 0
    assert took < 3                   # not the 3600 s interval
    assert len(fake_polls.instances) == 1


def test_stop_during_poll_lets_it_finish(fake_polls, capsys):
    after(0.2, term)
    after(0.6, lambda: fake_polls.instances[0].released.set())
    rc, took = run_poller()
    assert rc == 0
    assert 0.5 < took < 3             # waited for the poll, then stopped
    assert not fake_polls.instances[0].terminated
    assert len(fake_polls.instances) == 1   # and began no second one
    assert "stopping after this poll" in capsys.readouterr().out
    # told to give up only if it is still waiting for the lease (#109)
    assert fake_polls.instances[0].signals == [cli.STOP_WAITING]


def test_second_signal_aborts_the_poll(fake_polls, capsys):
    after(0.2, term)
    after(0.4, term)
    rc, took = run_poller()
    assert rc == 1
    assert took < 3
    assert fake_polls.instances[0].terminated
    err = capsys.readouterr().err
    assert "aborting the poll" in err and "cut short" in err


def test_wait_stops_with_the_signal(fake_polls, monkeypatch):
    monkeypatch.setattr(cli, "WAIT_STEP", 30)
    monkeypatch.setattr(cli.socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    after(0.3, term)
    rc, took = run_poller(["--wait", "librenms.example:8000"])
    assert rc == 0
    assert took < 3
    assert fake_polls.instances == []   # stopped before the first poll


def test_wait_gives_up_and_polls_anyway(fake_polls, monkeypatch, capsys):
    monkeypatch.setattr(cli, "WAIT_STEP", 0.01)
    monkeypatch.setattr(cli, "WAIT_TRIES", 3)
    monkeypatch.setattr(cli.socket, "create_connection",
                        lambda *a, **k: (_ for _ in ()).throw(OSError("down")))
    after(0.3, lambda: fake_polls.instances[0].released.set())
    after(0.5, term)
    rc, _ = run_poller(["--wait", "librenms.example:8000"])
    assert rc == 0
    assert "still down after" in capsys.readouterr().out
    assert len(fake_polls.instances) == 1


def test_real_process_stops_in_a_second(tmp_path):
    """The shape that matters: PID-1-like, nothing configured so each poll
    exits at once, then SIGTERM during the sleep."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("PATCHBAY_")}
    env.update(PATCHBAY_ENV=os.devnull, PATCHBAY_DB=str(tmp_path / "p.db"),
               PATCHBAY_POLL_INTERVAL="3600")
    p = subprocess.Popen([sys.executable, "-m", "patchbay", "poller"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(3)          # one poll has come and gone; now asleep
        assert p.poll() is None
        p.send_signal(signal.SIGTERM)
        started = time.monotonic()
        assert p.wait(timeout=10) == 0
        assert time.monotonic() - started < 2
        assert "stopped" in p.stdout.read()
    finally:
        if p.poll() is None:
            p.kill()


def _clean_env(tmp_path, **extra):
    env = {k: v for k, v in os.environ.items()
           if not k.startswith(("PATCHBAY_", "LIBRENMS_", "IPAM_", "UNIFI_",
                                "OPNSENSE_", "PFSENSE_", "VSPHERE_", "OXIDIZED_"))}
    env.update(PATCHBAY_ENV=os.devnull, PATCHBAY_DB=str(tmp_path / "p.db"), **extra)
    return env


def _hold_lease(tmp_path):
    from patchbay import db

    with db.connect(str(tmp_path / "p.db")) as c:
        db.init(c)
        assert db.claim_poll_lease(c, "web:1:other", ttl=600) == db.LEASE_CLAIMED


@pytest.mark.skipif(cli.STOP_WAITING is None, reason="no SIGUSR1 here")
def test_a_stop_while_waiting_on_the_lease_exits_at_once(tmp_path):
    """#109 x #111: the child waits for another poll's lease (up to 15
    minutes). A stop must not sit that out until docker's grace period
    runs out and SIGKILLs the poller: the waiting child holds nothing, so
    it is told to give up, and the poller stops in seconds."""
    _hold_lease(tmp_path)
    # configured, but never reached: the poll waits for the lease first
    env = _clean_env(tmp_path, PATCHBAY_POLL_INTERVAL="3600",
                     OXIDIZED_URL="http://192.0.2.1:9")
    p = subprocess.Popen([sys.executable, "-m", "patchbay", "poller"], env=env,
                         stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    try:
        time.sleep(4)          # the child is up and waiting on the lease
        assert p.poll() is None
        p.send_signal(signal.SIGTERM)
        started = time.monotonic()
        assert p.wait(timeout=15) == 0
        assert time.monotonic() - started < 5
        out, err = p.communicate()
        assert "another poll is running; waiting" in err
        assert "stopped while waiting" in out and "poller: stopped" in out
    finally:
        if p.poll() is None:
            p.kill()


@pytest.mark.skipif(cli.STOP_WAITING is None, reason="no SIGUSR1 here")
def test_a_leased_poll_ignores_the_give_up_signal(tmp_path):
    """Once a poll holds the lease, the poller's first stop lets it finish."""
    env = _clean_env(tmp_path)
    code = (
        "import os, signal, sys, time\n"
        "from patchbay import cli\n"
        "class Slow:\n"
        "    name = 'slow'\n"
        "    def configured(self, s): return True\n"
        "    def collect(self, s, conn):\n"
        "        os.kill(os.getpid(), cli.STOP_WAITING)\n"
        "        time.sleep(0.2)\n"
        "        return 'survived'\n"
        "cli.available = lambda s: {'slow': Slow()}\n"
        "sys.exit(cli.main(['poll']))\n")
    r = subprocess.run([sys.executable, "-c", code], env=env, capture_output=True,
                       text=True, timeout=30)
    assert r.returncode == 0, r.stderr
    assert "[ok]   slow: survived" in r.stdout
