#!/usr/bin/env python3
"""
Nexus Dashboard 工具集测试（31 个工具，mock httpx，不打真实集群）。

覆盖：每个工具请求的 URL（Infra/Manage 两套基地址）、路径参数编码、query 参数映射、
API Key 与用户名密码两种认证、token 缓存与 401 重登、必填参数校验、HTTP 错误映射、
nexus_overview 聚合逻辑、schema 与注册表一致性。

运行：
  .venv/bin/python test_nexus_dashboard_tools.py
"""

from __future__ import annotations

import json
import os
import sys
import unittest

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

os.environ.setdefault("AICHAT_DB_PATH", "/tmp/aichat-nd-test.db")

import tools  # noqa: E402
from tools import nexus_dashboard_tools as nd  # noqa: E402

BASE = "https://nd.example.com"
ENV_KEYS = ("ND_BASE_URL", "ND_USERNAME", "ND_API_KEY", "ND_PASSWORD", "ND_LOGIN_DOMAIN",
            "ND_VERIFY_TLS", "ND_TIMEOUT", "ND_TOKEN_TTL_SECONDS")


class FakeResponse:
    def __init__(self, payload=None, status_code: int = 200, text: str | None = None):
        self.status_code = status_code
        self._payload = payload if payload is not None else {}
        self.text = text if text is not None else json.dumps(self._payload)

    def json(self):
        return self._payload


class FakeHTTPXClient:
    """拦截 nexus_dashboard_tools.httpx.Client，记录每次请求并交给 handler 决定响应"""

    calls: list[dict] = []
    handler = staticmethod(lambda url, headers, params, body: FakeResponse({"ok": True}))

    def __init__(self, timeout=None, verify=None, **_):
        self.timeout = timeout
        self.verify = verify

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, headers=None, params=None):
        FakeHTTPXClient.calls.append(
            {"method": "GET", "url": url, "headers": headers or {}, "params": params, "verify": self.verify}
        )
        return FakeHTTPXClient.handler(url, headers, params, None)

    def post(self, url, json=None, headers=None):  # noqa: A002 - 与 httpx 参数名保持一致
        FakeHTTPXClient.calls.append(
            {"method": "POST", "url": url, "headers": headers or {}, "params": None,
             "json": json, "verify": self.verify}
        )
        return FakeHTTPXClient.handler(url, headers, None, json)


class NdToolTestBase(unittest.TestCase):
    def setUp(self) -> None:
        FakeHTTPXClient.calls = []
        FakeHTTPXClient.handler = staticmethod(
            lambda url, headers, params, body: FakeResponse({"ok": True, "url": url})
        )
        self._original_client = nd.httpx.Client
        nd.httpx.Client = FakeHTTPXClient  # type: ignore[assignment]
        self.addCleanup(lambda: setattr(nd.httpx, "Client", self._original_client))

        self._saved_env = {key: os.environ.get(key) for key in ENV_KEYS}
        self.addCleanup(self._restore_env)

        nd.reset_token()
        os.environ["ND_BASE_URL"] = BASE
        os.environ["ND_USERNAME"] = "admin"
        os.environ["ND_API_KEY"] = "test-nd-api-key"
        os.environ.pop("ND_PASSWORD", None)

    def _restore_env(self) -> None:
        for key, value in self._saved_env.items():
            if value is None:
                os.environ.pop(key, None)
            else:
                os.environ[key] = value
        nd.reset_token()

    def sample_arguments(self, tool_name: str) -> dict:
        schema = next(s for s in nd.SCHEMAS if s["function"]["name"] == tool_name)
        required = schema["function"]["parameters"].get("required", [])
        return {name: "fabric-a" if name == "fabric_name" else name + "-value" for name in required}


class NdUrlAndAuthTests(NdToolTestBase):
    def test_01_every_tool_hits_expected_url(self) -> None:
        for tool_name in nd.TOOL_NAMES:
            if tool_name == "nexus_overview":
                continue  # 聚合逻辑由 test_08 覆盖
            with self.subTest(tool=tool_name):
                FakeHTTPXClient.calls = []
                spec = nd.SPEC_BY_NAME[tool_name]
                arguments = self.sample_arguments(tool_name)

                payload = json.loads(nd.execute(tool_name, arguments))
                self.assertEqual(payload["status"], "success", payload)
                self.assertEqual(payload["provider"], "nexus_dashboard")
                self.assertEqual(payload["action"], spec["action"])
                self.assertEqual(payload["api"], spec["api"])

                # 期望路径：路径参数按 API 名字替换并 URL 编码
                expected_path = spec["path"]
                for arg_name, api_name in (spec.get("path_params") or {}).items():
                    expected_path = expected_path.replace(
                        "{" + api_name + "}", arguments[arg_name]
                    )

                self.assertEqual(len(FakeHTTPXClient.calls), 1, FakeHTTPXClient.calls)
                call = FakeHTTPXClient.calls[0]
                self.assertEqual(call["method"], "GET")
                self.assertEqual(call["url"], BASE + nd.API_BASE_PATHS[spec["api"]] + expected_path)
                self.assertEqual(call["headers"]["X-Nd-Username"], "admin")
                self.assertEqual(call["headers"]["X-Nd-Apikey"], "test-nd-api-key")
                self.assertIsNone(call["params"])

    def test_02_both_api_bases_are_used(self) -> None:
        bases = {
            nd.SPEC_BY_NAME[name]["api"]
            for name in nd.TOOL_NAMES
            if name in nd.SPEC_BY_NAME
        }
        self.assertEqual(bases, {"infra", "manage"})
        self.assertEqual(nd.API_BASE_PATHS["infra"], "/api/v1/infra")
        self.assertEqual(nd.API_BASE_PATHS["manage"], "/api/v1/manage")

    def test_03_query_parameters_are_mapped(self) -> None:
        nd.execute("nexus_infra_cluster_nodes", {"node_name": "nd-1", "node_role": "worker"})
        self.assertEqual(FakeHTTPXClient.calls[0]["params"], {"nodeName": "nd-1", "nodeRole": "worker"})

        FakeHTTPXClient.calls = []
        nd.execute("nexus_infra_capacities", {"pull_usage": "true"})
        self.assertEqual(FakeHTTPXClient.calls[0]["params"], {"pullUsage": "true"})

    def test_04_path_parameter_is_url_encoded(self) -> None:
        nd.execute("nexus_manage_fabric_summary", {"fabric_name": "site east/1"})
        self.assertTrue(
            FakeHTTPXClient.calls[0]["url"].endswith("/api/v1/manage/fabrics/site%20east%2F1/summary"),
            FakeHTTPXClient.calls[0]["url"],
        )

    def test_05_response_shape(self) -> None:
        FakeHTTPXClient.handler = staticmethod(
            lambda url, headers, params, body: FakeResponse({"fabrics": [{"name": "f1"}]})
        )
        payload = json.loads(nd.execute("nexus_manage_fabrics", {}))
        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["data"], {"fabrics": [{"name": "f1"}]})

    def test_06_verify_tls_defaults_off_and_can_be_enabled(self) -> None:
        os.environ.pop("ND_VERIFY_TLS", None)
        self.assertFalse(nd.verify_tls())
        nd.execute("nexus_infra_about", {})
        self.assertFalse(FakeHTTPXClient.calls[0]["verify"])

        FakeHTTPXClient.calls = []
        os.environ["ND_VERIFY_TLS"] = "true"
        self.assertTrue(nd.verify_tls())
        nd.execute("nexus_infra_about", {})
        self.assertTrue(FakeHTTPXClient.calls[0]["verify"])


class NdPasswordAuthTests(NdToolTestBase):
    def setUp(self) -> None:
        super().setUp()
        os.environ.pop("ND_API_KEY", None)
        os.environ["ND_PASSWORD"] = "super-secret"
        nd.reset_token()

    def test_07_login_then_cookie_and_token_cache(self) -> None:
        def handler(url, headers, params, body):
            if url.endswith("/api/v1/infra/login"):
                return FakeResponse({"jwttoken": "jwt-abc", "token": "jwt-abc"})
            return FakeResponse({"data": "ok"})

        FakeHTTPXClient.handler = staticmethod(handler)

        nd.execute("nexus_infra_about", {})
        nd.execute("nexus_manage_fabrics", {})

        self.assertEqual(len(FakeHTTPXClient.calls), 3, FakeHTTPXClient.calls)
        login = FakeHTTPXClient.calls[0]
        self.assertEqual(login["method"], "POST")
        self.assertEqual(login["url"], BASE + "/api/v1/infra/login")
        self.assertEqual(login["json"], {"domain": "local", "userName": "admin", "userPasswd": "super-secret"})

        for call in FakeHTTPXClient.calls[1:]:
            self.assertEqual(call["headers"]["Cookie"], "AuthCookie=jwt-abc")
            self.assertNotIn("X-Nd-Apikey", call["headers"])

    def test_08_expired_token_triggers_one_relogin(self) -> None:
        state = {"logins": 0, "gets": 0}

        def handler(url, headers, params, body):
            if url.endswith("/api/v1/infra/login"):
                state["logins"] += 1
                return FakeResponse({"jwttoken": f"jwt-{state['logins']}"})
            state["gets"] += 1
            if state["gets"] == 1:
                return FakeResponse({"message": "unauthorized"}, status_code=401)
            return FakeResponse({"status": "ok"})

        FakeHTTPXClient.handler = staticmethod(handler)
        payload = json.loads(nd.execute("nexus_infra_about", {}))

        self.assertEqual(payload["status"], "success", payload)
        self.assertEqual(state["logins"], 2)
        self.assertEqual(FakeHTTPXClient.calls[-1]["headers"]["Cookie"], "AuthCookie=jwt-2")

    def test_09_login_failure_is_reported(self) -> None:
        FakeHTTPXClient.handler = staticmethod(
            lambda url, headers, params, body: FakeResponse({"message": "bad creds"}, status_code=401)
        )
        payload = json.loads(nd.execute("nexus_infra_about", {}))
        self.assertEqual(payload["status"], "error")
        self.assertIn("登录失败", payload["message"])
        self.assertIn("ND_PASSWORD", payload["message"])


class NdOverviewTests(NdToolTestBase):
    def test_10_overview_aggregates_and_tolerates_partial_failure(self) -> None:
        def handler(url, headers, params, body):
            if url.endswith("/api/v1/manage/inventory/switches/summary"):
                return FakeResponse({"message": "boom"}, status_code=500)
            return FakeResponse({"url": url})

        FakeHTTPXClient.handler = staticmethod(handler)
        payload = json.loads(nd.execute("nexus_overview", {}))

        self.assertEqual(payload["status"], "success")
        self.assertEqual(payload["action"], "overview")
        self.assertIsNotNone(payload["data"]["about"])
        self.assertIsNotNone(payload["data"]["cluster_health"])
        self.assertIsNotNone(payload["data"]["fabrics_summary"])
        self.assertIsNone(payload["data"]["switches_summary"])
        self.assertIn("switches_summary", payload["errors"])

        urls = [call["url"] for call in FakeHTTPXClient.calls]
        self.assertIn(BASE + "/api/v1/infra/about", urls)
        self.assertIn(BASE + "/api/v1/infra/clusterhealth/status", urls)
        self.assertIn(BASE + "/api/v1/manage/fabricsSummary", urls)
        self.assertIn(BASE + "/api/v1/manage/inventory/switches/summary", urls)


class NdErrorTests(NdToolTestBase):
    def test_11_missing_path_parameter(self) -> None:
        payload = json.loads(nd.execute("nexus_manage_fabric_summary", {}))
        self.assertEqual(payload["status"], "error")
        self.assertIn("fabric_name", payload["message"])

    def test_12_http_error_mapping(self) -> None:
        FakeHTTPXClient.handler = staticmethod(
            lambda url, headers, params, body: FakeResponse({"message": "forbidden"}, status_code=403)
        )
        payload = json.loads(nd.execute("nexus_manage_fabrics", {}))
        self.assertEqual(payload["status"], "error")
        self.assertIn("HTTP 403", payload["message"])
        self.assertIn("权限", payload["message"])

    def test_13_unknown_tool(self) -> None:
        payload = json.loads(nd.execute("nexus_does_not_exist", {}))
        self.assertEqual(payload["status"], "error")
        self.assertIn("未知", payload["message"])

    def test_14_missing_configuration(self) -> None:
        os.environ.pop("ND_BASE_URL", None)
        payload = json.loads(nd.execute("nexus_infra_about", {}))
        self.assertEqual(payload["status"], "error")
        self.assertIn("ND_BASE_URL", payload["message"])

        os.environ["ND_BASE_URL"] = BASE
        os.environ["ND_USERNAME"] = ""
        payload = json.loads(nd.execute("nexus_infra_about", {}))
        self.assertIn("ND_USERNAME", payload["message"])

        os.environ["ND_USERNAME"] = "admin"
        os.environ.pop("ND_API_KEY", None)
        os.environ.pop("ND_PASSWORD", None)
        payload = json.loads(nd.execute("nexus_infra_about", {}))
        self.assertIn("ND_API_KEY", payload["message"])

    def test_15_base_url_normalisation(self) -> None:
        for raw in ("https://nd.example.com/", "https://nd.example.com/api/v1/infra",
                    "https://nd.example.com/api/v1/manage", "https://nd.example.com/api/v1"):
            with self.subTest(raw=raw):
                os.environ["ND_BASE_URL"] = raw
                self.assertEqual(nd.base_url(), "https://nd.example.com")


class NdSchemaTests(NdToolTestBase):
    def test_16_schema_shape(self) -> None:
        names = [schema["function"]["name"] for schema in nd.SCHEMAS]
        self.assertEqual(len(names), 31)
        self.assertEqual(len(set(names)), 31)
        self.assertEqual(set(names), set(nd.TOOL_NAMES))

        for schema in nd.SCHEMAS:
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

    def test_17_specs_are_read_only_gets(self) -> None:
        self.assertEqual(set(nd.SPEC_BY_NAME), set(nd.TOOL_NAMES) - {"nexus_overview"})
        for name, spec in nd.SPEC_BY_NAME.items():
            with self.subTest(tool=name):
                self.assertIn(spec["api"], nd.API_BASE_PATHS)
                self.assertTrue(spec["path"].startswith("/"))
                self.assertTrue(spec["action"])
                # path 里的占位符必须都在 path_params 里声明
                for api_name in spec.get("path_params", {}).values():
                    self.assertIn("{" + api_name + "}", spec["path"])

    def test_18_health_reports_configuration(self) -> None:
        info = nd.health()
        self.assertTrue(info["configured"])
        self.assertEqual(info["auth_mode"], "apikey")
        self.assertEqual(info["base_url"], BASE)
        self.assertEqual(info["tool_count"], 31)

        os.environ.pop("ND_API_KEY", None)
        os.environ["ND_PASSWORD"] = "pw"
        info = nd.health()
        self.assertEqual(info["auth_mode"], "password")
        self.assertEqual(info["login_domain"], "local")

    def test_19_registered_in_tool_registry(self) -> None:
        collected = {schema["function"]["name"] for schema in tools.default_schemas()}
        self.assertTrue(set(nd.TOOL_NAMES) <= collected)
        # azure 21 + meraki 45 + nexus dashboard 31 + ai 1
        self.assertEqual(len(tools.default_schemas()), 98)
        self.assertEqual(len(tools.health()["tools"]), 98)
        self.assertEqual(len(tools.health()["groups"]["nexus_dashboard"]["tools"]), 31)
        self.assertEqual(tools.execute_tool("nexus_does_not_exist", {}), None)


if __name__ == "__main__":
    unittest.main(verbosity=2)
