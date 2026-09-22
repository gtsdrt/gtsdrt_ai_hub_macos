"""
工具注册表：把各工具模块的 schema 与 execute 汇总给 main.py 的工具循环。

用法：
    schemas = default_schemas()                 # 传给 OpenAI 的 tools 参数
    result_json = execute_tool(name, arguments) # 返回 JSON 字符串；None 表示未注册
    health = health()                           # 各工具组的凭据/依赖状态

约定：每个 execute_* 都返回 JSON 字符串，失败结构固定为
{"status": "error", "message": "..."}。
"""

from __future__ import annotations

from typing import Optional

from . import ai_tools, azure_tools, meraki_tools, nexus_dashboard_tools

# 分组顺序决定 schema 顺序：Azure 在前，AI Provider 在最后
MODULES = {
    "azure": azure_tools,
    "meraki": meraki_tools,
    "nexus_dashboard": nexus_dashboard_tools,
    "ai": ai_tools,
}


def default_schemas() -> list[dict]:
    """默认挂载的全部工具 schema（Azure 21 + Meraki 45 + Nexus Dashboard 31 + AI 1，共 98 个）"""
    schemas: list[dict] = []
    for module in MODULES.values():
        schemas.extend(module.SCHEMAS)
    return schemas


def tool_names() -> list[str]:
    return [name for module in MODULES.values() for name in module.TOOL_NAMES]


def execute_tool(name: str, arguments: Optional[dict] = None) -> Optional[str]:
    """按名字分发到对应模块；未注册的工具返回 None，由调用方决定怎么报错"""
    for module in MODULES.values():
        if name in module.TOOL_NAMES:
            return module.execute(name, arguments or {})
    return None


def health() -> dict:
    return {
        "tools": tool_names(),
        "groups": {group: module.health() for group, module in MODULES.items()},
    }
