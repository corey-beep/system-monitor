"""Optional standalone Telegram bot exposing PC stats.

Secondary to your existing agents (OpenClaw "buddy" / Hermes), which already
own Telegram — this is a token-only fallback so you can have a dedicated stats
bot. Set SYSMON_BOT_TOKEN (and optionally SYSMON_ALLOWED_CHAT for a chat-id
allowlist), then run: python -m sysmon.telegram_bot

Commands: /stats /cpu /gpu /power
"""
import os

import httpx
from telegram import Update
from telegram.ext import Application, CommandHandler, ContextTypes

from . import config

TOKEN = os.environ.get("SYSMON_BOT_TOKEN", "")
ALLOWED = {c.strip() for c in os.environ.get("SYSMON_ALLOWED_CHAT", "").split(",") if c.strip()}


def _get(path: str) -> dict:
    headers = {"Authorization": f"Bearer {config.TOKEN}"} if config.TOKEN else {}
    r = httpx.get(f"{config.BASE_URL}{path}", headers=headers, timeout=5)
    r.raise_for_status()
    return r.json()


def _fmt_stats(m: dict) -> str:
    cpu, mem, dsk, pwr = m.get("cpu", {}), m.get("memory", {}), m.get("disk", {}), m.get("power", {})
    gpus = m.get("gpu") or []
    lines = [
        f"🖥 *{m.get('host', {}).get('hostname', 'pc')}*",
        f"CPU  {cpu.get('util_pct')}%  {cpu.get('temp_c')}°C  {cpu.get('power_w')}W",
    ]
    for g in gpus:
        lines.append(
            f"GPU  {g.get('util_gpu_pct')}%  {g.get('temp_c')}°C  "
            f"{g.get('power_w')}/{g.get('power_limit_w')}W  "
            f"{g.get('mem_used_mb')}/{g.get('mem_total_mb')}MB"
        )
    lines.append(f"RAM  {mem.get('used_gb')}/{mem.get('total_gb')}GB ({mem.get('pct')}%)")
    lines.append(f"Disk r{dsk.get('read_mb_s')} w{dsk.get('write_mb_s')} MB/s  NVMe {dsk.get('nvme_c')}°C")
    lines.append(f"Power ~{pwr.get('total_w')}W (CPU {pwr.get('cpu_package_w')} + GPU {pwr.get('gpu_w')})")
    return "\n".join(lines)


def _allowed(update: Update) -> bool:
    if not ALLOWED:
        return True
    return str(update.effective_chat.id) in ALLOWED


def _make_handler(render):
    async def handler(update: Update, _ctx: ContextTypes.DEFAULT_TYPE):
        if not _allowed(update):
            await update.message.reply_text("⛔ not authorized")
            return
        try:
            text = render()
        except Exception as e:
            text = f"⚠️ sysmon daemon unreachable: {e}"
        await update.message.reply_text(text, parse_mode="Markdown")
    return handler


def main():
    if not TOKEN:
        raise SystemExit("Set SYSMON_BOT_TOKEN to run the Telegram bot.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("stats", _make_handler(lambda: _fmt_stats(_get("/metrics")))))
    app.add_handler(CommandHandler("start", _make_handler(lambda: _fmt_stats(_get("/metrics")))))
    app.add_handler(CommandHandler("cpu", _make_handler(lambda: f"```\n{_get('/metrics/cpu')}\n```")))
    app.add_handler(CommandHandler("gpu", _make_handler(lambda: f"```\n{_get('/metrics/gpu')}\n```")))
    app.add_handler(CommandHandler("power", _make_handler(lambda: f"```\n{_get('/metrics/power')}\n```")))
    app.run_polling()


if __name__ == "__main__":
    main()
