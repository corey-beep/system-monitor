"""Translucent system bar docked at the top (or bottom) of the screen (GTK4).

Runs under XWayland (GDK_BACKEND=x11) because GNOME's Mutter implements neither
wlr-layer-shell nor client-controlled stacking on native Wayland. Under X11 we
mark the window as a DOCK, keep it above + sticky, and size/position it to span
the top/bottom edge of the chosen monitor (optionally reserving space via _NET_WM_STRUT).

If the X11 bits can't be applied (python-xlib missing or non-X backend) the bar
still renders; it just won't dock/pin itself.
"""
import os

# Must be set before gi imports a backend.
os.environ.setdefault("GDK_BACKEND", "x11")
# Software rendering: the bar is a few labels and small charts, so GTK's GL
# renderer only adds a GPU context, VRAM and memory for nothing.
os.environ.setdefault("GSK_RENDERER", "cairo")

import json
import urllib.request

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gdk, Pango  # noqa: E402

from . import config, panel  # noqa: E402

SEP = " <span foreground='#444a5c'>│</span> "

CSS = b"""
.sysmon-window { background: transparent; }
.sysmon-bar {
  background-color: rgba(18, 20, 28, 0.88);
  padding: 3px 14px;
}
.sysmon-bar.at-bottom { border-top: 1px solid rgba(255, 255, 255, 0.08); }
.sysmon-bar.at-top { border-bottom: 1px solid rgba(255, 255, 255, 0.08); }
.sysmon-row { font-family: monospace; font-size: 12px; color: #cdd2e0; }

/* Expandable "heaviest tasks" panel (grows away from the docked edge). */
.sysmon-panel { background-color: rgba(14, 16, 22, 0.95); }
.sysmon-hdr, .sysmon-cell, .sysmon-dim { font-family: monospace; font-size: 11px; }
.sysmon-hdr  { color: #7f8aa3; }
.sysmon-cell { color: #cdd2e0; }
.sysmon-dim  { color: #7f8aa3; }
.sysmon-childrow { background-color: rgba(255, 255, 255, 0.02); }
button.sysmon-flat {
  background: none; border: none; box-shadow: none;
  padding: 0 12px; min-height: 22px;
}
button.sysmon-flat:hover { background-color: rgba(255, 255, 255, 0.06); }
.sysmon-toggle { font-family: monospace; font-size: 11px; color: #8b93a7; }

/* Hover fade: opacity + transition timings are appended in _apply_css(). */
"""

# Vertical padding (px) added around the measured panel content.
PANEL_PAD = 12
# Minimum gap kept on each side of the centered panel content (px).
PANEL_SIDE_MIN = 40


# --------------------------------------------------------------------------- #
# X11 helpers (best-effort; failure never breaks the UI).
# --------------------------------------------------------------------------- #
class X11Bar:
    def __init__(self):
        self.ok = False
        try:
            import Xlib.display
            from Xlib import X, Xatom
            self._X = X
            self._Xatom = Xatom
            self._display = Xlib.display.Display()
            self._root = self._display.screen().root
            self.ok = True
        except Exception:
            self._display = None

    def _atom(self, name):
        return self._display.intern_atom(name)

    def _client_message(self, xid, msg_type, data):
        import Xlib.protocol.event
        win = self._display.create_resource_object("window", xid)
        ev = Xlib.protocol.event.ClientMessage(
            window=win, client_type=self._atom(msg_type), data=(32, data))
        mask = self._X.SubstructureRedirectMask | self._X.SubstructureNotifyMask
        self._root.send_event(ev, event_mask=mask)
        self._display.flush()

    def screen_height(self):
        try:
            return self._display.screen().height_in_pixels
        except Exception:
            return None

    def setup_dock(self, xid, x, y, w, h, reserve, top=False):
        """Pre-map: mark as DOCK, sticky/above, and (optionally) reserve space."""
        if not self.ok:
            return
        try:
            win = self._display.create_resource_object("window", xid)
            ATOM = self._Xatom.ATOM
            CARD = self._Xatom.CARDINAL
            win.change_property(self._atom("_NET_WM_WINDOW_TYPE"), ATOM, 32,
                                [self._atom("_NET_WM_WINDOW_TYPE_DOCK")])
            win.change_property(self._atom("_NET_WM_STATE"), ATOM, 32,
                                [self._atom("_NET_WM_STATE_ABOVE"),
                                 self._atom("_NET_WM_STATE_STICKY"),
                                 self._atom("_NET_WM_STATE_SKIP_TASKBAR"),
                                 self._atom("_NET_WM_STATE_SKIP_PAGER")])
            if reserve:
                if top:
                    strut = [0, 0, y + h, 0]
                    partial = strut + [0, 0, 0, 0, x, x + w - 1, 0, 0]
                else:
                    sh = self.screen_height() or (y + h)
                    strut = [0, 0, 0, sh - y]
                    partial = strut + [0, 0, 0, 0, 0, 0, x, x + w - 1]
                win.change_property(self._atom("_NET_WM_STRUT"), CARD, 32, strut)
                win.change_property(self._atom("_NET_WM_STRUT_PARTIAL"), CARD, 32, partial)
            self._display.flush()
        except Exception:
            pass

    def configure(self, xid, x, y, w, h):
        if not self.ok:
            return
        try:
            win = self._display.create_resource_object("window", xid)
            win.configure(x=int(x), y=int(y), width=int(w), height=int(h))
            self._display.flush()
        except Exception:
            pass

    def reassert_above(self, xid):
        if not self.ok:
            return
        try:
            self._client_message(xid, "_NET_WM_STATE",
                                  [1, self._atom("_NET_WM_STATE_ABOVE"),
                                   self._atom("_NET_WM_STATE_STICKY"), 1, 0])
        except Exception:
            pass

    def needs_redock(self, xid, x, y, w, h):
        """Read-only: True if the window has lost its DOCK type / ABOVE pin, or
        drifted off the target geometry. Lets callers re-dock only when the WM
        has actually reset us (lock/unlock, remap, workspace switch) instead of
        restacking every tick — which would flicker and fight the compositor."""
        if not self.ok:
            return False
        try:
            import Xlib.X
            win = self._display.create_resource_object("window", xid)

            def atoms(name):
                p = win.get_full_property(self._atom(name), Xlib.X.AnyPropertyType)
                return set(p.value) if p else set()

            if self._atom("_NET_WM_STATE_ABOVE") not in atoms("_NET_WM_STATE"):
                return True
            if self._atom("_NET_WM_WINDOW_TYPE_DOCK") not in atoms("_NET_WM_WINDOW_TYPE"):
                return True
            geo = win.get_geometry()
            coords = win.translate_coords(self._root, 0, 0)
            cx, cy = -coords.x, -coords.y
            # Tolerate a couple px of WM fudge before calling it drift.
            if (abs(cx - x) > 2 or abs(cy - y) > 2
                    or abs(geo.width - w) > 2 or abs(geo.height - h) > 2):
                return True
            return False
        except Exception:
            return False


def _fetch(path):
    req = urllib.request.Request(f"{config.BASE_URL}{path}")
    if config.TOKEN:
        req.add_header("Authorization", f"Bearer {config.TOKEN}")
    with urllib.request.urlopen(req, timeout=4) as resp:
        return json.loads(resp.read().decode())


class SysmonBar(Gtk.ApplicationWindow):
    def __init__(self, app):
        super().__init__(application=app, title="sysmon")
        self.set_decorated(False)
        self.add_css_class("sysmon-window")
        self._x11 = X11Bar()
        self._xid = None
        self._top = config.BAR_POSITION == "top"
        self._geo = self._monitor_geometry()
        # Read once, before we set any strut of our own, so reserving space
        # can't feed back into the offset.
        self._top_offset = self._initial_top_offset() if self._top else 0

        # Expandable-panel state.
        self._panel_open = False
        self._expanded: set[str] = set()   # app names currently drilled-down
        self._apps: list[dict] = []
        self._apps_sig = None
        self._panel_px = 0
        self._last = None   # latest /metrics snapshot

        # Vertical stack: metrics bar row on the docked edge, scrollable task
        # panel (hidden until toggled) growing away from it.
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        outer.add_css_class("sysmon-root")
        self.set_child(outer)
        self._root_box = outer

        # Dim when not hovered. The motion controller sits on the window so
        # moving between the bar and the panel doesn't count as leaving.
        self._fade_id = 0
        if config.BAR_FADE_DELAY > 0:
            motion = Gtk.EventControllerMotion()
            motion.connect("enter", self._on_pointer_enter)
            motion.connect("leave", self._on_pointer_leave)
            self.add_controller(motion)
            self._schedule_fade()

        # Full-width panel (dark strip) with a centered fixed-width column of
        # rows inside it, so the first column clears a left/right dock rather
        # than hiding under it.
        self._panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._panel.add_css_class("sysmon-panel")
        self._rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=8)
        self._rows.set_halign(Gtk.Align.CENTER)
        self._rows.set_margin_top(PANEL_PAD // 2)
        self._rows.set_margin_bottom(PANEL_PAD // 2)
        self._panel.append(self._rows)

        # Built once, updated in place: CPU / GPU / memory cards, a one-line
        # system footer, then the heaviest-apps list (rebuilt when it changes).
        self._sections = panel.Sections()
        self._rows.append(self._sections.box)
        self._footer = Gtk.Label()
        self._footer.add_css_class("sysmon-footer")
        self._footer.set_use_markup(True)
        self._footer.set_xalign(0.0)
        self._footer.set_ellipsize(Pango.EllipsizeMode.END)
        self._rows.append(self._footer)
        tasks_hdr = Gtk.Label(label="HEAVIEST APPS")
        tasks_hdr.add_css_class("sysmon-section")
        tasks_hdr.set_xalign(0.0)
        self._rows.append(tasks_hdr)
        self._tasks = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._rows.append(self._tasks)
        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_child(self._panel)
        self._scroller.set_visible(False)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.add_css_class("sysmon-bar")
        box.add_css_class("at-top" if self._top else "at-bottom")
        for child in ((box, self._scroller) if self._top else (self._scroller, box)):
            outer.append(child)

        self._bar = Gtk.Label()
        self._bar.add_css_class("sysmon-row")
        self._bar.set_use_markup(True)
        self._bar.set_hexpand(True)
        self._bar.set_halign(Gtk.Align.CENTER)
        box.append(self._bar)

        # Caret at the right edge toggles the panel.
        self._toggle = Gtk.Button(label=self._toggle_label())
        self._toggle.set_has_frame(False)
        self._toggle.add_css_class("sysmon-flat")
        self._toggle.add_css_class("sysmon-toggle")
        self._toggle.connect("clicked", self._on_toggle_panel)
        box.append(self._toggle)

        if self._geo:
            self.set_default_size(self._geo["w"], config.BAR_HEIGHT)

        self.connect("realize", self._on_realize)
        self.refresh()
        GLib.timeout_add(int(config.INTERVAL * 1000), self.refresh)

    def _monitor_geometry(self):
        try:
            monitors = Gdk.Display.get_default().get_monitors()
            mon = monitors.get_item(min(config.BAR_MONITOR, monitors.get_n_items() - 1))
            g = mon.get_geometry()
            return {"x": g.x, "y": g.y, "w": g.width, "h": g.height}
        except Exception:
            return None

    def _initial_top_offset(self):
        """How far below the monitor's top edge a top bar should sit: the
        explicit override, else the workarea's top inset (GNOME's top bar)."""
        if config.BAR_TOP_OFFSET is not None:
            return max(0, config.BAR_TOP_OFFSET)
        try:
            monitors = Gdk.Display.get_default().get_monitors()
            mon = monitors.get_item(min(config.BAR_MONITOR, monitors.get_n_items() - 1))
            wa, g = mon.get_workarea(), mon.get_geometry()
            return max(0, wa.y - g.y)
        except Exception:
            return 0

    def _toggle_label(self):
        """Caret points the way the panel opens (or closes)."""
        away, back = ("▾", "▴") if self._top else ("▴", "▾")
        return f"{back} collapse" if self._panel_open else f"{away} expand"

    def _current_height(self):
        """Window height: just the bar when collapsed, bar + panel when open."""
        return config.BAR_HEIGHT + (self._panel_px if self._panel_open else 0)

    def _target_geometry(self):
        """Edge-anchored slot sized to the current (collapsed/expanded) height.
        Recomputed live so a resolution/monitor change — or the panel opening —
        is reflected on the next tick rather than staying anchored to a stale
        size. Anchored to the docked edge, so the panel grows away from it."""
        g = self._monitor_geometry() or self._geo or {"x": 0, "y": 0, "w": 1920, "h": 1080}
        self._geo = g
        h = self._current_height()
        if self._top:
            return g["x"], g["y"] + self._top_offset, g["w"], h
        return g["x"], g["y"] + g["h"] - h, g["w"], h

    def _apply_geometry(self):
        """Push the current target geometry to the window now (after a panel
        open/close or drill-down changes the height) and keep it pinned above."""
        if not self._xid:
            return
        x, y, w, h = self._target_geometry()
        self.set_default_size(w, h)
        self._x11.configure(self._xid, x, y, w, h)
        self._x11.reassert_above(self._xid)

    def _on_realize(self, *_):
        try:
            self._xid = self.get_surface().get_xid()
        except Exception:
            self._xid = None
        if not self._xid:
            return
        x, y, w, h = self._target_geometry()
        self._x11.setup_dock(self._xid, x, y, w, h, config.BAR_RESERVE, self._top)
        self._x11.configure(self._xid, x, y, w, h)
        # Re-assert after map (Mutter applies some hints only once mapped).
        GLib.timeout_add(600, self._after_map)

    def _after_map(self):
        self._ensure_docked(force=True)
        return False

    def _ensure_docked(self, force=False):
        """Keep the bar docked to its edge + above. Called every refresh tick, but
        only re-applies dock/pin/geometry when the window has actually drifted
        or been un-pinned by the WM — so it self-heals a Mutter reset within one
        interval without restacking on every tick."""
        if not self._xid or not self._x11.ok:
            return
        x, y, w, h = self._target_geometry()
        if force or self._x11.needs_redock(self._xid, x, y, w, h):
            self._x11.setup_dock(self._xid, x, y, w, h, config.BAR_RESERVE, self._top)
            self._x11.configure(self._xid, x, y, w, h)
            self._x11.reassert_above(self._xid)

    # -- hover fade --------------------------------------------------------- #
    def _cancel_fade(self):
        if self._fade_id:
            GLib.source_remove(self._fade_id)
            self._fade_id = 0

    def _schedule_fade(self):
        self._cancel_fade()
        self._fade_id = GLib.timeout_add(int(config.BAR_FADE_DELAY * 1000), self._fade_out)

    def _fade_out(self):
        self._fade_id = 0
        self._root_box.add_css_class("sysmon-faded")
        return False

    def _on_pointer_enter(self, *_):
        self._cancel_fade()
        self._root_box.remove_css_class("sysmon-faded")

    def _on_pointer_leave(self, *_):
        self._schedule_fade()

    # -- rendering --------------------------------------------------------- #
    @staticmethod
    def _temp_markup(t, warn, hot):
        if t is None:
            return "—"
        color = "#ff6b6b" if t >= hot else "#ffd166" if t >= warn else "#cdd2e0"
        return f"<span foreground='{color}'>{t}°C</span>"

    @staticmethod
    def _lbl(text):
        return f"<span foreground='#7f8aa3'>{text}</span>"

    @staticmethod
    def _w(v):
        return "—" if v is None else f"{v:.0f}W"

    @staticmethod
    def _mini(frac, accent, warn=0.75, crit=0.9, width=5):
        """Tiny inline meter for the bar: eighth-block fill on a dim track,
        coloured by the same ok/warn/crit ramp as the panel meters."""
        if frac is None:
            return f"<span foreground='#2a2f3d'>{'█' * width}</span>"
        eighths = round(frac * width * 8)
        full, rem = divmod(eighths, 8)
        fill = "█" * full + (" ▏▎▍▌▋▊▉"[rem] if rem else "")
        color = panel.level_color(frac, accent, warn, crit)
        empty = width - full - (1 if rem else 0)
        return (f"<span background='#2a2f3d'><span foreground='{color}'>{fill}</span>"
                f"{' ' * empty}</span>")

    # -- heaviest-tasks panel ---------------------------------------------- #
    @staticmethod
    def _fmt_pct(v):
        return f"{v:.1f}%" if v else "0%"

    @staticmethod
    def _fmt_mb(v):
        if not v:
            return "—"
        return f"{v / 1024:.1f} GB" if v >= 1024 else f"{int(v)} MB"

    @staticmethod
    def _cell(text, width=None, xalign=0.0, cls="sysmon-cell",
              expand=False, ellipsize=False):
        lbl = Gtk.Label(label=str(text))
        lbl.add_css_class(cls)
        lbl.set_xalign(float(xalign))
        if width:
            lbl.set_width_chars(width)
        if expand:
            lbl.set_hexpand(True)
        if ellipsize:
            lbl.set_ellipsize(Pango.EllipsizeMode.END)
        return lbl

    def _row_cells(self, caret, name, cpu, ram, gpu, cls):
        row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        row.append(self._cell(caret, 2, 0.0, cls))
        row.append(self._cell(name, None, 0.0, cls, expand=True, ellipsize=True))
        row.append(self._cell(cpu, 6, 1.0, cls))
        row.append(self._cell(ram, 8, 1.0, cls))
        row.append(self._cell(gpu, 9, 1.0, cls))
        return row

    def _render_panel(self):
        """Rebuild the heaviest-apps rows (cheap for a handful of rows; keeps
        expand/collapse and live numbers on one code path), then resize."""
        child = self._tasks.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._tasks.remove(child)
            child = nxt
        if not self._panel_open:
            self._panel_px = 0
            return

        # Centered column width: the configured width, capped so it never runs
        # under the screen edges.
        mon_w = (self._geo or {"w": 1920}).get("w", 1920)
        width = max(320, min(config.PANEL_WIDTH, mon_w - 2 * PANEL_SIDE_MIN))
        self._rows.set_size_request(width, -1)

        self._tasks.append(self._row_cells(
            "", "application", "cpu", "ram", "vram", "sysmon-hdr"))
        for a in self._apps:
            expanded = a["app"] in self._expanded
            caret = "▾" if expanded else ("▸" if a["count"] > 1 else " ")
            name = f"{a['app']}  ({a['count']})" if a["count"] > 1 else a["app"]
            btn = Gtk.Button()
            btn.set_has_frame(False)
            btn.add_css_class("sysmon-flat")
            btn.set_child(self._row_cells(
                caret, name, self._fmt_pct(a["cpu_pct"]),
                self._fmt_mb(a["ram_mb"]), self._fmt_mb(a["gpu_mb"]), "sysmon-cell"))
            btn.connect("clicked", self._on_toggle_app, a["app"], a["count"])
            self._tasks.append(btn)
            if expanded:
                for p in a["pids"]:
                    r = self._row_cells(
                        "", f"↳ {p['name']} · {p['pid']}", self._fmt_pct(p["cpu_pct"]),
                        self._fmt_mb(p["ram_mb"]), self._fmt_mb(p["gpu_mb"]), "sysmon-dim")
                    r.add_css_class("sysmon-childrow")
                    self._tasks.append(r)
        self._resize_panel(width)

    def _resize_panel(self, width=None):
        """Size the panel to its real laid-out height (cards + footer + rows),
        capped at 60% of the monitor; past that it scrolls."""
        if width is None:
            width = self._rows.get_size_request()[0]
        try:
            _min, nat, _mb, _nb = self._rows.measure(Gtk.Orientation.VERTICAL, width)
        except Exception:
            nat = 520
        max_px = int((self._geo or {"h": 1080})["h"] * 0.6)
        px = min(nat + PANEL_PAD, max_px)
        if px == self._panel_px:
            return False
        self._panel_px = px
        # Neutralise min to 0 first so the (min <= max) invariant holds whether
        # the panel grew or shrank vs the previous render.
        self._scroller.set_min_content_height(0)
        self._scroller.set_max_content_height(px)
        self._scroller.set_min_content_height(px)
        return True

    def _fetch_apps(self):
        try:
            self._apps = _fetch("/metrics/apps").get("apps", [])
        except Exception:
            self._apps = []

    def _on_toggle_panel(self, _btn):
        self._panel_open = not self._panel_open
        self._scroller.set_visible(self._panel_open)
        self._toggle.set_label(self._toggle_label())
        if self._panel_open:
            if not self._apps:
                self._fetch_apps()
            if self._last:
                self._update_sections(self._last)
        self._render_panel()
        self._apply_geometry()

    def _on_toggle_app(self, _btn, app, count):
        if count <= 1:
            return
        self._expanded.discard(app) if app in self._expanded else self._expanded.add(app)
        self._render_panel()
        self._apply_geometry()

    def _update_panel(self):
        """Refresh panel numbers on the tick while it's open; only re-render (and
        re-lay-out the window) when the data actually changed, to avoid churn."""
        if not self._panel_open:
            return
        self._fetch_apps()
        sig = tuple((a["app"], a["count"], a["cpu_pct"], a["ram_mb"], a["gpu_mb"])
                    for a in self._apps)
        if sig != self._apps_sig:
            self._apps_sig = sig
            self._expanded &= {a["app"] for a in self._apps}  # drop vanished apps
            self._render_panel()
            self._apply_geometry()
        elif self._resize_panel():
            self._apply_geometry()

    def _update_sections(self, m):
        self._sections.update(m)
        disk = m.get("disk", {})
        pwr = m.get("power", {})
        temps = m.get("temps", {})
        fans = m.get("fans", {})
        host = m.get("host", {})
        lbl = self._lbl
        rpm = [int(r["rpm"]) for r in (fans.get("rpm") or []) if r.get("rpm")]
        gp = fans.get("gpu_fan_pct") or []
        fan_txt = (" ".join(map(str, sorted(rpm, reverse=True)[:4])) + " rpm") if rpm \
            else (f"gpu {gp[0]['fan_pct']:.0f}%" if gp else "—")
        up = host.get("uptime_s", 0)
        h, mm = divmod(up // 60, 60)
        d, h = divmod(h, 24)

        def n(v, s=""):
            return f"{v}{s}" if v is not None else "—"

        parts = [
            f"{lbl('DISK')} {n(disk.get('root_used_gb'))}/{n(disk.get('root_total_gb'), ' GB')}"
            f" ({n(disk.get('root_pct'), '%')}) · r {n(disk.get('read_mb_s'))} w "
            f"{n(disk.get('write_mb_s'))} MB/s · nvme {n(disk.get('nvme_c'), '°C')}",
            f"{lbl('POWER')} cpu {self._w(pwr.get('cpu_package_w'))} + gpu "
            f"{self._w(pwr.get('gpu_w'))} = {self._w(pwr.get('total_w'))}",
            f"{lbl('BOARD')} {n(temps.get('mobo_c'), '°C')}",
            f"{lbl('FANS')} {fan_txt}",
            f"{lbl('UP')} {d}d {h}h {mm}m",
            f"{lbl('PROCS')} {n(host.get('processes'))}",
            lbl(host.get("hostname", "")),
        ]
        self._footer.set_markup("   ".join(parts))

    def refresh(self):
        # Self-heal the dock/pin first: if the WM reset us (lock/unlock, remap,
        # workspace switch) this snaps the bar back to its edge and on top.
        self._ensure_docked()
        try:
            m = _fetch("/metrics")
        except Exception:
            self._bar.set_markup("<span foreground='#ff6b6b'>sysmon daemon offline…</span>")
            return True

        self._last = m
        self._sections.record(m)
        cpu = m.get("cpu", {})
        gpus = m.get("gpu") or []
        mem = m.get("memory", {})
        dsk = m.get("disk", {})
        pwr = m.get("power", {})
        status = m.get("status", "ok")
        alerts = m.get("alerts", [])
        lbl, mini = self._lbl, self._mini

        def n(v, s=""):
            return f"{v}{s}" if v is not None else "—"

        def frac(v, hi):
            return None if v is None or not hi else max(0.0, min(1.0, v / hi))

        cu = cpu.get("util_pct")
        f = cpu.get("freq_mhz")
        sections = [
            f"{lbl('CPU')} {mini(frac(cu, 100), panel.CPU_ACCENT)} {n(cu, '%')} "
            f"{self._temp_markup(cpu.get('temp_c'), 70, 85)} {self._w(cpu.get('power_w'))} "
            f"{lbl(f'{f / 1000:.1f}GHz' if f else '—')}",
        ]
        if gpus:
            g = gpus[0]
            gu = g.get("util_gpu_pct")
            gtag = ""
            if g.get("throttling"):
                gtag = f" <span foreground='#ff6b6b'>⚠{','.join(g['throttling'])}</span>"
            used, total = g.get("mem_used_mb"), g.get("mem_total_mb")
            vram = f"{used / 1024:.1f}/{total / 1024:.0f}G" if used is not None and total else "—"
            sections.append(
                f"{lbl('GPU')} {mini(frac(gu, 100), panel.GPU_ACCENT)} {n(gu, '%')} "
                f"{self._temp_markup(g.get('temp_c'), 70, 83)} {self._w(g.get('power_w'))} "
                f"{lbl('vram')} {vram}{gtag}")
        mp = mem.get("pct")
        ram = (f"{lbl('RAM')} {mini(frac(mp, 100), panel.MEM_ACCENT, 0.8, 0.92)} "
               f"{n(mem.get('used_gb'))}/{n(mem.get('total_gb'), 'G')}")
        su = mem.get("swap_used_gb") or 0
        if su >= 0.1:
            ram += f" {lbl('swap')} {su}G"
        sections.append(ram)
        sections.append(
            f"{lbl('DSK')} r{n(dsk.get('read_mb_s'))} w{n(dsk.get('write_mb_s'))}")
        sections.append(f"{lbl('PWR')} ~{self._w(pwr.get('total_w'))}")

        # Status + first alert, appended after the metrics so the whole line
        # sits centered on the bar. Uptime/host/fans live in the panel footer.
        scolor = {"ok": "#8ce99a", "warn": "#ffd166", "critical": "#ff6b6b"}.get(status, "#cdd2e0")
        sections.append(f"<span foreground='{scolor}'>● {status.upper()}</span>")
        if alerts:
            sections.append(f"<span foreground='{scolor}'>{alerts[0]['message']}</span>")

        self._bar.set_markup(SEP.join(sections))
        if self._panel_open:
            self._update_sections(m)
        self._update_panel()
        return True


def _apply_css():
    provider = Gtk.CssProvider()
    faded = max(0.0, min(1.0, config.BAR_FADE_OPACITY))
    fade_in = max(0, config.BAR_FADE_IN_MS)
    fade_out = max(0, config.BAR_FADE_OUT_MS)
    # The transition on the *target* state is the one that runs, so the plain
    # rule times the fade back in and the .sysmon-faded rule times the dim.
    provider.load_from_string(
        CSS.decode() + panel.CSS
        + f"\n.sysmon-root {{ opacity: 1; transition: opacity {fade_in}ms ease-out; }}"
        + f"\n.sysmon-root.sysmon-faded {{ opacity: {faded};"
        + f" transition: opacity {fade_out}ms ease-in-out; }}\n")
    Gtk.StyleContext.add_provider_for_display(
        Gdk.Display.get_default(), provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION)


def main():
    app = Gtk.Application(application_id="net.securitywiz.sysmon")

    def on_activate(a):
        _apply_css()
        SysmonBar(a).present()

    app.connect("activate", on_activate)
    app.run(None)


if __name__ == "__main__":
    main()
