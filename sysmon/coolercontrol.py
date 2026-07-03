"""CoolerControl client — fan RPMs / duties and liquid temps.

This is the only source of fan RPM on this machine (no hwmon fan inputs exist),
including the GPU fan's RPM. The coolercontrold API requires a session login,
so we Basic-auth to POST /login, keep the cookie, and read POST /status.

Disabled (status='no_credentials') until a password is supplied via
SYSMON_CC_PASSWD or ~/.config/sysmon/secrets.json. Parsing is schema-tolerant:
any channel exposing a non-null `rpm` becomes a fan entry.
"""
from __future__ import annotations

import time

import httpx


class CoolerControlClient:
    def __init__(self, url: str, user: str, passwd: str, interval: float = 3.0):
        self.url = url.rstrip("/")
        self.user = user
        self.passwd = passwd
        self.interval = interval
        self._client = httpx.Client(timeout=4.0)
        self._authed = False
        self._cache: dict | None = None
        self._last = 0.0
        self._status = "disabled" if not passwd else "pending"

    def _login(self) -> bool:
        try:
            r = self._client.post(f"{self.url}/login", auth=(self.user, self.passwd))
            if r.status_code == 200:
                self._authed = True
                self._status = "ok"
                return True
            self._status = "auth_failed"
        except Exception:
            self._status = "unreachable"
        self._authed = False
        return False

    def _fetch_status(self) -> dict | None:
        for attempt in (1, 2):
            if not self._authed and not self._login():
                return None
            try:
                r = self._client.post(f"{self.url}/status", json={"all": False})
            except Exception:
                self._status = "unreachable"
                return None
            if r.status_code == 401:  # session expired -> re-login once
                self._authed = False
                continue
            if r.status_code != 200:
                self._status = "error"
                return None
            try:
                return r.json()
            except Exception:
                self._status = "error"
                return None
        return None

    @staticmethod
    def _parse(payload: dict) -> dict:
        fans, temps, liquids = [], [], []
        for dev in payload.get("devices", []):
            dname = dev.get("name", "?")
            hist = dev.get("status_history") or []
            if not hist:
                continue
            latest = hist[-1]
            for ch in latest.get("channels", []):
                rpm = ch.get("rpm")
                if rpm is not None:
                    fans.append({
                        "device": dname,
                        "channel": ch.get("name"),
                        "rpm": rpm,
                        "duty_pct": ch.get("duty"),
                    })
            for t in latest.get("temps", []):
                entry = {"device": dname,
                         "name": t.get("frontend_name") or t.get("name"),
                         "temp_c": t.get("temp")}
                temps.append(entry)
                if (dev.get("type") or "").lower().startswith("liquid"):
                    liquids.append(entry)
        return {"fans": fans, "temps": temps, "liquids": liquids}

    def read(self, now: float | None = None) -> dict:
        now = now if now is not None else time.time()
        if not self.passwd:
            return {"status": "no_credentials", "fans": [], "temps": [], "liquids": []}
        if self._cache is not None and (now - self._last) < self.interval:
            return self._cache
        payload = self._fetch_status()
        if payload is None:
            result = {"status": self._status, "fans": [], "temps": [], "liquids": []}
        else:
            result = {"status": "ok", **self._parse(payload)}
        self._cache, self._last = result, now
        return result

    @property
    def status(self) -> str:
        return self._status
