"""
platforms — runtime platform detection and backend factory.

Usage:
    from platforms import get_capture_backend, get_process_utils, REGIONS

`get_capture_backend(callback, region)` returns a CaptureBackend for the
current platform. `get_process_utils()` returns platform-appropriate helpers
for process management (locking, killing, spawning).
"""

import sys as _sys

# Regions are a UI concept shared across all platforms.
REGIONS = ["top", "bottom", "left", "right", "border", "full"]


def _detect() -> str:
    if _sys.platform == "win32":
        return "windows"
    if _sys.platform.startswith("linux"):
        # Require GLib/Gio — proxy for a D-Bus session (GNOME/systemd desktop)
        try:
            import gi  # noqa: F401
            gi.require_version("Gio", "2.0")
            from gi.repository import Gio  # noqa: F401
            return "gnome"
        except Exception:
            pass
    raise RuntimeError(
        f"Unsupported platform: {_sys.platform!r}. "
        "Only 'gnome' (Linux with GLib/Gio) and 'windows' are planned."
    )


_PLATFORM = _detect()


def get_capture_backend(callback, region: str = "border"):
    """
    Return a started-ready CaptureBackend for the current platform.

    The returned object has:
        .start() -> None   — begin capture (blocks until ready)
        .stop()  -> None   — release resources
    """
    if _PLATFORM == "gnome":
        from platforms.gnome._capture import GnomeCaptureBackend
        return GnomeCaptureBackend(callback, region=region)
    if _PLATFORM == "windows":
        from platforms.windows import get_capture_backend as _w
        return _w(callback, region=region)
    raise RuntimeError(f"No capture backend for platform {_PLATFORM!r}")


def get_process_utils():
    """
    Return a ProcessUtils instance for the current platform.

    Provides:
        .acquire_lock(path) -> handle   — exclusive single-instance lock
        .kill_pid(pid)      -> None     — terminate process tree
        .spawn(cmd, logfile)-> Popen    — launch detached subprocess
        .write_pid(path, pid)
        .read_and_kill_pid(path)
        .clear_pid(path)
    """
    if _PLATFORM in ("gnome", "linux"):
        from platforms.gnome._process import PosixProcessUtils
        return PosixProcessUtils()
    if _PLATFORM == "windows":
        from platforms.windows import get_process_utils as _w
        return _w()
    raise RuntimeError(f"No process utils for platform {_PLATFORM!r}")


def get_tray_class():
    """
    Return the tray application class for the current platform.

    The class is constructed with no arguments and has a `.run()` method.
    """
    if _PLATFORM == "gnome":
        from platforms.gnome._tray import GtkTray
        return GtkTray
    if _PLATFORM == "windows":
        from platforms.windows import get_tray_class as _w
        return _w()
    raise RuntimeError(f"No tray implementation for platform {_PLATFORM!r}")
