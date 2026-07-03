"""Translucent system bar docked at the bottom of the screen (GTK4).

Runs under XWayland (GDK_BACKEND=x11) because GNOME's Mutter implements neither
wlr-layer-shell nor client-controlled stacking on native Wayland. Under X11 we
mark the window as a DOCK, keep it above + sticky, and size/position it to span
the bottom of the chosen monitor (optionally reserving space via _NET_WM_STRUT).

If the X11 bits can't be applied (python-xlib missing or non-X backend) the bar
still renders; it just won't dock/pin itself.
"""
import os

# Must be set before gi imports a backend.
os.environ.setdefault("GDK_BACKEND", "x11")

import json
import urllib.request

import gi

gi.require_version("Gtk", "4.0")
from gi.repository import Gtk, GLib, Gdk, Pango  # noqa: E402

from . import config  # noqa: E402

SEP = " <span foreground='#444a5c'>│</span> "

CSS = b"""
.sysmon-window { background: transparent; }
.sysmon-bar {
  background-color: rgba(18, 20, 28, 0.88);
  border-top: 1px solid rgba(255, 255, 255, 0.08);
  padding: 3px 14px;
}
.sysmon-row { font-family: monospace; font-size: 12px; color: #cdd2e0; }

/* Expandable "heaviest tasks" panel (sits above the bar, grows upward). */
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
"""

# Panel row heights (px). Kept in sync with the CSS min-heights so the window
# geometry we push to X11 matches what GTK actually lays out.
HDR_H, ROW_H, CHILD_H, PANEL_PAD = 20, 22, 18, 8
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

    def setup_dock(self, xid, x, y, w, h, reserve):
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
                sh = self.screen_height() or (y + h)
                bottom = sh - (y + h) + h
                win.change_property(self._atom("_NET_WM_STRUT"), CARD, 32,
                                    [0, 0, 0, bottom])
                win.change_property(self._atom("_NET_WM_STRUT_PARTIAL"), CARD, 32,
                                    [0, 0, 0, bottom, 0, 0, 0, 0, 0, 0, x, x + w - 1])
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
        self._geo = self._monitor_geometry()

        # Expandable-panel state.
        self._panel_open = False
        self._expanded: set[str] = set()   # app names currently drilled-down
        self._apps: list[dict] = []
        self._apps_sig = None
        self._panel_px = 0

        # Vertical stack: scrollable task panel on top (grows upward, hidden
        # until toggled), the metrics bar row on the bottom.
        outer = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self.set_child(outer)

        # Full-width panel (dark strip) with a centered fixed-width column of
        # rows inside it, so the first column clears a left/right dock rather
        # than hiding under it.
        self._panel = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._panel.add_css_class("sysmon-panel")
        self._rows = Gtk.Box(orientation=Gtk.Orientation.VERTICAL)
        self._rows.set_halign(Gtk.Align.CENTER)
        self._panel.append(self._rows)
        self._scroller = Gtk.ScrolledWindow()
        self._scroller.set_policy(Gtk.PolicyType.NEVER, Gtk.PolicyType.AUTOMATIC)
        self._scroller.set_child(self._panel)
        self._scroller.set_visible(False)
        outer.append(self._scroller)

        box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL)
        box.add_css_class("sysmon-bar")
        outer.append(box)

        self._bar = Gtk.Label()
        self._bar.add_css_class("sysmon-row")
        self._bar.set_use_markup(True)
        self._bar.set_hexpand(True)
        self._bar.set_halign(Gtk.Align.CENTER)
        box.append(self._bar)

        # Caret at the right edge toggles the panel.
        self._toggle = Gtk.Button(label="▴ tasks")
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

    def _current_height(self):
        """Window height: just the bar when collapsed, bar + panel when open."""
        return config.BAR_HEIGHT + (self._panel_px if self._panel_open else 0)

    def _target_geometry(self):
        """Bottom-anchored slot sized to the current (collapsed/expanded) height.
        Recomputed live so a resolution/monitor change — or the panel opening —
        is reflected on the next tick rather than staying anchored to a stale
        size. Anchored to the bottom edge, so the panel grows upward."""
        g = self._monitor_geometry() or self._geo or {"x": 0, "y": 0, "w": 1920, "h": 1080}
        self._geo = g
        h = self._current_height()
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
        self._x11.setup_dock(self._xid, x, y, w, h, config.BAR_RESERVE)
        self._x11.configure(self._xid, x, y, w, h)
        # Re-assert after map (Mutter applies some hints only once mapped).
        GLib.timeout_add(600, self._after_map)

    def _after_map(self):
        self._ensure_docked(force=True)
        return False

    def _ensure_docked(self, force=False):
        """Keep the bar docked bottom + above. Called every refresh tick, but
        only re-applies dock/pin/geometry when the window has actually drifted
        or been un-pinned by the WM — so it self-heals a Mutter reset within one
        interval without restacking on every tick."""
        if not self._xid or not self._x11.ok:
            return
        x, y, w, h = self._target_geometry()
        if force or self._x11.needs_redock(self._xid, x, y, w, h):
            self._x11.setup_dock(self._xid, x, y, w, h, config.BAR_RESERVE)
            self._x11.configure(self._xid, x, y, w, h)
            self._x11.reassert_above(self._xid)

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
        # Rebuild from scratch each time — cheap for a handful of rows, and it
        # keeps expand/collapse and live number updates on one code path.
        child = self._rows.get_first_child()
        while child:
            nxt = child.get_next_sibling()
            self._rows.remove(child)
            child = nxt
        if not self._panel_open:
            self._panel_px = 0
            return

        # Centered column width: the configured width, capped so it never runs
        # under the screen edges.
        mon_w = (self._geo or {"w": 1920}).get("w", 1920)
        self._rows.set_size_request(
            max(320, min(config.PANEL_WIDTH, mon_w - 2 * PANEL_SIDE_MIN)), -1)

        self._rows.append(self._row_cells(
            "", "application", "cpu", "ram", "vram", "sysmon-hdr"))
        px = HDR_H
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
            self._rows.append(btn)
            px += ROW_H
            if expanded:
                for p in a["pids"]:
                    r = self._row_cells(
                        "", f"↳ {p['name']} · {p['pid']}", self._fmt_pct(p["cpu_pct"]),
                        self._fmt_mb(p["ram_mb"]), self._fmt_mb(p["gpu_mb"]), "sysmon-dim")
                    r.add_css_class("sysmon-childrow")
                    self._rows.append(r)
                    px += CHILD_H

        # Cap the panel at 60% of the monitor and let it scroll past that.
        max_px = int((self._geo or {"h": 1080})["h"] * 0.6)
        self._panel_px = min(px + PANEL_PAD, max_px)
        # Pin the scroller to an exact height. Neutralise min to 0 first so the
        # (min <= max) invariant holds whether the panel grew or shrank vs the
        # previous render.
        self._scroller.set_min_content_height(0)
        self._scroller.set_max_content_height(self._panel_px)
        self._scroller.set_min_content_height(self._panel_px)

    def _fetch_apps(self):
        try:
            self._apps = _fetch("/metrics/apps").get("apps", [])
        except Exception:
            self._apps = []

    def _on_toggle_panel(self, _btn):
        self._panel_open = not self._panel_open
        self._scroller.set_visible(self._panel_open)
        self._toggle.set_label("▾ tasks" if self._panel_open else "▴ tasks")
        if self._panel_open and not self._apps:
            self._fetch_apps()
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

    def refresh(self):
        # Self-heal the dock/pin first: if the WM reset us (lock/unlock, remap,
        # workspace switch) this snaps the bar back to the bottom and on top.
        self._ensure_docked()
        try:
            m = _fetch("/metrics")
        except Exception:
            self._bar.set_markup("<span foreground='#ff6b6b'>sysmon daemon offline…</span>")
            return True

        cpu = m.get("cpu", {})
        gpus = m.get("gpu") or []
        mem = m.get("memory", {})
        dsk = m.get("disk", {})
        pwr = m.get("power", {})
        fans = m.get("fans", {})
        up = m.get("host", {}).get("uptime_s", 0)
        status = m.get("status", "ok")
        alerts = m.get("alerts", [])

        def n(v, s=""):
            return f"{v}{s}" if v is not None else "—"

        sections = [
            f"{self._lbl('CPU')} {n(cpu.get('util_pct'),'%')} "
            f"{self._temp_markup(cpu.get('temp_c'), 70, 85)} {n(cpu.get('power_w'),'W')}",
        ]
        if gpus:
            g = gpus[0]
            gtag = ""
            if g.get("throttling"):
                gtag = f" <span foreground='#ff6b6b'>⚠{','.join(g['throttling'])}</span>"
            sections.append(
                f"{self._lbl('GPU')} {n(g.get('util_gpu_pct'),'%')} "
                f"{self._temp_markup(g.get('temp_c'), 70, 83)} {n(g.get('power_w'),'W')} "
                f"{self._lbl(n(g.get('pstate')))}{gtag}")
        sections.append(f"{self._lbl('RAM')} {n(mem.get('used_gb'))}/{n(mem.get('total_gb'),'G')}")
        sections.append(
            f"{self._lbl('DSK')} r{n(dsk.get('read_mb_s'))} w{n(dsk.get('write_mb_s'))} "
            f"{self._lbl('nvme')}{n(dsk.get('nvme_c'),'°')}")

        rpm = fans.get("rpm") or []
        if rpm:
            top = sorted(rpm, key=lambda r: r.get("rpm") or 0, reverse=True)[:3]
            fan_txt = " ".join(f"{int(r['rpm'])}" for r in top if r.get("rpm")) + "rpm"
        else:
            gp = fans.get("gpu_fan_pct") or []
            fan_txt = f"gpu {n(gp[0]['fan_pct'],'%')}" if gp else "—"
        sections.append(f"{self._lbl('FAN')} {fan_txt}")
        sections.append(f"{self._lbl('PWR')} ~{n(pwr.get('total_w'),'W')}")

        # Status + first alert + uptime + host, appended after the metrics so
        # the whole line sits centered on the bar.
        scolor = {"ok": "#8ce99a", "warn": "#ffd166", "critical": "#ff6b6b"}.get(status, "#cdd2e0")
        sections.append(f"<span foreground='{scolor}'>● {status.upper()}</span>")
        if alerts:
            sections.append(f"<span foreground='{scolor}'>{alerts[0]['message']}</span>")
        h, mm = divmod(up // 60, 60)
        d, h = divmod(h, 24)
        sections.append(self._lbl(f"up {d}d{h}h{mm}m"))
        sections.append(self._lbl(m.get("host", {}).get("hostname", "")))

        self._bar.set_markup(SEP.join(sections))
        self._update_panel()
        return True


def _apply_css():
    provider = Gtk.CssProvider()
    provider.load_from_data(CSS)
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
