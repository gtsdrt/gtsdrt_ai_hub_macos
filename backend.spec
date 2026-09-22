# -*- mode: python ; coding: utf-8 -*-
"""
PyInstaller 配置：把 FastAPI 后端打成单文件可执行程序 dist/backend_server

构建：
    .venv/bin/pyinstaller backend.spec --clean

产物：
    dist/backend_server          # 单文件可执行，双击/子进程运行都可以
    运行方式：PORT=8000 ./dist/backend_server

说明：
  - console=False（等价于 macOS 的 --windowed / Windows 的 --noconsole）：
    作为后台进程由 Mac App 用 Process 拉起，不弹终端窗口。
  - uvicorn 内部用字符串动态导入协议/循环实现，必须手工列 hiddenimports，否则运行时报
    "Could not import module uvicorn.protocols.http.auto" 之类错误。
  - azure-* SDK 大量使用延迟导入，这里用 collect_submodules 递归收集。
  - tools/ 既作为模块收集，也把源码文件放进包内（满足“显式包含 tools/*.py”）。
"""

import os
import platform

from PyInstaller.utils.hooks import collect_submodules

# 目标架构：本项目只发布 arm64（Apple Silicon 原生，不依赖 Rosetta）。
# 注意 PyInstaller 默认跟随“当前解释器”架构——如果从 Rosetta 终端启动 python，
# platform.machine() 会返回 x86_64，打出来的包就没法在纯 arm64 上跑，
# 所以这里固定默认 arm64，可用 PYI_TARGET_ARCH 覆盖。
TARGET_ARCH = os.environ.get("PYI_TARGET_ARCH") or "arm64"

# ------------------------------------------------------------------ 隐藏导入

# uvicorn 的动态导入（PyInstaller 静态分析会漏）
uvicorn_hidden = [
    "uvicorn.logging",
    "uvicorn.loops.auto",
    "uvicorn.protocols.http.auto",
    "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan.on",
]

# azure-* SDK：凭据链和管理客户端都是延迟导入，必须递归收集
azure_hidden = (
    collect_submodules("azure.identity")
    + collect_submodules("azure.mgmt.resource")
    + collect_submodules("azure.mgmt.compute")
    + collect_submodules("azure.mgmt.monitor")
    + collect_submodules("azure.mgmt.network")
    + collect_submodules("azure.mgmt.storage")
    + collect_submodules("azure.mgmt.web")
    + collect_submodules("azure.mgmt.sql")
    + collect_submodules("azure.mgmt.keyvault")
    + collect_submodules("azure.mgmt.containerservice")
    + collect_submodules("azure.mgmt.containerinstance")
    + collect_submodules("azure.mgmt.resourcegraph")
    + ["msal", "msal_extensions"]
)

# 本地工具包（tools/azure_tools.py 等）
tools_hidden = [
    "tools",
    "tools.azure_tools",
    "tools.meraki_tools",
    "tools.ai_tools",
]

hiddenimports = uvicorn_hidden + azure_hidden + tools_hidden

# ------------------------------------------------------------------ 数据文件

datas = [
    ("tools/*.py", "tools"),  # 显式打包 tools/ 下所有源码（便于排查/热修）
]

# ------------------------------------------------------------------ 排除项

excludes = [
    # 打包工具链自身，运行时用不到
    "pip",
    "setuptools",
    "pkg_resources",
    "wheel",
    "PyInstaller",
    # 测试相关
    "pytest",
    "_pytest",
    "pytest_asyncio",
    "test",
    "tests",
    # 本项目里只用于开发/验证的脚本
    "test_storage",
    # GUI / 科学计算等完全用不到的重依赖
    "tkinter",
    "matplotlib",
    "numpy",
    "pandas",
    "scipy",
    "PyQt5",
    "PyQt6",
    "PySide2",
    "PySide6",
    "IPython",
    "notebook",
    "sqlalchemy",
    # 数据面 SDK（21 个工具都不用，管理面 azure.mgmt.* 全部保留）
    "azure.storage",
    "azure.keyvault",
    "azure.data.tables",
    "ansible",
    # 注意：不要排除 requests —— azure-core 的同步 transport 会动态导入它
]

# ------------------------------------------------------------------ 分析

analysis = Analysis(
    ["main.py"],
    pathex=["."],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=excludes,
    noarchive=False,
    optimize=0,
)

# 构建模式：默认 onefile（单文件）；设 PYI_ONEDIR=1 出目录版。
#
# 为什么要有目录版：macOS 上 onefile 每次启动都要把整包解包到临时目录，
# 实测固定开销约 10 秒（10MB 的最小 demo 也要 9.9s，CPU 只花 0.1s，时间全在解包+系统校验），
# 29MB 的后端因此要 20 秒才监听端口，App 就会误以为「无法连接本地后端」。
# onedir 只解包一次：首次 7 秒（系统校验新文件），之后 0.08 秒。
ONEDIR = os.environ.get("PYI_ONEDIR", "").strip().lower() in ("1", "true", "yes", "on")

pyz = PYZ(analysis.pure)

exe = EXE(
    pyz,
    analysis.scripts,
    [] if ONEDIR else analysis.binaries,
    [] if ONEDIR else analysis.datas,
    [],
    name="backend_server",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    runtime_tmpdir=None,
    console=False,           # macOS：不弹终端窗口（后台进程由 App 拉起）
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=TARGET_ARCH,
    codesign_identity=None,
    entitlements_file=None,
    exclude_binaries=ONEDIR,
)

if ONEDIR:
    coll = COLLECT(
        exe,
        analysis.binaries,
        analysis.datas,
        strip=False,
        upx=False,
        name="backend_server",
    )
