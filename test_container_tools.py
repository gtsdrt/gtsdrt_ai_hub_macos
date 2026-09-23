#!/usr/bin/env python3
"""
多容器注册表工具集测试（3 个工具，mock httpx，不打真实容器）。

覆盖：注册表三种来源（文件 / 内联 JSON / 旧 ANISBLE_* 环境变量）与优先级、文件热加载、
同名去重、白名单三种模式（auto / list / all）、容器自报 read_only 元数据的自动放行、
任务元数据只在需要时才拉、未知容器名报错、401 与容器 Stopped 的错误映射、超时与连接错误、
参数类型校验、health() 与注册表一致性。

运行：
  .venv/bin/python test_container_tools.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

os.environ.setdefault("AICHAT_DB_PATH", "/tmp/aichat-container-test.db")

import httpx  # noqa: E402

import tools  # noqa: E402
from tools import container_tools as ct  # noqa: E402

BASE = "https://ansible-executor.example.norwayeast.azurecontainerapps.io"
BASE2 = "https://terraform-executor.example.norwayeast.azurecontainerapps.io"

ENV_KEYS = (
    ct.ENDPOINTS_JSON_ENV, ct.REGISTRY_FILE_ENV, ct.LEGACY_URL_ENV, ct.LEGACY_KEY_ENV,
    ct.LEGACY_NAME_ENV, ct.ALLOWED_TASKS_ENV, ct.TIMEOUT_ENV, ct.LEGACY_TIMEOUT_ENV,
    "AICHAT_CONTAINER_KEY_C1",
)

# azure-acr 契约的 /tasks 返回（含 read_only 元数据的容器）
TASKS_PAYLOAD = {
    "tasks": {
        "meraki_get_switch_ports": {"required_params": ["serial"]},
        "meraki_get_firewall_rules": {"required_params": ["serial"], "read_only": True},
        "azure_create_vnet": {"required_params": ["resource_group_name", "vnet_name", "location"]},
        "azure_delete_resource-group": {"required_params": ["resource_group_name"], "read_only": False},
    }
}


class FakeResponse:
    def __init__(self, payload=None, status_code: int = 200, text: str | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeHTTPXClient:
    """拦截 container_tools.httpx.Client，记录请求，响应由 handler 决定"""

    calls: list[dict] = []
    handler = staticmethod(lambda method, url, headers, body: FakeResponse({"ok": True}))

    def __init__(self, timeout=None, **_):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, headers=None):
        FakeHTTPXClient.calls.append({"method": "GET", "url": url, "headers": headers or {}, "json": None})
        return FakeHTTPXClient.handler("GET", url, headers or {}, None)

    def post(self, url, json=None, headers=None):  # noqa: A002 - 与 httpx 参数名保持一致
        FakeHTTPXClient.calls.append({"method": "POST", "url": url, "headers": headers or {}, "json": json})
        return FakeHTTPXClient.handler("POST", url, headers or {}, json)


class ContainerTestBase(unittest.TestCase):
    def setUp(self) -> None:
        FakeHTTPXClient.calls = []
        FakeHTTPXClient.handler = staticmethod(
            lambda method, url, headers, body: FakeResponse(TASKS_PAYLOAD if url.endswith("/tasks") else {"ok": True})
        )
        self._original_client = ct.httpx.Client
        ct.httpx.Client = FakeHTTPXClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(ct.httpx, "Client", self._original_client))

        self._saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        self.addCleanup(self._restore_env)

        for key in ENV_KEYS:
            os.environ.pop(key, None)

        # 默认注册表：tmp 目录里的文件（每个用例独立，避免污染真实 App Support）
        self._tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmpdir.cleanup)
        self.registry_file = os.path.join(self._tmpdir.name, "containers.json")
        os.environ[ct.REGISTRY_FILE_ENV] = self.registry_file
        ct._FILE_CACHE["key"] = None
        ct._FILE_CACHE["entries"] = []
        self.addCleanup(lambda: ct._FILE_CACHE.update({"key": None, "entries": []}))

    def _restore_env(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value

    def write_registry(self, containers, mtime_ns: int | None = None) -> None:
        with open(self.registry_file, "w", encoding="utf-8") as handle:
            json.dump({"containers": containers}, handle, ensure_ascii=False)
        if mtime_ns is not None:
            os.utime(self.registry_file, ns=(mtime_ns, mtime_ns))

    def result(self, raw: str) -> dict:
        return json.loads(raw)


class RegistrySourceTests(ContainerTestBase):
    def test_01_env_json_registry(self) -> None:
        os.environ[ct.ENDPOINTS_JSON_ENV] = json.dumps(
            {"containers": [{"name": "envbox", "url": BASE, "api_key": "k"}]}
        )
        entries = ct.registry_entries()
        self.assertEqual([item.name for item in entries], ["envbox"])
        self.assertEqual(entries[0].source, "env_json")

    def test_02_file_registry_and_hot_reload(self) -> None:
        self.write_registry([{"name": "one", "url": BASE, "api_key": "k"}], mtime_ns=1_000_000_000)
        self.assertEqual([item.name for item in ct.registry_entries()], ["one"])

        # 改文件 -> 立即生效（不重启、不手动清缓存）
        self.write_registry(
            [
                {"name": "one", "url": BASE, "api_key": "k"},
                {"name": "two", "url": BASE2, "api_key": "k2", "description": "第二个容器"},
            ],
            mtime_ns=2_000_000_000,
        )
        names = [item.name for item in ct.registry_entries()]
        self.assertEqual(names, ["one", "two"])
        self.assertEqual(ct.registry_entries()[1].source, "file")

    def test_03_legacy_env_becomes_single_container(self) -> None:
        os.environ[ct.LEGACY_URL_ENV] = BASE + "/tasks"      # 顺手验证 URL 归一化
        os.environ[ct.LEGACY_KEY_ENV] = "legacy-key"
        os.environ[ct.ALLOWED_TASKS_ENV] = "azure_create_vnet"
        entries = ct.registry_entries()

        self.assertEqual([item.name for item in entries], ["ansible"])
        self.assertEqual(entries[0].url, BASE)
        self.assertEqual(entries[0].source, "legacy_env")
        self.assertEqual(entries[0].allowed_tasks, ["azure_create_vnet"])
        self.assertEqual(entries[0].effective_key, "legacy-key")

    def test_04_legacy_wildcard_means_all(self) -> None:
        os.environ[ct.LEGACY_URL_ENV] = BASE
        os.environ[ct.LEGACY_KEY_ENV] = "k"
        os.environ[ct.ALLOWED_TASKS_ENV] = "*"
        endpoint = ct.registry_entries()[0]
        self.assertTrue(endpoint.all_tasks_allowed)

    def test_05_file_wins_over_legacy_with_same_name(self) -> None:
        os.environ[ct.LEGACY_URL_ENV] = BASE
        os.environ[ct.LEGACY_KEY_ENV] = "legacy-key"
        self.write_registry([{"name": "ansible", "url": BASE2, "api_key": "file-key"}])

        entries = ct.registry_entries()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0].url, BASE2)
        self.assertEqual(entries[0].source, "file")

    def test_06_file_and_legacy_coexist_as_two_containers(self) -> None:
        os.environ[ct.LEGACY_URL_ENV] = BASE
        os.environ[ct.LEGACY_KEY_ENV] = "legacy-key"
        self.write_registry([{"name": "terraform", "url": BASE2, "api_key": "tf-key"}])

        self.assertEqual([item.name for item in ct.registry_entries()], ["terraform", "ansible"])

    def test_07_broken_registry_file_does_not_crash(self) -> None:
        with open(self.registry_file, "w", encoding="utf-8") as handle:
            handle.write("{ this is not json")
        self.assertEqual(ct.registry_entries(), [])
        payload = self.result(ct.container_list_endpoints({}))
        self.assertEqual(payload["count"], 0)
        self.assertIn("注册表为空", payload["hint"])

    def test_08_api_key_env_indirection(self) -> None:
        os.environ["MY_CONTAINER_KEY"] = "from-env"
        self.write_registry([{"name": "c1", "url": BASE, "api_key_env": "MY_CONTAINER_KEY"}])
        self.addCleanup(lambda: os.environ.pop("MY_CONTAINER_KEY", None))
        self.assertEqual(ct.registry_entries()[0].effective_key, "from-env")

    def test_09_entries_without_name_or_url_are_skipped(self) -> None:
        self.write_registry(
            [
                {"name": "", "url": BASE, "api_key": "k"},
                {"name": "ok", "url": "", "api_key": "k"},
                {"name": "good", "url": BASE, "api_key": "k"},
            ]
        )
        self.assertEqual([item.name for item in ct.registry_entries()], ["good"])

    def test_09b_app_injected_env_key_is_used_when_registry_has_none(self) -> None:
        """App 从 Keychain 注入 AICHAT_CONTAINER_KEY_<NAME>，注册表里可以完全不写密钥"""
        os.environ["AICHAT_CONTAINER_KEY_C1"] = "key-from-keychain"
        self.write_registry([{"name": "c1", "url": BASE, "description": "无明文密钥"}])
        endpoint = ct.registry_entries()[0]

        self.assertEqual(endpoint.auto_key_env, "AICHAT_CONTAINER_KEY_C1")
        self.assertEqual(endpoint.effective_key, "key-from-keychain")
        self.assertEqual(endpoint.key_source, "AICHAT_CONTAINER_KEY_C1")

    def test_09c_registry_api_key_wins_over_injected_env(self) -> None:
        os.environ["AICHAT_CONTAINER_KEY_C1"] = "key-from-keychain"
        self.write_registry([{"name": "c1", "url": BASE, "api_key": "key-from-registry"}])
        endpoint = ct.registry_entries()[0]
        self.assertEqual(endpoint.effective_key, "key-from-registry")
        self.assertEqual(endpoint.key_source, "registry")

    def test_09d_missing_key_message_lists_three_ways(self) -> None:
        self.write_registry([{"name": "c1", "url": BASE}])
        payload = self.result(ct.container_list_tasks({}))
        self.assertEqual(payload["status"], "error")
        self.assertIn("api_key", payload["message"])
        self.assertIn("AICHAT_CONTAINER_KEY_C1", payload["message"])
        self.assertIn("Keychain", payload["message"])


class ListToolTests(ContainerTestBase):
    def test_10_list_endpoints_reports_whitelist_and_source(self) -> None:
        os.environ[ct.LEGACY_URL_ENV] = BASE
        os.environ[ct.LEGACY_KEY_ENV] = "k"
        payload = self.result(ct.container_list_endpoints({}))

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["count"], 1)
        container = payload["containers"][0]
        self.assertEqual(container["name"], "ansible")
        self.assertTrue(container["api_key_configured"])
        self.assertIn("auto", container["whitelist"])
        self.assertEqual(payload["registry_file"], self.registry_file)

    def test_11_list_tasks_hits_endpoint_with_api_key(self) -> None:
        self.write_registry([{"name": "c1", "url": BASE, "api_key": "k1"}])
        payload = self.result(ct.container_list_tasks({}))

        self.assertEqual(payload["container"], "c1")
        self.assertEqual(payload["count"], 4)
        call = FakeHTTPXClient.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertEqual(call["url"], BASE + "/tasks")
        self.assertEqual(call["headers"]["X-API-Key"], "k1")

    def test_12_default_read_only_fallback_marks_callable(self) -> None:
        self.write_registry([{"name": "c1", "url": BASE, "api_key": "k"}])
        tasks = self.result(ct.container_list_tasks({}))["tasks"]

        self.assertTrue(tasks["meraki_get_switch_ports"]["callable_by_ai"])   # 内置只读名单
        self.assertTrue(tasks["meraki_get_firewall_rules"]["callable_by_ai"])  # 容器自报 read_only
        self.assertFalse(tasks["azure_create_vnet"]["callable_by_ai"])
        self.assertFalse(tasks["azure_delete_resource-group"]["callable_by_ai"])
        self.assertEqual(self.result(ct.container_list_tasks({"container": "c1"}))["callable_count"], 2)

    def test_13_container_read_only_metadata_extend_whitelist(self) -> None:
        """容器新加一个自报 read_only=1 的任务 -> 自动放行，不用改 App 配置"""
        FakeHTTPXClient.handler = staticmethod(
            lambda method, url, headers, body: FakeResponse(
                {"tasks": {"brand_new_query": {"required_params": [], "read_only": True}}}
            )
        )
        self.write_registry([{"name": "c1", "url": BASE, "api_key": "k"}])
        tasks = self.result(ct.container_list_tasks({}))["tasks"]
        self.assertTrue(tasks["brand_new_query"]["callable_by_ai"])
        self.assertTrue(tasks["brand_new_query"]["read_only"])

    def test_14_mode_list_ignores_read_only_metadata(self) -> None:
        self.write_registry(
            [{"name": "c1", "url": BASE, "api_key": "k", "mode": "list",
              "allowed_tasks": ["azure_create_vnet"]}]
        )
        tasks = self.result(ct.container_list_tasks({}))["tasks"]
        self.assertTrue(tasks["azure_create_vnet"]["callable_by_ai"])
        self.assertFalse(tasks["meraki_get_firewall_rules"]["callable_by_ai"])

    def test_15_mode_all_and_wildcard_allow_everything(self) -> None:
        self.write_registry([{"name": "c1", "url": BASE, "api_key": "k", "mode": "all"}])
        tasks = self.result(ct.container_list_tasks({}))["tasks"]
        self.assertTrue(all(meta["callable_by_ai"] for meta in tasks.values()))

        self.write_registry([{"name": "c1", "url": BASE, "api_key": "k", "allowed_tasks": "*"}])
        self.assertTrue(ct.registry_entries()[0].all_tasks_allowed)

    def test_16_missing_container_uses_first_but_second_can_be_named(self) -> None:
        self.write_registry(
            [
                {"name": "c1", "url": BASE, "api_key": "k1"},
                {"name": "c2", "url": BASE2, "api_key": "k2"},
            ]
        )
        self.assertEqual(self.result(ct.container_list_tasks({}))["container"], "c1")
        self.assertEqual(self.result(ct.container_list_tasks({"container": "c2"}))["container"], "c2")
        self.assertEqual(FakeHTTPXClient.calls[-1]["url"], BASE2 + "/tasks")


class RunTaskTests(ContainerTestBase):
    def test_17_builtin_read_only_fast_path_skips_metadata_fetch(self) -> None:
        self.write_registry([{"name": "c1", "url": BASE, "api_key": "k"}])
        FakeHTTPXClient.handler = staticmethod(
            lambda method, url, headers, body: FakeResponse(
                {"returncode": 0, "stdout": "out", "stderr": "", "data": {"ok": 1}}
            )
        )
        payload = self.result(
            ct.container_run_task({"task": "meraki_get_switch_ports", "params": {"serial": "Q2XX-1"}})
        )

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["container"], "c1")
        self.assertEqual(payload["data"], {"ok": 1})
        # 只读快路径：直接 POST，不去拉 /tasks
        self.assertEqual([call["method"] for call in FakeHTTPXClient.calls], ["POST"])
        self.assertEqual(FakeHTTPXClient.calls[0]["json"],
                         {"task": "meraki_get_switch_ports", "params": {"serial": "Q2XX-1"}})

    def test_18_auto_mode_checks_read_only_metadata_then_runs(self) -> None:
        self.write_registry([{"name": "c1", "url": BASE, "api_key": "k"}])

        def handler(method, url, headers, body):
            if url.endswith("/tasks"):
                return FakeResponse({"tasks": {"custom_query": {"required_params": [], "read_only": True}}})
            return FakeResponse({"returncode": 0, "stdout": "", "stderr": ""})

        FakeHTTPXClient.handler = staticmethod(handler)
        payload = self.result(ct.container_run_task({"task": "custom_query", "params": {}}))

        self.assertEqual(payload["status"], "success")
        self.assertEqual([call["method"] for call in FakeHTTPXClient.calls], ["GET", "POST"])

    def test_19_auto_mode_blocks_write_task_without_posting(self) -> None:
        self.write_registry([{"name": "c1", "url": BASE, "api_key": "k"}])
        payload = self.result(
            ct.container_run_task({"task": "azure_delete_resource-group", "params": {"resource_group_name": "rg"}})
        )

        self.assertEqual(payload["status"], "error")
        self.assertIn("不允许 AI 在容器 c1 上调用", payload["message"])
        self.assertIn("read_only=false", payload["message"])
        self.assertIn("allowed_tasks", payload["message"])
        self.assertEqual([call["method"] for call in FakeHTTPXClient.calls], ["GET"])  # 只有元数据查询

    def test_20_unknown_container_lists_available_names(self) -> None:
        self.write_registry(
            [{"name": "c1", "url": BASE, "api_key": "k1"}, {"name": "c2", "url": BASE2, "api_key": "k2"}]
        )
        payload = self.result(ct.container_list_tasks({"container": "nope"}))
        self.assertEqual(payload["status"], "error")
        self.assertIn("没有注册名为 nope 的容器", payload["message"])
        self.assertIn("c1、c2", payload["message"])
        self.assertEqual(FakeHTTPXClient.calls, [])

    def test_21_missing_task_name_and_bad_params(self) -> None:
        self.write_registry([{"name": "c1", "url": BASE, "api_key": "k"}])
        self.assertIn("task 是必填参数", self.result(ct.container_run_task({}))["message"])
        self.assertIn("params 必须是对象",
                      self.result(ct.container_run_task({"task": "x", "params": "oops"}))["message"])
        nested = self.result(
            ct.container_run_task({"task": "meraki_get_switch_ports", "params": {"serial": {"a": 1}}})
        )
        self.assertIn("类型不支持", nested["message"])
        self.assertEqual(FakeHTTPXClient.calls, [])

    def test_22_list_params_kept_and_none_dropped(self) -> None:
        self.write_registry([{"name": "c1", "url": BASE, "api_key": "k"}])
        FakeHTTPXClient.handler = staticmethod(
            lambda method, url, headers, body: FakeResponse({"returncode": 0, "stdout": "", "stderr": ""})
        )
        ct.container_run_task(
            {
                "task": "meraki_get_switch_ports",
                "params": {"serial": "Q", "subnet_names": ["a", "b"], "skip": None},
            }
        )
        self.assertEqual(FakeHTTPXClient.calls[0]["json"]["params"],
                         {"serial": "Q", "subnet_names": ["a", "b"]})

    def test_23_unsupported_protocol_is_rejected(self) -> None:
        self.write_registry([{"name": "c1", "url": BASE, "api_key": "k", "protocol": "knative"}])
        payload = self.result(ct.container_list_tasks({}))
        self.assertEqual(payload["status"], "error")
        self.assertIn("暂不支持", payload["message"])

    def test_24_missing_api_key_is_rejected_before_network(self) -> None:
        self.write_registry([{"name": "c1", "url": BASE, "api_key_env": "NOPE_NOT_SET"}])
        payload = self.result(ct.container_list_tasks({}))
        self.assertEqual(payload["status"], "error")
        self.assertIn("没有可用的 API Key", payload["message"])
        self.assertEqual(FakeHTTPXClient.calls, [])


class ErrorMappingTests(ContainerTestBase):
    def test_25_bad_api_key_maps_to_hint(self) -> None:
        self.write_registry([{"name": "c1", "url": BASE, "api_key": "bad"}])
        FakeHTTPXClient.handler = staticmethod(
            lambda method, url, headers, body: FakeResponse({"detail": "Invalid API Key"}, status_code=401)
        )
        payload = self.result(ct.container_list_tasks({}))
        self.assertIn("HTTP 401", payload["message"])
        self.assertIn("X-API-Key 不正确", payload["message"])

    def test_26_stopped_container_page_maps_to_start_hint(self) -> None:
        self.write_registry([{"name": "c1", "url": BASE, "api_key": "k"}])
        stopped = "<html><body><h1>Error 404 - This Container App is stopped</h1></body></html>"
        FakeHTTPXClient.handler = staticmethod(
            lambda method, url, headers, body: FakeResponse(None, status_code=404, text=stopped)
        )
        payload = self.result(ct.container_list_tasks({}))
        self.assertIn("Stopped", payload["message"])
        self.assertIn("az containerapp start", payload["message"])

    def test_27_timeout_and_connect_error(self) -> None:
        self.write_registry([{"name": "c1", "url": BASE, "api_key": "k"}])

        def raise_timeout(method, url, headers, body):
            raise httpx.TimeoutException("timed out")

        FakeHTTPXClient.handler = staticmethod(raise_timeout)
        self.assertIn("超时", self.result(ct.container_list_tasks({}))["message"])

        def raise_connect(method, url, headers, body):
            raise httpx.ConnectError("refused")

        FakeHTTPXClient.handler = staticmethod(raise_connect)
        self.assertIn("无法连接容器 c1", self.result(ct.container_list_tasks({}))["message"])


class HealthAndRegistryTests(ContainerTestBase):
    def test_28_health_reports_registry(self) -> None:
        self.write_registry(
            [
                {"name": "c1", "url": BASE, "api_key": "k1"},
                {"name": "c2", "url": BASE2, "api_key_env": "NOT_SET_KEY", "mode": "all"},
            ]
        )
        health = ct.health()

        self.assertEqual(health["container_count"], 2)
        self.assertFalse(health["configured"])          # c2 缺 key
        self.assertTrue(health["containers"][0]["has_api_key"])
        self.assertEqual(health["containers"][1]["mode"], "all")
        self.assertEqual(health["registry_file"], self.registry_file)
        self.assertEqual(health["tool_count"], 3)

    def test_29_health_when_registry_empty(self) -> None:
        health = ct.health()
        self.assertFalse(health["configured"])
        self.assertEqual(health["containers"], [])
        self.assertIn("注册表为空", health["hint"])

    def test_30_unknown_tool_and_schema_consistency(self) -> None:
        self.assertIn("未知的容器工具", self.result(ct.execute("container_nope", {}))["message"])

        names = [schema["function"]["name"] for schema in ct.SCHEMAS]
        self.assertEqual(names, ["container_list_endpoints", "container_list_tasks", "container_run_task"])
        self.assertEqual(ct.TOOL_NAMES, tuple(names))
        for schema in ct.SCHEMAS:
            self.assertEqual(schema["type"], "function")
            self.assertIn("parameters", schema["function"])

        collected = {schema["function"]["name"] for schema in tools.default_schemas()}
        self.assertTrue(set(ct.TOOL_NAMES) <= collected)
        # azure 21 + meraki 45 + nexus dashboard 31 + container 3 + ai 1
        self.assertEqual(len(tools.default_schemas()), 101)
        self.assertEqual(len(tools.health()["tools"]), 101)
        self.assertEqual(tools.health()["groups"]["container"]["tool_count"], 3)
        self.assertEqual(tools.execute_tool("container_nope", {}), None)

    def test_31_run_ansible_endpoint_is_never_exposed(self) -> None:
        blob = json.dumps(ct.SCHEMAS, ensure_ascii=False)
        self.assertNotIn("/run-ansible", blob)
        self.assertNotIn("run-ansible", blob)


if __name__ == "__main__":
    unittest.main(verbosity=2)
