#!/bin/bash
#
# 原生 arm64 工具库（给 build 脚本 source 用）
#
# 本项目只发布 Apple Silicon 原生产物，全程不使用 Rosetta：
#   1. arm64_require_hardware  —— 机器必须是能原生跑 arm64 的 Apple Silicon
#   2. arm64_require_native_shell —— 当前 shell 必须原生 arm64（被 Rosetta 翻译就中止）
#   3. arm64_assert_binary     —— 产物必须含 arm64 且不含 x86_64
#   4. arm64_assert_bundle     —— 目录（App Bundle）里不允许出现 x86_64
#
# 用法（在脚本里）：
#   source "$(dirname "$0")/arm64_native.sh"
#   arm64_require_native_shell
#
# 说明：为什么不是“检测到 Rosetta 就用 arch -arm64 重跑”？
#   因为 Rosetta 的翻译状态会被子进程继承：`arch -arm64 bash` 里的 bash 虽然是原生，
#   但它 fork/exec 出来的 python / xcodebuild 又会回到 x86_64。脚本自己无法把整条
#   进程链拉回原生，只能要求用户在一个没被翻译的终端里跑。
#   自动化场景（例如 CI 里已用 lipo 兜底校验产物）可用 AICHAT_ARM64_OVERRIDE=1 降级为警告。

set -euo pipefail

ARM64_XCODEBUILD_FLAGS=(ARCHS=arm64 ONLY_ACTIVE_ARCH=NO EXCLUDED_ARCHS=x86_64)

_arm64_log() { printf '\033[0;36m[arm64]\033[0m %s\n' "$*"; }
_arm64_warn() { printf '\033[1;33m[arm64] warning:\033[0m %s\n' "$*" >&2; }
_arm64_die() { printf '\033[0;31m[arm64] error:\033[0m %s\n' "$*" >&2; exit 1; }

# 机器本身能否原生执行 arm64（Intel 机器会失败）
arm64_require_hardware() {
    if ! /usr/bin/arch -arm64 /usr/bin/true >/dev/null 2>&1; then
        _arm64_die "这台机器不能原生执行 arm64（需要 Apple Silicon）。本项目只支持 arm64 原生，不接受 Rosetta。"
    fi
}

# 当前进程是否被 Rosetta 翻译（Apple Silicon 上跑 x86_64 进程）
arm64_is_translated() {
    [ "$(/usr/bin/uname -m)" = "x86_64" ]
}

# 当前 shell 必须原生 arm64；被 Rosetta 翻译时中止并给出修复步骤
arm64_require_native_shell() {
    arm64_require_hardware

    if arm64_is_translated; then
        if [ "${AICHAT_ARM64_OVERRIDE:-0}" = "1" ]; then
            _arm64_warn "当前 shell 是 x86_64（Rosetta 翻译）；AICHAT_ARM64_OVERRIDE=1 已放行，产物请用 lipo 复核。"
            return 0
        fi
        cat >&2 <<'EOF'
[arm64] error: 当前 shell 被 Rosetta 翻译（uname -m = x86_64），拒绝用它编译/打包。

本项目只做 Apple Silicon 原生 arm64，不接受 Rosetta。修复方式：
  1) 访达 → 应用程序 → 终端（或 iTerm）→ 显示简介 → 取消勾选「使用 Rosetta 打开」，
     然后完全退出并重开终端，确认 `uname -m` 输出 arm64。
  2) 确保用的是原生 arm64 的 Homebrew：/opt/homebrew/bin（而不是 /usr/local/bin）。
     检查：`which node python3 brew`；如果指向 /usr/local，换到 /opt/homebrew 下重装。
  3) 若终端是原生但仍被翻译，多半是启动它的父进程（IDE / 脚本 / Codex CLI）是 x86_64，
     换成 arm64 版本或直接从原生终端启动。

确需在此环境构建（例如 CI，产物另有 lipo 校验）时可显式放行：
  AICHAT_ARM64_OVERRIDE=1 ./scripts/...
EOF
        exit 1
    fi
    _arm64_log "运行架构：$(/usr/bin/uname -m)（原生 arm64，未使用 Rosetta）"
}

# 校验单个 Mach-O：必须含 arm64，且不含 x86_64
arm64_assert_binary() {
    local path="$1"
    [ -e "$path" ] || _arm64_die "找不到产物：$path"

    local archs
    archs="$(/usr/bin/lipo -archs "$path" 2>/dev/null || true)"
    [ -n "$archs" ] || _arm64_die "不是 Mach-O 可执行文件：$path"

    case " $archs " in
        *" arm64 "*) ;;
        *) _arm64_die "$path 不含 arm64 切片（实际：$archs）" ;;
    esac
    case " $archs " in
        *" x86_64 "*) _arm64_die "$path 含 x86_64 切片（实际：$archs）——本项目只发布纯 arm64" ;;
    esac

    _arm64_log "OK 纯 arm64：${path}（${archs}）"
}

# 递归校验目录下所有 Mach-O 文件都不含 x86_64
arm64_assert_bundle() {
    local dir="$1"
    [ -d "$dir" ] || _arm64_die "找不到目录：$dir"

    local offenders
    offenders="$(find "$dir" -type f -exec /bin/sh -c '
        for f do
            case "$(/usr/bin/file -b "$f" 2>/dev/null)" in
                *Mach-O*) /usr/bin/lipo -archs "$f" 2>/dev/null | /usr/bin/grep -q x86_64 && echo "$f" ;;
            esac
        done' sh {} + 2>/dev/null || true)"

    if [ -n "$offenders" ]; then
        printf '%s\n' "$offenders" >&2
        _arm64_die "上面这些文件含 x86_64 切片：$dir"
    fi
    _arm64_log "OK 目录内无 x86_64：$dir"
}
