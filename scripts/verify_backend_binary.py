#!/usr/bin/env python3
"""
验证打包产物 dist/backend_server（只用标准库，可用系统 Python 运行）：

  1. 在“干净环境变量”下运行它（不激活 .venv、不继承 PYTHONPATH/VIRTUAL_ENV）
  2. 确认它监听 PORT 指定的端口，并能响应 GET /api/health
  3. 用 SIGTERM 停止，并确认没有残留进程 / 僵尸进程

用法：
  python3 scripts/verify_backend_binary.py                 # 自动挑一个空闲端口
  python3 scripts/verify_backend_binary.py --port 8123
  python3 scripts/verify_backend_binary.py --binary dist/backend_server
"""

from __future__ import annotations

import argparse
import http.client
import json
import os
import signal
import socket
import subprocess
import sys
import time
from pathlib import Path

PROJECT_DIR = Path(__file__).resolve().parent.parent
DEFAULT_BINARY = PROJECT_DIR / "dist" / "backend_server"


def free_port() -> int:
    with socket.socket() as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def clean_environment(port: int) -> dict[str, str]:
    """只给最低限度的变量：不继承 venv / PYTHONPATH，模拟普通用户机器"""
    return {
        "PATH": "/usr/bin:/bin:/usr/sbin:/sbin",
        "HOME": os.path.expanduser("~"),  # SQLite 与 az login 缓存需要
        "USER": os.environ.get("USER", "user"),
        "LANG": os.environ.get("LANG", "en_US.UTF-8"),
        "PORT": str(port),
        "HOST": "127.0.0.1",
        "LOG_LEVEL": "warning",
    }


def http_get(port: int, path: str, timeout: float = 5.0) -> tuple[int, str]:
    connection = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        connection.request("GET", path)
        response = connection.getresponse()
        return response.status, response.read().decode("utf-8", errors="replace")
    finally:
        connection.close()


def wait_for_health(port: int, timeout: float) -> tuple[int, str]:
    deadline = time.time() + timeout
    last_error = "超时前没有任何响应"
    while time.time() < deadline:
        try:
            status, body = http_get(port, "/api/health")
            if status == 200:
                return status, body
            last_error = f"HTTP {status}: {body[:200]}"
        except Exception as exc:  # 进程还在解包/启动
            last_error = f"{type(exc).__name__}: {exc}"
        time.sleep(0.5)
    raise TimeoutError(f"等待 /api/health 超时（{timeout:.0f}s）：{last_error}")


def processes_matching(keyword: str) -> list[str]:
    """返回 cmdline 里包含 keyword 的进程（ps 输出，排除自己）"""
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,stat=,command="],
        capture_output=True, text=True, check=True,
    )
    own_pid = str(os.getpid())
    ignore_markers = ("ps -eo", "verify_backend_binary", "grep")
    matches = []
    for line in result.stdout.splitlines():
        parts = line.strip().split(None, 3)
        if len(parts) < 4:
            continue
        pid, _, stat, command = parts
        if pid == own_pid or any(marker in command for marker in ignore_markers):
            continue
        if keyword in command:
            matches.append(f"pid={pid} stat={stat} cmd={command[:120]}")
    return matches


def zombie_processes() -> list[str]:
    result = subprocess.run(
        ["ps", "-eo", "pid=,ppid=,stat=,command="],
        capture_output=True, text=True, check=True,
    )
    return [
        line.strip()[:120]
        for line in result.stdout.splitlines()
        if line.strip().split(None, 2)[-2].startswith("Z")
    ]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--binary",
        default=str(DEFAULT_BINARY),
        help="可执行文件路径，或 onedir 目录（目录里叫 backend_server）",
    )
    parser.add_argument("--port", type=int, default=0, help="0 表示自动选一个空闲端口")
    parser.add_argument("--timeout", type=float, default=90.0, help="等待 health 的最长秒数")
    parser.add_argument(
        "--require-arch",
        default="arm64",
        help="要求产物包含的架构（默认 arm64，本项目只发布 Apple Silicon 原生；传空字符串可跳过）",
    )
    args = parser.parse_args()

    binary = Path(args.binary).resolve()
    if binary.is_dir():
        # onedir 布局：目录/backend_server
        binary = binary / "backend_server"
    failures: list[str] = []

    def check(label: str, ok: bool, detail: str = "") -> None:
        print(("OK   " if ok else "FAIL ") + label + ("" if ok else f"  <-- {detail}"))
        if not ok:
            failures.append(label)

    print(f"二进制: {binary}")
    check("文件存在", binary.exists(), binary)
    check("有可执行权限", os.access(binary, os.X_OK), oct(binary.stat().st_mode) if binary.exists() else "-")
    if not binary.exists() or not os.access(binary, os.X_OK):
        return 1

    size_mb = binary.stat().st_size / 1024 / 1024
    print(f"体积: {size_mb:.1f} MB")

    # 架构检查：本项目只发 arm64，且不允许混入 x86_64
    archs = ""
    try:
        archs = subprocess.run(
            ["lipo", "-archs", str(binary)], capture_output=True, text=True, check=True
        ).stdout.strip()
    except Exception as exc:
        print(f"（跳过架构检查：{exc}）")

    if archs:
        print(f"架构: {archs}")
        if args.require_arch:
            check(f"包含 {args.require_arch} 切片", args.require_arch in archs.split(), archs)
            if args.require_arch == "arm64":
                check("不含 x86_64 切片（纯 Apple Silicon）", "x86_64" not in archs.split(), archs)

    port = args.port or free_port()
    environment = clean_environment(port)
    print(f"端口: {port}")
    print(f"干净环境变量: {json.dumps(environment, ensure_ascii=False)}")
    check("环境里没有 VIRTUAL_ENV", "VIRTUAL_ENV" not in environment)
    check("环境里没有 PYTHONPATH", "PYTHONPATH" not in environment)

    # start_new_session=True → 单独进程组，方便连同 PyInstaller 子进程一起收掉
    log_path = PROJECT_DIR / "build" / "verify_backend_server.log"
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log_file = open(log_path, "w", encoding="utf-8")

    process = subprocess.Popen(
        [str(binary)],
        cwd=str(binary.parent),
        env=environment,
        stdout=log_file,
        stderr=log_file,
        start_new_session=True,
    )
    pgid = os.getpgid(process.pid)
    print(f"已启动 pid={process.pid} pgid={pgid}（stdout/stderr → {log_path}）")

    try:
        status, body = wait_for_health(port, args.timeout)
        health = json.loads(body)
        print(f"\nGET /api/health → HTTP {status}")
        print(json.dumps(health, ensure_ascii=False, indent=2)[:900])

        check("health 返回 200", status == 200)
        check("status == ok", health.get("status") == "ok", health.get("status"))
        check("storage 有值", health.get("storage") in ("sqlite", "memory"), health.get("storage"))
        check("python.executable 指向打包产物", "backend_server" in str(health.get("python", {}).get("executable", "")), health.get("python"))
        registered = health.get("tools", {}).get("tools", [])
        # azure 21 + meraki 45 + nexus dashboard 31 + container 3 + ai 1 = 101
        check("工具已注册（101 个）", len(registered) == 101, len(registered))
        check(
            "关键工具都在列",
            {
                "list_storage_accounts", "query_resources", "get_webapp_metrics",
                "meraki_get_firewall_l3_rules",
                "nexus_infra_cluster_health", "nexus_manage_fabrics", "nexus_overview",
            } <= set(registered),
            sorted(set(registered))[:6],
        )
        check(
            "Nexus Dashboard 工具组已注册",
            health.get("nexus_dashboard", {}).get("tool_count") == 31,
            health.get("nexus_dashboard", {}).get("tool_count"),
        )
        providers = health.get("ai", {}).get("providers", {})
        check("AI provider 含 openai", "openai" in providers, sorted(providers))

        azure = health.get("azure", {})
        check("azure SDK 已打包进二进制", azure.get("sdk_available") is True, azure.get("sdk_error"))
        print(f"azure.credential_source = {azure.get('credential_source')}")
    except Exception as exc:
        check("能响应 /api/health", False, str(exc))
        tail = ""
        try:
            tail = log_path.read_text(encoding="utf-8", errors="replace")[-500:]
        except OSError:
            pass
        if tail:
            print(f"二进制输出（末尾 500 字符）：\n{tail}")

    print("\n停止进程（SIGTERM）…")
    stop_process(process, pgid)
    log_file.close()
    try:
        process.wait(timeout=15)
    except subprocess.TimeoutExpired:
        force_kill(process, pgid)
        process.wait(timeout=10)
        check("SIGTERM 能正常退出", False, "需要 SIGKILL 才退出")
    if "SIGTERM 能正常退出" not in failures:
        check("SIGTERM 能正常退出", True)
    print(f"退出码: {process.returncode}")

    # 端口应已释放
    time.sleep(0.5)
    try:
        with socket.socket() as sock:
            sock.settimeout(1.0)
            released = sock.connect_ex(("127.0.0.1", port)) != 0
    except Exception:
        released = True
    check("端口已释放", released, f"127.0.0.1:{port} 仍可连接")

    # 只匹配“本次启动的那个二进制路径”，避免把别处安装的 App 内嵌后端（例如
    # /Applications/AIChatApp.app/Contents/MacOS/backend_server）当成残留
    leftovers = processes_matching(str(binary))
    check("没有残留的 backend_server 进程", not leftovers, leftovers[:3])

    zombies = zombie_processes()
    check("没有僵尸进程", not zombies, zombies[:3])

    print("\n" + ("全部通过" if not failures else f"失败项: {failures}"))
    return 1 if failures else 0


def stop_process(process: subprocess.Popen, pgid: int) -> None:
    """先给整个进程组 SIGTERM；权限不足时退回只发给直接子进程"""
    try:
        os.killpg(pgid, signal.SIGTERM)
    except (PermissionError, ProcessLookupError):
        process.terminate()


def force_kill(process: subprocess.Popen, pgid: int) -> None:
    try:
        os.killpg(pgid, signal.SIGKILL)
    except (PermissionError, ProcessLookupError):
        process.kill()


if __name__ == "__main__":
    sys.exit(main())
