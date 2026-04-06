#!/usr/bin/env python3
"""
lamp_ambient.py — screen ambient colour → GATT-DEMO BLE lamp

Usage:
  python3 lamp_ambient.py [--live | --fast | --regular | --slow] [--region REGION]

Modes (all use 50 ms BLE writes; only alpha differs):
  --live     alpha=0.80 — tracks screen changes almost instantly
  --fast     alpha=0.50 — fast transitions
  --regular  alpha=0.20 — moderate lag (default)
  --slow     alpha=0.05 — very slow drift, good for desk work

Regions:
  top | bottom | left | right | border (default) | full

Ctrl+C to stop.
"""

import argparse
import asyncio
import colorsys
import os
import signal
import sys
import threading

import time

from bleak import BleakClient
from platforms import get_capture_backend, REGIONS

MAC              = "FF:24:03:18:45:51"
FFF3             = "0000fff3-0000-1000-8000-00805f9b34fb"
STATUS_FILE      = "/tmp/lamp_ambient.status"
CAPTURE_TIMEOUT  = 15   # seconds without a frame before treating capture as dead

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
    if s < 0.15:
        return _CMD_WHITE, 0.0, 0.0
    h_deg = h * 360
    s_pct = s * 100
    return _cmd_hsv(h_deg, s_pct), h_deg, s_pct


# ---------------------------------------------------------------------------
# Status file (read by tray to update icon)
# ---------------------------------------------------------------------------

def _write_status(s: str) -> None:
    try:
        with open(STATUS_FILE, "w") as f:
            f.write(s)
    except OSError:
        pass


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
            _write_status("connecting")
            async with BleakClient(MAC) as client:
                print("[ble] connected")
                _write_status("connected")
                await client.write_gatt_char(FFF3, _CMD_ON, response=False)
                last_cmd = _CMD_ON
                writes += 1
                print(f"[ble] power-on sent (total writes: {writes})")

                while client.is_connected and not stop_event.is_set():
                    if time.monotonic() - last_frame_time[0] > CAPTURE_TIMEOUT:
                        print(f"[main] no capture frame for {CAPTURE_TIMEOUT} s "
                              f"— capture died, exiting for restart")
                        stop_event.set()
                        break

                    r, g, b = color_state.get("rgb", (128, 128, 128))
                    cmd, h_deg, s_pct = _rgb_to_hsv_cmd(r, g, b)

                    if dead_zone > 0 and last_cmd not in (None, _CMD_ON):
                        h_diff = min(abs(h_deg - last_h), 360 - abs(h_deg - last_h))
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
            f"  --{m:8s}  alpha={v['alpha']:.2f}"
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

    active = "regular"
    for mode_name in MODES:
        if getattr(args, mode_name):
            active = mode_name
            break

    cfg = MODES[active]
    print(f"[main] mode={active}  alpha={cfg['alpha']}  "
          f"ble_sleep={cfg['ble_sleep']}s  dead_zone={cfg['dead_zone']}°  "
          f"region={args.region}")

    color_state: dict = {}
    alpha       = cfg["alpha"]
    frame_count = 0
    last_frame_time: list = [time.monotonic()]  # list so closure can mutate it

    def on_frame(r: int, g: int, b: int) -> None:
        nonlocal frame_count
        last_frame_time[0] = time.monotonic()
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

    cap = get_capture_backend(on_frame, region=args.region)
    try:
        cap.start()
    except Exception as exc:
        print(f"Capture start failed: {exc}", file=sys.stderr)
        sys.exit(1)

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
        _write_status("off")
        cap.stop()
        print(f"[main] done. total frames captured: {frame_count}")


if __name__ == "__main__":
    main()
