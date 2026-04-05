#!/usr/bin/env python3
"""
GATT-DEMO RGB Lamp controller (MAC: FF:24:03:18:45:51)

Usage:
  python3 lamp.py on
  python3 lamp.py off
  python3 lamp.py red|green|blue|yellow|cyan|magenta|orange|purple|pink|white
  python3 lamp.py cycle
  python3 lamp.py rgb 255 0 128
  python3 lamp.py hsv 120 100
  python3 lamp.py mode 1|2|3|4
"""
import asyncio
import colorsys
import sys
from bleak import BleakScanner, BleakClient

MAC  = "FF:24:03:18:45:51"
FFF3 = "0000fff3-0000-1000-8000-00805f9b34fb"
FFF4 = "0000fff4-0000-1000-8000-00805f9b34fb"


def cmd_power(on: bool) -> bytes:
    return bytes([0xbc, 0x01, 0x01, 0x01 if on else 0x00, 0x55])


def cmd_color_hsv(h_deg: float, s_pct: float = 100) -> bytes:
    """h_deg: 0-360, s_pct: 0-100  →  bc 04 06 H_hi H_lo S_hi S_lo 00 00 55"""
    h = int(round(h_deg)) & 0xFFFF
    s = int(round(s_pct * 10)) & 0xFFFF  # 100% → 1000
    return bytes([0xbc, 0x04, 0x06,
                  (h >> 8) & 0xff, h & 0xff,
                  (s >> 8) & 0xff, s & 0xff,
                  0x00, 0x00, 0x55])


def cmd_color_rgb(r: int, g: int, b: int) -> bytes:
    h, s, _v = colorsys.rgb_to_hsv(r / 255, g / 255, b / 255)
    return cmd_color_hsv(h * 360, s * 100)


def cmd_cycle() -> bytes:
    return bytes([0xbc, 0x0c, 0x01, 0x01, 0x55])


def cmd_mode(n: int) -> bytes:
    return bytes([0xbc, 0x11, 0x01, max(1, min(4, n)), 0x55])


NAMED = {
    "red":     cmd_color_rgb(255, 0,   0),
    "green":   cmd_color_rgb(0,   255, 0),
    "blue":    cmd_color_rgb(0,   0,   255),
    "yellow":  cmd_color_rgb(255, 255, 0),
    "cyan":    cmd_color_rgb(0,   255, 255),
    "magenta": cmd_color_rgb(255, 0,   255),
    "orange":  cmd_color_hsv(20, 100),
    "purple":  cmd_color_rgb(128, 0,   255),
    "pink":    cmd_color_rgb(255, 105, 180),
    "white":   bytes([0xbc, 0x09, 0x06, 0xff, 0x00, 0x00, 0x00, 0x00, 0x00, 0x55]),
}


def parse_commands(args):
    if not args:
        print(__doc__)
        sys.exit(0)
    verb = args[0].lower()
    if verb == "on":
        return [cmd_power(True)]
    if verb == "off":
        return [cmd_power(False)]
    if verb == "cycle":
        return [cmd_power(True), cmd_cycle()]
    if verb == "mode" and len(args) > 1:
        return [cmd_power(True), cmd_mode(int(args[1]))]
    if verb == "rgb" and len(args) == 4:
        return [cmd_power(True), cmd_color_rgb(int(args[1]), int(args[2]), int(args[3]))]
    if verb == "hsv" and len(args) >= 2:
        s = float(args[2]) if len(args) > 2 else 100
        return [cmd_power(True), cmd_color_hsv(float(args[1]), s)]
    if verb in NAMED:
        return [cmd_power(True), NAMED[verb]]
    print(f"Unknown command: {verb}")
    print(__doc__)
    sys.exit(1)


async def run(commands):
    print("Scanning...")
    await BleakScanner.discover(timeout=5)
    async with BleakClient(MAC) as client:
        print("Connected!")
        await client.start_notify(FFF4, lambda h, d: print(f"  << {d.hex()}"))
        await asyncio.sleep(0.3)
        for cmd in commands:
            print(f"  >> {cmd.hex()}")
            await client.write_gatt_char(FFF3, cmd, response=False)
            await asyncio.sleep(0.3)
        await asyncio.sleep(0.5)
        print("Done.")


if __name__ == "__main__":
    asyncio.run(run(parse_commands(sys.argv[1:])))
