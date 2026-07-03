"""Sensor collector — the single source of truth for all PC stats.

A background thread samples every sensor on an interval and keeps the delta
state needed for rate-based metrics (CPU power from RAPL energy counters,
disk throughput, CPU utilization). `snapshot()` returns the latest reading as
a plain dict; both the HTTP API and the desktop widget read from it, and the
MCP server / Telegram bot read it through the API.

Beyond raw sensors it also reports operational/debug info: GPU throttle reasons
+ PCIe link state, per-core CPU frequency, top processes, NVMe SMART, fan RPMs
(via CoolerControl), a per-source health map (so a null field explains itself),
and a derived status/alerts rollup.
"""
from __future__ import annotations

import os
import time
import socket
import platform
import threading
import subprocess
from collections import deque

import psutil

from . import config, health
from .processes import read_top
from .smart import SmartReader
from .coolercontrol import CoolerControlClient

RAPL_BASE = "/sys/class/powercap"

# nvidia-smi query fields, in order. We zip values back against these names so
# the parser is robust to field additions/reordering.
GPU_FIELDS = [
    "name", "temperature.gpu", "power.draw", "power.limit", "utilization.gpu",
    "utilization.memory", "memory.used", "memory.total", "clocks.gr", "fan.speed",
    "pstate", "pcie.link.gen.current", "pcie.link.gen.max",
    "pcie.link.width.current", "clocks.sm", "clocks.mem",
    "clocks_throttle_reasons.gpu_idle", "clocks_throttle_reasons.sw_power_cap",
    "clocks_throttle_reasons.hw_slowdown",
    "clocks_throttle_reasons.hw_thermal_slowdown",
    "clocks_throttle_reasons.sw_thermal_slowdown",
]
# Concerning throttle reasons (gpu_idle is normal, handled separately).
THROTTLE_LABELS = {
    "clocks_throttle_reasons.sw_power_cap": "sw_power_cap",
    "clocks_throttle_reasons.hw_slowdown": "hw_slowdown",
    "clocks_throttle_reasons.hw_thermal_slowdown": "hw_thermal",
    "clocks_throttle_reasons.sw_thermal_slowdown": "sw_thermal",
}


def _num(x):
    """Parse a float, tolerating nvidia-smi's '[N/A]' and blanks."""
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _int(x):
    v = _num(x)
    return int(v) if v is not None else None


def _read_khz(path):
    try:
        with open(path) as f:
            return int(f.read().strip())
    except (OSError, ValueError):
        return None


class Collector:
    def __init__(self, interval: float = 2.0, history: int = 120):
        self.interval = interval
        self._lock = threading.Lock()
        self._snapshot: dict = {}
        self._history: deque = deque(maxlen=history)
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

        # Delta state for rate metrics.
        self._last_disk_io = None
        self._last_disk_time = None
        self._last_rapl: dict[str, tuple[int, float]] = {}

        # Cached top-processes (refreshed on the slower PROC_INTERVAL cadence).
        self._procs_cache: dict = {"by_cpu": [], "by_ram": [], "by_gpu": [], "apps": []}
        self._procs_last = 0.0

        # Self-health bookkeeping.
        self._start = time.time()
        self._samples = 0
        self._consec_errors = 0
        self._last_error: str | None = None
        self._last_duration_ms: float | None = None
        self._sources: dict[str, str] = {}

        # Static info gathered once.
        self._cpu_model = self._read_cpu_model()
        self._kernel = platform.release()
        self._hostname = socket.gethostname()
        self._rapl_domains = self._discover_rapl()
        # Base (non-boost) clock in MHz: prefer cpufreq base_frequency (kHz),
        # fall back to acpi_cppc nominal_freq (already MHz, used by amd-pstate).
        base_khz = _read_khz("/sys/devices/system/cpu/cpu0/cpufreq/base_frequency")
        if base_khz:
            self._base_freq_mhz = base_khz / 1000
        else:
            self._base_freq_mhz = _read_khz(
                "/sys/devices/system/cpu/cpu0/acpi_cppc/nominal_freq")
        self._max_freq_khz = _read_khz(
            "/sys/devices/system/cpu/cpu0/cpufreq/cpuinfo_max_freq")

        # Optional sources.
        self.smart = SmartReader(config.SMART_INTERVAL) if config.SMART_ENABLED else None
        self.cc = CoolerControlClient(config.CC_URL, config.CC_USER, config.CC_PASSWD)

        psutil.cpu_percent(percpu=True)  # prime

    # ---- static / discovery -------------------------------------------------

    @staticmethod
    def _read_cpu_model() -> str:
        try:
            with open("/proc/cpuinfo") as f:
                for line in f:
                    if line.startswith("model name"):
                        return line.split(":", 1)[1].strip()
        except OSError:
            pass
        return platform.processor() or "unknown"

    @staticmethod
    def _discover_rapl() -> dict:
        domains = {}
        try:
            entries = os.listdir(RAPL_BASE)
        except OSError:
            return domains
        for entry in entries:
            if not entry.startswith("intel-rapl:"):
                continue
            path = os.path.join(RAPL_BASE, entry)
            try:
                with open(os.path.join(path, "name")) as f:
                    name = f.read().strip()
            except OSError:
                continue
            max_range = _read_khz(os.path.join(path, "max_energy_range_uj"))
            domains[entry] = {
                "name": name,
                "energy_file": os.path.join(path, "energy_uj"),
                "max_range": max_range,
            }
        return domains

    # ---- per-sensor readers -------------------------------------------------

    def _read_rapl_power(self, now: float) -> dict:
        out = {"cpu_package_w": None, "cpu_core_w": None}
        if not self._rapl_domains:
            self._sources["rapl"] = "absent"
            return out
        current: dict[str, tuple[int, float]] = {}
        readable = False
        for key, info in self._rapl_domains.items():
            try:
                with open(info["energy_file"]) as f:
                    energy = int(f.read().strip())
            except PermissionError:
                continue
            except (OSError, ValueError):
                continue
            readable = True
            current[key] = (energy, now)
            last = self._last_rapl.get(key)
            if not last:
                continue
            last_e, last_t = last
            dt = now - last_t
            de = energy - last_e
            if de < 0 and info["max_range"]:
                de += info["max_range"]
            if dt <= 0:
                continue
            watts = round((de / 1_000_000.0) / dt, 2)
            name = info["name"]
            if name.startswith("package"):
                out["cpu_package_w"] = watts
            elif name == "core":
                out["cpu_core_w"] = watts
        self._last_rapl = current
        self._sources["rapl"] = "ok" if readable else "permission_denied"
        return out

    def _read_gpu(self) -> list | None:
        try:
            res = subprocess.run(
                ["nvidia-smi", f"--query-gpu={','.join(GPU_FIELDS)}",
                 "--format=csv,noheader,nounits"],
                capture_output=True, text=True, timeout=5,
            )
        except FileNotFoundError:
            self._sources["nvidia_smi"] = "absent"
            return None
        except subprocess.TimeoutExpired:
            self._sources["nvidia_smi"] = "timeout"
            return None
        if res.returncode != 0:
            self._sources["nvidia_smi"] = "error"
            return None

        gpus = []
        for line in res.stdout.strip().splitlines():
            parts = [c.strip() for c in line.split(",")]
            if len(parts) < len(GPU_FIELDS):
                continue
            raw = dict(zip(GPU_FIELDS, parts))
            throttling = [
                label for field, label in THROTTLE_LABELS.items()
                if raw.get(field, "").lower() == "active"
            ]
            gpus.append({
                "name": raw["name"],
                "temp_c": _num(raw["temperature.gpu"]),
                "power_w": _num(raw["power.draw"]),
                "power_limit_w": _num(raw["power.limit"]),
                "util_gpu_pct": _num(raw["utilization.gpu"]),
                "util_mem_pct": _num(raw["utilization.memory"]),
                "mem_used_mb": _num(raw["memory.used"]),
                "mem_total_mb": _num(raw["memory.total"]),
                "clock_mhz": _num(raw["clocks.gr"]),
                "clock_sm_mhz": _num(raw["clocks.sm"]),
                "clock_mem_mhz": _num(raw["clocks.mem"]),
                "fan_pct": _num(raw["fan.speed"]),
                "pstate": raw["pstate"],
                "pcie": {
                    "gen_cur": _int(raw["pcie.link.gen.current"]),
                    "gen_max": _int(raw["pcie.link.gen.max"]),
                    "width_cur": _int(raw["pcie.link.width.current"]),
                },
                "throttle_idle": raw.get(
                    "clocks_throttle_reasons.gpu_idle", "").lower() == "active",
                "throttling": throttling,
            })
        self._sources["nvidia_smi"] = "ok" if gpus else "error"
        return gpus or None

    def _read_temps(self) -> dict:
        try:
            raw = psutil.sensors_temperatures()
            self._sources["libsensors"] = "ok" if raw else "empty"
        except Exception:
            raw = {}
            self._sources["libsensors"] = "error"

        def first(chip, *labels):
            for s in raw.get(chip, []):
                if s.label in labels:
                    return round(s.current, 1)
            return None

        def chip_max(chip):
            vals = [s.current for s in raw.get(chip, [])]
            return round(max(vals), 1) if vals else None

        chips = {
            chip: {(s.label or f"t{i}"): round(s.current, 1)
                   for i, s in enumerate(sensors)}
            for chip, sensors in raw.items()
        }
        return {
            "cpu_c": first("k10temp", "Tctl"),
            "cpu_ccd_c": first("k10temp", "Tccd1"),
            "nvme_c": first("nvme", "Composite"),
            "mobo_c": chip_max("gigabyte_wmi"),
            "chips": chips,
        }

    def _read_disk(self, now: float) -> dict:
        out = {"read_mb_s": None, "write_mb_s": None}
        io = psutil.disk_io_counters()
        if io and self._last_disk_io and self._last_disk_time:
            dt = now - self._last_disk_time
            if dt > 0:
                out["read_mb_s"] = round(
                    (io.read_bytes - self._last_disk_io.read_bytes) / 1e6 / dt, 2)
                out["write_mb_s"] = round(
                    (io.write_bytes - self._last_disk_io.write_bytes) / 1e6 / dt, 2)
        self._last_disk_io = io
        self._last_disk_time = now
        try:
            u = psutil.disk_usage("/")
            out["root_used_gb"] = round(u.used / 1e9, 1)
            out["root_total_gb"] = round(u.total / 1e9, 1)
            out["root_pct"] = u.percent
        except OSError:
            pass
        return out

    @staticmethod
    def _read_per_core_mhz() -> list:
        out = []
        i = 0
        while True:
            khz = _read_khz(
                f"/sys/devices/system/cpu/cpu{i}/cpufreq/scaling_cur_freq")
            if khz is None:
                break
            out.append(round(khz / 1000))
            i += 1
        return out

    def _read_fans(self, gpus, now) -> dict:
        cc = self.cc.read(now)
        self._sources["coolercontrol"] = cc.get("status", "unknown")
        return {
            "status": cc.get("status"),
            "rpm": cc.get("fans", []),
            "liquid_temps": cc.get("liquids", []),
            "gpu_fan_pct": [
                {"gpu": g["name"], "fan_pct": g["fan_pct"]}
                for g in (gpus or []) if g.get("fan_pct") is not None
            ],
        }

    # ---- assembly -----------------------------------------------------------

    def _build(self) -> dict:
        t0 = time.time()
        now = t0
        per_core = psutil.cpu_percent(percpu=True)
        total = round(sum(per_core) / len(per_core), 1) if per_core else None
        freq = psutil.cpu_freq()
        per_core_mhz = self._read_per_core_mhz()
        boosting = None
        if per_core_mhz and self._base_freq_mhz:
            boosting = max(per_core_mhz) > self._base_freq_mhz * 1.01
        try:
            load = list(os.getloadavg())
        except OSError:
            load = [None, None, None]
        vm = psutil.virtual_memory()
        sm = psutil.swap_memory()

        temps = self._read_temps()
        power = self._read_rapl_power(now)
        gpu = self._read_gpu()
        disk = self._read_disk(now)
        disk["nvme_c"] = temps["nvme_c"]
        # Heaviest part of the tick — refresh slowly and reuse the cache between.
        if now - self._procs_last >= config.PROC_INTERVAL:
            self._procs_cache = read_top(
                config.TOP_N, config.APPS_N, config.APPS_CHILDREN)
            self._procs_last = now
        procs = self._procs_cache
        smart = self.smart.read(now) if self.smart else []
        if self.smart:
            self._sources["smart"] = self.smart.status
        fans = self._read_fans(gpu, now)

        gpu_w = None
        if gpu:
            draws = [g["power_w"] for g in gpu if g["power_w"] is not None]
            gpu_w = round(sum(draws), 2) if draws else None
        cpu_w = power["cpu_package_w"]
        total_w = round(sum(v for v in (cpu_w, gpu_w) if v is not None), 2) \
            if (cpu_w is not None or gpu_w is not None) else None

        snap = {
            "ts": round(now, 1),
            "host": {
                "hostname": self._hostname,
                "kernel": self._kernel,
                "uptime_s": int(now - psutil.boot_time()),
                "processes": len(psutil.pids()),
            },
            "cpu": {
                "model": self._cpu_model,
                "cores": psutil.cpu_count(logical=False),
                "threads": psutil.cpu_count(logical=True),
                "util_pct": total,
                "per_core_pct": [round(c, 1) for c in per_core],
                "freq_mhz": round(freq.current) if freq else None,
                "per_core_mhz": per_core_mhz,
                "freq_max_mhz": round(self._max_freq_khz / 1000) if self._max_freq_khz else None,
                "boosting": boosting,
                "load": load,
                "temp_c": temps["cpu_c"],
                "ccd_temp_c": temps["cpu_ccd_c"],
                "power_w": cpu_w,
            },
            "gpu": gpu,
            "memory": {
                "used_gb": round(vm.used / 1e9, 1),
                "total_gb": round(vm.total / 1e9, 1),
                "pct": vm.percent,
                "swap_used_gb": round(sm.used / 1e9, 1),
                "swap_total_gb": round(sm.total / 1e9, 1),
            },
            "disk": disk,
            "power": {
                "cpu_package_w": power["cpu_package_w"],
                "cpu_core_w": power["cpu_core_w"],
                "gpu_w": gpu_w,
                "total_w": total_w,
            },
            "temps": temps,
            "processes": procs,
            "fans": fans,
            "smart": smart,
            "health": {
                "sources": dict(self._sources),
                "stale": False,
                "last_sample_age_s": 0.0,
                "sample_duration_ms": self._last_duration_ms,
                "uptime_s": int(now - self._start),
                "version": "1.1.0",
                "interval_s": self.interval,
                "samples": self._samples,
                "consecutive_errors": self._consec_errors,
                "last_error": self._last_error,
            },
        }
        # Derived status + alerts.
        snap.update(health.evaluate(snap))
        self._last_duration_ms = round((time.time() - t0) * 1000, 1)
        snap["health"]["sample_duration_ms"] = self._last_duration_ms
        return snap

    @staticmethod
    def _compact(snap: dict) -> dict:
        gpu = snap.get("gpu") or [{}]
        return {
            "ts": snap.get("ts"),
            "cpu_pct": snap.get("cpu", {}).get("util_pct"),
            "cpu_c": snap.get("cpu", {}).get("temp_c"),
            "gpu_pct": gpu[0].get("util_gpu_pct"),
            "gpu_c": gpu[0].get("temp_c"),
            "power_w": snap.get("power", {}).get("total_w"),
            "status": snap.get("status"),
        }

    # ---- lifecycle ----------------------------------------------------------

    def _run(self):
        while not self._stop.is_set():
            self._tick()
            self._stop.wait(self.interval)

    def _tick(self):
        try:
            snap = self._build()
            with self._lock:
                self._snapshot = snap
                self._history.append(self._compact(snap))
            self._samples += 1
            self._consec_errors = 0
            self._last_error = None
        except Exception as e:
            self._consec_errors += 1
            self._last_error = repr(e)

    def start(self):
        if self._thread is not None:
            return
        self._tick()
        self._thread = threading.Thread(target=self._run, daemon=True)
        self._thread.start()

    def stop(self):
        self._stop.set()

    def snapshot(self) -> dict:
        with self._lock:
            snap = dict(self._snapshot)
        # Read-time staleness: the collector loop may be wedged even though a
        # past snapshot exists. Flag it and escalate status.
        ts = snap.get("ts")
        if ts is not None:
            age = round(time.time() - ts, 1)
            h = snap.setdefault("health", {})
            h["last_sample_age_s"] = age
            if age > max(3 * self.interval, 10):
                h["stale"] = True
                snap["status"] = "critical"
                snap.setdefault("alerts", []).append({
                    "level": "critical", "kind": "stale",
                    "message": f"metrics stale ({age}s old)"})
        if self._last_error:
            snap.setdefault("health", {})["last_error"] = self._last_error
        return snap

    def history(self) -> list:
        with self._lock:
            return list(self._history)
