"""
Cisco Nexus Dashboard 工具集（只读查询，31 个工具：Infra 17 + Manage 13 + 全景 1）。

覆盖官方两套 API（路径与 OpenAPI 规范均已核对，来源见文末）：
  · Infra API   https://<nd>/api/v1/infra/...     集群/节点/容量/许可/租户/集成…
  · Manage API  https://<nd>/api/v1/manage/...    fabric/交换机/接口/网络/VRF/链路…

认证（Nexus Dashboard 只有这两种官方方式，二选一）：
  · API Key（推荐，适合自动化）：请求头 X-Nd-Username + X-Nd-Apikey
  · 用户名密码登录：POST /api/v1/infra/login -> jwttoken -> Cookie: AuthCookie=<token>
    （token 进程内缓存，默认 10 分钟过期或遇到 401 自动重登）

配置环境变量：
  ND_BASE_URL       必填，形如 https://nd.example.com（不要带路径，代码自己拼 /api/v1/...）
  ND_API_KEY        API Key 模式必填
  ND_USERNAME       API Key 模式必填；密码模式必填
  ND_PASSWORD       密码模式必填
  ND_LOGIN_DOMAIN   密码模式的登录域，默认 local
  ND_VERIFY_TLS     是否校验 TLS 证书，默认 false（Nexus Dashboard 常见自签证书）
  ND_TIMEOUT        单次请求超时秒数，默认 30
  ND_TOKEN_TTL_SECONDS  token 缓存时长，默认 600

只做只读 GET，不提供任何写操作（新建/修改/删除/升级/重启一律不暴露）。

规范出处（2026-09 核对）：
  https://pubhub.devnetcloud.com/media/nexus-dashboard-api-v1/docs/reference/infra.json
  https://pubhub.devnetcloud.com/media/nexus-dashboard-api-v1/docs/reference/manage.json
  https://pubhub.devnetcloud.com/media/nexus-dashboard-api-v1/docs/overview/authentication.html
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from typing import Any, Optional
from urllib.parse import quote

import httpx

logger = logging.getLogger("tools.nexus_dashboard")

PROVIDER = "nexus_dashboard"

BASE_URL_ENV = "ND_BASE_URL"
USERNAME_ENV = "ND_USERNAME"
API_KEY_ENV = "ND_API_KEY"
PASSWORD_ENV = "ND_PASSWORD"
DOMAIN_ENV = "ND_LOGIN_DOMAIN"
VERIFY_TLS_ENV = "ND_VERIFY_TLS"
TIMEOUT_ENV = "ND_TIMEOUT"
TOKEN_TTL_ENV = "ND_TOKEN_TTL_SECONDS"

DEFAULT_LOGIN_DOMAIN = "local"
DEFAULT_TIMEOUT = 30.0
DEFAULT_TOKEN_TTL = 600.0

# 官方基地址：https://<nd>/api/v1/infra 与 https://<nd>/api/v1/manage
API_BASE_PATHS = {"infra": "/api/v1/infra", "manage": "/api/v1/manage"}

# ------------------------------------------------------------------ 工具表
# 一条 SPEC = 一个工具：api 决定基地址，path 支持 {fabricName} 这类路径参数，
# path_params / query_params 的值是「工具参数名 -> API 里的名字」。
SPECS = [
    # ---------------- Infra API：集群与平台 ----------------
    {
        "name": "nexus_infra_about",
        "api": "infra",
        "path": "/about",
        "action": "get_about",
        "description": "获取 Nexus Dashboard 集群基本信息（产品名、版本、构建号、登录横幅）",
    },
    {
        "name": "nexus_infra_cluster_status",
        "api": "infra",
        "path": "/cluster/status",
        "action": "get_cluster_status",
        "description": "获取 Nexus Dashboard 集群状态（集群名、state 与各安装阶段进度）",
    },
    {
        "name": "nexus_infra_cluster_config",
        "api": "infra",
        "path": "/cluster/config",
        "action": "get_cluster_config",
        "description": "获取 Nexus Dashboard 集群配置（节点、管理/数据网络、VIP 等）",
    },
    {
        "name": "nexus_infra_cluster_nodes",
        "api": "infra",
        "path": "/cluster/nodes",
        "action": "get_cluster_nodes",
        "description": "列出 Nexus Dashboard 集群节点（配置与运行状态），可按节点名/角色/状态过滤",
        "query_params": {
            "node_name": "nodeName",
            "node_role": "nodeRole",
            "bootstrap_state": "nodeBootstrapState",
            "operational_state": "nodeOperationalState",
        },
    },
    {
        "name": "nexus_infra_cluster_health",
        "api": "infra",
        "path": "/clusterhealth/status",
        "action": "get_cluster_health",
        "description": "获取集群健康状态（整体 isHealthy、核心组件、k8s、各节点资源占用）",
        "query_params": {"health_category": "healthCategory", "node_name": "nodeName"},
    },
    {
        "name": "nexus_infra_capacities",
        "api": "infra",
        "path": "/capacities",
        "action": "get_capacities",
        "description": "获取集群容量上限与当前用量",
        "query_params": {"pull_usage": "pullUsage"},
    },
    {
        "name": "nexus_infra_system_resources",
        "api": "infra",
        "path": "/systemResources/summary",
        "action": "get_system_resources",
        "description": "获取系统资源概览（CPU/内存/磁盘等汇总）",
    },
    {
        "name": "nexus_infra_hardware_status",
        "api": "infra",
        "path": "/systemResources/nodes/hardware",
        "action": "get_hardware_status",
        "description": "获取 Nexus Dashboard 节点硬件状态",
    },
    {
        "name": "nexus_infra_my_roles",
        "api": "infra",
        "path": "/myRoles",
        "action": "get_my_roles",
        "description": "获取当前账号在各域/fabric 上的角色（排查权限问题用）",
    },
    {
        "name": "nexus_infra_license_status",
        "api": "infra",
        "path": "/license/smartLicenseStatus",
        "action": "get_license_status",
        "description": "获取智能许可（Smart Licensing）状态",
    },
    {
        "name": "nexus_infra_license_usage",
        "api": "infra",
        "path": "/license/usageSummary",
        "action": "get_license_usage",
        "description": "获取许可用量汇总",
    },
    {
        "name": "nexus_infra_remote_clusters",
        "api": "infra",
        "path": "/clusters",
        "action": "list_remote_clusters",
        "description": "列出已接入的远端集群（Multi-Cluster Connectivity）",
    },
    {
        "name": "nexus_infra_tenants",
        "api": "infra",
        "path": "/tenants",
        "action": "list_tenants",
        "description": "列出 Nexus Dashboard 上的全部租户",
    },
    {
        "name": "nexus_infra_remote_storage",
        "api": "infra",
        "path": "/remoteStorage",
        "action": "list_remote_storage",
        "description": "列出已配置的远端备份存储",
    },
    {
        "name": "nexus_infra_audit_records",
        "api": "infra",
        "path": "/auditRecords",
        "action": "list_audit_records",
        "description": "查询集群审计记录（谁在什么时候做了什么）",
    },
    {
        "name": "nexus_infra_general_settings",
        "api": "infra",
        "path": "/settings/general",
        "action": "get_general_settings",
        "description": "获取系统通用设置（DNS/NTP/代理等）",
    },
    {
        "name": "nexus_infra_integrations",
        "api": "infra",
        "path": "/integrations",
        "action": "list_integrations",
        "description": "列出已配置的外部集成（vCenter/DNS/IPAM/K8s/Slack 等）",
    },
    # ---------------- Manage API：fabric 与交换机 ----------------
    {
        "name": "nexus_manage_fabrics",
        "api": "manage",
        "path": "/fabrics",
        "action": "list_fabrics",
        "description": "列出 Nexus Dashboard 管理的全部 fabric（含管理类型、许可级别、安全域）",
    },
    {
        "name": "nexus_manage_fabrics_summary",
        "api": "manage",
        "path": "/fabricsSummary",
        "action": "list_fabrics_summary",
        "description": "列出 fabric 汇总（连通性状态、告警等级、异常等级、feature 状态）",
    },
    {
        "name": "nexus_manage_switches",
        "api": "manage",
        "path": "/inventory/switches",
        "action": "list_switches",
        "description": "列出全部 fabric 的交换机（全局清单）",
    },
    {
        "name": "nexus_manage_switches_summary",
        "api": "manage",
        "path": "/inventory/switches/summary",
        "action": "get_switches_summary",
        "description": "获取交换机全局汇总（按角色/软件版本/同步状态/异常等级计数）",
    },
    {
        "name": "nexus_manage_neighbor_switches",
        "api": "manage",
        "path": "/inventory/neighborSwitches",
        "action": "list_neighbor_switches",
        "description": "列出所有邻居交换机（可发现未被纳管的设备）",
    },
    {
        "name": "nexus_manage_fabric_summary",
        "api": "manage",
        "path": "/fabrics/{fabricName}/summary",
        "action": "get_fabric_summary",
        "path_params": {"fabric_name": "fabricName"},
        "description": "获取指定 fabric 的汇总信息（状态、交换机数量、连通性）",
    },
    {
        "name": "nexus_manage_fabric_status",
        "api": "manage",
        "path": "/fabrics/{fabricName}/status",
        "action": "get_fabric_status",
        "path_params": {"fabric_name": "fabricName"},
        "description": "获取指定 fabric 的状态信息",
    },
    {
        "name": "nexus_manage_fabric_switches",
        "api": "manage",
        "path": "/fabrics/{fabricName}/switches",
        "action": "list_fabric_switches",
        "path_params": {"fabric_name": "fabricName"},
        "description": "列出指定 fabric 下的交换机",
    },
    {
        "name": "nexus_manage_fabric_switches_summary",
        "api": "manage",
        "path": "/fabrics/{fabricName}/switches/summary",
        "action": "get_fabric_switches_summary",
        "path_params": {"fabric_name": "fabricName"},
        "description": "获取指定 fabric 的交换机汇总",
    },
    {
        "name": "nexus_manage_fabric_interfaces",
        "api": "manage",
        "path": "/fabrics/{fabricName}/interfacesSummary",
        "action": "get_fabric_interfaces_summary",
        "path_params": {"fabric_name": "fabricName"},
        "description": "获取指定 fabric 的接口汇总（up/down、错误计数等）",
    },
    {
        "name": "nexus_manage_fabric_tenants",
        "api": "manage",
        "path": "/fabrics/{fabricName}/tenants",
        "action": "list_fabric_tenants",
        "path_params": {"fabric_name": "fabricName"},
        "description": "列出指定 fabric 上的租户",
    },
    {
        "name": "nexus_manage_fabric_networks",
        "api": "manage",
        "path": "/fabrics/{fabricName}/networks",
        "action": "list_fabric_networks",
        "path_params": {"fabric_name": "fabricName"},
        "description": "列出指定 fabric 上的网络（VLAN/VXLAN 映射）",
    },
    {
        "name": "nexus_manage_fabric_vrfs",
        "api": "manage",
        "path": "/fabrics/{fabricName}/vrfs",
        "action": "list_fabric_vrfs",
        "path_params": {"fabric_name": "fabricName"},
        "description": "列出指定 fabric 上的 VRF",
    },
]

SPEC_BY_NAME = {spec["name"]: spec for spec in SPECS}
TOOL_NAMES = [spec["name"] for spec in SPECS] + ["nexus_overview"]

_OVERVIEW_SCHEMA = {
    "type": "function",
    "function": {
        "name": "nexus_overview",
        "description": "Nexus Dashboard 全景查询：集群信息 + 集群健康 + fabric 汇总 + 交换机汇总（一次调用顶四次）",
        "parameters": {"type": "object", "properties": {}, "required": []},
    },
}


def _build_schema(spec):
    """把一条 SPEC 展开成 OpenAI function schema"""
    properties = {}
    required = []

    for arg_name, api_name in (spec.get("path_params") or {}).items():
        properties[arg_name] = {
            "type": "string",
            "description": "fabric 名称（对应 API 参数 " + api_name + "）",
        }
        required.append(arg_name)

    for arg_name, api_name in (spec.get("query_params") or {}).items():
        properties[arg_name] = {
            "type": "string",
            "description": "可选过滤条件（对应 API 参数 " + api_name + "）",
        }

    return {
        "type": "function",
        "function": {
            "name": spec["name"],
            "description": spec["description"],
            "parameters": {"type": "object", "properties": properties, "required": required},
        },
    }


SCHEMAS = [_build_schema(spec) for spec in SPECS] + [_OVERVIEW_SCHEMA]

# ------------------------------------------------------------------ 配置

_TOKEN_LOCK = threading.Lock()
_TOKEN_STATE = {"token": None, "issued_at": 0.0}


def _ok(**payload):
    return json.dumps({"status": "success", "provider": PROVIDER, **payload}, ensure_ascii=False)


def _error(message):
    return json.dumps({"status": "error", "provider": PROVIDER, "message": message}, ensure_ascii=False)


def _env(name):
    return (os.environ.get(name) or "").strip()


def base_url():
    """形如 https://nd.example.com（顺手兼容用户多写的 /api/v1/... 后缀）"""
    raw = _env(BASE_URL_ENV).rstrip("/")
    for suffix in ("/api/v1/infra", "/api/v1/manage", "/api/v1"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)].rstrip("/")
    return raw


def username():
    return _env(USERNAME_ENV)


def api_key():
    return _env(API_KEY_ENV)


def password():
    return _env(PASSWORD_ENV)


def login_domain():
    return _env(DOMAIN_ENV) or DEFAULT_LOGIN_DOMAIN


def auth_mode():
    """apikey / password / none"""
    if api_key():
        return "apikey"
    if password():
        return "password"
    return "none"


def verify_tls():
    # Nexus Dashboard 出厂多为自签证书，官方示例也是 --insecure，所以默认不校验
    return _env(VERIFY_TLS_ENV).lower() in ("1", "true", "yes", "on")


def timeout_seconds():
    try:
        return float(_env(TIMEOUT_ENV) or DEFAULT_TIMEOUT)
    except ValueError:
        return DEFAULT_TIMEOUT


def token_ttl_seconds():
    try:
        return float(_env(TOKEN_TTL_ENV) or DEFAULT_TOKEN_TTL)
    except ValueError:
        return DEFAULT_TOKEN_TTL


def is_configured():
    if not base_url() or not username():
        return False
    return auth_mode() != "none"


def _missing_config_hint():
    if not base_url():
        return "未配置 " + BASE_URL_ENV + "（形如 https://nd.example.com），Nexus Dashboard 工具不可用"
    if not username():
        return "未配置 " + USERNAME_ENV + "，Nexus Dashboard 工具不可用"
    if auth_mode() == "none":
        return "未配置 " + API_KEY_ENV + " 或 " + PASSWORD_ENV + "，Nexus Dashboard 工具不可用"
    return "Nexus Dashboard 工具不可用"


def health():
    configured = is_configured()
    mode = auth_mode()
    return {
        "configured": configured,
        "auth_mode": mode,
        "base_url": base_url() or None,
        "api_key_env": API_KEY_ENV,
        "username_env": USERNAME_ENV,
        "verify_tls": verify_tls(),
        "login_domain": login_domain() if mode == "password" else None,
        "tools": list(TOOL_NAMES),
        "tool_count": len(TOOL_NAMES),
        "hint": None if configured else _missing_config_hint(),
    }


# ------------------------------------------------------------------ 认证


def _headers():
    """按配置返回认证头；密码模式会按需登录拿 token。"""
    mode = auth_mode()
    if mode == "apikey":
        return {"X-Nd-Username": username(), "X-Nd-Apikey": api_key()}
    if mode == "password":
        return {"Cookie": "AuthCookie=" + _token()}
    raise RuntimeError(_missing_config_hint())


def _login():
    """POST /api/v1/infra/login，返回 jwttoken（兼容只返回 token 的版本）"""
    url = base_url() + API_BASE_PATHS["infra"] + "/login"
    payload = {"domain": login_domain(), "userName": username(), "userPasswd": password()}

    with httpx.Client(timeout=timeout_seconds(), verify=verify_tls()) as client:
        response = client.post(url, json=payload)

    if response.status_code >= 400:
        snippet = response.text[:300].replace("\n", " ")
        raise RuntimeError(
            "Nexus Dashboard 登录失败：HTTP "
            + str(response.status_code)
            + "（检查 "
            + USERNAME_ENV
            + " / "
            + PASSWORD_ENV
            + " / "
            + DOMAIN_ENV
            + "）"
            + snippet
        )

    try:
        body = response.json()
    except Exception as exc:
        raise RuntimeError("Nexus Dashboard 登录响应不是 JSON：" + str(exc)) from exc

    token = body.get("jwttoken") or body.get("token") or ""
    if not token:
        raise RuntimeError("Nexus Dashboard 登录成功但响应里没有 jwttoken")
    return str(token)


def _token(force_refresh=False):
    """进程内缓存 token；超过 TTL 或 force_refresh 时重新登录。"""
    with _TOKEN_LOCK:
        fresh = (
            not force_refresh
            and _TOKEN_STATE["token"]
            and (time.monotonic() - float(_TOKEN_STATE["issued_at"])) < token_ttl_seconds()
        )
        if fresh:
            return str(_TOKEN_STATE["token"])

        token = _login()
        _TOKEN_STATE["token"] = token
        _TOKEN_STATE["issued_at"] = time.monotonic()
        return token


def reset_token():
    """测试或排障用：清掉缓存的登录 token"""
    with _TOKEN_LOCK:
        _TOKEN_STATE["token"] = None
        _TOKEN_STATE["issued_at"] = 0.0


def _drop_token_if_current(token):
    """并发下只清掉自己那次用的 token，避免把别人刚拿到的新 token 清掉"""
    with _TOKEN_LOCK:
        if _TOKEN_STATE["token"] == token:
            _TOKEN_STATE["token"] = None
            _TOKEN_STATE["issued_at"] = 0.0


# ------------------------------------------------------------------ HTTP


def _url(api, path):
    base = base_url()
    if not base:
        raise RuntimeError(_missing_config_hint())
    return base + API_BASE_PATHS[api] + path


def _request(api, path, params=None):
    """GET 一次；密码模式下遇到 401 自动重登重试一次。"""
    if not is_configured():
        raise RuntimeError(_missing_config_hint())
    url = _url(api, path)
    headers = _headers()

    def once(headers):
        with httpx.Client(timeout=timeout_seconds(), verify=verify_tls()) as client:
            return client.get(url, headers=headers, params=params or None)

    response = once(headers)

    # 密码模式 token 可能过期/被踢，重登一次再试
    if response.status_code == 401 and auth_mode() == "password":
        _drop_token_if_current(str(_TOKEN_STATE.get("token") or ""))
        response = once(_headers())

    if response.status_code >= 400:
        snippet = response.text[:300].replace("\n", " ")
        hint = "（认证/权限问题：检查 API Key 或账号角色）" if response.status_code in (401, 403) else ""
        raise RuntimeError(
            "Nexus Dashboard API " + path + " 返回 HTTP " + str(response.status_code) + hint + "：" + snippet
        )

    if not response.text:
        return {}
    try:
        return response.json()
    except Exception:
        return {"raw": response.text[:2000]}


# ------------------------------------------------------------------ 工具实现


def _require(arguments, name):
    value = arguments.get(name)
    if value is None or str(value).strip() == "":
        raise ValueError(name + " 是必填参数")
    return str(value)


def _render_path(spec, arguments):
    """把 {fabricName} 这类占位符换成 URL 编码后的真实值，并回显用到的参数"""
    path = spec["path"]
    echo = {}
    for arg_name, api_name in (spec.get("path_params") or {}).items():
        value = _require(arguments, arg_name)
        path = path.replace("{" + api_name + "}", quote(value, safe=""))
        echo[arg_name] = value
    return path, echo


def _render_query(spec, arguments):
    params = {}
    for arg_name, api_name in (spec.get("query_params") or {}).items():
        value = arguments.get(arg_name)
        if value is None or str(value).strip() == "":
            continue
        if isinstance(value, bool):
            params[api_name] = "true" if value else "false"
        else:
            params[api_name] = str(value)
    return params


def _run_tool(spec, arguments):
    path, echo = _render_path(spec, arguments)
    query = _render_query(spec, arguments)
    data = _request(spec["api"], path, query)
    return _ok(action=spec["action"], api=spec["api"], **echo, data=data)


def _overview(_arguments):
    """集群信息 + 健康 + fabric 汇总 + 交换机汇总；任一子项失败只记错误，不影响其它结果"""
    parts = {"about": None, "cluster_health": None, "fabrics_summary": None, "switches_summary": None}
    calls = [
        ("about", "infra", "/about"),
        ("cluster_health", "infra", "/clusterhealth/status"),
        ("fabrics_summary", "manage", "/fabricsSummary"),
        ("switches_summary", "manage", "/inventory/switches/summary"),
    ]
    errors = {}
    for key, api, path in calls:
        try:
            parts[key] = _request(api, path)
        except Exception as exc:  # 单个子项失败不影响整体
            errors[key] = type(exc).__name__ + ": " + str(exc)

    payload = {"action": "overview", "data": parts}
    if errors:
        payload["errors"] = errors
    return _ok(**payload)


def execute(tool_name, arguments=None):
    """执行 Nexus Dashboard 工具；任何异常都收敛成 {"status": "error", "message": ...}"""
    arguments = arguments or {}

    if tool_name == "nexus_overview":
        handler = _overview
    elif tool_name in SPEC_BY_NAME:
        spec = SPEC_BY_NAME[tool_name]
        handler = lambda args, _spec=spec: _run_tool(_spec, args)
    else:
        return _error("未知的 Nexus Dashboard 工具：" + str(tool_name))

    try:
        return handler(arguments)
    except Exception as exc:
        logger.warning("[NexusDashboard] %s 执行失败：%s: %s", tool_name, type(exc).__name__, exc)
        return _error(type(exc).__name__ + ": " + str(exc))


# ---------------------------------------------------------------- 公开入口
# 连接测试（/api/test_connection）这类场景直接调用，不经过 AI 工具循环。


def nexus_infra_about(arguments=None):
    return execute("nexus_infra_about", arguments or {})


def nexus_infra_cluster_status(arguments=None):
    return execute("nexus_infra_cluster_status", arguments or {})


def nexus_manage_fabrics(arguments=None):
    return execute("nexus_manage_fabrics", arguments or {})
