"""
Azure 工具集（第一批：订阅 / 资源组 / 虚拟机 / CPU 指标）

认证：统一用 DefaultAzureCredential()，本地会自动读取
AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET（EnvironmentCredential）。
如果本机已经 az login，也可以设 AZURE_USE_DEFAULT_CREDENTIAL=true 走 CLI 凭据。

每个 execute 都返回 JSON 字符串，失败结构固定为 {"status": "error", "message": "..."}。
"""

from __future__ import annotations

import asyncio
import importlib
import importlib.util
import json
import logging
import os
import threading
import time
from typing import Any, Optional

import httpx

logger = logging.getLogger("tools.azure")

SUBSCRIPTION_ENV = "AZURE_SUBSCRIPTION_ID"
ARM_ENDPOINT = "https://management.azure.com"

# 凭据探测：只 get_token（不打任何管理 API）
# DefaultAzureCredential 会依次尝试 EnvironmentCredential / AzureCliCredential 等，
# 正常要 3~6 秒，所以超时给 8 秒；探测在后台线程做，/api/health 永远不阻塞。
PROBE_TIMEOUT_SECONDS = float(os.environ.get("AZURE_PROBE_TIMEOUT_SECONDS", 8.0))
PROBE_CACHE_SECONDS = float(os.environ.get("AZURE_PROBE_CACHE_SECONDS", 30.0))
PROBE_REFRESH_SECONDS = float(os.environ.get("AZURE_PROBE_REFRESH_SECONDS", 30.0))
PROBE_ERROR_CHARS = 200

# 单个工具结果的字节上限（超出就截断并提示）
MAX_RESULT_BYTES = int(os.environ.get("AZURE_TOOL_MAX_RESULT_BYTES", 100 * 1024))

# 首次请求还没拿到探测结果时的占位快照
PROBING_SNAPSHOT: dict = {
    "configured": None,
    "credential_source": "probing",
    "probe_error": None,
}

SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "list_subscriptions",
            "description": "列出当前账号可访问的所有 Azure 订阅",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_all_resource_groups",
            "description": "列出指定订阅中的所有资源组及其位置",
            "parameters": {
                "type": "object",
                "properties": {
                    "subscription_id": {
                        "type": "string",
                        "description": "订阅 ID，不填则用环境变量 AZURE_SUBSCRIPTION_ID",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_virtual_machines",
            "description": "列出指定资源组中的虚拟机及其电源状态（Running/Stopped 等）",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "subscription_id": {
                        "type": "string",
                        "description": "订阅 ID，不填则用环境变量 AZURE_SUBSCRIPTION_ID",
                    },
                },
                "required": ["resource_group_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_vm_cpu_metric",
            "description": "获取指定虚拟机最近 5 分钟的平均 CPU 使用率（百分比）",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "vm_name": {"type": "string", "description": "虚拟机名称"},
                    "subscription_id": {
                        "type": "string",
                        "description": "订阅 ID，不填则用环境变量 AZURE_SUBSCRIPTION_ID",
                    },
                },
                "required": ["resource_group_name", "vm_name"],
            },
        },
    },
    # ---------------------------------------------------------------- 网络类
    {
        "type": "function",
        "function": {
            "name": "list_vnets_in_resource_group",
            "description": "列出指定资源组中的所有虚拟网络（VNet）名称",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "subscription_id": {"type": "string", "description": "订阅 ID，不填则用 AZURE_SUBSCRIPTION_ID"},
                },
                "required": ["resource_group_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_vnet_details",
            "description": "获取指定 VNet 的详细信息（地址空间、子网列表、DNS 等）",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "vnet_name": {"type": "string", "description": "VNet 名称"},
                    "subscription_id": {"type": "string", "description": "订阅 ID，不填则用 AZURE_SUBSCRIPTION_ID"},
                },
                "required": ["resource_group_name", "vnet_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "check_subnets_without_nsg",
            "description": "检查指定资源组内所有 VNet 的子网，列出没有关联网络安全组（NSG）的子网",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "subscription_id": {"type": "string", "description": "订阅 ID，不填则用 AZURE_SUBSCRIPTION_ID"},
                },
                "required": ["resource_group_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_subnet_info",
            "description": "获取指定子网的详细信息（地址前缀、关联 NSG、服务终结点等）",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "vnet_name": {"type": "string", "description": "VNet 名称"},
                    "subnet_name": {"type": "string", "description": "子网名称"},
                    "subscription_id": {"type": "string", "description": "订阅 ID，不填则用 AZURE_SUBSCRIPTION_ID"},
                },
                "required": ["resource_group_name", "vnet_name", "subnet_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_nsgs_in_resource_group",
            "description": "列出指定资源组中的所有网络安全组（NSG）名称",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "subscription_id": {"type": "string", "description": "订阅 ID，不填则用 AZURE_SUBSCRIPTION_ID"},
                },
                "required": ["resource_group_name"],
            },
        },
    },
    # ---------------------------------------------------------------- 资源清单类
    {
        "type": "function",
        "function": {
            "name": "list_storage_accounts",
            "description": "列出指定资源组中的所有存储账户名称",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "subscription_id": {"type": "string", "description": "订阅 ID，不填则用 AZURE_SUBSCRIPTION_ID"},
                },
                "required": ["resource_group_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_web_apps",
            "description": "列出指定资源组中的所有 Web App（App Service）名称",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "subscription_id": {"type": "string", "description": "订阅 ID，不填则用 AZURE_SUBSCRIPTION_ID"},
                },
                "required": ["resource_group_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_sql_servers",
            "description": "列出指定资源组中的所有 Azure SQL Server 名称",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "subscription_id": {"type": "string", "description": "订阅 ID，不填则用 AZURE_SUBSCRIPTION_ID"},
                },
                "required": ["resource_group_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_keyvaults",
            "description": "列出指定资源组中的所有 Key Vault 名称",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "subscription_id": {"type": "string", "description": "订阅 ID，不填则用 AZURE_SUBSCRIPTION_ID"},
                },
                "required": ["resource_group_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_aks_clusters",
            "description": "列出指定资源组中的所有 AKS（Kubernetes）集群名称",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "subscription_id": {"type": "string", "description": "订阅 ID，不填则用 AZURE_SUBSCRIPTION_ID"},
                },
                "required": ["resource_group_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_container_instances",
            "description": "列出指定资源组中的所有容器实例（ACI）名称",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "subscription_id": {"type": "string", "description": "订阅 ID，不填则用 AZURE_SUBSCRIPTION_ID"},
                },
                "required": ["resource_group_name"],
            },
        },
    },
    # ---------------------------------------------------------------- KQL
    {
        "type": "function",
        "function": {
            "name": "query_resources",
            "description": "使用 Kusto 查询语言（KQL）跨资源组、跨区域查询 Azure 资源。示例：resources | where type =~ 'Microsoft.Compute/virtualMachines' | project name, location",
            "parameters": {
                "type": "object",
                "properties": {
                    "kql_query": {"type": "string", "description": "KQL 查询语句"},
                    "subscription_id": {"type": "string", "description": "订阅 ID，不填则用 AZURE_SUBSCRIPTION_ID"},
                },
                "required": ["kql_query"],
            },
        },
    },
    # ---------------------------------------------------------------- 监控类
    {
        "type": "function",
        "function": {
            "name": "get_webapp_metrics",
            "description": "获取 Web App（App Service）的关键监控指标，默认最近 1 小时、5 分钟粒度",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "webapp_name": {"type": "string", "description": "Web App 名称"},
                    "metric_names": {
                        "type": "string",
                        "description": "指标名称，逗号分隔。默认 Requests,AverageResponseTime,Http4xx,Http5xx,BytesReceived,BytesSent,MemoryWorkingSet,CpuTime",
                    },
                    "timespan": {"type": "string", "description": "时间范围，如 PT1H(1小时)、PT24H、P7D，默认 PT1H"},
                    "interval": {"type": "string", "description": "聚合间隔，如 PT5M、PT1H、P1D，默认 PT5M"},
                    "aggregation": {"type": "string", "description": "聚合方式，如 Average,Total,Count，默认 Average,Total,Count"},
                    "subscription_id": {"type": "string", "description": "订阅 ID，不填则用 AZURE_SUBSCRIPTION_ID"},
                },
                "required": ["resource_group_name", "webapp_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_network_metrics",
            "description": "获取网络资源的监控指标，支持 nsg / loadbalancer / applicationgateway / vnetgateway / publicip / firewall / frontdoor / cdn，默认最近 1 小时",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "resource_name": {"type": "string", "description": "网络资源名称"},
                    "resource_type": {
                        "type": "string",
                        "description": "资源类型：nsg / loadbalancer / applicationgateway / vnetgateway / publicip / firewall / frontdoor / cdn",
                    },
                    "metric_names": {"type": "string", "description": "指标名称，逗号分隔；不填则用该类型的默认指标"},
                    "timespan": {"type": "string", "description": "时间范围，如 PT1H、P1D，默认 PT1H"},
                    "interval": {"type": "string", "description": "聚合间隔，如 PT5M、PT1H，默认 PT5M"},
                    "aggregation": {"type": "string", "description": "聚合方式，如 Average,Total,Count，默认 Average,Total,Count"},
                    "subscription_id": {"type": "string", "description": "订阅 ID，不填则用 AZURE_SUBSCRIPTION_ID"},
                },
                "required": ["resource_group_name", "resource_name", "resource_type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_app_insights_metrics",
            "description": "获取 Application Insights 组件的监控指标，如请求数、异常数、依赖耗时、页面浏览",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "app_insights_name": {"type": "string", "description": "Application Insights 名称"},
                    "metric_names": {
                        "type": "string",
                        "description": "指标名称，逗号分隔。默认 requests/count,exceptions/count,dependencies/duration,pageViews/count",
                    },
                    "timespan": {"type": "string", "description": "时间范围，如 PT1H、P1D，默认 PT1H"},
                    "interval": {"type": "string", "description": "聚合间隔，如 PT5M、PT1H，默认 PT5M"},
                    "aggregation": {"type": "string", "description": "聚合方式，默认 Average,Total,Count"},
                    "subscription_id": {"type": "string", "description": "订阅 ID，不填则用 AZURE_SUBSCRIPTION_ID"},
                },
                "required": ["resource_group_name", "app_insights_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_resource_metrics",
            "description": "通用指标查询：可查询任何 Azure 资源的监控指标，需要提供资源提供程序（如 Microsoft.Compute/virtualMachines）",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_provider": {
                        "type": "string",
                        "description": "资源提供程序，如 Microsoft.Web/sites、Microsoft.Compute/virtualMachines、Microsoft.Network/networkSecurityGroups",
                    },
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "resource_name": {"type": "string", "description": "资源名称"},
                    "metric_names": {"type": "string", "description": "指标名称，逗号分隔；为空则返回所有可用指标"},
                    "timespan": {"type": "string", "description": "时间范围，如 PT1H、P1D、P7D，默认 PT1H"},
                    "interval": {"type": "string", "description": "聚合间隔，如 PT5M、PT1H，默认 PT5M"},
                    "aggregation": {"type": "string", "description": "聚合方式，默认 Average,Total,Count"},
                    "subscription_id": {"type": "string", "description": "订阅 ID，不填则用 AZURE_SUBSCRIPTION_ID"},
                },
                "required": ["resource_provider", "resource_group_name", "resource_name"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_resource_metric_definitions",
            "description": "列出指定资源支持的所有监控指标名称、单位和聚合方式，帮助确定可查询的指标",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_provider": {
                        "type": "string",
                        "description": "资源提供程序，如 Microsoft.Web/sites、Microsoft.Compute/virtualMachines",
                    },
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "resource_name": {"type": "string", "description": "资源名称"},
                    "subscription_id": {"type": "string", "description": "订阅 ID，不填则用 AZURE_SUBSCRIPTION_ID"},
                },
                "required": ["resource_provider", "resource_group_name", "resource_name"],
            },
        },
    },
]

TOOL_NAMES: tuple[str, ...] = tuple(schema["function"]["name"] for schema in SCHEMAS)

_credential: Any = None


# ---------------------------------------------------------------- 结果与校验


def _ok(**payload: Any) -> str:
    return json.dumps({"status": "success", **payload}, ensure_ascii=False)


def _error(message: str) -> str:
    return json.dumps({"status": "error", "message": message}, ensure_ascii=False)


MAX_ERROR_CHARS = 600


def _shorten(message: str, limit: int = MAX_ERROR_CHARS) -> str:
    """DefaultAzureCredential 的报错会列出整条凭据链，压成一行并截断，避免灌爆上下文"""
    flattened = " ".join(str(message).split())
    if len(flattened) <= limit:
        return flattened
    return flattened[:limit] + "…（已截断，完整错误见后端日志）"


def _require(arguments: dict, name: str) -> str:
    value = arguments.get(name)
    if value is None or str(value).strip() == "":
        raise ValueError(f"{name} 是必填参数")
    return str(value)


def default_subscription_id() -> Optional[str]:
    value = (os.environ.get(SUBSCRIPTION_ENV) or "").strip()
    return value or None


def _subscription_id(arguments: dict) -> str:
    subscription_id = str(arguments.get("subscription_id") or "").strip() or default_subscription_id()
    if not subscription_id:
        raise ValueError(
            f"未指定订阅：请在参数里传 subscription_id，或设置环境变量 {SUBSCRIPTION_ENV}"
        )
    return subscription_id


# ---------------------------------------------------------------- 凭据


def credential_source() -> str:
    """client_secret（三个 AZURE_* 都齐）/ default（显式允许）/ missing"""
    tenant = (os.environ.get("AZURE_TENANT_ID") or "").strip()
    client_id = (os.environ.get("AZURE_CLIENT_ID") or "").strip()
    client_secret = (os.environ.get("AZURE_CLIENT_SECRET") or "").strip()
    if tenant and client_id and client_secret:
        return "client_secret"
    if (os.environ.get("AZURE_USE_DEFAULT_CREDENTIAL") or "").strip().lower() in ("1", "true", "yes", "on"):
        return "default"
    return "missing"


_sdk_deep_state: Optional[tuple[bool, Optional[str]]] = None
_sdk_lock = threading.Lock()
# 后台深度检查线程与工具调用里的懒导入可能同时 import 同一批 azure 子模块，
# 触发 CPython 的 _ModuleLock 死锁；这里把所有 azure import 串行化。
_IMPORT_LOCK = threading.RLock()


def _deep_sdk_check() -> tuple[bool, Optional[str]]:
    """真正 import 每个客户端类（21 个工具的依赖都要能 import）"""
    global _sdk_deep_state

    try:
        with _IMPORT_LOCK:
            import azure.identity  # noqa: F401

            for name in CLIENT_SOURCES:
                _client_class(name)
        result: tuple[bool, Optional[str]] = (True, None)
    except ImportError as exc:
        result = (False, f"缺少 {exc.name}，请执行 pip install -r requirements-fastapi.txt")
    except RuntimeError as exc:
        result = (False, str(exc))
    except Exception as exc:  # 包结构异常时也算不可用
        result = (False, f"{type(exc).__name__}: {exc}")

    with _sdk_lock:
        _sdk_deep_state = result
    return result


def sdk_available(deep: bool = False) -> tuple[bool, Optional[str]]:
    """
    检查 azure SDK 是否装好。
      默认（deep=False）：只确认模块存在（find_spec，毫秒级），并把深度检查丢到后台线程，
                         这样 /api/health 不会被 import 21 个客户端拖慢。
      deep=True：真正 import 每个客户端类。
    """
    if deep:
        return _deep_sdk_check()

    with _sdk_lock:
        cached = _sdk_deep_state
    if cached is not None:
        return cached

    modules = ["azure.identity", "azure.mgmt.resource"] + [
        module_name for module_name, _ in CLIENT_SOURCES.values()
    ]
    for module_name in modules:
        try:
            if importlib.util.find_spec(module_name) is None:
                return False, f"缺少 {module_name}，请执行 pip install -r requirements-fastapi.txt"
        except ModuleNotFoundError:
            return False, f"缺少 {module_name}，请执行 pip install -r requirements-fastapi.txt"

    # 后台补一次深度检查（结果会缓存，供后续 health 使用）
    threading.Thread(target=_deep_sdk_check, name="azure-sdk-check", daemon=True).start()
    return True, None


def _resource_management_client_class() -> Any:
    """
    azure-mgmt-resource 22 之前是 azure.mgmt.resource.ResourceManagementClient，
    23 之后挪到了 azure.mgmt.resource.resources，这里做兼容。
    """
    try:
        from azure.mgmt.resource import ResourceManagementClient  # type: ignore

        return ResourceManagementClient
    except ImportError:
        from azure.mgmt.resource.resources import ResourceManagementClient  # type: ignore

        return ResourceManagementClient


def _subscription_client_class() -> Optional[Any]:
    """
    列出订阅的客户端在不同版本里位置不一样：
      azure-mgmt-resource < 23: azure.mgmt.resource.SubscriptionClient
      azure-mgmt-resource（拆分后）: azure.mgmt.resource.subscriptions
      独立包 azure-mgmt-subscription: azure.mgmt.subscription
    都没有时返回 None，由 _list_subscriptions_via_rest 走 ARM REST 兜底。
    """
    for module_name, attr in (
        ("azure.mgmt.resource", "SubscriptionClient"),
        ("azure.mgmt.resource.subscriptions", "SubscriptionClient"),
        ("azure.mgmt.subscription", "SubscriptionClient"),
    ):
        try:
            module = __import__(module_name, fromlist=[attr])
            return getattr(module, attr)
        except (ImportError, AttributeError):
            continue
    return None


def get_credential() -> Any:
    """DefaultAzureCredential 会按顺序尝试环境变量 / 托管身份 / az CLI 等来源"""
    global _credential
    if _credential is None:
        try:
            with _IMPORT_LOCK:
                from azure.identity import DefaultAzureCredential
        except ImportError as exc:  # pragma: no cover - 依赖缺失时给出可读错误
            raise RuntimeError(f"缺少 {exc.name}，请执行 pip install -r requirements-fastapi.txt") from exc

        logger.info("[Azure] 初始化 DefaultAzureCredential（来源：%s）", credential_source())
        _credential = DefaultAzureCredential()
    return _credential


# ---------------------------------------------------------------- 客户端集中管理
# 21 个工具都从这里拿客户端：同一个 credential + 同一个 subscription_id，按需惰性创建。

CLIENT_SOURCES: dict[str, tuple[str, str]] = {
    "network": ("azure.mgmt.network", "NetworkManagementClient"),
    "compute": ("azure.mgmt.compute", "ComputeManagementClient"),
    "monitor": ("azure.mgmt.monitor", "MonitorManagementClient"),
    "storage": ("azure.mgmt.storage", "StorageManagementClient"),
    "web": ("azure.mgmt.web", "WebSiteManagementClient"),
    "sql": ("azure.mgmt.sql", "SqlManagementClient"),
    "keyvault": ("azure.mgmt.keyvault", "KeyVaultManagementClient"),
    "aks": ("azure.mgmt.containerservice", "ContainerServiceClient"),
    "aci": ("azure.mgmt.containerinstance", "ContainerInstanceManagementClient"),
    "resourcegraph": ("azure.mgmt.resourcegraph", "ResourceGraphClient"),
}


def _client_class(name: str) -> Any:
    """惰性导入客户端类；缺依赖时给可读提示（resource 有多版本包结构，单独处理）"""
    if name == "resource":
        return _resource_management_client_class()

    module_name, attribute = CLIENT_SOURCES[name]
    try:
        with _IMPORT_LOCK:
            module = importlib.import_module(module_name)
            return getattr(module, attribute)
    except (ImportError, AttributeError) as exc:
        raise RuntimeError(
            f"缺少 Azure 依赖（{module_name}），请执行 pip install -r requirements-fastapi.txt"
        ) from exc


def _resourcegraph_query_request_class() -> Any:
    with _IMPORT_LOCK:
        from azure.mgmt.resourcegraph.models import QueryRequest

        return QueryRequest


class AzureClients:
    """
    一次工具调用里的客户端集合：
      clients = AzureClients(arguments)
      clients.storage.storage_accounts.list_by_resource_group(rg)
    统一使用 get_credential() 和同一个 subscription_id（参数 > AZURE_SUBSCRIPTION_ID）。
    """

    def __init__(self, arguments: Optional[dict] = None):
        arguments = arguments or {}
        self.arguments = arguments
        self.credential = get_credential()
        self.subscription_id = _subscription_id(arguments)
        self._cache: dict[str, Any] = {}

    def _get(self, name: str) -> Any:
        if name not in self._cache:
            factory = _client_class(name)
            self._cache[name] = factory(self.credential, self.subscription_id)
        return self._cache[name]

    # 管理类客户端（需要订阅）
    @property
    def network(self) -> Any:
        return self._get("network")

    @property
    def compute(self) -> Any:
        return self._get("compute")

    @property
    def monitor(self) -> Any:
        return self._get("monitor")

    @property
    def storage(self) -> Any:
        return self._get("storage")

    @property
    def web(self) -> Any:
        return self._get("web")

    @property
    def sql(self) -> Any:
        return self._get("sql")

    @property
    def keyvault(self) -> Any:
        return self._get("keyvault")

    @property
    def aks(self) -> Any:
        return self._get("aks")

    @property
    def aci(self) -> Any:
        return self._get("aci")

    @property
    def resource(self) -> Any:
        return self._get("resource")

    # Resource Graph 只需要凭据（不用订阅 ID 构造）
    @property
    def resourcegraph(self) -> Any:
        if "resourcegraph" not in self._cache:
            self._cache["resourcegraph"] = _client_class("resourcegraph")(self.credential)
        return self._cache["resourcegraph"]

    # 资源 ID 构造（监控类工具都用它）
    def resource_id(self, resource_group_name: str, resource_provider: str, resource_name: str) -> str:
        return (
            f"/subscriptions/{self.subscription_id}/resourceGroups/{resource_group_name}"
            f"/providers/{resource_provider}/{resource_name}"
        )


def health() -> dict:
    """
    Azure 工具状态。

    configured 的判定不再只看环境变量：
      - AZURE_* 齐全            → true，credential_source = "explicit"
      - 否则探测 DefaultAzureCredential（get_token，2 秒上限）
          成功 → true，credential_source = "default_chain"
          失败 → false，credential_source = "missing"
    探测结果缓存 5 秒，避免 /api/health 每次都跑一遍凭据链。
    """
    return health_with_probe(probe=None)


def health_with_probe(probe: Optional[dict] = None) -> dict:
    """probe 传入探测快照（current_probe() 的结果），不传则取当前快照（不阻塞）"""
    available, error = sdk_available()
    source = credential_source()  # 环境变量识别结果（字段保持不变）

    if probe is None:
        probe = current_probe()

    probing = probe.get("credential_source") == "probing"

    if not available:
        configured: Optional[bool] = False
        credential_source_name = "missing"
    elif probing:
        configured = None  # 探测中：前端据此显示「检测中…」
        credential_source_name = "probing"
    else:
        configured = bool(probe.get("configured"))
        credential_source_name = probe.get("credential_source") or "missing"

    # probe_error 只在确实失败时给
    probe_error = probe.get("probe_error") if (configured is False and not probing) else None

    return {
        "configured": configured,
        "credential_source": credential_source_name,
        "probe_error": probe_error,
        "sdk_available": available,
        "sdk_error": error,
        "credential": source,
        "subscription_id": default_subscription_id(),
        "subscription_id_env": SUBSCRIPTION_ENV,
        "tools": list(TOOL_NAMES),
        "hint": _status_hint(
            available,
            configured,
            {"credential_source": credential_source_name, "probe_error": probe_error},
        ),
    }


def _status_hint(available: bool, configured: Optional[bool], probe: dict) -> Optional[str]:
    if configured is True:
        return None
    if probe.get("credential_source") == "probing":
        return (
            f"正在后台检测 Azure 凭据（最长 {int(PROBE_TIMEOUT_SECONDS)} 秒，完成后自动更新，"
            f"每 {int(PROBE_REFRESH_SECONDS)} 秒刷新一次）"
        )
    return _missing_hint(available, probe)


def _missing_hint(available: bool, probe: dict) -> str:
    if not available:
        return "未安装 Azure 依赖：pip install -r requirements-fastapi.txt"
    if probe.get("credential_source") == "missing":
        reason = probe.get("probe_error")
        base = (
            "未检测到可用 Azure 凭据：请设置 AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET，"
            "或先执行 az login（DefaultAzureCredential 会复用本地凭据）"
        )
        return f"{base}｜探测失败：{reason}" if reason else base
    return "未知原因"


# ---------------------------------------------------------------- 凭据探测（带缓存）

_probe_cache: Optional[dict] = None
_probe_cache_at: float = 0.0
_probe_lock = threading.Lock()
_refresh_thread: Optional[threading.Thread] = None


def _shorten_probe_error(message: Any, limit: int = PROBE_ERROR_CHARS) -> str:
    flattened = " ".join(str(message or "").split())
    if len(flattened) <= limit:
        return flattened
    return flattened[:limit]


def _probe_outcome(configured: bool, credential_source_name: str, error: Optional[str]) -> dict:
    return {
        "configured": bool(configured),
        "credential_source": credential_source_name,
        "probe_error": _shorten_probe_error(error) if error else None,
    }


def _fetch_token():
    """只做 get_token，不调用任何 Azure 管理 API（避免浪费配额）"""
    credential = get_credential()
    return credential.get_token(f"{ARM_ENDPOINT}/.default").token


def _cached_probe() -> Optional[dict]:
    with _probe_lock:
        cached = _probe_cache
        fresh = cached is not None and (time.time() - _probe_cache_at) < PROBE_CACHE_SECONDS
    return dict(cached) if (cached is not None and fresh) else None


def _store_probe(outcome: dict) -> dict:
    global _probe_cache, _probe_cache_at
    with _probe_lock:
        _probe_cache = dict(outcome)
        _probe_cache_at = time.time()
    return dict(outcome)


def reset_probe_cache() -> None:
    """测试或需要强制重新探测时使用"""
    global _probe_cache, _probe_cache_at
    with _probe_lock:
        _probe_cache = None
        _probe_cache_at = 0.0


def current_probe() -> dict:
    """
    不阻塞的探测快照：
      - 有 30 秒内的缓存 → 直接返回
      - 否则返回 {"configured": null, "credential_source": "probing"}，
        同时确保后台刷新线程已经在跑（它做完会更新缓存）
    这样 /api/health 永远不会等凭据链。
    """
    with _probe_lock:
        cached = dict(_probe_cache) if _probe_cache is not None else None
        fresh = cached is not None and (time.time() - _probe_cache_at) < PROBE_CACHE_SECONDS

    if cached is not None and fresh:
        return cached

    # 环境变量齐全 / SDK 缺失这两个分支不需要网络，直接给结论，不必等后台线程
    precheck = _probe_precheck()
    if precheck is not None:
        return _store_probe(precheck)

    start_background_refresh()
    return dict(PROBING_SNAPSHOT)


def start_background_refresh() -> None:
    """启动后台刷新线程（只在第一次调用时真正创建，之后每 PROBE_REFRESH_SECONDS 秒刷一次）"""
    global _refresh_thread

    if PROBE_REFRESH_SECONDS <= 0:
        return

    with _probe_lock:
        if _refresh_thread is not None and _refresh_thread.is_alive():
            return
        thread = threading.Thread(
            target=_refresh_loop, name="azure-probe-refresh", daemon=True
        )
        _refresh_thread = thread

    logger.info(
        "[Azure] 启动后台凭据探测线程（超时 %ss，缓存 %ss，刷新 %ss）",
        PROBE_TIMEOUT_SECONDS, PROBE_CACHE_SECONDS, PROBE_REFRESH_SECONDS,
    )
    thread.start()


def _refresh_loop() -> None:
    while True:
        try:
            # 环境变量齐全 / SDK 缺失时 probe_credential_sync 会立即返回，不会联网络
            probe_credential_sync(force=True)
        except Exception:  # 后台线程绝不能因此退出
            logger.exception("[Azure] 后台凭据探测异常")

        if PROBE_REFRESH_SECONDS <= 0:
            return
        time.sleep(PROBE_REFRESH_SECONDS)


def _probe_sync(timeout_seconds: float) -> tuple[bool, Optional[str]]:
    """同步探测：worker 线程是 daemon，超时后不会拖住进程退出"""
    box: dict = {}
    finished = threading.Event()

    def worker() -> None:
        try:
            _fetch_token()
            box["ok"] = True
        except Exception as exc:
            box["ok"] = False
            box["error"] = f"{type(exc).__name__}: {exc}"
        finally:
            finished.set()

    threading.Thread(target=worker, name="azure-credential-probe", daemon=True).start()

    if not finished.wait(timeout_seconds):
        return False, f"DefaultAzureCredential 探测超时（{timeout_seconds:g}s）"
    return bool(box.get("ok")), box.get("error")


async def _probe_async(timeout_seconds: float) -> tuple[bool, Optional[str]]:
    try:
        await asyncio.wait_for(asyncio.to_thread(_fetch_token), timeout=timeout_seconds)
        return True, None
    except (asyncio.TimeoutError, TimeoutError):
        return False, f"DefaultAzureCredential 探测超时（{timeout_seconds:g}s）"
    except Exception as exc:
        return False, f"{type(exc).__name__}: {exc}"


def _probe_precheck() -> Optional[dict]:
    """环境变量齐全 / SDK 缺失时不必探测，直接给结论"""
    if credential_source() == "client_secret":
        return _probe_outcome(True, "explicit", None)
    available, _ = sdk_available()
    if not available:
        return _probe_outcome(False, "missing", "未安装 Azure 依赖")
    return None


def probe_credential_sync(
    timeout_seconds: Optional[float] = None, force: bool = False
) -> dict:
    if not force:
        cached = _cached_probe()
        if cached is not None:
            return cached

    precheck = _probe_precheck()
    if precheck is not None:
        return _store_probe(precheck)

    ok, error = _probe_sync(timeout_seconds or PROBE_TIMEOUT_SECONDS)
    return _store_probe(_probe_outcome(ok, "default_chain" if ok else "missing", error))


async def probe_credential_async(
    timeout_seconds: Optional[float] = None, force: bool = False
) -> dict:
    """/api/health 用这个：asyncio.wait_for 卡超时，不阻塞事件循环"""
    if not force:
        cached = _cached_probe()
        if cached is not None:
            return cached

    precheck = _probe_precheck()
    if precheck is not None:
        return _store_probe(precheck)

    ok, error = await _probe_async(timeout_seconds or PROBE_TIMEOUT_SECONDS)
    return _store_probe(_probe_outcome(ok, "default_chain" if ok else "missing", error))


# ---------------------------------------------------------------- 各工具实现


def _list_subscriptions(arguments: dict) -> str:
    credential = get_credential()
    source = credential_source()
    client_class = _subscription_client_class()

    if client_class is not None:
        subscriptions = [
            {
                "id": subscription.subscription_id,
                "name": subscription.display_name,
                "state": str(getattr(subscription, "state", "") or ""),
                "source": source,
            }
            for subscription in client_class(credential).subscriptions.list()
        ]
    else:
        # azure-mgmt-resource >= 23 不再自带 SubscriptionClient，直接查 ARM REST
        subscriptions = _list_subscriptions_via_rest(credential, source)

    logger.info("[Azure] list_subscriptions -> %s 个订阅", len(subscriptions))
    return _ok(subscriptions=subscriptions, count=len(subscriptions))


def _list_subscriptions_via_rest(credential: Any, source: str) -> list[dict]:
    token = credential.get_token(f"{ARM_ENDPOINT}/.default").token
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}

    subscriptions: list[dict] = []
    url: Optional[str] = f"{ARM_ENDPOINT}/subscriptions"
    params: Optional[dict] = {"api-version": "2022-12-01"}

    with httpx.Client(timeout=30.0) as client:
        while url:
            response = client.get(url, headers=headers, params=params)
            if response.status_code >= 400:
                snippet = response.text[:300].replace("\n", " ")
                raise RuntimeError(f"ARM REST 返回 HTTP {response.status_code}：{snippet}")

            payload = response.json() if response.text else {}
            for item in payload.get("value", []):
                subscriptions.append({
                    "id": item.get("subscriptionId"),
                    "name": item.get("displayName"),
                    "state": item.get("state"),
                    "source": f"{source}+arm_rest",
                })

            url = payload.get("nextLink")
            params = None

    return subscriptions


def _list_all_resource_groups(arguments: dict) -> str:
    subscription_id = _subscription_id(arguments)
    client = _resource_management_client_class()(get_credential(), subscription_id)

    resource_groups = [
        {"name": group.name, "location": group.location}
        for group in client.resource_groups.list()
    ]
    logger.info("[Azure] list_all_resource_groups(%s) -> %s 个", subscription_id, len(resource_groups))
    return _ok(
        subscription_id=subscription_id,
        resource_groups=resource_groups,
        count=len(resource_groups),
    )


def _list_virtual_machines(arguments: dict) -> str:
    from azure.mgmt.compute import ComputeManagementClient

    resource_group_name = _require(arguments, "resource_group_name")
    subscription_id = _subscription_id(arguments)
    client = ComputeManagementClient(get_credential(), subscription_id)

    vms = []
    for vm in client.virtual_machines.list(resource_group_name):
        instance_view = client.virtual_machines.instance_view(resource_group_name, vm.name)

        power_state = "Unknown"
        for status in instance_view.statuses or []:
            if status.code and status.code.startswith("PowerState/"):
                power_state = status.code.split("/", 1)[1]
                break

        vms.append({
            "name": vm.name,
            "location": vm.location,
            "vm_size": getattr(vm.hardware_profile, "vm_size", None) if vm.hardware_profile else None,
            "status": power_state,
        })

    logger.info("[Azure] list_virtual_machines(%s/%s) -> %s 台", subscription_id, resource_group_name, len(vms))
    return _ok(
        subscription_id=subscription_id,
        resource_group_name=resource_group_name,
        vms=vms,
        count=len(vms),
    )


def _get_vm_cpu_metric(arguments: dict) -> str:
    from azure.mgmt.monitor import MonitorManagementClient

    resource_group_name = _require(arguments, "resource_group_name")
    vm_name = _require(arguments, "vm_name")
    subscription_id = _subscription_id(arguments)

    resource_id = (
        f"/subscriptions/{subscription_id}/resourceGroups/{resource_group_name}"
        f"/providers/Microsoft.Compute/virtualMachines/{vm_name}"
    )

    client = MonitorManagementClient(get_credential(), subscription_id)
    metrics = client.metrics.list(
        resource_uri=resource_id,
        metricnames="Percentage CPU",
        timespan="PT5M",
        interval="PT1M",
        aggregation="Average",
    )

    average_cpu = None
    if metrics.value:
        timeseries = metrics.value[0].timeseries
        if timeseries and timeseries[0].data:
            average_cpu = timeseries[0].data[-1].average

    logger.info("[Azure] get_vm_cpu_metric(%s) -> %s", vm_name, average_cpu)
    return _ok(
        subscription_id=subscription_id,
        resource_group_name=resource_group_name,
        vm_name=vm_name,
        metric="Percentage CPU",
        timespan="PT5M",
        interval="PT1M",
        cpu_percentage=average_cpu,
    )


# ---------------------------------------------------------------- 网络类


def _list_vnets_in_resource_group(arguments: dict) -> str:
    resource_group_name = _require(arguments, "resource_group_name")
    clients = AzureClients(arguments)

    vnets = [vnet.name for vnet in clients.network.virtual_networks.list(resource_group_name)]
    return _ok(
        subscription_id=clients.subscription_id,
        resource_group_name=resource_group_name,
        vnets=vnets,
        count=len(vnets),
    )


def _get_vnet_details(arguments: dict) -> str:
    resource_group_name = _require(arguments, "resource_group_name")
    vnet_name = _require(arguments, "vnet_name")
    clients = AzureClients(arguments)

    vnet = clients.network.virtual_networks.get(resource_group_name, vnet_name)
    return _ok(
        subscription_id=clients.subscription_id,
        resource_group_name=resource_group_name,
        vnet={
            "name": vnet.name,
            "location": vnet.location,
            "address_space": list(getattr(vnet.address_space, "address_prefixes", None) or []),
            "dns_servers": list(vnet.dhcp_options.dns_servers or []) if vnet.dhcp_options else [],
            "subnets": [
                {
                    "name": subnet.name,
                    "address_prefix": subnet.address_prefix,
                    "nsg": _nsg_name(subnet),
                }
                for subnet in (vnet.subnets or [])
            ],
        },
    )


def _check_subnets_without_nsg(arguments: dict) -> str:
    resource_group_name = _require(arguments, "resource_group_name")
    clients = AzureClients(arguments)

    unprotected = []
    for vnet in clients.network.virtual_networks.list(resource_group_name):
        for subnet in (vnet.subnets or []):
            if not _nsg_name(subnet):
                unprotected.append({
                    "vnet": vnet.name,
                    "subnet": subnet.name,
                    "address_prefix": subnet.address_prefix,
                })

    return _ok(
        subscription_id=clients.subscription_id,
        resource_group_name=resource_group_name,
        unprotected=unprotected,
        count=len(unprotected),
    )


def _get_subnet_info(arguments: dict) -> str:
    resource_group_name = _require(arguments, "resource_group_name")
    vnet_name = _require(arguments, "vnet_name")
    subnet_name = _require(arguments, "subnet_name")
    clients = AzureClients(arguments)

    subnet = clients.network.subnets.get(resource_group_name, vnet_name, subnet_name)
    return _ok(
        subscription_id=clients.subscription_id,
        resource_group_name=resource_group_name,
        vnet_name=vnet_name,
        subnet={
            "name": subnet.name,
            "address_prefix": subnet.address_prefix,
            "nsg": _nsg_name(subnet),
            "route_table": getattr(subnet.route_table, "id", None) if subnet.route_table else None,
            "service_endpoints": [
                getattr(endpoint, "service", None) for endpoint in (subnet.service_endpoints or [])
            ],
            "delegations": [
                getattr(delegation, "service_name", None) for delegation in (subnet.delegations or [])
            ],
        },
    )


def _list_nsgs_in_resource_group(arguments: dict) -> str:
    resource_group_name = _require(arguments, "resource_group_name")
    clients = AzureClients(arguments)

    nsgs = [
        nsg.name
        for nsg in clients.network.network_security_groups.list(resource_group_name)
    ]
    return _ok(
        subscription_id=clients.subscription_id,
        resource_group_name=resource_group_name,
        nsgs=nsgs,
        count=len(nsgs),
    )


def _nsg_name(subnet: Any) -> Optional[str]:
    """子网关联的 NSG 名字（没关联返回 None）"""
    nsg = getattr(subnet, "network_security_group", None)
    if not nsg:
        return None
    if getattr(nsg, "name", None):
        return nsg.name
    # 有些返回只带 id
    resource_id = getattr(nsg, "id", "") or ""
    return resource_id.rsplit("/", 1)[-1] if resource_id else None


# ---------------------------------------------------------------- 资源清单类


def _list_resource_names(arguments: dict, client_name: str, lister: Any, field: str) -> str:
    """资源清单类工具的共同实现：列出某个资源组里的资源名"""
    resource_group_name = _require(arguments, "resource_group_name")
    clients = AzureClients(arguments)

    items = list(lister(getattr(clients, client_name), resource_group_name))
    names = [getattr(item, "name", None) for item in items]
    names = [name for name in names if name]

    return _ok(
        subscription_id=clients.subscription_id,
        resource_group_name=resource_group_name,
        **{field: names, "count": len(names)},
    )


def _list_storage_accounts(arguments: dict) -> str:
    return _list_resource_names(
        arguments, "storage",
        lambda client, rg: client.storage_accounts.list_by_resource_group(rg),
        "storage_accounts",
    )


def _list_web_apps(arguments: dict) -> str:
    return _list_resource_names(
        arguments, "web",
        lambda client, rg: client.web_apps.list_by_resource_group(rg),
        "web_apps",
    )


def _list_sql_servers(arguments: dict) -> str:
    return _list_resource_names(
        arguments, "sql",
        lambda client, rg: client.servers.list_by_resource_group(rg),
        "sql_servers",
    )


def _list_keyvaults(arguments: dict) -> str:
    return _list_resource_names(
        arguments, "keyvault",
        lambda client, rg: client.vaults.list_by_resource_group(rg),
        "keyvaults",
    )


def _list_aks_clusters(arguments: dict) -> str:
    return _list_resource_names(
        arguments, "aks",
        lambda client, rg: client.managed_clusters.list_by_resource_group(rg),
        "aks_clusters",
    )


def _list_container_instances(arguments: dict) -> str:
    return _list_resource_names(
        arguments, "aci",
        lambda client, rg: client.container_groups.list_by_resource_group(rg),
        "container_instances",
    )


# ---------------------------------------------------------------- KQL（Resource Graph）

KQL_RESULT_LIMIT = 100


def _query_resources(arguments: dict) -> str:
    kql_query = _require(arguments, "kql_query")
    clients = AzureClients(arguments)

    QueryRequest = _resourcegraph_query_request_class()
    request = QueryRequest(query=kql_query, subscriptions=[clients.subscription_id])
    response = clients.resourcegraph.resources(request)
    rows = list(getattr(response, "data", None) or [])

    results = [
        {
            "name": row.get("name"),
            "type": row.get("type"),
            "location": row.get("location"),
            "resourceGroup": row.get("resourceGroup"),
            "id": row.get("id"),
        }
        for row in rows[:KQL_RESULT_LIMIT]
        if isinstance(row, dict)
    ]

    payload: dict = {
        "total": len(rows),
        "count": len(results),
        "results": results,
    }
    if len(rows) > KQL_RESULT_LIMIT:
        tail = f"结果共 {len(rows)} 条，仅返回前 {KQL_RESULT_LIMIT} 条；请收窄 KQL 查询条件"
        payload["note"] = tail
        payload["truncated"] = True
        payload["message"] = tail

    return _ok(subscription_id=clients.subscription_id, **payload)


# ---------------------------------------------------------------- 监控类

DEFAULT_METRIC_TIMESPAN = "PT1H"
DEFAULT_METRIC_INTERVAL = "PT5M"
DEFAULT_METRIC_AGGREGATION = "Average,Total,Count"

DEFAULT_WEBAPP_METRICS = (
    "Requests,AverageResponseTime,Http4xx,Http5xx,BytesReceived,BytesSent,MemoryWorkingSet,CpuTime"
)
DEFAULT_APP_INSIGHTS_METRICS = "requests/count,exceptions/count,dependencies/duration,pageViews/count"

# 网络资源类型 → (资源提供程序前缀, 默认指标)
NETWORK_RESOURCE_TYPES: dict[str, tuple[str, str]] = {
    "nsg": ("Microsoft.Network/networkSecurityGroups", "PacketCount,ByteCount"),
    "loadbalancer": (
        "Microsoft.Network/loadBalancers",
        "VIPAvailability,DataPathAvailability,ByteCount,PacketCount,SNATConnectionCount",
    ),
    "applicationgateway": (
        "Microsoft.Network/applicationGateways",
        "Throughput,UnhealthyHostCount,ResponseStatus,CurrentConnections",
    ),
    "vnetgateway": (
        "Microsoft.Network/virtualNetworkGateways",
        "AverageBandwidth,P2SBandwidth,TunnelAverageBandwidth",
    ),
    "publicip": (
        "Microsoft.Network/publicIPAddresses",
        "BytesInDDoS,BytesOutDDoS,DDoSTriggerTCPPackets,DDoSTriggerUDPPackets",
    ),
    "firewall": ("Microsoft.Network/azureFirewalls", "DataProcessed,Throughput"),
    "frontdoor": (
        "Microsoft.Network/frontDoors",
        "RequestCount,Latency,BackendHealthPercentage",
    ),
    "cdn": (
        "Microsoft.Cdn/profiles",
        "RequestCount,ResponseSize,OriginHealthPercentage",
    ),
}


def _metric_data_point(dp: Any) -> dict:
    return {
        "time": dp.time_stamp.isoformat() if dp.time_stamp else None,
        "average": dp.average,
        "total": dp.total,
        "count": dp.count,
        "minimum": dp.minimum,
        "maximum": dp.maximum,
    }


def _format_metrics(metrics_result: Any) -> dict:
    """把 SDK 的 metrics.list() 结果转成统一结构：
    {metric_name: {"unit": ..., "data": [{time, average, total, ...}]}}"""
    formatted: dict[str, dict] = {}
    for metric in (getattr(metrics_result, "value", None) or []):
        metric_name = getattr(getattr(metric, "name", None), "value", None) or str(metric.name)
        data_points: list[dict] = []
        for timeseries in (metric.timeseries or []):
            for point in (timeseries.data or []):
                data_points.append(_metric_data_point(point))
        formatted[metric_name] = {
            "unit": str(metric.unit) if metric.unit else None,
            "data": data_points,
        }
    return formatted


def _list_metrics(resource_id: str, arguments: dict, default_metrics: str) -> str:
    clients = AzureClients(arguments)
    metric_names = arguments.get("metric_names") or default_metrics

    metrics = clients.monitor.metrics.list(
        resource_uri=resource_id,
        metricnames=metric_names,
        timespan=arguments.get("timespan") or DEFAULT_METRIC_TIMESPAN,
        interval=arguments.get("interval") or DEFAULT_METRIC_INTERVAL,
        aggregation=arguments.get("aggregation") or DEFAULT_METRIC_AGGREGATION,
    )

    return _ok(
        subscription_id=clients.subscription_id,
        resource_id=resource_id,
        metric_names=metric_names,
        metrics=_format_metrics(metrics),
    )


def _get_webapp_metrics(arguments: dict) -> str:
    resource_group_name = _require(arguments, "resource_group_name")
    webapp_name = _require(arguments, "webapp_name")
    clients = AzureClients(arguments)

    resource_id = clients.resource_id(resource_group_name, "Microsoft.Web/sites", webapp_name)
    return _list_metrics(resource_id, arguments, DEFAULT_WEBAPP_METRICS)


def _get_network_metrics(arguments: dict) -> str:
    resource_group_name = _require(arguments, "resource_group_name")
    resource_name = _require(arguments, "resource_name")
    resource_type = _require(arguments, "resource_type").strip().lower()

    if resource_type in NETWORK_RESOURCE_TYPES:
        provider, default_metrics = NETWORK_RESOURCE_TYPES[resource_type]
    else:
        # 未知类型：按 Microsoft.Network/<type>s 拼（保持与原实现一致）
        provider = f"Microsoft.Network/{resource_type}s"
        default_metrics = arguments.get("metric_names") or ""

    clients = AzureClients(arguments)
    resource_id = clients.resource_id(resource_group_name, provider, resource_name)
    return _list_metrics(resource_id, arguments, default_metrics)


def _get_app_insights_metrics(arguments: dict) -> str:
    resource_group_name = _require(arguments, "resource_group_name")
    app_insights_name = _require(arguments, "app_insights_name")
    clients = AzureClients(arguments)

    resource_id = clients.resource_id(
        resource_group_name, "Microsoft.Insights/components", app_insights_name
    )
    return _list_metrics(resource_id, arguments, DEFAULT_APP_INSIGHTS_METRICS)


def _get_resource_metrics(arguments: dict) -> str:
    resource_provider = _require(arguments, "resource_provider")
    resource_group_name = _require(arguments, "resource_group_name")
    resource_name = _require(arguments, "resource_name")
    clients = AzureClients(arguments)

    resource_id = clients.resource_id(resource_group_name, resource_provider, resource_name)
    return _list_metrics(resource_id, arguments, arguments.get("metric_names") or "")


def _get_resource_metric_definitions(arguments: dict) -> str:
    resource_provider = _require(arguments, "resource_provider")
    resource_group_name = _require(arguments, "resource_group_name")
    resource_name = _require(arguments, "resource_name")
    clients = AzureClients(arguments)

    resource_id = clients.resource_id(resource_group_name, resource_provider, resource_name)
    definitions = clients.monitor.metric_definitions.list(resource_uri=resource_id)

    items = [
        {
            "name": getattr(getattr(d, "name", None), "value", None) or str(d.name),
            "unit": str(d.unit) if getattr(d, "unit", None) else None,
            "primary_aggregation_type": str(d.primary_aggregation_type)
            if getattr(d, "primary_aggregation_type", None) else None,
            "supported_aggregation_types": [
                str(item) for item in (getattr(d, "supported_aggregation_types", None) or [])
            ],
        }
        for d in (definitions or [])
    ]

    return _ok(
        subscription_id=clients.subscription_id,
        resource_id=resource_id,
        definitions=items,
        count=len(items),
    )


_HANDLERS = {
    # 第一批（4）
    "list_subscriptions": _list_subscriptions,
    "list_all_resource_groups": _list_all_resource_groups,
    "list_virtual_machines": _list_virtual_machines,
    "get_vm_cpu_metric": _get_vm_cpu_metric,
    # 网络类（5）
    "list_vnets_in_resource_group": _list_vnets_in_resource_group,
    "get_vnet_details": _get_vnet_details,
    "check_subnets_without_nsg": _check_subnets_without_nsg,
    "get_subnet_info": _get_subnet_info,
    "list_nsgs_in_resource_group": _list_nsgs_in_resource_group,
    # 资源清单类（6）
    "list_storage_accounts": _list_storage_accounts,
    "list_web_apps": _list_web_apps,
    "list_sql_servers": _list_sql_servers,
    "list_keyvaults": _list_keyvaults,
    "list_aks_clusters": _list_aks_clusters,
    "list_container_instances": _list_container_instances,
    # KQL（1）
    "query_resources": _query_resources,
    # 监控类（5）
    "get_webapp_metrics": _get_webapp_metrics,
    "get_network_metrics": _get_network_metrics,
    "get_app_insights_metrics": _get_app_insights_metrics,
    "get_resource_metrics": _get_resource_metrics,
    "get_resource_metric_definitions": _get_resource_metric_definitions,
}


def execute(tool_name: str, arguments: Optional[dict] = None) -> str:
    """执行 Azure 工具；任何异常都收敛成 {"status": "error", "message": ...}"""
    arguments = arguments or {}
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        return _error(f"未知的 Azure 工具：{tool_name}")

    try:
        payload = handler(arguments)
    except ImportError as exc:
        logger.warning("[Azure] %s 缺少依赖：%s", tool_name, exc)
        return _error(
            f"缺少 Azure 依赖（{getattr(exc, 'name', exc)}），"
            "请执行 pip install -r requirements-fastapi.txt"
        )
    except Exception as exc:
        logger.warning("[Azure] %s 执行失败：%s: %s", tool_name, type(exc).__name__, exc)

        text = _shorten(f"{type(exc).__name__}: {exc}")
        if "authentication" in text.lower() or "credential" in text.lower():
            text += (
                "｜提示：确认 AZURE_TENANT_ID / AZURE_CLIENT_ID / AZURE_CLIENT_SECRET 是否正确，"
                "或设置 AZURE_USE_DEFAULT_CREDENTIAL=true 后先执行 az login"
            )
        return _error(text)

    return _truncate_payload(tool_name, payload)


def _truncate_payload(tool_name: str, payload: str) -> str:
    """单个工具结果超过 MAX_RESULT_BYTES 时按 UTF-8 边界截断并附加提示"""
    raw = payload.encode("utf-8")
    if len(raw) <= MAX_RESULT_BYTES:
        return payload

    logger.warning("[Azure] %s 结果过大（%s bytes），已截断", tool_name, len(raw))
    clipped = raw[:MAX_RESULT_BYTES].decode("utf-8", errors="ignore")
    return (
        clipped
        + f"\n…（结果超过 {MAX_RESULT_BYTES // 1024}KB 已截断，"
        f"原大小约 {len(raw) // 1024}KB；请收窄查询条件或指定更小的 metric_names）"
    )


# ---------------------------------------------------------------- 公开入口
# 连接测试（/api/test_connection）这类场景直接调用，不经过 AI 工具循环。


def list_subscriptions(arguments: Optional[dict] = None) -> str:
    return execute("list_subscriptions", arguments or {})


def list_all_resource_groups(arguments: Optional[dict] = None) -> str:
    return execute("list_all_resource_groups", arguments or {})


def list_virtual_machines(arguments: Optional[dict] = None) -> str:
    return execute("list_virtual_machines", arguments or {})


def get_vm_cpu_metric(arguments: Optional[dict] = None) -> str:
    return execute("get_vm_cpu_metric", arguments or {})
