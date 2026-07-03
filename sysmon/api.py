"""FastAPI app exposing the collector snapshot as JSON.

This is the universal substrate: the GTK widget, the MCP server, the OpenClaw
skill and the Telegram bot all read these endpoints. Bound to localhost by the
daemon (sysmon/__main__.py); set SYSMON_TOKEN to require bearer auth if you ever
expose it further.
"""
from contextlib import asynccontextmanager

from fastapi import FastAPI, Header, HTTPException

from . import config
from .collector import Collector

collector = Collector(interval=config.INTERVAL, history=config.HISTORY)


@asynccontextmanager
async def lifespan(_app: FastAPI):
    collector.start()
    yield
    collector.stop()


app = FastAPI(title="sysmon", version="1.0", lifespan=lifespan)


def _auth(authorization: str | None):
    if not config.TOKEN:
        return
    if authorization != f"Bearer {config.TOKEN}":
        raise HTTPException(status_code=401, detail="invalid or missing token")


@app.get("/health")
def health():
    return {"ok": True, "ts": collector.snapshot().get("ts")}


@app.get("/metrics")
def metrics(authorization: str | None = Header(default=None)):
    _auth(authorization)
    return collector.snapshot()


@app.get("/metrics/cpu")
def cpu(authorization: str | None = Header(default=None)):
    _auth(authorization)
    return collector.snapshot().get("cpu", {})


@app.get("/metrics/gpu")
def gpu(authorization: str | None = Header(default=None)):
    _auth(authorization)
    return {"gpu": collector.snapshot().get("gpu")}


@app.get("/metrics/power")
def power(authorization: str | None = Header(default=None)):
    _auth(authorization)
    return collector.snapshot().get("power", {})


@app.get("/metrics/temps")
def temps(authorization: str | None = Header(default=None)):
    _auth(authorization)
    return collector.snapshot().get("temps", {})


@app.get("/metrics/processes")
def processes(authorization: str | None = Header(default=None)):
    _auth(authorization)
    return collector.snapshot().get("processes", {})


@app.get("/metrics/fans")
def fans(authorization: str | None = Header(default=None)):
    _auth(authorization)
    return collector.snapshot().get("fans", {})


@app.get("/metrics/smart")
def smart(authorization: str | None = Header(default=None)):
    _auth(authorization)
    return {"smart": collector.snapshot().get("smart", [])}


@app.get("/metrics/history")
def history(authorization: str | None = Header(default=None)):
    _auth(authorization)
    return {"history": collector.history()}


@app.get("/status")
def status(authorization: str | None = Header(default=None)):
    """Operational view: overall status, alerts, per-source health, staleness.

    Lighter than /metrics — meant for 'is the box (and the monitor) OK?' checks.
    """
    _auth(authorization)
    snap = collector.snapshot()
    return {
        "status": snap.get("status"),
        "alerts": snap.get("alerts", []),
        "health": snap.get("health", {}),
    }
