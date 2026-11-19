"""In-process NVIDIA GPU reads via NVML (the library nvidia-smi itself uses).

Spawning nvidia-smi costs ~25 ms of fork/exec + driver setup per call; NVML
calls on an open handle take microseconds. Everything here degrades to
`available = False` (callers then fall back to nvidia-smi) if the
`nvidia-ml-py` package or the driver library is missing.
"""
from __future__ import annotations

try:
    import pynvml as _n
except ImportError:  # pragma: no cover - optional dependency
    _n = None

_handles: list | None = None
_driver: str | None = None


def _init() -> bool:
    global _handles, _driver
    if _handles is not None:
        return bool(_handles)
    _handles = []
    if _n is None:
        return False
    try:
        _n.nvmlInit()
        _handles = [_n.nvmlDeviceGetHandleByIndex(i)
                    for i in range(_n.nvmlDeviceGetCount())]
        _driver = _str(_n.nvmlSystemGetDriverVersion())
    except Exception:
        _handles = []
    return bool(_handles)


def available() -> bool:
    return _init()


def _str(v):
    return v.decode() if isinstance(v, bytes) else v


def _try(fn, *args):
    try:
        return fn(*args)
    except Exception:
        return None


def _throttle_reasons(h):
    fn = getattr(_n, "nvmlDeviceGetCurrentClocksEventReasons", None) \
        or _n.nvmlDeviceGetCurrentClocksThrottleReasons
    mask = _try(fn, h)
    if mask is None:
        return False, []
    labels = (
        (_n.nvmlClocksThrottleReasonSwPowerCap, "sw_power_cap"),
        (_n.nvmlClocksThrottleReasonHwSlowdown, "hw_slowdown"),
        (_n.nvmlClocksThrottleReasonHwThermalSlowdown, "hw_thermal"),
        (_n.nvmlClocksThrottleReasonSwThermalSlowdown, "sw_thermal"),
    )
    return (bool(mask & _n.nvmlClocksThrottleReasonGpuIdle),
            [name for bit, name in labels if mask & bit])


def read_gpus() -> list | None:
    """Same schema as the nvidia-smi parser in collector.py."""
    if not _init():
        return None
    n = _n
    out = []
    for h in _handles:
        util = _try(n.nvmlDeviceGetUtilizationRates, h)
        mem = _try(n.nvmlDeviceGetMemoryInfo, h, n.nvmlMemory_v2) \
            or _try(n.nvmlDeviceGetMemoryInfo, h)
        power = _try(n.nvmlDeviceGetPowerUsage, h)
        limit = _try(n.nvmlDeviceGetPowerManagementLimit, h)
        pstate = _try(n.nvmlDeviceGetPerformanceState, h)
        enc = _try(n.nvmlDeviceGetEncoderUtilization, h)
        dec = _try(n.nvmlDeviceGetDecoderUtilization, h)
        idle, throttling = _throttle_reasons(h)

        def clock(kind, getter=n.nvmlDeviceGetClockInfo):
            v = _try(getter, h, kind)
            return float(v) if v is not None else None

        out.append({
            "name": _str(_try(n.nvmlDeviceGetName, h)) or "NVIDIA GPU",
            "temp_c": _try(n.nvmlDeviceGetTemperature, h, n.NVML_TEMPERATURE_GPU),
            "power_w": round(power / 1000, 2) if power is not None else None,
            "power_limit_w": round(limit / 1000, 2) if limit is not None else None,
            "util_gpu_pct": float(util.gpu) if util else None,
            "util_mem_pct": float(util.memory) if util else None,
            "mem_used_mb": round(mem.used / 2**20) if mem else None,
            "mem_total_mb": round(mem.total / 2**20) if mem else None,
            "clock_mhz": clock(n.NVML_CLOCK_GRAPHICS),
            "clock_sm_mhz": clock(n.NVML_CLOCK_SM),
            "clock_mem_mhz": clock(n.NVML_CLOCK_MEM),
            "fan_pct": _try(n.nvmlDeviceGetFanSpeed, h),
            "clock_max_mhz": clock(n.NVML_CLOCK_GRAPHICS, n.nvmlDeviceGetMaxClockInfo),
            "clock_mem_max_mhz": clock(n.NVML_CLOCK_MEM, n.nvmlDeviceGetMaxClockInfo),
            "clock_video_mhz": clock(n.NVML_CLOCK_VIDEO),
            "util_enc_pct": float(enc[0]) if enc else None,
            "util_dec_pct": float(dec[0]) if dec else None,
            "driver": _driver,
            "pstate": f"P{pstate}" if pstate is not None and pstate < 32 else None,
            "pcie": {
                "gen_cur": _try(n.nvmlDeviceGetCurrPcieLinkGeneration, h),
                "gen_max": _try(n.nvmlDeviceGetMaxPcieLinkGeneration, h),
                "width_cur": _try(n.nvmlDeviceGetCurrPcieLinkWidth, h),
            },
            "throttle_idle": idle,
            "throttling": throttling,
        })
    return out or None


def gpu_procs() -> dict[int, float] | None:
    """pid -> GPU memory MiB for compute processes (like nvidia-smi
    --query-compute-apps). None when NVML isn't usable."""
    if not _init():
        return None
    out: dict[int, float] = {}
    for h in _handles:
        for p in _try(_n.nvmlDeviceGetComputeRunningProcesses, h) or []:
            mb = p.usedGpuMemory
            if mb is not None:
                out[p.pid] = out.get(p.pid, 0.0) + round(mb / 2**20, 1)
    return out
