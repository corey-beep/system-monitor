# sysmon

A desktop PC-stats widget **and** an agent-queryable metrics service for Linux.

One collector samples every sensor on the box and serves it as JSON on
`localhost`. A translucent bar docked at the bottom of the screen displays it,
and your agents (Claude Code, Hermes via MCP; OpenClaw "buddy" via HTTP) read the
same data — so "what's my GPU temp?" works over Telegram and in any agent.

Built and verified on: Ubuntu 25.10, GNOME/Wayland, Ryzen 7 5800XT, RTX 3080.

## What it reports

- **CPU** — model, total + per-core utilization, frequency, load average,
  temperature (k10temp Tctl + per-CCD), package power (RAPL)
- **GPU** — temp, power draw vs limit, GPU/VRAM utilization, VRAM used/total,
  core clock, fan % (NVIDIA via `nvidia-smi`)
- **Memory / swap**, **disk** throughput + root usage + NVMe temp,
  **motherboard** temp, **power** (CPU + GPU watts), uptime/host info
- `/metrics/history` keeps the last ~120 samples for trends

**Operational / debug data:**
- **Status rollup** — overall `ok`/`warn`/`critical` + an `alerts[]` list (hot
  temps, GPU throttling, degraded PCIe link, disk full, drive wear, stale data)
- **Self-health** — per-sensor-source status (so a `null` field explains *why*:
  `permission_denied`, `timeout`, `no_credentials`…), staleness, sample latency,
  collector uptime/error counters
- **GPU detail** — throttle reasons, pstate, PCIe link gen/width (current vs max),
  SM/memory clocks
- **CPU detail** — per-core frequency + boost state
- **Top processes** by CPU, RAM, and GPU memory
- **NVMe SMART** — wear %, spare, power-on hours, unsafe shutdowns, errors, TB written
- **Fan RPMs** — case/CPU/GPU fans + liquid temps via CoolerControl

## Install

```bash
./install.sh
```

This creates a venv, installs deps, adds the RAPL udev rule (sudo, one-time, for
CPU power), starts the daemon as a `systemd --user` service, and adds the widget
to login autostart. Then follow the printed commands to register the MCP server.

## Architecture

```
                 ┌──────────────────────────┐
   sensors  ───► │  collector (1 source of   │
 (psutil,        │  truth, samples @2s)      │
  nvidia-smi,    └─────────────┬─────────────┘
  RAPL, libsensors)            │
                  FastAPI @ 127.0.0.1:8787  (GET /metrics, /cpu, /gpu, /power, …)
                               │
        ┌──────────────┬───────┴───────┬──────────────────┐
   GTK4 widget     MCP server      OpenClaw skill    Telegram bot
   (XWayland)    (Hermes / Claude)   ("buddy")        (optional)
```

## Querying it

```bash
curl -s http://127.0.0.1:8787/metrics            | jq        # everything
curl -s http://127.0.0.1:8787/status             | jq        # ok/warn/critical + alerts + source health
curl -s http://127.0.0.1:8787/metrics/gpu        | jq '.gpu[0]'   # incl. throttle/pstate/pcie
curl -s http://127.0.0.1:8787/metrics/processes  | jq        # top by cpu/ram/gpu
curl -s http://127.0.0.1:8787/metrics/smart      | jq        # NVMe health
curl -s http://127.0.0.1:8787/metrics/fans       | jq        # fan RPMs (needs CoolerControl pass)
curl -s http://127.0.0.1:8787/metrics/power      | jq
```

### Agents

- **Claude Code / Hermes (MCP):**
  ```bash
  claude mcp add sysmon -- "$PWD/.venv/bin/python" -m sysmon.mcp_server
  ```
  Tools: `get_system_stats`, `get_cpu`, `get_gpu`, `get_power`, `get_temperatures`,
  `get_status`, `get_processes`, `get_fans`, `get_smart`.
  For Hermes, point its MCP config at the same command.

- **OpenClaw "buddy":**
  ```bash
  cp -r integrations/openclaw/pc-stats ~/.openclaw/workspace/skills/
  ```
  Then ask buddy "how hot is my CPU?" — it curls the API.

- **Standalone Telegram bot (optional):**
  ```bash
  SYSMON_BOT_TOKEN=<token> .venv/bin/python -m sysmon.telegram_bot
  ```
  Commands: `/stats /cpu /gpu /power`. Restrict with `SYSMON_ALLOWED_CHAT=<id>`.

## Configuration (env vars)

| Var | Default | Meaning |
|-----|---------|---------|
| `SYSMON_HOST` / `SYSMON_PORT` | `127.0.0.1` / `8787` | API bind address |
| `SYSMON_TOKEN` | _(empty)_ | require `Authorization: Bearer` — set this if you ever bind beyond localhost |
| `SYSMON_CC_PASSWD` | _(empty)_ | CoolerControl password → enables fan RPMs (or put `cc_passwd` in `~/.config/sysmon/secrets.json`) |
| `SYSMON_SMART` / `SYSMON_SMART_INTERVAL` | `1` / `60` | NVMe SMART on/off and poll seconds |
| `SYSMON_CPU_TEMP_WARN` / `_CRIT`, `SYSMON_GPU_TEMP_WARN` / `_CRIT` | 80/90, 80/87 | alert thresholds (°C) |
| `SYSMON_INTERVAL` / `SYSMON_PROC_INTERVAL` | `3` / `15` | core sample seconds / slower top-process scan seconds |
| `SYSMON_BAR_HEIGHT` / `SYSMON_BAR_MONITOR` / `SYSMON_BAR_RESERVE` | `30` / `0` / `0` | bar px height / which monitor / reserve screen space (1) |

### Fan RPMs (CoolerControl)

There are no hwmon fan inputs on this board, so fan RPM (incl. the GPU fan) comes
from your running CoolerControl daemon, which needs a login. Set your password once:

```bash
mkdir -p ~/.config/sysmon
cp integrations/secrets.example.json ~/.config/sysmon/secrets.json
chmod 600 ~/.config/sysmon/secrets.json   # edit cc_passwd, then: systemctl --user restart sysmond
```

Until then `/metrics/fans` reports `status: "no_credentials"` and you still get the
GPU fan **percentage** (from nvidia-smi). NVMe SMART needs `nvme-cli` + the scoped
sudoers rule in `integrations/sudoers/` (both handled by `install.sh`).

## Two platform gotchas (by design)

1. **CPU power needs the udev rule.** `/sys/.../intel-rapl:0/energy_uj` is
   root-only by default, so `cpu.power_w` is `null` until
   `integrations/udev/99-rapl-readable.rules` is installed (done by `install.sh`).
   GPU power needs nothing.

2. **The bar runs under XWayland.** GNOME/Wayland won't let a client dock itself
   or stay above other windows, so the bar forces `GDK_BACKEND=x11` and sets
   EWMH hints — `_NET_WM_WINDOW_TYPE_DOCK`, above + sticky + skip-taskbar, sized
   to span the bottom of the monitor (and `_NET_WM_STRUT` to reserve space when
   `SYSMON_BAR_RESERVE=1`). If `python-xlib` is missing it still renders, just
   undocked. Tune with `SYSMON_BAR_HEIGHT` / `SYSMON_BAR_MONITOR`.

## Manual run (no install)

```bash
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m sysmon                 # daemon (API)
env GDK_BACKEND=x11 .venv/bin/python -m sysmon.widget   # widget
```
