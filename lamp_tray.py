#!/usr/bin/env python3
"""
lamp_tray.py — system tray indicator for the RGB ambient lamp

Click the icon to open a menu:
  Speed:   Live / Fast / Regular / Slow  (click to start / switch)
  Region:  Top / Bottom / Left / Right / Border / Full
  Off      — stop the lamp
  Quit     — exit the tray

The icon label shows the active mode. lamp_ambient.py is managed as a
subprocess; switching modes or regions restarts it automatically.

Logs go to /tmp/lamp_tray.log.
"""

import fcntl
import os
import signal
import subprocess
import sys

import gi
gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("Gtk", "3.0")
from gi.repository import AyatanaAppIndicator3 as AppIndicator, Gtk, GLib

SCRIPT   = os.path.join(os.path.dirname(os.path.abspath(__file__)), "lamp_ambient.py")
PYTHON   = sys.executable
LOGFILE  = "/tmp/lamp_tray.log"
PIDFILE  = "/tmp/lamp_ambient.pid"

MODES   = ["live", "fast", "regular", "slow"]
REGIONS = ["top", "bottom", "left", "right", "border", "full"]

ICON_ON  = "weather-clear-night-symbolic"
ICON_OFF = "weather-clear-symbolic"


class LampIndicator:
    def __init__(self):
        self._proc:   subprocess.Popen | None = None
        self._mode:   str | None = None
        self._region: str        = "border"

        self._ind = AppIndicator.Indicator.new(
            "rgb-lamp",
            ICON_OFF,
            AppIndicator.IndicatorCategory.HARDWARE,
        )
        self._ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._ind.set_title("RGB Lamp")

        self._mode_items:   dict[str, Gtk.CheckMenuItem] = {}
        self._region_items: dict[str, Gtk.CheckMenuItem] = {}
        self._build_menu()

    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        menu = Gtk.Menu()

        # --- Speed / mode ---
        hdr = Gtk.MenuItem(label="Speed")
        hdr.set_sensitive(False)
        menu.append(hdr)

        for name in MODES:
            item = Gtk.CheckMenuItem(label=name.capitalize())
            item.set_draw_as_radio(True)
            item.connect("activate", self._on_mode, name)
            menu.append(item)
            self._mode_items[name] = item

        menu.append(Gtk.SeparatorMenuItem())

        # --- Region ---
        hdr2 = Gtk.MenuItem(label="Sample region")
        hdr2.set_sensitive(False)
        menu.append(hdr2)

        for name in REGIONS:
            item = Gtk.CheckMenuItem(label=name.capitalize())
            item.set_draw_as_radio(True)
            if name == self._region:
                item.set_active(True)
            item.connect("activate", self._on_region, name)
            menu.append(item)
            self._region_items[name] = item

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

    def _set_mode_check(self, active_mode: str | None) -> None:
        """Update checkmarks without triggering callbacks."""
        for name, item in self._mode_items.items():
            item.handler_block_by_func(self._on_mode)
            item.set_active(name == active_mode)
            item.handler_unblock_by_func(self._on_mode)

    def _set_region_check(self, active_region: str) -> None:
        for name, item in self._region_items.items():
            item.handler_block_by_func(self._on_region)
            item.set_active(name == active_region)
            item.handler_unblock_by_func(self._on_region)

    # ------------------------------------------------------------------

    def _start(self, mode: str, region: str) -> None:
        self._stop_proc()
        log(f"starting --{mode} --region {region}")
        logf = open(LOGFILE, "a")
        self._proc = subprocess.Popen(
            [PYTHON, SCRIPT, f"--{mode}", "--region", region],
            stdout=logf,
            stderr=logf,
            start_new_session=True,
        )
        logf.close()
        # Write pidfile so future tray instances can kill orphaned processes
        with open(PIDFILE, "w") as f:
            f.write(str(self._proc.pid))
        self._mode   = mode
        self._region = region
        self._ind.set_icon_full(ICON_ON, f"Lamp: {mode} · {region}")
        self._ind.set_label(mode, mode)
        self._set_mode_check(mode)
        self._set_region_check(region)
        log(f"pid={self._proc.pid}")

    def _stop_proc(self) -> None:
        # Also kill any orphaned process from a previous tray session
        _kill_pidfile()
        if self._proc is None:
            return
        log(f"stopping pid={self._proc.pid}")
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
        _clear_pidfile()

    def _mark_off(self) -> None:
        self._ind.set_icon_full(ICON_OFF, "Lamp: off")
        self._ind.set_label("", "")
        self._set_mode_check(None)

    # ------------------------------------------------------------------

    def _on_mode(self, item: Gtk.CheckMenuItem, mode: str) -> None:
        if not item.get_active():
            # User unchecked the current mode — treat as Off
            if self._mode == mode:
                self._stop_proc()
                self._mark_off()
            return
        self._start(mode, self._region)

    def _on_region(self, item: Gtk.CheckMenuItem, region: str) -> None:
        if not item.get_active():
            return
        if self._region == region and self._proc is not None:
            return
        self._region = region
        self._set_region_check(region)
        if self._mode is not None:
            self._start(self._mode, region)

    def _on_off(self, _item) -> None:
        self._stop_proc()
        self._mark_off()

    def _on_quit(self, _item) -> None:
        self._stop_proc()
        Gtk.main_quit()

    # ------------------------------------------------------------------

    def watch_proc(self) -> bool:
        if self._proc is not None and self._proc.poll() is not None:
            log(f"process exited with code {self._proc.returncode}")
            self._proc = None
            self._mode = None
            self._ind.set_icon_full(ICON_OFF, "Lamp: off (crashed)")
            self._mark_off()
        return True


# ---------------------------------------------------------------------------

def log(msg: str) -> None:
    print(f"[tray] {msg}", flush=True)


def _kill_pidfile() -> None:
    """Kill any lamp_ambient process recorded in the pidfile."""
    try:
        with open(PIDFILE) as f:
            pid = int(f.read().strip())
        try:
            os.killpg(os.getpgid(pid), signal.SIGTERM)
            log(f"killed orphaned pid={pid}")
        except ProcessLookupError:
            pass
    except (FileNotFoundError, ValueError):
        pass


def _clear_pidfile() -> None:
    try:
        os.unlink(PIDFILE)
    except FileNotFoundError:
        pass


def _acquire_lock():
    lock_path = "/tmp/rgb-lamp-tray.lock"
    f = open(lock_path, "w")
    try:
        fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        print("lamp_tray already running — exiting")
        sys.exit(0)
    return f


def main() -> None:
    _lock = _acquire_lock()  # noqa: F841

    # Redirect our own stdout/stderr to the log file
    logf = open(LOGFILE, "a")
    sys.stdout = logf
    sys.stderr = logf

    log("started")
    _kill_pidfile()  # clean up any orphan from a previous tray session

    ind = LampIndicator()
    GLib.timeout_add(2000, ind.watch_proc)

    def _sig(signum, frame):
        ind._stop_proc()
        Gtk.main_quit()

    signal.signal(signal.SIGINT, _sig)
    signal.signal(signal.SIGTERM, _sig)

    Gtk.main()
    log("exited")


if __name__ == "__main__":
    main()
