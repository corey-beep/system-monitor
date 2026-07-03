"""Daemon entry point: `python -m sysmon`.

Serves the FastAPI app (which starts the collector thread via its lifespan)
on the configured host/port. Run under a systemd --user service for autostart.
"""
import uvicorn

from . import config


def main():
    uvicorn.run(
        "sysmon.api:app",
        host=config.HOST,
        port=config.PORT,
        log_level="warning",
    )


if __name__ == "__main__":
    main()
