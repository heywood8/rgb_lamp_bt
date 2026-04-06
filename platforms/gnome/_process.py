"""
platforms/gnome/_process.py — POSIX process management utilities.

Covers: single-instance locking (flock), process-tree termination (killpg),
pidfile read/write/clear, and detached subprocess spawning.

Not imported directly — use platforms.get_process_utils() instead.
"""

import fcntl
import os
import signal
import subprocess
import sys


class PosixProcessUtils:
    # ------------------------------------------------------------------
    # Single-instance lock

    def acquire_lock(self, path: str):
        """
        Acquire an exclusive non-blocking flock on `path`.
        Returns the open file handle (must stay alive for the lock to hold).
        Exits the process if another instance already holds the lock.
        """
        f = open(path, "w")
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            print(f"Another instance is already running (lock: {path}) — exiting")
            sys.exit(0)
        return f

    # ------------------------------------------------------------------
    # PID file

    def write_pid(self, path: str, pid: int) -> None:
        with open(path, "w") as f:
            f.write(str(pid))

    def read_and_kill_pid(self, path: str) -> None:
        """Kill the process group recorded in `path`, if it exists."""
        try:
            with open(path) as f:
                pid = int(f.read().strip())
            self.kill_pid(pid)
        except (FileNotFoundError, ValueError):
            pass

    def clear_pid(self, path: str) -> None:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass

    # ------------------------------------------------------------------
    # Process termination

    def kill_pid(self, pid: int) -> None:
        """Send SIGTERM to the process group of `pid`."""
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
        except ProcessLookupError:
            pass

    # ------------------------------------------------------------------
    # Subprocess spawning

    def spawn(self, cmd: list[str], logfile) -> subprocess.Popen:
        """
        Launch `cmd` as a detached process in its own session,
        with stdout/stderr redirected to `logfile` (an open file object).
        """
        return subprocess.Popen(
            cmd,
            stdout=logfile,
            stderr=logfile,
            start_new_session=True,
        )
