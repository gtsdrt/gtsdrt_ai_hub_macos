#!/usr/bin/env python3
"""
Meraki 工具集测试（45 个工具，mock httpx，不打真实 Dashboard API）。

覆盖：每个工具调用的 URL 与认证头、返回格式（status/provider/action/回显字段/data）、
必填参数校验、HTTP 错误映射（401）、network_overview 特殊逻辑、schema 与注册表一致性。

运行：
  .venv/bin/python test_meraki_tools.py
"""

from __future__ import annotations

import json
import os
import re
import sys
import unittest

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

os.environ.setdefault("AICHAT_DB_PATH", "/tmp/aichat-meraki-test.db")
os.environ["MERAKI_API_KEY"] = "test-meraki-key"

import tools  # noqa: E402
from tools import meraki_tools  # noqa: E402


class FakeResponse:
    def __init__(self, payload=None, status_code: int = 200, text: str | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else []
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeHTTPXClient:
    """拦截 meraki_tools.httpx.Client，记录每次 GET 的 URL 与请求头"""

    calls: list[dict] = []
    response: FakeResponse = FakeResponse([{"id": "1", "name": "fake"}])

    def __init__(self, timeout=None, **_):
        self.timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, headers=None, params=None):
        FakeHTTPXClient.calls.append({"url": url, "headers": headers or {}, "params": params})
        return FakeHTTPXClient.response


class MerakiToolTestBase(unittest.TestCase):
    def setUp(self) -> None:
        FakeHTTPXClient.calls = []
        FakeHTTPXClient.response = FakeResponse([{"id": "1", "name": "fake"}])
        self._original = meraki_tools.httpx.Client
        meraki_tools.httpx.Client = FakeHTTPXClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(meraki_tools.httpx, "Client", self._original))
        os.environ["MERAKI_API_KEY"] = "test-meraki-key"

    def sample_arguments(self, tool_name: str) -> dict:
        """按 schema 的 required 字段构造参数"""
        schema = next(s for s in meraki_tools.SCHEMAS if s["function"]["name"] == tool_name)
        required = schema["function"]["parameters"].get("required", [])
        sample = {"org_id": "org-1", "network_id": "N_123", "serial": "Q2XX-XXXX-XXXX",
                  "vlan_id": "10", "port_id": "1", "ssid_number": "0",
                  "config_template_id": "ct-1", "profile_id": "p-1"}
        return {name: sample.get(name, f"{name}-value") for name in required}


class MerakiEndpointTests(MerakiToolTestBase):
    def test_01_every_tool_hits_expected_url(self) -> None:
        for tool_name in meraki_tools.TOOL_NAMES:
            if tool_name in meraki_tools.SPECIAL_TOOLS:
                continue  # network_overview 的逻辑与多端点由 test_08 专门覆盖
            with self.subTest(tool=tool_name):
                FakeHTTPXClient.calls = []
                FakeHTTPXClient.response = FakeResponse({"ok": True, "tool": tool_name})
                arguments = self.sample_arguments(tool_name)

                payload = json.loads(meraki_tools.execute(tool_name, arguments))
                self.assertEqual(payload["status"], "success", payload)

                spec = meraki_tools.TOOL_SPECS[tool_name]
                verb, template = meraki_tools.ENDPOINTS[spec["method"]]
                expected_path = template.format(**{
                    name: arguments[name] for name in spec["params"]
                })

                self.assertEqual(len(FakeHTTPXClient.calls), 1, FakeHTTPXClient.calls)
                call = FakeHTTPXClient.calls[0]
                self.assertEqual(call["url"], f"https://api.meraki.com/api/v1{expected_path}")
                self.assertEqual(verb, "GET")
                self.assertEqual(call["headers"]["X-Cisco-Meraki-API-Key"], "test-meraki-key")
                self.assertIsNone(call["params"])

    def test_02_response_shape(self) -> None:
        FakeHTTPXClient.response = FakeResponse([{"id": "N_1", "name": "net"}])
        payload = json.loads(meraki_tools.execute("meraki_list_networks", {"org_id": "org-9"}))
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["provider"], "meraki")
        self.assertEqual(payload["action"], "list_networks")
        self.assertEqual(payload["org_id"], "org-9")
        self.assertEqual(payload["data"], [{"id": "N_1", "name": "net"}])

    def test_03_bearer_auth_mode(self) -> None:
        os.environ["MERAKI_AUTH_HEADER"] = "bearer"
        self.addCleanup(lambda: os.environ.pop("MERAKI_AUTH_HEADER", None))
        meraki_tools.execute("meraki_list_organizations", {})
        headers = FakeHTTPXClient.calls[0]["headers"]
        self.assertEqual(headers["Authorization"], "Bearer test-meraki-key")
        self.assertNotIn("X-Cisco-Meraki-API-Key", headers)

    def test_04_missing_api_key(self) -> None:
        os.environ["MERAKI_API_KEY"] = ""
        self.addCleanup(lambda: os.environ.__setitem__("MERAKI_API_KEY", "test-meraki-key"))
        payload = json.loads(meraki_tools.execute("meraki_list_organizations", {}))
        self.assertEqual(payload["status"], "error")
        self.assertIn("MERAKI_API_KEY", payload["message"])


class MerakiErrorTests(MerakiToolTestBase):
    def test_05_required_arguments(self) -> None:
        for tool_name in meraki_tools.TOOL_NAMES:
            required = meraki_tools.TOOL_SPECS[tool_name]["params"] or ("network_id", "org_id")
            if tool_name == "meraki_list_organizations":
                continue
            with self.subTest(tool=tool_name):
                payload = json.loads(meraki_tools.execute(tool_name, {}))
                self.assertEqual(payload["status"], "error", payload)
                self.assertIn(required[0], payload["message"])

    def test_06_http_error_is_mapped(self) -> None:
        FakeHTTPXClient.response = FakeResponse(
            status_code=401, text='{"errors":["No valid authentication method found"]}'
        )
        payload = json.loads(meraki_tools.execute("meraki_list_organizations", {}))
        self.assertEqual(payload["status"], "error")
        self.assertIn("HTTP 401", payload["message"])
        self.assertIn("No valid authentication method found", payload["message"])

    def test_07_unknown_tool(self) -> None:
        payload = json.loads(meraki_tools.execute("meraki_nope", {}))
        self.assertEqual(payload["status"], "error")
        self.assertIn("未知的 Meraki 工具", payload["message"])

    def test_08_network_overview(self) -> None:
        calls = {"n": 0}

        class Sequenced(FakeHTTPXClient):
            def get(self, url, headers=None, params=None):
                FakeHTTPXClient.calls.append({"url": url, "headers": headers or {}, "params": params})
                calls["n"] += 1
                if url.endswith("/networks"):
                    return FakeResponse([{"id": "N_123", "name": "branch"}])
                if url.endswith("/devices"):
                    return FakeResponse([{"serial": "Q2XX", "model": "MX68"}])
                return FakeResponse({"status": "ok"})

        meraki_tools.httpx.Client = Sequenced  # type: ignore[assignment]
        payload = json.loads(
            meraki_tools.execute("meraki_get_network_overview", {"org_id": "org-1", "network_id": "N_123"})
        )
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["action"], "get_network_overview")
        self.assertEqual(payload["data"]["network"]["name"], "branch")
        self.assertEqual(payload["data"]["devices"][0]["model"], "MX68")
        self.assertEqual(payload["data"]["vpn_status"], {"status": "ok"})
        self.assertEqual(calls["n"], 3)


class MerakiRegistryTests(MerakiToolTestBase):
    def test_09_schemas_complete(self) -> None:
        names = [schema["function"]["name"] for schema in meraki_tools.SCHEMAS]
        self.assertEqual(len(names), 45)
        self.assertEqual(len(set(names)), 45)

        for schema in meraki_tools.SCHEMAS:
            with self.subTest(tool=schema["function"]["name"]):
                function = schema["function"]
                self.assertEqual(schema["type"], "function")
                self.assertTrue(function.get("description"))
                parameters = function["parameters"]
                self.assertEqual(parameters["type"], "object")
                self.assertIn("required", parameters)
                for prop_name, spec in parameters["properties"].items():
                    self.assertIn("description", spec, f"{prop_name} 缺 description")
                    self.assertIn("type", spec, f"{prop_name} 缺 type")

    def test_10_specs_and_endpoints_consistent(self) -> None:
        self.assertEqual(set(meraki_tools.TOOL_NAMES), set(meraki_tools.TOOL_SPECS))
        self.assertEqual(set(meraki_tools.TOOL_NAMES), set(meraki_tools._HANDLERS))

        for tool_name, spec in meraki_tools.TOOL_SPECS.items():
            with self.subTest(tool=tool_name):
                self.assertIn(spec["method"], meraki_tools.ENDPOINTS)
                if tool_name in meraki_tools.SPECIAL_TOOLS:
                    continue
                verb, template = meraki_tools.ENDPOINTS[spec["method"]]
                self.assertEqual(verb, "GET")
                self.assertEqual(set(re.findall(r"\{(\w+)\}", template)), set(spec["params"]))
                for echo in spec["echo"]:
                    self.assertIn(echo, spec["params"])

    def test_11_registered_in_tool_registry(self) -> None:
        collected = {schema["function"]["name"] for schema in tools.default_schemas()}
        self.assertTrue(set(meraki_tools.TOOL_NAMES) <= collected)
        # azure 21 + meraki 45 + nexus dashboard 31 + ai 1
        self.assertEqual(len(tools.default_schemas()), 101)
        self.assertEqual(len(tools.health()["tools"]), 101)
        self.assertEqual(len(tools.health()["groups"]["meraki"]["tools"]), 45)


if __name__ == "__main__":
    unittest.main(verbosity=2)
