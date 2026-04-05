#!/usr/bin/env python3
"""
lamp_ambient.py — screen ambient colour → GATT-DEMO BLE lamp

Reads the average colour of your monitor via the xdg ScreenCast portal
(PipeWire + GStreamer) and drives the lamp to match it continuously.

Usage:
  python3 lamp_ambient.py

A GNOME dialog will ask you to select a monitor to share.
Ctrl+C to stop.
"""

import asyncio
import colorsys
import os
import signal
import subprocess
import sys
import threading

import gi
gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import GLib, Gio

from bleak import BleakClient

MAC  = "FF:24:03:18:45:51"
FFF3 = "0000fff3-0000-1000-8000-00805f9b34fb"

_PORTAL_BUS  = "org.freedesktop.portal.Desktop"
_PORTAL_PATH = "/org/freedesktop/portal/desktop"
_PORTAL_SC   = "org.freedesktop.portal.ScreenCast"
_PORTAL_REQ  = "org.freedesktop.portal.Request"


# ---------------------------------------------------------------------------
# BLE command helpers
# ---------------------------------------------------------------------------

def _cmd_power(on: bool) -> bytes:
    return bytes([0xbc, 0x01, 0x01, 0x01 if on else 0x00, 0x55])


def _cmd_hsv(h_deg: float, s_pct: float) -> bytes:
    h = int(round(h_deg)) & 0xFFFF
    s = int(round(s_pct * 10)) & 0xFFFF
    return bytes([0xbc, 0x04, 0x06,
                  (h >> 8) & 0xff, h & 0xff,
                  (s >> 8) & 0xff, s & 0xff,
                  0x00, 0x00, 0x55])


_CMD_WHITE = bytes([0xbc, 0x09, 0x06, 0xff, 0x00, 0x00, 0x00, 0x00, 0x00, 0x55])
_CMD_OFF   = _cmd_power(False)
_CMD_ON    = _cmd_power(True)


def _rgb_to_cmd(r: int, g: int, b: int) -> bytes:
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if v < 0.05:
        return _CMD_OFF
    if s < 0.15:
        return _CMD_WHITE
    return _cmd_hsv(h * 360, s * 100)


# ---------------------------------------------------------------------------
# xdg ScreenCast portal
# ---------------------------------------------------------------------------

def _req_path(bus: Gio.DBusConnection, token: str) -> str:
    sender = bus.get_unique_name().lstrip(":").replace(".", "_")
    return f"/org/freedesktop/portal/desktop/request/{sender}/{token}"


def _call_and_wait(
    bus: Gio.DBusConnection,
    proxy: Gio.DBusProxy,
    method: str,
    params: GLib.Variant,
    req_token: str,
) -> dict:
    """Subscribe to portal Response signal before calling method, then wait."""
    req_path = _req_path(bus, req_token)
    outcome: dict = {}
    loop = GLib.MainLoop()

    def on_response(conn, sender, path, iface, sig, variant, _):
        status, results = variant.unpack()
        outcome["status"] = status
        outcome["results"] = results
        loop.quit()

    sub = bus.signal_subscribe(
        None, _PORTAL_REQ, "Response", req_path,
        None, Gio.DBusSignalFlags.NONE, on_response, None,
    )
    proxy.call_sync(method, params, Gio.DBusCallFlags.NONE, -1, None)
    loop.run()
    bus.signal_unsubscribe(sub)

    if outcome.get("status", 1) != 0:
        raise RuntimeError(
            f"Portal '{method}' rejected (status={outcome.get('status')})"
        )
    return outcome["results"]


_TOKEN_FILE = os.path.expanduser("~/.local/share/lamp_ambient.token")


def _load_token() -> str | None:
    try:
        return open(_TOKEN_FILE).read().strip() or None
    except FileNotFoundError:
        return None


def _save_token(token: str) -> None:
    os.makedirs(os.path.dirname(_TOKEN_FILE), exist_ok=True)
    open(_TOKEN_FILE, "w").write(token)


_portal_refs: list = []   # keep bus/proxy alive for the process lifetime


def setup_screencast() -> tuple[int, int]:
    """Drive the xdg ScreenCast portal. Returns (pipewire_fd, node_id)."""
    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)
    pid = os.getpid()

    proxy = Gio.DBusProxy.new_sync(
        bus, Gio.DBusProxyFlags.NONE, None,
        _PORTAL_BUS, _PORTAL_PATH, _PORTAL_SC, None,
    )

    # 1. CreateSession
    sess_tok = f"lampsess{pid}"
    r1_tok   = f"lamp1{pid}"
    r1 = _call_and_wait(bus, proxy, "CreateSession",
        GLib.Variant("(a{sv})", ({
            "handle_token":         GLib.Variant("s", r1_tok),
            "session_handle_token": GLib.Variant("s", sess_tok),
        },)),
        r1_tok,
    )
    session = r1["session_handle"]

    # 2. SelectSources (types=1 → MONITOR)
    # persist_mode=2: remember selection until explicitly revoked.
    # restore_token: if we saved one last time, pass it back — no dialog shown.
    saved_token = _load_token()
    select_opts: dict = {
        "handle_token": GLib.Variant("s", f"lamp2{pid}"),
        "types":        GLib.Variant("u", 1),
        "multiple":     GLib.Variant("b", False),
        "persist_mode": GLib.Variant("u", 2),
    }
    if saved_token:
        print(f"[portal] using saved restore token")
        select_opts["restore_token"] = GLib.Variant("s", saved_token)
    r2_tok = f"lamp2{pid}"
    _call_and_wait(bus, proxy, "SelectSources",
        GLib.Variant("(oa{sv})", (session, select_opts)),
        r2_tok,
    )

    # 3. Start → response contains streams + new restore_token
    r3_tok = f"lamp3{pid}"
    r3 = _call_and_wait(bus, proxy, "Start",
        GLib.Variant("(osa{sv})", (session, "", {
            "handle_token": GLib.Variant("s", r3_tok),
        })),
        r3_tok,
    )
    streams = r3["streams"]
    node_id = int(streams[0][0])
    stream_props = streams[0][1] if len(streams[0]) > 1 else {}
    print(f"[portal] node_id={node_id}  props={dict(stream_props)}")

    new_token = r3.get("restore_token")
    if new_token:
        _save_token(new_token)
        print(f"[portal] restore token saved — next run needs no dialog")

    # 4. OpenPipeWireRemote → Unix FD (no Request pattern, returns directly)
    result_v, fd_list = proxy.call_with_unix_fd_list_sync(
        "OpenPipeWireRemote",
        GLib.Variant("(oa{sv})", (session, {})),
        Gio.DBusCallFlags.NONE, -1, None, None,
    )
    fd_index = result_v.unpack()[0]
    pw_fd = fd_list.get(fd_index)

    # Keep bus and proxy alive so the portal session isn't closed by GC
    _portal_refs.extend([bus, proxy])

    print(f"[portal] pw_fd={pw_fd}")
    return pw_fd, node_id


# ---------------------------------------------------------------------------
# Frame capture via gst-launch-1.0 subprocess
# ---------------------------------------------------------------------------

FRAME_W = 64
FRAME_H = 64
FRAME_BYTES = FRAME_W * FRAME_H * 3  # RGB24


def start_capture(pw_fd: int, node_id: int, color_state: dict) -> subprocess.Popen:
    """
    Spawn gst-launch-1.0 with the PipeWire fd, read raw 64×64 RGB frames from
    its stdout, compute EMA-smoothed average colour into color_state['rgb'].
    """
    pipeline = (
        f"pipewiresrc target-object={node_id} ! "
        "videoconvert ! videoscale ! "
        f"video/x-raw,format=RGB,width={FRAME_W},height={FRAME_H} ! "
        "fdsink sync=false"
    )
    print(f"[gst] {pipeline}")
    os.close(pw_fd)  # not using restricted remote; close to avoid fd leak

    proc = subprocess.Popen(
        ["gst-launch-1.0"] + pipeline.split(),
        stdout=subprocess.PIPE,
    )

    def reader():
        alpha = 0.2
        while True:
            data = proc.stdout.read(FRAME_BYTES)
            if len(data) < FRAME_BYTES:
                break
            n = FRAME_BYTES // 3
            r = sum(data[0::3]) // n
            g = sum(data[1::3]) // n
            b = sum(data[2::3]) // n
            prev = color_state.get("rgb")
            if prev:
                pr, pg, pb = prev
                r = int(alpha * r + (1 - alpha) * pr)
                g = int(alpha * g + (1 - alpha) * pg)
                b = int(alpha * b + (1 - alpha) * pb)
            color_state["rgb"] = (r, g, b)

    threading.Thread(target=reader, daemon=True).start()

    # Give gst-launch a moment to start; check it didn't exit immediately
    import time
    time.sleep(1.5)
    if proc.poll() is not None:
        raise RuntimeError(
            f"gst-launch-1.0 exited immediately (rc={proc.returncode}). "
            "Check that gstreamer1-plugin-pipewire is installed."
        )

    return proc


# ---------------------------------------------------------------------------
# BLE loop
# ---------------------------------------------------------------------------

async def ble_loop(color_state: dict, stop_event: threading.Event) -> None:
    last_cmd: bytes | None = None
    last_h: float | None = None
    last_s: float | None = None

    while not stop_event.is_set():
        try:
            print("[ble] connecting...")
            async with BleakClient(MAC) as client:
                print("[ble] connected")
                await client.write_gatt_char(FFF3, _CMD_ON, response=False)
                last_cmd = _CMD_ON

                while client.is_connected and not stop_event.is_set():
                    r, g, b = color_state.get("rgb", (128, 128, 128))
                    cmd = _rgb_to_cmd(r, g, b)

                    if cmd != last_cmd:
                        h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
                        h_deg, s_pct = h * 360, s * 100
                        changed = (
                            last_h is None
                            or cmd in (_CMD_OFF, _CMD_WHITE)
                            or abs(h_deg - last_h) > 8
                            or abs(s_pct - last_s) > 8
                        )
                        if changed:
                            await client.write_gatt_char(FFF3, cmd, response=False)
                            last_cmd = cmd
                            last_h, last_s = h_deg, s_pct

                    await asyncio.sleep(0.3)

        except Exception as exc:
            if stop_event.is_set():
                break
            print(f"[ble] {exc} — reconnecting in 5 s...")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    print("Setting up ScreenCast portal (a GNOME dialog will appear)...")
    try:
        pw_fd, node_id = setup_screencast()
    except Exception as exc:
        print(f"Portal setup failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print("Starting capture pipeline...")
    color_state: dict = {}
    try:
        proc = start_capture(pw_fd, node_id, color_state)
    except Exception as exc:
        print(f"Capture failed: {exc}", file=sys.stderr)
        sys.exit(1)

    stop_event = threading.Event()

    def on_signal(signum, frame):
        print("\n[main] stopping...")
        stop_event.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    print("[main] running — Ctrl+C to stop")
    try:
        asyncio.run(ble_loop(color_state, stop_event))
    finally:
        proc.terminate()
        proc.wait()
        print("[main] done.")


if __name__ == "__main__":
    main()
