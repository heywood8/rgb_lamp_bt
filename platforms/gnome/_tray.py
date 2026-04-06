"""
platforms/gnome/_tray.py — GTK + AyatanaAppIndicator3 system tray.

GNOME/Linux-specific. Manages lamp_ambient.py as a subprocess and exposes
a status-bar icon with mode and region menus.

Not imported directly — use platforms.get_tray_class() instead.
"""

import os
import signal
import sys

import gi
gi.require_version("AyatanaAppIndicator3", "0.1")
gi.require_version("Gtk", "3.0")
from gi.repository import AyatanaAppIndicator3 as AppIndicator, Gtk, GLib

from platforms import get_process_utils, REGIONS

SCRIPT     = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "lamp_ambient.py"
)
PYTHON     = sys.executable
LOGFILE    = "/tmp/lamp_tray.log"
PIDFILE    = "/tmp/lamp_ambient.pid"
STATUSFILE = "/tmp/lamp_ambient.status"
LOCKFILE   = "/tmp/rgb-lamp-tray.lock"

MODES = ["live", "fast", "regular", "slow"]

ICON_ON      = "weather-clear-night-symbolic"
ICON_OFF     = "weather-clear-symbolic"
ICON_PENDING = "content-loading-symbolic"

_ANIM_FRAMES = ["⠋", "⠙", "⠹", "⠸", "⠼", "⠴", "⠦", "⠧", "⠇", "⠏"]


def _log(msg: str) -> None:
    print(f"[tray] {msg}", flush=True)


class _LampIndicator:
    def __init__(self, proc_utils):
        self._pu           = proc_utils
        self._proc         = None
        self._mode:   str | None = None
        self._region: str        = "border"
        self._pending:     bool  = False
        self._anim_frame:  int   = 0
        self._anim_source        = None

        self._ind = AppIndicator.Indicator.new(
            "rgb-lamp", ICON_OFF, AppIndicator.IndicatorCategory.HARDWARE,
        )
        self._ind.set_status(AppIndicator.IndicatorStatus.ACTIVE)
        self._ind.set_title("RGB Lamp")

        self._mode_items:   dict[str, Gtk.CheckMenuItem] = {}
        self._region_items: dict[str, Gtk.CheckMenuItem] = {}
        self._build_menu()

    # ------------------------------------------------------------------

    def _build_menu(self) -> None:
        menu = Gtk.Menu()

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
        _log(f"starting --{mode} --region {region}")
        logf = open(LOGFILE, "a")
        self._proc = self._pu.spawn(
            [PYTHON, SCRIPT, f"--{mode}", "--region", region], logf
        )
        logf.close()
        self._pu.write_pid(PIDFILE, self._proc.pid)
        self._mode   = mode
        self._region = region
        self._set_mode_check(mode)
        self._set_region_check(region)
        self._set_pending(True)
        _log(f"pid={self._proc.pid}")

    def _stop_proc(self) -> None:
        self._pu.read_and_kill_pid(PIDFILE)
        if self._proc is None:
            return
        self._set_pending(True)
        _log(f"stopping pid={self._proc.pid}")
        self._pu.kill_pid(self._proc.pid)
        try:
            self._proc.wait(timeout=5)
        except Exception:
            self._proc.kill()
        self._proc = None
        self._mode = None
        self._pu.clear_pid(PIDFILE)
        self._set_pending(False)

    def _mark_off(self) -> None:
        self._set_pending(False)
        self._ind.set_icon_full(ICON_OFF, "Lamp: off")
        self._ind.set_label("", "")
        self._set_mode_check(None)

    def _set_pending(self, pending: bool) -> None:
        self._pending = pending
        if pending:
            self._ind.set_icon_full(ICON_PENDING, "Lamp: connecting...")
            self._anim_frame = 0
            if self._anim_source is None:
                self._anim_source = GLib.timeout_add(100, self._animate)
        else:
            if self._anim_source is not None:
                GLib.source_remove(self._anim_source)
                self._anim_source = None

    def _animate(self) -> bool:
        if not self._pending:
            self._anim_source = None
            return False
        frame = _ANIM_FRAMES[self._anim_frame % len(_ANIM_FRAMES)]
        self._anim_frame += 1
        self._ind.set_label(frame, frame)
        return True

    # ------------------------------------------------------------------

    def _on_mode(self, item: Gtk.CheckMenuItem, mode: str) -> None:
        if not item.get_active():
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
            code = self._proc.returncode
            _log(f"process exited with code {code}")
            self._proc = None
            # If a mode was active (not a manual Off), restart automatically
            if self._mode is not None:
                _log(f"auto-restarting ({self._mode}, {self._region})")
                self._start(self._mode, self._region)
                return True
            self._mark_off()
            self._ind.set_icon_full(ICON_OFF, "Lamp: off (stopped)")
            return True

        if self._pending and self._mode is not None:
            try:
                with open(STATUSFILE) as f:
                    status = f.read().strip()
                if status == "connected":
                    _log("BLE connected — lamp is on")
                    self._set_pending(False)
                    label = f"{self._mode} · {self._region}"
                    self._ind.set_icon_full(ICON_ON, f"Lamp: {label}")
                    self._ind.set_label(self._mode, self._mode)
            except (FileNotFoundError, OSError):
                pass

        return True


class GtkTray:
    """Entry point for the GNOME tray. Call .run() to start the GTK main loop."""

    def run(self) -> None:
        pu = get_process_utils()

        _lock = pu.acquire_lock(LOCKFILE)  # noqa: F841 — must stay alive

        logf = open(LOGFILE, "a")
        sys.stdout = logf
        sys.stderr = logf

        _log("started")
        pu.read_and_kill_pid(PIDFILE)  # clean up orphan from previous session

        ind = _LampIndicator(pu)
        GLib.timeout_add(500, ind.watch_proc)

        def _sig(signum, frame):
            ind._stop_proc()
            Gtk.main_quit()

        signal.signal(signal.SIGINT, _sig)
        signal.signal(signal.SIGTERM, _sig)

        Gtk.main()
        _log("exited")
