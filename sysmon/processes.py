"""Top processes by CPU, RAM and GPU memory — the 'what's hammering my PC?' data."""
from __future__ import annotations

import subprocess

import psutil

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
    """pid -> GPU memory MiB, via nvidia-smi compute-apps."""
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


def read_top(n: int = 5) -> dict:
    """Return {by_cpu, by_ram, by_gpu}, each a list of compact process dicts."""
    if not _primed:
        _prime()

    ncpu = psutil.cpu_count(logical=True) or 1
    rows = []
    for p in psutil.process_iter(["pid", "name", "memory_info"]):
        try:
            cpu = p.cpu_percent(None) / ncpu  # normalize to whole-machine %
            mi = p.info["memory_info"]
            rows.append({
                "pid": p.info["pid"],
                "name": p.info["name"],
                "cpu_pct": round(cpu, 1),
                "ram_mb": round(mi.rss / 1e6, 1) if mi else None,
            })
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue

    gpu_mem = _gpu_procs()
    name_by_pid = {r["pid"]: r["name"] for r in rows}

    by_cpu = sorted(rows, key=lambda r: r["cpu_pct"] or 0, reverse=True)[:n]
    by_ram = sorted(rows, key=lambda r: r["ram_mb"] or 0, reverse=True)[:n]
    by_gpu = [
        {"pid": pid, "name": name_by_pid.get(pid, "?"), "gpu_mb": mb}
        for pid, mb in sorted(gpu_mem.items(), key=lambda kv: kv[1], reverse=True)[:n]
    ]
    return {"by_cpu": by_cpu, "by_ram": by_ram, "by_gpu": by_gpu}
