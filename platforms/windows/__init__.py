"""
platforms/windows — Windows backend stubs.

Not yet implemented. Each function raises NotImplementedError with a note
on what the Windows equivalent would look like.
"""


def get_capture_backend(callback, region="border"):
    raise NotImplementedError(
        "Windows screen capture not yet implemented. "
        "Planned: dxcam or PIL.ImageGrab for frame capture, "
        "replacing the GNOME Mutter ScreenCast + PipeWire stack."
    )


def get_process_utils():
    raise NotImplementedError(
        "Windows process utilities not yet implemented. "
        "Planned: msvcrt.locking for single-instance lock, "
        "os.kill(pid, signal.SIGTERM) for process termination "
        "(no process groups on Windows)."
    )


def get_tray_class():
    raise NotImplementedError(
        "Windows tray not yet implemented. "
        "Planned: pystray with a Pillow-generated icon image."
    )
