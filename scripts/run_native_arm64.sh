#!/bin/bash
#
# 用原生 arm64 跑任意命令（替代手写 arch -arm64 前缀）
#
#   ./scripts/run_native_arm64.sh .venv/bin/python main.py
#   ./scripts/run_native_arm64.sh .venv/bin/python -m PyInstaller backend.spec
#   ./scripts/run_native_arm64.sh .venv/bin/python test_azure_tools.py
#
# 直接执行的目标进程一定是 arm64（含 arm64 切片才行）；
# 若目标只有 x86_64 切片，arch 会直接报 “Bad CPU type in executable”，不会退化成 Rosetta。

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-$0}")" && pwd)"
# shellcheck source=arm64_native.sh
source "$SCRIPT_DIR/arm64_native.sh"

if [ "$#" -eq 0 ]; then
    _arm64_die "用法：$(basename "$0") <命令> [参数...]（例如 $(basename "$0") .venv/bin/python main.py）"
fi

arm64_require_hardware

_arm64_log "以原生 arm64 执行：$*"
exec /usr/bin/arch -arm64 "$@"
