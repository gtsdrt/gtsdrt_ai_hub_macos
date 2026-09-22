import csv
import datetime
import json
import logging
import os
import re
import uuid
from collections import defaultdict
from io import StringIO
from typing import Any

import azure.durable_functions as df
import azure.functions as func
import httpx
import jwt
import requests
from azure.core.credentials import AzureNamedKeyCredential
from azure.core.exceptions import ClientAuthenticationError
from azure.data.tables import TableServiceClient
from azure.identity import ClientSecretCredential, DefaultAzureCredential
from azure.keyvault.secrets import SecretClient
from azure.mgmt.compute import ComputeManagementClient
from azure.mgmt.containerinstance import ContainerInstanceManagementClient
from azure.mgmt.containerservice import ContainerServiceClient
from azure.mgmt.keyvault import KeyVaultManagementClient
from azure.mgmt.monitor import MonitorManagementClient
from azure.mgmt.network import NetworkManagementClient
from azure.mgmt.resource import ResourceManagementClient, SubscriptionClient
from azure.mgmt.resourcegraph import ResourceGraphClient
from azure.mgmt.resourcegraph.models import QueryRequest
from azure.mgmt.sql import SqlManagementClient
from azure.mgmt.storage import StorageManagementClient
from azure.mgmt.web import WebSiteManagementClient
from openai import OpenAI

###app = df.DFApp(http_auth_level=func.AuthLevel.FUNCTION)
app = func.FunctionApp(http_auth_level=func.AuthLevel.FUNCTION)

credential = DefaultAzureCredential()


#=====（修改内容：新增默认 AI Provider、多 Provider 初始余额与定价配置）=====
def _env_float(name: str, default: float = 0.0) -> float:
    try:
        return float(os.environ.get(name, default))
    except Exception:
        return default


DEFAULT_AI_PROVIDER = os.environ.get("DEFAULT_AI_PROVIDER", "deepseek")
VALID_AI_PROVIDERS = {"kimi", "deepseek"}

DEEPSEEK_INITIAL_BALANCE = _env_float("DEEPSEEK_INITIAL_BALANCE", 0)
KIMI_INITIAL_BALANCE = _env_float("KIMI_INITIAL_BALANCE", 0)

PRICING = {
    "deepseek": {
        "prompt_per_million_usd": _env_float("DEEPSEEK_PROMPT_PRICE_PER_MILLION", 0.14),
        "completion_per_million_usd": _env_float("DEEPSEEK_COMPLETION_PRICE_PER_MILLION", 0.28),
    },
    "kimi": {
        "prompt_per_million_usd": _env_float("KIMI_PROMPT_PRICE_PER_MILLION", 0.0),
        "completion_per_million_usd": _env_float("KIMI_COMPLETION_PRICE_PER_MILLION", 0.0),
    },
}


def _normalize_provider(provider: str) -> str:
    p = str(provider or DEFAULT_AI_PROVIDER).strip().lower()
    if p not in VALID_AI_PROVIDERS:
        return "qwen"
    return p


def _estimate_cost(provider: str, prompt_tokens: int, completion_tokens: int) -> float:
    provider = _normalize_provider(provider)
    pricing = PRICING.get(provider, {})
    prompt_price = float(pricing.get("prompt_per_million_usd", 0.0))
    completion_price = float(pricing.get("completion_per_million_usd", 0.0))

    return (
        (float(prompt_tokens or 0) / 1_000_000) * prompt_price
        + (float(completion_tokens or 0) / 1_000_000) * completion_price
    )
#=====（修改内容结束）=====

#=====Ansible Container Configuration=====

ANSIBLE_CONTAINER_URL = os.environ.get(
    "ANSIBLE_CONTAINER_URL",
    "https://ansible-executor.proudhill-c1ac3b31.norwayeast.azurecontainerapps.io"
)
ANSIBLE_CONTAINER_SECRET_NAME = os.environ.get("ANSIBLE_CONTAINER_SECRET_NAME", "ANSIBLE-EXECUTOR-API-KEY")

# ===== Playbook 内容模板 =====
# 注意：容器 /run-ansible 接收的是完整 YAML 内容（不是文件名！）
# cisco.meraki 的 info 模块返回字段为 meraki_response（不是 response）
# 参数用 __ORG_ID__ / __NETWORK_ID__ 占位符，调用前由 Python 替换

MERAKI_ORGS_PLAYBOOK = """- name: List Meraki organizations
  hosts: localhost
  gather_facts: false
  collections:
    - cisco.meraki
  tasks:
    - name: Get organizations
      cisco.meraki.organizations_info:
        meraki_api_key: "{{ lookup('env', 'MERAKI_DASHBOARD_API_KEY') }}"
      register: result
    - name: Output
      ansible.builtin.debug:
        var: result.meraki_response
"""

MERAKI_NETWORKS_PLAYBOOK = """- name: List Meraki networks
  hosts: localhost
  gather_facts: false
  collections:
    - cisco.meraki
  tasks:
    - name: Get networks
      cisco.meraki.networks_info:
        meraki_api_key: "{{ lookup('env', 'MERAKI_DASHBOARD_API_KEY') }}"
        organizationId: "__ORG_ID__"
      register: result
    - name: Output
      ansible.builtin.debug:
        var: result.meraki_response
"""

MERAKI_DEVICES_PLAYBOOK = """- name: List Meraki devices
  hosts: localhost
  gather_facts: false
  collections:
    - cisco.meraki
  tasks:
    - name: Get devices
      cisco.meraki.devices_info:
        meraki_api_key: "{{ lookup('env', 'MERAKI_DASHBOARD_API_KEY') }}"
        networkId: "__NETWORK_ID__"
      register: result
    - name: Output
      ansible.builtin.debug:
        var: result.meraki_response
"""

MERAKI_VPN_STATUS_PLAYBOOK = """- name: Get Meraki VPN status
  hosts: localhost
  gather_facts: false
  collections:
    - cisco.meraki
  tasks:
    - name: Get VPN statuses
      cisco.meraki.networks_appliance_vpn_statuses_info:
        meraki_api_key: "{{ lookup('env', 'MERAKI_DASHBOARD_API_KEY') }}"
        networkId: "__NETWORK_ID__"
      register: result
    - name: Output
      ansible.builtin.debug:
        var: result.meraki_response
"""

# Ansible 工具名 → (playbook 内容, 需要的参数列表)
ANSIBLE_MERAKI_PLAYBOOKS = {
    "ansible_meraki_list_organizations": (MERAKI_ORGS_PLAYBOOK, []),
    "ansible_meraki_list_networks":      (MERAKI_NETWORKS_PLAYBOOK, ["org_id"]),
    "ansible_meraki_list_devices":       (MERAKI_DEVICES_PLAYBOOK, ["network_id"]),
    "ansible_meraki_get_vpn_status":     (MERAKI_VPN_STATUS_PLAYBOOK, ["network_id"]),
    # 按需补充
}

def _run_ansible_playbook(playbook_content: str, timeout: int = 180) -> dict:
    """调用 ansible-executor 容器执行 playbook（传完整 YAML 内容）"""
    api_key = get_secret_from_keyvault(ANSIBLE_CONTAINER_SECRET_NAME)
    if not api_key:
        raise RuntimeError(f"容器 API Key 未找到（Key Vault secret: {ANSIBLE_CONTAINER_SECRET_NAME}）")

    resp = httpx.post(
        f"{ANSIBLE_CONTAINER_URL}/run-ansible",
        json={"playbook": playbook_content},
        headers={"X-API-Key": api_key},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()

# ===== 通用 Ansible 任务透传（一次性接入：后续新功能只改容器镜像，本文件零改动）=====
def _container_api_key() -> str:
    api_key = get_secret_from_keyvault(ANSIBLE_CONTAINER_SECRET_NAME)
    if not api_key:
        raise RuntimeError(f"容器 API Key 未找到（Key Vault secret: {ANSIBLE_CONTAINER_SECRET_NAME}）")
    return api_key


def _list_ansible_tasks() -> dict:
    """查询容器当前支持的内置任务列表（对应容器 GET /tasks）"""
    resp = httpx.get(
        f"{ANSIBLE_CONTAINER_URL}/tasks",
        headers={"X-API-Key": _container_api_key()},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def _run_ansible_task(task: str, params: dict, timeout: int = 300) -> dict:
    """调用容器 /run-task 执行内置命名任务"""
    resp = httpx.post(
        f"{ANSIBLE_CONTAINER_URL}/run-task",
        json={"task": task, "params": params or {}},
        headers={"X-API-Key": _container_api_key()},
        timeout=timeout,
    )
    resp.raise_for_status()
    return resp.json()


def execute_ansible_task_tool(tool_name: str, arguments: dict) -> str:
    """通用 Ansible 任务工具：ansible_list_tasks / ansible_run_task"""
    try:
        if tool_name == "ansible_list_tasks":
            data = _list_ansible_tasks()
            return json.dumps({"status": "success", **data}, ensure_ascii=False)

        if tool_name == "ansible_run_task":
            task = arguments.get("task")
            if not task:
                return json.dumps({"status": "error", "message": "Missing required argument: task"})

            logging.info(f"[Ansible] 开始执行任务: {task}, params={arguments.get('params')}")

            result = _run_ansible_task(task, arguments.get("params") or {})

            logging.info(f"[Ansible] 任务返回: rc={result.get('returncode')}, data_type={type(result.get('data'))}, data_preview={str(result.get('data'))[:500]}")

            if result.get("returncode") != 0:
                # 失败时返回详细错误（包括 stdout 前 2000 字）
                stderr = result.get("stderr", "")
                stdout = result.get("stdout", "")
                error_msg = f"Task failed (rc={result.get('returncode')}): {stderr[-500:]}"
                if not stderr and stdout:
                    error_msg = f"Task failed (rc={result.get('returncode')}): stdout={stdout[-1000:]}"
                logging.error(f"[Ansible] 任务失败: {error_msg}")
                return json.dumps({
                    "status": "error",
                    "message": error_msg,
                    "stdout_preview": stdout[-2000:] if stdout else "",
                    "stderr_preview": stderr[-1000:] if stderr else ""
                }, ensure_ascii=False)

            # 成功时，如果 data 为 None，把 stdout 摘要也带回去
            data = result.get("data")
            if data is None:
                stdout = result.get("stdout", "")
                logging.warning("[Ansible] 任务成功但 data 为 None，返回 stdout 摘要")
                # 尝试从 stdout 中提取人类可读的结果
                try:
                    doc = json.loads(stdout)
                    # 提取所有 debug 任务的 msg
                    msgs = []
                    for play in doc.get("plays", []):
                        for t in play.get("tasks", []):
                            for host_result in t.get("hosts", {}).values():
                                if "msg" in host_result:
                                    msgs.append(host_result["msg"])
                    if msgs:
                        data = msgs[-1]  # 最后一个 debug 输出
                    else:
                        data = {"_note": "Playbook executed successfully but no structured data extracted", "_raw_preview": stdout[-1000:]}
                except Exception:
                    data = {"_note": "Playbook executed successfully", "_raw_preview": stdout[-1000:]}

            return json.dumps({
                "status": "success",
                "provider": "ansible-container",
                "task": task,
                "data": data,
            }, ensure_ascii=False)

        return json.dumps({"status": "error", "message": f"Unknown task tool: {tool_name}"})
    except Exception as e:
        logging.exception(f"[Ansible] execute_ansible_task_tool 异常: {e}")
        return json.dumps({"status": "error", "message": str(e)})
# ===== 通用任务透传结束 =====

def _extract_ansible_result(stdout: str):
    """从 Ansible 裸 stdout 中提取最后一个任务的 debug 输出"""
    # 先检查 PLAY RECAP 是否失败
    if "failed=0" not in stdout or "unreachable=0" not in stdout:
        raise RuntimeError(f"Ansible play 执行有失败任务: {stdout[-800:]}")

    # 定位最后一个 "ok: [localhost] => " 输出块
    idx = stdout.rfind("=> ")
    if idx == -1:
        raise ValueError("stdout 中未找到任务输出")

    tail = stdout[idx + 3:]
    recap = tail.find("\nPLAY RECAP")
    if recap != -1:
        tail = tail[:recap]

    payload = json.loads(tail.strip())

    # debug var=result.meraki_response 的输出键为 "result.meraki_response"
    for key in ("result.meraki_response", "result.response", "msg"):
        if key in payload:
            return payload[key]
    return payload

def execute_meraki_ansible_tool(tool_name: str, arguments: dict) -> str:
    """通过 Ansible 容器执行 Meraki 查询"""
    try:
        entry = ANSIBLE_MERAKI_PLAYBOOKS.get(tool_name)
        if not entry:
            return json.dumps({"status": "error", "message": f"No playbook mapped for: {tool_name}"})

        playbook, required_args = entry

        # 校验必填参数
        missing = [a for a in required_args if not arguments.get(a)]
        if missing:
            return json.dumps({"status": "error", "message": f"Missing required arguments: {missing}"})

        # 替换占位符注入参数
        content = playbook
        for a in required_args:
            content = content.replace(f"__{a.upper()}__", str(arguments[a]))

        result = _run_ansible_playbook(content)

        if result.get("returncode") != 0:
            return json.dumps({
                "status": "error",
                "message": f"Ansible failed (rc={result.get('returncode')}): {result.get('stderr', '')[-500:]}"
            })

        data = _extract_ansible_result(result.get("stdout", ""))
        return json.dumps({
            "status": "success",
            "provider": "meraki-ansible",
            "action": tool_name,
            "data": data,
        }, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})

#=====（修改内容：修复 CONFIG key 尾随空格）=====
CONFIG = {
    # 占位符：实际部署请用环境变量 SUBSCRIPTION_ID 覆盖
    "SUBSCRIPTION_ID": os.environ.get("SUBSCRIPTION_ID", "00000000-0000-0000-0000-000000000000"),
    "MANAGED_IDENTITY_OBJECT_ID": os.environ.get("MANAGED_IDENTITY_OBJECT_ID", "00000000-0000-0000-0000-000000000001"),

    # Kimi / Moonshot
    "KEY_VAULT_URL": os.environ.get("KEY_VAULT_URL", "https://your-keyvault.vault.azure.net/"),
    "SECRET_NAME": "kimi-api-key",
    "AI_BASE_URL": "https://api.moonshot.ai/v1",
    "AI_MODEL_NAME": "kimi-k3",

    # DeepSeek
    "DEEPSEEK_KEY_VAULT_URL": os.environ.get("DEEPSEEK_KEY_VAULT_URL", "https://your-keyvault.vault.azure.net/"),
    "DEEPSEEK_SECRET_NAME": "deepseek-api-key",
    "DEEPSEEK_BASE_URL": "https://api.deepseek.com",
    "DEEPSEEK_MODEL_NAME": "deepseek-flash",
    # DeepSeek 视觉模型：仅当请求带图片时自动切换（可用环境变量覆盖）
    "DEEPSEEK_VISION_MODEL_NAME": os.environ.get("DEEPSEEK_VISION_MODEL_NAME", "deepseek-v4-flash-vision-exp"),


    "VNET_RG_MAPPING": {
        "example-fw-vnet": "ExampleFw",
        "example-mgmt-vnet": "ExampleMgmt"
    },

    "KNOWN_RESOURCE_GROUPS": ["api-test", "ExampleFw", "ExampleMgmt"]
}
#=====（修改内容结束）=====


# ========== JWT 认证配置（从环境变量读取） ==========
JWT_SECRET = os.environ.get("JWT_SECRET", "your-very-secret-key-change-it-in-production")
JWT_EXPIRATION_MINUTES = int(os.environ.get("JWT_EXPIRATION_MINUTES", 60 * 24))
ADMIN_USERNAME = os.environ.get("ADMIN_USERNAME", "admin")
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "password123")


# ========== Cosmos DB for Table 持久化配置 ==========
COSMOS_ENDPOINT = os.environ.get("COSMOS_ENDPOINT")
COSMOS_KEY = os.environ.get("COSMOS_KEY")
COSMOS_TABLE_NAME = os.environ.get("COSMOS_TABLE_NAME", "UsageRecords")
_table_service = None


#=====（修改内容：修复 Cosmos Table 初始化、写入、查询函数）=====
def _get_table_service():
    """延迟初始化 TableServiceClient"""
    global _table_service

    if _table_service is not None:
        return _table_service

    if not COSMOS_ENDPOINT or not COSMOS_KEY:
        logging.warning("Cosmos DB 环境变量未设置，用量将仅记录到内存")
        return None

    match = re.match(r"https://([^.]+)\.", COSMOS_ENDPOINT)
    if not match:
        logging.warning(f"无法从 COSMOS_ENDPOINT 解析账户名: {COSMOS_ENDPOINT}")
        return None

    account_name = match.group(1)
    cosmos_credential = AzureNamedKeyCredential(account_name, COSMOS_KEY)

    _table_service = TableServiceClient(
        endpoint=COSMOS_ENDPOINT,
        credential=cosmos_credential
    )

    try:
        _table_service.create_table_if_not_exists(COSMOS_TABLE_NAME)
        logging.info(f"Cosmos DB 表 {COSMOS_TABLE_NAME} 就绪")
    except Exception as e:
        logging.warning(f"Cosmos DB 表创建检查: {e}")

    return _table_service


def _persist_usage(provider, model, prompt_tokens, completion_tokens, total_tokens, endpoint, timestamp=None):
    """将用量记录写入 Cosmos DB Table"""
    try:
        provider = _normalize_provider(provider)
        ts = timestamp or datetime.datetime.utcnow().isoformat()

        svc = _get_table_service()
        if svc is None:
            return

        table_client = svc.get_table_client(COSMOS_TABLE_NAME)

        safe_ts = ts.replace(":", "-").replace(".", "-").replace("+", "-")

        entity = {
            "PartitionKey": provider,
            "RowKey": f"{safe_ts}-{uuid.uuid4().hex[:8]}",
            "EventTimestamp": ts,
            "Model": model or "unknown",
            "PromptTokens": int(prompt_tokens or 0),
            "CompletionTokens": int(completion_tokens or 0),
            "TotalTokens": int(total_tokens or 0),
            "Endpoint": endpoint or "unknown",
            "EstimatedCostUsd": round(_estimate_cost(provider, prompt_tokens, completion_tokens), 8),
        }

        table_client.create_entity(entity=entity)
        logging.info(f"用量记录已写入 Cosmos DB: {provider} {total_tokens} tokens")

    except Exception as e:
        logging.error(f"Cosmos DB 写入失败: {e}")


def _query_usage_records(provider, hours=24):
    """从 Cosmos DB 查询最近 N 小时的用量记录"""
    records = []

    try:
        provider = _normalize_provider(provider)
        svc = _get_table_service()
        if svc is None:
            return records

        table_client = svc.get_table_client(COSMOS_TABLE_NAME)
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
        cutoff_str = cutoff.isoformat()

        query_filter = f"PartitionKey eq '{provider}' and EventTimestamp ge '{cutoff_str}'"

        for entity in table_client.query_entities(query_filter):
            records.append({
                "timestamp": entity.get("EventTimestamp", ""),
                "model": entity.get("Model", "unknown"),
                "prompt_tokens": entity.get("PromptTokens", 0),
                "completion_tokens": entity.get("CompletionTokens", 0),
                "total_tokens": entity.get("TotalTokens", 0),
                "endpoint": entity.get("Endpoint", "unknown"),
            })

    except Exception as e:
        logging.error(f"Cosmos DB 查询失败: {e}")

    return records


def _query_usage_by_date(provider):
    """按日期汇总用量"""
    provider = _normalize_provider(provider)
    by_date = defaultdict(lambda: {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0})

    try:
        svc = _get_table_service()
        if svc is None:
            return {}

        table_client = svc.get_table_client(COSMOS_TABLE_NAME)

        for entity in table_client.query_entities(f"PartitionKey eq '{provider}'"):
            ts = entity.get("EventTimestamp", "")
            day = ts[:10] if ts else "unknown"

            by_date[day]["prompt_tokens"] += int(entity.get("PromptTokens", 0) or 0)
            by_date[day]["completion_tokens"] += int(entity.get("CompletionTokens", 0) or 0)
            by_date[day]["total_tokens"] += int(entity.get("TotalTokens", 0) or 0)

    except Exception as e:
        logging.error(f"Cosmos DB 汇总查询失败: {e}")
        return {}

    return dict(by_date)
#=====（修改内容结束）=====

# ========== 对话历史持久化（Cosmos DB Table）==========
CONVERSATION_TABLE_NAME = os.environ.get("CONVERSATION_TABLE_NAME", "ConversationHistory")


def _get_conversation_table_service():
    """获取对话历史表客户端（独立初始化，不依赖全局 _table_service）"""
    if not COSMOS_ENDPOINT or not COSMOS_KEY:
        logging.warning("Cosmos DB 环境变量未设置，对话历史将仅记录到内存")
        return None

    match = re.match(r"https://([^.]+)\.", COSMOS_ENDPOINT)
    if not match:
        logging.warning(f"无法从 COSMOS_ENDPOINT 解析账户名: {COSMOS_ENDPOINT}")
        return None

    account_name = match.group(1)
    cosmos_credential = AzureNamedKeyCredential(account_name, COSMOS_KEY)

    svc = TableServiceClient(endpoint=COSMOS_ENDPOINT, credential=cosmos_credential)

    try:
        svc.create_table_if_not_exists(CONVERSATION_TABLE_NAME)
        logging.info(f"Cosmos DB 对话历史表 {CONVERSATION_TABLE_NAME} 就绪")
    except Exception as e:
        logging.warning(f"Cosmos DB 对话历史表创建检查: {e}")

    return svc


def _clean_messages_for_storage(messages: list) -> list:
    """
    清理消息列表，只保留可 JSON 序列化的 role/content 字段。
    丢弃不可序列化的 tool_calls 对象和 tool 角色消息（中间过程不需要持久化）。
    """
    cleaned = []
    for msg in messages:
        if not isinstance(msg, dict):
            continue
        role = msg.get("role")
        # 只保留 user 和 assistant 的最终回复，跳过 tool 和带 tool_calls 的 assistant
        if role not in ("user", "assistant", "system"):
            continue
        # 多模态 content（含 base64 图片）压平为纯文本，防止 Cosmos DB 单字段超限
        content = _flatten_content_to_text(msg.get("content"))
        # 注入了文件全文的 user 消息可能非常长，截断保存（Cosmos Table 单字段上限 64KB）
        if role == "user" and isinstance(content, str) and len(content) > 2000:
            content = (
                content[:800]
                + f"\n……[原文共 {len(content)} 字符，中间部分已省略]……\n"
                + content[-800:]
            )
        clean_msg = {"role": role, "content": content}
        # 保留 system prompt
        if role == "system" or role == "user":
            cleaned.append(clean_msg)
        elif role == "assistant":
            # 跳过只包含 tool_calls 而没有 content 的 assistant 消息
            if not content and msg.get("tool_calls"):
                continue
            cleaned.append(clean_msg)
    return cleaned


def _save_conversation(conversation_id: str, messages: list):
    """保存对话历史到 Cosmos DB Table"""
    try:
        if not conversation_id:
            logging.warning("[Conversation] 保存失败: conversation_id 为空")
            return

        svc = _get_conversation_table_service()
        if svc is None:
            logging.warning("[Conversation] 保存失败: 无法连接到 Cosmos DB")
            return

        table_client = svc.get_table_client(CONVERSATION_TABLE_NAME)

        # 清理消息（移除不可序列化的 tool_calls 对象）
        cleaned = _clean_messages_for_storage(messages)

        # 只保留最近 20 轮（40 条消息）防止超限
        trimmed = cleaned[-40:] if len(cleaned) > 40 else cleaned

        # 标题：优先沿用已有标题；新对话取首条用户消息（截断 30 字）
        title = None
        try:
            existing = table_client.get_entity(partition_key="conversation", row_key=conversation_id)
            title = existing.get("Title")
        except Exception:
            title = None

        if not title:
            for m in cleaned:
                if m.get("role") == "user" and m.get("content"):
                    title = str(m["content"]).replace("\n", " ").strip()[:30]
                    break
        if not title:
            title = "新对话"

        entity = {
            "PartitionKey": "conversation",
            "RowKey": conversation_id,
            "Messages": json.dumps(trimmed, ensure_ascii=False),
            "UpdatedAt": datetime.datetime.utcnow().isoformat(),
            "MessageCount": len(trimmed),
            "Title": title
        }
        table_client.upsert_entity(entity=entity)
        logging.info(f"[Conversation] 历史已保存: {conversation_id}, {len(trimmed)} 条消息")

    except Exception as e:
        logging.error(f"[Conversation] 保存历史失败: {e}")


def _load_conversation(conversation_id: str) -> list:
    """从 Cosmos DB Table 加载对话历史"""
    try:
        if not conversation_id:
            return []

        svc = _get_conversation_table_service()
        if svc is None:
            logging.warning("加载对话历史失败: 无法连接到 Cosmos DB")
            return []

        table_client = svc.get_table_client(CONVERSATION_TABLE_NAME)
        entity = table_client.get_entity(partition_key="conversation", row_key=conversation_id)
        messages = json.loads(entity.get("Messages", "[]"))
        logging.info(f"[Conversation] 历史已加载: {conversation_id}, {len(messages)} 条消息")
        return messages

    except Exception as e:
        logging.info(f"[Conversation] 加载历史失败（可能是新对话）: {e}")
        return []


#=====（修改内容：内存回退记录）=====
kimi_usage_records = []
deepseek_usage_records = []
#=====（修改内容结束）=====


#=====（修改内容：新增通用 Provider 内存记录辅助函数）=====
def _get_memory_records(provider: str):
    provider = _normalize_provider(provider)

    if provider == "deepseek":
        return deepseek_usage_records
    return kimi_usage_records


def _append_memory_record(provider: str, record: dict):
    provider = _normalize_provider(provider)
    records = _get_memory_records(provider)
    records.append(record)

    # 防止内存记录无限增长
    if len(records) > 5000:
        records.pop(0)


def _usage_to_dict(usage_obj):
    if not usage_obj:
        return None

    if hasattr(usage_obj, "model_dump"):
        try:
            return usage_obj.model_dump()
        except Exception:
            pass

    return {
        "prompt_tokens": getattr(usage_obj, "prompt_tokens", None),
        "completion_tokens": getattr(usage_obj, "completion_tokens", None),
        "total_tokens": getattr(usage_obj, "total_tokens", None),
    }


def _build_usage_summary(provider: str, hours: int = 24) -> str:
    provider = _normalize_provider(provider)

    recent = _query_usage_records(provider, hours)

    if not recent:
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
        memory_records = _get_memory_records(provider)
        recent = [r for r in memory_records if r.get("timestamp", "") >= cutoff.isoformat()]

    total_requests = len(recent)
    total_prompt = sum(int(r.get("prompt_tokens", 0) or 0) for r in recent)
    total_completion = sum(int(r.get("completion_tokens", 0) or 0) for r in recent)
    total_tokens = sum(int(r.get("total_tokens", 0) or 0) for r in recent)

    by_model = {}

    for r in recent:
        m = r.get("model", "unknown")
        by_model.setdefault(m, {"requests": 0, "tokens": 0})
        by_model[m]["requests"] += 1
        by_model[m]["tokens"] += int(r.get("total_tokens", 0) or 0)

    return json.dumps(
        {
            "status": "success",
            "provider": provider,
            "period_hours": hours,
            "total_requests": total_requests,
            "total_prompt_tokens": total_prompt,
            "total_completion_tokens": total_completion,
            "total_tokens": total_tokens,
            "by_model": by_model,
            "records_count": len(recent),
        },
        ensure_ascii=False
    )


def _build_api_usage_response(provider: str) -> str:
    provider = _normalize_provider(provider)

    by_date = _query_usage_by_date(provider)

    total_prompt = sum(int(d.get("prompt_tokens", 0) or 0) for d in by_date.values())
    total_completion = sum(int(d.get("completion_tokens", 0) or 0) for d in by_date.values())
    total_all = sum(int(d.get("total_tokens", 0) or 0) for d in by_date.values())

    est_cost = _estimate_cost(provider, total_prompt, total_completion)

    initial_balances = {
        "kimi": KIMI_INITIAL_BALANCE,
        "deepseek": DEEPSEEK_INITIAL_BALANCE,
    }

    initial_balance = initial_balances.get(provider, 0.0)
    remaining = initial_balance - est_cost if initial_balance > 0 else None

    return json.dumps(
        {
            "status": "success",
            "provider": provider,
            "usage_by_date": by_date,
            "total_tokens": {
                "prompt": total_prompt,
                "completion": total_completion,
                "total": total_all,
            },
            "estimated_cost_usd": round(est_cost, 6),
            "initial_balance_usd": initial_balance if initial_balance > 0 else "not set",
            "remaining_balance_usd": round(remaining, 6) if remaining is not None else "N/A",
            "note": "数据来自 Cosmos DB 持久化存储。实际余额请以对应 API 控制台为准。",
        },
        ensure_ascii=False
    )
#=====（修改内容结束）=====


#=====（修改内容：新增/修复 Kimi、DeepSeek、API Key 与请求函数）=====
def _get_kimi_api_key() -> str:
    """从 Key Vault 获取 Kimi API Key"""
    client = SecretClient(vault_url=CONFIG["KEY_VAULT_URL"], credential=credential)
    return client.get_secret(CONFIG["SECRET_NAME"]).value


def _get_deepseek_api_key() -> str:
    """从 Key Vault 获取 DeepSeek API Key"""
    client = SecretClient(vault_url=CONFIG["DEEPSEEK_KEY_VAULT_URL"], credential=credential)
    return client.get_secret(CONFIG["DEEPSEEK_SECRET_NAME"]).value


def _kimi_api_request(method: str, endpoint: str, json_data=None) -> dict:
    """向 Kimi API 发送请求"""
    api_key = _get_kimi_api_key()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json"
    }

    base_url = CONFIG["AI_BASE_URL"].rstrip("/")
    url = endpoint if endpoint.startswith("http") else f"{base_url}{endpoint}"

    if method.upper() == "GET":
        resp = httpx.get(url, headers=headers, timeout=30)
    elif method.upper() == "POST":
        resp = httpx.post(url, headers=headers, json=json_data, timeout=30)
    else:
        raise ValueError(f"Unsupported method: {method}")

    resp.raise_for_status()
    return resp.json()


def _deepseek_api_request(method: str, endpoint: str, json_data=None) -> dict:
    """向 DeepSeek API 发送请求"""
    api_key = _get_deepseek_api_key()

    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
        "Accept": "application/json"
    }

    base_url = CONFIG["DEEPSEEK_BASE_URL"].rstrip("/")
    url = endpoint if endpoint.startswith("http") else f"{base_url}{endpoint}"

    if method.upper() == "GET":
        resp = httpx.get(url, headers=headers, timeout=30)
    elif method.upper() == "POST":
        resp = httpx.post(url, headers=headers, json=json_data, timeout=30)
    else:
        raise ValueError(f"Unsupported method: {method}")

    resp.raise_for_status()
    return resp.json()




#=====（修改内容：工具执行函数，并修复 Kimi / DeepSeek 工具执行函数）=====
def execute_kimi_tool(tool_name: str, arguments: dict) -> str:
    try:
        if tool_name == "kimi_get_balance":
            data = _kimi_api_request("GET", "/users/me/balance")
            bal = data.get("data", data) if isinstance(data, dict) else {}

            return json.dumps(
                {
                    "status": "success",
                    "provider": "kimi",
                    "available_balance": bal.get("available_balance"),
                    "voucher_balance": bal.get("voucher_balance"),
                    "cash_balance": bal.get("cash_balance"),
                    "currency": bal.get("currency", "USD"),
                    "raw": data,
                },
                ensure_ascii=False
            )

        elif tool_name == "kimi_estimate_tokens":
            payload = {
                "messages": arguments["messages"],
                "model": arguments.get("model", CONFIG["AI_MODEL_NAME"])
            }

            data = _kimi_api_request("POST", "/tokens/estimate", json_data=payload)

            return json.dumps(
                {
                    "status": "success",
                    "provider": "kimi",
                    "estimated_tokens": data.get("data", {}).get("total_tokens") if isinstance(data.get("data"), dict) else None,
                    "detail": data,
                },
                ensure_ascii=False
            )

        elif tool_name == "kimi_get_usage_summary":
            hours = int(arguments.get("hours", 24))
            return _build_usage_summary("kimi", hours)

        return json.dumps({"status": "error", "message": f"Unknown Kimi tool: {tool_name}"})

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})


def execute_deepseek_tool(tool_name: str, arguments: dict) -> str:
    try:
        if tool_name == "deepseek_get_balance":
            data = _deepseek_api_request("GET", "/user/balance")

            return json.dumps(
                {
                    "status": "success",
                    "provider": "deepseek",
                    "is_available": data.get("is_available"),
                    "balance_infos": data.get("balance_infos", []),
                    "raw": data,
                },
                ensure_ascii=False
            )

        elif tool_name == "deepseek_estimate_tokens":
            messages = arguments.get("messages", [])
            total_chars = 0

            for msg in messages:
                content = msg.get("content", "")
                if isinstance(content, str):
                    total_chars += len(content)
                else:
                    total_chars += len(json.dumps(content, ensure_ascii=False))

            estimated = int(total_chars * 0.5)

            return json.dumps(
                {
                    "status": "success",
                    "provider": "deepseek",
                    "estimated_tokens": estimated,
                    "note": "此为粗略估算。精确值请调用 deepseek_chat 后查看 usage。",
                },
                ensure_ascii=False
            )

        elif tool_name == "deepseek_get_usage_summary":
            hours = int(arguments.get("hours", 24))
            return _build_usage_summary("deepseek", hours)

        return json.dumps({"status": "error", "message": f"Unknown DeepSeek tool: {tool_name}"})

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})



#=====（修改内容：修复 azure_tools schema 尾随空格）=====
azure_tools = [
    {
        "type": "function",
        "function": {
            "name": "list_all_resource_groups",
            "description": "列出当前订阅中的所有资源组及其位置",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_vnets_in_resource_group",
            "description": "列出指定资源组中的所有虚拟网络",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"}
                },
                "required": ["resource_group_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_vnet_details",
            "description": "获取指定虚拟网络的详细信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "vnet_name": {"type": "string", "description": "虚拟网络名称"}
                },
                "required": ["resource_group_name", "vnet_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "check_subnets_without_nsg",
            "description": "检查没有关联NSG的子网",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"}
                },
                "required": ["resource_group_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_subnet_info",
            "description": "获取子网详细信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "vnet_name": {"type": "string", "description": "虚拟网络名称"},
                    "subnet_name": {"type": "string", "description": "子网名称"}
                },
                "required": ["resource_group_name", "vnet_name", "subnet_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_nsgs_in_resource_group",
            "description": "列出所有NSG",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"}
                },
                "required": ["resource_group_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_virtual_machines",
            "description": "列出指定资源组中的虚拟机及其电源状态（Running/Stopped等）",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"}
                },
                "required": ["resource_group_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_vm_cpu_metric",
            "description": "获取指定虚拟机最近5分钟的平均CPU使用率（百分比）",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "vm_name": {"type": "string", "description": "虚拟机名称"}
                },
                "required": ["resource_group_name", "vm_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_storage_accounts",
            "description": "列出指定资源组中的所有存储账户",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"}
                },
                "required": ["resource_group_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_web_apps",
            "description": "列出指定资源组中的所有 Web 应用 (App Service)",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"}
                },
                "required": ["resource_group_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_sql_servers",
            "description": "列出指定资源组中的 SQL 服务器",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"}
                },
                "required": ["resource_group_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_keyvaults",
            "description": "列出指定资源组中的 Key Vault 实例",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"}
                },
                "required": ["resource_group_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_aks_clusters",
            "description": "列出指定资源组中的 AKS Kubernetes 集群",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"}
                },
                "required": ["resource_group_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_container_instances",
            "description": "列出指定资源组中的容器实例 (ACI)",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"}
                },
                "required": ["resource_group_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "query_resources",
            "description": "使用 Kusto 查询语言 (KQL) 跨资源组、跨区域查询 Azure 资源。示例：resources | where type =~ 'Microsoft.Compute/virtualMachines' | project name, location",
            "parameters": {
                "type": "object",
                "properties": {
                    "kql_query": {"type": "string", "description": "KQL 查询语句"}
                },
                "required": ["kql_query"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "list_subscriptions",
            "description": "列出当前账号可访问的所有订阅",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_webapp_metrics",
            "description": "获取 Web App (App Service) 的关键监控指标。默认查询最近1小时，5分钟粒度。",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "webapp_name": {"type": "string", "description": "Web App 名称"},
                    "metric_names": {"type": "string", "description": "指标名称，逗号分隔。常用: Requests,AverageResponseTime,Http2xx,Http3xx,Http4xx,Http5xx,BytesReceived,BytesSent,MemoryWorkingSet,CpuTime"},
                    "timespan": {"type": "string", "description": "时间范围，如 PT1H(1小时), PT24H(24小时), P7D(7天)"},
                    "interval": {"type": "string", "description": "聚合间隔，如 PT5M, PT1H, P1D"},
                    "aggregation": {"type": "string", "description": "聚合方式，如 Average,Total,Count 或组合"}
                },
                "required": ["resource_group_name", "webapp_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_network_metrics",
            "description": "获取网络资源的监控指标。支持 NSG、Load Balancer、Application Gateway、VNet Gateway、Public IP、Firewall、Front Door、CDN 等。默认查询最近1小时。",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "resource_name": {"type": "string", "description": "网络资源名称"},
                    "resource_type": {"type": "string", "description": "资源类型: nsg, loadbalancer, applicationgateway, vnetgateway, publicip, firewall, frontdoor, cdn"},
                    "metric_names": {"type": "string", "description": "指标名称，逗号分隔。如 NSG默认 PacketCount,ByteCount"},
                    "timespan": {"type": "string", "description": "时间范围，如 PT1H, P1D"},
                    "interval": {"type": "string", "description": "聚合间隔，如 PT5M, PT1H"},
                    "aggregation": {"type": "string", "description": "聚合方式，如 Average,Total,Count"}
                },
                "required": ["resource_group_name", "resource_name", "resource_type"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_app_insights_metrics",
            "description": "获取 Application Insights 组件的监控指标，如请求数、异常数、依赖项耗时、页面浏览等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "app_insights_name": {"type": "string", "description": "Application Insights 名称"},
                    "metric_names": {"type": "string", "description": "指标名称，逗号分隔。常用: requests/count,exceptions/count,dependencies/duration,pageViews/count,requests/duration"},
                    "timespan": {"type": "string", "description": "时间范围，如 PT1H, P1D"},
                    "interval": {"type": "string", "description": "聚合间隔，如 PT5M, PT1H"},
                    "aggregation": {"type": "string", "description": "聚合方式，如 Average,Total,Count"}
                },
                "required": ["resource_group_name", "app_insights_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_resource_metrics",
            "description": "通用指标查询工具，可查询任何 Azure 资源的监控指标。需要先知道 resource_provider 格式，如 Microsoft.Web/sites, Microsoft.Compute/virtualMachines, Microsoft.Sql/servers 等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_provider": {"type": "string", "description": "资源提供程序，如 Microsoft.Web/sites, Microsoft.Compute/virtualMachines, Microsoft.Network/networkSecurityGroups"},
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "resource_name": {"type": "string", "description": "资源名称"},
                    "metric_names": {"type": "string", "description": "指标名称，逗号分隔。为空则返回所有可用指标"},
                    "timespan": {"type": "string", "description": "时间范围，如 PT1H, P1D, P7D"},
                    "interval": {"type": "string", "description": "聚合间隔，如 PT5M, PT1H"},
                    "aggregation": {"type": "string", "description": "聚合方式，如 Average,Total,Count,Minimum,Maximum"}
                },
                "required": ["resource_provider", "resource_group_name", "resource_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_resource_metric_definitions",
            "description": "列出指定资源支持的所有监控指标名称和单位，帮助确定可查询的指标。适用于任何 Azure 资源类型。",
            "parameters": {
                "type": "object",
                "properties": {
                    "resource_provider": {"type": "string", "description": "资源提供程序，如 Microsoft.Web/sites, Microsoft.Compute/virtualMachines"},
                    "resource_group_name": {"type": "string", "description": "资源组名称"},
                    "resource_name": {"type": "string", "description": "资源名称"}
                },
                "required": ["resource_provider", "resource_group_name", "resource_name"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "kimi_get_balance",
            "description": "查询当前 Kimi API Key 的账户余额，包括可用余额、代金券余额和现金余额。",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "kimi_estimate_tokens",
            "description": "预估一组消息将消耗的 Kimi Token 数量，用于在调用前估算成本。",
            "parameters": {
                "type": "object",
                "properties": {
                    "messages": {"type": "array", "description": "消息列表，格式同 ChatCompletion messages"},
                    "model": {"type": "string", "description": "模型名称，如 kimi-k3"}
                },
                "required": ["messages"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "kimi_get_usage_summary",
            "description": "获取最近记录的 Kimi API 用量汇总，包括总请求数、总 token 数、按模型统计等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "description": "查询最近多少小时内的记录，默认 24"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "deepseek_get_balance",
            "description": "查询当前 DeepSeek API Key 的账户余额，包括是否有可用余额及各币种余额详情。",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "deepseek_estimate_tokens",
            "description": "预估一组消息在 DeepSeek 模型上将消耗的 Token 数量，用于调用前估算成本。",
            "parameters": {
                "type": "object",
                "properties": {
                    "messages": {"type": "array", "description": "消息列表，格式同 ChatCompletion messages"},
                    "model": {"type": "string", "description": "模型名称，如 deepseek-v4-pro"}
                },
                "required": ["messages"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "deepseek_get_usage_summary",
            "description": "获取最近记录的 DeepSeek API 用量汇总，包括总请求数、总 token 数、按模型统计等。",
            "parameters": {
                "type": "object",
                "properties": {
                    "hours": {"type": "integer", "description": "查询最近多少小时内的记录，默认 24"}
                },
                "required": []
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "get_api_usage",
            "description": "查询指定 AI Provider (/kimi/deepseek) 的累计 token 使用量、预估费用和剩余余额（基于本地 Cosmos DB 记录）。",
            "parameters": {
                "type": "object",
                "properties": {
                    "provider": {"type": "string", "description": "AI Provider：kimi / deepseek"}
                },
                "required": []
            }
        }
    },
    # Meraki 查询工具
    {
        "type": "function",
        "function": {
            "name": "meraki_list_organizations",
            "description": "列出 Cisco Meraki Dashboard 中所有可访问的组织（Organization）列表",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_list_networks",
            "description": "列出指定 Meraki 组织下的所有网络（Network）",
            "parameters": {
                "type": "object",
                "properties": {
                    "org_id": {"type": "string", "description": "Meraki 组织 ID"}
                },
                "required": ["org_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_list_devices",
            "description": "列出指定 Meraki 网络下的所有设备（包括 vMX、MX、AP、Switch 等）",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID，格式如 N_1234567890"}
                },
                "required": ["network_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_vpn_status",
            "description": "获取指定 Meraki 网络的 VPN 状态（适用于 vMX/MX 网络）",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_device_uplink",
            "description": "获取指定 Meraki 设备的上行链路状态",
            "parameters": {
                "type": "object",
                "properties": {
                    "serial": {"type": "string", "description": "设备序列号，如 QBSB-VQ3J-XZ54"}
                },
                "required": ["serial"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_vmx_licenses",
            "description": "获取指定组织中所有 vMX 相关的许可证信息",
            "parameters": {
                "type": "object",
                "properties": {
                    "org_id": {"type": "string", "description": "Meraki 组织 ID"}
                },
                "required": ["org_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_network_overview",
            "description": "获取 Meraki 网络全景信息：网络详情 + 设备列表 + VPN 状态（一站式查询）",
            "parameters": {
                "type": "object",
                "properties": {
                    "org_id": {"type": "string", "description": "Meraki 组织 ID"},
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["org_id", "network_id"]
            }
        }
    },
        # ===== Appliance 网络基础 =====
    {
        "type": "function",
        "function": {
            "name": "meraki_get_appliance_settings",
            "description": "获取指定 Meraki 网络（MX/Z）的 Appliance 设置",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_appliance_vlans",
            "description": "列出指定 Meraki 网络（MX/Z）的所有 VLAN",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_appliance_vlan",
            "description": "获取指定 Meraki 网络的某个 VLAN 详情",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"},
                    "vlan_id": {"type": "string", "description": "VLAN ID"}
                },
                "required": ["network_id", "vlan_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_appliance_ports",
            "description": "列出 MX 安全设备所有端口的 VLAN 设置",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_appliance_port",
            "description": "获取 MX 安全设备单个端口的 VLAN 设置",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"},
                    "port_id": {"type": "string", "description": "端口 ID"}
                },
                "required": ["network_id", "port_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_appliance_static_routes",
            "description": "列出 MX 网络的静态路由",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_appliance_single_lan",
            "description": "获取 MX 网络的 Single LAN 配置",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    # ===== Appliance 防火墙 =====
    {
        "type": "function",
        "function": {
            "name": "meraki_get_firewall_l3_rules",
            "description": "获取 MX 网络的 L3 防火墙规则",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_firewall_l7_rules",
            "description": "获取 MX 网络的 L7 防火墙规则",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_firewall_1to1_nat",
            "description": "获取 MX 网络的 1:1 NAT 规则",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_firewall_1toMany_nat",
            "description": "获取 MX 网络的 1:Many NAT 规则",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_firewall_port_forwarding",
            "description": "获取 MX 网络的端口转发规则",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    # ===== Appliance 安全服务 =====
    {
        "type": "function",
        "function": {
            "name": "meraki_get_security_intrusion",
            "description": "获取 MX 网络的入侵防护设置",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_security_malware",
            "description": "获取 MX 网络的恶意软件防护设置",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    # ===== Appliance 内容过滤 =====
    {
        "type": "function",
        "function": {
            "name": "meraki_get_content_filtering",
            "description": "获取 MX 网络的内容过滤设置",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    # ===== Appliance VPN =====
    {
        "type": "function",
        "function": {
            "name": "meraki_get_vpn_site_to_site",
            "description": "获取 MX 网络的 Site-to-Site VPN 设置",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_vpn_bgp",
            "description": "获取 MX 网络的 VPN BGP 配置",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    # ===== Appliance 流量整形 / SD-WAN =====
    {
        "type": "function",
        "function": {
            "name": "meraki_get_traffic_shaping",
            "description": "获取 MX 网络的流量整形设置",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_traffic_shaping_rules",
            "description": "获取 MX 网络的流量整形规则",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_traffic_shaping_uplink_bandwidth",
            "description": "获取 MX 网络的上行带宽设置",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_traffic_shaping_uplink_selection",
            "description": "获取 MX 网络的上行选择（SD-WAN）设置",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    # ===== Appliance Uplink =====
    {
        "type": "function",
        "function": {
            "name": "meraki_get_device_appliance_uplinks_settings",
            "description": "获取指定 MX 设备的上行链路设置",
            "parameters": {
                "type": "object",
                "properties": {
                    "serial": {"type": "string", "description": "设备序列号"}
                },
                "required": ["serial"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_org_appliance_uplink_statuses",
            "description": "列出组织中所有 MX/Z 设备的上行状态",
            "parameters": {
                "type": "object",
                "properties": {
                    "org_id": {"type": "string", "description": "Meraki 组织 ID"}
                },
                "required": ["org_id"]
            }
        }
    },
    # ===== Appliance 高可用 =====
    {
        "type": "function",
        "function": {
            "name": "meraki_get_warm_spare",
            "description": "获取 MX 网络的 Warm Spare（高可用）设置",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    # ===== Appliance SSID / RF（MX 无线功能） =====
    {
        "type": "function",
        "function": {
            "name": "meraki_get_appliance_ssids",
            "description": "列出 MX 网络的所有 SSID（MX 内置无线功能）",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_appliance_ssid",
            "description": "获取 MX 网络的某个 SSID 详情",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"},
                    "ssid_number": {"type": "string", "description": "SSID 编号，如 0, 1, 2..."}
                },
                "required": ["network_id", "ssid_number"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_appliance_rf_profiles",
            "description": "列出 MX 网络的所有 RF 配置文件",
            "parameters": {
                "type": "object",
                "properties": {
                    "network_id": {"type": "string", "description": "Meraki 网络 ID"}
                },
                "required": ["network_id"]
            }
        }
    },
    # ===== 组织级 =====
    {
        "type": "function",
        "function": {
            "name": "meraki_get_org_appliance_vlans",
            "description": "列出组织中所有 MX 网络的 VLAN（组织级视图）",
            "parameters": {
                "type": "object",
                "properties": {
                    "org_id": {"type": "string", "description": "Meraki 组织 ID"}
                },
                "required": ["org_id"]
            }
        }
    },
        # ===== Switch 端口配置与状态 =====
    {
        "type": "function",
        "function": {
            "name": "meraki_get_device_switch_ports",
            "description": "列出某台 Meraki 交换机的所有端口配置",
            "parameters": {
                "type": "object",
                "properties": {
                    "serial": {"type": "string", "description": "交换机设备序列号，如 QBSB-VQ3J-XZ54"}
                },
                "required": ["serial"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_device_switch_port",
            "description": "返回某台 Meraki 交换机的单个端口配置",
            "parameters": {
                "type": "object",
                "properties": {
                    "serial": {"type": "string", "description": "交换机设备序列号"},
                    "port_id": {"type": "string", "description": "端口 ID，如 1, 2, 3..."}
                },
                "required": ["serial", "port_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_device_switch_ports_statuses",
            "description": "返回某台 Meraki 交换机所有端口的实时状态",
            "parameters": {
                "type": "object",
                "properties": {
                    "serial": {"type": "string", "description": "交换机设备序列号"}
                },
                "required": ["serial"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_device_switch_ports_statuses_packets",
            "description": "返回某台 Meraki 交换机所有端口的包计数器",
            "parameters": {
                "type": "object",
                "properties": {
                    "serial": {"type": "string", "description": "交换机设备序列号"}
                },
                "required": ["serial"]
            }
        }
    },
    # ===== 组织级 Switch 端口 =====
    {
        "type": "function",
        "function": {
            "name": "meraki_get_org_switch_ports_by_switch",
            "description": "按交换机列出组织内所有端口配置",
            "parameters": {
                "type": "object",
                "properties": {
                    "org_id": {"type": "string", "description": "Meraki 组织 ID"}
                },
                "required": ["org_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_org_switch_ports_statuses_by_switch",
            "description": "按交换机列出组织内所有端口状态",
            "parameters": {
                "type": "object",
                "properties": {
                    "org_id": {"type": "string", "description": "Meraki 组织 ID"}
                },
                "required": ["org_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_org_switch_ports_statuses_packets_by_device_by_port",
            "description": "按设备和端口列出组织内所有端口的包计数器",
            "parameters": {
                "type": "object",
                "properties": {
                    "org_id": {"type": "string", "description": "Meraki 组织 ID"}
                },
                "required": ["org_id"]
            }
        }
    },
    # ===== Switch 端口镜像 =====
    {
        "type": "function",
        "function": {
            "name": "meraki_get_org_switch_ports_mirrors_by_switch",
            "description": "按交换机列出组织内的端口镜像配置",
            "parameters": {
                "type": "object",
                "properties": {
                    "org_id": {"type": "string", "description": "Meraki 组织 ID"}
                },
                "required": ["org_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_org_switch_stacks_ports_mirrors_by_stack",
            "description": "按堆叠列出组织内的端口镜像配置",
            "parameters": {
                "type": "object",
                "properties": {
                    "org_id": {"type": "string", "description": "Meraki 组织 ID"}
                },
                "required": ["org_id"]
            }
        }
    },
    {
        "type": "function",
        "function": {
            "name": "meraki_get_org_config_templates_switch_profiles_ports_mirrors_by_switch_profile",
            "description": "按模板配置文件列出端口镜像配置",
            "parameters": {
                "type": "object",
                "properties": {
                    "org_id": {"type": "string", "description": "Meraki 组织 ID"},
                    "config_template_id": {"type": "string", "description": "配置模板 ID"},
                    "profile_id": {"type": "string", "description": "Switch Profile ID"}
                },
                "required": ["org_id", "config_template_id", "profile_id"]
            }
        }
    },
    # ===== Ansible Container tools =====
{
        "type": "function",
        "function": {
            "name": "ansible_meraki_list_organizations",
            "description": "通过 Ansible playbook 查询 Meraki Dashboard 组织列表（与 meraki_list_organizations 直连方式等价，但经由 Ansible 容器执行）",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ansible_meraki_list_networks",
            "description": "通过 Ansible playbook 查询指定 Meraki 组织下的网络列表",
            "parameters": {
                "type": "object",
                "properties": {
                    "org_id": {"type": "string", "description": "Meraki 组织 ID"}
                },
                "required": ["org_id"]
            }
        }
    },
    # ===== 通用 Ansible 任务透传（容器内置任务，后续新功能无需改本文件）=====
    {
        "type": "function",
        "function": {
            "name": "ansible_list_tasks",
            "description": "列出 Ansible 容器当前支持的所有内置运维任务及其必填参数（如读取设备接口配置）。调用 ansible_run_task 前先调用本工具发现可用任务。",
            "parameters": {"type": "object", "properties": {}, "required": []}
        }
    },
    {
        "type": "function",
        "function": {
            "name": "ansible_run_task",
            "description": "在 Ansible 容器中执行一个内置运维任务（如 meraki_get_switch_ports 读取 Meraki 交换机端口配置）。任务名和必填参数请先通过 ansible_list_tasks 查询。",
            "parameters": {
                "type": "object",
                "properties": {
                    "task": {"type": "string", "description": "任务名，如 meraki_get_switch_ports"},
                    "params": {"type": "object", "description": "任务参数字典，如 {\"serial\": \"Q2XX-XXXX-XXXX\"}"}
                },
                "required": ["task"]
            }
        }
    }
]
#=====（修改内容结束）=====

def _get_credential_for_subscription(subscription_id: str | None = None):
    """
    根据 subscription_id 自动选择凭据：
    - Tenant B (SP) 的订阅 → 使用 ClientSecretCredential (SP)
    - 其他订阅 → 使用 DefaultAzureCredential (Tenant A Managed Identity)
    """
    if not subscription_id:
        return credential  # 全局 DefaultAzureCredential

    secondary_sub_id = os.environ.get("SUBSCRIPTION_SECONDARY_ID")

    if subscription_id == secondary_sub_id:
        tenant_id = os.environ.get("TENANT_SECONDARY_ID")
        client_id = os.environ.get("SP_CLIENT_ID")
        client_secret = os.environ.get("SP_CLIENT_SECRET")

        if all([tenant_id, client_id, client_secret]):
            logging.info(f"Using Tenant B SP credential for subscription {subscription_id}")
            return ClientSecretCredential(
                tenant_id=tenant_id,
                client_id=client_id,
                client_secret=client_secret
            )
        else:
            logging.warning("Tenant B credentials incomplete, falling back to default credential")

    return credential


#=====（修改内容：修复 execute_azure_tool，支持动态订阅、工具、通用 get_api_usage）=====
def execute_azure_tool(tool_name: str, arguments: dict) -> str:
    try:
        default_sub_id = CONFIG["SUBSCRIPTION_ID"]
        target_sub_id = arguments.get("subscription_id", default_sub_id)

        # 根据目标订阅自动选择凭据（Tenant A 或 Tenant B）
        tool_credential = _get_credential_for_subscription(target_sub_id)

        network_client = NetworkManagementClient(tool_credential, target_sub_id)
        resource_client = ResourceManagementClient(tool_credential, target_sub_id)
        compute_client = ComputeManagementClient(tool_credential, target_sub_id)
        monitor_client = MonitorManagementClient(tool_credential, target_sub_id)
        storage_client = StorageManagementClient(tool_credential, target_sub_id)
        web_client = WebSiteManagementClient(tool_credential, target_sub_id)
        sql_client = SqlManagementClient(tool_credential, target_sub_id)
        kv_client = KeyVaultManagementClient(tool_credential, target_sub_id)
        aks_client = ContainerServiceClient(tool_credential, target_sub_id)
        aci_client = ContainerInstanceManagementClient(tool_credential, target_sub_id)
        rg_client = ResourceGraphClient(tool_credential)

        if tool_name == "list_all_resource_groups":
            rgs = list(resource_client.resource_groups.list())
            return json.dumps({
                "status": "success",
                "subscription_id": target_sub_id,
                "resource_groups": [{"name": r.name, "location": r.location} for r in rgs]
            })

        elif tool_name == "list_vnets_in_resource_group":
            vnets = list(network_client.virtual_networks.list(arguments["resource_group_name"]))
            return json.dumps({"status": "success", "vnets": [v.name for v in vnets]})

        elif tool_name == "get_vnet_details":
            vnet = network_client.virtual_networks.get(arguments["resource_group_name"], arguments["vnet_name"])
            return json.dumps({"status": "success", "vnet": vnet.name})

        elif tool_name == "check_subnets_without_nsg":
            rg_name = arguments["resource_group_name"]
            risky = []

            for vnet in network_client.virtual_networks.list(rg_name):
                for subnet in (vnet.subnets or []):
                    if not subnet.network_security_group:
                        risky.append({"vnet": vnet.name, "subnet": subnet.name})

            return json.dumps({"status": "success", "unprotected": risky})

        elif tool_name == "get_subnet_info":
            subnet = network_client.subnets.get(
                arguments["resource_group_name"],
                arguments["vnet_name"],
                arguments["subnet_name"]
            )
            return json.dumps({"status": "success", "subnet": subnet.name})

        elif tool_name == "list_nsgs_in_resource_group":
            nsgs = list(network_client.network_security_groups.list(arguments["resource_group_name"]))
            return json.dumps({"status": "success", "nsgs": [n.name for n in nsgs]})

        elif tool_name == "list_virtual_machines":
            vms = list(compute_client.virtual_machines.list(arguments["resource_group_name"]))
            result = []

            for vm in vms:
                instance_view = compute_client.virtual_machines.instance_view(
                    arguments["resource_group_name"],
                    vm.name
                )

                status = "Unknown"
                for s in instance_view.statuses:
                    if s.code.startswith("PowerState/"):
                        status = s.code.split("/")[1]
                        break

                result.append({"name": vm.name, "status": status})

            return json.dumps({"status": "success", "vms": result})

        elif tool_name == "get_vm_cpu_metric":
            resource_id = (
                f"/subscriptions/{target_sub_id}/resourceGroups/{arguments['resource_group_name']}"
                f"/providers/Microsoft.Compute/virtualMachines/{arguments['vm_name']}"
            )

            metrics = monitor_client.metrics.list(
                resource_uri=resource_id,
                metricnames="Percentage CPU",
                timespan="PT5M",
                interval="PT1M",
                aggregation="Average"
            )

            avg_cpu = None
            if metrics.value:
                data = metrics.value[0].timeseries[0].data
                if data:
                    avg_cpu = data[-1].average

            return json.dumps({"status": "success", "cpu_percentage": avg_cpu})

        elif tool_name == "list_storage_accounts":
            accounts = list(storage_client.storage_accounts.list_by_resource_group(arguments["resource_group_name"]))
            return json.dumps({"status": "success", "storage_accounts": [acc.name for acc in accounts]})

        elif tool_name.startswith("meraki_"):
            return execute_meraki_tool(tool_name, arguments)
        
        elif tool_name.startswith("ansible_meraki_"):
            return execute_meraki_ansible_tool(tool_name, arguments)

        elif tool_name in ("ansible_run_task", "ansible_list_tasks"):
            return execute_ansible_task_tool(tool_name, arguments)

        elif tool_name == "list_web_apps":
            apps = list(web_client.web_apps.list_by_resource_group(arguments["resource_group_name"]))
            return json.dumps({"status": "success", "web_apps": [app.name for app in apps]})

        elif tool_name == "list_sql_servers":
            servers = list(sql_client.servers.list_by_resource_group(arguments["resource_group_name"]))
            return json.dumps({"status": "success", "sql_servers": [s.name for s in servers]})

        elif tool_name == "list_keyvaults":
            vaults = list(kv_client.vaults.list_by_resource_group(arguments["resource_group_name"]))
            return json.dumps({"status": "success", "keyvaults": [v.name for v in vaults]})

        elif tool_name == "list_aks_clusters":
            clusters = list(aks_client.managed_clusters.list_by_resource_group(arguments["resource_group_name"]))
            return json.dumps({"status": "success", "aks_clusters": [c.name for c in clusters]})

        elif tool_name == "list_container_instances":
            groups = list(aci_client.container_groups.list_by_resource_group(arguments["resource_group_name"]))
            return json.dumps({"status": "success", "container_instances": [g.name for g in groups]})

        elif tool_name == "query_resources":
            query = QueryRequest(
                query=arguments["kql_query"],
                subscriptions=[target_sub_id]
            )
            response = rg_client.resources(query)
            result_data = response.data if hasattr(response, "data") else []
            return json.dumps({"status": "success", "results": result_data})

        elif tool_name == "list_subscriptions":
            # 同时查询 Tenant A 和 Tenant B 的订阅
            all_subs = []

            # Tenant A (DefaultAzureCredential)
            try:
                sub_client_a = SubscriptionClient(credential)
                for s in sub_client_a.subscriptions.list():
                    all_subs.append({
                        "id": s.subscription_id,
                        "name": s.display_name,
                        "source": "tenant_a"
                    })
            except Exception as e:
                logging.warning(f"Tenant A subscription list failed: {e}")

            # Tenant B (Tenant B SP)
            try:
                secondary_cred = _get_credential_for_subscription(os.environ.get("SUBSCRIPTION_SECONDARY_ID"))
                if secondary_cred is not credential:
                    sub_client_b = SubscriptionClient(secondary_cred)
                    for s in sub_client_b.subscriptions.list():
                        all_subs.append({
                            "id": s.subscription_id,
                            "name": s.display_name,
                            "source": "tenant_b"
                        })
            except Exception as e:
                logging.warning(f"Tenant B subscription list failed: {e}")

            return json.dumps({
                "status": "success",
                "subscriptions": all_subs
            })

        # ... 其余 metrics 工具保持不变（略，与原版相同）...
        elif tool_name == "get_webapp_metrics":
            resource_id = (
                f"/subscriptions/{target_sub_id}/resourceGroups/{arguments['resource_group_name']}"
                f"/providers/Microsoft.Web/sites/{arguments['webapp_name']}"
            )
            metric_names = arguments.get("metric_names", "Requests,AverageResponseTime,Http4xx,Http5xx,BytesReceived,BytesSent,MemoryWorkingSet,CpuTime")
            timespan = arguments.get("timespan", "PT1H")
            interval = arguments.get("interval", "PT5M")
            aggregation = arguments.get("aggregation", "Average,Total,Count")

            metrics = monitor_client.metrics.list(
                resource_uri=resource_id,
                metricnames=metric_names,
                timespan=timespan,
                interval=interval,
                aggregation=aggregation
            )
            result = {}
            for metric in metrics.value:
                metric_name = metric.name.value
                result[metric_name] = {"unit": str(metric.unit) if metric.unit else None, "data": []}
                if metric.timeseries:
                    for ts in metric.timeseries:
                        for dp in ts.data:
                            result[metric_name]["data"].append({
                                "time": dp.time_stamp.isoformat() if dp.time_stamp else None,
                                "average": dp.average,
                                "total": dp.total,
                                "count": dp.count,
                                "minimum": dp.minimum,
                                "maximum": dp.maximum
                            })
            return json.dumps({"status": "success", "metrics": result})

        elif tool_name == "get_network_metrics":
            resource_type = arguments.get("resource_type", "nsg")
            resource_id = (
                f"/subscriptions/{target_sub_id}/resourceGroups/{arguments['resource_group_name']}"
            )
            if resource_type == "nsg":
                resource_id += f"/providers/Microsoft.Network/networkSecurityGroups/{arguments['resource_name']}"
                default_metrics = "PacketCount,ByteCount"
            elif resource_type == "loadbalancer":
                resource_id += f"/providers/Microsoft.Network/loadBalancers/{arguments['resource_name']}"
                default_metrics = "VIPAvailability,DataPathAvailability,ByteCount,PacketCount,SNATConnectionCount"
            elif resource_type == "applicationgateway":
                resource_id += f"/providers/Microsoft.Network/applicationGateways/{arguments['resource_name']}"
                default_metrics = "Throughput,UnhealthyHostCount,ResponseStatus,CurrentConnections"
            elif resource_type == "vnetgateway":
                resource_id += f"/providers/Microsoft.Network/virtualNetworkGateways/{arguments['resource_name']}"
                default_metrics = "AverageBandwidth,P2SBandwidth,TunnelAverageBandwidth"
            elif resource_type == "publicip":
                resource_id += f"/providers/Microsoft.Network/publicIPAddresses/{arguments['resource_name']}"
                default_metrics = "BytesInDDoS,BytesOutDDoS,DDoSTriggerTCPPackets,DDoSTriggerUDPPackets"
            elif resource_type == "firewall":
                resource_id += f"/providers/Microsoft.Network/azureFirewalls/{arguments['resource_name']}"
                default_metrics = "DataProcessed,Throughput"
            elif resource_type == "frontdoor":
                resource_id += f"/providers/Microsoft.Network/frontDoors/{arguments['resource_name']}"
                default_metrics = "RequestCount,Latency,BackendHealthPercentage"
            elif resource_type == "cdn":
                resource_id += f"/providers/Microsoft.Cdn/profiles/{arguments['resource_name']}"
                default_metrics = "RequestCount,ResponseSize,OriginHealthPercentage"
            else:
                resource_id += f"/providers/Microsoft.Network/{resource_type}s/{arguments['resource_name']}"
                default_metrics = arguments.get("metric_names", "")

            metric_names = arguments.get("metric_names", default_metrics)
            timespan = arguments.get("timespan", "PT1H")
            interval = arguments.get("interval", "PT5M")
            aggregation = arguments.get("aggregation", "Average,Total,Count")

            metrics = monitor_client.metrics.list(
                resource_uri=resource_id,
                metricnames=metric_names,
                timespan=timespan,
                interval=interval,
                aggregation=aggregation
            )
            result = {}
            for metric in metrics.value:
                metric_name = metric.name.value
                result[metric_name] = {"unit": str(metric.unit) if metric.unit else None, "data": []}
                if metric.timeseries:
                    for ts in metric.timeseries:
                        for dp in ts.data:
                            result[metric_name]["data"].append({
                                "time": dp.time_stamp.isoformat() if dp.time_stamp else None,
                                "average": dp.average,
                                "total": dp.total,
                                "count": dp.count,
                                "minimum": dp.minimum,
                                "maximum": dp.maximum
                            })
            return json.dumps({"status": "success", "metrics": result})

        elif tool_name == "get_app_insights_metrics":
            resource_id = (
                f"/subscriptions/{target_sub_id}/resourceGroups/{arguments['resource_group_name']}"
                f"/providers/Microsoft.Insights/components/{arguments['app_insights_name']}"
            )
            metric_names = arguments.get("metric_names", "requests/count,exceptions/count,dependencies/duration,pageViews/count")
            timespan = arguments.get("timespan", "PT1H")
            interval = arguments.get("interval", "PT5M")
            aggregation = arguments.get("aggregation", "Average,Total,Count")

            metrics = monitor_client.metrics.list(
                resource_uri=resource_id,
                metricnames=metric_names,
                timespan=timespan,
                interval=interval,
                aggregation=aggregation
            )
            result = {}
            for metric in metrics.value:
                metric_name = metric.name.value
                result[metric_name] = {"unit": str(metric.unit) if metric.unit else None, "data": []}
                if metric.timeseries:
                    for ts in metric.timeseries:
                        for dp in ts.data:
                            result[metric_name]["data"].append({
                                "time": dp.time_stamp.isoformat() if dp.time_stamp else None,
                                "average": dp.average,
                                "total": dp.total,
                                "count": dp.count,
                                "minimum": dp.minimum,
                                "maximum": dp.maximum
                            })
            return json.dumps({"status": "success", "metrics": result})

        elif tool_name == "get_resource_metrics":
            resource_id = (
                f"/subscriptions/{target_sub_id}/resourceGroups/{arguments['resource_group_name']}"
                f"/providers/{arguments['resource_provider']}/{arguments['resource_name']}"
            )
            metric_names = arguments.get("metric_names", "")
            timespan = arguments.get("timespan", "PT1H")
            interval = arguments.get("interval", "PT5M")
            aggregation = arguments.get("aggregation", "Average,Total,Count")

            metrics = monitor_client.metrics.list(
                resource_uri=resource_id,
                metricnames=metric_names,
                timespan=timespan,
                interval=interval,
                aggregation=aggregation
            )
            result = {}
            for metric in metrics.value:
                metric_name = metric.name.value
                result[metric_name] = {"unit": str(metric.unit) if metric.unit else None, "data": []}
                if metric.timeseries:
                    for ts in metric.timeseries:
                        for dp in ts.data:
                            result[metric_name]["data"].append({
                                "time": dp.time_stamp.isoformat() if dp.time_stamp else None,
                                "average": dp.average,
                                "total": dp.total,
                                "count": dp.count,
                                "minimum": dp.minimum,
                                "maximum": dp.maximum
                            })
            return json.dumps({"status": "success", "metrics": result})

        elif tool_name == "get_resource_metric_definitions":
            resource_id = (
                f"/subscriptions/{target_sub_id}/resourceGroups/{arguments['resource_group_name']}"
                f"/providers/{arguments['resource_provider']}/{arguments['resource_name']}"
            )
            definitions = monitor_client.metric_definitions.list(resource_uri=resource_id)
            defs = []
            for d in definitions:
                defs.append({
                    "name": d.name.value,
                    "unit": str(d.unit),
                    "primary_aggregation_type": d.primary_aggregation_type,
                    "supported_aggregation_types": [str(t) for t in d.supported_aggregation_types] if d.supported_aggregation_types else []
                })
            return json.dumps({"status": "success", "definitions": defs})

        # ---- AI 用量管理工具分发 ----
        elif tool_name.startswith("kimi_"):
            return execute_kimi_tool(tool_name, arguments)

        elif tool_name.startswith("deepseek_"):
            return execute_deepseek_tool(tool_name, arguments)

        elif tool_name == "get_api_usage":
            provider = arguments.get("provider", DEFAULT_AI_PROVIDER)
            return _build_api_usage_response(provider)

        return json.dumps({"status": "success"})

    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)})
#=====（修改内容结束）=====


#=====（修改内容：get_ai_client / get_ai_model 支持 / kimi / deepseek 三个 provider）=====
def get_ai_client(provider: str | None = None) -> OpenAI:
    """根据 provider 获取对应 AI 的 OpenAI 客户端"""
    provider = _normalize_provider(provider)

    if provider == "deepseek":
        client = SecretClient(vault_url=CONFIG["DEEPSEEK_KEY_VAULT_URL"], credential=credential)
        api_key = client.get_secret(CONFIG["DEEPSEEK_SECRET_NAME"]).value
        base_url = CONFIG["DEEPSEEK_BASE_URL"]

    else:
        client = SecretClient(vault_url=CONFIG["KEY_VAULT_URL"], credential=credential)
        api_key = client.get_secret(CONFIG["SECRET_NAME"]).value
        base_url = CONFIG["AI_BASE_URL"]

    http_client = httpx.Client(timeout=httpx.Timeout(360.0, connect=10.0))

    return OpenAI(
        api_key=api_key,
        base_url=base_url,
        http_client=http_client
    )


def get_ai_model(provider: str | None = None) -> str:
    """根据 provider 获取对应模型名称"""
    provider = _normalize_provider(provider)

    if provider == "deepseek":
        return CONFIG["DEEPSEEK_MODEL_NAME"]

    return CONFIG["AI_MODEL_NAME"]
#=====（修改内容结束）=====


#=====（修改内容：图像识别（Vision）支持）=====
# Kimi：kimi-k3 原生支持视觉输入，直接用默认模型即可
# DeepSeek：带图片的请求自动切换为 CONFIG["DEEPSEEK_VISION_MODEL_NAME"]
MAX_CHAT_IMAGES = int(os.environ.get("MAX_CHAT_IMAGES", 5))


def _normalize_image_data_url(img) -> str | None:
    """把前端传来的图片规范成 data:image/...;base64,... 形式"""
    if not isinstance(img, str) or not img.strip():
        return None
    img = img.strip()
    if img.startswith("data:image/"):
        return img
    # 纯 base64（未带 data: 前缀）默认按 jpeg 处理
    return f"data:image/jpeg;base64,{img}"


def _messages_contain_images(messages) -> bool:
    """检测消息列表中是否包含图片（image_url / video_url 部分）"""
    for m in messages or []:
        if isinstance(m, dict) and isinstance(m.get("content"), list):
            for part in m["content"]:
                if isinstance(part, dict) and part.get("type") in ("image_url", "video_url"):
                    return True
    return False


def _build_multimodal_user_content(prompt: str, images: list) -> list:
    """构造 OpenAI 多模态 user content：图片部分在前、文字部分在后"""
    parts = []
    for img in (images or [])[:MAX_CHAT_IMAGES]:
        url = _normalize_image_data_url(img)
        if url:
            parts.append({"type": "image_url", "image_url": {"url": url}})
    parts.append({"type": "text", "text": prompt or "请描述这张图片。"})
    return parts


def _flatten_content_to_text(content) -> str:
    """
    把多模态 content 数组转成纯文本，图片部分替换为 [图片xN] 占位符。
    用于对话历史持久化：避免把 base64 大图写入 Cosmos DB（单字段 64KB 上限），
    也避免后续轮次把旧图片重复发给 API。
    """
    if isinstance(content, str):
        return content
    if not isinstance(content, list):
        return "" if content is None else str(content)

    texts = []
    image_count = 0
    for part in content:
        if not isinstance(part, dict):
            continue
        if part.get("type") == "text":
            texts.append(str(part.get("text", "")))
        elif part.get("type") in ("image_url", "video_url"):
            image_count += 1

    prefix = f"[图片x{image_count}] " if image_count else ""
    return prefix + "\n".join(t for t in texts if t)
#=====（修改内容结束）=====


#=====（修改内容：文件分析支持（代码 / 日志 / PDF / Word / Excel / PPT））=====
# 每个文件提取后注入提示词的最大字符数（防止 token 爆炸，可用环境变量覆盖）
MAX_FILE_CHARS = int(os.environ.get("MAX_FILE_CHARS", 50000))
MAX_CHAT_FILES = int(os.environ.get("MAX_CHAT_FILES", 5))

# 纯文本类扩展名：直接按 UTF-8 解码，不需要第三方库
_TEXT_FILE_EXTS = {
    ".txt", ".md", ".markdown", ".py", ".js", ".ts", ".jsx", ".tsx", ".java",
    ".c", ".cpp", ".h", ".hpp", ".cs", ".go", ".rs", ".php", ".rb", ".sh",
    ".json", ".xml", ".yaml", ".yml", ".toml", ".ini", ".cfg", ".conf",
    ".csv", ".tsv", ".log", ".sql", ".html", ".htm", ".css", ".env", ".ps1",
}


def _decode_file_base64(content_base64: str) -> bytes:
    """解码前端传来的 base64 文件内容（兼容 data: 前缀）"""
    import base64 as _b64
    s = (content_base64 or "").split(",", 1)[-1]
    return _b64.b64decode(s)


def _extract_file_text(name: str, file_obj: dict) -> str:
    """
    从上传文件中提取纯文本。
    file_obj 二选一：
      {"name": ..., "content": "文本内容"}        —— 前端已按文本读取
      {"name": ..., "content_base64": "..."}      —— 二进制文件，后端按扩展名解析
    依赖：PDF→pdfplumber 或 pypdf；Word→python-docx；Excel→openpyxl；PPT→python-pptx
    """
    from io import BytesIO

    ext = os.path.splitext(name or "")[1].lower()

    # 1) 前端已按文本读取，直接使用
    if isinstance(file_obj.get("content"), str):
        return file_obj["content"]

    raw = _decode_file_base64(file_obj.get("content_base64") or "")

    # 2) 文本类扩展名直接解码
    if ext in _TEXT_FILE_EXTS:
        return raw.decode("utf-8", errors="replace")

    # 3) 旧版 Office 二进制格式不支持，给出明确提示
    if ext in (".doc", ".xls", ".ppt"):
        raise RuntimeError(f"暂不支持旧版 Office 格式 {ext}，请另存为 .docx/.xlsx/.pptx 后重新上传")

    # 4) PDF
    if ext == ".pdf":
        try:
            import pdfplumber
            with pdfplumber.open(BytesIO(raw)) as pdf:
                return "\n".join((page.extract_text() or "") for page in pdf.pages)
        except ImportError:
            try:
                from pypdf import PdfReader
                reader = PdfReader(BytesIO(raw))
                return "\n".join((page.extract_text() or "") for page in reader.pages)
            except ImportError:
                raise RuntimeError("解析 PDF 需要在 requirements.txt 中加入 pdfplumber 或 pypdf")

    # 5) Word (.docx)
    if ext == ".docx":
        try:
            import docx
        except ImportError:
            raise RuntimeError("解析 Word 需要在 requirements.txt 中加入 python-docx")
        d = docx.Document(BytesIO(raw))
        return "\n".join(p.text for p in d.paragraphs if p.text.strip())

    # 6) Excel (.xlsx / .xlsm)
    if ext in (".xlsx", ".xlsm"):
        try:
            import openpyxl
        except ImportError:
            raise RuntimeError("解析 Excel 需要在 requirements.txt 中加入 openpyxl")
        wb = openpyxl.load_workbook(BytesIO(raw), data_only=True, read_only=True)
        lines = []
        for ws in wb.worksheets:
            lines.append(f"# Sheet: {ws.title}")
            for row in ws.iter_rows(values_only=True):
                cells = ["" if c is None else str(c) for c in row]
                if any(cells):
                    lines.append("\t".join(cells))
        return "\n".join(lines)

    # 7) PowerPoint (.pptx)
    if ext == ".pptx":
        try:
            from pptx import Presentation
        except ImportError:
            raise RuntimeError("解析 PPT 需要在 requirements.txt 中加入 python-pptx")
        prs = Presentation(BytesIO(raw))
        lines = []
        for i, slide in enumerate(prs.slides, 1):
            lines.append(f"# Slide {i}")
            for shape in slide.shapes:
                if shape.has_text_frame:
                    txt = shape.text_frame.text.strip()
                    if txt:
                        lines.append(txt)
        return "\n".join(lines)

    # 8) 未知类型：尝试按文本解码（解出来是乱码也不阻断）
    return raw.decode("utf-8", errors="replace")


def _build_files_context(files: list) -> str:
    """把上传文件列表解析并拼成注入提示词的文本块，超长文件截断"""
    blocks = []
    for f in (files or [])[:MAX_CHAT_FILES]:
        if not isinstance(f, dict):
            continue
        name = f.get("name") or "unnamed"
        try:
            text = _extract_file_text(name, f)
        except Exception as e:
            text = f"[文件解析失败: {e}]"
        if len(text) > MAX_FILE_CHARS:
            orig_len = len(text)
            text = text[:MAX_FILE_CHARS] + f"\n……[文件过长已截断，原长 {orig_len} 字符，仅保留前 {MAX_FILE_CHARS} 字符]"
        blocks.append(f"===== 文件：{name} =====\n{text}\n===== 文件 {name} 结束 =====")
    return "\n\n".join(blocks)
#=====（修改内容结束）=====


# ========== 公共 AI 工具循环 ==========
#=====（修改内容：run_ai_with_tools 支持 / kimi / deepseek，并将用量写入 Cosmos DB）=====
def run_ai_with_tools(messages, subscription_id, ai_provider=None, tools="default", model=None, temperature=None):
    """
    执行 AI 对话，自动处理工具调用，返回最终 assistant 回复内容。

    tools 参数（向后兼容，默认 "default" 保持原行为）：
      - "default"：挂载全部 azure_tools（运维 Agent 模式）
      - None 或 []：不挂任何工具（纯通用对话模式）
      - 自定义 list：挂载指定工具集
    model：覆盖默认模型名；temperature：覆盖默认采样温度（None 则不下发，用服务端默认）。
    """
    provider = _normalize_provider(ai_provider)
    logging.info(f"[AI] 开始初始化客户端, provider={provider}")
    ai_client = get_ai_client(provider)
    model = model or get_ai_model(provider)

    # 带图片的请求：DeepSeek 默认模型不支持视觉，自动切换为视觉模型；
    # Kimi(kimi-k3) 原生支持视觉输入，保持默认模型即可
    if _messages_contain_images(messages) and provider == "deepseek" \
            and model == CONFIG["DEEPSEEK_MODEL_NAME"]:
        model = CONFIG["DEEPSEEK_VISION_MODEL_NAME"]
        logging.info(f"[AI] 检测到图片输入，DeepSeek 自动切换为视觉模型: {model}")

    logging.info(f"[AI] 客户端就绪, provider={provider}, model={model}")

    # 解析工具配置
    if tools == "default":
        tool_list = azure_tools
    elif not tools:  # None 或空列表 → 纯对话
        tool_list = None
    else:
        tool_list = tools

    request_kwargs = {
        "model": model,
        "messages": messages,
    }
    if tool_list:
        request_kwargs["tools"] = tool_list
        request_kwargs["tool_choice"] = "auto"
    if temperature is not None:
        request_kwargs["temperature"] = temperature

    max_iterations = 10 if tool_list else 1

    for iteration in range(max_iterations):
        logging.info(f"[AI] 第 {iteration + 1}/{max_iterations} 轮调用 {provider}/{model} 开始")
        response = ai_client.chat.completions.create(**request_kwargs)

        if response.usage:
            usage = response.usage

            _persist_usage(
                provider=provider,
                model=model,
                prompt_tokens=usage.prompt_tokens,
                completion_tokens=usage.completion_tokens,
                total_tokens=usage.total_tokens,
                endpoint="chat.completions"
            )

            record = {
                "timestamp": datetime.datetime.utcnow().isoformat(),
                "model": model,
                "prompt_tokens": usage.prompt_tokens,
                "completion_tokens": usage.completion_tokens,
                "total_tokens": usage.total_tokens,
                "endpoint": "chat.completions"
            }

            _append_memory_record(provider, record)

        logging.info(f"[AI] 第 {iteration + 1} 轮调用完成, usage={response.usage}")
        msg = response.choices[0].message

        if tool_list and msg.tool_calls:
            logging.info(f"[AI] 本轮触发 {len(msg.tool_calls)} 个工具调用: {[t.function.name for t in msg.tool_calls]}")
            messages.append({
                "role": "assistant",
                "content": msg.content,
                "tool_calls": msg.tool_calls
            })

            for tool_call in msg.tool_calls:
                tool_args = json.loads(tool_call.function.arguments)

                if subscription_id and "subscription_id" not in tool_args:
                    tool_args["subscription_id"] = subscription_id

                obs = execute_azure_tool(tool_call.function.name, tool_args)

                messages.append({
                    "role": "tool",
                    "tool_call_id": tool_call.id,
                    "name": tool_call.function.name,
                    "content": obs
                })
        else:
            return msg.content

    raise Exception("AI 循环超过最大迭代次数")
#=====（修改内容结束）=====


#=====（修改内容：新增统一 JWT 校验函数，修复原接口中 token 校验重复代码）=====
def _verify_jwt(req: func.HttpRequest):
    """
    验证请求身份，支持两种方式：
    1. Authorization: Bearer <JWT>（本地 JWT）
    2. X-MS-CLIENT-PRINCIPAL（Easy Auth 注入的 Base64 用户信息）
    任一通过即可。
    通过返回 None，失败返回 HttpResponse（401）。
    """
    # 1. 尝试 JWT
    auth_header = req.headers.get("Authorization")
    if auth_header and auth_header.startswith("Bearer "):
        token = auth_header.split(" ", 1)[1]
        try:
            jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
            logging.info("JWT authentication succeeded.")
            return None
        except jwt.ExpiredSignatureError:
            logging.warning("JWT expired, fallback to Easy Auth.")
            # 继续尝试 Easy Auth，而不是直接返回 401
        except jwt.InvalidTokenError:
            logging.warning("Invalid JWT, fallback to Easy Auth.")
            # 同上

    # 2. 尝试 Easy Auth（X-MS-CLIENT-PRINCIPAL）
    principal = req.headers.get("X-MS-CLIENT-PRINCIPAL")
    if principal:
        # 只要该头存在且非空，就认为已通过 Azure 平台认证
        logging.info("Easy Auth principal found, authentication succeeded.")
        return None

    # 两种方式都失败
    return func.HttpResponse(
        json.dumps({"error": "Missing or invalid authentication"}),
        status_code=401,
        mimetype="application/json"
    )
#=====Meraki 执行=====
def execute_meraki_tool(tool_name: str, arguments: dict) -> str:
    """执行 Meraki Dashboard API 查询工具"""
    try:
        # 从 Key Vault 获取 Meraki API Key
        meraki_api_key = get_secret_from_keyvault(MERAKI_SECRET_NAME)
        if not meraki_api_key:
            return json.dumps({
                "status": "error",
                "message": f"Meraki API Key not found in Key Vault '{CONFIG['KEY_VAULT_URL']}'"
            })

        meraki = MerakiClient(meraki_api_key)

        if tool_name == "meraki_list_organizations":
            data = meraki.get_organizations()
            return json.dumps({"status": "success", "provider": "meraki", "action": "list_organizations", "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_list_networks":
            org_id = arguments.get("org_id")
            if not org_id:
                return json.dumps({"status": "error", "message": "org_id is required"})
            data = meraki.get_networks(org_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "list_networks", "org_id": org_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_list_devices":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_devices(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "list_devices", "network_id": network_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_vpn_status":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_vpn_status(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_vpn_status", "network_id": network_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_device_uplink":
            serial = arguments.get("serial")
            if not serial:
                return json.dumps({"status": "error", "message": "serial is required"})
            data = meraki.get_uplink_status(serial)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_uplink_status", "serial": serial, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_vmx_licenses":
            org_id = arguments.get("org_id")
            if not org_id:
                return json.dumps({"status": "error", "message": "org_id is required"})
            data = meraki.get_vmx_licenses(org_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_vmx_licenses", "org_id": org_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_network_overview":
            org_id = arguments.get("org_id")
            network_id = arguments.get("network_id")
            if not org_id or not network_id:
                return json.dumps({"status": "error", "message": "org_id and network_id are required"})

            overview = {"network": None, "devices": [], "vpn_status": None}
            networks = meraki.get_networks(org_id)
            for net in networks:
                if net["id"] == network_id:
                    overview["network"] = net
                    break
            overview["devices"] = meraki.get_network_devices(network_id)
            try:
                overview["vpn_status"] = meraki.get_vpn_status(network_id)
            except Exception:
                overview["vpn_status"] = {"error": "VPN status not available for this network type"}

            return json.dumps({
                "status": "success",
                "provider": "meraki",
                "action": "get_network_overview",
                "org_id": org_id,
                "network_id": network_id,
                "data": overview
            }, ensure_ascii=False)
                # ===== Appliance 网络基础 =====
        elif tool_name == "meraki_get_appliance_settings":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_settings(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_appliance_settings", "network_id": network_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_appliance_vlans":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_vlans(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_appliance_vlans", "network_id": network_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_appliance_vlan":
            network_id = arguments.get("network_id")
            vlan_id = arguments.get("vlan_id")
            if not network_id or not vlan_id:
                return json.dumps({"status": "error", "message": "network_id and vlan_id are required"})
            data = meraki.get_network_appliance_vlan(network_id, vlan_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_appliance_vlan", "network_id": network_id, "vlan_id": vlan_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_appliance_ports":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_ports(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_appliance_ports", "network_id": network_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_appliance_port":
            network_id = arguments.get("network_id")
            port_id = arguments.get("port_id")
            if not network_id or not port_id:
                return json.dumps({"status": "error", "message": "network_id and port_id are required"})
            data = meraki.get_network_appliance_port(network_id, port_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_appliance_port", "network_id": network_id, "port_id": port_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_appliance_static_routes":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_static_routes(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_appliance_static_routes", "network_id": network_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_appliance_single_lan":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_single_lan(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_appliance_single_lan", "network_id": network_id, "data": data}, ensure_ascii=False)

        # ===== Appliance 防火墙 =====
        elif tool_name == "meraki_get_firewall_l3_rules":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_firewall_l3_rules(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_firewall_l3_rules", "network_id": network_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_firewall_l7_rules":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_firewall_l7_rules(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_firewall_l7_rules", "network_id": network_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_firewall_1to1_nat":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_firewall_one_to_one_nat(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_firewall_1to1_nat", "network_id": network_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_firewall_1toMany_nat":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_firewall_one_to_many_nat(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_firewall_1toMany_nat", "network_id": network_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_firewall_port_forwarding":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_firewall_port_forwarding(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_firewall_port_forwarding", "network_id": network_id, "data": data}, ensure_ascii=False)

        # ===== Appliance 安全服务 =====
        elif tool_name == "meraki_get_security_intrusion":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_security_intrusion(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_security_intrusion", "network_id": network_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_security_malware":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_security_malware(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_security_malware", "network_id": network_id, "data": data}, ensure_ascii=False)

        # ===== Appliance 内容过滤 =====
        elif tool_name == "meraki_get_content_filtering":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_content_filtering(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_content_filtering", "network_id": network_id, "data": data}, ensure_ascii=False)

        # ===== Appliance VPN =====
        elif tool_name == "meraki_get_vpn_site_to_site":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_vpn_site_to_site(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_vpn_site_to_site", "network_id": network_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_vpn_bgp":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_vpn_bgp(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_vpn_bgp", "network_id": network_id, "data": data}, ensure_ascii=False)

        # ===== Appliance 流量整形 / SD-WAN =====
        elif tool_name == "meraki_get_traffic_shaping":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_traffic_shaping(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_traffic_shaping", "network_id": network_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_traffic_shaping_rules":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_traffic_shaping_rules(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_traffic_shaping_rules", "network_id": network_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_traffic_shaping_uplink_bandwidth":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_traffic_shaping_uplink_bandwidth(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_traffic_shaping_uplink_bandwidth", "network_id": network_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_traffic_shaping_uplink_selection":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_traffic_shaping_uplink_selection(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_traffic_shaping_uplink_selection", "network_id": network_id, "data": data}, ensure_ascii=False)

        # ===== Appliance Uplink =====
        elif tool_name == "meraki_get_device_appliance_uplinks_settings":
            serial = arguments.get("serial")
            if not serial:
                return json.dumps({"status": "error", "message": "serial is required"})
            data = meraki.get_device_appliance_uplinks_settings(serial)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_device_appliance_uplinks_settings", "serial": serial, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_org_appliance_uplink_statuses":
            org_id = arguments.get("org_id")
            if not org_id:
                return json.dumps({"status": "error", "message": "org_id is required"})
            data = meraki.get_organization_appliance_uplink_statuses(org_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_org_appliance_uplink_statuses", "org_id": org_id, "data": data}, ensure_ascii=False)

        # ===== Appliance 高可用 =====
        elif tool_name == "meraki_get_warm_spare":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_warm_spare(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_warm_spare", "network_id": network_id, "data": data}, ensure_ascii=False)

        # ===== Appliance SSID / RF（MX 无线功能） =====
        elif tool_name == "meraki_get_appliance_ssids":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_ssids(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_appliance_ssids", "network_id": network_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_appliance_ssid":
            network_id = arguments.get("network_id")
            ssid_number = arguments.get("ssid_number")
            if not network_id or ssid_number is None:
                return json.dumps({"status": "error", "message": "network_id and ssid_number are required"})
            data = meraki.get_network_appliance_ssid(network_id, str(ssid_number))
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_appliance_ssid", "network_id": network_id, "ssid_number": ssid_number, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_appliance_rf_profiles":
            network_id = arguments.get("network_id")
            if not network_id:
                return json.dumps({"status": "error", "message": "network_id is required"})
            data = meraki.get_network_appliance_rf_profiles(network_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_appliance_rf_profiles", "network_id": network_id, "data": data}, ensure_ascii=False)

        # ===== 组织级 =====
        elif tool_name == "meraki_get_org_appliance_vlans":
            org_id = arguments.get("org_id")
            if not org_id:
                return json.dumps({"status": "error", "message": "org_id is required"})
            data = meraki.get_organization_appliance_vlans(org_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_org_appliance_vlans", "org_id": org_id, "data": data}, ensure_ascii=False)

                # ===== Switch 端口配置与状态 =====
        elif tool_name == "meraki_get_device_switch_ports":
            serial = arguments.get("serial")
            if not serial:
                return json.dumps({"status": "error", "message": "serial is required"})
            data = meraki.get_device_switch_ports(serial)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_device_switch_ports", "serial": serial, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_device_switch_port":
            serial = arguments.get("serial")
            port_id = arguments.get("port_id")
            if not serial or not port_id:
                return json.dumps({"status": "error", "message": "serial and port_id are required"})
            data = meraki.get_device_switch_port(serial, str(port_id))
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_device_switch_port", "serial": serial, "port_id": port_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_device_switch_ports_statuses":
            serial = arguments.get("serial")
            if not serial:
                return json.dumps({"status": "error", "message": "serial is required"})
            data = meraki.get_device_switch_ports_statuses(serial)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_device_switch_ports_statuses", "serial": serial, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_device_switch_ports_statuses_packets":
            serial = arguments.get("serial")
            if not serial:
                return json.dumps({"status": "error", "message": "serial is required"})
            data = meraki.get_device_switch_ports_statuses_packets(serial)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_device_switch_ports_statuses_packets", "serial": serial, "data": data}, ensure_ascii=False)

        # ===== 组织级 Switch 端口 =====
        elif tool_name == "meraki_get_org_switch_ports_by_switch":
            org_id = arguments.get("org_id")
            if not org_id:
                return json.dumps({"status": "error", "message": "org_id is required"})
            data = meraki.get_organization_switch_ports_by_switch(org_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_org_switch_ports_by_switch", "org_id": org_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_org_switch_ports_statuses_by_switch":
            org_id = arguments.get("org_id")
            if not org_id:
                return json.dumps({"status": "error", "message": "org_id is required"})
            data = meraki.get_organization_switch_ports_statuses_by_switch(org_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_org_switch_ports_statuses_by_switch", "org_id": org_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_org_switch_ports_statuses_packets_by_device_by_port":
            org_id = arguments.get("org_id")
            if not org_id:
                return json.dumps({"status": "error", "message": "org_id is required"})
            data = meraki.get_organization_switch_ports_statuses_packets_by_device_by_port(org_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_org_switch_ports_statuses_packets_by_device_by_port", "org_id": org_id, "data": data}, ensure_ascii=False)

        # ===== Switch 端口镜像 =====
        elif tool_name == "meraki_get_org_switch_ports_mirrors_by_switch":
            org_id = arguments.get("org_id")
            if not org_id:
                return json.dumps({"status": "error", "message": "org_id is required"})
            data = meraki.get_organization_switch_ports_mirrors_by_switch(org_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_org_switch_ports_mirrors_by_switch", "org_id": org_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_org_switch_stacks_ports_mirrors_by_stack":
            org_id = arguments.get("org_id")
            if not org_id:
                return json.dumps({"status": "error", "message": "org_id is required"})
            data = meraki.get_organization_switch_stacks_ports_mirrors_by_stack(org_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_org_switch_stacks_ports_mirrors_by_stack", "org_id": org_id, "data": data}, ensure_ascii=False)

        elif tool_name == "meraki_get_org_config_templates_switch_profiles_ports_mirrors_by_switch_profile":
            org_id = arguments.get("org_id")
            config_template_id = arguments.get("config_template_id")
            profile_id = arguments.get("profile_id")
            if not org_id or not config_template_id or not profile_id:
                return json.dumps({"status": "error", "message": "org_id, config_template_id and profile_id are required"})
            data = meraki.get_organization_config_templates_switch_profiles_ports_mirrors_by_switch_profile(org_id, config_template_id, profile_id)
            return json.dumps({"status": "success", "provider": "meraki", "action": "get_org_config_templates_switch_profiles_ports_mirrors_by_switch_profile", "org_id": org_id, "config_template_id": config_template_id, "profile_id": profile_id, "data": data}, ensure_ascii=False)

        return json.dumps({"status": "error", "message": f"Unknown Meraki tool: {tool_name}"})

    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response else 500
        error_body = e.response.text if e.response else str(e)
        return json.dumps({"status": "error", "message": f"Meraki API Error ({status_code})", "details": error_body}, ensure_ascii=False)
    except Exception as e:
        return json.dumps({"status": "error", "message": str(e)}, ensure_ascii=False)

# ========== 登录接口 ==========
#=====（修改内容：修复 login 字段尾随空格）=====
@app.route(route="login", methods=[func.HttpMethod.POST], auth_level=func.AuthLevel.ANONYMOUS)
def login(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Login function triggered.")

    try:
        req_body = req.get_json()

        username = req_body.get("username")
        password = req_body.get("password")

        if username == ADMIN_USERNAME and password == ADMIN_PASSWORD:
            payload = {
                "username": username,
                "exp": datetime.datetime.utcnow() + datetime.timedelta(minutes=JWT_EXPIRATION_MINUTES)
            }

            token = jwt.encode(payload, JWT_SECRET, algorithm="HS256")

            return func.HttpResponse(
                json.dumps({"token": token}),
                status_code=200,
                mimetype="application/json"
            )

        return func.HttpResponse(
            json.dumps({"error": "Invalid credentials"}),
            status_code=401,
            mimetype="application/json"
        )

    except Exception as e:
        logging.error(f"Login error: {e!s}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            status_code=500,
            mimetype="application/json"
        )
#=====（修改内容结束）=====


# ========== 通用导出接口 ==========
@app.route(route="export", methods=[func.HttpMethod.GET], auth_level=func.AuthLevel.ANONYMOUS)
def export_data(req: func.HttpRequest) -> func.HttpResponse:
    """通用导出端点，支持导出存储账户、虚拟机等资源列表为 CSV"""
    logging.info("Export API triggered.")

    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    resource_type = req.params.get("resource_type")
    subscription_id = req.params.get("subscription_id")
    resource_group_name = req.params.get("resource_group_name")

    if not resource_type or not subscription_id:
        return func.HttpResponse("Missing resource_type or subscription_id", status_code=400)

    try:
        if resource_type == "storage":
            client = StorageManagementClient(credential, subscription_id)

            if resource_group_name:
                accounts = list(client.storage_accounts.list_by_resource_group(resource_group_name))
            else:
                accounts = list(client.storage_accounts.list())

            output = StringIO()
            writer = csv.writer(output)
            writer.writerow(["Name", "Location", "Resource Group", "Subscription ID"])

            for acc in accounts:
                rg = acc.id.split("/")[4] if acc.id else "N/A"
                writer.writerow([acc.name, acc.location, rg, subscription_id])

            csv_content = output.getvalue()
            output.close()

        elif resource_type == "vm":
            client = ComputeManagementClient(credential, subscription_id)

            if resource_group_name:
                vms = list(client.virtual_machines.list(resource_group_name))
            else:
                vms = list(client.virtual_machines.list_all())

            output = StringIO()
            writer = csv.writer(output)
            writer.writerow(["Name", "Location", "Resource Group", "Status", "Subscription ID"])

            for vm in vms:
                rg = vm.id.split("/")[4] if vm.id else "N/A"

                try:
                    instance_view = client.virtual_machines.instance_view(resource_group_name or rg, vm.name)
                    status = "Unknown"

                    for s in instance_view.statuses:
                        if s.code.startswith("PowerState/"):
                            status = s.code.split("/")[1]
                            break

                except Exception:
                    status = "N/A"

                writer.writerow([vm.name, vm.location, rg, status, subscription_id])

            csv_content = output.getvalue()
            output.close()

        else:
            return func.HttpResponse(f"Unsupported resource_type: {resource_type}", status_code=400)

        return func.HttpResponse(
            csv_content,
            status_code=200,
            mimetype="text/csv",
            headers={
                "Content-Disposition": f"attachment; filename={resource_type}_{datetime.datetime.now().strftime('%Y%m%d')}.csv"
            }
        )

    except Exception as e:
        logging.error(f"Export error: {e!s}")
        return func.HttpResponse(f"Export failed: {e!s}", status_code=500)


# ========== 订阅列表接口 ==========
@app.route(route="list_subscriptions", methods=[func.HttpMethod.GET, func.HttpMethod.POST], auth_level=func.AuthLevel.ANONYMOUS)
def list_subscriptions_api(req: func.HttpRequest) -> func.HttpResponse:
    """供前端下拉框调用的订阅列表端点，同时包含 Tenant A 和 Tenant B 的订阅"""
    logging.info("list_subscriptions API triggered.")

    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    all_subs = []

    # 1. Tenant A 的订阅（DefaultAzureCredential）
    try:
        sub_client_a = SubscriptionClient(credential)
        subs_a = list(sub_client_a.subscriptions.list())
        for s in subs_a:
            all_subs.append({
                "id": s.subscription_id,
                "name": s.display_name,
                "tenant_id": getattr(s, "tenant_id", None) or "tenant-a",
                "source": "tenant_a"
            })
        logging.info(f"Tenant A subscriptions: {len(subs_a)}")
    except Exception as e:
        logging.warning(f"Tenant A subscription list failed: {e}")

    # 2. Tenant B (SP) 的订阅（ClientSecretCredential）
    try:
        secondary_tenant_id = os.environ.get("TENANT_SECONDARY_ID")
        client_id = os.environ.get("SP_CLIENT_ID")
        client_secret = os.environ.get("SP_CLIENT_SECRET")

        if all([secondary_tenant_id, client_id, client_secret]):
            secondary_credential = ClientSecretCredential(
                tenant_id=secondary_tenant_id,
                client_id=client_id,
                client_secret=client_secret
            )
            sub_client_b = SubscriptionClient(secondary_credential)
            subs_b = list(sub_client_b.subscriptions.list())
            for s in subs_b:
                all_subs.append({
                    "id": s.subscription_id,
                    "name": s.display_name,
                    "tenant_id": secondary_tenant_id,
                    "source": "tenant_b"
                })
            logging.info(f"Tenant B subscriptions: {len(subs_b)}")
    except Exception as e:
        logging.warning(f"Tenant B subscription list failed: {e}")

    return func.HttpResponse(
        json.dumps({
            "status": "success",
            "count": len(all_subs),
            "subscriptions": all_subs
        }, indent=2, ensure_ascii=False),
        status_code=200,
        mimetype="application/json"
    )


# ========== infra-agent 接口 ==========
#=====（修改内容：infra_agent 支持前端通过 ai_provider / provider / api 指定 / kimi / deepseek）=====
@app.route(route="infra_agent", methods=[func.HttpMethod.POST], auth_level=func.AuthLevel.ANONYMOUS)
def infra_agent(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Azure Infra Agent triggered.")

    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    try:
        req_body = req.get_json()

        # ========== 多轮对话上下文支持 ==========
        conversation_id = req_body.get("conversation_id")

        # 1. 优先使用前端传入的完整 messages（前端自己管理历史）
        messages = req_body.get("messages")

        if messages:
            # 前端自己管理历史，后端只负责执行
            pass

        elif conversation_id:
            # 2. 后端管理历史：加载已有历史 + 追加当前问题
            history = _load_conversation(conversation_id)
            user_prompt = req_body.get("prompt")
            if not user_prompt:
                return func.HttpResponse(
                    json.dumps({"error": "使用 conversation_id 时必须提供 'prompt'"}, ensure_ascii=False),
                    status_code=400,
                    mimetype="application/json"
                )

            if not history:
                # 新对话，加入 system prompt
                history = [{
                    "role": "system",
                    "content": "You are an Azure operation assistant. Respond in the same language as the user."
                }]

            history.append({"role": "user", "content": user_prompt})
            messages = history

        else:
            # 3. 纯单轮模式（兼容旧接口）
            user_prompt = req_body.get("prompt")
            if not user_prompt:
                return func.HttpResponse(
                    json.dumps({"error": "请提供 'prompt'、'messages' 或 'conversation_id'"}, ensure_ascii=False),
                    status_code=400,
                    mimetype="application/json"
                )
            messages = [
                {
                    "role": "system",
                    "content": "You are an Azure operation assistant. Respond in the same language as the user."
                },
                {"role": "user", "content": user_prompt}
            ]
        # =====================================

        subscription_id_from_req = req_body.get("subscription_id")

        ai_provider = (
            req_body.get("ai_provider")
            or req_body.get("provider")
            or req_body.get("api")
            or DEFAULT_AI_PROVIDER
        )

        final_content = run_ai_with_tools(messages, subscription_id_from_req, ai_provider)

        # ========== 保存更新后的历史 ==========
        if conversation_id and not req_body.get("messages"):
            # 把 assistant 回复也追加进去再保存
            messages.append({"role": "assistant", "content": final_content})
            _save_conversation(conversation_id, messages)
        # =====================================

        return func.HttpResponse(
            json.dumps({
                "response": final_content,
                "conversation_id": conversation_id
            }, ensure_ascii=False),
            mimetype="application/json",
            status_code=200
        )

    except Exception as e:
        logging.error(f"Error: {e!s}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}, ensure_ascii=False),
            status_code=500,
            mimetype="application/json"
        )



@app.route(route="export_drawio", methods=[func.HttpMethod.GET], auth_level=func.AuthLevel.ANONYMOUS)
def export_drawio(req):
    logging.info("Export Draw.io XML triggered.")

    # ========== JWT 验证（保持不变） ==========
    auth_header = req.headers.get("Authorization")
    if not auth_header or not auth_header.startswith("Bearer "):
        return func.HttpResponse("Missing or invalid token", status_code=401)
    token = auth_header.split(" ")[1]
    try:
        jwt.decode(token, JWT_SECRET, algorithms=["HS256"])
    except jwt.ExpiredSignatureError:
        return func.HttpResponse("Token expired", status_code=401)
    except jwt.InvalidTokenError:
        return func.HttpResponse("Invalid token", status_code=401)

    subscription_id = req.params.get('subscription_id')
    if not subscription_id:
        return func.HttpResponse("Missing subscription_id", status_code=400)

    try:
        # 1. 一次性获取所有资源（Resource Graph，~1 秒）
        logging.info("Fetching resources via Azure Resource Graph...")
        rg_client = ResourceGraphClient(credential)
        kql = (
            f'resources | where subscriptionId == "{subscription_id}" '
            '| project name, type, location, resourceGroup, id, kind '
            '| limit 1000'
        )
        query = QueryRequest(query=kql, subscriptions=[subscription_id])
        rg_response = rg_client.resources(query)
        resources_data = rg_response.data if hasattr(rg_response, 'data') else []

        resource_client = ResourceManagementClient(credential, subscription_id)
        rgs = list(resource_client.resource_groups.list())
        rg_list = [{"name": r.name, "location": r.location} for r in rgs]

        logging.info(f"Found {len(resources_data)} resources in {len(rg_list)} resource groups")

        # 2. 纯 Python 生成 XML（~2 秒，零 AI 调用）
        xml_content = _generate_drawio_xml(resources_data, rg_list, subscription_id)

        logging.info(f"Generated XML size: {len(xml_content)} characters")

        filename = f"azure_architecture_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.drawio"
        return func.HttpResponse(
            xml_content,
            status_code=200,
            mimetype="application/xml",
            headers={"Content-Disposition": f"attachment; filename={filename}"}
        )

    except Exception as e:
        logging.error(f"Export drawio error: {e!s}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            status_code=500,
            mimetype="application/json"
        )

# ========== Draw.IO 格式XML生成 ==========


def _generate_drawio_xml(resources_data, rg_list, subscription_id):
    """纯 Python 生成 draw.io XML，节点尺寸根据内容自适应"""
    import math
    import re
    import xml.etree.ElementTree as ET

    def sanitize(name):
        return re.sub(r'[^a-zA-Z0-9_-]', '_', str(name))[:60]

    # 按 region 分组 RG
    rg_by_region = {}
    for rg in rg_list:
        rg_by_region.setdefault(rg['location'], []).append(rg['name'])

    # 按 RG 分组资源
    resources_by_rg = {}
    for r in resources_data:
        rg = r.get('resourceGroup', 'Unknown')
        resources_by_rg.setdefault(rg, []).append(r)

    # ARM ID -> XML ID 映射
    id_map = {}
    for rg_name, res_list in resources_by_rg.items():
        for res in res_list:
            xml_id = f"{sanitize(rg_name)}_{sanitize(res['name'])}_{sanitize(res['type'].split('/')[-1])}"
            id_map[res['id']] = xml_id

    # 样式与映射
    STYLE_MAP = {
        # Compute
        'microsoft.compute/virtualmachines': ('#81D4FA', '#0277BD'),
        'microsoft.compute/virtualmachinescalesets': ('#81D4FA', '#0277BD'),
        'microsoft.containerservice/managedclusters': ('#B2DFDB', '#00695C'),
        'microsoft.containerinstance/containergroups': ('#C5CAE9', '#283593'),
        'microsoft.app/containerapps': ('#C5CAE9', '#283593'),
        'microsoft.app/managedenvironments': ('#C5CAE9', '#283593'),
        'microsoft.batch/batchaccounts': ('#81D4FA', '#0277BD'),
        'microsoft.servicefabric/clusters': ('#C5CAE9', '#283593'),
        # Web
        'microsoft.web/sites': ('#A5D6A7', '#2E7D32'),
        'microsoft.web/staticsites': ('#81C784', '#2E7D32'),
        'microsoft.web/serverfarms': ('#C8E6C9', '#388E3C'),
        'microsoft.web/certificates': ('#C8E6C9', '#388E3C'),
        'microsoft.logic/workflows': ('#B3E5FC', '#01579B'),
        'microsoft.logic/integrationserviceenvironments': ('#B3E5FC', '#01579B'),
        # Networking
        'microsoft.network/virtualnetworks': ('#90CAF9', '#1565C0'),
        'microsoft.network/networksecuritygroups': ('#FFECB3', '#FF8F00'),
        'microsoft.network/virtualnetworkgateways': ('#90CAF9', '#1565C0'),
        'microsoft.network/connections': ('#90CAF9', '#1565C0'),
        'microsoft.network/loadbalancers': ('#FFECB3', '#FF8F00'),
        'microsoft.network/applicationgateways': ('#FFECB3', '#FF8F00'),
        'microsoft.network/frontdoors': ('#FFE0B2', '#E65100'),
        'microsoft.network/frontdoorwebapplicationfirewallpolicies': ('#FFCDD2', '#B71C1C'),
        'microsoft.network/publicipaddresses': ('#E1BEE7', '#7B1FA2'),
        'microsoft.network/networkinterfaces': ('#F3E5F5', '#7B1FA2'),
        'microsoft.network/privatednszones': ('#E1BEE7', '#7B1FA2'),
        'microsoft.network/dnszones': ('#E1BEE7', '#7B1FA2'),
        'microsoft.network/bastionhosts': ('#90CAF9', '#1565C0'),
        'microsoft.network/networkwatchers': ('#E0E0E0', '#616161'),
        'microsoft.network/firewallpolicies': ('#FFECB3', '#FF8F00'),
        'microsoft.network/azurefirewalls': ('#FFECB3', '#FF8F00'),
        'microsoft.cdn/profiles': ('#FFE0B2', '#E65100'),
        'microsoft.cdn/endpoints': ('#FFE0B2', '#E65100'),
        # Storage
        'microsoft.storage/storageaccounts': ('#FFCC80', '#E65100'),
        'microsoft.storagesync/storagesyncservices': ('#FFCC80', '#E65100'),
        # Database
        'microsoft.sql/servers': ('#B39DDB', '#4527A0'),
        'microsoft.sql/servers/databases': ('#B39DDB', '#4527A0'),
        'microsoft.dbforpostgresql/flexibleservers': ('#B39DDB', '#4527A0'),
        'microsoft.dbforpostgresql/servers': ('#B39DDB', '#4527A0'),
        'microsoft.dbformysql/flexibleservers': ('#B39DDB', '#4527A0'),
        'microsoft.dbformysql/servers': ('#B39DDB', '#4527A0'),
        'microsoft.documentdb/databaseaccounts': ('#B39DDB', '#4527A0'),  # Cosmos DB
        'microsoft.cache/redis': ('#B39DDB', '#4527A0'),
        'microsoft.search/searchservices': ('#B39DDB', '#4527A0'),
        # Integration / Messaging
        'microsoft.servicebus/namespaces': ('#FFE082', '#F57F17'),
        'microsoft.eventhub/namespaces': ('#FFE082', '#F57F17'),
        'microsoft.eventgrid/topics': ('#FFE082', '#F57F17'),
        'microsoft.eventgrid/systemtopics': ('#FFE082', '#F57F17'),
        'microsoft.eventgrid/domains': ('#FFE082', '#F57F17'),
        'microsoft.relay/namespaces': ('#FFE082', '#F57F17'),
        'microsoft.apimanagement/service': ('#80CBC4', '#00695C'),
        # Security / Identity
        'microsoft.keyvault/vaults': ('#CE93D8', '#6A1B9A'),
        'microsoft.managedidentity/userassignedidentities': ('#F3E5F5', '#7B1FA2'),
        'microsoft.authorization/policyassignments': ('#F3E5F5', '#7B1FA2'),
        'microsoft.security/pricings': ('#FFCDD2', '#B71C1C'),
        'microsoft.security/automations': ('#FFCDD2', '#B71C1C'),
        # Monitoring
        'microsoft.insights/components': ('#B3E5FC', '#01579B'),
        'microsoft.insights/actiongroups': ('#B3E5FC', '#01579B'),
        'microsoft.insights/activitylogalerts': ('#B3E5FC', '#01579B'),
        'microsoft.insights/metricalerts': ('#B3E5FC', '#01579B'),
        'microsoft.insights/workbooks': ('#B3E5FC', '#01579B'),
        'microsoft.operationalinsights/workspaces': ('#EEEEEE', '#424242'),
        'microsoft.alertsmanagement/smartdetectoralertrules': ('#B3E5FC', '#01579B'),
        # DevOps / Testing
        'microsoft.devtestlab/labs': ('#FFAB91', '#D84315'),
        'microsoft.loadtestservice/loadtests': ('#FFAB91', '#D84315'),
        # AI / Cognitive
        'microsoft.cognitiveservices/accounts': ('#CE93D8', '#6A1B9A'),
        'microsoft.machinelearningservices/workspaces': ('#CE93D8', '#6A1B9A'),
        'microsoft.openai/accounts': ('#CE93D8', '#6A1B9A'),
        # IoT
        'microsoft.devices/iothubs': ('#C5CAE9', '#283593'),
        'microsoft.devices/provisioningservices': ('#C5CAE9', '#283593'),
        'microsoft.timeseriesinsights/environments': ('#C5CAE9', '#283593'),
        # Misc
        'microsoft.resources/resourcegroups': ('#BBDEFB', '#1976D2'),
        'microsoft.resources/subscriptions': ('#BBDEFB', '#1976D2'),
        'microsoft.resources/deployments': ('#BBDEFB', '#1976D2'),
        'microsoft.recoveryservices/vaults': ('#FFCDD2', '#B71C1C'),
        'microsoft.automation/automationaccounts': ('#FFE082', '#F57F17'),
        'microsoft.appconfiguration/configurationstores': ('#C8E6C9', '#388E3C'),
    }


    ICON_MAP = {
        'microsoft.compute/virtualmachines': '🖥️',
        'microsoft.compute/virtualmachinescalesets': '🖥️',
        'microsoft.containerservice/managedclusters': '☸️',
        'microsoft.containerinstance/containergroups': '📦',
        'microsoft.app/containerapps': '📦',
        'microsoft.app/managedenvironments': '📦',
        'microsoft.batch/batchaccounts': '⚡',
        'microsoft.servicefabric/clusters': '🔷',
        'microsoft.web/sites': '🌐',
        'microsoft.web/staticsites': '⚡',
        'microsoft.web/serverfarms': '📦',
        'microsoft.web/certificates': '🔐',
        'microsoft.logic/workflows': '🔀',
        'microsoft.logic/integrationserviceenvironments': '🔀',
        'microsoft.network/virtualnetworks': '🌐',
        'microsoft.network/networksecuritygroups': '🔒',
        'microsoft.network/virtualnetworkgateways': '🔐',
        'microsoft.network/connections': '🔗',
        'microsoft.network/loadbalancers': '⚖️',
        'microsoft.network/applicationgateways': '🛡️',
        'microsoft.network/frontdoors': '🚪',
        'microsoft.network/frontdoorwebapplicationfirewallpolicies': '🛡️',
        'microsoft.network/publicipaddresses': '🌐',
        'microsoft.network/networkinterfaces': '🔌',
        'microsoft.network/privatednszones': '📇',
        'microsoft.network/dnszones': '📇',
        'microsoft.network/bastionhosts': '🏰',
        'microsoft.network/networkwatchers': '🔍',
        'microsoft.network/firewallpolicies': '🔥',
        'microsoft.network/azurefirewalls': '🔥',
        'microsoft.cdn/profiles': '🚀',
        'microsoft.cdn/endpoints': '🚀',
        'microsoft.storage/storageaccounts': '💾',
        'microsoft.storagesync/storagesyncservices': '💾',
        'microsoft.sql/servers': '🗄️',
        'microsoft.sql/servers/databases': '🗄️',
        'microsoft.dbforpostgresql/flexibleservers': '🐘',
        'microsoft.dbforpostgresql/servers': '🐘',
        'microsoft.dbformysql/flexibleservers': '🐬',
        'microsoft.dbformysql/servers': '🐬',
        'microsoft.documentdb/databaseaccounts': '🌍',
        'microsoft.cache/redis': '🔴',
        'microsoft.search/searchservices': '🔍',
        'microsoft.servicebus/namespaces': '📨',
        'microsoft.eventhub/namespaces': '📊',
        'microsoft.eventgrid/topics': '📨',
        'microsoft.eventgrid/systemtopics': '📨',
        'microsoft.eventgrid/domains': '📨',
        'microsoft.relay/namespaces': '📡',
        'microsoft.apimanagement/service': '🔗',
        'microsoft.keyvault/vaults': '🔑',
        'microsoft.managedidentity/userassignedidentities': '🪪',
        'microsoft.authorization/policyassignments': '📋',
        'microsoft.security/pricings': '🛡️',
        'microsoft.security/automations': '🤖',
        'microsoft.insights/components': '📊',
        'microsoft.insights/actiongroups': '🔔',
        'microsoft.insights/activitylogalerts': '🔔',
        'microsoft.insights/metricalerts': '📈',
        'microsoft.insights/workbooks': '📓',
        'microsoft.operationalinsights/workspaces': '📋',
        'microsoft.alertsmanagement/smartdetectoralertrules': '🧠',
        'microsoft.devtestlab/labs': '🧪',
        'microsoft.loadtestservice/loadtests': '🧪',
        'microsoft.cognitiveservices/accounts': '🧠',
        'microsoft.machinelearningservices/workspaces': '🤖',
        'microsoft.openai/accounts': '🧠',
        'microsoft.devices/iothubs': '📡',
        'microsoft.devices/provisioningservices': '📡',
        'microsoft.timeseriesinsights/environments': '📈',
        'microsoft.resources/resourcegroups': '📁',
        'microsoft.resources/subscriptions': '📋',
        'microsoft.resources/deployments': '🚀',
        'microsoft.recoveryservices/vaults': '🔄',
        'microsoft.automation/automationaccounts': '⚙️',
        'microsoft.appconfiguration/configurationstores': '⚙️',
    }

    LABEL_MAP = {
         'microsoft.compute/virtualmachines': 'VM',
        'microsoft.compute/virtualmachinescalesets': 'VMSS',
        'microsoft.containerservice/managedclusters': 'AKS',
        'microsoft.containerinstance/containergroups': 'ACI',
        'microsoft.app/containerapps': 'Container App',
        'microsoft.app/managedenvironments': 'Container Env',
        'microsoft.batch/batchaccounts': 'Batch',
        'microsoft.servicefabric/clusters': 'Service Fabric',
        'microsoft.web/sites': 'Web App',
        'microsoft.web/staticsites': 'Static Web',
        'microsoft.web/serverfarms': 'App Plan',
        'microsoft.web/certificates': 'Cert',
        'microsoft.logic/workflows': 'Logic App',
        'microsoft.logic/integrationserviceenvironments': 'ISE',
        'microsoft.network/virtualnetworks': 'VNet',
        'microsoft.network/networksecuritygroups': 'NSG',
        'microsoft.network/virtualnetworkgateways': 'VPN GW',
        'microsoft.network/connections': 'Conn',
        'microsoft.network/loadbalancers': 'Load Balancer',
        'microsoft.network/applicationgateways': 'App GW',
        'microsoft.network/frontdoors': 'Front Door',
        'microsoft.network/frontdoorwebapplicationfirewallpolicies': 'WAF Policy',
        'microsoft.network/publicipaddresses': 'Public IP',
        'microsoft.network/networkinterfaces': 'NIC',
        'microsoft.network/privatednszones': 'Private DNS',
        'microsoft.network/dnszones': 'DNS Zone',
        'microsoft.network/bastionhosts': 'Bastion',
        'microsoft.network/networkwatchers': 'Net Watcher',
        'microsoft.network/firewallpolicies': 'FW Policy',
        'microsoft.network/azurefirewalls': 'Firewall',
        'microsoft.cdn/profiles': 'CDN Profile',
        'microsoft.cdn/endpoints': 'CDN Endpoint',
        'microsoft.storage/storageaccounts': 'Storage',
        'microsoft.storagesync/storagesyncservices': 'Sync',
        'microsoft.sql/servers': 'SQL Server',
        'microsoft.sql/servers/databases': 'SQL DB',
        'microsoft.dbforpostgresql/flexibleservers': 'PostgreSQL',
        'microsoft.dbforpostgresql/servers': 'PostgreSQL',
        'microsoft.dbformysql/flexibleservers': 'MySQL',
        'microsoft.dbformysql/servers': 'MySQL',
        'microsoft.documentdb/databaseaccounts': 'Cosmos DB',
        'microsoft.cache/redis': 'Redis',
        'microsoft.search/searchservices': 'Cognitive Search',
        'microsoft.servicebus/namespaces': 'Service Bus',
        'microsoft.eventhub/namespaces': 'Event Hubs',
        'microsoft.eventgrid/topics': 'Event Grid',
        'microsoft.eventgrid/systemtopics': 'Event Grid',
        'microsoft.eventgrid/domains': 'Event Grid',
        'microsoft.relay/namespaces': 'Relay',
        'microsoft.apimanagement/service': 'APIM',
        'microsoft.keyvault/vaults': 'Key Vault',
        'microsoft.managedidentity/userassignedidentities': 'Managed ID',
        'microsoft.authorization/policyassignments': 'Policy',
        'microsoft.security/pricings': 'Security',
        'microsoft.security/automations': 'Security',
        'microsoft.insights/components': 'App Insights',
        'microsoft.insights/actiongroups': 'Action Group',
        'microsoft.insights/activitylogalerts': 'Alert',
        'microsoft.insights/metricalerts': 'Metric Alert',
        'microsoft.insights/workbooks': 'Workbook',
        'microsoft.operationalinsights/workspaces': 'Log Analytics',
        'microsoft.alertsmanagement/smartdetectoralertrules': 'Smart Detect',
        'microsoft.devtestlab/labs': 'DevTest Lab',
        'microsoft.loadtestservice/loadtests': 'Load Test',
        'microsoft.cognitiveservices/accounts': 'Cognitive',
        'microsoft.machinelearningservices/workspaces': 'ML Workspace',
        'microsoft.openai/accounts': 'OpenAI',
        'microsoft.devices/iothubs': 'IoT Hub',
        'microsoft.devices/provisioningservices': 'DPS',
        'microsoft.timeseriesinsights/environments': 'TSI',
        'microsoft.resources/resourcegroups': 'Resource Group',
        'microsoft.resources/subscriptions': 'Subscription',
        'microsoft.resources/deployments': 'Deployment',
        'microsoft.recoveryservices/vaults': 'RSV',
        'microsoft.automation/automationaccounts': 'Automation',
        'microsoft.appconfiguration/configurationstores': 'App Config',
    }

    REGION_COLORS = {
        # Europe
        'Norway East': ('#E3F2FD', '#1565C0'),
        'Sweden Central': ('#FFF3E0', '#E65100'),
        'West Europe': ('#E8F5E9', '#2E7D32'),
        'North Europe': ('#F3E5F5', '#7B1FA2'),
        'UK South': ('#E0F2F1', '#00695C'),
        'UK West': ('#E0F2F1', '#00695C'),
        'France Central': ('#E8EAF6', '#283593'),
        'Germany West Central': ('#E8EAF6', '#283593'),
        'Switzerland North': ('#E8EAF6', '#283593'),
        'Poland Central': ('#E8EAF6', '#283593'),
        # Americas
        'East US': ('#F3E5F5', '#7B1FA2'),
        'East US 2': ('#F3E5F5', '#7B1FA2'),
        'West US': ('#F3E5F5', '#7B1FA2'),
        'West US 2': ('#F3E5F5', '#7B1FA2'),
        'West US 3': ('#F3E5F5', '#7B1FA2'),
        'Central US': ('#F3E5F5', '#7B1FA2'),
        'North Central US': ('#F3E5F5', '#7B1FA2'),
        'South Central US': ('#F3E5F5', '#7B1FA2'),
        'Brazil South': ('#E8F5E9', '#2E7D32'),
        'Canada Central': ('#E0F2F1', '#00695C'),
        'Canada East': ('#E0F2F1', '#00695C'),
        # Asia Pacific
        'Southeast Asia': ('#E0F2F1', '#00695C'),
        'East Asia': ('#E0F2F1', '#00695C'),
        'Japan East': ('#FFF3E0', '#E65100'),
        'Japan West': ('#FFF3E0', '#E65100'),
        'Australia East': ('#E3F2FD', '#1565C0'),
        'Australia Southeast': ('#E3F2FD', '#1565C0'),
        'Korea Central': ('#E8EAF6', '#283593'),
        'Korea South': ('#E8EAF6', '#283593'),
        'Central India': ('#FFF3E0', '#E65100'),
        'South India': ('#FFF3E0', '#E65100'),
        'West India': ('#FFF3E0', '#E65100'),
        # Middle East / Africa
        'UAE North': ('#E8F5E9', '#2E7D32'),
        'South Africa North': ('#E8F5E9', '#2E7D32'),
        'Qatar Central': ('#E8F5E9', '#2E7D32'),
        # Global
        'Global': ('#ECEFF1', '#546E7A'),
    }

    def get_region_colors(region):
        return REGION_COLORS.get(region, ('#ECEFF1', '#546E7A'))

    def get_res_style(res):
        t = res.get('type', '').lower()
        kind = res.get('kind', '') or ''
        if t == 'microsoft.web/sites' and 'functionapp' in kind:
            return ('#FFE082', '#F57F17'), '⚡', 'Function App'
        fill, stroke = STYLE_MAP.get(t, ('#E0E0E0', '#616161'))
        icon = ICON_MAP.get(t, '🔹')
        label = LABEL_MAP.get(t, t.split('/')[-1])
        return (fill, stroke), icon, label

    # ========== 自适应布局参数 ==========
    COL_GAP = 10           # 节点水平间距
    ROW_GAP = 8            # 节点垂直间距（额外）
    RG_PAD_X = 12          # RG 左右内边距
    RG_TITLE_H = 30        # RG swimlane 标题栏高度（draw.io 默认）
    RG_PAD_BOTTOM = 6      # RG 底部留白
    REGION_TITLE_H = 30    # 区域 swimlane 标题栏高度
    REGION_PAD_TOP = 35    # 区域标题栏下方的起始 y（必须 > 30）
    REGION_PAD_BOTTOM = 10 # 区域底部留白
    RG_GAP = 6             # RG 之间间距
    COLS_PER_ROW = 8       # 每行最多节点数
    NODE_MIN_W = 90
    NODE_MAX_W = 170
    NODE_FONT_SIZE = 9

    def calc_node_size(name, label):
        """根据文字长度计算节点宽高，避免溢出"""
        w = min(NODE_MAX_W, max(NODE_MIN_W, int(len(name) * 6.2 + 22)))
        chars_per_line = max(8, int((w - 8) / 6.5))
        name_lines = max(1, math.ceil(len(name) / chars_per_line))
        total_lines = 1 + name_lines
        h = max(32, min(68, 8 + total_lines * 11 + 4))
        return w, h

    # 构建 XML
    mxfile = ET.Element('mxfile')
    mxfile.set('host', 'app.diagrams.net')
    mxfile.set('agent', 'Azure Assistant')
    mxfile.set('version', '21.0.0')

    diagram = ET.SubElement(mxfile, 'diagram')
    diagram.set('name', 'Azure Architecture')

    mxGraphModel = ET.SubElement(diagram, 'mxGraphModel')
    mxGraphModel.set('dx', '1600')
    mxGraphModel.set('dy', '1000')
    mxGraphModel.set('grid', '1')
    mxGraphModel.set('gridSize', '10')
    mxGraphModel.set('guides', '1')
    mxGraphModel.set('tooltips', '1')
    mxGraphModel.set('connect', '1')
    mxGraphModel.set('arrows', '1')
    mxGraphModel.set('fold', '1')
    mxGraphModel.set('page', '1')
    mxGraphModel.set('pageScale', '1')
    mxGraphModel.set('pageWidth', '1800')
    mxGraphModel.set('pageHeight', '1400')
    mxGraphModel.set('math', '0')
    mxGraphModel.set('shadow', '0')

    root = ET.SubElement(mxGraphModel, 'root')

    c0 = ET.SubElement(root, 'mxCell')
    c0.set('id', '0')
    c1 = ET.SubElement(root, 'mxCell')
    c1.set('id', '1')
    c1.set('parent', '0')

    # 标题
    title = ET.SubElement(root, 'mxCell')
    title.set('id', 'title')
    title.set('value', f'Azure Infrastructure Architecture\nSubscription: {subscription_id[:8]}...')
    title.set('style', 'text;html=1;strokeColor=none;fillColor=none;align=center;verticalAlign=middle;whiteSpace=wrap;rounded=0;fontSize=22;fontStyle=1;fontColor=#0078D4;')
    title.set('vertex', '1')
    title.set('parent', '1')
    g = ET.SubElement(title, 'mxGeometry')
    g.set('x', '600')
    g.set('y', '10')
    g.set('width', '600')
    g.set('height', '40')
    g.set('as', 'geometry')

    y_cursor = 70
    edge_counter = [0]

    def add_edge(src_arm_id, dst_arm_id, color='#1565C0', dashed='0'):
        if src_arm_id not in id_map or dst_arm_id not in id_map:
            return
        src = id_map[src_arm_id]
        dst = id_map[dst_arm_id]
        edge_counter[0] += 1
        eid = f'edge_{edge_counter[0]}'
        d_str = 'dashed=1;' if dashed == '1' else ''
        edge = ET.SubElement(root, 'mxCell')
        edge.set('id', eid)
        edge.set('style', f'edgeStyle=orthogonalEdgeStyle;rounded=1;orthogonalLoop=1;jettySize=auto;html=1;strokeColor={color};strokeWidth=1.5;exitX=1;exitY=0.5;exitDx=0;exitDy=0;entryX=0;entryY=0.5;entryDx=0;entryDy=0;{d_str}')
        edge.set('edge', '1')
        edge.set('parent', '1')
        edge.set('source', src)
        edge.set('target', dst)
        g = ET.SubElement(edge, 'mxGeometry')
        g.set('relative', '1')
        g.set('as', 'geometry')

    # 生成区域和资源节点
    for region in sorted(rg_by_region.keys()):
        rgs_in_region = rg_by_region[region]
        fill, stroke = get_region_colors(region)

        # 计算每个 RG 尺寸
        rg_layouts = {}
        total_rg_height = 0
        for rg_name in rgs_in_region:
            res_list = resources_by_rg.get(rg_name, [])
            if not res_list:
                # 空 RG：只有标题栏
                rg_layouts[rg_name] = {'width': 300, 'height': RG_TITLE_H, 'nodes': []}
                total_rg_height += RG_TITLE_H
                continue

            # 按行分组，计算每行尺寸
            rows = []
            current_row = []
            row_width = RG_PAD_X
            max_row_w = RG_PAD_X

            for res in res_list:
                (fc, sc), icon, label = get_res_style(res)
                w, h = calc_node_size(res['name'], label)
                if len(current_row) >= COLS_PER_ROW or (row_width + w + COL_GAP > 1600 and current_row):
                    rows.append(current_row)
                    max_row_w = max(max_row_w, row_width)
                    current_row = []
                    row_width = RG_PAD_X

                current_row.append({
                    'res': res, 'w': w, 'h': h, 'fill': fc, 'stroke': sc,
                    'icon': icon, 'label': label
                })
                row_width += w + COL_GAP

            if current_row:
                rows.append(current_row)
                max_row_w = max(max_row_w, row_width)

            # 计算 RG 内容高度
            rg_content_h = 0
            for row in rows:
                row_h = max(n['h'] for n in row) + ROW_GAP
                rg_content_h += row_h
            rg_content_h -= ROW_GAP  # 最后一行不需要底部间距

            rg_w = min(1600, max(320, max_row_w + RG_PAD_X))
            # RG 高度 = 标题栏 + 内容 + 底部留白
            rg_h = RG_TITLE_H + rg_content_h + RG_PAD_BOTTOM

            rg_layouts[rg_name] = {'width': rg_w, 'height': rg_h, 'nodes': rows}
            total_rg_height += rg_h

        # 区域高度 = 标题栏 + 顶部留白 + 所有 RG 高度 + RG 间距 + 底部留白
        n_rgs = len(rgs_in_region)
        rg_spacing_total = max(0, (n_rgs - 1) * RG_GAP)
        region_height = REGION_TITLE_H + REGION_PAD_TOP + total_rg_height + rg_spacing_total + REGION_PAD_BOTTOM

        # 区域 swimlane
        region_id = f"region_{sanitize(region)}"
        region_cell = ET.SubElement(root, 'mxCell')
        region_cell.set('id', region_id)
        region_cell.set('value', f'📍 {region}')
        region_cell.set('style', f'swimlane;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};fontStyle=1;fontSize=14;rounded=1;shadow=1;startSize={REGION_TITLE_H};')
        region_cell.set('vertex', '1')
        region_cell.set('parent', '1')
        g = ET.SubElement(region_cell, 'mxGeometry')
        g.set('x', '40')
        g.set('y', str(y_cursor))
        g.set('width', '1700')
        g.set('height', str(region_height))
        g.set('as', 'geometry')

        # RG swimlanes
        rg_y = REGION_PAD_TOP
        for rg_name in rgs_in_region:
            layout = rg_layouts[rg_name]
            rg_w = layout['width']
            rg_h = layout['height']

            rg_id = f"rg_{sanitize(rg_name)}"
            rg_cell = ET.SubElement(root, 'mxCell')
            rg_cell.set('id', rg_id)
            rg_cell.set('value', f'📁 {rg_name}')
            rg_cell.set('style', f'swimlane;whiteSpace=wrap;html=1;fillColor=#BBDEFB;strokeColor=#1976D2;fontSize=12;fontStyle=1;rounded=1;startSize={RG_TITLE_H};')
            rg_cell.set('vertex', '1')
            rg_cell.set('parent', region_id)
            g = ET.SubElement(rg_cell, 'mxGeometry')
            g.set('x', '20')
            g.set('y', str(rg_y))
            g.set('width', str(rg_w))
            g.set('height', str(rg_h))
            g.set('as', 'geometry')

            # 放置节点（从 RG 标题栏下方开始）
            if layout['nodes']:
                node_y = RG_TITLE_H + 4  # 标题栏下方留 4px
                for row in layout['nodes']:
                    node_x = RG_PAD_X
                    row_h = max(n['h'] for n in row) + ROW_GAP
                    for n in row:
                        res = n['res']
                        xml_id = id_map[res['id']]

                        cell = ET.SubElement(root, 'mxCell')
                        cell.set('id', xml_id)
                        cell.set('value', f'<b>{n["icon"]} {n["label"]}</b><br>{res["name"]}')
                        cell.set('style', f'rounded=1;whiteSpace=wrap;html=1;fillColor={n["fill"]};strokeColor={n["stroke"]};fontSize={NODE_FONT_SIZE};shadow=1;')
                        cell.set('vertex', '1')
                        cell.set('parent', rg_id)
                        g = ET.SubElement(cell, 'mxGeometry')
                        g.set('x', str(node_x))
                        g.set('y', str(node_y))
                        g.set('width', str(n['w']))
                        g.set('height', str(n['h']))
                        g.set('as', 'geometry')

                        node_x += n['w'] + COL_GAP
                    node_y += row_h - ROW_GAP  # 减去 ROW_GAP 因为行内已加

            rg_y += rg_h + RG_GAP

        y_cursor += region_height + 10

    # 连接边（保持不变）
    for rg_name, res_list in resources_by_rg.items():
        vnets = [r for r in res_list if r['type'].lower() == 'microsoft.network/virtualnetworks']
        vms = [r for r in res_list if r['type'].lower() == 'microsoft.compute/virtualmachines']
        webapps = [r for r in res_list if r['type'].lower() == 'microsoft.web/sites' and 'functionapp' not in (r.get('kind') or '')]
        funcs = [r for r in res_list if r['type'].lower() == 'microsoft.web/sites' and 'functionapp' in (r.get('kind') or '')]
        statics = [r for r in res_list if r['type'].lower() == 'microsoft.web/staticsites']
        storages = [r for r in res_list if r['type'].lower() == 'microsoft.storage/storageaccounts']
        kvs = [r for r in res_list if r['type'].lower() == 'microsoft.keyvault/vaults']
        insights = [r for r in res_list if r['type'].lower() == 'microsoft.insights/components']
        nics = [r for r in res_list if r['type'].lower() == 'microsoft.network/networkinterfaces']
        pips = [r for r in res_list if r['type'].lower() == 'microsoft.network/publicipaddresses']

        for vm in vms:
            for vnet in vnets:
                add_edge(vm['id'], vnet['id'], '#0277BD')

        for web in webapps + funcs + statics:
            for st in storages:
                add_edge(web['id'], st['id'], '#E65100', '1')

        for func in funcs:
            for kv in kvs:
                add_edge(func['id'], kv['id'], '#6A1B9A', '1')

        for ins in insights:
            for web in webapps + funcs:
                add_edge(ins['id'], web['id'], '#01579B', '1')

        for nic in nics:
            for pip in pips:
                add_edge(nic['id'], pip['id'], '#7B1FA2')

    all_web = [r for r in resources_data if r['type'].lower() in ('microsoft.web/sites', 'microsoft.web/staticsites')]
    frontdoors = [r for r in resources_data if r['type'].lower() == 'microsoft.network/frontdoors']
    apims = [r for r in resources_data if r['type'].lower() == 'microsoft.apimanagement/service']

    for fd in frontdoors:
        for web in all_web:
            add_edge(fd['id'], web['id'], '#E65100', '1')

    for apim in apims:
        for web in all_web:
            add_edge(apim['id'], web['id'], '#00695C', '1')

    # 图例
    legend_y = y_cursor + 15
    legend = ET.SubElement(root, 'mxCell')
    legend.set('id', 'legend')
    legend.set('value', '📋 LEGEND')
    legend.set('style', 'swimlane;whiteSpace=wrap;html=1;fillColor=#FAFAFA;strokeColor=#9E9E9E;fontStyle=1;fontSize=12;rounded=1;')
    legend.set('vertex', '1')
    legend.set('parent', '1')
    g = ET.SubElement(legend, 'mxGeometry')
    g.set('x', '40')
    g.set('y', str(legend_y))
    g.set('width', '1700')
    g.set('height', '65')
    g.set('as', 'geometry')

    legend_items = [
        ('VNet', '#90CAF9', '#1565C0'), ('VM', '#81D4FA', '#0277BD'),
        ('NSG', '#FFECB3', '#FF8F00'), ('Web App', '#A5D6A7', '#2E7D32'),
        ('Function', '#FFE082', '#F57F17'), ('Static Web', '#81C784', '#2E7D32'),
        ('Storage', '#FFCC80', '#E65100'), ('Key Vault', '#CE93D8', '#6A1B9A'),
        ('APIM', '#80CBC4', '#00695C'), ('Front Door', '#FFE0B2', '#E65100'),
        ('App Insights', '#B3E5FC', '#01579B'), ('Log Analytics', '#EEEEEE', '#424242'),
        ('AKS', '#B2DFDB', '#00695C'), ('SQL', '#B39DDB', '#4527A0'),
    ]

    for idx, (label, fill, stroke) in enumerate(legend_items):
        x = 15 + idx * 110
        item = ET.SubElement(root, 'mxCell')
        item.set('id', f'leg_{idx}')
        item.set('value', label)
        item.set('style', f'rounded=1;whiteSpace=wrap;html=1;fillColor={fill};strokeColor={stroke};fontSize=9;')
        item.set('vertex', '1')
        item.set('parent', 'legend')
        g = ET.SubElement(item, 'mxGeometry')
        g.set('x', str(x))
        g.set('y', '22')
        g.set('width', '95')
        g.set('height', '28')
        g.set('as', 'geometry')

    total_height = max(1400, legend_y + 120)
    mxGraphModel.set('pageHeight', str(total_height))

    xml_bytes = ET.tostring(mxfile, encoding='unicode')
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + xml_bytes
#=====（修改内容结束）=====


#=====（修改内容：新增通用 Chat Proxy，支持 / kimi / deepseek 三个 provider 的 chat、usage 持久化）=====
def _proxy_chat_completion(req: func.HttpRequest, provider: str) -> func.HttpResponse:
    provider = _normalize_provider(provider)

    try:
        req_body = req.get_json()

        messages = req_body.get("messages")
        if not messages:
            return func.HttpResponse(
                json.dumps({"error": "Missing 'messages'"}),
                status_code=400,
                mimetype="application/json"
            )

        model = req_body.get("model") or get_ai_model(provider)
        # 带图片的消息：DeepSeek 自动切换为视觉模型（Kimi 原生支持，无需切换）
        if provider == "deepseek" and not req_body.get("model") \
                and _messages_contain_images(messages):
            model = CONFIG["DEEPSEEK_VISION_MODEL_NAME"]
        temperature = req_body.get("temperature", 0.7)
        stream = bool(req_body.get("stream", False))

        client = get_ai_client(provider)

        if stream:
            base_kwargs = {
                "model": model,
                "messages": messages,
                "temperature": temperature,
                "stream": True,
            }

            try:
                completion = client.chat.completions.create(
                    stream_options={"include_usage": True},
                    **base_kwargs
                )
            except Exception:
                completion = client.chat.completions.create(**base_kwargs)

            usage_data = None

            for chunk in completion:
                if getattr(chunk, "usage", None):
                    usage_data = chunk.usage

            if usage_data:
                _persist_usage(
                    provider=provider,
                    model=model,
                    prompt_tokens=usage_data.prompt_tokens,
                    completion_tokens=usage_data.completion_tokens,
                    total_tokens=usage_data.total_tokens,
                    endpoint="chat.completions.stream"
                )

                record = {
                    "timestamp": datetime.datetime.utcnow().isoformat(),
                    "model": model,
                    "prompt_tokens": usage_data.prompt_tokens,
                    "completion_tokens": usage_data.completion_tokens,
                    "total_tokens": usage_data.total_tokens,
                    "endpoint": "chat.completions.stream"
                }

                _append_memory_record(provider, record)

            return func.HttpResponse(
                json.dumps(
                    {
                        "status": "success",
                        "provider": provider,
                        "message": "Stream completed",
                        "usage": _usage_to_dict(usage_data)
                    },
                    ensure_ascii=False
                ),
                status_code=200,
                mimetype="application/json"
            )

        else:
            completion = client.chat.completions.create(
                model=model,
                messages=messages,
                temperature=temperature,
                stream=False
            )

            usage = completion.usage

            if usage:
                _persist_usage(
                    provider=provider,
                    model=model,
                    prompt_tokens=usage.prompt_tokens,
                    completion_tokens=usage.completion_tokens,
                    total_tokens=usage.total_tokens,
                    endpoint="chat.completions"
                )

                record = {
                    "timestamp": datetime.datetime.utcnow().isoformat(),
                    "model": model,
                    "prompt_tokens": usage.prompt_tokens,
                    "completion_tokens": usage.completion_tokens,
                    "total_tokens": usage.total_tokens,
                    "endpoint": "chat.completions"
                }

                _append_memory_record(provider, record)

            return func.HttpResponse(
                json.dumps(
                    {
                        "status": "success",
                        "provider": provider,
                        "content": completion.choices[0].message.content,
                        "usage": _usage_to_dict(usage)
                    },
                    ensure_ascii=False
                ),
                status_code=200,
                mimetype="application/json"
            )

    except Exception as e:
        logging.error(f"{provider} chat error: {e!s}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}),
            status_code=500,
            mimetype="application/json"
        )


def _usage_export(provider: str, hours: int) -> func.HttpResponse:
    provider = _normalize_provider(provider)

    recent = _query_usage_records(provider, hours)

    if not recent:
        cutoff = datetime.datetime.utcnow() - datetime.timedelta(hours=hours)
        memory_records = _get_memory_records(provider)
        recent = [r for r in memory_records if r.get("timestamp", "") >= cutoff.isoformat()]

    output = StringIO()
    writer = csv.writer(output)
    writer.writerow(["Timestamp", "Model", "Endpoint", "Prompt Tokens", "Completion Tokens", "Total Tokens"])

    for r in recent:
        writer.writerow([
            r.get("timestamp", ""),
            r.get("model", "unknown"),
            r.get("endpoint", "unknown"),
            r.get("prompt_tokens", 0),
            r.get("completion_tokens", 0),
            r.get("total_tokens", 0)
        ])

    csv_content = output.getvalue()
    output.close()

    return func.HttpResponse(
        csv_content,
        status_code=200,
        mimetype="text/csv",
        headers={
            "Content-Disposition": f"attachment; filename={provider}_usage_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}.csv"
        }
    )
#=====（修改内容结束）=====


# ========== Kimi 用量管理 HTTP 接口 ==========
#=====（修改内容：Kimi HTTP 接口改为统一 JWT 校验和通用 chat/usage 函数）=====
@app.route(route="kimi_balance", methods=[func.HttpMethod.GET], auth_level=func.AuthLevel.ANONYMOUS)
def kimi_balance(req: func.HttpRequest) -> func.HttpResponse:
    """查询 Kimi API 账户余额"""
    logging.info("Kimi balance query triggered.")

    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    try:
        result = execute_kimi_tool("kimi_get_balance", {})
        return func.HttpResponse(result, status_code=200, mimetype="application/json")
    except Exception as e:
        logging.error(f"Kimi balance error: {e!s}")
        return func.HttpResponse(json.dumps({"error": str(e)}), status_code=500, mimetype="application/json")


@app.route(route="kimi_estimate", methods=[func.HttpMethod.POST], auth_level=func.AuthLevel.ANONYMOUS)
def kimi_estimate(req: func.HttpRequest) -> func.HttpResponse:
    """预估 Kimi Token 消耗"""
    logging.info("Kimi token estimate triggered.")

    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    try:
        req_body = req.get_json()
        messages = req_body.get("messages")
        model = req_body.get("model", CONFIG["AI_MODEL_NAME"])

        if not messages:
            return func.HttpResponse(
                json.dumps({"error": "Missing 'messages'"}),
                status_code=400,
                mimetype="application/json"
            )

        result = execute_kimi_tool("kimi_estimate_tokens", {"messages": messages, "model": model})
        return func.HttpResponse(result, status_code=200, mimetype="application/json")

    except Exception as e:
        logging.error(f"Kimi estimate error: {e!s}")
        return func.HttpResponse(json.dumps({"error": str(e)}), status_code=500, mimetype="application/json")


@app.route(route="kimi_chat", methods=[func.HttpMethod.POST], auth_level=func.AuthLevel.ANONYMOUS)
def kimi_chat(req: func.HttpRequest) -> func.HttpResponse:
    """代理 Kimi Chat Completion，同时记录用量"""
    logging.info("Kimi chat proxy triggered.")

    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    return _proxy_chat_completion(req, "kimi")


@app.route(route="kimi_usage", methods=[func.HttpMethod.GET], auth_level=func.AuthLevel.ANONYMOUS)
def kimi_usage(req: func.HttpRequest) -> func.HttpResponse:
    """查询本地记录的 Kimi 用量历史"""
    logging.info("Kimi usage history triggered.")

    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    try:
        hours = int(req.params.get("hours", "24"))
        result = execute_kimi_tool("kimi_get_usage_summary", {"hours": hours})
        return func.HttpResponse(result, status_code=200, mimetype="application/json")
    except Exception as e:
        logging.error(f"Kimi usage error: {e!s}")
        return func.HttpResponse(json.dumps({"error": str(e)}), status_code=500, mimetype="application/json")


@app.route(route="kimi_usage_export", methods=[func.HttpMethod.GET], auth_level=func.AuthLevel.ANONYMOUS)
def kimi_usage_export(req: func.HttpRequest) -> func.HttpResponse:
    """导出 Kimi 用量历史为 CSV"""
    logging.info("Kimi usage export triggered.")

    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    try:
        hours = int(req.params.get("hours", "24"))
        return _usage_export("kimi", hours)
    except Exception as e:
        logging.error(f"Kimi usage export error: {e!s}")
        return func.HttpResponse(json.dumps({"error": str(e)}), status_code=500, mimetype="application/json")
#=====（修改内容结束）=====


# ========== DeepSeek 用量管理 HTTP 接口 ==========
#=====（修改内容：DeepSeek HTTP 接口改为统一 JWT 校验和通用 chat/usage 函数）=====
@app.route(route="deepseek_balance", methods=[func.HttpMethod.GET], auth_level=func.AuthLevel.ANONYMOUS)
def deepseek_balance(req: func.HttpRequest) -> func.HttpResponse:
    """查询 DeepSeek API 账户余额"""
    logging.info("DeepSeek balance query triggered.")

    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    try:
        result = execute_deepseek_tool("deepseek_get_balance", {})
        return func.HttpResponse(result, status_code=200, mimetype="application/json")
    except Exception as e:
        logging.error(f"DeepSeek balance error: {e!s}")
        return func.HttpResponse(json.dumps({"error": str(e)}), status_code=500, mimetype="application/json")


@app.route(route="deepseek_estimate", methods=[func.HttpMethod.POST], auth_level=func.AuthLevel.ANONYMOUS)
def deepseek_estimate(req: func.HttpRequest) -> func.HttpResponse:
    """预估 DeepSeek Token 消耗"""
    logging.info("DeepSeek token estimate triggered.")

    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    try:
        req_body = req.get_json()
        messages = req_body.get("messages")
        model = req_body.get("model", CONFIG["DEEPSEEK_MODEL_NAME"])

        if not messages:
            return func.HttpResponse(
                json.dumps({"error": "Missing 'messages'"}),
                status_code=400,
                mimetype="application/json"
            )

        result = execute_deepseek_tool("deepseek_estimate_tokens", {"messages": messages, "model": model})
        return func.HttpResponse(result, status_code=200, mimetype="application/json")

    except Exception as e:
        logging.error(f"DeepSeek estimate error: {e!s}")
        return func.HttpResponse(json.dumps({"error": str(e)}), status_code=500, mimetype="application/json")


@app.route(route="deepseek_chat", methods=[func.HttpMethod.POST], auth_level=func.AuthLevel.ANONYMOUS)
def deepseek_chat(req: func.HttpRequest) -> func.HttpResponse:
    """代理 DeepSeek Chat Completion，同时记录用量"""
    logging.info("DeepSeek chat proxy triggered.")

    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    return _proxy_chat_completion(req, "deepseek")


@app.route(route="deepseek_usage", methods=[func.HttpMethod.GET], auth_level=func.AuthLevel.ANONYMOUS)
def deepseek_usage(req: func.HttpRequest) -> func.HttpResponse:
    """查询本地记录的 DeepSeek 用量历史"""
    logging.info("DeepSeek usage history triggered.")

    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    try:
        hours = int(req.params.get("hours", "24"))
        result = execute_deepseek_tool("deepseek_get_usage_summary", {"hours": hours})
        return func.HttpResponse(result, status_code=200, mimetype="application/json")
    except Exception as e:
        logging.error(f"DeepSeek usage error: {e!s}")
        return func.HttpResponse(json.dumps({"error": str(e)}), status_code=500, mimetype="application/json")


@app.route(route="deepseek_usage_export", methods=[func.HttpMethod.GET], auth_level=func.AuthLevel.ANONYMOUS)
def deepseek_usage_export(req: func.HttpRequest) -> func.HttpResponse:
    """导出 DeepSeek 用量历史为 CSV"""
    logging.info("DeepSeek usage export triggered.")

    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    try:
        hours = int(req.params.get("hours", "24"))
        return _usage_export("deepseek", hours)
    except Exception as e:
        logging.error(f"DeepSeek usage export error: {e!s}")
        return func.HttpResponse(json.dumps({"error": str(e)}), status_code=500, mimetype="application/json")
#=====（修改内容结束）=====


#=====（修改内容：/api_usage 支持 provider 参数，可查询 / kimi / deepseek）=====
@app.route(route="api_usage", methods=[func.HttpMethod.GET], auth_level=func.AuthLevel.ANONYMOUS)
def api_usage_endpoint(req: func.HttpRequest) -> func.HttpResponse:
    """独立查询指定 Provider 的 API 用量与余额"""
    logging.info("API usage query triggered.")

    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    provider = req.params.get("provider", DEFAULT_AI_PROVIDER)

    return func.HttpResponse(
        _build_api_usage_response(provider),
        mimetype="application/json",
        status_code=200
    )
#=====（修改内容结束）=====


#=====（修改内容：新增 /ai_providers 接口，方便前端确认后端支持的三个 API）=====
@app.route(route="ai_providers", methods=[func.HttpMethod.GET], auth_level=func.AuthLevel.ANONYMOUS)
def ai_providers(req: func.HttpRequest) -> func.HttpResponse:
    """返回后端支持的 AI Provider 列表"""
    logging.info("AI providers query triggered.")

    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    providers = [
        {
            "id": "kimi",
            "name": "Kimi",
            "model": CONFIG["AI_MODEL_NAME"],
            "balance_endpoint_configured": True,
            "usage_endpoint_configured": True,
        },
        {
            "id": "deepseek",
            "name": "DeepSeek",
            "model": CONFIG["DEEPSEEK_MODEL_NAME"],
            "balance_endpoint_configured": True,
            "usage_endpoint_configured": True,
        }
    ]

    return func.HttpResponse(
        json.dumps(
            {
                "status": "success",
                "default_provider": _normalize_provider(DEFAULT_AI_PROVIDER),
                "providers": providers
            },
            ensure_ascii=False
        ),
        status_code=200,
        mimetype="application/json"
    )

#===== 跨租户读取 Tenant B Subscription X =====
@app.route(route="tenant_b_resources", methods=[func.HttpMethod.GET], auth_level=func.AuthLevel.ANONYMOUS)
def tenant_b_resources(req: func.HttpRequest) -> func.HttpResponse:
    """
    读取 Tenant B (SP) 中 Subscription X 的所有资源。
    使用 Service Principal + Azure REST API（已验证端到端通畅）。
    """
    logging.info("tenant_b_resources API triggered.")

    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    try:
        # 1. 读取环境变量
        secondary_tenant_id = os.environ.get("TENANT_SECONDARY_ID")
        client_id = os.environ.get("SP_CLIENT_ID")
        client_secret = os.environ.get("SP_CLIENT_SECRET")
        secondary_subscription_id = os.environ.get("SUBSCRIPTION_SECONDARY_ID")

        if not all([secondary_tenant_id, client_id, client_secret, secondary_subscription_id]):
            missing = [
                k for k, v in {
                    "TENANT_SECONDARY_ID": secondary_tenant_id,
                    "SP_CLIENT_ID": client_id,
                    "SP_CLIENT_SECRET": client_secret,
                    "SUBSCRIPTION_SECONDARY_ID": secondary_subscription_id
                }.items() if not v
            ]
            return func.HttpResponse(
                json.dumps({
                    "status": "error",
                    "message": f"Missing environment variables: {', '.join(missing)}"
                }),
                status_code=500,
                mimetype="application/json"
            )

        # 2. 获取 Access Token
        secondary_credential = ClientSecretCredential(
            tenant_id=secondary_tenant_id,
            client_id=client_id,
            client_secret=client_secret
        )
        token = secondary_credential.get_token("https://management.azure.com/.default")
        headers = {"Authorization": f"Bearer {token.token}"}

        # 3. 获取所有资源组
        logging.info("Fetching resource groups via REST API...")
        rg_url = f"https://management.azure.com/subscriptions/{secondary_subscription_id}/resourcegroups?api-version=2021-04-01"
        rg_resp = httpx.get(rg_url, headers=headers, timeout=30)
        rg_resp.raise_for_status()
        rg_data = rg_resp.json()

        resource_groups = []
        for rg in rg_data.get("value", []):
            resource_groups.append({
                "name": rg.get("name"),
                "location": rg.get("location"),
                "provisioning_state": rg.get("properties", {}).get("provisioningState"),
                "tags": rg.get("tags", {})
            })

        # 4. 获取所有资源（跨资源组）
        logging.info("Fetching all resources via REST API...")
        res_url = f"https://management.azure.com/subscriptions/{secondary_subscription_id}/resources?api-version=2021-04-01"
        res_resp = httpx.get(res_url, headers=headers, timeout=30)
        res_resp.raise_for_status()
        res_data = res_resp.json()

        all_resources = []
        for resource in res_data.get("value", []):
            # 从 ID 解析资源组: /subscriptions/.../resourceGroups/{rg}/providers/...
            parts = resource.get("id", "").split("/")
            rg_name = parts[4] if len(parts) >= 5 else None

            all_resources.append({
                "name": resource.get("name"),
                "type": resource.get("type"),
                "resource_group": rg_name,
                "location": resource.get("location"),
                "tags": resource.get("tags", {}),
                "provisioning_state": resource.get("properties", {}).get("provisioningState"),
                "created_time": resource.get("properties", {}).get("createdTime"),
                "changed_time": resource.get("properties", {}).get("changedTime"),
                "id": resource.get("id")
            })

        # 5. 返回结果
        result = {
            "status": "success",
            "subscription_id": secondary_subscription_id,
            "tenant_id": secondary_tenant_id,
            "resource_group_count": len(resource_groups),
            "resource_count": len(all_resources),
            "resource_groups": resource_groups,
            "resources": all_resources
        }

        logging.info(
            f"Tenant B scan complete: {len(resource_groups)} RGs, {len(all_resources)} resources"
        )

        return func.HttpResponse(
            json.dumps(result, indent=2, ensure_ascii=False),
            status_code=200,
            mimetype="application/json"
        )

    except ClientAuthenticationError as e:
        logging.error(f"Tenant B authentication failed: {e!s}")
        return func.HttpResponse(
            json.dumps({
                "status": "error",
                "message": "Authentication failed. Check SP credentials."
            }),
            status_code=401,
            mimetype="application/json"
        )
    except httpx.HTTPStatusError as e:
        logging.error(f"Tenant B HTTP error: {e.response.status_code} - {e.response.text[:500]}")
        return func.HttpResponse(
            json.dumps({
                "status": "error",
                "message": f"Azure API returned {e.response.status_code}: {e.response.text[:200]}"
            }),
            status_code=e.response.status_code,
            mimetype="application/json"
        )
    except Exception as e:
        logging.exception("Tenant B unexpected error")
        return func.HttpResponse(
            json.dumps({"status": "error", "message": str(e)}),
            status_code=500,
            mimetype="application/json"
        )


# ==================== Meraki 配置 ====================
# ==================== Meraki 配置 ====================

MERAKI_SECRET_NAME = os.environ.get("MERAKI_SECRET_NAME", "MERAKI-API-KEY")
MERAKI_BASE_URL = "https://api.meraki.com/api/v1"

def get_secret_from_keyvault(secret_name: str) -> str:
    """使用 Managed Identity 从 Azure Key Vault 获取指定 Secret"""
    try:
        credential = DefaultAzureCredential()
        client = SecretClient(vault_url=CONFIG["KEY_VAULT_URL"], credential=credential)
        secret = client.get_secret(secret_name)
        logging.info(f"Successfully retrieved secret '{secret_name}' from Key Vault")
        return secret.value
    except Exception as e:
        logging.error(f"Failed to retrieve secret '{secret_name}' from Key Vault: {e}")
        # 本地开发时可以从环境变量回退
        fallback_env_name = secret_name.replace("-", "_").upper() + "_FALLBACK"
        fallback = os.environ.get(fallback_env_name, "")
        if fallback:
            logging.warning(f"Using fallback from environment variable '{fallback_env_name}'")
        return fallback

# ==================== Meraki Dashboard API 客户端 ====================
class MerakiClient:
    def __init__(self, api_key: str):
        self.api_key = api_key
        self.base_url = MERAKI_BASE_URL
        self.headers = {
            "Authorization": f"Bearer {api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json"
        }

    def _request(self, method: str, endpoint: str, params: dict | None = None, data: dict | None = None) -> Any:
        url = f"{self.base_url}{endpoint}"
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=self.headers,
                params=params,
                json=data,
                timeout=30
            )
            response.raise_for_status()
            return response.json() if response.text else {}
        except requests.exceptions.RequestException as e:
            logging.error(f"Meraki API request failed: {e}")
            raise

    def get_organizations(self) -> list:
        return self._request("GET", "/organizations")

    def get_organization(self, org_id: str) -> dict:
        return self._request("GET", f"/organizations/{org_id}")

    def get_networks(self, org_id: str) -> list:
        return self._request("GET", f"/organizations/{org_id}/networks")

    def get_network_devices(self, network_id: str) -> list:
        return self._request("GET", f"/networks/{network_id}/devices")

    def get_device(self, serial: str) -> dict:
        return self._request("GET", f"/devices/{serial}")

    def get_vpn_status(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/vpn/status")

    def get_uplink_status(self, serial: str) -> dict:
        return self._request("GET", f"/devices/{serial}/uplinks")

    def get_vmx_licenses(self, org_id: str) -> list:
        licenses = self._request("GET", f"/organizations/{org_id}/licenses")
        return [lic for lic in licenses if "vmx" in lic.get("edition", "").lower()]
        # ========== Appliance (MX/Z) 只读端点 ==========

    def get_network_appliance_settings(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/settings")

    def get_network_appliance_vlans(self, network_id: str) -> list:
        return self._request("GET", f"/networks/{network_id}/appliance/vlans")

    def get_network_appliance_vlan(self, network_id: str, vlan_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/vlans/{vlan_id}")

    def get_network_appliance_ports(self, network_id: str) -> list:
        return self._request("GET", f"/networks/{network_id}/appliance/ports")

    def get_network_appliance_port(self, network_id: str, port_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/ports/{port_id}")

    def get_network_appliance_static_routes(self, network_id: str) -> list:
        return self._request("GET", f"/networks/{network_id}/appliance/staticRoutes")

    def get_network_appliance_single_lan(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/singleLan")

    def get_network_appliance_firewall_l3_rules(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/firewall/l3FirewallRules")

    def get_network_appliance_firewall_l7_rules(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/firewall/l7FirewallRules")

    def get_network_appliance_firewall_one_to_one_nat(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/firewall/oneToOneNatRules")

    def get_network_appliance_firewall_one_to_many_nat(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/firewall/oneToManyNatRules")

    def get_network_appliance_firewall_port_forwarding(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/firewall/portForwardingRules")

    def get_network_appliance_security_intrusion(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/security/intrusion")

    def get_network_appliance_security_malware(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/security/malware")

    def get_network_appliance_content_filtering(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/contentFiltering")

    def get_network_appliance_vpn_site_to_site(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/vpn/siteToSiteVpn")

    def get_network_appliance_vpn_bgp(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/vpn/bgp")

    def get_network_appliance_traffic_shaping(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/trafficShaping")

    def get_network_appliance_traffic_shaping_rules(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/trafficShaping/rules")

    def get_network_appliance_traffic_shaping_uplink_bandwidth(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/trafficShaping/uplinkBandwidth")

    def get_network_appliance_traffic_shaping_uplink_selection(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/trafficShaping/uplinkSelection")

    def get_device_appliance_uplinks_settings(self, serial: str) -> dict:
        return self._request("GET", f"/devices/{serial}/appliance/uplinks/settings")

    def get_organization_appliance_uplink_statuses(self, org_id: str) -> list:
        return self._request("GET", f"/organizations/{org_id}/appliance/uplink/statuses")

    def get_network_appliance_warm_spare(self, network_id: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/warmSpare")

    # ========== Appliance SSID / RF（MX 无线功能） ==========

    def get_network_appliance_ssids(self, network_id: str) -> list:
        return self._request("GET", f"/networks/{network_id}/appliance/ssids")

    def get_network_appliance_ssid(self, network_id: str, ssid_number: str) -> dict:
        return self._request("GET", f"/networks/{network_id}/appliance/ssids/{ssid_number}")

    def get_network_appliance_rf_profiles(self, network_id: str) -> list:
        return self._request("GET", f"/networks/{network_id}/appliance/rfProfiles")

    # ========== 组织级 ==========

    def get_organization_appliance_vlans(self, org_id: str) -> list:
        return self._request("GET", f"/organizations/{org_id}/appliance/vlans")
        # ========== Switch（交换机 MS）端口只读端点 ==========

    def get_device_switch_ports(self, serial: str) -> list:
        return self._request("GET", f"/devices/{serial}/switch/ports")

    def get_device_switch_port(self, serial: str, port_id: str) -> dict:
        return self._request("GET", f"/devices/{serial}/switch/ports/{port_id}")

    def get_device_switch_ports_statuses(self, serial: str) -> list:
        return self._request("GET", f"/devices/{serial}/switch/ports/statuses")

    def get_device_switch_ports_statuses_packets(self, serial: str) -> list:
        return self._request("GET", f"/devices/{serial}/switch/ports/statuses/packets")

    def get_organization_switch_ports_by_switch(self, org_id: str) -> list:
        return self._request("GET", f"/organizations/{org_id}/switch/ports/bySwitch")

    def get_organization_switch_ports_statuses_by_switch(self, org_id: str) -> list:
        return self._request("GET", f"/organizations/{org_id}/switch/ports/statuses/bySwitch")

    def get_organization_switch_ports_statuses_packets_by_device_by_port(self, org_id: str) -> list:
        return self._request("GET", f"/organizations/{org_id}/switch/ports/statuses/packets/byDevice/byPort")

    def get_organization_switch_ports_mirrors_by_switch(self, org_id: str) -> list:
        return self._request("GET", f"/organizations/{org_id}/switch/ports/mirrors/bySwitch")

    def get_organization_switch_stacks_ports_mirrors_by_stack(self, org_id: str) -> list:
        return self._request("GET", f"/organizations/{org_id}/switch/stacks/ports/mirrors/byStack")

    def get_organization_config_templates_switch_profiles_ports_mirrors_by_switch_profile(self, org_id: str, config_template_id: str, profile_id: str) -> list:
        return self._request("GET", f"/organizations/{org_id}/configTemplates/{config_template_id}/switch/profiles/{profile_id}/ports/mirrors/bySwitch")

# ==================== Kimi / Moonshot AI 客户端 ====================
class KimiAIClient:
    def __init__(self, api_key: str, base_url: str, model_name: str):
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.model_name = model_name

    def analyze_meraki_data(self, meraki_data: dict, query: str = "") -> str:
        system_prompt = """你是一个 Cisco Meraki 网络专家。请分析提供的 Meraki Dashboard 数据，
给出简洁、专业的中文总结。关注以下方面：
1. 网络整体健康状态
2. 设备在线/离线情况
3. VPN 隧道状态（如有 vMX）
4. 潜在风险或建议
"""
        user_content = f"查询意图: {query or '全面分析'}\n\nMeraki 数据:\n{json.dumps(meraki_data, ensure_ascii=False, indent=2)}"

        payload = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_content}
            ],
            "temperature": 0.3,
            "max_tokens": 2000
        }

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}"
        }

        try:
            response = requests.post(url, headers=headers, json=payload, timeout=60)
            response.raise_for_status()
            result = response.json()
            return result["choices"][0]["message"]["content"]
        except requests.exceptions.RequestException as e:
            logging.error(f"Kimi API request failed: {e}")
            return f"AI 分析失败: {e!s}"
        except (KeyError, IndexError) as e:
            logging.error(f"Kimi API response parsing failed: {e}")
            return "AI 分析返回格式异常"

# ==================== 主 HTTP 函数 ====================
@app.route(route="meraki_dashboard", methods=["GET", "POST"], auth_level=func.AuthLevel.ANONYMOUS)
def meraki_dashboard(req: func.HttpRequest) -> func.HttpResponse:
    logging.info("Meraki Dashboard + Kimi AI function triggered.")

    # 从 Key Vault 获取 Meraki API Key
    meraki_api_key = get_secret_from_keyvault(MERAKI_SECRET_NAME)
    if not meraki_api_key:
        return func.HttpResponse(
            json.dumps({
                "error": "Meraki API Key not found",
                "details": f"Secret '{MERAKI_SECRET_NAME}' not found in Key Vault '{CONFIG['KEY_VAULT_URL']}'. "
                           "Please check: (1) Secret exists in Key Vault, (2) Function App Managed Identity has 'Get' permission on secrets."
            }, ensure_ascii=False),
            status_code=500,
            mimetype="application/json"
        )

    meraki = MerakiClient(meraki_api_key)

    # 解析请求参数
    try:
        if req.method == "POST":
            req_body = req.get_json() or {}
        else:
            req_body = {}

        action = req_body.get("action") or req.params.get("action", "list_organizations")
        org_id = req_body.get("orgId") or req.params.get("orgId", "")
        network_id = req_body.get("networkId") or req.params.get("networkId", "")
        device_serial = req_body.get("serial") or req.params.get("serial", "")
        use_ai = req_body.get("useAi") or req.params.get("useAi", "false").lower() == "true"
        ai_query = req_body.get("aiQuery") or req.params.get("aiQuery", "")

    except Exception as e:
        return func.HttpResponse(
            json.dumps({"error": f"Invalid request: {e!s}"}, ensure_ascii=False),
            status_code=400,
            mimetype="application/json"
        )

    # 执行 Meraki API 调用
    try:
        result = {"action": action}

        if action == "list_organizations":
            result["data"] = meraki.get_organizations()

        elif action == "get_organization":
            if not org_id:
                return func.HttpResponse(json.dumps({"error": "orgId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_organization(org_id)

        elif action == "list_networks":
            if not org_id:
                return func.HttpResponse(json.dumps({"error": "orgId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_networks(org_id)

        elif action == "list_devices":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_devices(network_id)

        elif action == "get_device":
            if not device_serial:
                return func.HttpResponse(json.dumps({"error": "serial is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_device(device_serial)

        elif action == "get_vpn_status":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_vpn_status(network_id)

        elif action == "get_uplink_status":
            if not device_serial:
                return func.HttpResponse(json.dumps({"error": "serial is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_uplink_status(device_serial)

        elif action == "get_vmx_licenses":
            if not org_id:
                return func.HttpResponse(json.dumps({"error": "orgId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_vmx_licenses(org_id)

        elif action == "get_network_overview":
            if not org_id or not network_id:
                return func.HttpResponse(json.dumps({"error": "orgId and networkId are required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            overview = {"network": None, "devices": [], "vpn_status": None}
            networks = meraki.get_networks(org_id)
            for net in networks:
                if net["id"] == network_id:
                    overview["network"] = net
                    break
            overview["devices"] = meraki.get_network_devices(network_id)
            try:
                overview["vpn_status"] = meraki.get_vpn_status(network_id)
            except Exception:
                overview["vpn_status"] = {"error": "VPN status not available for this network type"}
            result["data"] = overview
        elif action == "get_appliance_settings":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_settings(network_id)

        elif action == "get_appliance_vlans":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_vlans(network_id)

        elif action == "get_appliance_vlan":
            vlan_id = req_body.get("vlanId") or req.params.get("vlanId", "")
            if not network_id or not vlan_id:
                return func.HttpResponse(json.dumps({"error": "networkId and vlanId are required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_vlan(network_id, vlan_id)

        elif action == "get_appliance_ports":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_ports(network_id)

        elif action == "get_appliance_port":
            port_id = req_body.get("portId") or req.params.get("portId", "")
            if not network_id or not port_id:
                return func.HttpResponse(json.dumps({"error": "networkId and portId are required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_port(network_id, port_id)

        elif action == "get_appliance_static_routes":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_static_routes(network_id)

        elif action == "get_appliance_single_lan":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_single_lan(network_id)

        elif action == "get_firewall_l3_rules":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_firewall_l3_rules(network_id)

        elif action == "get_firewall_l7_rules":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_firewall_l7_rules(network_id)

        elif action == "get_firewall_1to1_nat":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_firewall_one_to_one_nat(network_id)

        elif action == "get_firewall_1toMany_nat":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_firewall_one_to_many_nat(network_id)

        elif action == "get_firewall_port_forwarding":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_firewall_port_forwarding(network_id)

        elif action == "get_security_intrusion":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_security_intrusion(network_id)

        elif action == "get_security_malware":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_security_malware(network_id)

        elif action == "get_content_filtering":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_content_filtering(network_id)

        elif action == "get_vpn_site_to_site":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_vpn_site_to_site(network_id)

        elif action == "get_vpn_bgp":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_vpn_bgp(network_id)

        elif action == "get_traffic_shaping":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_traffic_shaping(network_id)

        elif action == "get_traffic_shaping_rules":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_traffic_shaping_rules(network_id)

        elif action == "get_traffic_shaping_uplink_bandwidth":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_traffic_shaping_uplink_bandwidth(network_id)

        elif action == "get_traffic_shaping_uplink_selection":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_traffic_shaping_uplink_selection(network_id)

        elif action == "get_device_appliance_uplinks_settings":
            if not device_serial:
                return func.HttpResponse(json.dumps({"error": "serial is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_device_appliance_uplinks_settings(device_serial)

        elif action == "get_org_appliance_uplink_statuses":
            if not org_id:
                return func.HttpResponse(json.dumps({"error": "orgId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_organization_appliance_uplink_statuses(org_id)

        elif action == "get_warm_spare":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_warm_spare(network_id)

        elif action == "get_appliance_ssids":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_ssids(network_id)

        elif action == "get_appliance_ssid":
            ssid_number = req_body.get("ssidNumber") or req.params.get("ssidNumber", "")
            if not network_id or ssid_number == "":
                return func.HttpResponse(json.dumps({"error": "networkId and ssidNumber are required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_ssid(network_id, str(ssid_number))

        elif action == "get_appliance_rf_profiles":
            if not network_id:
                return func.HttpResponse(json.dumps({"error": "networkId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_network_appliance_rf_profiles(network_id)

        elif action == "get_org_appliance_vlans":
            if not org_id:
                return func.HttpResponse(json.dumps({"error": "orgId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_organization_appliance_vlans(org_id)
        elif action == "get_device_switch_ports":
            if not device_serial:
                return func.HttpResponse(json.dumps({"error": "serial is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_device_switch_ports(device_serial)

        elif action == "get_device_switch_port":
            port_id = req_body.get("portId") or req.params.get("portId", "")
            if not device_serial or not port_id:
                return func.HttpResponse(json.dumps({"error": "serial and portId are required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_device_switch_port(device_serial, str(port_id))

        elif action == "get_device_switch_ports_statuses":
            if not device_serial:
                return func.HttpResponse(json.dumps({"error": "serial is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_device_switch_ports_statuses(device_serial)

        elif action == "get_device_switch_ports_statuses_packets":
            if not device_serial:
                return func.HttpResponse(json.dumps({"error": "serial is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_device_switch_ports_statuses_packets(device_serial)

        elif action == "get_org_switch_ports_by_switch":
            if not org_id:
                return func.HttpResponse(json.dumps({"error": "orgId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_organization_switch_ports_by_switch(org_id)

        elif action == "get_org_switch_ports_statuses_by_switch":
            if not org_id:
                return func.HttpResponse(json.dumps({"error": "orgId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_organization_switch_ports_statuses_by_switch(org_id)

        elif action == "get_org_switch_ports_statuses_packets_by_device_by_port":
            if not org_id:
                return func.HttpResponse(json.dumps({"error": "orgId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_organization_switch_ports_statuses_packets_by_device_by_port(org_id)

        elif action == "get_org_switch_ports_mirrors_by_switch":
            if not org_id:
                return func.HttpResponse(json.dumps({"error": "orgId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_organization_switch_ports_mirrors_by_switch(org_id)

        elif action == "get_org_switch_stacks_ports_mirrors_by_stack":
            if not org_id:
                return func.HttpResponse(json.dumps({"error": "orgId is required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_organization_switch_stacks_ports_mirrors_by_stack(org_id)

        elif action == "get_org_config_templates_switch_profiles_ports_mirrors_by_switch_profile":
            config_template_id = req_body.get("configTemplateId") or req.params.get("configTemplateId", "")
            profile_id = req_body.get("profileId") or req.params.get("profileId", "")
            if not org_id or not config_template_id or not profile_id:
                return func.HttpResponse(json.dumps({"error": "orgId, configTemplateId and profileId are required"}, ensure_ascii=False), status_code=400, mimetype="application/json")
            result["data"] = meraki.get_organization_config_templates_switch_profiles_ports_mirrors_by_switch_profile(org_id, config_template_id, profile_id)

        else:
            return func.HttpResponse(
                json.dumps({"error": f"Unknown action: {action}"}, ensure_ascii=False),
                status_code=400,
                mimetype="application/json"
            )

        # AI 分析（Kimi / Moonshot）
        if use_ai:
            kimi_key = get_secret_from_keyvault(CONFIG["SECRET_NAME"])
            if kimi_key:
                ai = KimiAIClient(kimi_key, CONFIG["AI_BASE_URL"], CONFIG["AI_MODEL_NAME"])
                result["ai_analysis"] = ai.analyze_meraki_data(result["data"], ai_query)
            else:
                result["ai_analysis"] = f"AI 分析未启用：无法从 Key Vault 获取 Secret '{CONFIG['SECRET_NAME']}'，请检查 Managed Identity 权限配置"

        return func.HttpResponse(
            json.dumps(result, ensure_ascii=False, indent=2),
            status_code=200,
            mimetype="application/json"
        )

    except requests.exceptions.HTTPError as e:
        status_code = e.response.status_code if e.response else 500
        error_body = e.response.text if e.response else str(e)
        logging.error(f"Meraki API HTTP Error {status_code}: {error_body}")
        return func.HttpResponse(
            json.dumps({"error": f"Meraki API Error ({status_code})", "details": error_body}, ensure_ascii=False),
            status_code=status_code,
            mimetype="application/json"
        )
    except Exception as e:
        logging.exception("Unhandled error in meraki_dashboard function")
        return func.HttpResponse(
            json.dumps({"error": str(e)}, ensure_ascii=False),
            status_code=500,
            mimetype="application/json"
        )


# ==================== Durable Functions: 异步 AI 对话（彻底解决 HTTP 120 秒超时）====================

@app.route(route="infra_agent_async", methods=[func.HttpMethod.POST], auth_level=func.AuthLevel.ANONYMOUS)
@app.durable_client_input(client_name="durable_client")
async def start_ai_conversation(req: func.HttpRequest, durable_client) -> func.HttpResponse:
    """
    启动异步 AI 对话任务。
    返回 202 Accepted + statusQueryGetUri，客户端轮询该地址获取最终结果。
    彻底摆脱 HTTP 120 秒连接超时限制。
    """
    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    try:
        req_body = req.get_json()

        user_prompt = req_body.get("prompt")
        messages = req_body.get("messages")
        conversation_id = req_body.get("conversation_id")

        if not messages and not user_prompt:
            return func.HttpResponse(
                json.dumps({"error": "请提供 'prompt' 或 'messages'"}, ensure_ascii=False),
                status_code=400,
                mimetype="application/json"
            )

        orchestrator_input = {
            "messages": messages,
            "prompt": user_prompt,
            "conversation_id": conversation_id,
            "subscription_id": req_body.get("subscription_id"),
            "ai_provider": (
                req_body.get("ai_provider")
                or req_body.get("provider")
                or req_body.get("api")
                or DEFAULT_AI_PROVIDER
            ),
            "model": req_body.get("model"),
            "temperature": req_body.get("temperature", 0.7),
            "webhook_url": req_body.get("webhook_url"),
        }

        instance_id = await durable_client.start_new(
            "ai_conversation_orchestrator",
            None,
            orchestrator_input
        )

        logging.info(f"Started durable orchestration: {instance_id}")

        # 注意：不使用 create_check_status_response —— 它会返回带 system key 的
        # 函数应用直连轮询 URL，经 APIM 暴露给浏览器存在安全风险。
        # 只回 instance_id，前端统一走 APIM 的 /ai_task_status 轮询。
        return func.HttpResponse(
            json.dumps({
                "instance_id": instance_id,
                "status": "accepted",
                "conversation_id": conversation_id,
                "poll_endpoint": "ai_task_status"
            }, ensure_ascii=False),
            status_code=202,
            mimetype="application/json"
        )

    except Exception as e:
        logging.error(f"Start orchestration failed: {e!s}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}, ensure_ascii=False),
            status_code=500,
            mimetype="application/json"
        )


@app.orchestration_trigger(context_name="context")
def ai_conversation_orchestrator(context):
    """
    Durable Orchestrator: 编排 AI 调用流程。
    所有非确定性操作（AI 调用、I/O）均封装在 Activity 中通过 yield 调用。
    Orchestrator 在每次 yield 后自动持久化检查点，Function App 重启也能恢复。
    """
    input_data = context.get_input()

    # 防御：输入可能为 None 或非 dict（客户端传参异常时不至于让编排器崩溃）
    if input_data is None:
        input_data = {}
    elif not isinstance(input_data, dict):
        input_data = {"prompt": str(input_data)}

    logging.info(
        f"[Orchestrator] {context.instance_id} 启动, "
        f"provider={input_data.get('ai_provider')}, "
        f"has_messages={bool(input_data.get('messages'))}, "
        f"has_prompt={bool(input_data.get('prompt'))}"
    )

    # 注意：Python SDK 的 RetryOptions 只支持两个参数，不支持 backoff_coefficient
    retry_options = df.RetryOptions(
        first_retry_interval_in_milliseconds=5000,
        max_number_of_attempts=3
    )

    try:
        # 在后台 Activity 中执行实际 AI 调用（可运行数分钟，不受 HTTP 限制）
        result = yield context.call_activity_with_retry(
            "ai_conversation_activity",
            retry_options,
            input_data
        )

        # 可选：发送 Webhook 回调通知
        webhook_url = input_data.get("webhook_url")
        if webhook_url:
            try:
                yield context.call_activity(
                    "send_webhook_notification",
                    {
                        "url": webhook_url,
                        "payload": {
                            "instance_id": context.instance_id,
                            "status": "completed",
                            "conversation_id": input_data.get("conversation_id"),
                            "response": result
                        }
                    }
                )
            except Exception:
                pass  # webhook 失败不应影响主流程

        return {
            "status": "completed",
            "response": result,
            "conversation_id": input_data.get("conversation_id"),
            "instance_id": context.instance_id
        }

    except Exception as e:
        logging.exception(f"[Orchestrator] {context.instance_id} 执行失败: {e}")
        # 失败时也尝试通知 webhook
        webhook_url = input_data.get("webhook_url")
        if webhook_url:
            try:
                yield context.call_activity(
                    "send_webhook_notification",
                    {
                        "url": webhook_url,
                        "payload": {
                            "instance_id": context.instance_id,
                            "status": "failed",
                            "conversation_id": input_data.get("conversation_id"),
                            "error": str(e)
                        }
                    }
                )
            except Exception:
                pass

        return {
            "status": "failed",
            "error": str(e),
            "conversation_id": input_data.get("conversation_id"),
            "instance_id": context.instance_id
        }


@app.activity_trigger(input_name="payload")
def ai_conversation_activity(payload: dict) -> str:
    """
    Durable Activity: 在后台执行实际的 AI 调用。
    完全复用现有的 run_ai_with_tools，兼容 /kimi/deepseek + 工具调用 + 用量记录。
    """
    try:
        messages = payload.get("messages")
        prompt = payload.get("prompt")
        conversation_id = payload.get("conversation_id")
        subscription_id = payload.get("subscription_id")
        ai_provider = payload.get("ai_provider", DEFAULT_AI_PROVIDER)
        logging.info(f"[Activity] ai_conversation_activity 开始, provider={ai_provider}, conversation_id={conversation_id}, has_prompt={bool(prompt)}")

        # 构建消息列表（兼容单轮 prompt 模式）
        if conversation_id and not messages and prompt:
            # 有 conversation_id → 加载历史
            history = _load_conversation(conversation_id)
            if not history:
                history = [{"role": "system", "content": "You are an Azure operation assistant. Respond in the same language as the user."}]
            history.append({"role": "user", "content": prompt})
            messages = history
        elif not messages and prompt:
            # 没有 conversation_id → 新建对话
            messages = [
                {"role": "system", "content": "You are an Azure operation assistant. Respond in the same language as the user."},
                {"role": "user", "content": prompt}
            ]

        # 调用现有 AI 工具循环（同步阻塞，在 Activity 中无 HTTP 超时限制）
        logging.info(f"[Activity] 开始执行 run_ai_with_tools, provider={ai_provider}")
        final_content = run_ai_with_tools(messages, subscription_id, ai_provider)
        logging.info(f"[Activity] run_ai_with_tools 完成, 返回内容长度={len(final_content or '')}")

        # 保存对话历史（与现有 infra_agent 逻辑一致）
        if conversation_id:
            messages.append({"role": "assistant", "content": final_content})
            _save_conversation(conversation_id, messages)

        return final_content

    except Exception as e:
        logging.error(f"Activity execution failed: {e!s}")
        raise  # 抛出异常以触发 orchestrator 重试


@app.activity_trigger(input_name="payload")
def send_webhook_notification(payload: dict) -> dict:
    """发送回调通知到用户指定的 webhook"""
    url = payload.get("url")
    data = payload.get("payload")

    try:
        resp = httpx.post(url, json=data, timeout=30)
        resp.raise_for_status()
        return {"status": "sent", "http_status": resp.status_code}
    except Exception as e:
        logging.warning(f"Webhook notification failed: {e!s}")
        return {"status": "failed", "error": str(e)}

@app.route(route="health", methods=["GET"], auth_level=func.AuthLevel.ANONYMOUS)
def health(req: func.HttpRequest) -> func.HttpResponse:
        return func.HttpResponse('{"status":"ok"}', status_code=200, mimetype="application/json")


@app.route(route="ai_task_status", methods=[func.HttpMethod.GET], auth_level=func.AuthLevel.ANONYMOUS)
@app.durable_client_input(client_name="durable_client")
async def get_ai_task_status(req: func.HttpRequest, durable_client) -> func.HttpResponse:
    """查询异步 AI 任务状态（比默认 statusQueryGetUri 更友好的 JSON 格式）"""
    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    instance_id = req.params.get("instance_id")
    if not instance_id:
        return func.HttpResponse(
            json.dumps({"error": "Missing 'instance_id'"}, ensure_ascii=False),
            status_code=400,
            mimetype="application/json"
        )

    try:
        status = await durable_client.get_status(instance_id)

        if status is None:
            return func.HttpResponse(
                json.dumps({
                    "error": "任务不存在或已被清理",
                    "instance_id": instance_id,
                    "status": "not_found"
                }, ensure_ascii=False),
                status_code=404,
                mimetype="application/json"
            )

        runtime_status = status.runtime_status.name if status.runtime_status else "Unknown"

        response_body = {
            "instance_id": instance_id,
            "status": runtime_status.lower(),
            "created_at": status.created_time.isoformat() if status.created_time else None,
            "last_updated_at": status.last_updated_time.isoformat() if status.last_updated_time else None,
        }

        if runtime_status == "Completed":
            response_body["result"] = status.output
        elif runtime_status == "Failed":
            # 注意：Python SDK 的 DurableOrchestrationStatus 没有 error_message 属性，
            # 失败详情在 output 里（orchestrator 已捕获异常并返回结构化 error 字段）
            if isinstance(status.output, dict):
                response_body["error"] = (
                    status.output.get("error")
                    or json.dumps(status.output, ensure_ascii=False)
                )
            elif status.output:
                response_body["error"] = str(status.output)
            else:
                response_body["error"] = "任务执行失败（无详细输出），请查看函数应用日志中 ai_conversation_orchestrator / ai_conversation_activity 的错误记录"
        elif runtime_status in ("Running", "Pending"):
            response_body["message"] = "AI 正在处理中，请稍后重试"

        return func.HttpResponse(
            json.dumps(response_body, ensure_ascii=False),
            status_code=200,
            mimetype="application/json"
        )

    except Exception as e:
        logging.exception(f"[ai_task_status] 查询实例 {instance_id} 失败")
        return func.HttpResponse(
            json.dumps({
                "error": f"查询失败: {type(e).__name__}: {e!s}",
                "instance_id": instance_id
            }, ensure_ascii=False),
            status_code=500,
            mimetype="application/json"
        )


# ============================================================
# 通用对话模式（General Chat）
# 与运维 Agent（infra_agent）并存的一组新端点：
#   - 默认不挂任何运维工具，体验等同于直接与 Kimi 通用助手对话
#   - 请求体 enable_tools=true 时可临时启用 Azure/Meraki 运维工具
#   - 默认模型为 Kimi（kimi-k3），可用 ai_provider 切换 deepseek 
#   - 多轮对话历史由后端 Cosmos DB 管理，凭 conversation_id 续聊
# 端点：
#   POST   /api/chat               同步通用对话
#   POST   /api/chat_async         异步通用对话（Durable，轮询 ai_task_status）
#   GET    /api/chat_history       查询对话历史
#   DELETE /api/chat_conversation  删除对话历史
# ============================================================

GENERAL_CHAT_DEFAULT_PROVIDER = os.environ.get("GENERAL_CHAT_PROVIDER", "kimi")


def _build_general_system_prompt(custom_prompt: str | None = None) -> str:
    """通用对话的 system prompt，可通过请求参数 system_prompt 覆盖"""
    if custom_prompt:
        return custom_prompt

    today = datetime.datetime.utcnow().strftime("%Y-%m-%d")
    return (
        "你是 Kimi，由 Moonshot AI（月之暗面）开发的 AI 助手。\n"
        f"当前日期：{today}。\n"
        "你可以就任何话题与用户自然交流：回答问题、解释概念、写作与润色、翻译、"
        "编程与调试、数据分析、头脑风暴等。\n"
        "请始终使用与用户相同的语言回复；回答准确、结构清晰、详略得当；"
        "不确定或不知道的事情要如实说明，不要编造。"
    )


def _build_general_chat_messages(req_body: dict, conversation_id: str):
    """
    构建通用对话的消息列表（同步 / 异步端点共用）。
    优先级：
      1. 请求直接传 messages（调用方自己管理历史）→ 若缺少 system 消息则自动补通用 system prompt
      2. 传 prompt → 加载 conversation_id 对应的历史（新对话则注入 system prompt）后追加本轮提问
    返回 (messages, error_message)；error_message 为 None 表示成功。
    """
    system_prompt = _build_general_system_prompt(req_body.get("system_prompt"))
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

    # 上传的文件解析成纯文本，注入到本轮提示词前面
    if files:
        files_context = _build_files_context(files)
        user_prompt = f"{files_context}\n\n以上是用户上传的文件内容。{user_prompt or '请分析上述文件。'}"

    history = _load_conversation(conversation_id)
    if not history:
        history = [{"role": "system", "content": system_prompt}]

    if images:
        # 带图片：构造多模态 user 消息（image_url + text 数组）
        history.append({"role": "user", "content": _build_multimodal_user_content(user_prompt, images)})
    else:
        history.append({"role": "user", "content": user_prompt})
    return history, None


def _delete_conversation(conversation_id: str) -> bool:
    """从 Cosmos DB 删除指定对话历史，返回是否删除成功"""
    svc = _get_conversation_table_service()
    if svc is None:
        return False

    try:
        table_client = svc.get_table_client(CONVERSATION_TABLE_NAME)
        table_client.delete_entity(partition_key="conversation", row_key=conversation_id)
        logging.info(f"[Conversation] 历史已删除: {conversation_id}")
        return True
    except Exception as e:
        logging.info(f"[Conversation] 删除失败（可能不存在）: {e}")
        return False


# ---------- 同步通用对话 ----------
@app.route(route="chat", methods=[func.HttpMethod.POST], auth_level=func.AuthLevel.ANONYMOUS)
def general_chat(req: func.HttpRequest) -> func.HttpResponse:
    """
    通用对话（同步，直接返回最终回复）。
    注意：Azure Functions HTTP 触发器有约 120 秒连接超时，超长回复请改用 /api/chat_async。

    请求体（JSON）：
      prompt / messages   二选一；messages 为 OpenAI 格式消息数组（调用方自管历史）
      images              可选；base64 图片数组（data:image/...;base64,... 或纯 base64），
                          传入后自动启用图像识别（DeepSeek 切视觉模型，Kimi 原生支持）
      files               可选；文件数组 [{"name","content"} 或 {"name","content_base64"}]，
                          后端解析为文本注入提示词，支持代码/日志/PDF/Word/Excel/PPT
      conversation_id     可选；不传则自动生成并在响应中返回，凭它续聊
      system_prompt       可选；自定义助手人格，覆盖默认通用 prompt
      enable_tools        可选；true 时启用 Azure/Meraki 运维工具（默认 false）
      ai_provider         可选；kimi（默认）/ deepseek / 
      model               可选；覆盖 provider 默认模型
      temperature         可选；采样温度
      subscription_id     可选；仅 enable_tools=true 时注入工具调用
    """
    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    try:
        req_body = req.get_json()
    except Exception:
        return func.HttpResponse(
            json.dumps({"error": "请求体必须是合法 JSON"}, ensure_ascii=False),
            status_code=400,
            mimetype="application/json"
        )

    conversation_id = req_body.get("conversation_id") or uuid.uuid4().hex

    provider = _normalize_provider(
        req_body.get("ai_provider")
        or req_body.get("provider")
        or GENERAL_CHAT_DEFAULT_PROVIDER
    )

    messages, err = _build_general_chat_messages(req_body, conversation_id)
    if err:
        return func.HttpResponse(
            json.dumps({"error": err}, ensure_ascii=False),
            status_code=400,
            mimetype="application/json"
        )

    enable_tools = bool(req_body.get("enable_tools", False))
    model = req_body.get("model")
    temperature = req_body.get("temperature")

    try:
        final_content = run_ai_with_tools(
            messages,
            req_body.get("subscription_id"),
            provider,
            tools="default" if enable_tools else None,
            model=model,
            temperature=temperature,
        )
    except Exception as e:
        logging.exception(f"[GeneralChat] 调用失败: {e}")
        return func.HttpResponse(
            json.dumps({"error": str(e), "conversation_id": conversation_id}, ensure_ascii=False),
            status_code=500,
            mimetype="application/json"
        )

    # 持久化本轮对话（assistant 最终回复追加后整体 upsert）
    messages.append({"role": "assistant", "content": final_content})
    _save_conversation(conversation_id, messages)

    return func.HttpResponse(
        json.dumps({
            "status": "success",
            "response": final_content,
            "conversation_id": conversation_id,
            "provider": provider,
            "model": model or get_ai_model(provider),
            "tools_enabled": enable_tools,
        }, ensure_ascii=False),
        status_code=200,
        mimetype="application/json"
    )


# ---------- 异步通用对话（Durable Functions，摆脱 HTTP 超时） ----------
@app.route(route="chat_async", methods=[func.HttpMethod.POST], auth_level=func.AuthLevel.ANONYMOUS)
@app.durable_client_input(client_name="durable_client")
async def general_chat_async(req: func.HttpRequest, durable_client) -> func.HttpResponse:
    """
    通用对话（异步）。请求体与 /api/chat 相同，另支持 webhook_url 回调。
    返回 202 + instance_id，客户端轮询 GET /api/ai_task_status?instance_id=... 获取结果。
    """
    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    try:
        req_body = req.get_json()
    except Exception:
        return func.HttpResponse(
            json.dumps({"error": "请求体必须是合法 JSON"}, ensure_ascii=False),
            status_code=400,
            mimetype="application/json"
        )

    if not req_body.get("prompt") and not req_body.get("messages") \
            and not req_body.get("images") and not req_body.get("files"):
        return func.HttpResponse(
            json.dumps({"error": "请提供 'prompt' 或 'messages'"}, ensure_ascii=False),
            status_code=400,
            mimetype="application/json"
        )

    conversation_id = req_body.get("conversation_id") or uuid.uuid4().hex

    orchestrator_input = {
        "prompt": req_body.get("prompt"),
        "messages": req_body.get("messages"),
        "images": req_body.get("images"),
        "files": req_body.get("files"),
        "conversation_id": conversation_id,
        "system_prompt": req_body.get("system_prompt"),
        "enable_tools": bool(req_body.get("enable_tools", False)),
        "ai_provider": (
            req_body.get("ai_provider")
            or req_body.get("provider")
            or GENERAL_CHAT_DEFAULT_PROVIDER
        ),
        "model": req_body.get("model"),
        "temperature": req_body.get("temperature"),
        "subscription_id": req_body.get("subscription_id"),
        "webhook_url": req_body.get("webhook_url"),
    }

    try:
        instance_id = await durable_client.start_new(
            "general_chat_orchestrator",
            None,
            orchestrator_input
        )
    except Exception as e:
        logging.error(f"[GeneralChat] 启动异步编排失败: {e!s}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}, ensure_ascii=False),
            status_code=500,
            mimetype="application/json"
        )

    logging.info(f"[GeneralChat] 异步任务已启动: {instance_id}")

    return func.HttpResponse(
        json.dumps({
            "instance_id": instance_id,
            "status": "accepted",
            "conversation_id": conversation_id,
            "poll_endpoint": "ai_task_status"
        }, ensure_ascii=False),
        status_code=202,
        mimetype="application/json"
    )


@app.orchestration_trigger(context_name="context")
def general_chat_orchestrator(context):
    """
    Durable Orchestrator：编排通用对话调用。
    结构与 ai_conversation_orchestrator 一致，区别仅在调用的 activity 与 system prompt。
    """
    input_data = context.get_input()

    if input_data is None:
        input_data = {}
    elif not isinstance(input_data, dict):
        input_data = {"prompt": str(input_data)}

    logging.info(
        f"[GeneralChat Orchestrator] {context.instance_id} 启动, "
        f"provider={input_data.get('ai_provider')}, "
        f"conversation_id={input_data.get('conversation_id')}"
    )

    retry_options = df.RetryOptions(
        first_retry_interval_in_milliseconds=5000,
        max_number_of_attempts=3
    )

    try:
        result = yield context.call_activity_with_retry(
            "general_chat_activity",
            retry_options,
            input_data
        )

        webhook_url = input_data.get("webhook_url")
        if webhook_url:
            try:
                yield context.call_activity(
                    "send_webhook_notification",
                    {
                        "url": webhook_url,
                        "payload": {
                            "instance_id": context.instance_id,
                            "status": "completed",
                            "conversation_id": input_data.get("conversation_id"),
                            "response": result
                        }
                    }
                )
            except Exception:
                pass  # webhook 失败不影响主流程

        return {
            "status": "completed",
            "response": result,
            "conversation_id": input_data.get("conversation_id"),
            "instance_id": context.instance_id
        }

    except Exception as e:
        logging.exception(f"[GeneralChat Orchestrator] {context.instance_id} 执行失败: {e}")

        webhook_url = input_data.get("webhook_url")
        if webhook_url:
            try:
                yield context.call_activity(
                    "send_webhook_notification",
                    {
                        "url": webhook_url,
                        "payload": {
                            "instance_id": context.instance_id,
                            "status": "failed",
                            "conversation_id": input_data.get("conversation_id"),
                            "error": str(e)
                        }
                    }
                )
            except Exception:
                pass

        return {
            "status": "failed",
            "error": str(e),
            "conversation_id": input_data.get("conversation_id"),
            "instance_id": context.instance_id
        }


@app.activity_trigger(input_name="payload")
def general_chat_activity(payload: dict) -> str:
    """
    Durable Activity：在后台执行实际的通用对话 AI 调用。
    复用 run_ai_with_tools；enable_tools=false 时纯对话不挂工具，无 HTTP 超时限制。
    """
    conversation_id = payload.get("conversation_id") or uuid.uuid4().hex
    provider = payload.get("ai_provider") or GENERAL_CHAT_DEFAULT_PROVIDER
    enable_tools = bool(payload.get("enable_tools", False))

    logging.info(
        f"[GeneralChat Activity] 开始, provider={provider}, "
        f"conversation_id={conversation_id}, enable_tools={enable_tools}"
    )

    messages, err = _build_general_chat_messages(payload, conversation_id)
    if err:
        raise ValueError(err)

    final_content = run_ai_with_tools(
        messages,
        payload.get("subscription_id"),
        provider,
        tools="default" if enable_tools else None,
        model=payload.get("model"),
        temperature=payload.get("temperature"),
    )

    logging.info(f"[GeneralChat Activity] 完成, 回复长度={len(final_content or '')}")

    messages.append({"role": "assistant", "content": final_content})
    _save_conversation(conversation_id, messages)

    return final_content


# ---------- 对话历史管理 ----------
@app.route(route="chat_history", methods=[func.HttpMethod.GET], auth_level=func.AuthLevel.ANONYMOUS)
def get_chat_history(req: func.HttpRequest) -> func.HttpResponse:
    """查询指定 conversation_id 的完整对话历史"""
    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    conversation_id = req.params.get("conversation_id")
    if not conversation_id:
        return func.HttpResponse(
            json.dumps({"error": "Missing 'conversation_id'"}, ensure_ascii=False),
            status_code=400,
            mimetype="application/json"
        )

    messages = _load_conversation(conversation_id)

    return func.HttpResponse(
        json.dumps({
            "status": "success",
            "conversation_id": conversation_id,
            "message_count": len(messages),
            "messages": messages,
        }, ensure_ascii=False),
        status_code=200,
        mimetype="application/json"
    )


@app.route(route="chat_conversation", methods=[func.HttpMethod.DELETE], auth_level=func.AuthLevel.ANONYMOUS)
def delete_chat_conversation(req: func.HttpRequest) -> func.HttpResponse:
    """删除指定 conversation_id 的对话历史"""
    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    conversation_id = req.params.get("conversation_id")

    if not conversation_id:
        try:
            conversation_id = (req.get_json() or {}).get("conversation_id")
        except Exception:
            conversation_id = None

    if not conversation_id:
        return func.HttpResponse(
            json.dumps({"error": "Missing 'conversation_id'"}, ensure_ascii=False),
            status_code=400,
            mimetype="application/json"
        )

    deleted = _delete_conversation(conversation_id)

    return func.HttpResponse(
        json.dumps({
            "status": "deleted" if deleted else "not_found",
            "conversation_id": conversation_id,
        }, ensure_ascii=False),
        status_code=200 if deleted else 404,
        mimetype="application/json"
    )


# ---------- 对话列表（侧边栏用） ----------
@app.route(route="chat_conversations", methods=[func.HttpMethod.GET], auth_level=func.AuthLevel.ANONYMOUS)
def list_chat_conversations(req: func.HttpRequest) -> func.HttpResponse:
    """
    列出全部对话，按更新时间倒序。
    返回每条对话的 conversation_id / title / updated_at / message_count，不含消息正文。
    """
    jwt_error = _verify_jwt(req)
    if jwt_error:
        return jwt_error

    svc = _get_conversation_table_service()
    if svc is None:
        return func.HttpResponse(
            json.dumps({"status": "success", "conversations": [], "note": "Cosmos DB 未配置"}, ensure_ascii=False),
            status_code=200,
            mimetype="application/json"
        )

    conversations = []
    try:
        table_client = svc.get_table_client(CONVERSATION_TABLE_NAME)

        for entity in table_client.query_entities("PartitionKey eq 'conversation'"):
            title = entity.get("Title")

            # 兼容旧记录（无 Title 字段）：从消息中取首条用户消息做标题
            if not title:
                try:
                    msgs = json.loads(entity.get("Messages", "[]"))
                    for m in msgs:
                        if m.get("role") == "user" and m.get("content"):
                            title = str(m["content"]).replace("\n", " ").strip()[:30]
                            break
                except Exception:
                    pass

            conversations.append({
                "conversation_id": entity.get("RowKey"),
                "title": title or "新对话",
                "updated_at": entity.get("UpdatedAt", ""),
                "message_count": int(entity.get("MessageCount", 0) or 0),
            })

        conversations.sort(key=lambda c: c.get("updated_at") or "", reverse=True)

    except Exception as e:
        logging.error(f"[Conversation] 列表查询失败: {e}")
        return func.HttpResponse(
            json.dumps({"error": str(e)}, ensure_ascii=False),
            status_code=500,
            mimetype="application/json"
        )

    return func.HttpResponse(
        json.dumps({"status": "success", "conversations": conversations}, ensure_ascii=False),
        status_code=200,
        mimetype="application/json"
    )
