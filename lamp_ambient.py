#!/usr/bin/env python3
"""
lamp_ambient.py — screen ambient colour → GATT-DEMO BLE lamp

Usage:
  python3 lamp_ambient.py [--live | --fast | --regular | --slow]

Modes:
  --live     Most responsive. Tracks every frame change instantly.
  --fast     Fast transitions, small dead zone.
  --regular  Moderate smoothing and dead zone (default).
  --slow     For desk work: slow blending, large dead zone, 1 s updates.

Ctrl+C to stop.
"""

import argparse
import asyncio
import colorsys
import os
import signal
import sys
import threading

import gi
gi.require_version("GLib", "2.0")
gi.require_version("Gio", "2.0")
from gi.repository import GLib, Gio

from bleak import BleakClient
from pw_capture import PwCapture, REGIONS

MAC  = "FF:24:03:18:45:51"
FFF3 = "0000fff3-0000-1000-8000-00805f9b34fb"

_SC      = "org.gnome.Mutter.ScreenCast"
_SC_PATH = "/org/gnome/Mutter/ScreenCast"

# Mode presets — all modes use fast BLE writes (0.05 s) and no dead zone
# so transitions are always smooth. Only alpha differs: it controls how
# strongly each new captured frame pulls the running colour average.
# Higher alpha = reacts faster to screen changes.
MODES = {
    "live":    dict(alpha=0.80, ble_sleep=0.05, dead_zone=0),
    "fast":    dict(alpha=0.50, ble_sleep=0.05, dead_zone=0),
    "regular": dict(alpha=0.20, ble_sleep=0.05, dead_zone=0),
    "slow":    dict(alpha=0.05, ble_sleep=0.05, dead_zone=0),
}


# ---------------------------------------------------------------------------
# BLE helpers
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


def _rgb_to_hsv_cmd(r: int, g: int, b: int):
    """Return (cmd_bytes, h_deg, s_pct) for the given RGB."""
    h, s, v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    if v < 0.05:
        return _CMD_OFF, 0.0, 0.0
    if s < 0.15:
        return _CMD_WHITE, 0.0, 0.0
    h_deg = h * 360
    s_pct = s * 100
    return _cmd_hsv(h_deg, s_pct), h_deg, s_pct


# ---------------------------------------------------------------------------
# Mutter ScreenCast portal — no dialog, GNOME-internal API
# ---------------------------------------------------------------------------

def get_mutter_node(bus: Gio.DBusConnection) -> int:
    """
    Create a Mutter ScreenCast session, record the primary monitor, start it,
    and return the PipeWire node_id from the PipeWireStreamAdded signal.
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

    loop = GLib.MainLoop()
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

    def on_stream_added(conn, sender, path, iface, sig, params, _):
        node_id = params.unpack()[0]
        print(f"[mutter] PipeWireStreamAdded: node_id={node_id}")
        result["node_id"] = node_id
        loop.quit()

    bus.signal_subscribe(
        _SC, _SC + ".Stream", "PipeWireStreamAdded",
        stream_path, None, Gio.DBusSignalFlags.NONE,
        on_stream_added, None,
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


# ---------------------------------------------------------------------------
# BLE loop
# ---------------------------------------------------------------------------

async def ble_loop(color_state: dict, stop_event: threading.Event,
                   ble_sleep: float, dead_zone: float) -> None:
    last_cmd: bytes | None = None
    last_h: float = -999.0
    last_s: float = -999.0
    writes = 0

    while not stop_event.is_set():
        try:
            print("[ble] connecting...")
            async with BleakClient(MAC) as client:
                print("[ble] connected")
                await client.write_gatt_char(FFF3, _CMD_ON, response=False)
                last_cmd = _CMD_ON
                writes += 1
                print(f"[ble] power-on sent (total writes: {writes})")

                while client.is_connected and not stop_event.is_set():
                    r, g, b = color_state.get("rgb", (128, 128, 128))
                    cmd, h_deg, s_pct = _rgb_to_hsv_cmd(r, g, b)

                    if dead_zone > 0 and last_cmd not in (None, _CMD_ON):
                        h_diff = abs(h_deg - last_h)
                        # wrap-around hue distance
                        h_diff = min(h_diff, 360 - h_diff)
                        s_diff = abs(s_pct - last_s)
                        skip = (h_diff < dead_zone and s_diff < dead_zone
                                and cmd == last_cmd)
                    else:
                        skip = (cmd == last_cmd)

                    if not skip:
                        await client.write_gatt_char(FFF3, cmd, response=False)
                        writes += 1
                        print(f"[ble] write #{writes} h={h_deg:.1f}° s={s_pct:.1f}%"
                              f"  rgb=({r},{g},{b})")
                        last_cmd = cmd
                        last_h = h_deg
                        last_s = s_pct

                    await asyncio.sleep(ble_sleep)

        except Exception as exc:
            if stop_event.is_set():
                break
            print(f"[ble] {exc} — reconnecting in 5 s...")
            await asyncio.sleep(5)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(
        description="Screen ambient colour → BLE lamp",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="\n".join(
            f"  --{m:8s}  alpha={v['alpha']:.2f}  poll={v['ble_sleep']:.2f}s"
            f"  dead_zone={v['dead_zone']}°"
            for m, v in MODES.items()
        ),
    )
    group = parser.add_mutually_exclusive_group()
    for mode_name in MODES:
        group.add_argument(f"--{mode_name}", action="store_true",
                           help=f"{mode_name} mode")
    parser.add_argument(
        "--region",
        choices=REGIONS,
        default="border",
        metavar="REGION",
        help="screen area to sample: " + ", ".join(REGIONS) + " (default: border)",
    )
    args = parser.parse_args()

    # Determine active mode
    active = "regular"
    for mode_name in MODES:
        if getattr(args, mode_name):
            active = mode_name
            break

    cfg = MODES[active]
    print(f"[main] mode={active}  alpha={cfg['alpha']}  "
          f"ble_sleep={cfg['ble_sleep']}s  dead_zone={cfg['dead_zone']}°  "
          f"region={args.region}")

    bus = Gio.bus_get_sync(Gio.BusType.SESSION, None)

    print("[mutter] setting up screen cast...")
    try:
        node_id = get_mutter_node(bus)
    except Exception as exc:
        print(f"ScreenCast setup failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"[mutter] node_id={node_id} — capture starting")

    color_state: dict = {}
    alpha = cfg["alpha"]
    frame_count = 0

    def on_frame(r: int, g: int, b: int) -> None:
        nonlocal frame_count
        prev = color_state.get("rgb")
        if prev:
            pr, pg, pb = prev
            r = int(alpha * r + (1 - alpha) * pr)
            g = int(alpha * g + (1 - alpha) * pg)
            b = int(alpha * b + (1 - alpha) * pb)
        color_state["rgb"] = (r, g, b)
        frame_count += 1
        if frame_count % 30 == 0:
            print(f"[pw] frame #{frame_count}  rgb=({r},{g},{b})")

    cap = PwCapture(node_id, on_frame, region=args.region)
    try:
        cap.start()
    except Exception as exc:
        print(f"PipeWire capture failed: {exc}", file=sys.stderr)
        sys.exit(1)

    print("[pw] capture started")

    stop_event = threading.Event()

    def on_signal(signum, frame):
        print("\n[main] stopping...")
        stop_event.set()

    signal.signal(signal.SIGINT, on_signal)
    signal.signal(signal.SIGTERM, on_signal)

    print("[main] running — Ctrl+C to stop")
    try:
        asyncio.run(ble_loop(color_state, stop_event,
                             ble_sleep=cfg["ble_sleep"],
                             dead_zone=cfg["dead_zone"]))
    finally:
        cap.stop()
        print(f"[main] done. total frames captured: {frame_count}")


if __name__ == "__main__":
    main()
