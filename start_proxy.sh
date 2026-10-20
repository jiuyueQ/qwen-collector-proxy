#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ -f collector.env ]]; then
  set -a
  source ./collector.env
  set +a
fi
PYTHON_BIN="${PYTHON_BIN:-$PWD/.conda-env/bin/python}"
if [[ ! -x "$PYTHON_BIN" ]]; then
  echo "找不到 Python: $PYTHON_BIN。请按 README 创建 .conda-env，或设置 PYTHON_BIN。" >&2
  exit 1
fi
arguments=(serve --host "${PROXY_HOST:-127.0.0.1}" --port "${PROXY_PORT:-9000}"
  --upstream "${UPSTREAM_BASE_URL:-http://127.0.0.1:8000/v1}"
  --data-dir "${COLLECTOR_DATA_DIR:-$PWD/data}"
  --read-timeout "${UPSTREAM_READ_TIMEOUT:-600}")
if [[ "${INCLUDE_REASONING:-0}" == "1" ]]; then
  arguments+=(--include-reasoning)
fi
exec "$PYTHON_BIN" collector_proxy.py "${arguments[@]}" "$@"
