"""Process supervisor: runs the Streamlit dashboard and the scheduler in one container.

Render gives one Docker web service a single persistent disk, so both processes
share a container. Rules:
  * dashboard exits  -> stop the scheduler, exit non-zero (Render restarts the service)
  * scheduler exits  -> keep the dashboard up, restart the scheduler with backoff
  * SIGTERM / SIGINT -> stop the scheduler first (it may be mid-send), then the dashboard

Keep-alive: Render's free plan puts a web service to sleep after 15 minutes without
inbound traffic, and a sleeping service sends nothing. When RENDER_EXTERNAL_URL (set by
Render) or KEEP_ALIVE_URL is present, a background thread requests the service's own
public health URL every KEEP_ALIVE_MINUTES (default 9), which counts as inbound traffic.
KEEP_ALIVE_MINUTES=0 turns it off.
"""
import os
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request

DEFAULT_PORT = "8501"
DEFAULT_SHUTDOWN_GRACE_SECONDS = 240
DASHBOARD_STOP_SECONDS = 10
POLL_INTERVAL_SECONDS = 1
BACKOFF_BASE_SECONDS = 10
BACKOFF_MAX_SECONDS = 300
HEALTHY_RUN_SECONDS = 120
_STOP_POLL_SECONDS = 0.5


def _positive_int(value, name, maximum=None):
    text = str(value).strip()
    if not (text.isascii() and text.isdecimal()):
        raise ValueError(f"{name} must be a positive integer")
    number = int(text)
    if number < 1 or (maximum is not None and number > maximum):
        limit = f" between 1 and {maximum}" if maximum else " greater than 0"
        raise ValueError(f"{name} must be{limit}")
    return number


def parse_port(env):
    return _positive_int(env.get("PORT", DEFAULT_PORT), "PORT", maximum=65535)


def parse_grace_seconds(env):
    return _positive_int(
        env.get("SHUTDOWN_GRACE_SECONDS", DEFAULT_SHUTDOWN_GRACE_SECONDS),
        "SHUTDOWN_GRACE_SECONDS",
    )


def build_commands(env):
    """Return (dashboard_cmd, scheduler_cmd)."""
    port = str(parse_port(env))
    dashboard = [
        sys.executable, "-m", "streamlit", "run", "dashboard.py",
        "--server.port", port,
        "--server.address", "0.0.0.0",
        "--server.headless", "true",
    ]
    scheduler = [sys.executable, "main.py"]
    return dashboard, scheduler


KEEP_ALIVE_DEFAULT_MINUTES = 9
KEEP_ALIVE_MAX_MINUTES = 14          # Render sleeps after 15 idle minutes
KEEP_ALIVE_PATH = "/_stcore/health"  # Streamlit's health endpoint, also Render's check
KEEP_ALIVE_TIMEOUT_SECONDS = 30


def keep_alive_settings(env):
    """Return (url, interval_seconds) for the self-ping, or None when it is off.

    Raises ValueError for a bad KEEP_ALIVE_MINUTES or URL.
    """
    raw = str(env.get("KEEP_ALIVE_MINUTES", "")).strip()
    if raw == "0":
        return None
    base = (env.get("KEEP_ALIVE_URL") or env.get("RENDER_EXTERNAL_URL") or "").strip()
    if not base:
        return None
    minutes = (_positive_int(raw, "KEEP_ALIVE_MINUTES", maximum=KEEP_ALIVE_MAX_MINUTES)
               if raw else KEEP_ALIVE_DEFAULT_MINUTES)
    parts = urllib.parse.urlsplit(base)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        raise ValueError("KEEP_ALIVE_URL / RENDER_EXTERNAL_URL must start with https://")
    return base.rstrip("/") + KEEP_ALIVE_PATH, minutes * 60


def ping_once(url, opener=urllib.request.urlopen, timeout=KEEP_ALIVE_TIMEOUT_SECONDS):
    """Request `url` once. Returns (ok, detail); never raises."""
    try:
        with opener(url, timeout=timeout) as response:
            status = getattr(response, "status", 200)
            return 200 <= status < 400, f"HTTP {status}"
    except Exception as exc:  # noqa: BLE001 - a failed ping must never stop the service
        return False, type(exc).__name__


def keep_alive_loop(url, interval, stop, ping=ping_once, log=None):
    """Ping `url` every `interval` seconds until `stop` (a threading.Event) is set."""
    log = log or (lambda msg: print(f"[keep-alive] {msg}", flush=True))
    failures = 0
    while not stop.wait(interval):
        ok, detail = ping(url)
        if ok:
            if failures:
                log(f"ping works again ({detail})")
            failures = 0
        else:
            failures += 1
            log(f"ping failed ({detail}); trying again in {interval // 60} minutes")


def start_keep_alive(env, thread_factory=threading.Thread):
    """Start the self-ping thread if configured. Returns (thread, stop_event) or None."""
    try:
        settings = keep_alive_settings(env)
    except ValueError as exc:
        print(f"[keep-alive] off: {exc}", file=sys.stderr, flush=True)
        return None
    if settings is None:
        return None
    url, interval = settings
    stop = threading.Event()
    thread = thread_factory(target=keep_alive_loop, args=(url, interval, stop),
                            name="keep-alive", daemon=True)
    thread.start()
    print(f"[keep-alive] pinging {url} every {interval // 60} minutes", flush=True)
    return thread, stop


def backoff_delay(failures):
    """Seconds to wait before restart number `failures` (0-based)."""
    return min(BACKOFF_BASE_SECONDS * 2 ** min(failures, 16), BACKOFF_MAX_SECONDS)


class Supervisor:
    def __init__(self, dashboard_cmd, scheduler_cmd, *,
                 grace_seconds=DEFAULT_SHUTDOWN_GRACE_SECONDS,
                 popen=subprocess.Popen, sleep=time.sleep, monotonic=time.monotonic,
                 log=None):
        self.dashboard_cmd = dashboard_cmd
        self.scheduler_cmd = scheduler_cmd
        self.grace_seconds = grace_seconds
        self._popen = popen
        self._sleep = sleep
        self._monotonic = monotonic
        self._log = log or (lambda msg: print(f"[supervisor] {msg}", flush=True))
        self.dashboard = None
        self.scheduler = None
        self._stop_requested = False
        self._failures = 0
        self._scheduler_started_at = None
        self._restart_at = None

    def request_shutdown(self, *_args):
        """Safe to call from a signal handler: only sets a flag."""
        self._stop_requested = True

    def run(self):
        """Run until shutdown or dashboard exit. Returns the process exit code."""
        code = 1
        try:
            self.dashboard = self._popen(self.dashboard_cmd)
            self._start_scheduler()
            while True:
                if self._stop_requested:
                    self._log("shutdown requested")
                    code = 0
                    break
                dash_code = self.dashboard.poll()
                if dash_code is not None:
                    code = dash_code if dash_code != 0 else 1
                    self._log(f"dashboard exited with code {dash_code}; shutting down")
                    break
                self._check_scheduler()
                self._sleep(POLL_INTERVAL_SECONDS)
        finally:
            self._shutdown()
        return code

    def _start_scheduler(self):
        self.scheduler = self._popen(self.scheduler_cmd)
        self._scheduler_started_at = self._monotonic()
        self._restart_at = None

    def _check_scheduler(self):
        now = self._monotonic()
        if self.scheduler is None:
            if now >= self._restart_at:
                try:
                    self._start_scheduler()
                except OSError as exc:
                    self._schedule_restart(now, f"spawn failed: {exc.__class__.__name__}",
                                           ran_for=0)
            return
        code = self.scheduler.poll()
        if code is None:
            return
        self.scheduler = None
        self._schedule_restart(now, f"exit code {code}",
                               ran_for=now - self._scheduler_started_at)

    def _schedule_restart(self, now, reason, ran_for):
        if ran_for >= HEALTHY_RUN_SECONDS:
            self._failures = 0
        delay = backoff_delay(self._failures)
        self._failures += 1
        self._restart_at = now + delay
        self._log(f"scheduler stopped ({reason}) after {ran_for:.0f}s; "
                  f"restarting in {delay}s")

    # --- shutdown -------------------------------------------------------

    def _shutdown(self):
        """Scheduler first (may be mid-send), then dashboard. Never leaves children."""
        try:
            self._stop_child(self.scheduler, "scheduler", self.grace_seconds)
        finally:
            self._stop_child(self.dashboard, "dashboard", DASHBOARD_STOP_SECONDS)

    def _stop_child(self, proc, name, timeout):
        if proc is None or proc.poll() is not None:
            return
        self._log(f"stopping {name} (up to {timeout}s)")
        try:
            proc.send_signal(signal.SIGTERM)
        except OSError:
            pass  # already gone
        deadline = self._monotonic() + timeout
        while proc.poll() is None and self._monotonic() < deadline:
            self._sleep(_STOP_POLL_SECONDS)
        if proc.poll() is None:
            self._log(f"{name} did not exit in time; killing")
            proc.kill()
            proc.wait()


def main(env=None):
    env = os.environ if env is None else env
    try:
        dashboard_cmd, scheduler_cmd = build_commands(env)
        grace = parse_grace_seconds(env)
    except ValueError as exc:
        print(f"[supervisor] invalid configuration: {exc}", file=sys.stderr, flush=True)
        return 2
    supervisor = Supervisor(dashboard_cmd, scheduler_cmd, grace_seconds=grace)
    signal.signal(signal.SIGTERM, supervisor.request_shutdown)
    signal.signal(signal.SIGINT, supervisor.request_shutdown)
    print(f"[supervisor] starting ReelDrop dashboard on port {parse_port(env)}", flush=True)
    keep_alive = start_keep_alive(env)
    try:
        return supervisor.run()
    finally:
        if keep_alive:
            keep_alive[1].set()


if __name__ == "__main__":
    sys.exit(main())
