"""Process supervisor: runs the Streamlit dashboard and the scheduler in one container.

Render gives one Docker web service a single persistent disk, so both processes
share a container. Rules:
  * dashboard exits  -> stop the scheduler, exit non-zero (Render restarts the service)
  * scheduler exits  -> keep the dashboard up, restart the scheduler with backoff
  * SIGTERM / SIGINT -> stop the scheduler first (it may be mid-send), then the dashboard
"""
import os
import signal
import subprocess
import sys
import time

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
    return supervisor.run()


if __name__ == "__main__":
    sys.exit(main())
