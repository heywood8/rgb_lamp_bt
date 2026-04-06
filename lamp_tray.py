#!/usr/bin/env python3
"""
lamp_tray.py — system tray indicator for the RGB ambient lamp.

Platform-agnostic entry point. Detects the current platform and delegates
to the appropriate tray implementation in platforms/.

Logs go to /tmp/lamp_tray.log.
"""

from platforms import get_tray_class

if __name__ == "__main__":
    tray = get_tray_class()()
    tray.run()
