"""NVMe SMART health via `sudo -n nvme smart-log ... -o json` (cached, slow poll).

Needs nvme-cli installed and the scoped sudoers rule in
integrations/sudoers/sysmon-nvme so the unprivileged daemon can read it.
"""
from __future__ import annotations

import glob
import json
import subprocess
import time


class SmartReader:
    def __init__(self, interval: float = 60.0):
        self.interval = interval
        self._cache: list | None = None
        self._last = 0.0
        self._status = "pending"  # pending | ok | no_devices | needs_setup | error

    @staticmethod
    def _controllers() -> list[str]:
        # One controller per /dev/nvmeN (the char device smart-log expects).
        return sorted(glob.glob("/dev/nvme[0-9]"))

    def _read_one(self, dev: str) -> dict | None:
        try:
            res = subprocess.run(
                ["sudo", "-n", "nvme", "smart-log", dev, "-o", "json"],
                capture_output=True, text=True, timeout=8,
            )
        except (FileNotFoundError, subprocess.TimeoutExpired):
            self._status = "needs_setup"
            return None
        if res.returncode != 0:
            # Most commonly: sudo needs the scoped NOPASSWD rule.
            self._status = "needs_setup"
            return None
        try:
            d = json.loads(res.stdout)
        except json.JSONDecodeError:
            self._status = "error"
            return None
        kelvin = d.get("temperature")
        duw = d.get("data_units_written")
        dur = d.get("data_units_read")
        return {
            "device": dev,
            "wear_pct": d.get("percent_used"),
            "available_spare_pct": d.get("avail_spare"),
            "spare_threshold_pct": d.get("spare_thresh"),
            "power_on_hours": d.get("power_on_hours"),
            "unsafe_shutdowns": d.get("unsafe_shutdowns"),
            "media_errors": d.get("media_errors"),
            "critical_warning": d.get("critical_warning"),
            "temp_c": round(kelvin - 273.15, 1) if isinstance(kelvin, (int, float)) else None,
            # 1 data unit = 512000 bytes per the NVMe spec.
            "written_tb": round(duw * 512000 / 1e12, 2) if isinstance(duw, (int, float)) else None,
            "read_tb": round(dur * 512000 / 1e12, 2) if isinstance(dur, (int, float)) else None,
        }

    def read(self, now: float | None = None) -> list:
        now = now if now is not None else time.time()
        if self._cache is not None and (now - self._last) < self.interval:
            return self._cache
        devs = self._controllers()
        if not devs:
            self._status = "no_devices"
            self._cache, self._last = [], now
            return self._cache
        results = [r for d in devs if (r := self._read_one(d))]
        if results:
            self._status = "ok"
        self._cache, self._last = results, now
        return results

    @property
    def status(self) -> str:
        return self._status
