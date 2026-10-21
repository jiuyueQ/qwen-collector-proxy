#!/usr/bin/env bash
# Linux/nohup deployment. For systemd deployments use systemctl restart instead.
set -euo pipefail
umask 0077
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ -f collector.env ]]; then
  set -a
  source ./collector.env
  set +a
fi
PYTHON_BIN="${PYTHON_BIN:-$PWD/.conda-env/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "找不到 Python: $PYTHON_BIN" >&2
  exit 1
fi
mkdir -p logs
exec 9>logs/restart.lock
if ! flock -n 9; then
  echo "另一个代理重启操作正在执行。" >&2
  exit 1
fi

# Discover old manual launches without PID files. Only Python processes serving
# THIS checkout are eligible for SIGTERM; never kill a model or a port owner.
"$PYTHON_BIN" - "$PWD" <<'PY'
import os
from pathlib import Path
import signal
import sys
import time

project = Path(sys.argv[1]).resolve()
target = project / "collector_proxy.py"

def matches(pid):
    try:
        process = Path("/proc") / str(pid)
        args = [os.fsdecode(arg) for arg in (process / "cmdline").read_bytes().split(b"\0") if arg]
        if len(args) < 3 or not Path(args[0]).name.startswith("python"):
            return False
        if args[2] != "serve":
            return False
        script = Path(args[1])
        if not script.is_absolute():
            script = (process / "cwd").resolve() / script
        return script.resolve() == target
    except (OSError, ValueError):
        return False

pids = [int(p.name) for p in Path("/proc").iterdir() if p.name.isdigit() and matches(int(p.name))]
for pid in pids:
    if matches(pid):
        print(f"停止本项目采集代理 PID={pid}", flush=True)
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
deadline = time.monotonic() + 45
while any(matches(pid) for pid in pids):
    if time.monotonic() >= deadline:
        raise SystemExit("旧代理未在 45 秒内退出；本次不启动新进程，请查看日志。")
    time.sleep(0.5)
PY

nohup bash "$PWD/start_proxy.sh" >> "$PWD/logs/proxy.log" 2>&1 < /dev/null 9>&- &
proxy_pid=$!
printf '%s\n' "$proxy_pid" > logs/proxy.pid
sleep 2
if ! kill -0 "$proxy_pid" 2>/dev/null; then
  echo "代理启动失败，请检查 logs/proxy.log" >&2
  tail -n 40 logs/proxy.log
  exit 1
fi
echo "已后台启动采集代理 PID=$proxy_pid；日志: $PWD/logs/proxy.log"
echo "存活检查: curl -fsS http://127.0.0.1:${PROXY_PORT:-9000}/healthz"
