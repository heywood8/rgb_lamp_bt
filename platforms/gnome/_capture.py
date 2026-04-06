"""
platforms/gnome/_capture.py — GNOME screen-capture backend.

Orchestrates:
  1. Mutter ScreenCast D-Bus API  → PipeWire node_id
  2. PwCapture (ctypes PipeWire)  → per-frame RGB callback

Not imported directly — use platforms.get_capture_backend() instead.
"""

import sys

import gi
gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import GLib, Gio

from platforms.gnome._pw_stream import PwCapture

_SC      = "org.gnome.Mutter.ScreenCast"
_SC_PATH = "/org/gnome/Mutter/ScreenCast"


def _get_mutter_node(bus: Gio.DBusConnection) -> int:
    """
    Open a Mutter ScreenCast session for the primary monitor and return the
    PipeWire node_id emitted by PipeWireStreamAdded.
    """
    try:
        r = bus.call_sync(
            "org.gnome.Mutter.DisplayConfig",
            "/org/gnome/Mutter/DisplayConfig",
            "org.gnome.Mutter.DisplayConfig", "GetCurrentState",
            None, None, Gio.DBusCallFlags.NONE, -1, None,
        )
        _serial, monitors, _lgroups, _props = r.unpack()
        connector = monitors[0][0][0]
    except Exception as e:
        print(f"[mutter] DisplayConfig error: {e} — trying HDMI-1")
        connector = "HDMI-1"

    print(f"[mutter] using connector: {connector}")

    loop   = GLib.MainLoop()
    result: dict = {}

    r = bus.call_sync(
        _SC, _SC_PATH, _SC, "CreateSession",
        GLib.Variant("(a{sv})", ({},)),
        GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, -1, None,
    )
    session = r.unpack()[0]
    print(f"[mutter] session: {session}")

    r = bus.call_sync(
        _SC, session, _SC + ".Session", "RecordMonitor",
        GLib.Variant("(sa{sv})", (connector, {})),
        GLib.VariantType("(o)"), Gio.DBusCallFlags.NONE, -1, None,
    )
    stream_path = r.unpack()[0]
    print(f"[mutter] stream path: {stream_path}")

    def _on_stream_added(conn, sender, path, iface, sig, params, _):
        node_id = params.unpack()[0]
        print(f"[mutter] PipeWireStreamAdded: node_id={node_id}")
        result["node_id"] = node_id
        loop.quit()

    bus.signal_subscribe(
        _SC, _SC + ".Stream", "PipeWireStreamAdded",
        stream_path, None, Gio.DBusSignalFlags.NONE,
        _on_stream_added, None,
    )

    bus.call_sync(
        _SC, session, _SC + ".Session", "Start",
        None, None, Gio.DBusCallFlags.NONE, -1, None,
    )
    print("[mutter] Start called, waiting for PipeWireStreamAdded signal...")

    GLib.timeout_add(5000, loop.quit)
    loop.run()

    if "node_id" not in result:
        raise RuntimeError("PipeWireStreamAdded signal not received within 5 s")

    return result["node_id"]


class GnomeCaptureBackend:
    """
    Screen-capture backend for GNOME/Wayland via Mutter ScreenCast + PipeWire.

    Usage:
        cap = GnomeCaptureBackend(callback, region="border")
        cap.start()   # blocks until PipeWire stream is streaming
        # ... callback(r, g, b) is called for each frame ...
        cap.stop()
    """

    def __init__(self, callback, region: str = "border"):
        self._callback = callback
        self._region   = region
        self._bus      = None
        self._cap      = None

    def start(self) -> None:
        self._bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

        print("[mutter] setting up screen cast...")
        try:
            node_id = _get_mutter_node(self._bus)
        except Exception as exc:
            print(f"ScreenCast setup failed: {exc}", file=sys.stderr)
            raise

        print(f"[mutter] node_id={node_id} — capture starting")

        self._cap = PwCapture(node_id, self._callback, region=self._region)
        try:
            self._cap.start()
        except Exception as exc:
            print(f"PipeWire capture failed: {exc}", file=sys.stderr)
            raise

        print("[pw] capture started")

    def stop(self) -> None:
        if self._cap is not None:
            self._cap.stop()
            self._cap = None
