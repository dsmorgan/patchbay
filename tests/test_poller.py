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
        FakePoll.instances.append(self)

    def wait(self):
        self.released.wait(10)

    def poll(self):
        return 0 if self.released.is_set() else None

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
