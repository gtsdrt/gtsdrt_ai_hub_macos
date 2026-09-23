"""
多容器注册表工具集（Serverless 容器调用；注册表驱动 + 热加载）

背景：这类容器（例如 gtsdrt/Azure_ACR）代码在 GitHub，由 GitHub Actions 构建镜像推到 ACR，
以 Azure Container Apps（Serverless）运行。本模块**不绑定某一个容器**：
注册表里加一行就接入一个新容器，既不改代码，也不用重启后端。

工具（3 个，名字固定，模型只需要学一次）：
  container_list_endpoints()                          列出注册表里所有容器
  container_list_tasks(container?)                    列出某容器内置任务 + 哪些对 AI 开放
  container_run_task(container?, task, params?)       执行某容器里的白名单任务

容器侧 API 约定（protocol = "azure-acr"，即 Azure_ACR 那套）：
  GET  /health    健康检查（无需鉴权）
  GET  /tasks     列出任务：{"tasks": {"<name>": {"required_params": [...], "read_only": true|false}}}
  POST /run-task  {"task": "...", "params": {...}}   执行容器内置 playbook
  POST /run-ansible 能执行任意 playbook，**故意不暴露给 AI**。

配置来源（优先级从高到低，详见 registry_entries()）：
  1) 环境变量 CONTAINER_ENDPOINTS_JSON  —— 内联 JSON，便于 CI / 测试
  2) 注册表文件 AICHAT_CONTAINERS_FILE 或 ~/Library/Application Support/AIChatApp/containers.json
     （**每次调用都重新读**：改文件立即生效，不用重启后端）
  3) 旧配置兼容：ANSIBLE_EXECUTOR_URL + ANSIBLE_EXECUTOR_API_KEY -> 自动生成一个条目

注册表文件格式（顶层可以是 {"containers": [...]} 或直接是 [...]）：
  {
    "containers": [
      {
        "name": "ansible",                                  // 必填，唯一，模型用它指定容器
        "url": "https://ansible-executor.xxx.azurecontainerapps.io",   // 必填
        "api_key": "...",                                   // 或改用 api_key_env
        "api_key_env": "ANSIBLE_EXECUTOR_API_KEY",          // 从环境变量读 key（Keychain 注入场景）
        "protocol": "azure-acr",                            // 可选，默认 azure-acr
        "mode": "auto",                                     // 可选：auto（默认）/ list / all
        "allowed_tasks": ["meraki_get_switch_ports"],       // 可选，显式白名单
        "timeout": 300,                                     // 可选，单次请求超时秒数
        "description": "执行 Ansible playbook 的容器"         // 可选，给模型看的说明
      }
    ]
  }

白名单语义（mode）：
  auto（默认）  allowed_tasks 里的放行；容器在 /tasks 里标了 read_only: true 的自动放行；
                再回退到内置 DEFAULT_READ_ONLY_TASKS（保持旧行为）。写操作默认拦住。
  list          只放行 allowed_tasks 里明确列出的任务（容器自报的 read_only 不算数）
  all           不限制（创建/删除类任务也交给模型，危险）
  allowed_tasks 里写 "*" 等价于 mode=all。
"""

from __future__ import annotations

import json
import logging
import os
import re
import time
from dataclasses import dataclass, field
from typing import Any, Optional

import httpx

logger = logging.getLogger("tools.container")

PROVIDER = "container"

# ---- 环境变量 ----
ENDPOINTS_JSON_ENV = "CONTAINER_ENDPOINTS_JSON"          # 内联 JSON
REGISTRY_FILE_ENV = "AICHAT_CONTAINERS_FILE"             # 注册表文件路径
LEGACY_URL_ENV = "ANSIBLE_EXECUTOR_URL"                  # 旧配置（单容器）
LEGACY_KEY_ENV = "ANSIBLE_EXECUTOR_API_KEY"
LEGACY_NAME_ENV = "ANSIBLE_EXECUTOR_NAME"
ALLOWED_TASKS_ENV = "ANSIBLE_ALLOWED_TASKS"              # 旧白名单（仅作用于旧配置生成的条目）
TIMEOUT_ENV = "CONTAINER_DEFAULT_TIMEOUT"
LEGACY_TIMEOUT_ENV = "ANSIBLE_EXECUTOR_TIMEOUT"

DEFAULT_TIMEOUT = 300.0
SUPPORTED_PROTOCOLS = ("azure-acr",)

# 容器没有 read_only 元数据时，auto 模式回退到这份内置只读名单（保持 2026-09-23 之前的行为）
DEFAULT_READ_ONLY_TASKS: tuple[str, ...] = (
    "meraki_get_switch_ports",
    "meraki_get_switch_port_statuses",
    "meraki_get_firewall_rules",
)

# 结果里 stdout / stderr 各留多少字符（main.py 还会按 MAX_TOOL_RESULT_CHARS 整体截断）
STDOUT_LIMIT = 6000
STDERR_LIMIT = 2000

KNOWN_PROTOCOL_PATHS = {"/run-task", "/run-ansible", "/tasks", "/health", "/docs", "/openapi.json"}

# App（打包版）从 Keychain 读出密钥后在启动后端时注入的变量名前缀：
# 容器名 ansible -> AICHAT_CONTAINER_KEY_ANSIBLE
AUTO_KEY_ENV_PREFIX = "AICHAT_CONTAINER_KEY_"

SCHEMAS: list[dict] = [
    {
        "type": "function",
        "function": {
            "name": "container_list_endpoints",
            "description": (
                "列出当前注册的所有 Serverless 容器（名字、地址、可调用任务的白名单模式、说明）。"
                "不确定该用哪个容器、或想确认新容器是否已经注册时先调它。"
            ),
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "container_list_tasks",
            "description": (
                "列出某个容器内置的所有任务及其必填参数，并标明哪些任务当前允许 AI 调用。"
                "准备调用 container_run_task 前先用它确认任务名和参数名。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "container": {
                        "type": "string",
                        "description": "容器名（见 container_list_endpoints）；只有一个容器时可省略",
                    }
                },
                "required": [],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "container_run_task",
            "description": (
                "在指定容器里执行一个内置任务（例如查询 Meraki 交换机端口状态、按模板创建 Azure VNet/子网/NSG）。"
                "任务名与参数名以 container_list_tasks 返回为准；写操作类任务是否可调用由容器各自的白名单决定，"
                "被拒绝时结果会说明该怎么放开。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "container": {
                        "type": "string",
                        "description": "容器名（见 container_list_endpoints）；只有一个容器时可省略",
                    },
                    "task": {
                        "type": "string",
                        "description": "任务名，例如 meraki_get_switch_ports、azure_create_vnet",
                    },
                    "params": {
                        "type": "object",
                        "description": (
                            "任务参数键值对，键名必须与 container_list_tasks 返回的 required_params 一致；"
                            "例如 {\"serial\": \"Q2XX-XXXX-XXXX\"}"
                        ),
                    },
                },
                "required": ["task"],
            },
        },
    },
]

TOOL_NAMES: tuple[str, ...] = tuple(schema["function"]["name"] for schema in SCHEMAS)


# ------------------------------------------------------------------ 基础工具


def _ok(**payload: Any) -> str:
    return json.dumps({"status": "success", "provider": PROVIDER, **payload}, ensure_ascii=False)


def _error(message: str) -> str:
    return json.dumps({"status": "error", "provider": PROVIDER, "message": message}, ensure_ascii=False)


def _env(name: str) -> str:
    return (os.environ.get(name) or "").strip()


def registry_path() -> str:
    """注册表文件路径：环境变量优先，默认放 App Support（与 aichat.db 同目录）"""
    override = _env(REGISTRY_FILE_ENV)
    if override:
        return os.path.expanduser(override)
    return os.path.expanduser("~/Library/Application Support/AIChatApp/containers.json")


def _default_timeout() -> float:
    for name in (TIMEOUT_ENV, LEGACY_TIMEOUT_ENV):
        raw = _env(name)
        if raw:
            try:
                value = float(raw)
            except ValueError:
                continue
            if value > 0:
                return value
    return DEFAULT_TIMEOUT


def _normalize_url(raw: str) -> str:
    """去掉尾巴上的斜杠和用户可能多粘的接口路径"""
    url = (raw or "").strip().rstrip("/")
    for suffix in KNOWN_PROTOCOL_PATHS:
        if url.endswith(suffix):
            url = url[: -len(suffix)].rstrip("/")
    return url


def _split_tasks(raw: Any) -> list[str]:
    """allowed_tasks 允许写成 list 或 "a,b,c" """
    if raw is None:
        return []
    if isinstance(raw, str):
        return [item.strip() for item in raw.split(",") if item.strip()]
    if isinstance(raw, (list, tuple)):
        return [str(item).strip() for item in raw if str(item).strip()]
    return []


@dataclass
class Endpoint:
    """一个注册好的容器"""

    name: str
    url: str
    api_key: str = ""
    api_key_env: str = ""
    protocol: str = "azure-acr"
    mode: str = "auto"
    allowed_tasks: list[str] = field(default_factory=list)
    timeout: float = DEFAULT_TIMEOUT
    description: str = ""
    source: str = "file"          # file / env_json / legacy_env

    @property
    def all_tasks_allowed(self) -> bool:
        return self.mode == "all" or "*" in self.allowed_tasks

    @property
    def auto_key_env(self) -> str:
        """
        App（打包版）从 Keychain 读出密钥后注入的变量名。
        这样「密钥不落盘」也能用：注册表里不写 api_key，App 启动后端时注入即可。
        """
        return AUTO_KEY_ENV_PREFIX + re.sub(r"[^A-Za-z0-9]+", "_", self.name).upper()

    @property
    def effective_key(self) -> str:
        """取密钥：注册表里的 api_key > api_key_env 指向的变量 > App 注入的 AICHAT_CONTAINER_KEY_<NAME>"""
        if self.api_key:
            return self.api_key
        if self.api_key_env and _env(self.api_key_env):
            return _env(self.api_key_env)
        return _env(self.auto_key_env)

    @property
    def key_source(self) -> Optional[str]:
        """密钥来自哪里（给 health / 排障用）：registry / <变量名> / None"""
        if self.api_key:
            return "registry"
        if self.api_key_env and _env(self.api_key_env):
            return self.api_key_env
        if _env(self.auto_key_env):
            return self.auto_key_env
        return None


# ------------------------------------------------------------------ 注册表


_FILE_CACHE: dict[str, Any] = {"key": None, "entries": []}


def _parse_registry_payload(payload: Any, source: str) -> list[Endpoint]:
    if isinstance(payload, dict):
        items = payload.get("containers") or payload.get("endpoints") or []
    elif isinstance(payload, list):
        items = payload
    else:
        items = []

    endpoints: list[Endpoint] = []
    for item in items:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name") or "").strip()
        url = _normalize_url(str(item.get("url") or item.get("base_url") or ""))
        if not name or not url:
            logger.warning("[Container] 跳过注册表条目（缺 name 或 url）：%s", item)
            continue
        mode = str(item.get("mode") or "auto").strip().lower()
        if mode not in ("auto", "list", "all"):
            logger.warning("[Container] %s 的 mode=%s 无效，按 auto 处理", name, mode)
            mode = "auto"
        try:
            timeout = float(item.get("timeout") or 0) or _default_timeout()
        except (TypeError, ValueError):
            timeout = _default_timeout()
        endpoints.append(
            Endpoint(
                name=name,
                url=url,
                api_key=str(item.get("api_key") or "").strip(),
                api_key_env=str(item.get("api_key_env") or "").strip(),
                protocol=str(item.get("protocol") or "azure-acr").strip().lower() or "azure-acr",
                mode=mode,
                allowed_tasks=_split_tasks(item.get("allowed_tasks")),
                timeout=timeout if timeout > 0 else DEFAULT_TIMEOUT,
                description=str(item.get("description") or "").strip(),
                source=source,
            )
        )
    return endpoints


def _file_endpoints() -> list[Endpoint]:
    """读注册表文件；用 mtime+size 做缓存，文件一改立即重新解析（热加载）"""
    path = registry_path()
    try:
        stat = os.stat(path)
    except OSError:
        _FILE_CACHE["key"] = None
        _FILE_CACHE["entries"] = []
        return []

    key = (path, stat.st_mtime_ns, stat.st_size)
    if _FILE_CACHE.get("key") == key:
        return list(_FILE_CACHE["entries"])

    try:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
    except Exception as exc:
        logger.warning("[Container] 注册表文件解析失败（%s）：%s: %s", path, type(exc).__name__, exc)
        _FILE_CACHE["key"] = None
        _FILE_CACHE["entries"] = []
        return []

    entries = _parse_registry_payload(payload, source="file")
    _FILE_CACHE["key"] = key
    _FILE_CACHE["entries"] = entries
    return list(entries)


def _env_json_endpoints() -> list[Endpoint]:
    raw = _env(ENDPOINTS_JSON_ENV)
    if not raw:
        return []
    try:
        return _parse_registry_payload(json.loads(raw), source="env_json")
    except Exception as exc:
        logger.warning("[Container] %s 不是合法 JSON：%s: %s", ENDPOINTS_JSON_ENV, type(exc).__name__, exc)
        return []


def _legacy_endpoint() -> list[Endpoint]:
    """旧配置：ANSIBLE_EXECUTOR_URL / _API_KEY 当成一个条目，名字默认 ansible"""
    url = _normalize_url(_env(LEGACY_URL_ENV))
    key = _env(LEGACY_KEY_ENV)
    if not url:
        return []
    name = _env(LEGACY_NAME_ENV) or "ansible"
    allowed = _split_tasks(_env(ALLOWED_TASKS_ENV))
    mode = "auto"
    if allowed == ["*"]:
        allowed, mode = [], "all"
    return [
        Endpoint(
            name=name,
            url=url,
            api_key=key,
            api_key_env=LEGACY_KEY_ENV,
            protocol="azure-acr",
            mode=mode,
            allowed_tasks=[task for task in allowed if task != "*"],
            timeout=_default_timeout(),
            description="旧的单容器配置（ANSIBLE_EXECUTOR_URL）",
            source="legacy_env",
        )
    ]


def registry_entries() -> list[Endpoint]:
    """
    合并三处配置成一个有序注册表（文件 > 环境 JSON > 旧配置）：
    同名条目只保留优先级最高的那一条，避免同一容器被注册两遍。
    """
    merged: list[Endpoint] = []
    seen: set[str] = set()
    for group in (_file_endpoints(), _env_json_endpoints(), _legacy_endpoint()):
        for endpoint in group:
            if endpoint.name in seen:
                continue
            seen.add(endpoint.name)
            merged.append(endpoint)
    return merged


def endpoint_names() -> list[str]:
    return [endpoint.name for endpoint in registry_entries()]


def resolve_endpoint(name: Optional[str]) -> Endpoint:
    """
    按名字取容器；名字为空时取注册表里唯一/第一个容器。
    找不到时抛出带「现有容器清单 + 怎么加容器」的说明性错误。
    """
    entries = registry_entries()
    if not entries:
        raise RuntimeError(
            "容器注册表是空的：请在 " + registry_path() + " 里加一条，"
            "或设置环境变量 " + ENDPOINTS_JSON_ENV + " / " + LEGACY_URL_ENV + "。"
            "格式示例见 tools/container_tools.py 顶部注释。"
        )

    wanted = (name or "").strip()
    if not wanted:
        if len(entries) == 1:
            return entries[0]
        return entries[0]

    for endpoint in entries:
        if endpoint.name == wanted:
            return endpoint
    raise RuntimeError(
        "没有注册名为 " + wanted + " 的容器。当前可用：" + "、".join(item.name for item in entries)
    )


# ------------------------------------------------------------------ 白名单


def _meta_read_only(meta: Any) -> Optional[bool]:
    if isinstance(meta, dict) and isinstance(meta.get("read_only"), bool):
        return meta["read_only"]
    return None


def is_callable(endpoint: Endpoint, task: str, meta: Any = None) -> bool:
    """判断某任务是否允许 AI 调用（meta 来自容器 /tasks；拿不到就传 None）"""
    if endpoint.all_tasks_allowed:
        return True
    if task in endpoint.allowed_tasks:
        return True
    if endpoint.mode == "list":
        return False
    # auto：容器自报只读的放行，再回退到内置只读名单
    if _meta_read_only(meta) is True:
        return True
    return task in DEFAULT_READ_ONLY_TASKS


def _mode_label(endpoint: Endpoint) -> str:
    if endpoint.all_tasks_allowed:
        return "全部任务（mode=all）"
    if endpoint.mode == "list":
        return "白名单：" + ("、".join(endpoint.allowed_tasks) or "（空）")
    explicit = "、".join(endpoint.allowed_tasks) if endpoint.allowed_tasks else "（无）"
    return "auto（显式：" + explicit + "；容器自报 read_only 的自动放行；其余回退内置只读名单）"


def _reject_reason(endpoint: Endpoint, task: str, meta: Any) -> str:
    if _meta_read_only(meta) is False:
        return "；容器把它标为写操作（read_only=false），默认不对 AI 开放"
    return "；它不是只读任务，也不在 " + endpoint.name + " 的白名单里"


# ------------------------------------------------------------------ HTTP


def _headers(endpoint: Endpoint) -> dict:
    return {"X-API-Key": endpoint.effective_key, "Accept": "application/json"}


def _stopped_container_hint(status_code: int, text: str) -> str:
    """ACA 在容器停止时返回自己的 HTML 404 页，识别出来给一条能直接照做的提示"""
    lowered = (text or "").lower()
    if status_code == 404 and ("<html" in lowered or "container app" in lowered):
        return "（容器当前处于 Stopped 状态，先在 Azure 上启动它：az containerapp start -n <name> -g <rg>）"
    return ""


def _check_ready(endpoint: Endpoint) -> None:
    if endpoint.protocol not in SUPPORTED_PROTOCOLS:
        raise RuntimeError(
            "容器 " + endpoint.name + " 声明的 protocol=" + endpoint.protocol
            + " 暂不支持（当前只支持：" + "、".join(SUPPORTED_PROTOCOLS) + "）"
        )
    if not endpoint.url:
        raise RuntimeError("容器 " + endpoint.name + " 没有配置 url")
    if not endpoint.effective_key:
        raise RuntimeError(
            "容器 " + endpoint.name + " 没有可用的 API Key。三种填法（任选其一）："
            "① 在注册表里写 \"api_key\"；"
            "② 写 \"api_key_env\": \"变量名\"，由环境变量提供；"
            "③ 由 App 设置页填写（存 Keychain，启动后端时注入 " + endpoint.auto_key_env + "）。"
        )


def _request(endpoint: Endpoint, method: str, path: str, payload: Optional[dict] = None) -> Any:
    _check_ready(endpoint)
    url = endpoint.url + path
    try:
        with httpx.Client(timeout=endpoint.timeout) as client:
            if method == "GET":
                response = client.get(url, headers=_headers(endpoint))
            else:
                response = client.post(url, headers=_headers(endpoint), json=payload or {})
    except httpx.TimeoutException as exc:
        raise RuntimeError(
            "调用容器 " + endpoint.name + " 超时（" + str(int(endpoint.timeout)) + "s）：" + path
            + "；任务可能还在容器里跑，可稍后用相同参数重试或调大该条目的 timeout"
        ) from exc
    except httpx.HTTPError as exc:
        raise RuntimeError(
            "无法连接容器 " + endpoint.name + "（" + url + "）：" + type(exc).__name__ + ": " + str(exc)
        ) from exc

    text = response.text or ""
    if response.status_code >= 400:
        snippet = text[:300].replace("\n", " ").strip()
        if response.status_code in (401, 403):
            hint = "（X-API-Key 不正确：检查容器 " + endpoint.name + " 的 api_key / api_key_env）"
        else:
            hint = _stopped_container_hint(response.status_code, text)
        raise RuntimeError(
            "容器 " + endpoint.name + " " + path + " 返回 HTTP " + str(response.status_code) + hint + "：" + snippet
        )

    if not text:
        return {}
    try:
        return response.json()
    except Exception:
        return {"raw": text[:2000]}


def _fetch_tasks(endpoint: Endpoint) -> dict:
    data = _request(endpoint, "GET", "/tasks")
    tasks = data.get("tasks") if isinstance(data, dict) else None
    if not isinstance(tasks, dict):
        tasks = {}
    return tasks


# ------------------------------------------------------------------ 工具实现


def _list_endpoints(_arguments: dict) -> str:
    entries = registry_entries()
    items = [
        {
            "name": endpoint.name,
            "url": endpoint.url,
            "protocol": endpoint.protocol,
            "api_key_configured": bool(endpoint.effective_key),
            "whitelist": _mode_label(endpoint),
            "timeout": endpoint.timeout,
            "description": endpoint.description or None,
            "source": endpoint.source,
        }
        for endpoint in entries
    ]
    if not entries:
        return _ok(
            action="list_endpoints",
            count=0,
            containers=[],
            registry_file=registry_path(),
            hint="注册表为空：在 " + registry_path() + " 里加一条容器即可（改完立即生效，不用重启后端）",
        )
    return _ok(
        action="list_endpoints",
        count=len(items),
        containers=items,
        registry_file=registry_path(),
    )


def _list_tasks(arguments: dict) -> str:
    endpoint = resolve_endpoint(arguments.get("container"))
    raw_tasks = _fetch_tasks(endpoint)

    normalized: dict[str, dict] = {}
    for name, meta in raw_tasks.items():
        required = meta.get("required_params") if isinstance(meta, dict) else None
        entry = {
            "required_params": required or [],
            "callable_by_ai": is_callable(endpoint, name, meta),
        }
        read_only = _meta_read_only(meta)
        if read_only is not None:
            entry["read_only"] = read_only
        normalized[name] = entry

    callable_names = [name for name, meta in normalized.items() if meta["callable_by_ai"]]
    return _ok(
        action="list_tasks",
        container=endpoint.name,
        url=endpoint.url,
        count=len(normalized),
        callable_count=len(callable_names),
        whitelist=_mode_label(endpoint),
        tasks=normalized,
    )


def _run_task(arguments: dict) -> str:
    endpoint = resolve_endpoint(arguments.get("container"))

    task = str(arguments.get("task") or "").strip()
    if not task:
        raise ValueError("task 是必填参数（任务名，如 meraki_get_switch_ports）")

    # 先做不花网络的输入校验（任务名已在上面检查），再做授权判断、最后才发请求
    params = arguments.get("params")
    if params is None:
        params = {}
    if not isinstance(params, dict):
        raise ValueError("params 必须是对象（键值对），例如 {\"serial\": \"Q2XX-XXXX-XXXX\"}")

    cleaned: dict[str, Any] = {}
    for key, value in params.items():
        if value is None:
            continue
        if isinstance(value, (str, int, float, bool)):
            cleaned[str(key)] = value
        elif isinstance(value, (list, tuple)) and all(
            isinstance(item, (str, int, float, bool)) for item in value
        ):
            cleaned[str(key)] = list(value)
        else:
            raise ValueError("参数 " + str(key) + " 的类型不支持（只接受字符串/数字/布尔/简单数组）")

    # 决策：显式白名单/全部放行时不用打网络；auto 模式下需要看容器的 read_only 元数据
    needs_meta = not endpoint.all_tasks_allowed and task not in endpoint.allowed_tasks and endpoint.mode == "auto"
    meta = None
    if needs_meta and task not in DEFAULT_READ_ONLY_TASKS:
        try:
            meta = _fetch_tasks(endpoint).get(task)
        except Exception as exc:  # 拿不到元数据不影响「内置只读名单」这条快路径
            logger.warning("[Container] %s 取任务元数据失败：%s", endpoint.name, exc)

    if not is_callable(endpoint, task, meta):
        raise RuntimeError(
            "任务 " + task + " 不允许 AI 在容器 " + endpoint.name + " 上调用"
            + _reject_reason(endpoint, task, meta)
            + "。当前规则：" + _mode_label(endpoint)
            + "。需要放开时，在注册表里给 " + endpoint.name
            + " 的 allowed_tasks 加上 \"" + task + "\"（或 mode=all，危险）"
        )

    started = time.perf_counter()
    data = _request(endpoint, "POST", "/run-task", {"task": task, "params": cleaned})
    duration_ms = int((time.perf_counter() - started) * 1000)

    if not isinstance(data, dict):
        data = {"raw": data}

    stdout = str(data.get("stdout") or "")
    stderr = str(data.get("stderr") or "")
    returncode = data.get("returncode")
    logger.info("[Container] %s/%s -> returncode=%s duration=%sms", endpoint.name, task, returncode, duration_ms)

    payload: dict[str, Any] = {
        "action": "run_task",
        "container": endpoint.name,
        "task": task,
        "params": cleaned,
        "returncode": returncode,
        "duration_ms": duration_ms,
        # 容器已用 json callback 抽了干净数据出来，优先给模型看这个
        "data": data.get("data"),
        "stdout": stdout[:STDOUT_LIMIT],
        "stderr": stderr[:STDERR_LIMIT],
    }
    if len(stdout) > STDOUT_LIMIT:
        payload["stdout_truncated"] = True
    return _ok(**payload)


_HANDLERS = {
    "container_list_endpoints": _list_endpoints,
    "container_list_tasks": _list_tasks,
    "container_run_task": _run_task,
}


def execute(tool_name: str, arguments: Optional[dict] = None) -> str:
    """执行容器工具；任何异常都收敛成 {"status": "error", ...}"""
    arguments = arguments or {}
    handler = _HANDLERS.get(tool_name)
    if handler is None:
        return _error("未知的容器工具：" + str(tool_name))

    try:
        return handler(arguments)
    except Exception as exc:
        logger.warning("[Container] %s 执行失败：%s: %s", tool_name, type(exc).__name__, exc)
        return _error(type(exc).__name__ + ": " + str(exc))


# ------------------------------------------------------------------ 健康检查


def health() -> dict:
    """给 /api/health 与设置页用的注册表快照（不发网络请求）"""
    try:
        entries = registry_entries()
    except Exception as exc:  # 理论上不会抛，兜底避免拖垮 /api/health
        return {"configured": False, "containers": [], "hint": type(exc).__name__ + ": " + str(exc)}

    containers = [
        {
            "name": endpoint.name,
            "url": endpoint.url,
            "protocol": endpoint.protocol,
            "mode": "all" if endpoint.all_tasks_allowed else endpoint.mode,
            "allowed_tasks": endpoint.allowed_tasks,
            "has_api_key": bool(endpoint.effective_key),
            # 密钥来源：registry（写在注册表里）/ 环境变量名 / None
            "key_source": endpoint.key_source,
            "auto_key_env": endpoint.auto_key_env,
            "timeout": endpoint.timeout,
            "source": endpoint.source,
        }
        for endpoint in entries
    ]
    misconfigured = [item["name"] for item in containers if not item["has_api_key"]]
    return {
        "configured": bool(containers) and not misconfigured,
        "container_count": len(containers),
        "containers": containers,
        "registry_file": registry_path(),
        "registry_file_exists": os.path.exists(registry_path()),
        "default_read_only_tasks": list(DEFAULT_READ_ONLY_TASKS),
        "tools": list(TOOL_NAMES),
        "tool_count": len(TOOL_NAMES),
        "hint": None if containers else (
            "注册表为空：在 " + registry_path() + " 里加一条容器，"
            "或设置 " + ENDPOINTS_JSON_ENV + " / " + LEGACY_URL_ENV
        ),
    }


# ---------------------------------------------------------------- 公开入口
# 连接测试（/api/test_connection）这类场景直接调用，不经过 AI 工具循环。


def container_list_endpoints(arguments: Optional[dict] = None) -> str:
    return execute("container_list_endpoints", arguments or {})


def container_list_tasks(arguments: Optional[dict] = None) -> str:
    return execute("container_list_tasks", arguments or {})


def container_run_task(arguments: Optional[dict] = None) -> str:
    return execute("container_run_task", arguments or {})
