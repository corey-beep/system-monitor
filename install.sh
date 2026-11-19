#!/usr/bin/env bash
# Install sysmon: venv, deps, RAPL udev rule, daemon autostart, widget autostart.
set -euo pipefail

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$DIR"

echo "==> Creating venv (with system site-packages for psutil + GTK/gi)"
python3 -m venv --system-site-packages .venv
.venv/bin/pip install -U pip >/dev/null
.venv/bin/pip install -r requirements.txt

echo "==> Installing RAPL udev rule (needs sudo) so CPU power is readable"
if sudo cp integrations/udev/99-rapl-readable.rules /etc/udev/rules.d/; then
  sudo udevadm control --reload-rules
  # Trigger the 'add' action explicitly — the rule matches add (as fired at
  # boot); a bare `udevadm trigger` sends 'change' and wouldn't apply it now.
  sudo udevadm trigger --action=add --subsystem-match=powercap || true
  echo "    done (CPU power will report watts; skip/remove the rule to disable)"
else
  echo "    skipped — CPU power will report null until installed"
fi

echo "==> Installing GTK cairo bridge (panel charts and meters)"
if ! python3 -c "import gi; gi.require_foreign('cairo')" 2>/dev/null; then
  sudo apt-get install -y python3-gi-cairo || echo "    couldn't install python3-gi-cairo — panel charts won't draw"
fi

echo "==> Installing NVMe SMART support (nvme-cli + scoped sudoers)"
if ! command -v nvme >/dev/null; then
  sudo apt-get install -y nvme-cli || echo "    couldn't install nvme-cli — SMART will report needs_setup"
fi
if command -v nvme >/dev/null; then
  sed "s|@USER@|$USER|g" integrations/sudoers/sysmon-nvme > /tmp/sysmon-nvme
  sudo install -m 0440 -o root -g root /tmp/sysmon-nvme /etc/sudoers.d/sysmon-nvme && rm -f /tmp/sysmon-nvme
  sudo visudo -c -f /etc/sudoers.d/sysmon-nvme >/dev/null && echo "    sudoers rule OK" \
    || { echo "    BAD sudoers syntax — removing"; sudo rm -f /etc/sudoers.d/sysmon-nvme; }
fi

echo "==> CoolerControl fan RPMs (optional)"
echo "    Fans need your CoolerControl password. Set it once via:"
echo "      mkdir -p ~/.config/sysmon && cp integrations/secrets.example.json ~/.config/sysmon/secrets.json"
echo "      chmod 600 ~/.config/sysmon/secrets.json   # then edit cc_passwd"

echo "==> Installing systemd --user daemon service"
mkdir -p ~/.config/systemd/user
sed "s|@DIR@|$DIR|g" integrations/systemd/sysmond.service \
  > ~/.config/systemd/user/sysmond.service
systemctl --user daemon-reload
systemctl --user enable --now sysmond.service

echo "==> Installing widget autostart entry"
mkdir -p ~/.config/autostart
sed "s|@DIR@|$DIR|g" integrations/autostart/sysmon-widget.desktop \
  > ~/.config/autostart/sysmon-widget.desktop

echo "==> Starting widget now"
env GDK_BACKEND=x11 "$DIR/.venv/bin/python" -m sysmon.widget &

cat <<EOF

==> Done.

  API:        curl -s http://127.0.0.1:8787/metrics | jq
  Daemon:     systemctl --user status sysmond
  Widget:     restarts on login (XDG autostart); running now in background

  Register the MCP server with Claude Code:
    claude mcp add sysmon -- $DIR/.venv/bin/python -m sysmon.mcp_server

  For OpenClaw "buddy":
    cp -r integrations/openclaw/pc-stats ~/.openclaw/workspace/skills/

  Optional Telegram bot:
    SYSMON_BOT_TOKEN=xxxx $DIR/.venv/bin/python -m sysmon.telegram_bot
EOF
