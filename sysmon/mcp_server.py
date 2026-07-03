"""MCP server exposing PC stats as agent tools (stdio transport).

Each tool fetches from the local sysmon HTTP API so there is exactly one place
that talks to sensors (the daemon). Register with:

  Claude Code:
    claude mcp add sysmon -- /path/to/.venv/bin/python -m sysmon.mcp_server

  Hermes (natively supports MCP servers): add the same command to its MCP config.
"""
import httpx

from mcp.server.fastmcp import FastMCP

from . import config

mcp = FastMCP("sysmon")


def _get(path: str) -> dict:
    headers = {"Authorization": f"Bearer {config.TOKEN}"} if config.TOKEN else {}
    try:
        r = httpx.get(f"{config.BASE_URL}{path}", headers=headers, timeout=5)
        r.raise_for_status()
        return r.json()
    except Exception as e:
        return {"error": f"sysmon daemon unreachable at {config.BASE_URL}: {e}"}


@mcp.tool()
def get_system_stats() -> dict:
    """Full snapshot of this PC: CPU, GPU, memory, disk, power and temperatures."""
    return _get("/metrics")


@mcp.tool()
def get_cpu() -> dict:
    """CPU model, per-core and total utilization, frequency, load average,
    temperature and package power draw."""
    return _get("/metrics/cpu")


@mcp.tool()
def get_gpu() -> dict:
    """NVIDIA GPU stats: temperature, power draw vs limit, GPU/memory
    utilization, VRAM used/total, core clock and fan speed."""
    return _get("/metrics/gpu")


@mcp.tool()
def get_power() -> dict:
    """Power draw in watts: CPU package, CPU core, GPU, and their combined total.
    (No whole-system PSU sensor exists; this is the two biggest loads.)"""
    return _get("/metrics/power")


@mcp.tool()
def get_temperatures() -> dict:
    """All temperatures: CPU (Tctl + per-CCD), NVMe drive, motherboard, and the
    raw per-chip sensor readings."""
    return _get("/metrics/temps")


@mcp.tool()
def get_status() -> dict:
    """Operational health: overall status (ok/warn/critical), the list of active
    alerts (hot temps, GPU throttling, degraded PCIe link, disk full, drive
    wear, stale data), per-sensor-source health (why a field might be missing),
    and collector self-stats. Use this for 'is the PC OK?' questions."""
    return _get("/status")


@mcp.tool()
def get_processes() -> dict:
    """Top processes right now by CPU%, by RAM, and by GPU memory.
    Answers 'what's using the most CPU/RAM/GPU?'."""
    return _get("/metrics/processes")


@mcp.tool()
def get_fans() -> dict:
    """Fan speeds: RPM + duty for case/CPU/GPU fans (via CoolerControl), GPU fan
    percentage, and liquid-cooler temps. Fan RPM needs CoolerControl credentials
    configured; check the 'status' field if rpm is empty."""
    return _get("/metrics/fans")


@mcp.tool()
def get_smart() -> dict:
    """NVMe SMART health per drive: wear %, available spare, power-on hours,
    unsafe shutdowns, media errors, temperature, and TB written/read."""
    return _get("/metrics/smart")


def main():
    mcp.run()


if __name__ == "__main__":
    main()
