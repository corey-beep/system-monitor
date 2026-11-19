"""Expanded-panel building blocks: meters, history charts and the CPU / GPU /
memory section cards.

Everything here is drawn with cairo on Gtk.DrawingArea, so it costs nothing
while the panel is closed (GTK doesn't draw unmapped widgets) and only one
small redraw per tick while it's open. Cards are built once and updated in
place; nothing is rebuilt on the refresh tick.
"""
from __future__ import annotations

import math
from collections import deque

import cairo
import gi

gi.require_foreign("cairo")
from gi.repository import Gtk, Pango

# Palette. Section accents + the shared ok/warn/crit ramp for meters.
CPU_ACCENT = "#7aa2f7"
GPU_ACCENT = "#9ece6a"
MEM_ACCENT = "#bb9af7"
SECOND = "#ff9e64"     # second series on the history charts (temp, VRAM, swap)
WARN = "#e0af68"
CRIT = "#f7768e"
DIM = "#7f8aa3"
TEXT = "#cdd2e0"
CACHE = "#565f89"      # memory composition: cache/buffers segment

# History length (samples). At the default 3s tick this is 6 minutes.
HISTORY = 120

CSS = """
.sysmon-card {
  background-color: rgba(255, 255, 255, 0.035);
  border: 1px solid rgba(255, 255, 255, 0.06);
  border-radius: 10px;
  padding: 10px 12px;
}
.sysmon-title { font-family: monospace; font-size: 12px; font-weight: bold; }
.sysmon-sub   { font-family: monospace; font-size: 10px; color: #7f8aa3; }
.sysmon-big   { font-family: monospace; font-size: 15px; font-weight: bold; color: #e6e9f2; }
.sysmon-mlabel, .sysmon-mvalue, .sysmon-dkey, .sysmon-dval, .sysmon-legend {
  font-family: monospace; font-size: 10.5px;
}
.sysmon-mlabel, .sysmon-dkey, .sysmon-legend { color: #7f8aa3; }
.sysmon-mvalue, .sysmon-dval { color: #cdd2e0; }
.sysmon-section { font-family: monospace; font-size: 10px; color: #565f89;
                  letter-spacing: 1px; }
.sysmon-footer  { font-family: monospace; font-size: 10.5px; color: #cdd2e0; }
"""


def rgb(hex_color: str):
    h = hex_color.lstrip("#")
    return tuple(int(h[i:i + 2], 16) / 255 for i in (0, 2, 4))


def level_color(frac, accent, warn=0.75, crit=0.9):
    """Accent while healthy, amber past `warn`, red past `crit` (fractions)."""
    if frac is None:
        return accent
    return CRIT if frac >= crit else WARN if frac >= warn else accent


def _rounded(cr, x, y, w, h, r):
    r = max(0.0, min(r, h / 2, w / 2))
    cr.new_sub_path()
    cr.arc(x + w - r, y + r, r, -math.pi / 2, 0)
    cr.arc(x + w - r, y + h - r, r, 0, math.pi / 2)
    cr.arc(x + r, y + h - r, r, math.pi / 2, math.pi)
    cr.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    cr.close_path()


def _frac(v, lo, hi):
    if v is None or hi is None or hi <= lo:
        return None
    return max(0.0, min(1.0, (v - lo) / (hi - lo)))


# --------------------------------------------------------------------------- #
# Drawing widgets
# --------------------------------------------------------------------------- #
class Meter(Gtk.DrawingArea):
    """Rounded horizontal bar. One segment for a plain meter, several for a
    stacked composition (e.g. memory used | cache | free)."""

    def __init__(self, height=7):
        super().__init__()
        self.set_content_height(height)
        self.set_hexpand(True)
        self.set_valign(Gtk.Align.CENTER)
        self._segments: list[tuple[float, str]] = []
        self.set_draw_func(self._draw)

    def set(self, segments):
        segments = [(f, c) for f, c in segments if f]
        if segments != self._segments:
            self._segments = segments
            self.queue_draw()

    def _draw(self, _area, cr, w, h):
        _rounded(cr, 0, 0, w, h, h / 2)
        cr.set_source_rgba(1, 1, 1, 0.07)
        cr.fill_preserve()
        cr.clip()
        x = 0.0
        for frac, color in self._segments:
            fw = w * max(0.0, min(1.0, frac))
            cr.set_source_rgb(*rgb(color))
            cr.rectangle(x, 0, fw, h)
            cr.fill()
            x += fw


class Spark(Gtk.DrawingArea):
    """History chart: a filled area for the primary series and a thin line
    for an optional second series, both on a fixed 0..100 scale."""

    def __init__(self, color, second=SECOND, height=46):
        super().__init__()
        self.set_content_height(height)
        self.set_hexpand(True)
        self.color = color
        self.second = second
        self.a: deque = deque(maxlen=HISTORY)
        self.b: deque = deque(maxlen=HISTORY)
        self.set_draw_func(self._draw)

    def push(self, a, b=None):
        self.a.append(a)
        self.b.append(b)

    def _points(self, series, w, h):
        step = w / max(1, HISTORY - 1)
        x0 = w - step * (len(series) - 1)   # newest sample at the right edge
        pts = []
        for i, v in enumerate(series):
            if v is None:
                continue
            v = max(0.0, min(100.0, v))
            pts.append((x0 + i * step, h - 1 - (h - 2) * v / 100.0))
        return pts

    def _draw(self, _area, cr, w, h):
        _rounded(cr, 0, 0, w, h, 6)
        cr.set_source_rgba(0, 0, 0, 0.25)
        cr.fill_preserve()
        cr.clip()
        # 25/50/75% guides.
        cr.set_source_rgba(1, 1, 1, 0.05)
        cr.set_line_width(1)
        for g in (0.25, 0.5, 0.75):
            y = round(h * g) + 0.5
            cr.move_to(0, y)
            cr.line_to(w, y)
        cr.stroke()

        pts = self._points(self.a, w, h)
        if len(pts) >= 2:
            r, g, b = rgb(self.color)
            cr.move_to(pts[0][0], h)
            for x, y in pts:
                cr.line_to(x, y)
            cr.line_to(pts[-1][0], h)
            cr.close_path()
            grad = cairo.LinearGradient(0, 0, 0, h)
            grad.add_color_stop_rgba(0, r, g, b, 0.45)
            grad.add_color_stop_rgba(1, r, g, b, 0.03)
            cr.set_source(grad)
            cr.fill()
            cr.move_to(*pts[0])
            for x, y in pts[1:]:
                cr.line_to(x, y)
            cr.set_source_rgb(r, g, b)
            cr.set_line_width(1.5)
            cr.stroke()

        pts = self._points(self.b, w, h)
        if len(pts) >= 2:
            cr.move_to(*pts[0])
            for x, y in pts[1:]:
                cr.line_to(x, y)
            cr.set_source_rgba(*rgb(self.second), 0.9)
            cr.set_line_width(1.2)
            cr.set_dash([3, 2])
            cr.stroke()


class CoreGrid(Gtk.DrawingArea):
    """One small vertical bar per logical CPU, filled by its utilisation."""

    def __init__(self, accent, height=26):
        super().__init__()
        self.set_content_height(height)
        self.set_hexpand(True)
        self.accent = accent
        self.values: list[float] = []
        self.set_draw_func(self._draw)

    def set(self, values):
        values = list(values or [])
        if values != self.values:
            self.values = values
            self.queue_draw()

    def _draw(self, _area, cr, w, h):
        n = len(self.values)
        if not n:
            return
        gap = 3
        bw = (w - gap * (n - 1)) / n
        for i, v in enumerate(self.values):
            x = i * (bw + gap)
            _rounded(cr, x, 0, bw, h, 2)
            cr.set_source_rgba(1, 1, 1, 0.07)
            cr.fill()
            frac = max(0.0, min(1.0, (v or 0) / 100))
            fh = max(1.5, h * frac)
            _rounded(cr, x, h - fh, bw, fh, 2)
            cr.set_source_rgb(*rgb(level_color(frac, self.accent, 0.7, 0.9)))
            cr.fill()


# --------------------------------------------------------------------------- #
# Cards
# --------------------------------------------------------------------------- #
def _label(cls, text="", xalign=0.0, chars=None, expand=False, ellipsize=False):
    lbl = Gtk.Label(label=text)
    lbl.add_css_class(cls)
    lbl.set_xalign(xalign)
    if chars:
        lbl.set_width_chars(chars)
    if expand:
        lbl.set_hexpand(True)
    if ellipsize:
        lbl.set_ellipsize(Pango.EllipsizeMode.END)
    return lbl


class Card(Gtk.Box):
    """One section: header (title, subtitle, headline number), history chart
    with legend, labelled meters, then a two-column key/value detail grid."""

    def __init__(self, title, accent, legend_a, legend_b):
        super().__init__(orientation=Gtk.Orientation.VERTICAL, spacing=7)
        self.add_css_class("sysmon-card")
        self.set_hexpand(True)
        self.accent = accent

        head = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
        t = _label("sysmon-title")
        t.set_markup(f"<span foreground='{accent}'>{title}</span>")
        head.append(t)
        self.sub = _label("sysmon-sub", expand=True, ellipsize=True)
        self.sub.set_valign(Gtk.Align.CENTER)
        head.append(self.sub)
        self.big = _label("sysmon-big", xalign=1.0)
        head.append(self.big)
        self.append(head)

        self.spark = Spark(accent)
        self.append(self.spark)
        leg = _label("sysmon-legend")
        leg.set_markup(
            f"<span foreground='{accent}'>━</span> {legend_a}   "
            f"<span foreground='{SECOND}'>┅</span> {legend_b}   "
            f"<span foreground='#565f89'>last {HISTORY * 3 // 60} min</span>")
        self.legend = leg
        self.append(leg)

        self._meters_grid = Gtk.Grid(column_spacing=8, row_spacing=5)
        self.append(self._meters_grid)
        self._meters: dict[str, tuple[Meter, Gtk.Label]] = {}

        self._extra = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=4)
        self.append(self._extra)

        self._details_grid = Gtk.Grid(column_spacing=8, row_spacing=3)
        self._details_grid.set_column_homogeneous(False)
        self.append(self._details_grid)
        self._details: dict[str, Gtk.Label] = {}
        self._n_details = 0

    def add_meter(self, key, label):
        row = len(self._meters)
        self._meters_grid.attach(_label("sysmon-mlabel", label, chars=6), 0, row, 1, 1)
        m = Meter()
        self._meters_grid.attach(m, 1, row, 1, 1)
        v = _label("sysmon-mvalue", "—", xalign=1.0, chars=15)
        self._meters_grid.attach(v, 2, row, 1, 1)
        self._meters[key] = (m, v)

    def set_meter(self, key, frac, text, color=None, segments=None):
        m, v = self._meters[key]
        if segments is None:
            segments = [(frac, color or level_color(frac, self.accent))]
        m.set(segments)
        v.set_text(text)

    def add_extra(self, widget, caption=None):
        if caption:
            self._extra.append(_label("sysmon-section", caption))
        self._extra.append(widget)

    def add_detail(self, key, label):
        i = self._n_details
        row, col = divmod(i, 2)
        k = _label("sysmon-dkey", label)
        v = _label("sysmon-dval", "—", expand=True, ellipsize=True)
        self._details_grid.attach(k, col * 2, row, 1, 1)
        self._details_grid.attach(v, col * 2 + 1, row, 1, 1)
        self._details[key] = v
        self._n_details += 1

    def set_detail(self, key, text, color=None):
        lbl = self._details[key]
        if color:
            lbl.set_markup(f"<span foreground='{color}'>{text}</span>")
        else:
            lbl.set_text(str(text))


# --------------------------------------------------------------------------- #
# Formatting
# --------------------------------------------------------------------------- #
def _n(v, fmt="{:.0f}", suffix=""):
    return "—" if v is None else fmt.format(v) + suffix


def _ghz(mhz):
    return "—" if not mhz else f"{mhz / 1000:.2f} GHz"


def _pressure(v):
    """PSI %: anything sustained above a few % is felt as lag."""
    if v is None:
        return "—", None
    color = CRIT if v >= 20 else WARN if v >= 5 else None
    return f"{v:.1f}%", color


# --------------------------------------------------------------------------- #
# The three sections
# --------------------------------------------------------------------------- #
class Sections:
    """Builds the CPU / GPU / memory cards and maps a /metrics snapshot onto
    them. `record()` runs every tick (so history fills while the panel is
    closed); `update()` only while it's open."""

    def __init__(self):
        self.box = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=10)
        self.box.set_homogeneous(True)

        # CPU
        c = Card("CPU", CPU_ACCENT, "usage", "temp °C")
        for key, lbl in (("util", "usage"), ("temp", "temp"),
                         ("power", "power"), ("clock", "clock")):
            c.add_meter(key, lbl)
        self.cores = CoreGrid(CPU_ACCENT)
        c.add_extra(self.cores, "PER-THREAD USAGE")
        for key, lbl in (("load", "load"), ("psi", "pressure"),
                         ("runq", "run queue"), ("ctx", "ctx sw/s"),
                         ("ccd", "CCD temp"), ("boost", "boost"),
                         ("gov", "governor"), ("epp", "profile")):
            c.add_detail(key, lbl)
        self.cpu = c

        # GPU
        g = Card("GPU", GPU_ACCENT, "usage", "VRAM %")
        for key, lbl in (("util", "usage"), ("vram", "VRAM"), ("temp", "temp"),
                         ("power", "power"), ("core", "core"), ("mem", "mem clk"),
                         ("fan", "fan")):
            g.add_meter(key, lbl)
        for key, lbl in (("pstate", "P-state"), ("pcie", "PCIe"),
                         ("enc", "encoder"), ("dec", "decoder"),
                         ("memctl", "mem ctrl"), ("video", "video clk"),
                         ("throttle", "throttle"), ("driver", "driver")):
            g.add_detail(key, lbl)
        self.gpu = g

        # Memory
        r = Card("MEMORY", MEM_ACCENT, "used %", "swap %")
        for key, lbl in (("comp", "layout"), ("used", "used"), ("swap", "swap"),
                         ("commit", "commit")):
            r.add_meter(key, lbl)
        legend = _label("sysmon-legend")
        legend.set_markup(
            f"<span foreground='{MEM_ACCENT}'>■</span> used   "
            f"<span foreground='{CACHE}'>■</span> cache/buffers   "
            f"<span foreground='#3b4052'>■</span> free")
        r.add_extra(legend)
        for key, lbl in (("avail", "available"), ("cached", "cached"),
                         ("shared", "shared"), ("dirty", "dirty"),
                         ("psi", "pressure"), ("psifull", "stalled"),
                         ("io", "IO press."), ("swcache", "swap cache")):
            r.add_detail(key, lbl)
        self.mem = r

        for card in (c, g, r):
            self.box.append(card)

    # -- every tick -------------------------------------------------------- #
    def record(self, m):
        cpu = m.get("cpu", {})
        gpu = (m.get("gpu") or [{}])[0]
        mem = m.get("memory", {})
        self.cpu.spark.push(cpu.get("util_pct"), cpu.get("temp_c"))
        vram = None
        if gpu.get("mem_total_mb"):
            vram = 100 * (gpu.get("mem_used_mb") or 0) / gpu["mem_total_mb"]
        self.gpu.spark.push(gpu.get("util_gpu_pct"), vram)
        swap = None
        if mem.get("swap_total_gb"):
            swap = 100 * (mem.get("swap_used_gb") or 0) / mem["swap_total_gb"]
        self.mem.spark.push(mem.get("pct"), swap)

    # -- while open -------------------------------------------------------- #
    def update(self, m):
        self._update_cpu(m.get("cpu", {}))
        self._update_gpu((m.get("gpu") or [None])[0])
        self._update_mem(m.get("memory", {}))
        for card in (self.cpu, self.gpu, self.mem):
            card.spark.queue_draw()

    def _update_cpu(self, cpu):
        c = self.cpu
        c.sub.set_text(f"{cpu.get('model', '')} · {cpu.get('cores')}c/{cpu.get('threads')}t")
        util = cpu.get("util_pct")
        c.big.set_text(_n(util, suffix="%"))
        c.set_meter("util", _frac(util, 0, 100), _n(util, "{:.1f}", "%"))
        t = cpu.get("temp_c")
        # Zen 3 Tctl: fine into the 70s, throttles near 90.
        c.set_meter("temp", _frac(t, 25, 95), _n(t, "{:.1f}", "°C"),
                    color=CRIT if (t or 0) >= 85 else WARN if (t or 0) >= 70 else CPU_ACCENT)
        w = cpu.get("power_w")
        c.set_meter("power", _frac(w, 0, 142), _n(w, "{:.1f}", " W"))   # 142 W PPT
        f, fmax = cpu.get("freq_mhz"), cpu.get("freq_max_mhz")
        c.set_meter("clock", _frac(f, 0, fmax), f"{_ghz(f)}", color=CPU_ACCENT)
        self.cores.set(cpu.get("per_core_pct"))

        load = cpu.get("load") or []
        if len(load) == 3 and None not in load:
            threads = cpu.get("threads") or 1
            color = WARN if load[0] > threads else None
            c.set_detail("load", " / ".join(f"{x:.2f}" for x in load), color)
        txt, col = _pressure(cpu.get("pressure_pct"))
        c.set_detail("psi", txt, col)
        rq, blk = cpu.get("procs_running"), cpu.get("procs_blocked")
        c.set_detail("runq", f"{_n(rq)} running · {_n(blk)} blocked",
                     WARN if (blk or 0) > 2 else None)
        ctx = cpu.get("ctx_per_s")
        c.set_detail("ctx", "—" if ctx is None else
                     f"{ctx / 1000:.1f}k" if ctx >= 1000 else str(ctx))
        c.set_detail("ccd", _n(cpu.get("ccd_temp_c"), "{:.1f}", "°C"))
        b = cpu.get("boosting")
        c.set_detail("boost", f"{'on' if b else 'off'} · max {_ghz(fmax)}" if b is not None else "—")
        c.set_detail("gov", cpu.get("governor") or "—")
        c.set_detail("epp", (cpu.get("epp") or "—").replace("_", " "))

    def _update_gpu(self, g):
        c = self.gpu
        if not g:
            c.sub.set_text("no GPU data")
            c.big.set_text("—")
            return
        c.sub.set_text(g.get("name", ""))
        util = g.get("util_gpu_pct")
        c.big.set_text(_n(util, suffix="%"))
        c.set_meter("util", _frac(util, 0, 100), _n(util, suffix="%"))
        used, total = g.get("mem_used_mb"), g.get("mem_total_mb")
        vf = _frac(used, 0, total)
        c.set_meter("vram", vf, f"{(used or 0) / 1024:.1f} / {(total or 0) / 1024:.0f} GB"
                    if total else "—")
        t = g.get("temp_c")
        c.set_meter("temp", _frac(t, 25, 90), _n(t, "{:.0f}", "°C"),
                    color=CRIT if (t or 0) >= 83 else WARN if (t or 0) >= 70 else GPU_ACCENT)
        w, lim = g.get("power_w"), g.get("power_limit_w")
        c.set_meter("power", _frac(w, 0, lim), f"{_n(w)} / {_n(lim)} W")
        clk, cmax = g.get("clock_mhz"), g.get("clock_max_mhz")
        c.set_meter("core", _frac(clk, 0, cmax), f"{_n(clk)} MHz", color=GPU_ACCENT)
        mclk, mmax = g.get("clock_mem_mhz"), g.get("clock_mem_max_mhz")
        c.set_meter("mem", _frac(mclk, 0, mmax), f"{_n(mclk)} MHz", color=GPU_ACCENT)
        fan = g.get("fan_pct")
        c.set_meter("fan", _frac(fan, 0, 100), _n(fan, suffix="%"), color=GPU_ACCENT)

        ps = g.get("pstate") or "—"
        # P0 with an idle core means something is pinning clocks (wasted watts).
        idle_p0 = ps == "P0" and (util or 0) < 10
        c.set_detail("pstate", f"{ps}{'  (idle!)' if idle_p0 else ''}",
                     WARN if idle_p0 else None)
        p = g.get("pcie") or {}
        c.set_detail("pcie", f"Gen{_n(p.get('gen_cur'))}/{_n(p.get('gen_max'))} "
                             f"x{_n(p.get('width_cur'))}")
        c.set_detail("enc", _n(g.get("util_enc_pct"), suffix="%"))
        c.set_detail("dec", _n(g.get("util_dec_pct"), suffix="%"))
        c.set_detail("memctl", _n(g.get("util_mem_pct"), suffix="%"))
        c.set_detail("video", _n(g.get("clock_video_mhz"), suffix=" MHz"))
        thr = g.get("throttling") or []
        if thr:
            c.set_detail("throttle", ", ".join(thr), CRIT)
        else:
            c.set_detail("throttle", "idle" if g.get("throttle_idle") else "none")
        c.set_detail("driver", g.get("driver") or "—")

    def _update_mem(self, mem):
        c = self.mem
        total = mem.get("total_gb") or 0
        used = mem.get("used_gb") or 0
        cached = mem.get("cached_gb") or 0
        c.sub.set_text(f"{total:.0f} GB RAM · {mem.get('swap_total_gb', 0):.0f} GB swap")
        pct = mem.get("pct")
        c.big.set_text(_n(pct, suffix="%"))
        if total:
            uf, cf = used / total, min(cached / total, 1 - used / total)
            c.set_meter("comp", None, f"{mem.get('free_gb', 0):.1f} GB free",
                        segments=[(uf, level_color(uf, MEM_ACCENT, 0.8, 0.92)),
                                  (cf, CACHE)])
        c.set_meter("used", _frac(pct, 0, 100), f"{used:.1f} / {total:.1f} GB",
                    color=level_color(_frac(pct, 0, 100), MEM_ACCENT, 0.8, 0.92))
        su, st = mem.get("swap_used_gb"), mem.get("swap_total_gb")
        sf = _frac(su, 0, st)
        c.set_meter("swap", sf, f"{_n(su, '{:.1f}')} / {_n(st, '{:.0f}')} GB",
                    color=level_color(sf, MEM_ACCENT, 0.5, 0.8))
        com = mem.get("committed_gb")
        lim = total + (st or 0)
        cf = _frac(com, 0, lim)
        c.set_meter("commit", cf, f"{_n(com, '{:.1f}')} GB",
                    color=level_color(cf, MEM_ACCENT, 0.8, 0.95))

        c.set_detail("avail", _n(mem.get("available_gb"), "{:.1f}", " GB"))
        c.set_detail("cached", _n(mem.get("cached_gb"), "{:.1f}", " GB"))
        c.set_detail("shared", _n(mem.get("shared_gb"), "{:.1f}", " GB"))
        d = mem.get("dirty_gb")
        c.set_detail("dirty", "—" if d is None else f"{d * 1000:.0f} MB" if d < 1 else f"{d:.1f} GB")
        for key, field in (("psi", "pressure_pct"), ("psifull", "pressure_full_pct"),
                           ("io", "io_pressure_pct")):
            txt, col = _pressure(mem.get(field))
            c.set_detail(key, txt, col)
        c.set_detail("swcache", _n(mem.get("swap_cached_gb"), "{:.1f}", " GB"))
