#!/usr/bin/env python3
"""
lamp_tray.py — system tray indicator for the RGB ambient lamp

Left-click or right-click the icon to open a menu:
  Off / Live / Fast / Regular / Slow

The icon label shows the active mode. The lamp_ambient.py script
is managed as a subprocess; switching modes kills the old process
and starts a new one.
"""

import os
import signal
import subprocess
import sys

import gi
gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("Gtk", "3.0")
from gi.repository import AyatanaAppIndicator3 as AppIndicator, Gtk, GLib

SCRIPT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lamp_ambient.py")
PYTHON = sys.executable

MODES = ["live", "fast", "regular", "slow"]

# Icon names (from the system's icon theme — all commonly available)
ICON_ON  = "weather-clear-night-symbolic"   # glowing moon ≈ ambient light
ICON_OFF = "weather-clear-symbolic"


class LampIndicator:
    def __init__(self):
        self._proc: subprocess.Popen | None = None
        self._mode: str | None = None

        self._ind = AppIndicator.Indicator.new(
            "rgb-lamp",
            ICON_OFF,
            AppIndicator.IndicatorCategory.HARDWARE,
        )
        self._ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._ind.set_title("RGB Lamp")

        self._build_menu()

    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        menu = Gtk.Menu()

        # Mode items (radio-style)
        self._mode_items: dict[str, Gtk.CheckMenuItem] = {}
        group = []
        for name in MODES:
            item = Gtk.RadioMenuItem.new_with_label(group, name.capitalize())
            group = item.get_group()
            item.connect("activate", self._on_mode, name)
            menu.append(item)
            self._mode_items[name] = item

        menu.append(Gtk.SeparatorMenuItem())

        off_item = Gtk.MenuItem(label="Off")
        off_item.connect("activate", self._on_off)
        menu.append(off_item)

        menu.append(Gtk.SeparatorMenuItem())

        quit_item = Gtk.MenuItem(label="Quit")
        quit_item.connect("activate", self._on_quit)
        menu.append(quit_item)

        menu.show_all()
        self._ind.set_menu(menu)

    # ------------------------------------------------------------------

    def _start(self, mode: str) -> None:
        self._stop()
        print(f"[tray] starting --{mode}")
        self._proc = subprocess.Popen(
            [PYTHON, SCRIPT, f"--{mode}"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )
        self._mode = mode
        self._ind.set_icon_full(ICON_ON, f"Lamp: {mode}")
        self._ind.set_label(mode, mode)
        print(f"[tray] pid={self._proc.pid}")

    def _stop(self) -> None:
        if self._proc is None:
            return
        print(f"[tray] stopping pid={self._proc.pid}")
        try:
            os.killpg(os.getpgid(self._proc.pid), signal.SIGTERM)
        except ProcessLookupError:
            pass
        try:
            self._proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            self._proc.kill()
        self._proc = None
        self._mode = None
        self._ind.set_icon_full(ICON_OFF, "Lamp: off")
        self._ind.set_label("", "")

    # ------------------------------------------------------------------

    def _on_mode(self, item: Gtk.RadioMenuItem, mode: str) -> None:
        # RadioMenuItem fires activate for both the newly selected AND the
        # previously selected item (deselection). Only act on selection.
        if not item.get_active():
            return
        if self._mode == mode:
            return
        self._start(mode)

    def _on_off(self, _item) -> None:
        self._stop()
        # Deselect all radio items without triggering _on_mode
        for item in self._mode_items.values():
            item.handler_block_by_func(self._on_mode)
            item.set_active(False)
            item.handler_unblock_by_func(self._on_mode)

    def _on_quit(self, _item) -> None:
        self._stop()
        Gtk.main_quit()

    # ------------------------------------------------------------------

    def _watch_proc(self) -> bool:
        """GLib idle callback: detect if the subprocess died unexpectedly."""
        if self._proc is not None and self._proc.poll() is not None:
            print(f"[tray] process exited with code {self._proc.returncode}")
            self._proc = None
            self._mode = None
            self._ind.set_icon_full(ICON_OFF, "Lamp: off (stopped)")
            self._ind.set_label("", "")
            for item in self._mode_items.values():
                item.handler_block_by_func(self._on_mode)
                item.set_active(False)
                item.handler_unblock_by_func(self._on_mode)
        return True  # keep calling


def main() -> None:
    ind = LampIndicator()
    GLib.timeout_add(2000, ind._watch_proc)

    # Clean up subprocess on SIGINT/SIGTERM
    def _sig(signum, frame):
        ind._stop()
        Gtk.main_quit()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    Gtk.main()


if __name__ == "__main__":
    main()
