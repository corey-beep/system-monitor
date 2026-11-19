"""Shared configuration for the sysmon service.

Everything is overridable via environment variables so the daemon, widget,
MCP server and Telegram bot all agree on where the API lives.
"""
import os

HOST = os.environ.get("SYSMON_HOST", "127.0.0.1")
PORT = int(os.environ.get("SYSMON_PORT", "8787"))
BASE_URL = os.environ.get("SYSMON_URL", f"http://{HOST}:{PORT}")

# How often the collector samples sensors (and the bar repaints), in seconds.
INTERVAL = float(os.environ.get("SYSMON_INTERVAL", "3.0"))

# How many samples to retain for the /metrics/history endpoint.
HISTORY = int(os.environ.get("SYSMON_HISTORY", "120"))

# Optional bearer token. Empty = no auth (correct for localhost-only binding).
# Set SYSMON_TOKEN to require `Authorization: Bearer <token>` on every request,
# which is what you'd do if you ever expose the API beyond localhost.
TOKEN = os.environ.get("SYSMON_TOKEN", "")

# --- Optional sources --------------------------------------------------------

# CoolerControl — the only source of fan RPMs / liquid temps on this box
# (no hwmon fan inputs exist). Supply your CoolerControl password via the env
# var below, or put {"cc_passwd": "..."} in ~/.config/sysmon/secrets.json
# (preferred — keeps it out of the systemd unit). Empty password disables it.
CC_URL = os.environ.get("SYSMON_CC_URL", "http://127.0.0.1:11987")
CC_USER = os.environ.get("SYSMON_CC_USER", "CCAdmin")

# NVMe SMART (drive wear / power-on hours / errors). Needs nvme-cli + the
# scoped sudoers rule in integrations/sudoers/. Polled slowly (it barely moves).
SMART_ENABLED = os.environ.get("SYSMON_SMART", "1") != "0"
SMART_INTERVAL = float(os.environ.get("SYSMON_SMART_INTERVAL", "60"))

# Top-N processes to report per dimension (cpu / ram / gpu).
TOP_N = int(os.environ.get("SYSMON_TOP_N", "5"))

# Heaviest-applications view (widget's expandable panel): how many app groups to
# report, and how many child PIDs to keep per app for the drill-down.
APPS_N = int(os.environ.get("SYSMON_APPS_N", "8"))
APPS_CHILDREN = int(os.environ.get("SYSMON_APPS_CHILDREN", "12"))
# The task panel spans the full-width bar, but its content is centered in a
# column this wide (px) so the first column clears a left/right dock instead of
# hiding behind it. Capped to the monitor width; tune to taste.
PANEL_WIDTH = int(os.environ.get("SYSMON_PANEL_WIDTH", "1320"))

# The top-processes scan is the heaviest part of a tick (an extra nvidia-smi
# spawn + a full process walk), so refresh it on a slower cadence than the core
# temps/power metrics and cache in between. Bump this to lean out further.
PROC_INTERVAL = float(os.environ.get("SYSMON_PROC_INTERVAL", "15.0"))

# Desktop bar geometry.
BAR_HEIGHT = int(os.environ.get("SYSMON_BAR_HEIGHT", "30"))
BAR_MONITOR = int(os.environ.get("SYSMON_BAR_MONITOR", "0"))  # which monitor
# Which screen edge to dock to: "top" or "bottom".
BAR_POSITION = "top" if os.environ.get("SYSMON_BAR_POSITION", "bottom").lower() == "top" else "bottom"
# Px to push a top bar down so it clears GNOME's own top bar. Empty = auto
# (read from the monitor workarea at startup).
_top_off = os.environ.get("SYSMON_BAR_TOP_OFFSET", "").strip()
BAR_TOP_OFFSET = int(_top_off) if _top_off else None
# Reserve screen space (real panel) vs float over windows. Reserving is riskier
# on multi-monitor GNOME, so default to an always-on-top overlay bar.
BAR_RESERVE = os.environ.get("SYSMON_BAR_RESERVE", "0") == "1"
# Dim the bar when the pointer isn't over it: fade to BAR_FADE_OPACITY after
# BAR_FADE_DELAY seconds without hover; hovering fades it back in. Durations
# are in ms. Set the delay to 0 to disable fading.
BAR_FADE_DELAY = float(os.environ.get("SYSMON_BAR_FADE_DELAY", "3.0"))
BAR_FADE_OPACITY = float(os.environ.get("SYSMON_BAR_FADE_OPACITY", "0.25"))
BAR_FADE_IN_MS = int(os.environ.get("SYSMON_BAR_FADE_IN_MS", "400"))
BAR_FADE_OUT_MS = int(os.environ.get("SYSMON_BAR_FADE_OUT_MS", "900"))

# --- Alert thresholds (drive the status rollup) ------------------------------
CPU_TEMP_WARN = float(os.environ.get("SYSMON_CPU_TEMP_WARN", "80"))
CPU_TEMP_CRIT = float(os.environ.get("SYSMON_CPU_TEMP_CRIT", "90"))
GPU_TEMP_WARN = float(os.environ.get("SYSMON_GPU_TEMP_WARN", "80"))
GPU_TEMP_CRIT = float(os.environ.get("SYSMON_GPU_TEMP_CRIT", "87"))
DISK_PCT_WARN = float(os.environ.get("SYSMON_DISK_PCT_WARN", "90"))
NVME_WEAR_WARN = float(os.environ.get("SYSMON_NVME_WEAR_WARN", "90"))


def _secret(key: str, default: str = "") -> str:
    """Read a secret from env first, then ~/.config/sysmon/secrets.json."""
    env_map = {"cc_passwd": "SYSMON_CC_PASSWD"}
    val = os.environ.get(env_map.get(key, ""), "")
    if val:
        return val
    try:
        import json
        path = os.path.expanduser("~/.config/sysmon/secrets.json")
        with open(path) as f:
            return json.load(f).get(key, default)
    except Exception:
        return default


CC_PASSWD = _secret("cc_passwd")
