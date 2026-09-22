"""
本地 AI 对话后端（FastAPI）

这是一个**不依赖 function_app.py、不依赖 Azure** 的独立服务：
  - 真实调用：配置 DEEPSEEK_API_KEY / KIMI_API_KEY 后走 OpenAI 兼容接口；
  - Mock 模式：没有配置 Key 时返回可读的 mock 回复，方便前端联调；
  - 对话历史与异步任务状态持久化在 SQLite
    （~/Library/Application Support/AIChatApp/aichat.db，可用 AICHAT_DB_PATH 覆盖）；
    SQLite 初始化失败时自动回退到内存字典，接口不变；
  - chat_async 用 FastAPI 后台任务实现，接口契约与旧版 Azure Functions 一致。

配置：支持环境变量，也支持项目根目录的 .env（见 .env.example）。

运行：
  pip install -r requirements-fastapi.txt
  cp .env.example .env        # 按需填入 Key，不填就是 mock 模式
  python main.py              # 或 uvicorn main:app --reload --port 8000
  open http://127.0.0.1:8000/docs

接口：
  POST   /api/login                 用户名密码换 JWT
  POST   /api/auth/google/start     发起 Google OAuth + PKCE 登录
  GET    /api/auth/google/callback  Google 浏览器回调
  GET    /api/auth/google/status    客户端领取后端 JWT
  POST   /api/auth/github/start     发起 GitHub Device Flow 登录
  GET    /api/auth/github/status    轮询 GitHub 并领取后端 JWT
  GET    /api/health                健康检查（含 AI 配置概览）
  POST   /api/chat                  同步对话
  POST   /api/chat_async            异步对话（返回 instance_id）
  GET    /api/ai_task_status        查询异步任务状态
  GET    /api/chat_history          查询某个会话的消息
  DELETE /api/chat_conversation     删除会话
  GET    /api/chat_conversations    会话列表（侧边栏用）

存储说明：SQLite 是“每次操作一个连接”，多线程/多 worker 都安全（WAL 模式）；
如果哪天要换成 Postgres，替换点集中在“存储层”一节。
"""

from __future__ import annotations

import asyncio
import base64
import datetime
import html
import hashlib
import hmac
import json
import logging
import os
import platform
import secrets
import sqlite3
import sys
import threading
import time
import urllib.parse
import urllib.request
import uuid
from contextlib import asynccontextmanager, contextmanager
from typing import Any, Callable, Iterator, Optional


# ============================================================
# 0. 运行架构：只支持 Apple Silicon 原生 arm64（拒绝 Rosetta）
# ============================================================
# 故意放在第三方 import 之前：被 Rosetta 翻译时先给出清晰的中文提示，
# 而不是让 fastapi / pydantic 抛 “incompatible architecture (have 'arm64', need 'x86_64')”。

# 逃生开关：仅用于排查问题（例如必须在 Intel 机器上跑一次）。
# 正常使用不要设置——本项目只发布 arm64 原生产物。
ALLOW_ROSETTA = os.environ.get("ALLOW_ROSETTA", "false").strip().lower() in {"1", "true", "yes", "on"}


def _is_translated() -> bool:
    """True = 当前进程正被 Rosetta 2 翻译成 x86_64 运行"""
    if platform.machine() != "x86_64":
        return False
    try:
        import ctypes  # noqa: PLC0415  （只在这里用一次，不需要全局导入）

        libc = ctypes.CDLL(None, use_errno=True)
        value = ctypes.c_int(0)
        size = ctypes.c_size_t(ctypes.sizeof(value))
        rc = libc.sysctlbyname(
            b"sysctl.proc_translated",
            ctypes.byref(value),
            ctypes.byref(size),
            None,
            0,
        )
        if rc == 0:
            return bool(value.value)
    except Exception:  # pragma: no cover - 取不到 sysctl 时走保守判断
        pass
    # 取不到 sysctl 时保守处理：机器是 x86_64 却又在跑本服务，就按“被翻译”算
    # （本项目只发 arm64 原生产物，真 Intel 机器本来也不该跑它）
    return True


def arch_report() -> dict:
    """给 /api/health 和启动日志用的运行架构快照"""
    machine = platform.machine()
    return {
        "machine": machine,
        "translated": _is_translated(),
        "native_arm64": machine == "arm64",
        "rosetta_allowed": ALLOW_ROSETTA,
        "frozen": bool(getattr(sys, "frozen", False)),
    }


def _enforce_native_arm64() -> None:
    """启动即拒绝 Rosetta：本项目只支持 Apple Silicon 原生 arm64"""
    if platform.machine() == "arm64" or ALLOW_ROSETTA:
        return

    reason = "Rosetta 2 翻译运行" if _is_translated() else "非 arm64 机器（本项目只发布 arm64 原生产物）"
    print(
        "\n".join(
            [
                "",
                "=" * 68,
                f"拒绝启动：检测到 x86_64 运行环境（{reason}）",
                "本项目只支持 Apple Silicon 原生 arm64，不使用 Rosetta。",
                "",
                "修复方式：",
                "  1) 用原生 arm64 解释器启动：./scripts/run_native_arm64.sh .venv/bin/python main.py",
                "  2) 终端别勾选「使用 Rosetta 打开」（访达 → 应用程序 → 终端 → 显示简介）",
                "  3) 重新打包后端：./scripts/run_native_arm64.sh .venv/bin/python -m PyInstaller backend.spec",
                "",
                "仅排查问题时可临时放行：ALLOW_ROSETTA=1 python main.py",
                "=" * 68,
                "",
            ]
        ),
        file=sys.stderr,
        flush=True,
    )
    sys.exit(78)  # EX_CONFIG


_enforce_native_arm64()

import httpx
from fastapi import BackgroundTasks, Body, Depends, FastAPI, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict

_LOG_LEVELS = ("CRITICAL", "ERROR", "WARNING", "INFO", "DEBUG", "NOTSET")


def _log_level_name(default: str = "INFO") -> str:
    """
    LOG_LEVEL 统一成标准写法：进 logging 要大写，进 uvicorn 要小写。
    随手写 LOG_LEVEL=debug / warning 也不会让服务起不来。
    """
    raw = str(os.environ.get("LOG_LEVEL", default) or default).strip().upper()
    return raw if raw in _LOG_LEVELS else default


logging.basicConfig(
    level=_log_level_name(),
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
logger = logging.getLogger("main")


def _quiet_noisy_loggers() -> None:
    """
    第三方库默认把每条请求/每次凭据尝试都打成 INFO，日志面板和终端会被刷屏，
    顺带拖慢前端渲染。这里统一压到 WARNING（可用环境变量调回 INFO 排查问题）。
    """
    for name, level in (
        # azure 的 WARNING 会整段打印「尝试了哪些凭据」的链路 dump（几十行、几 KB），
        # 对桌面 App 日志面板是纯噪音；失败原因我们自己在 tools 层已经记了。
        ("azure", _env("AZURE_LOG_LEVEL", default="ERROR")),
        ("httpx", _env("HTTP_LOG_LEVEL", default="WARNING")),
        ("httpx2", _env("HTTP_LOG_LEVEL", default="WARNING")),
        ("httpcore", _env("HTTP_LOG_LEVEL", default="WARNING")),
        ("urllib3", _env("HTTP_LOG_LEVEL", default="WARNING")),
        ("openai", _env("HTTP_LOG_LEVEL", default="WARNING")),
    ):
        logging.getLogger(name).setLevel(level.strip().upper())

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
if getattr(sys, "frozen", False):
    # PyInstaller 打包后：把“程序所在目录”当成项目目录，
    # 这样 dist/ 旁边的 .env 能被读到，tools/ 的 data 文件也能定位。
    BASE_DIR = os.path.dirname(os.path.abspath(sys.executable))
if BASE_DIR not in sys.path:
    # 保证从任意工作目录（例如 uvicorn main:app）都能 import tools 包
    sys.path.insert(0, BASE_DIR)

import tools as tool_registry  # noqa: E402  （必须在 sys.path 处理之后导入）
from tools import ai_tools, azure_tools, meraki_tools  # noqa: E402

# 模块加载时刻，用于 /api/health 的 uptime_seconds
PROCESS_STARTED_AT = time.time()

# ============================================================
# 1. 配置：先加载 .env，再读环境变量
# ============================================================


def _load_env_file() -> None:
    """加载项目根目录的 .env；已存在的环境变量优先，不会被 .env 覆盖"""
    env_path = os.path.join(BASE_DIR, ".env")
    if not os.path.exists(env_path):
        return

    try:
        from dotenv import load_dotenv

        load_dotenv(env_path, override=False)
        logger.info("已加载 .env：%s", env_path)
        return
    except ImportError:
        logger.warning("未安装 python-dotenv，改用内置极简 .env 解析（pip install python-dotenv）")

    with open(env_path, "r", encoding="utf-8") as fh:
        for raw_line in fh:
            line = raw_line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


_load_env_file()


def _env(*names: str, default: str = "") -> str:
    """按顺序取第一个非空环境变量"""
    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _env_bool(name: str, default: bool = False) -> bool:
    value = os.environ.get(name)
    if value is None:
        return default
    return value.strip().lower() in ("1", "true", "yes", "on")


# .env 载入、_env 就绪之后再压第三方日志级别
_quiet_noisy_loggers()


def _provider_settings() -> dict[str, dict]:
    """各 AI Provider 的 Key / Base URL / 默认模型，全部可由环境变量覆盖"""
    return {
        "kimi": {
            "label": "Kimi (Moonshot)",
            "api_key_env": "KIMI_API_KEY",
            "api_key": _env("KIMI_API_KEY", "MOONSHOT_API_KEY"),
            "base_url": _env("KIMI_BASE_URL", "AI_BASE_URL", default="https://api.moonshot.ai/v1"),
            "model": _env("KIMI_MODEL_NAME", "AI_MODEL_NAME", default="kimi-k3"),
        },
        "deepseek": {
            "label": "DeepSeek",
            "api_key_env": "DEEPSEEK_API_KEY",
            "api_key": _env("DEEPSEEK_API_KEY"),
            "base_url": _env("DEEPSEEK_BASE_URL", default="https://api.deepseek.com"),
            "model": _env("DEEPSEEK_MODEL_NAME", default="deepseek-flash"),
            # 带图片的请求自动切到视觉模型（可用 DEEPSEEK_VISION_MODEL_NAME 覆盖）
            "vision_model": _env("DEEPSEEK_VISION_MODEL_NAME", default="deepseek-v4-flash-vision-exp"),
        },
        "openai": {
            "label": "OpenAI",
            "api_key_env": "OPENAI_API_KEY",
            "api_key": _env("OPENAI_API_KEY"),
            "base_url": _env("OPENAI_BASE_URL", default="https://api.openai.com/v1"),
            # 默认模型可用 OPENAI_MODEL_NAME 覆盖（例如 gpt-5、gpt-4.1、o4-mini 等）
            "model": _env("OPENAI_MODEL_NAME", default="gpt-4o"),
            # Azure 的 OpenAI 兼容端点用 api-key 头；auto = 识别到 Azure 域名时自动带上
            "auth_mode": _env("OPENAI_AUTH_MODE", default="auto"),
        },
    }


PROVIDERS: dict[str, dict] = _provider_settings()

API_PREFIX = os.environ.get("API_PREFIX", "/api")
DEFAULT_AI_PROVIDER = os.environ.get("DEFAULT_AI_PROVIDER", "deepseek").strip().lower()
if DEFAULT_AI_PROVIDER not in PROVIDERS:
    logger.warning("DEFAULT_AI_PROVIDER=%s 不是受支持的 provider，回退到 deepseek", DEFAULT_AI_PROVIDER)
    DEFAULT_AI_PROVIDER = "deepseek"

GENERAL_CHAT_PROVIDER = os.environ.get("GENERAL_CHAT_PROVIDER", DEFAULT_AI_PROVIDER).strip().lower()
if GENERAL_CHAT_PROVIDER not in PROVIDERS:
    GENERAL_CHAT_PROVIDER = DEFAULT_AI_PROVIDER

# auto：有 Key 就真实调用，没 Key 就 mock；true：强制 mock；false：没 Key 直接报错
AI_MOCK_MODE = os.environ.get("AI_MOCK_MODE", "auto").strip().lower()
AI_MOCK_DELAY_SECONDS = _env_float("AI_MOCK_DELAY_SECONDS", 0.5)
AI_REQUEST_TIMEOUT_SECONDS = _env_float("AI_REQUEST_TIMEOUT_SECONDS", 360.0)

JWT_SECRET = os.environ.get("JWT_SECRET", "your-very-secret-key-change-it-in-production")
JWT_ALGORITHM = "HS256"
JWT_EXPIRATION_MINUTES = int(os.environ.get("JWT_EXPIRATION_MINUTES", 60 * 24))
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "password123")

# Google 登录使用 OAuth 2.0 Authorization Code + PKCE。Client Secret 对桌面应用不是必需项；
# 如 Google Cloud 中使用的是 Web Application 类型，可通过 GOOGLE_CLIENT_SECRET 提供。
GOOGLE_CLIENT_ID = _env("GOOGLE_CLIENT_ID")
GOOGLE_CLIENT_SECRET = _env("GOOGLE_CLIENT_SECRET")
GOOGLE_REDIRECT_URI = _env("GOOGLE_REDIRECT_URI")
GOOGLE_OAUTH_STATE_TTL_SECONDS = _env_float("GOOGLE_OAUTH_STATE_TTL_SECONDS", 300.0)
GOOGLE_ALLOWED_EMAILS = {
    item.strip().lower() for item in _env("GOOGLE_ALLOWED_EMAILS").split(",") if item.strip()
}
GOOGLE_ALLOWED_DOMAINS = {
    item.strip().lower().lstrip("@")
    for item in _env("GOOGLE_ALLOWED_DOMAINS").split(",")
    if item.strip()
}

# GitHub 桌面端使用 Device Flow，不需要把 Client Secret 放进 App。
GITHUB_CLIENT_ID = _env("GITHUB_CLIENT_ID")
GITHUB_OAUTH_STATE_TTL_SECONDS = _env_float("GITHUB_OAUTH_STATE_TTL_SECONDS", 900.0)
GITHUB_ALLOWED_LOGINS = {
    item.strip().lower() for item in _env("GITHUB_ALLOWED_LOGINS").split(",") if item.strip()
}
GITHUB_ALLOWED_EMAILS = {
    item.strip().lower() for item in _env("GITHUB_ALLOWED_EMAILS").split(",") if item.strip()
}

# 任务记录保留时长，每次查询任务时清理过期记录
# 默认 24 小时；TASK_TTL_SECONDS 优先级更高（自动化测试里常设成 1 秒）
TASK_TTL_HOURS = _env_float("TASK_TTL_HOURS", 24.0)
TASK_TTL_SECONDS = _env_float("TASK_TTL_SECONDS", 0.0) or TASK_TTL_HOURS * 3600

# 持久化位置：~/Library/Application Support/AIChatApp/aichat.db（可用环境变量覆盖，方便测试）
APP_SUPPORT_DIR = os.path.expanduser("~/Library/Application Support/AIChatApp")
DB_PATH = os.environ.get("AICHAT_DB_PATH") or os.path.join(APP_SUPPORT_DIR, "aichat.db")

MAX_CHAT_IMAGES = int(os.environ.get("MAX_CHAT_IMAGES", 5))
MAX_CHAT_FILES = int(os.environ.get("MAX_CHAT_FILES", 5))
MAX_FILE_CHARS = int(os.environ.get("MAX_FILE_CHARS", 50000))
MAX_HISTORY_MESSAGES = int(os.environ.get("MAX_HISTORY_MESSAGES", 40))


def _normalize_provider(provider: Optional[str]) -> str:
    candidate = str(provider or "").strip().lower()
    return candidate if candidate in PROVIDERS else DEFAULT_AI_PROVIDER


def _provider_config(provider: Optional[str]) -> dict:
    return PROVIDERS[_normalize_provider(provider)]


def _get_ai_model(provider: Optional[str], model: Optional[str] = None) -> str:
    return model or _provider_config(provider)["model"]


def _api_key_for(provider: Optional[str]) -> str:
    return _provider_config(provider).get("api_key") or ""


def _should_mock(provider: Optional[str]) -> bool:
    """返回是否走 mock 回复"""
    if AI_MOCK_MODE == "true":
        return True

    has_key = bool(_api_key_for(provider))
    if AI_MOCK_MODE == "false":
        if not has_key:
            env_name = _provider_config(provider)["api_key_env"]
            raise RuntimeError(f"AI_MOCK_MODE=false 但未配置 {env_name}，无法发起真实请求")
        return False

    return not has_key


# ============================================================
# 2. 鉴权：Bearer JWT（PyJWT 可选）或 Easy Auth 头
# ============================================================


def _b64url_decode(segment: str) -> bytes:
    return base64.urlsafe_b64decode(segment + "=" * (-len(segment) % 4))


def _decode_token_stdlib(token: str) -> dict:
    """不依赖第三方库的 HS256 校验，保证最小依赖也能跑通登录"""
    try:
        header_b64, payload_b64, signature_b64 = token.split(".")
    except ValueError as exc:
        raise ValueError("token 格式不正确") from exc

    signing_input = f"{header_b64}.{payload_b64}".encode()
    expected = hmac.new(JWT_SECRET.encode(), signing_input, hashlib.sha256).digest()
    if not hmac.compare_digest(expected, _b64url_decode(signature_b64)):
        raise ValueError("签名校验失败")

    payload = json.loads(_b64url_decode(payload_b64))
    exp = payload.get("exp")
    if exp is not None and float(exp) < time.time():
        raise ValueError("token 已过期")
    return payload


def _decode_token(token: str) -> dict:
    """装了 PyJWT 就用它，否则用标准库实现（签名算法均为 HS256）"""
    try:
        import jwt as pyjwt  # type: ignore
    except ImportError:
        return _decode_token_stdlib(token)

    try:
        return pyjwt.decode(token, JWT_SECRET, algorithms=[JWT_ALGORITHM])
    except Exception as exc:
        raise ValueError(str(exc)) from exc


def _encode_token(username: str, *, provider: str = "local", subject: Optional[str] = None) -> str:
    payload = {
        "username": username,
        "provider": provider,
        "sub": subject or f"{provider}:{username}",
        # Unix 时间戳是 JWT 标准写法，PyJWT 和标准库实现都认
        "exp": int(time.time()) + JWT_EXPIRATION_MINUTES * 60,
    }
    try:
        import jwt as pyjwt  # type: ignore

        return pyjwt.encode(payload, JWT_SECRET, algorithm=JWT_ALGORITHM)
    except ImportError:
        header = {"alg": JWT_ALGORITHM, "typ": "JWT"}

        def _enc(obj: dict) -> str:
            raw = json.dumps(obj, separators=(",", ":")).encode()
            return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

        signing_input = f"{_enc(header)}.{_enc(payload)}"
        signature = hmac.new(JWT_SECRET.encode(), signing_input.encode(), hashlib.sha256).digest()
        sig_b64 = base64.urlsafe_b64encode(signature).rstrip(b"=").decode()
        return f"{signing_input}.{sig_b64}"


# Google OAuth 登录是短流程，状态只保存在内存并在成功交付 token 后删除。
# 浏览器回调与 App 轮询可能落在不同线程，因此所有访问都经过同一把锁。
_GOOGLE_LOGIN_LOCK = threading.Lock()
_GOOGLE_LOGINS: dict[str, dict[str, Any]] = {}
_GITHUB_LOGIN_LOCK = threading.Lock()
_GITHUB_LOGINS: dict[str, dict[str, Any]] = {}


def _google_redirect_uri(request: Request) -> str:
    if GOOGLE_REDIRECT_URI:
        return GOOGLE_REDIRECT_URI
    return f"{str(request.base_url).rstrip('/')}{API_PREFIX}/auth/google/callback"


def _cleanup_google_logins(now: Optional[float] = None) -> None:
    cutoff = (now or time.time()) - GOOGLE_OAUTH_STATE_TTL_SECONDS
    with _GOOGLE_LOGIN_LOCK:
        expired = [state for state, item in _GOOGLE_LOGINS.items() if item["created_at"] < cutoff]
        for state in expired:
            _GOOGLE_LOGINS.pop(state, None)


def _google_email_allowed(email: str) -> bool:
    normalized = email.strip().lower()
    if not GOOGLE_ALLOWED_EMAILS and not GOOGLE_ALLOWED_DOMAINS:
        return True
    domain = normalized.rsplit("@", 1)[-1] if "@" in normalized else ""
    return normalized in GOOGLE_ALLOWED_EMAILS or domain in GOOGLE_ALLOWED_DOMAINS


def _cleanup_github_logins(now: Optional[float] = None) -> None:
    current = now or time.time()
    with _GITHUB_LOGIN_LOCK:
        expired = [
            state for state, item in _GITHUB_LOGINS.items()
            if item.get("expires_at", 0) < current
        ]
        for state in expired:
            _GITHUB_LOGINS.pop(state, None)


def _github_identity_allowed(login: str, email: str) -> bool:
    normalized_login = login.strip().lower()
    normalized_email = email.strip().lower()
    if not GITHUB_ALLOWED_LOGINS and not GITHUB_ALLOWED_EMAILS:
        return True
    return normalized_login in GITHUB_ALLOWED_LOGINS or normalized_email in GITHUB_ALLOWED_EMAILS


def _google_callback_page(title: str, message: str, *, success: bool) -> HTMLResponse:
    color = "#17803d" if success else "#b42318"
    safe_title = html.escape(title)
    safe_message = html.escape(message)
    return HTMLResponse(
        f"""<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>{safe_title}</title></head>
<body style="font-family:-apple-system,BlinkMacSystemFont,sans-serif;max-width:560px;margin:72px auto;padding:0 24px">
<h1 style="color:{color}">{safe_title}</h1><p>{safe_message}</p><p>现在可以关闭此页面并返回 AIChatApp。</p>
</body></html>""",
        status_code=200 if success else 400,
    )


def require_auth(
    authorization: Optional[str] = Header(default=None),
    x_ms_client_principal: Optional[str] = Header(default=None, alias="X-MS-CLIENT-PRINCIPAL"),
) -> None:
    """FastAPI 依赖：Bearer JWT 或 Easy Auth 头任一通过即可"""
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization.split(" ", 1)[1]
        try:
            _decode_token(token)
            return
        except ValueError as exc:
            logger.warning("JWT 校验失败，回退 Easy Auth：%s", exc)

    if x_ms_client_principal:
        return

    raise HTTPException(status_code=401, detail="Missing or invalid authentication")


# ============================================================
# 3. 存储层：SQLite（默认） + 内存 fallback
#    - 每次操作新建连接（线程池并发下最省心）
#    - 初始化失败（磁盘权限等）自动降级为进程内存字典，接口不变
# ============================================================

_STORAGE_BACKEND = "sqlite"
_STORAGE_ERROR: Optional[str] = None


def _now_iso() -> str:
    """统一带微秒的 UTC ISO8601，保证字符串比较与时间顺序一致"""
    return datetime.datetime.now(datetime.timezone.utc).isoformat(timespec="microseconds")


def _db_connect() -> sqlite3.Connection:
    connection = sqlite3.connect(DB_PATH, timeout=5.0)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA busy_timeout = 5000")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA synchronous = NORMAL")
    return connection


@contextmanager
def _db() -> Iterator[sqlite3.Connection]:
    """一次操作一个连接，退出时自动提交/回滚并关闭"""
    connection = _db_connect()
    try:
        with connection:
            yield connection
    finally:
        connection.close()


def _init_storage() -> None:
    """建目录 + 建表；出问题就回退到内存模式，不让服务起不来"""
    global _STORAGE_BACKEND, _STORAGE_ERROR

    try:
        directory = os.path.dirname(DB_PATH)
        if directory:
            os.makedirs(directory, exist_ok=True)

        with _db() as connection:
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS conversations (
                    id TEXT PRIMARY KEY,
                    title TEXT,
                    messages TEXT,
                    updated_at TEXT,
                    message_count INTEGER
                )
                """
            )
            connection.execute(
                """
                CREATE TABLE IF NOT EXISTS tasks (
                    instance_id TEXT PRIMARY KEY,
                    status TEXT,
                    result TEXT,
                    error TEXT,
                    created_at TEXT,
                    updated_at TEXT
                )
                """
            )

        _STORAGE_BACKEND = "sqlite"
        _STORAGE_ERROR = None
        logger.info("存储层就绪：SQLite %s", DB_PATH)
    except Exception as exc:
        _STORAGE_BACKEND = "memory"
        _STORAGE_ERROR = f"{type(exc).__name__}: {exc}"
        logger.warning("SQLite 初始化失败（%s），已回退到内存模式", _STORAGE_ERROR)


def _fallback_to_memory(reason: str) -> None:
    """运行期出现的数据库错误也降级到内存，保证接口可用"""
    global _STORAGE_BACKEND, _STORAGE_ERROR
    if _STORAGE_BACKEND != "memory":
        logger.error("SQLite 不可用（%s），已切换为内存模式", reason)
    _STORAGE_BACKEND = "memory"
    _STORAGE_ERROR = reason


def _row_to_task(row: sqlite3.Row) -> dict:
    result = row["result"]
    try:
        result = json.loads(result) if result else None
    except json.JSONDecodeError:
        result = None

    return {
        "instance_id": row["instance_id"],
        "status": row["status"],
        "result": result,
        "error": row["error"],
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        # 兼容旧字段名（/api/ai_task_status 仍在用）
        "last_updated_at": row["updated_at"],
    }


def storage_status() -> dict:
    """给 /api/health 用：sqlite 或 memory"""
    return {
        "storage": _STORAGE_BACKEND,
        "db_path": DB_PATH if _STORAGE_BACKEND == "sqlite" else None,
        "error": _STORAGE_ERROR,
    }


# 进程启动时初始化存储（失败自动回退内存）
_init_storage()


# ---------- 异步任务表（chat_async 登记任务后立刻返回 202，工作线程里跑 AI） ----------

_TASKS: dict[str, dict] = {}
_TASKS_LOCK = threading.Lock()


def _task_ttl_cutoff_iso() -> str:
    cutoff = datetime.datetime.now(datetime.timezone.utc) - datetime.timedelta(seconds=TASK_TTL_SECONDS)
    return cutoff.isoformat(timespec="microseconds")


def _cleanup_expired_tasks() -> None:
    """每次查询/创建任务时清理 created_at 超过 TTL（默认 24 小时）的记录"""
    cutoff = _task_ttl_cutoff_iso()

    if _STORAGE_BACKEND == "sqlite":
        try:
            with _db() as connection:
                removed = connection.execute("DELETE FROM tasks WHERE created_at < ?", (cutoff,)).rowcount
            if removed:
                logger.info("已清理 %s 条过期任务（created_at < %s）", removed, cutoff)
            return
        except Exception as exc:
            _fallback_to_memory(f"任务 TTL 清理失败：{exc}")

    cutoff_ts = time.time() - TASK_TTL_SECONDS
    with _TASKS_LOCK:
        for key in [k for k, task in _TASKS.items() if task.get("_ts", 0) < cutoff_ts]:
            _TASKS.pop(key, None)


def _create_task(conversation_id: Optional[str]) -> str:
    instance_id = uuid.uuid4().hex
    now = _now_iso()

    _cleanup_expired_tasks()

    if _STORAGE_BACKEND == "sqlite":
        try:
            with _db() as connection:
                connection.execute(
                    """
                    INSERT INTO tasks (instance_id, status, result, error, created_at, updated_at)
                    VALUES (?, ?, NULL, NULL, ?, ?)
                    """,
                    (instance_id, "pending", now, now),
                )
            return instance_id
        except Exception as exc:
            _fallback_to_memory(f"创建任务写库失败：{exc}")

    with _TASKS_LOCK:
        _TASKS[instance_id] = {
            "instance_id": instance_id,
            "status": "pending",
            "created_at": now,
            "last_updated_at": now,
            "conversation_id": conversation_id,
            "result": None,
            "error": None,
            "_ts": time.time(),
        }
    return instance_id


def _update_task(instance_id: str, **fields: Any) -> None:
    now = _now_iso()

    if _STORAGE_BACKEND == "sqlite":
        assignments = ["updated_at = ?"]
        values: list = [now]

        if "status" in fields:
            assignments.append("status = ?")
            values.append(fields["status"])
        if "result" in fields:
            assignments.append("result = ?")
            values.append(json.dumps(fields["result"], ensure_ascii=False))
        if "error" in fields:
            assignments.append("error = ?")
            values.append(fields["error"])

        values.append(instance_id)

        try:
            with _db() as connection:
                connection.execute(
                    f"UPDATE tasks SET {', '.join(assignments)} WHERE instance_id = ?",
                    values,
                )
            return
        except Exception as exc:
            _fallback_to_memory(f"更新任务写库失败：{exc}")

    with _TASKS_LOCK:
        task = _TASKS.get(instance_id)
        if task is None:
            return
        task.update(fields)
        task["last_updated_at"] = now
        task["_ts"] = time.time()


def _get_task(instance_id: str) -> Optional[dict]:
    _cleanup_expired_tasks()

    if _STORAGE_BACKEND == "sqlite":
        try:
            with _db() as connection:
                row = connection.execute(
                    """
                    SELECT instance_id, status, result, error, created_at, updated_at
                    FROM tasks WHERE instance_id = ?
                    """,
                    (instance_id,),
                ).fetchone()
            return _row_to_task(row) if row else None
        except Exception as exc:
            _fallback_to_memory(f"查询任务失败：{exc}")

    with _TASKS_LOCK:
        task = _TASKS.get(instance_id)
        return dict(task) if task else None


# ============================================================
# 4. 会话存储（SQLite：conversations 表；初始化失败时用内存字典）
# ============================================================

_CONVERSATIONS: dict[str, dict] = {}
_CONVERSATIONS_LOCK = threading.Lock()


def _flatten_content_to_text(content: Any) -> str:
    """多模态 content 转纯文本，图片用 [图片xN] 占位，避免把 base64 大图写进历史"""
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)

    texts, image_count = [], 0
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            texts.append(str(part.get("text", "")))
        elif part.get("type") in ("image_url", "video_url"):
            image_count += 1
    prefix = f"[图片x{image_count}] " if image_count else ""
    return prefix + "\n".join(t for t in texts if t)


def _clean_messages_for_storage(messages: list) -> list:
    """
    只保留 user/assistant/system 的最终内容，丢掉 tool 中间过程。
    assistant 消息额外保留 tool_calls_log / iterations_used，供前端展示与回看。
    """
    cleaned = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        if role not in ("user", "assistant", "system"):
            continue

        content = _flatten_content_to_text(msg.get("content"))
        if role == "user" and isinstance(content, str) and len(content) > 2000:
            content = (
                content[:800]
                + f"\n……[原文共 {len(content)} 字符，中间部分已省略]……\n"
                + content[-800:]
            )

        if role == "assistant":
            # 只有 tool_calls 没有正文的中间消息不落库
            if not content and msg.get("tool_calls"):
                continue

            message = {
                "role": role,
                "content": content,
                "tool_calls_log": _clean_tool_calls_log(msg.get("tool_calls_log")),
                "iterations_used": int(msg.get("iterations_used") or 0),
            }
            # 旧数据没有这个字段，只有调用方显式传了才写（前端用来区分“想调工具但没挂上”）
            if "tools_enabled" in msg and msg.get("tools_enabled") is not None:
                message["tools_enabled"] = bool(msg["tools_enabled"])

            cleaned.append(message)
            continue

        cleaned.append({"role": role, "content": content})
    return cleaned


# tool_calls_log 落库时保留的字段（顺序即前端展示顺序）
TOOL_LOG_FIELDS = ("iteration", "tool_name", "arguments", "result_preview", "result_status", "duration_ms")


def _clean_tool_calls_log(entries: Any) -> list[dict]:
    """只保留约定字段，并对 result_preview 再做一次长度兜底"""
    cleaned: list[dict] = []
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue

        item = {field: entry.get(field) for field in TOOL_LOG_FIELDS}
        item["iteration"] = int(item.get("iteration") or 0)
        item["tool_name"] = str(item.get("tool_name") or "")
        item["result_preview"] = str(item.get("result_preview") or "")[:TOOL_RESULT_PREVIEW_CHARS]
        item["result_status"] = str(item.get("result_status") or "error")
        item["duration_ms"] = int(item.get("duration_ms") or 0)
        if not isinstance(item.get("arguments"), dict):
            item["arguments"] = {}
        cleaned.append(item)
    return cleaned


def _derive_title(cleaned_messages: list) -> str:
    """标题优先取首条用户消息（截断 30 字）"""
    for msg in cleaned_messages:
        if msg.get("role") == "user" and msg.get("content"):
            return str(msg["content"]).replace("\n", " ").strip()[:30] or "新对话"
    return "新对话"


def _save_conversation(conversation_id: str, messages: list) -> None:
    """保存会话（SQLite upsert；写库失败时降级到内存）"""
    cleaned = _clean_messages_for_storage(messages)
    trimmed = cleaned[-MAX_HISTORY_MESSAGES:]
    now = _now_iso()

    if _STORAGE_BACKEND == "sqlite":
        try:
            with _db() as connection:
                row = connection.execute(
                    "SELECT title FROM conversations WHERE id = ?", (conversation_id,)
                ).fetchone()
                title = (row["title"] if row else None) or _derive_title(cleaned)

                connection.execute(
                    """
                    INSERT INTO conversations (id, title, messages, updated_at, message_count)
                    VALUES (?, ?, ?, ?, ?)
                    ON CONFLICT(id) DO UPDATE SET
                        title = excluded.title,
                        messages = excluded.messages,
                        updated_at = excluded.updated_at,
                        message_count = excluded.message_count
                    """,
                    (
                        conversation_id,
                        title,
                        json.dumps(trimmed, ensure_ascii=False),
                        now,
                        len(trimmed),
                    ),
                )
            logger.info("[Conversation] 已写入 SQLite：%s（%s 条消息）", conversation_id, len(trimmed))
            return
        except Exception as exc:
            _fallback_to_memory(f"保存会话写库失败：{exc}")

    with _CONVERSATIONS_LOCK:
        existing = _CONVERSATIONS.get(conversation_id) or {}
        _CONVERSATIONS[conversation_id] = {
            "messages": trimmed,
            "title": existing.get("title") or _derive_title(cleaned),
            "updated_at": now,
            "message_count": len(trimmed),
        }


def _load_conversation(conversation_id: str) -> list:
    """读取会话消息列表；不存在或损坏时返回空列表"""
    if _STORAGE_BACKEND == "sqlite":
        try:
            with _db() as connection:
                row = connection.execute(
                    "SELECT messages FROM conversations WHERE id = ?", (conversation_id,)
                ).fetchone()
            if row is None:
                return []
            try:
                messages = json.loads(row["messages"] or "[]")
            except json.JSONDecodeError:
                logger.warning("[Conversation] %s 的消息内容无法解析，按空会话处理", conversation_id)
                return []
            logger.info("[Conversation] 已从 SQLite 读取：%s（%s 条消息）", conversation_id, len(messages))
            return messages
        except Exception as exc:
            _fallback_to_memory(f"读取会话失败：{exc}")

    with _CONVERSATIONS_LOCK:
        return list((_CONVERSATIONS.get(conversation_id) or {}).get("messages", []))


def _delete_conversation(conversation_id: str) -> bool:
    if _STORAGE_BACKEND == "sqlite":
        try:
            with _db() as connection:
                removed = connection.execute(
                    "DELETE FROM conversations WHERE id = ?", (conversation_id,)
                ).rowcount
            return removed > 0
        except Exception as exc:
            _fallback_to_memory(f"删除会话失败：{exc}")

    with _CONVERSATIONS_LOCK:
        return _CONVERSATIONS.pop(conversation_id, None) is not None


def list_conversations() -> list:
    """会话列表（按更新时间倒序），与旧版返回结构一致"""
    if _STORAGE_BACKEND == "sqlite":
        try:
            with _db() as connection:
                rows = connection.execute(
                    """
                    SELECT id, title, updated_at, message_count FROM conversations
                    ORDER BY updated_at DESC
                    """
                ).fetchall()
            return [
                {
                    "conversation_id": row["id"],
                    "title": row["title"] or "新对话",
                    "updated_at": row["updated_at"] or "",
                    "message_count": int(row["message_count"] or 0),
                }
                for row in rows
            ]
        except Exception as exc:
            _fallback_to_memory(f"会话列表查询失败：{exc}")

    with _CONVERSATIONS_LOCK:
        items = [
            {
                "conversation_id": cid,
                "title": data.get("title", "新对话"),
                "updated_at": data.get("updated_at", ""),
                "message_count": data.get("message_count", 0),
            }
            for cid, data in _CONVERSATIONS.items()
        ]
    items.sort(key=lambda item: item.get("updated_at") or "", reverse=True)
    return items


# 兼容旧调用名（对外契约不变）
save_conversation = _save_conversation
load_conversation = _load_conversation
delete_conversation = _delete_conversation


# ============================================================
# 5. 提示词与输入处理（system prompt / 图片 / 文件）
# ============================================================


def build_system_prompt(custom_prompt: Optional[str] = None) -> str:
    if custom_prompt:
        return custom_prompt

    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    return (
        "你是一个可靠、友好的 AI 助手。\n"
        f"当前日期：{today}。\n"
        "你可以就任何话题与用户自然交流：回答问题、解释概念、写作与润色、翻译、"
        "编程与调试、数据分析、头脑风暴等。\n"
        "请始终使用与用户相同的语言回复；回答准确、结构清晰、详略得当；"
        "不确定或不知道的事情要如实说明，不要编造。"
    )


def _normalize_image_data_url(img: Any) -> Optional[str]:
    if not isinstance(img, str) or not img.strip():
        return None
    img = img.strip()
    return img if img.startswith("data:image/") else f"data:image/jpeg;base64,{img}"


def _build_multimodal_user_content(prompt: Optional[str], images: list) -> list:
    """OpenAI 多模态格式：图片在前、文字在后"""
    parts = []
    for img in (images or [])[:MAX_CHAT_IMAGES]:
        url = _normalize_image_data_url(img)
        if url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
    parts.append({"type": "text", "text": prompt or "请描述这张图片。"})
    return parts


def _messages_contain_images(messages: list) -> bool:
    for msg in messages or []:
        if isinstance(msg, dict) and isinstance(msg.get("content"), list):
            for part in msg["content"]:
                if isinstance(part, dict) and part.get("type") in ("image_url", "video_url"):
                    return True
    return False


_TEXT_FILE_EXTS = {
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".jsx", ".tsx", ".java",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".php", ".rb", ".sh",
    ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".csv", ".tsv", ".log", ".sql", ".html", ".htm", ".css", ".env", ".ps1",
}


def _extract_file_text(name: str, file_obj: dict) -> str:
    """
    把上传文件转成纯文本。
      {"name": ..., "content": "文本内容"}        —— 前端已按文本读取
      {"name": ..., "content_base64": "..."}     —— 二进制，后端按扩展名解析
    PDF / Word / Excel / PPT 需要额外安装对应库，缺库时返回明确的提示文本。
    """
    import io

    ext = os.path.splitext(name or "")[1].lower()

    if isinstance(file_obj.get("content"), str):
        return file_obj["content"]

    raw_b64 = (file_obj.get("content_base64") or "").split(",", 1)[-1]
    raw = base64.b64decode(raw_b64) if raw_b64 else b""

    if ext in _TEXT_FILE_EXTS:
        return raw.decode("utf-8", errors="replace")

    if ext == ".pdf":
        try:
            import pdfplumber

            with pdfplumber.open(io.BytesIO(raw)) as pdf:
                return "\n".join((page.extract_text() or "") for page in pdf.pages)
        except ImportError:
            try:
                from pypdf import PdfReader

                reader = PdfReader(io.BytesIO(raw))
                return "\n".join((page.extract_text() or "") for page in reader.pages)
            except ImportError:
                return "[未解析：本地后端解析 PDF 需要额外安装 pdfplumber 或 pypdf]"

    if ext == ".docx":
        try:
            import docx
        except ImportError:
            return "[未解析：本地后端解析 Word 需要额外安装 python-docx]"
        document = docx.Document(io.BytesIO(raw))
        return "\n".join(p.text for p in document.paragraphs if p.text.strip())

    if ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            return "[未解析：本地后端解析 Excel 需要额外安装 openpyxl]"
        workbook = openpyxl.load_workbook(io.BytesIO(raw), data_only=True, read_only=True)
        lines = []
        for sheet in workbook.worksheets:
            lines.append(f"# Sheet: {sheet.title}")
            for row in sheet.iter_rows(values_only=True):
                cells = ["" if cell is None else str(cell) for cell in row]
                if any(cells):
                    lines.append("\t".join(cells))
        return "\n".join(lines)

    if ext == ".pptx":
        try:
            from pptx import Presentation
        except ImportError:
            return "[未解析：本地后端解析 PPT 需要额外安装 python-pptx]"
        presentation = Presentation(io.BytesIO(raw))
        lines = []
        for index, slide in enumerate(presentation.slides, 1):
            lines.append(f"# Slide {index}")
            for shape in slide.shapes:
                if shape.has_text_frame and shape.text_frame.text.strip():
                    lines.append(shape.text_frame.text.strip())
        return "\n".join(lines)

    return raw.decode("utf-8", errors="replace")


def _build_files_context(files: list) -> str:
    """把文件解析成注入提示词的文本块，超长文件截断"""
    blocks = []
    for file_obj in (files or [])[:MAX_CHAT_FILES]:
        if not isinstance(file_obj, dict):
            continue
        name = file_obj.get("name") or "unnamed"
        try:
            text = _extract_file_text(name, file_obj)
        except Exception as exc:
            text = f"[文件解析失败：{exc}]"

        if len(text) > MAX_FILE_CHARS:
            text = text[:MAX_FILE_CHARS] + f"\n……[文件过长已截断，原长 {len(text)} 字符]"
        blocks.append(f"===== 文件：{name} =====\n{text}\n===== 文件 {name} 结束 =====")
    return "\n\n".join(blocks)


def build_chat_messages(req_body: dict, conversation_id: str) -> tuple[Optional[list], Optional[str]]:
    """
    构建本轮发给模型的消息列表，返回 (messages, error)。
      1. 请求直接传 messages（调用方自管历史）→ 缺 system 时补上
      2. 传 prompt → 读取 conversation_id 的历史后追加本轮提问
    """
    system_prompt = build_system_prompt(req_body.get("system_prompt"))

    messages = req_body.get("messages")
    if messages:
        if not any(isinstance(m, dict) and m.get("role") == "system" for m in messages):
            messages = [{"role": "system", "content": system_prompt}] + list(messages)
        return messages, None

    user_prompt = req_body.get("prompt")
    images = req_body.get("images") or []
    files = req_body.get("files") or []
    if isinstance(images, str):
        images = [images]
    if isinstance(files, dict):
        files = [files]

    if not user_prompt and not images and not files:
        return None, "请提供 'prompt' 或 'messages'"

    if files:
        files_context = _build_files_context(files)
        user_prompt = f"{files_context}\n\n以上是用户上传的文件内容。{user_prompt or '请分析上述文件。'}"

    history = _load_conversation(conversation_id) or [{"role": "system", "content": system_prompt}]

    if images:
        history.append({"role": "user", "content": _build_multimodal_user_content(user_prompt, images)})
    else:
        history.append({"role": "user", "content": user_prompt})
    return history, None


# ============================================================
# 6. AI 调用层（openai SDK）
# ============================================================

MAX_TOOL_ITERATIONS = int(os.environ.get("MAX_TOOL_ITERATIONS", 10))
# 单个工具结果注入上下文的最大字符数（防止一次性拉回几千台设备把上下文撑爆）
MAX_TOOL_RESULT_CHARS = int(os.environ.get("MAX_TOOL_RESULT_CHARS", 20000))
# 返回给前端的 result_preview 截断长度（严格截断，避免大结果撑爆前端）
TOOL_RESULT_PREVIEW_CHARS = int(os.environ.get("TOOL_RESULT_PREVIEW_CHARS", 500))

# 连接测试（POST /api/test_connection）的硬超时与错误信息长度上限
TEST_CONNECTION_TIMEOUT_SECONDS = _env_float("TEST_CONNECTION_TIMEOUT_SECONDS", 15.0)
TEST_CONNECTION_ERROR_CHARS = 500
TEST_CONNECTION_DETAIL_LIMIT = 5

# /api/chat、/api/chat_async 里 enable_tools 的默认值（安全默认：不挂工具）
# 前端可以从 /api/health 的 tools.enabled_by_default 读到这个值
TOOLS_ENABLED_BY_DEFAULT = _env_bool("TOOLS_ENABLED_BY_DEFAULT", False)

# 同步路由（/api/chat、/api/ai_task_status、/api/chat_conversations 等）跑在 anyio 线程池里，
# 快速模型调用会长时间占用线程；池子太小会让「所有」接口一起排队卡住。
MAX_SYNC_THREADS = int(os.environ.get("MAX_SYNC_THREADS", 64))
# uvicorn 访问日志（每个请求一行）；日志面板刷屏时可设为 false
ACCESS_LOG = _env_bool("ACCESS_LOG", True)

# 工具集在 tools/ 包里（azure_tools / meraki_tools / ai_tools），
# run_ai_with_tools(tools="default") 会挂载 tools.default_schemas()。

_USAGE_TOTALS: dict[str, dict] = {}
_USAGE_LOCK = threading.Lock()


def get_ai_client(provider: Optional[str] = None):
    """按 provider 构造 OpenAI 客户端（Kimi / DeepSeek 都是 OpenAI 兼容接口）"""
    config = _provider_config(provider)
    if not config.get("api_key"):
        raise RuntimeError(
            f"未配置 {config['api_key_env']}，无法调用 {config['label']}。"
            "请写入环境变量或项目根目录的 .env（参考 .env.example）。"
        )

    import httpx
    from openai import OpenAI

    return OpenAI(
        api_key=config["api_key"],
        base_url=config["base_url"],
        default_headers=_openai_extra_headers(config) or None,
        timeout=httpx.Timeout(AI_REQUEST_TIMEOUT_SECONDS, connect=10.0),
        max_retries=1,
    )


def _openai_extra_headers(config: dict) -> dict:
    """
    Azure 的 OpenAI 兼容端点（*.openai.azure.com/openai/v1、*.services.ai.azure.com）
    惯用 api-key 头；auto 模式下识别到这类域名就同时带上，真实 OpenAI 不受影响。
    可用 OPENAI_AUTH_MODE=bearer / api-key 强制指定。
    """
    api_key = config.get("api_key")
    if not api_key:
        return {}

    mode = str(config.get("auth_mode") or "auto").strip().lower()
    base_url = str(config.get("base_url") or "").lower()
    looks_azure = (
        "openai.azure.com" in base_url
        or "services.ai.azure.com" in base_url
        or "/openai/v1" in base_url
    )

    if mode in ("api-key", "apikey", "azure", "api_key") or (mode == "auto" and looks_azure):
        return {"api-key": api_key}
    return {}


def _record_usage(provider: str, model: str, usage: Any) -> None:
    """把 token 用量累计到内存（本地后端只做统计，不做计费）"""
    prompt_tokens = int(getattr(usage, "prompt_tokens", 0) or 0)
    completion_tokens = int(getattr(usage, "completion_tokens", 0) or 0)
    total_tokens = int(getattr(usage, "total_tokens", 0) or 0)

    with _USAGE_LOCK:
        entry = _USAGE_TOTALS.setdefault(
            provider, {"calls": 0, "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
        )
        entry["calls"] += 1
        entry["prompt_tokens"] += prompt_tokens
        entry["completion_tokens"] += completion_tokens
        entry["total_tokens"] += total_tokens

    logger.info(
        "[AI] %s/%s usage: prompt=%s completion=%s total=%s",
        provider, model, prompt_tokens, completion_tokens, total_tokens,
    )


def usage_summary() -> dict:
    with _USAGE_LOCK:
        return {provider: dict(entry) for provider, entry in _USAGE_TOTALS.items()}


def _mock_reply(provider: str, model: str, messages: list, tools: Optional[list] = None) -> str:
    """未配置 Key 时的联调回复：能验证多轮历史、模型选择、异步链路是否通"""
    if AI_MOCK_DELAY_SECONDS > 0:
        time.sleep(AI_MOCK_DELAY_SECONDS)

    last_user = next(
        (
            _flatten_content_to_text(m.get("content"))
            for m in reversed(messages)
            if isinstance(m, dict) and m.get("role") == "user"
        ),
            "",
    )
    env_name = _provider_config(provider)["api_key_env"]
    tools_line = ""
    if tools:
        names = [schema.get("function", {}).get("name") for schema in tools]
        tools_line = f"已挂载 {len(names)} 个工具：{', '.join(str(n) for n in names)}\n"

    return (
        f"[mock 模式] 未检测到 {env_name}，本回复由本地假数据生成。\n"
        f"provider={provider}, model={model}, 本次共 {len(messages)} 条消息。\n"
        f"{tools_line}"
        f"你说的是：{last_user[:200]}\n"
        f"在 .env 里配置 {env_name} 后即可切换到真实模型。"
    )


def _resolve_tools(tools: Any) -> Optional[list]:
    """
    tools 参数的三态：
      None / [] / False        → 不挂工具（纯对话）
      "default" / "all" / True → 挂载 tools.default_schemas()（第一批 8 个工具）
      ["工具名", ...]           → 只挂载这些工具
      list[dict]               → 原样使用调用方给的 schema
    """
    if tools is None or tools is False or tools == []:
        return None
    if tools is True or (isinstance(tools, str) and tools.strip().lower() in ("default", "all", "true")):
        return tool_registry.default_schemas()
    if isinstance(tools, list):
        if all(isinstance(item, str) for item in tools):
            wanted = {str(item).strip() for item in tools}
            selected = [
                schema for schema in tool_registry.default_schemas()
                if schema.get("function", {}).get("name") in wanted
            ]
            if not selected:
                logger.warning("tools 里没有匹配的内置工具：%s", sorted(wanted))
                return None
            return selected
        return tools
    logger.warning("无法解析 tools=%r，按纯对话处理", tools)
    return None


def _sanitize_history(messages: list) -> list:
    """
    发给模型的消息只保留 role/content（以及工具循环需要的 tool_calls / tool_call_id），
    避免把 tool_calls_log、iterations_used 这类自定义字段带进请求。
    """
    cleaned: list[dict] = []
    for message in messages or []:
        if not isinstance(message, dict):
            continue

        role = message.get("role")
        if role not in ("system", "user", "assistant", "tool"):
            continue

        item: dict = {"role": role, "content": message.get("content")}
        if role == "tool":
            item["tool_call_id"] = message.get("tool_call_id")
            if message.get("name"):
                item["name"] = message["name"]
        if message.get("tool_calls"):
            item["tool_calls"] = message["tool_calls"]
        cleaned.append(item)
    return cleaned


def _execute_tool(name: str, arguments: dict) -> str:
    """分发到 tools 包；未知工具与异常都返回 {"status": "error", "message": ...}"""
    try:
        result = tool_registry.execute_tool(name, arguments)
    except Exception as exc:
        # 不打完整堆栈：工具失败（无凭据、网络、401）是常见情况，堆栈会把日志面板刷爆
        logger.error("[Tool] %s 执行失败：%s: %s", name, type(exc).__name__, exc)
        return json.dumps({"status": "error", "message": str(exc)}, ensure_ascii=False)

    if result is None:
        return json.dumps({"status": "error", "message": f"未注册的工具：{name}"}, ensure_ascii=False)

    if len(result) > MAX_TOOL_RESULT_CHARS:
        logger.warning("[Tool] %s 结果过长（%s 字符），已截断", name, len(result))
        result = result[:MAX_TOOL_RESULT_CHARS] + f"\n…（工具结果超过 {MAX_TOOL_RESULT_CHARS} 字符已截断）"
    return result


def _tool_result_status(result: Any) -> str:
    """从工具返回的 JSON 里读 status，非 success 一律算 error"""
    if not isinstance(result, str):
        return "error"
    try:
        payload = json.loads(result)
    except json.JSONDecodeError:
        return "error"
    if isinstance(payload, dict) and str(payload.get("status", "")).lower() == "success":
        return "success"
    return "error"


def _tool_result_preview(result: Any, limit: int = TOOL_RESULT_PREVIEW_CHARS) -> str:
    """严格截断到 limit 个字符（默认 500）"""
    text = result if isinstance(result, str) else json.dumps(result, ensure_ascii=False)
    return text[:limit]


def run_ai_with_tools(
    messages: list,
    subscription_id: Optional[str] = None,
    ai_provider: Optional[str] = None,
    tools: Any = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
) -> dict:
    """
    执行一次 AI 对话，返回：
        {
          "response": "最终 AI 回复",
          "tool_calls_log": [ {iteration, tool_name, arguments, result_preview,
                               result_status, duration_ms}, ... ],
          "iterations_used": 2,      # 实际发生的模型调用轮数（mock 模式下是 0）
        }

    - 未配置 Key（AI_MOCK_MODE=auto）时返回 mock 回复，方便前端联调；
    - 配置了 Key 时走 openai SDK 的真实请求；
    - tools="default" 时挂载 tools/ 包里的默认工具集，并进入多轮工具调用循环；
      调用方也可以直接传 schema 列表；subscription_id 会自动注入工具参数。
    - 只要文本回复时用 run_ai_with_tools_text()。
    """
    provider = _normalize_provider(ai_provider)
    tool_list = _resolve_tools(tools)
    tool_calls_log: list[dict] = []

    # 历史里可能带着 tool_calls_log 等自定义字段，发给模型前先剥掉
    messages = _sanitize_history(messages)

    if tool_list:
        logger.info(
            "[AI] 挂载 %s 个工具：%s",
            len(tool_list),
            [schema.get("function", {}).get("name") for schema in tool_list],
        )

    if _should_mock(provider):
        logger.info("[AI] provider=%s 未配置 Key，返回 mock 回复", provider)
        return {
            "response": _mock_reply(provider, _get_ai_model(provider, model), messages, tool_list),
            "tool_calls_log": [],
            "iterations_used": 0,
        }

    client = get_ai_client(provider)
    model = _get_ai_model(provider, model)

    # DeepSeek 默认模型不支持视觉，带图片时自动切到视觉模型
    config = _provider_config(provider)
    if provider == "deepseek" and _messages_contain_images(messages) and model == config["model"]:
        vision_model = config.get("vision_model")
        if vision_model:
            logger.info("[AI] 检测到图片输入，DeepSeek 切换视觉模型：%s", vision_model)
            model = vision_model

    request_kwargs: dict = {"model": model, "messages": messages}
    if tool_list:
        request_kwargs["tools"] = tool_list
        request_kwargs["tool_choice"] = "auto"
    if temperature is not None:
        request_kwargs["temperature"] = temperature

    max_iterations = MAX_TOOL_ITERATIONS if tool_list else 1
    logger.info("[AI] 调用 provider=%s model=%s 消息数=%s", provider, model, len(messages))

    for iteration in range(max_iterations):
        response = client.chat.completions.create(**request_kwargs)

        if getattr(response, "usage", None):
            _record_usage(provider, model, response.usage)

        message = response.choices[0].message

        if tool_list and getattr(message, "tool_calls", None):
            logger.info(
                "[AI] 第 %s 轮触发 %s 个工具调用：%s",
                iteration + 1, len(message.tool_calls), [c.function.name for c in message.tool_calls],
            )
            messages.append(
                {"role": "assistant", "content": message.content, "tool_calls": message.tool_calls}
            )

            for tool_call in message.tool_calls:
                try:
                    tool_args = json.loads(tool_call.function.arguments or "{}")
                except json.JSONDecodeError:
                    tool_args = {}
                if subscription_id and "subscription_id" not in tool_args:
                    tool_args["subscription_id"] = subscription_id

                # 记录工具调用（含耗时），result_preview 严格截断
                started_at = time.perf_counter()
                tool_result = _execute_tool(tool_call.function.name, tool_args)
                duration_ms = int((time.perf_counter() - started_at) * 1000)

                result_status = _tool_result_status(tool_result)
                tool_calls_log.append({
                    "iteration": iteration + 1,
                    "tool_name": tool_call.function.name,
                    "arguments": tool_args,
                    "result_preview": _tool_result_preview(tool_result),
                    "result_status": result_status,
                    "duration_ms": duration_ms,
                })
                logger.info(
                    "[Tool] 第 %s 轮 %s -> %s（%s ms）",
                    iteration + 1, tool_call.function.name, result_status, duration_ms,
                )

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.function.name,
                    "content": tool_result,
                })
            continue

        return {
            "response": message.content or "",
            "tool_calls_log": tool_calls_log,
            "iterations_used": iteration + 1,
        }

    raise RuntimeError(f"工具调用超过最大轮次（{MAX_TOOL_ITERATIONS}）")


def run_ai_with_tools_text(
    messages: list,
    subscription_id: Optional[str] = None,
    ai_provider: Optional[str] = None,
    tools: Any = None,
    model: Optional[str] = None,
    temperature: Optional[float] = None,
) -> str:
    """向后兼容包装：只取 response 字符串，忽略 tool_calls_log"""
    outcome = run_ai_with_tools(
        messages,
        subscription_id,
        ai_provider,
        tools=tools,
        model=model,
        temperature=temperature,
    )
    return outcome["response"]


# ============================================================
# 7. Webhook 回调
# ============================================================


def send_webhook(url: str, payload: dict) -> None:
    """异步任务完成后回调调用方；失败只记日志，不影响主流程"""
    try:
        body = json.dumps(payload, ensure_ascii=False).encode()
        request = urllib.request.Request(
            url, data=body, headers={"Content-Type": "application/json"}, method="POST"
        )
        with urllib.request.urlopen(request, timeout=15) as resp:
            logger.info("[Webhook] %s 返回 HTTP %s", url, resp.status)
    except Exception as exc:
        logger.warning("[Webhook] 回调失败（不影响主流程）：%s", exc)


# ============================================================
# 8. 后台任务执行体：chat_async 真正干活的地方
# ============================================================


def _run_chat_task(instance_id: str, payload: dict) -> None:
    """同步函数，Starlette 会把它丢到线程池执行，不阻塞事件循环"""
    conversation_id = payload.get("conversation_id") or uuid.uuid4().hex
    provider = _normalize_provider(payload.get("ai_provider"))
    enable_tools = bool(payload.get("enable_tools", TOOLS_ENABLED_BY_DEFAULT))
    webhook_url = payload.get("webhook_url")

    _update_task(instance_id, status="running")
    logger.info(
        "[ChatTask] %s 开始 provider=%s conversation=%s tools=%s",
        instance_id, provider, conversation_id, enable_tools,
    )

    try:
        messages, error = build_chat_messages(payload, conversation_id)
        if error:
            raise ValueError(error)

        outcome = run_ai_with_tools(
            messages,
            payload.get("subscription_id"),
            provider,
            tools=(payload.get("tools") or "default") if enable_tools else None,
            model=payload.get("model"),
            temperature=payload.get("temperature"),
        )
        result = outcome
        final_content = result["response"]
        tool_calls_log = result["tool_calls_log"]
        iterations_used = result["iterations_used"]

        # 助手消息连同工具调用日志一起落库，前端从历史里也能看到
        messages.append({
            "role": "assistant",
            "content": final_content,
            "tool_calls_log": tool_calls_log,
            "iterations_used": iterations_used,
            "tools_enabled": enable_tools,
        })
        _save_conversation(conversation_id, messages)

        _update_task(
            instance_id,
            status="completed",
            result={
                "status": "completed",
                "response": final_content,
                "tool_calls_log": tool_calls_log,
                "iterations_used": iterations_used,
                "tools_enabled": enable_tools,
                "conversation_id": conversation_id,
                "instance_id": instance_id,
            },
        )
        logger.info(
            "[ChatTask] %s 完成，回复长度=%s，工具调用 %s 次，模型轮数 %s",
            instance_id, len(final_content or ""), len(tool_calls_log), iterations_used,
        )

        if webhook_url:
            send_webhook(webhook_url, {
                "instance_id": instance_id,
                "status": "completed",
                "conversation_id": conversation_id,
                "response": final_content,
            })

    except Exception as exc:
        logger.error("[ChatTask] %s 执行失败：%s: %s", instance_id, type(exc).__name__, exc)
        _update_task(instance_id, status="failed", error=f"{type(exc).__name__}: {exc}")
        if webhook_url:
            send_webhook(webhook_url, {
                "instance_id": instance_id,
                "status": "failed",
                "conversation_id": conversation_id,
                "error": str(exc),
            })


# ============================================================
# 9. 连接测试（供前端配置向导使用）
#    直接调用工具、不走 AI 循环，所以不消耗 token；每次都实时打网络，不用任何缓存。
# ============================================================

CONNECTION_TARGETS = ("azure", "meraki", "deepseek", "kimi", "openai")


def _shorten_connection_error(message: Any, limit: int = TEST_CONNECTION_ERROR_CHARS) -> str:
    """压成一行并截断到 500 字符"""
    flattened = " ".join(str(message or "未知错误").split())
    if len(flattened) <= limit:
        return flattened
    return flattened[:limit]


def _tool_call_for_target(target: str) -> str:
    """同步执行工具（会被丢进线程池 + 15s 硬超时），返回工具原始 JSON 字符串"""
    if target == "azure":
        return azure_tools.list_subscriptions({})
    if target == "meraki":
        return meraki_tools.meraki_list_organizations({})
    if target == "deepseek":
        return ai_tools.deepseek_get_balance({})
    if target == "openai":
        return _openai_models_probe()
    raise ValueError(f"不支持的目标：{target}")


def _openai_models_probe() -> str:
    """
    用 GET /models 验证 OpenAI 的 Key（不消耗 token，OpenAI 兼容端点也支持）。
    返回结构与其它工具一致：成功带 data/count，失败带 message。
    """
    config = _provider_config("openai")
    api_key = config.get("api_key") or ""
    if not api_key:
        return json.dumps(
            {"status": "error", "message": f"未配置 {config['api_key_env']}"}, ensure_ascii=False
        )

    url = f"{str(config['base_url']).rstrip('/')}/models"
    # 与真实调用用同样的认证头（Azure 端点用 api-key）
    headers = {"Accept": "application/json", "Authorization": f"Bearer {api_key}"}
    headers.update(_openai_extra_headers(config))
    response = httpx.get(
        url,
        headers=headers,
        timeout=TEST_CONNECTION_TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        snippet = response.text[:300].replace("\n", " ")
        return json.dumps(
            {"status": "error", "message": f"OpenAI {url} 返回 HTTP {response.status_code}：{snippet}"},
            ensure_ascii=False,
        )

    payload = response.json() if response.text else {}
    models = [
        item.get("id") for item in (payload.get("data") or [])
        if isinstance(item, dict) and item.get("id")
    ]
    return json.dumps(
        {"status": "success", "base_url": config["base_url"], "data": models, "count": len(models)},
        ensure_ascii=False,
    )


def _summarize_subscriptions(payload: dict) -> tuple[str, list]:
    subscriptions = payload.get("subscriptions") or []
    count = payload.get("count", len(subscriptions))
    details = [
        {
            "name": item.get("name"),
            "id": item.get("id"),
            "state": item.get("state"),
        }
        for item in subscriptions[:TEST_CONNECTION_DETAIL_LIMIT]
        if isinstance(item, dict)
    ]
    if not subscriptions:
        return "连接成功，但当前凭据下没有可访问的订阅", details
    return f"连接成功，发现 {count} 个订阅", details


def _summarize_organizations(payload: dict) -> tuple[str, list]:
    organizations = payload.get("data") or []
    count = payload.get("count", len(organizations))
    details = [
        {"name": item.get("name"), "id": item.get("id")}
        for item in organizations[:TEST_CONNECTION_DETAIL_LIMIT]
        if isinstance(item, dict)
    ]
    if not organizations:
        return "连接成功，但该 API Key 下没有可访问的 Meraki 组织", details
    return f"连接成功，发现 {count} 个组织", details


def _summarize_balance(payload: dict) -> tuple[str, list]:
    balance_infos = payload.get("balance_infos") or []
    details = [
        {
            "currency": item.get("currency"),
            "total_balance": item.get("total_balance"),
            "granted_balance": item.get("granted_balance"),
            "topped_up_balance": item.get("topped_up_balance"),
        }
        for item in balance_infos[:TEST_CONNECTION_DETAIL_LIMIT]
        if isinstance(item, dict)
    ]

    if payload.get("is_available") is False:
        return "连接成功，但账户当前不可用（余额不足或未充值）", details

    balances = "、".join(
        f"{item['currency']} {item['total_balance']}"
        for item in details
        if item.get("currency") and item.get("total_balance") is not None
    )
    if balances:
        return f"连接成功，余额可用（{balances}）", details
    return "连接成功，DeepSeek 接口可访问", details


def _summarize_connection(target: str, payload: dict) -> tuple[str, list]:
    if target == "azure":
        return _summarize_subscriptions(payload)
    if target == "meraki":
        return _summarize_organizations(payload)
    if target == "openai":
        return _summarize_openai_models(payload)
    return _summarize_balance(payload)


def _summarize_openai_models(payload: dict) -> tuple[str, list]:
    models = payload.get("data") or []
    count = payload.get("count", len(models))
    details = [{"model": name} for name in models[:TEST_CONNECTION_DETAIL_LIMIT]]
    if not models:
        return "连接成功，OpenAI 接口可访问（未返回可用模型列表）", details
    preview = "、".join(str(details_item["model"]) for details_item in details)
    return f"连接成功，可用模型 {count} 个（如 {preview}）", details


# ============================================================
# 10. FastAPI 应用与路由
# ============================================================

def _tune_sync_threadpool() -> None:
    """
    同步路由默认跑在任何 anyio 的 40 线程池里：几个长请求就能占满，
    之后连 /api/ai_task_status、/api/chat_conversations 都会排队，表现为「整个 App 卡住」。
    这里把上限提到 MAX_SYNC_THREADS（需要在一个运行中的事件循环里改，所以放在 lifespan）。
    """
    try:
        from anyio import to_thread

        limiter = to_thread.current_default_thread_limiter()
        if limiter.total_tokens < MAX_SYNC_THREADS:
            limiter.total_tokens = MAX_SYNC_THREADS
        logger.info("同步路由线程池上限：%s", limiter.total_tokens)
    except Exception as exc:  # 拿不到就沿用默认值，不影响功能
        logger.warning("调整线程池上限失败（沿用默认值）：%s", exc)


@asynccontextmanager
async def lifespan(_: FastAPI):
    report = arch_report()
    logger.info(
        "运行架构：machine=%s translated=%s native_arm64=%s frozen=%s",
        report["machine"],
        report["translated"],
        report["native_arm64"],
        report["frozen"],
    )
    _tune_sync_threadpool()
    yield


app = FastAPI(
    title="Local AI Chat API",
    version="1.0.0",
    description="本地 AI 对话后端：FastAPI + openai SDK，支持 Kimi / DeepSeek 与 mock 联调模式",
    lifespan=lifespan,
)


@app.exception_handler(HTTPException)
async def http_exception_handler(request: Request, exc: HTTPException) -> JSONResponse:
    """错误体统一为 {"error": ...}，与前端既有契约保持一致"""
    return JSONResponse(status_code=exc.status_code, content={"error": exc.detail})


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    username: Optional[str] = None
    password: Optional[str] = None


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    prompt: Optional[str] = None
    messages: Optional[list] = None
    images: Optional[Any] = None
    files: Optional[Any] = None
    conversation_id: Optional[str] = None
    system_prompt: Optional[str] = None
    enable_tools: bool = TOOLS_ENABLED_BY_DEFAULT
    tools: Optional[list] = None
    ai_provider: Optional[str] = None
    provider: Optional[str] = None
    model: Optional[str] = None
    temperature: Optional[float] = None
    subscription_id: Optional[str] = None
    webhook_url: Optional[str] = None


class TestConnectionRequest(BaseModel):
    """连接测试请求：{"target": "azure" | "meraki" | "deepseek" | "kimi"}"""

    model_config = ConfigDict(extra="allow")

    target: Optional[str] = None


def _body_dict(model: BaseModel) -> dict:
    """兼容 pydantic v1 / v2"""
    if hasattr(model, "model_dump"):
        return model.model_dump(exclude_none=True)
    return model.dict(exclude_none=True)


def _chat_body_to_payload(req_body: dict) -> dict:
    """整理成后台任务 / 对话流程用的 payload"""
    payload = dict(req_body)
    payload["conversation_id"] = req_body.get("conversation_id") or uuid.uuid4().hex
    payload["ai_provider"] = (
        req_body.get("ai_provider") or req_body.get("provider") or GENERAL_CHAT_PROVIDER
    )
    return payload


# ---------- 认证 ----------


@app.post(f"{API_PREFIX}/login", tags=["auth"])
def login(payload: LoginRequest) -> JSONResponse:
    """用户名密码换 JWT（默认 admin / password123，可用环境变量覆盖）"""
    if payload.username == ADMIN_USERNAME and payload.password == ADMIN_PASSWORD:
        return JSONResponse({"token": _encode_token(payload.username, provider="local")})
    raise HTTPException(status_code=401, detail="Invalid credentials")


@app.post(f"{API_PREFIX}/auth/google/start", tags=["auth"])
def start_google_login(request: Request) -> JSONResponse:
    """创建一次 Google OAuth + PKCE 登录，桌面 App 打开 authorization_url 后轮询 status。"""
    if not GOOGLE_CLIENT_ID:
        raise HTTPException(status_code=503, detail="Google 登录尚未配置（缺少 GOOGLE_CLIENT_ID）")

    _cleanup_google_logins()
    state = secrets.token_urlsafe(32)
    verifier = secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b"=").decode()
    redirect_uri = _google_redirect_uri(request)
    params = {
        "client_id": GOOGLE_CLIENT_ID,
        "redirect_uri": redirect_uri,
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
        "access_type": "online",
        "prompt": "select_account",
    }
    authorization_url = "https://accounts.google.com/o/oauth2/v2/auth?" + urllib.parse.urlencode(params)
    with _GOOGLE_LOGIN_LOCK:
        _GOOGLE_LOGINS[state] = {
            "created_at": time.time(),
            "status": "waiting",
            "code_verifier": verifier,
            "redirect_uri": redirect_uri,
        }
    return JSONResponse({
        "authorization_url": authorization_url,
        "state": state,
        "expires_in": int(GOOGLE_OAUTH_STATE_TTL_SECONDS),
    })


@app.get(f"{API_PREFIX}/auth/google/callback", tags=["auth"], response_class=HTMLResponse)
async def google_login_callback(
    state: str = Query(default=""),
    code: str = Query(default=""),
    error: str = Query(default=""),
) -> HTMLResponse:
    """Google 重定向到本地后端；完成换 token 后，由桌面 App 通过 state 领取本 App JWT。"""
    _cleanup_google_logins()
    with _GOOGLE_LOGIN_LOCK:
        pending = _GOOGLE_LOGINS.get(state)
        existing_status = pending.get("status") if pending else None
        if existing_status == "waiting":
            pending["status"] = "processing"

    if not state or not pending:
        return _google_callback_page("登录请求已失效", "请返回 App 重新发起 Google 登录。", success=False)
    if existing_status == "complete":
        return _google_callback_page("Google 登录成功", "此登录已经完成。", success=True)
    if existing_status == "error":
        return _google_callback_page(
            "Google 登录失败", str(pending.get("error") or "此登录请求已经失败。"), success=False
        )
    if existing_status != "waiting":
        return _google_callback_page("正在处理登录", "请返回 App 等待登录结果。", success=True)
    if error:
        message = f"Google 拒绝了登录：{error}"
        with _GOOGLE_LOGIN_LOCK:
            pending.update(status="error", error=message)
        return _google_callback_page("Google 登录未完成", message, success=False)
    if not code:
        message = "Google 回调中缺少授权码。"
        with _GOOGLE_LOGIN_LOCK:
            pending.update(status="error", error=message)
        return _google_callback_page("Google 登录失败", message, success=False)

    try:
        token_payload = {
            "client_id": GOOGLE_CLIENT_ID,
            "code": code,
            "code_verifier": pending["code_verifier"],
            "grant_type": "authorization_code",
            "redirect_uri": pending["redirect_uri"],
        }
        if GOOGLE_CLIENT_SECRET:
            token_payload["client_secret"] = GOOGLE_CLIENT_SECRET

        async with httpx.AsyncClient(timeout=15.0) as client:
            token_response = await client.post("https://oauth2.googleapis.com/token", data=token_payload)
            token_response.raise_for_status()
            access_token = token_response.json().get("access_token")
            if not access_token:
                raise RuntimeError("Google 未返回 access_token")
            user_response = await client.get(
                "https://openidconnect.googleapis.com/v1/userinfo",
                headers={"Authorization": f"Bearer {access_token}"},
            )
            user_response.raise_for_status()
            profile = user_response.json()

        email = str(profile.get("email") or "").strip().lower()
        subject = str(profile.get("sub") or "").strip()
        if not email or not subject or profile.get("email_verified") is not True:
            raise RuntimeError("Google 账号没有已验证的邮箱地址")
        if not _google_email_allowed(email):
            raise PermissionError(f"账号 {email} 不在允许登录的范围内")

        app_token = _encode_token(email, provider="google", subject=f"google:{subject}")
        with _GOOGLE_LOGIN_LOCK:
            pending.update(status="complete", token=app_token, email=email)
        return _google_callback_page("Google 登录成功", f"已验证账号 {email}。", success=True)
    except Exception as exc:
        logger.warning("Google OAuth 回调失败：%s", exc)
        message = str(exc)[:300] or "Google 登录失败"
        with _GOOGLE_LOGIN_LOCK:
            pending.update(status="error", error=message)
        return _google_callback_page("Google 登录失败", message, success=False)


@app.get(f"{API_PREFIX}/auth/google/status", tags=["auth"])
def google_login_status(state: str = Query(...)) -> JSONResponse:
    """桌面 App 轮询登录状态；成功结果只能领取一次。"""
    _cleanup_google_logins()
    with _GOOGLE_LOGIN_LOCK:
        pending = _GOOGLE_LOGINS.get(state)
        if not pending:
            raise HTTPException(status_code=404, detail="Google 登录请求不存在或已过期")
        status = pending.get("status", "waiting")
        if status == "complete":
            result = {"status": status, "token": pending["token"], "email": pending["email"]}
            _GOOGLE_LOGINS.pop(state, None)
            return JSONResponse(result)
        if status == "error":
            return JSONResponse({"status": status, "error": pending.get("error", "Google 登录失败")})
        return JSONResponse({"status": status})


@app.post(f"{API_PREFIX}/auth/github/start", tags=["auth"])
async def start_github_login() -> JSONResponse:
    """创建 GitHub Device Flow；用户在浏览器输入短码，App 通过 state 轮询结果。"""
    if not GITHUB_CLIENT_ID:
        raise HTTPException(status_code=503, detail="GitHub 登录尚未配置（缺少 GITHUB_CLIENT_ID）")

    _cleanup_github_logins()
    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            response = await client.post(
                "https://github.com/login/device/code",
                data={"client_id": GITHUB_CLIENT_ID, "scope": "read:user user:email"},
                headers={"Accept": "application/json", "User-Agent": "AIChatApp"},
            )
            response.raise_for_status()
            device = response.json()
    except Exception as exc:
        logger.warning("GitHub Device Flow 启动失败：%s", exc)
        raise HTTPException(status_code=502, detail=f"无法连接 GitHub：{str(exc)[:300]}") from exc

    device_code = str(device.get("device_code") or "")
    user_code = str(device.get("user_code") or "")
    verification_uri = str(device.get("verification_uri") or "")
    if not device_code or not user_code or not verification_uri:
        raise HTTPException(status_code=502, detail="GitHub 返回的 Device Flow 数据不完整")

    expires_in = max(60, int(device.get("expires_in") or GITHUB_OAUTH_STATE_TTL_SECONDS))
    interval = max(5, int(device.get("interval") or 5))
    state = secrets.token_urlsafe(32)
    with _GITHUB_LOGIN_LOCK:
        _GITHUB_LOGINS[state] = {
            "status": "waiting",
            "device_code": device_code,
            "user_code": user_code,
            "verification_uri": verification_uri,
            "expires_at": time.time() + min(expires_in, GITHUB_OAUTH_STATE_TTL_SECONDS),
            "interval": interval,
            "next_poll_at": 0.0,
            "polling": False,
        }
    return JSONResponse({
        "state": state,
        "user_code": user_code,
        "verification_uri": verification_uri,
        "expires_in": expires_in,
        "interval": interval,
    })


@app.get(f"{API_PREFIX}/auth/github/status", tags=["auth"])
async def github_login_status(state: str = Query(...)) -> JSONResponse:
    """按 GitHub 要求的间隔轮询 Device Flow；成功后一次性交付本 App JWT。"""
    _cleanup_github_logins()
    now = time.time()
    with _GITHUB_LOGIN_LOCK:
        pending = _GITHUB_LOGINS.get(state)
        if not pending:
            raise HTTPException(status_code=404, detail="GitHub 登录请求不存在或已过期")
        status = pending.get("status", "waiting")
        if status == "complete":
            result = {
                "status": status,
                "token": pending["token"],
                "login": pending["login"],
                "email": pending.get("email"),
            }
            _GITHUB_LOGINS.pop(state, None)
            return JSONResponse(result)
        if status == "error":
            return JSONResponse({"status": status, "error": pending.get("error", "GitHub 登录失败")})
        if pending.get("polling") or now < pending.get("next_poll_at", 0):
            return JSONResponse({"status": "waiting"})
        pending["polling"] = True
        pending["next_poll_at"] = now + pending["interval"]
        device_code = pending["device_code"]

    try:
        headers = {"Accept": "application/json", "User-Agent": "AIChatApp"}
        async with httpx.AsyncClient(timeout=15.0) as client:
            token_response = await client.post(
                "https://github.com/login/oauth/access_token",
                data={
                    "client_id": GITHUB_CLIENT_ID,
                    "device_code": device_code,
                    "grant_type": "urn:ietf:params:oauth:grant-type:device_code",
                },
                headers=headers,
            )
            token_response.raise_for_status()
            token_data = token_response.json()
            oauth_error = str(token_data.get("error") or "")
            if oauth_error:
                with _GITHUB_LOGIN_LOCK:
                    pending["polling"] = False
                    if oauth_error == "authorization_pending":
                        return JSONResponse({"status": "waiting"})
                    if oauth_error == "slow_down":
                        pending["interval"] += 5
                        pending["next_poll_at"] = time.time() + pending["interval"]
                        return JSONResponse({"status": "waiting"})
                    message = str(token_data.get("error_description") or oauth_error)[:300]
                    pending.update(status="error", error=message)
                    return JSONResponse({"status": "error", "error": message})

            access_token = str(token_data.get("access_token") or "")
            if not access_token:
                raise RuntimeError("GitHub 未返回 access_token")
            api_headers = {
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {access_token}",
                "User-Agent": "AIChatApp",
                "X-GitHub-Api-Version": "2022-11-28",
            }
            profile_response = await client.get("https://api.github.com/user", headers=api_headers)
            profile_response.raise_for_status()
            profile = profile_response.json()
            emails_response = await client.get("https://api.github.com/user/emails", headers=api_headers)
            emails_response.raise_for_status()
            emails = emails_response.json()

        login_name = str(profile.get("login") or "").strip()
        github_id = str(profile.get("id") or "").strip()
        verified_emails = [
            str(item.get("email") or "").strip().lower()
            for item in emails if item.get("verified") is True and item.get("email")
        ]
        primary = next(
            (
                str(item.get("email") or "").strip().lower()
                for item in emails
                if item.get("verified") is True and item.get("primary") is True and item.get("email")
            ),
            verified_emails[0] if verified_emails else "",
        )
        if not login_name or not github_id:
            raise RuntimeError("GitHub 用户资料缺少 login 或 id")
        if not _github_identity_allowed(login_name, primary):
            raise PermissionError(f"GitHub 账号 {login_name} 不在允许登录的范围内")

        username = primary or login_name
        app_token = _encode_token(username, provider="github", subject=f"github:{github_id}")
        with _GITHUB_LOGIN_LOCK:
            pending.update(
                status="complete", token=app_token, login=login_name, email=primary, polling=False
            )
            _GITHUB_LOGINS.pop(state, None)
        return JSONResponse({"status": "complete", "token": app_token, "login": login_name, "email": primary})
    except Exception as exc:
        logger.warning("GitHub Device Flow 轮询失败：%s", exc)
        message = str(exc)[:300] or "GitHub 登录失败"
        with _GITHUB_LOGIN_LOCK:
            pending.update(status="error", error=message, polling=False)
        return JSONResponse({"status": "error", "error": message})


@app.get(f"{API_PREFIX}/health", tags=["auth"])
async def health() -> JSONResponse:
    """
    健康检查 + AI 配置概览 + 存储后端（sqlite / memory），方便 App 显示状态。
    Azure 的 configured 取自后台探测快照：这条请求永不阻塞——还没探测完就返回
    configured=null / credential_source="probing"（后台每 30 秒刷新一次）。
    """
    azure_probe = azure_tools.current_probe()  # 只读快照，不等待凭据链

    return JSONResponse({
        "status": "ok",
        # 进程信息（前端检查清单用）
        "python": {
            "version": platform.python_version(),
            "executable": sys.executable,
            "implementation": platform.python_implementation(),
        },
        # 运行架构：native_arm64=true 且 translated=false 才算“原生 Apple Silicon”
        "arch": arch_report(),
        "uptime_seconds": int(time.time() - PROCESS_STARTED_AT),
        "auth": {
            "local_admin": {"enabled": True},
            "google": {
                "configured": bool(GOOGLE_CLIENT_ID),
                "redirect_uri": GOOGLE_REDIRECT_URI or "automatic_loopback",
                "restricted": bool(GOOGLE_ALLOWED_EMAILS or GOOGLE_ALLOWED_DOMAINS),
            },
            "github": {
                "configured": bool(GITHUB_CLIENT_ID),
                "flow": "device",
                "restricted": bool(GITHUB_ALLOWED_LOGINS or GITHUB_ALLOWED_EMAILS),
            },
        },
        "storage": _STORAGE_BACKEND,
        "ai": {
            "mock_mode": AI_MOCK_MODE,
            "default_provider": DEFAULT_AI_PROVIDER,
            "default_chat_provider": GENERAL_CHAT_PROVIDER,
            "providers": {
                name: {
                    "label": config["label"],
                    "configured": bool(config.get("api_key")),
                    "model": config["model"],
                    "base_url": config["base_url"],
                    "api_key_env": config["api_key_env"],
                }
                for name, config in PROVIDERS.items()
            },
        },
        "storage_detail": {
            "backend": _STORAGE_BACKEND,
            # storage 顶层字段保持原来的字符串形式，这里再给一份 type，方便按 storage.type 取值
            "type": _STORAGE_BACKEND,
            "db_path": DB_PATH if _STORAGE_BACKEND == "sqlite" else None,
            "error": _STORAGE_ERROR,
            "task_ttl_hours": TASK_TTL_HOURS,
            "task_ttl_seconds": TASK_TTL_SECONDS,
        },
        "azure": azure_tools.health_with_probe(azure_probe),
        "meraki": tool_registry.MODULES["meraki"].health(),
        "tools": {
            **tool_registry.health(),
            "enabled_by_default": TOOLS_ENABLED_BY_DEFAULT,
        },
        "usage": usage_summary(),
    })


# ---------- 同步对话 ----------


@app.post(f"{API_PREFIX}/test_connection", tags=["tools"])
async def test_connection(
    payload: TestConnectionRequest,
    _: None = Depends(require_auth),
) -> JSONResponse:
    """
    连接测试（配置向导用）：直接调用工具，不走 AI 循环、不消耗 token。
    每次都实时打网络，不复用 /api/health 的任何缓存。
    """
    target = str(payload.target or "").strip().lower()
    if target not in CONNECTION_TARGETS:
        raise HTTPException(
            status_code=400,
            detail=f"target 必须是 {' / '.join(CONNECTION_TARGETS)} 之一",
        )

    started_at = time.perf_counter()

    def elapsed_ms() -> int:
        return int((time.perf_counter() - started_at) * 1000)

    # Kimi 余额工具还没实现，直接告知前端，不假装成功
    if target == "kimi":
        logger.info("[TestConnection] target=kimi status=not_implemented duration=%sms", elapsed_ms())
        return JSONResponse({
            "status": "not_implemented",
            "message": "Kimi 余额查询工具尚未实现（后端只注册了 deepseek_get_balance）",
            "duration_ms": elapsed_ms(),
        })

    try:
        raw_result = await asyncio.wait_for(
            asyncio.to_thread(_tool_call_for_target, target),
            timeout=TEST_CONNECTION_TIMEOUT_SECONDS,
        )
    except (asyncio.TimeoutError, TimeoutError):
        logger.warning("[TestConnection] target=%s status=timeout duration=%sms", target, elapsed_ms())
        return JSONResponse({
            "status": "error",
            "message": f"连接超时（{int(TEST_CONNECTION_TIMEOUT_SECONDS)}s）",
            "duration_ms": elapsed_ms(),
        })
    except Exception as exc:  # 兜底：工具本身已把异常转成 JSON，这里只防意外
        logger.exception("[TestConnection] target=%s 调用异常", target)
        return JSONResponse({
            "status": "error",
            "message": _shorten_connection_error(f"{type(exc).__name__}: {exc}"),
            "duration_ms": elapsed_ms(),
        })

    try:
        parsed = json.loads(raw_result)
    except (TypeError, json.JSONDecodeError):
        parsed = {"status": "error", "message": raw_result}

    if not isinstance(parsed, dict) or parsed.get("status") != "success":
        message = _shorten_connection_error(
            parsed.get("message") if isinstance(parsed, dict) else parsed
        )
        logger.warning(
            "[TestConnection] target=%s status=error duration=%sms message=%s",
            target, elapsed_ms(), message,
        )
        return JSONResponse({
            "status": "error",
            "message": message,
            "duration_ms": elapsed_ms(),
        })

    message, details = _summarize_connection(target, parsed)
    logger.info(
        "[TestConnection] target=%s status=success duration=%sms message=%s",
        target, elapsed_ms(), message,
    )
    return JSONResponse({
        "status": "success",
        "message": message,
        "details": details,
        "duration_ms": elapsed_ms(),
    })


@app.post(f"{API_PREFIX}/chat", tags=["chat"])
def general_chat(payload: ChatRequest, _: None = Depends(require_auth)) -> JSONResponse:
    """
    同步对话，直接返回最终回复。
    真实模型调用是阻塞的，长回复请改用 /api/chat_async。
    """
    data = _chat_body_to_payload(_body_dict(payload))
    conversation_id = data["conversation_id"]
    provider = _normalize_provider(data.get("ai_provider"))
    enable_tools = bool(data.get("enable_tools", TOOLS_ENABLED_BY_DEFAULT))

    try:
        messages, error = build_chat_messages(data, conversation_id)
        if error:
            raise HTTPException(status_code=400, detail=error)

        outcome = run_ai_with_tools(
            messages,
            data.get("subscription_id"),
            provider,
            tools=(data.get("tools") or "default") if enable_tools else None,
            model=data.get("model"),
            temperature=data.get("temperature"),
        )
        result = outcome
        final_content = result["response"]
        tool_calls_log = result["tool_calls_log"]
        iterations_used = result["iterations_used"]
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("[Chat] 调用失败：%s: %s", type(exc).__name__, exc)
        raise HTTPException(status_code=500, detail=str(exc)) from exc

    messages.append({
        "role": "assistant",
        "content": final_content,
        "tool_calls_log": tool_calls_log,
        "iterations_used": iterations_used,
        "tools_enabled": enable_tools,
    })
    _save_conversation(conversation_id, messages)

    return JSONResponse({
        "status": "success",
        "response": final_content,
        "tool_calls_log": tool_calls_log,
        "iterations_used": iterations_used,
        "conversation_id": conversation_id,
        "provider": provider,
        "model": _get_ai_model(provider, data.get("model")),
        "mock": _should_mock(provider),
        "tools_enabled": enable_tools,
    })


# ---------- 异步对话（后台任务） ----------


@app.post(f"{API_PREFIX}/chat_async", tags=["chat"], status_code=202)
def general_chat_async(
    background_tasks: BackgroundTasks,
    payload: ChatRequest,
    _: None = Depends(require_auth),
) -> JSONResponse:
    """
    异步对话：立刻返回 202 + instance_id，真正的 AI 调用在后台线程执行，
    最终回复写回内存任务表，客户端轮询 GET /api/ai_task_status?instance_id=... 取结果。
    """
    req_body = _body_dict(payload)
    if not req_body.get("prompt") and not req_body.get("messages") \
            and not req_body.get("images") and not req_body.get("files"):
        raise HTTPException(status_code=400, detail="请提供 'prompt' 或 'messages'")

    data = _chat_body_to_payload(req_body)
    instance_id = _create_task(data["conversation_id"])
    background_tasks.add_task(_run_chat_task, instance_id, data)

    logger.info("[Chat] 异步任务已登记：%s", instance_id)
    return JSONResponse(
        status_code=202,
        content={
            "instance_id": instance_id,
            "status": "accepted",
            "conversation_id": data["conversation_id"],
            "poll_endpoint": "ai_task_status",
            # 回显本次请求是否挂载了工具（默认值见 TOOLS_ENABLED_BY_DEFAULT）
            "tools_enabled": bool(data.get("enable_tools", TOOLS_ENABLED_BY_DEFAULT)),
        },
    )


@app.get(f"{API_PREFIX}/ai_task_status", tags=["chat"])
def ai_task_status(
    instance_id: str = Query(...),
    _: None = Depends(require_auth),
) -> JSONResponse:
    """查询异步任务状态：pending / running / completed / failed"""
    task = _get_task(instance_id)
    if task is None:
        return JSONResponse(
            status_code=404,
            content={
                "error": "任务不存在或已被清理",
                "instance_id": instance_id,
                "status": "not_found",
            },
        )

    body: dict = {
        "instance_id": instance_id,
        "status": task["status"],
        "created_at": task.get("created_at"),
        "last_updated_at": task.get("last_updated_at"),
    }

    if task["status"] == "completed":
        body["result"] = task.get("result")
    elif task["status"] == "failed":
        body["error"] = task.get("error") or "任务执行失败（无详细输出）"
    else:
        body["message"] = "AI 正在处理中，请稍后重试"

    return JSONResponse(body)


# ---------- 对话历史管理 ----------


@app.get(f"{API_PREFIX}/chat_history", tags=["chat"])
def get_chat_history(
    conversation_id: str = Query(...),
    _: None = Depends(require_auth),
) -> JSONResponse:
    messages = _load_conversation(conversation_id)
    return JSONResponse({
        "status": "success",
        "conversation_id": conversation_id,
        "message_count": len(messages),
        "messages": messages,
    })


@app.delete(f"{API_PREFIX}/chat_conversation", tags=["chat"])
def delete_chat_conversation(
    conversation_id: Optional[str] = Query(default=None),
    payload: Optional[ChatRequest] = Body(default=None),
    _: None = Depends(require_auth),
) -> JSONResponse:
    """删除会话；conversation_id 放 query 或 JSON body 都可以（兼容旧前端）"""
    cid = conversation_id or (payload.conversation_id if payload else None)
    if not cid:
        raise HTTPException(status_code=400, detail="Missing 'conversation_id'")

    deleted = _delete_conversation(cid)
    return JSONResponse(
        status_code=200 if deleted else 404,
        content={
            "status": "deleted" if deleted else "not_found",
            "conversation_id": cid,
        },
    )


@app.get(f"{API_PREFIX}/chat_conversations", tags=["chat"])
def list_chat_conversations(_: None = Depends(require_auth)) -> JSONResponse:
    """会话列表（侧边栏用），按更新时间倒序，不含消息正文"""
    return JSONResponse({"status": "success", "conversations": list_conversations()})


if __name__ == "__main__":
    import uvicorn

    host = os.environ.get("HOST", "0.0.0.0")
    port = int(os.environ.get("PORT", 8000))

    if os.environ.get("RELOAD", "false").lower() == "true":
        # reload 必须用 import 字符串（仅开发时用）
        uvicorn.run("main:app", host=host, port=port, reload=True)
    else:
        # 直接传 app 对象：PyInstaller 打包后模块名不是 "main"，用 import 字符串会失败
        uvicorn.run(
            app,
            host=host,
            port=port,
            log_level=_log_level_name().lower(),
            access_log=ACCESS_LOG,
        )
