---
name: pc-stats
description: Query live stats of Corey's desktop PC (CPU/GPU temps, utilization, power draw, RAM, disk) via the local sysmon service.
---

# PC Stats

Use this skill whenever Corey asks about the desktop PC's health, temperature,
load, power usage, or "how hard is it working". The `sysmon` daemon runs on the
PC and serves JSON on `http://127.0.0.1:8787`.

## How to answer

Fetch the relevant endpoint and summarize the numbers conversationally.

- Everything: `curl -s http://127.0.0.1:8787/metrics | jq`
- **Is the PC OK?**: `curl -s http://127.0.0.1:8787/status | jq` — overall
  ok/warn/critical, active alerts, and per-sensor health. Use this first for
  vague "how's the PC?" questions.
- CPU only: `curl -s http://127.0.0.1:8787/metrics/cpu | jq`
- GPU only: `curl -s http://127.0.0.1:8787/metrics/gpu | jq`  (incl. throttling, pstate, pcie)
- Power only: `curl -s http://127.0.0.1:8787/metrics/power | jq`
- Temps only: `curl -s http://127.0.0.1:8787/metrics/temps | jq`
- What's using it: `curl -s http://127.0.0.1:8787/metrics/processes | jq` (top by cpu/ram/gpu)
- Drive health: `curl -s http://127.0.0.1:8787/metrics/smart | jq`
- Fans: `curl -s http://127.0.0.1:8787/metrics/fans | jq`

## Field guide

- `cpu.temp_c` — CPU temperature (°C), `cpu.util_pct` — total CPU usage,
  `cpu.power_w` — CPU package power (watts; may be null if the RAPL udev rule
  isn't installed).
- `gpu[0].temp_c` / `.util_gpu_pct` / `.power_w` / `.power_limit_w` —
  NVIDIA RTX 3080 temp, usage, and power vs its limit.
- `power.total_w` — combined CPU + GPU draw (not whole-system; there's no PSU
  sensor).
- `memory.used_gb` / `.total_gb`, `disk.nvme_c` (drive temp), `temps.mobo_c`.

## Example

> Corey: "how hot is my GPU right now?"

Run `curl -s http://127.0.0.1:8787/metrics/gpu | jq '.gpu[0]'` and reply with
the temperature, e.g. "GPU is at 29°C, drawing 31W of its 380W limit, 6% load —
basically idle."

If the curl fails, the sysmon daemon is down: tell Corey to run
`systemctl --user start sysmond`.
