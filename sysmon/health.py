"""Derive a single status verdict + alert list from a snapshot.

Gives agents/Telegram a one-line answer ('WARN — GPU thermal throttling')
instead of making them reason over raw numbers. Pure function, no I/O.
"""
from __future__ import annotations

from . import config

_RANK = {"ok": 0, "warn": 1, "critical": 2}


def evaluate(snap: dict) -> dict:
    alerts = []  # each: {level, kind, message}

    def add(level, kind, message):
        alerts.append({"level": level, "kind": kind, "message": message})

    # CPU temperature
    ct = snap.get("cpu", {}).get("temp_c")
    if ct is not None:
        if ct >= config.CPU_TEMP_CRIT:
            add("critical", "cpu_temp", f"CPU {ct}°C")
        elif ct >= config.CPU_TEMP_WARN:
            add("warn", "cpu_temp", f"CPU {ct}°C")

    # GPU temperature + throttling
    for i, g in enumerate(snap.get("gpu") or []):
        gt = g.get("temp_c")
        tag = f"GPU{i}" if len(snap.get("gpu") or []) > 1 else "GPU"
        if gt is not None:
            if gt >= config.GPU_TEMP_CRIT:
                add("critical", "gpu_temp", f"{tag} {gt}°C")
            elif gt >= config.GPU_TEMP_WARN:
                add("warn", "gpu_temp", f"{tag} {gt}°C")
        throttles = g.get("throttling") or []
        if throttles:
            add("warn", "gpu_throttle", f"{tag} throttling: {', '.join(throttles)}")
        pcie = g.get("pcie") or {}
        cur, mx = pcie.get("gen_cur"), pcie.get("gen_max")
        # Only flag a degraded link when the GPU is actually busy (idle GPUs
        # legitimately drop to gen1 to save power).
        if cur and mx and cur < mx and (g.get("util_gpu_pct") or 0) >= 30:
            add("warn", "pcie_link",
                f"{tag} PCIe at gen{cur} of gen{mx} under load")

    # Disk usage
    rp = snap.get("disk", {}).get("root_pct")
    if rp is not None and rp >= config.DISK_PCT_WARN:
        add("warn", "disk_full", f"root filesystem {rp}% full")

    # Swap thrashing (heuristic: meaningfully into swap)
    mem = snap.get("memory", {})
    su, st = mem.get("swap_used_gb"), mem.get("swap_total_gb")
    if su and st and su > 1.0 and su / st > 0.5:
        add("warn", "swap", f"swap {su}/{st}GB in use")

    # NVMe wear + critical warnings
    for s in snap.get("smart") or []:
        w = s.get("wear_pct")
        if w is not None and w >= config.NVME_WEAR_WARN:
            add("warn", "nvme_wear", f"{s.get('device')} wear {w}%")
        if s.get("critical_warning"):
            add("critical", "nvme_crit",
                f"{s.get('device')} SMART critical_warning={s.get('critical_warning')}")

    # Sensor sources that are down/degraded
    for name, st_ in (snap.get("health", {}).get("sources") or {}).items():
        if st_ not in ("ok", "disabled", "no_credentials", "pending"):
            add("warn", "source", f"{name}: {st_}")

    # Stale data (collector wedged)
    if snap.get("health", {}).get("stale"):
        add("critical", "stale", "metrics are stale (collector not updating)")

    status = "ok"
    for a in alerts:
        if _RANK[a["level"]] > _RANK[status]:
            status = a["level"]
    return {"status": status, "alerts": alerts}
