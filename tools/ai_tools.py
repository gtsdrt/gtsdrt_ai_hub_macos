"""
AI Provider 工具集（第一批：DeepSeek 余额查询）

复用 AI Provider 的 Key 与 Base URL：DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL。
"""

from __future__ import annotations

import json
import logging
import os
from typing import Any, Optional

import httpx

logger = logging.getLogger("tools.ai")

API_KEY_ENV = "DEEPSEEK_API_KEY"
DEFAULT_BASE_URL = "https://api.deepseek.com"

SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "deepseek_get_balance",
            "description": "查询当前 DeepSeek API Key 的账户余额，包括是否有可用余额及各币种余额详情",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
]

TOOL_NAMES: tuple[str, ...] = tuple(schema["function"]["name"] for schema in SCHEMAS)


def _ok(**payload: Any) -> str:
    return json.dumps({"status": "success", "provider": "deepseek", **payload}, ensure_ascii=False)


def _error(message: str) -> str:
    return json.dumps({"status": "error", "message": message}, ensure_ascii=False)


def base_url() -> str:
    return (os.environ.get("DEEPSEEK_BASE_URL") or DEFAULT_BASE_URL).rstrip("/")


def api_key() -> str:
    return (os.environ.get(API_KEY_ENV) or "").strip()


def is_configured() -> bool:
    return bool(api_key())


def health() -> dict:
    configured = is_configured()
    return {
        "configured": configured,
        "api_key_env": API_KEY_ENV,
        "base_url": base_url(),
        "tools": list(TOOL_NAMES),
        "hint": None if configured else f"未配置 {API_KEY_ENV}，DeepSeek 余额查询不可用",
    }


def _get_balance(arguments: dict) -> str:
    key = api_key()
    if not key:
        raise RuntimeError(f"未配置 {API_KEY_ENV}")

    with httpx.Client(timeout=30.0) as client:
        response = client.get(
            f"{base_url()}/user/balance",
            headers={"Authorization": f"Bearer {key}", "Accept": "application/json"},
        )

    if response.status_code >= 400:
        snippet = response.text[:300].replace("\n", " ")
        raise RuntimeError(f"DeepSeek 余额查询返回 HTTP {response.status_code}：{snippet}")

    data = response.json() if response.text else {}
    logger.info(
        "[AI] deepseek_get_balance -> is_available=%s",
        data.get("is_available") if isinstance(data, dict) else None,
    )

    return _ok(
        is_available=data.get("is_available") if isinstance(data, dict) else None,
        balance_infos=data.get("balance_infos", []) if isinstance(data, dict) else [],
        raw=data,
    )


_HANDLERS = {
    "deepseek_get_balance": _get_balance,
}


def execute(tool_name: str, arguments: Optional[dict] = None) -> str:
    arguments = arguments or {}
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        return _error(f"未知的 AI 工具：{tool_name}")

    try:
        return handler(arguments)
    except Exception as exc:
        logger.warning("[AI] %s 执行失败：%s: %s", tool_name, type(exc).__name__, exc)
        return _error(f"{type(exc).__name__}: {exc}")


# ---------------------------------------------------------------- 公开入口
# 连接测试（/api/test_connection）这类场景直接调用，不经过 AI 工具循环。


def deepseek_get_balance(arguments: Optional[dict] = None) -> str:
    return execute("deepseek_get_balance", arguments or {})
