#!/usr/bin/env python3
"""Cycles cyan → magenta → yellow every 2 seconds for 2 minutes."""
import asyncio
from bleak import BleakScanner, BleakClient
import sys
sys.path.insert(0, '/var/home/heywood8/scripts')
from lamp import cmd_power, cmd_color_rgb, FFF3, FFF4, MAC

COLORS = [
    ("cyan",    cmd_color_rgb(0,   255, 255)),
    ("magenta", cmd_color_rgb(255, 0,   255)),
    ("yellow",  cmd_color_rgb(255, 255, 0)),
]

async def main():
    print("Scanning...")
    await BleakScanner.discover(timeout=5)
    async with BleakClient(MAC) as client:
        print("Connected! Running for 2 minutes...")
        await client.start_notify(FFF4, lambda h, d: None)
        await client.write_gatt_char(FFF3, cmd_power(True), response=False)
        await asyncio.sleep(0.3)

        end = asyncio.get_event_loop().time() + 120
        i = 0
        while asyncio.get_event_loop().time() < end:
            name, cmd = COLORS[i % len(COLORS)]
            print(f"  {name}")
            await client.write_gatt_char(FFF3, cmd, response=False)
            await asyncio.sleep(2)
            i += 1

        print("Done.")

if __name__ == "__main__":
    asyncio.run(main())
