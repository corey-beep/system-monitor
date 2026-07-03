#!/usr/bin/env bash
# ExecStartPre guard for sysmond: guarantees the venv's pip deps are importable
# before the daemon launches.
#
# Why this exists: the venv is built with --system-site-packages and its pip
# deps live in .venv/lib/pythonX.Y/. An OS Python major bump (e.g. 3.13 -> 3.14)
# makes .venv/bin/python resolve to the new interpreter, whose site-packages dir
# doesn't exist, so it silently falls back to system packages — uvicorn/fastapi/
# mcp/xlib vanish and the daemon crash-loops on `import uvicorn`. This guard
# detects that and rebuilds the venv on demand so the daemon self-heals instead
# of restarting forever.
set -euo pipefail

DIR="${1:?usage: venv-guard.sh <project-dir>}"
PY="$DIR/.venv/bin/python"

# Serialize concurrent guards. Both the daemon (ExecStartPre) and the widget
# (XDG autostart) run this at login; without a lock they could rm -rf and
# rebuild the same .venv at the same time and corrupt it. flock makes the second
# caller wait, then fall through to the fast path against the now-healthy venv.
exec 9>"$DIR/.venv-guard.lock"
flock 9

# Fast path: venv exists and every core dep imports — nothing to do.
if [ -x "$PY" ] && "$PY" -c 'import uvicorn, fastapi, httpx, mcp, Xlib' 2>/dev/null; then
  exit 0
fi

echo "sysmond venv-guard: core deps unimportable — rebuilding venv against $(python3 --version)" >&2
rm -rf "$DIR/.venv"
python3 -m venv --system-site-packages "$DIR/.venv"
"$DIR/.venv/bin/pip" install -U pip >/dev/null
"$DIR/.venv/bin/pip" install -r "$DIR/requirements.txt"

# Fail loudly if the rebuild still can't import — better to surface a broken
# start than to hand a half-built venv to ExecStart.
"$PY" -c 'import uvicorn, fastapi, httpx, mcp, Xlib'
echo "sysmond venv-guard: rebuild complete" >&2
