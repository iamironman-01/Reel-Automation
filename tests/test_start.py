import signal
import sys

import pytest

import start


class FakeProc:
    """Stands in for subprocess.Popen. `exit_at` is a clock time at which it dies."""

    def __init__(self, clock, name, exit_at=None, exit_code=0, ignore_sigterm=False):
        self.clock = clock
        self.name = name
        self.exit_at = exit_at
        self.exit_code = exit_code
        self.ignore_sigterm = ignore_sigterm
        self.returncode = None
        self.pid = 1000
        self.signals = []
        self.killed = False
        self.sigterm_at = None
        self.exited_at = None

    def poll(self):
        if self.returncode is None and self.exit_at is not None \
                and self.clock.now >= self.exit_at:
            self.returncode = self.exit_code
            self.exited_at = self.clock.now
        return self.returncode

    def send_signal(self, sig):
        self.signals.append(sig)
        if sig == signal.SIGTERM:
            self.sigterm_at = self.clock.now
            if not self.ignore_sigterm and self.returncode is None:
                self.returncode = 0

    def terminate(self):
        self.send_signal(signal.SIGTERM)

    def kill(self):
        self.killed = True
        self.returncode = -9

    def wait(self, timeout=None):
        return self.returncode


class Clock:
    def __init__(self):
        self.now = 0.0
        self.sleeps = []
        self.on_sleep = None

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds
        if self.on_sleep:
            self.on_sleep(self)


class World:
    """Fake Popen factory: records every spawned process in order."""

    def __init__(self):
        self.clock = Clock()
        self.procs = []
        self.plans = {"dash": [], "sched": []}

    def plan(self, kind, **kwargs):
        self.plans[kind].append(kwargs)

    def popen(self, cmd):
        kind = "dash" if "streamlit" in cmd else "sched"
        kwargs = self.plans[kind].pop(0) if self.plans[kind] else {}
        if "exit_in" in kwargs:
            kwargs["exit_at"] = self.clock.now + kwargs.pop("exit_in")
        proc = FakeProc(self.clock, kind, **kwargs)
        proc.cmd = cmd
        proc.spawned_at = self.clock.now
        self.procs.append(proc)
        return proc

    def of(self, kind):
        return [p for p in self.procs if p.name == kind]

    def supervisor(self, grace=240):
        dash, sched = start.build_commands({})
        return start.Supervisor(
            dash, sched, grace_seconds=grace, popen=self.popen,
            sleep=self.clock.sleep, monotonic=self.clock.monotonic, log=lambda m: None,
        )

    def assert_none_running(self):
        assert all(p.poll() is not None for p in self.procs)


# --- command construction -------------------------------------------------

def test_build_commands_defaults():
    dash, sched = start.build_commands({})
    assert dash == [sys.executable, "-m", "streamlit", "run", "dashboard.py",
                    "--server.port", "8501", "--server.address", "0.0.0.0",
                    "--server.headless", "true"]
    assert sched == [sys.executable, "main.py"]


def test_build_commands_uses_port_from_env():
    dash, _ = start.build_commands({"PORT": "10000"})
    assert dash[dash.index("--server.port") + 1] == "10000"


@pytest.mark.parametrize("bad", ["", "abc", "0", "65536", "-1", "80.5", "8 0", "²"])
def test_invalid_port_rejected(bad):
    with pytest.raises(ValueError):
        start.build_commands({"PORT": bad})


def test_port_bounds_accepted():
    assert start.parse_port({"PORT": "1"}) == 1
    assert start.parse_port({"PORT": "65535"}) == 65535


def test_grace_seconds_parsing():
    assert start.parse_grace_seconds({}) == 240
    assert start.parse_grace_seconds({"SHUTDOWN_GRACE_SECONDS": "30"}) == 30
    for bad in ["0", "-5", "abc", "", "1.5"]:
        with pytest.raises(ValueError):
            start.parse_grace_seconds({"SHUTDOWN_GRACE_SECONDS": bad})


def test_backoff_delay_sequence_and_cap():
    assert [start.backoff_delay(i) for i in range(7)] == [10, 20, 40, 80, 160, 300, 300]
    assert start.backoff_delay(10_000) == 300


# --- dashboard exit -------------------------------------------------------

@pytest.mark.parametrize("dash_code,expected", [(0, 1), (3, 3)])
def test_dashboard_exit_stops_scheduler_and_returns_nonzero(dash_code, expected):
    w = World()
    w.plan("dash", exit_in=5, exit_code=dash_code)
    code = w.supervisor().run()
    assert code == expected
    sched = w.of("sched")[0]
    assert signal.SIGTERM in sched.signals
    w.assert_none_running()


def test_dashboard_exit_kills_scheduler_after_grace_if_stuck():
    w = World()
    w.plan("dash", exit_in=2, exit_code=1)
    w.plan("sched", ignore_sigterm=True)
    w.supervisor(grace=30).run()
    sched = w.of("sched")[0]
    assert sched.killed
    assert sched.clock.now - sched.sigterm_at >= 30
    w.assert_none_running()


# --- scheduler restart ----------------------------------------------------

def test_scheduler_crash_restarts_with_exponential_backoff_capped():
    w = World()
    # Each scheduler instance dies 1s after it starts; dashboard dies at t=3000.
    for _ in range(10):
        w.plan("sched", exit_in=1, exit_code=1)
    w.plan("dash", exit_at=3000, exit_code=0)
    w.supervisor().run()

    scheds = w.of("sched")
    assert len(scheds) >= 8
    gaps = [round(scheds[i + 1].spawned_at - p.exited_at)
            for i, p in enumerate(scheds[:7])]
    # Detection happens on the 1 s poll, so allow a 1-2 s tolerance on the delay.
    expected = [10, 20, 40, 80, 160, 300, 300]
    for gap, want in zip(gaps, expected):
        assert want - 1 <= gap <= want + 1, (gaps, expected)
    # Dashboard was never restarted or signalled before its own exit.
    assert len(w.of("dash")) == 1
    assert w.of("dash")[0].signals == []
    w.assert_none_running()


def test_scheduler_clean_exit_also_restarted_and_dashboard_untouched():
    w = World()
    w.plan("sched", exit_in=1, exit_code=0)
    w.plan("dash", exit_at=60, exit_code=0)
    w.supervisor().run()
    assert len(w.of("sched")) >= 2
    assert w.of("dash")[0].signals == []


def test_backoff_resets_after_long_healthy_run():
    w = World()
    logs = []
    w.plan("sched", exit_in=1, exit_code=1)    # failure 1 -> 10 s
    w.plan("sched", exit_in=1, exit_code=1)    # failure 2 -> 20 s
    w.plan("sched", exit_in=500, exit_code=1)  # healthy 500 s, then crash -> reset -> 10 s
    w.plan("sched", exit_in=1, exit_code=1)
    w.plan("dash", exit_at=1500, exit_code=0)
    dash, sched = start.build_commands({})
    sup = start.Supervisor(dash, sched, popen=w.popen, sleep=w.clock.sleep,
                           monotonic=w.clock.monotonic, log=logs.append)
    sup.run()
    delays = [int(m.split("restarting in ")[1].rstrip("s")) for m in logs
              if "restarting in" in m]
    assert delays[:4] == [10, 20, 10, 20]
    w.assert_none_running()


def test_restart_logs_exit_code():
    w = World()
    logs = []
    w.plan("sched", exit_in=1, exit_code=7)
    w.plan("dash", exit_at=20, exit_code=0)
    dash, sched = start.build_commands({})
    start.Supervisor(dash, sched, popen=w.popen, sleep=w.clock.sleep,
                     monotonic=w.clock.monotonic, log=logs.append).run()
    assert any("exit code 7" in m for m in logs)


def test_scheduler_spawn_failure_during_restart_is_retried():
    w = World()
    real_popen = w.popen
    calls = {"sched": 0}

    def flaky(cmd):
        if "main.py" in cmd:
            calls["sched"] += 1
            if calls["sched"] == 2:
                raise OSError("boom")
        return real_popen(cmd)

    w.plan("sched", exit_in=1, exit_code=1)
    w.plan("dash", exit_at=200, exit_code=0)
    dash, sched = start.build_commands({})
    start.Supervisor(dash, sched, popen=flaky, sleep=w.clock.sleep,
                     monotonic=w.clock.monotonic, log=lambda m: None).run()
    assert calls["sched"] >= 3
    w.assert_none_running()


# --- graceful shutdown ----------------------------------------------------

def test_shutdown_sigterms_scheduler_first_and_returns_zero():
    w = World()
    order = []
    sup = w.supervisor()
    w.clock.on_sleep = lambda c: sup.request_shutdown() if c.now >= 5 else None
    real = FakeProc.send_signal

    def record(self, sig):
        order.append(self.name)
        real(self, sig)

    FakeProc.send_signal = record
    try:
        code = sup.run()
    finally:
        FakeProc.send_signal = real
    assert code == 0
    assert order == ["sched", "dash"]
    assert not any(p.killed for p in w.procs)
    w.assert_none_running()


def test_shutdown_waits_for_scheduler_then_kills_only_after_grace():
    w = World()
    w.plan("sched", ignore_sigterm=True)
    sup = w.supervisor(grace=240)
    w.clock.on_sleep = lambda c: sup.request_shutdown() if c.now >= 3 else None
    code = sup.run()
    sched, dash = w.of("sched")[0], w.of("dash")[0]
    assert code == 0
    assert sched.signals == [signal.SIGTERM]
    assert sched.killed
    assert sched.clock.now - sched.sigterm_at >= 240
    # Dashboard is only touched after the scheduler is gone.
    assert dash.sigterm_at >= sched.sigterm_at + 240
    assert not dash.killed
    w.assert_none_running()


def test_shutdown_does_not_kill_scheduler_that_finishes_within_grace():
    w = World()
    # Scheduler finishes its in-progress send 100 s after SIGTERM.
    class Slow(FakeProc):
        def send_signal(self, sig):
            self.signals.append(sig)
            self.sigterm_at = self.clock.now
            self.exit_at, self.exit_code = self.clock.now + 100, 0

    sup = w.supervisor(grace=240)
    orig = w.popen

    def popen(cmd):
        if "main.py" in cmd:
            p = Slow(w.clock, "sched")
            p.cmd = cmd
            w.procs.append(p)
            return p
        return orig(cmd)

    sup._popen = popen
    w.clock.on_sleep = lambda c: sup.request_shutdown() if c.now >= 3 else None
    assert sup.run() == 0
    assert not w.of("sched")[0].killed
    w.assert_none_running()


def test_stuck_dashboard_killed_after_ten_seconds():
    w = World()
    w.plan("dash", ignore_sigterm=True)
    sup = w.supervisor()
    w.clock.on_sleep = lambda c: sup.request_shutdown() if c.now >= 2 else None
    assert sup.run() == 0
    dash = w.of("dash")[0]
    assert dash.killed
    assert 10 <= dash.clock.now - dash.sigterm_at < 12
    w.assert_none_running()


def test_no_orphans_when_scheduler_spawn_fails_at_startup():
    w = World()
    real = w.popen

    def popen(cmd):
        if "main.py" in cmd:
            raise OSError("no python")
        return real(cmd)

    dash, sched = start.build_commands({})
    sup = start.Supervisor(dash, sched, popen=popen, sleep=w.clock.sleep,
                           monotonic=w.clock.monotonic, log=lambda m: None)
    with pytest.raises(OSError):
        sup.run()
    assert w.of("dash")[0].signals == [signal.SIGTERM]
    w.assert_none_running()


def test_no_orphans_on_unexpected_exception_in_loop():
    w = World()
    sup = w.supervisor()

    def boom(c):
        raise RuntimeError("bug")

    w.clock.on_sleep = boom
    with pytest.raises(RuntimeError):
        sup.run()
    w.assert_none_running()


def test_poll_interval_is_one_second():
    w = World()
    w.plan("dash", exit_in=5, exit_code=0)
    w.supervisor().run()
    assert w.clock.sleeps[0] == 1


# --- main() -----------------------------------------------------------------

def test_main_installs_handlers_and_returns_supervisor_code(monkeypatch, capsys):
    installed = {}
    monkeypatch.setattr(start.signal, "signal", lambda s, h: installed.setdefault(s, h))

    class FakeSup:
        def __init__(self, dash, sched, grace_seconds):
            self.grace = grace_seconds

        def request_shutdown(self, *a):
            pass

        def run(self):
            return 42

    monkeypatch.setattr(start, "Supervisor", FakeSup)
    code = start.main({"PORT": "10000", "SHUTDOWN_GRACE_SECONDS": "60",
                       "SECRET_TOKEN": "s3cret"})
    assert code == 42
    assert set(installed) == {signal.SIGTERM, signal.SIGINT}
    out = capsys.readouterr().out
    assert "10000" in out and "s3cret" not in out


def test_main_rejects_bad_port_without_starting(monkeypatch, capsys):
    monkeypatch.setattr(start, "Supervisor",
                        lambda *a, **k: pytest.fail("must not start"))
    assert start.main({"PORT": "nope"}) == 2
    assert "PORT" in capsys.readouterr().err


def test_import_has_no_side_effects_on_signals():
    before = signal.getsignal(signal.SIGTERM)
    w = World()
    w.supervisor()
    assert signal.getsignal(signal.SIGTERM) is before


# --- keep-alive (Render free plan) ------------------------------------------------

def test_keep_alive_off_without_a_url():
    assert start.keep_alive_settings({}) is None
    assert start.keep_alive_settings({"KEEP_ALIVE_MINUTES": "5"}) is None


def test_keep_alive_uses_render_url_and_nine_minutes_by_default():
    env = {"RENDER_EXTERNAL_URL": "https://reeldrop.onrender.com/"}
    assert start.keep_alive_settings(env) == (
        "https://reeldrop.onrender.com/_stcore/health", 9 * 60)


def test_keep_alive_url_overrides_render_url_and_minutes_are_configurable():
    env = {"RENDER_EXTERNAL_URL": "https://a.onrender.com",
           "KEEP_ALIVE_URL": "https://b.example.com", "KEEP_ALIVE_MINUTES": "14"}
    assert start.keep_alive_settings(env) == ("https://b.example.com/_stcore/health", 840)


def test_keep_alive_can_be_turned_off():
    env = {"RENDER_EXTERNAL_URL": "https://a.onrender.com", "KEEP_ALIVE_MINUTES": "0"}
    assert start.keep_alive_settings(env) is None


@pytest.mark.parametrize("env", [
    {"RENDER_EXTERNAL_URL": "https://a.onrender.com", "KEEP_ALIVE_MINUTES": "15"},
    {"RENDER_EXTERNAL_URL": "https://a.onrender.com", "KEEP_ALIVE_MINUTES": "abc"},
    {"RENDER_EXTERNAL_URL": "https://a.onrender.com", "KEEP_ALIVE_MINUTES": "-1"},
    {"KEEP_ALIVE_URL": "ftp://a.onrender.com"},
    {"KEEP_ALIVE_URL": "a.onrender.com"},
])
def test_keep_alive_rejects_bad_settings(env):
    with pytest.raises(ValueError):
        start.keep_alive_settings(env)


class _Response:
    def __init__(self, status):
        self.status = status

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


@pytest.mark.parametrize("status,ok", [(200, True), (302, True), (404, False), (503, False)])
def test_ping_once_status(status, ok):
    seen = []

    def opener(url, timeout):
        seen.append((url, timeout))
        return _Response(status)

    assert start.ping_once("https://x/_stcore/health", opener=opener) == (ok, f"HTTP {status}")
    assert seen == [("https://x/_stcore/health", start.KEEP_ALIVE_TIMEOUT_SECONDS)]


def test_ping_once_never_raises():
    def opener(url, timeout):
        raise ConnectionError("down")

    assert start.ping_once("https://x", opener=opener) == (False, "ConnectionError")


class _FakeStop:
    """Event stand-in: wait() returns True (stop) after `rounds` waits."""

    def __init__(self, rounds):
        self.rounds = rounds
        self.waits = []

    def wait(self, timeout):
        self.waits.append(timeout)
        return len(self.waits) > self.rounds


def test_keep_alive_loop_pings_each_interval_and_logs_only_changes():
    results = iter([(True, "HTTP 200"), (False, "URLError"), (False, "URLError"),
                    (True, "HTTP 200")])
    pings, logs = [], []
    stop = _FakeStop(rounds=4)

    def ping(url):
        pings.append(url)
        return next(results)

    start.keep_alive_loop("https://x/h", 540, stop, ping=ping, log=logs.append)
    assert pings == ["https://x/h"] * 4
    assert stop.waits == [540] * 5                     # waits first, so no ping at start
    assert len(logs) == 3 and "ping works again" in logs[-1]


def test_start_keep_alive_starts_a_daemon_thread(capsys):
    made = []

    class FakeThread:
        def __init__(self, target, args, name, daemon):
            made.append((target, args[:2], name, daemon))
            self.started = False

        def start(self):
            self.started = True

    thread, stop = start.start_keep_alive({"RENDER_EXTERNAL_URL": "https://r.onrender.com"},
                                          thread_factory=FakeThread)
    assert thread.started and not stop.is_set()
    assert made == [(start.keep_alive_loop, ("https://r.onrender.com/_stcore/health", 540),
                     "keep-alive", True)]
    assert "every 9 minutes" in capsys.readouterr().out


def test_start_keep_alive_bad_setting_is_off_not_fatal(capsys):
    assert start.start_keep_alive({"RENDER_EXTERNAL_URL": "https://r.onrender.com",
                                   "KEEP_ALIVE_MINUTES": "60"}) is None
    assert "off" in capsys.readouterr().err


def test_main_starts_and_stops_keep_alive(monkeypatch):
    import threading

    monkeypatch.setattr(start.signal, "signal", lambda s, h: None)
    started = []

    class FakeSup:
        def __init__(self, *a, **k):
            pass

        def request_shutdown(self, *a):
            pass

        def run(self):
            return 0

    def fake_start(env):
        stop = threading.Event()
        started.append(stop)
        return object(), stop

    monkeypatch.setattr(start, "Supervisor", FakeSup)
    monkeypatch.setattr(start, "start_keep_alive", fake_start)
    assert start.main({"RENDER_EXTERNAL_URL": "https://r.onrender.com"}) == 0
    assert len(started) == 1 and started[0].is_set()
