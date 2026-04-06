"""
platforms/macos — macOS backend stubs.

Not yet implemented. Each function raises NotImplementedError with a note
on what the macOS equivalent would look like.

Notes on planned implementations:

SCREEN CAPTURE
  macOS 12.3+: ScreenCaptureKit via PyObjC (SCStreamOutput delegate).
  Older / simpler: Quartz CGWindowListCreateImage, or mss (uses Quartz
  internally and works on macOS without changes to the capture interface).
  No PipeWire or D-Bus involved.

PROCESS UTILITIES
  macOS is POSIX — fcntl.flock, os.killpg, and os.getpgid all work.
  PosixProcessUtils from platforms/gnome/_process.py can be reused
  directly; no macOS-specific implementation needed.

TRAY
  Option A: `rumps` — minimal Pythonic macOS menu-bar app library.
  Option B: PyObjC NSStatusBar + NSMenu for full native control.
  Option C: `pystray` — cross-platform, supports macOS out of the box
             but with less native feel.
"""


def get_capture_backend(callback, region="border"):
    raise NotImplementedError(
        "macOS screen capture not yet implemented. "
        "Planned: mss (Quartz-based, drop-in for the capture interface) "
        "or ScreenCaptureKit via PyObjC for a native no-permission-dialog path."
    )


def get_process_utils():
    # macOS is POSIX — reuse the POSIX implementation directly.
    from platforms.gnome._process import PosixProcessUtils
    return PosixProcessUtils()


def get_tray_class():
    raise NotImplementedError(
        "macOS tray not yet implemented. "
        "Planned: rumps (lightweight menu-bar library) or pystray."
    )
