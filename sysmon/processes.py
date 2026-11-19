"""Top processes by CPU, RAM and GPU memory — the 'what's hammering my PC?' data."""
from __future__ import annotations

import os
import subprocess

import psutil

from . import nvml


def _app_of(info: dict) -> str:
    """Application label for grouping: the executable's basename, falling back to
    the process name. Using the exe (not the mutable `comm`/name) merges apps
    that rename their children — e.g. Firefox's content processes report a comm
    of 'Isolated Web Content' but all run the same `firefox` binary."""
    exe = info.get("exe")
    if exe:
        base = os.path.basename(exe)
        # Ignore version-named binaries (e.g. an AppImage exe literally named
        # '2.1.199') — those group nicer under their process name.
        if base and any(c.isalpha() for c in base):
            return base
    return info.get("name") or "?"

# Prime per-process CPU counters so the first real read isn't all zeros.
_primed = False


def _prime():
    global _primed
    for p in psutil.process_iter(["pid"]):
        try:
            p.cpu_percent(None)
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _primed = True


def _gpu_procs() -> dict[int, float]:
    """pid -> GPU memory MiB: NVML in-process, else nvidia-smi compute-apps."""
    via_nvml = nvml.gpu_procs()
    if via_nvml is not None:
        return via_nvml
    try:
        res = subprocess.run(
            ["nvidia-smi", "--query-compute-apps=pid,used_memory",
             "--format=csv,noheader,nounits"],
            capture_output=True, text=True, timeout=5,
        )
    except (FileNotFoundError, subprocess.TimeoutExpired):
        return {}
    if res.returncode != 0:
        return {}
    out = {}
    for line in res.stdout.strip().splitlines():
        parts = [c.strip() for c in line.split(",")]
        if len(parts) >= 2:
            try:
                out[int(parts[0])] = float(parts[1])
            except ValueError:
                continue
    return out


def _group_apps(rows: list[dict], n: int, max_children: int) -> list[dict]:
    """Collapse per-PID rows into per-application groups keyed by process name,
    summing cpu%/RAM/VRAM and keeping each group's child PIDs for the drill-down.

    Sorted by cpu% then RAM so the 'heaviest' float to the top and RAM-heavy but
    idle apps (browsers) still surface when everything's near 0% CPU."""
    groups: dict[str, dict] = {}
    for r in rows:
        name = r["app"]
        g = groups.get(name)
        if g is None:
            g = groups[name] = {
                "app": name, "cpu_pct": 0.0, "ram_mb": 0.0,
                "gpu_mb": 0.0, "count": 0, "pids": [],
            }
        g["cpu_pct"] += r["cpu_pct"] or 0
        g["ram_mb"] += r["ram_mb"] or 0
        g["gpu_mb"] += r["gpu_mb"] or 0
        g["count"] += 1
        g["pids"].append(r)

    out = []
    for g in groups.values():
        g["cpu_pct"] = round(g["cpu_pct"], 1)
        g["ram_mb"] = round(g["ram_mb"], 1)
        g["gpu_mb"] = round(g["gpu_mb"], 1) if g["gpu_mb"] else None
        g["pids"] = sorted(
            g["pids"], key=lambda p: (p["cpu_pct"] or 0, p["ram_mb"] or 0),
            reverse=True)[:max_children]
        out.append(g)
    out.sort(key=lambda g: (g["cpu_pct"], g["ram_mb"]), reverse=True)
    return out[:n]


def read_top(n: int = 5, apps_n: int = 8, apps_children: int = 12) -> dict:
    """Return {by_cpu, by_ram, by_gpu, apps}.

    The first three are top-N per-PID lists (compat with existing consumers);
    `apps` is the grouped heaviest-applications view the widget's panel renders."""
    if not _primed:
        _prime()

    ncpu = psutil.cpu_count(logical=True) or 1
    gpu_mem = _gpu_procs()
    rows = []
    for p in psutil.process_iter(["pid", "name", "exe", "memory_info"]):
        try:
            cpu = p.cpu_percent(None) / ncpu  # normalize to whole-machine %
            mi = p.info["memory_info"]
            pid = p.info["pid"]
            rows.append({
                "pid": pid,
                "name": p.info["name"],
                "app": _app_of(p.info),
                "cpu_pct": round(cpu, 1),
                "ram_mb": round(mi.rss / 1e6, 1) if mi else None,
                "gpu_mb": gpu_mem.get(pid),
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    name_by_pid = {r["pid"]: r["name"] for r in rows}

    by_cpu = sorted(rows, key=lambda r: r["cpu_pct"] or 0, reverse=True)[:n]
    by_ram = sorted(rows, key=lambda r: r["ram_mb"] or 0, reverse=True)[:n]
    by_gpu = [
        {"pid": pid, "name": name_by_pid.get(pid, "?"), "gpu_mb": mb}
        for pid, mb in sorted(gpu_mem.items(), key=lambda kv: kv[1], reverse=True)[:n]
    ]
    apps = _group_apps(rows, apps_n, apps_children)
    return {"by_cpu": by_cpu, "by_ram": by_ram, "by_gpu": by_gpu, "apps": apps}
