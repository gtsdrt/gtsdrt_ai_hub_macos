#!/usr/bin/env python3
"""
鉴权加固回归测试（标准库 unittest）

这些用例锁定的是「曾经真实存在过」的几类失守，防止以后被改回去：

  1. X-MS-CLIENT-PRINCIPAL 请求头曾经是一个「任意值即放行」的鉴权后门
  2. JWT_SECRET 曾经有公开默认值，任何人都能伪造 token
  3. ADMIN_PASSWORD 曾经默认 admin / password123
  4. /api/health 曾经未鉴权就暴露订阅 ID 与本机路径
  5. /docs、/openapi.json 曾经无需鉴权即可访问
  6. webhook_url 曾经可以打内网 / 云元数据地址（SSRF）

运行：
  .venv/bin/python test_auth_hardening.py
  .venv/bin/python test_auth_hardening.py -v
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

try:
    from fastapi.testclient import TestClient
except ImportError:  # pragma: no cover
    raise SystemExit(
        "缺少依赖：请先执行\n"
        f"  {sys.executable} -m pip install -r requirements-fastapi.txt"
    )

STRONG_PASSWORD = "unit-test-admin-password"
TEST_JWT_SECRET = "unit-test-secret-at-least-32-bytes-long"
PUBLIC_DEFAULT_JWT_SECRET = "your-very-secret-key-change-it-in-production"
FAKE_ENV_FILE = "/nonexistent-aichat-test.env"


def fresh_main(db_path: str, **env_overrides: str):
    """在干净的配置下重新 import main（模拟一次进程启动）"""
    for name in (
        "AICHAT_TRUST_EASY_AUTH",
        "AICHAT_EASY_AUTH_SECRET",
        "AICHAT_ENABLE_DOCS",
        "AICHAT_ALLOW_PRIVATE_WEBHOOKS",
    ):
        os.environ.pop(name, None)

    os.environ["AICHAT_ENV_FILE"] = FAKE_ENV_FILE  # 不要读仓库里真实的 .env
    os.environ["AICHAT_DB_PATH"] = db_path
    os.environ["AICHAT_JWT_SECRET_FILE"] = db_path + ".jwt"
    os.environ["JWT_SECRET"] = TEST_JWT_SECRET
    os.environ["ADMIN_PASSWORD"] = STRONG_PASSWORD
    os.environ.update(env_overrides)

    sys.modules.pop("main", None)
    return importlib.import_module("main")


def forge_hs256(secret: str, claims: dict) -> str:
    def enc(obj: dict) -> str:
        raw = json.dumps(obj, separators=(",", ":")).encode()
        return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

    signing_input = f"{enc({'alg': 'HS256', 'typ': 'JWT'})}.{enc(claims)}"
    signature = hmac.new(secret.encode(), signing_input.encode(), hashlib.sha256).digest()
    return f"{signing_input}." + base64.urlsafe_b64encode(signature).rstrip(b"=").decode()


class MainModuleTestCase(unittest.TestCase):
    """共用：临时目录 + 一次干净的 main 导入"""

    env_overrides: dict[str, str] = {}

    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="aichat-auth-hardening-")
        cls.db_path = os.path.join(cls.temp_dir.name, "auth.db")
        cls.main = fresh_main(cls.db_path, **cls.env_overrides)
        cls.client = TestClient(cls.main.app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        sys.modules.pop("main", None)
        cls.temp_dir.cleanup()


# ---------------------------------------------------------------------------
# 1. Easy Auth 请求头不再是无条件放行
# ---------------------------------------------------------------------------


class EasyAuthHeaderTests(MainModuleTestCase):
    def test_header_alone_is_rejected(self) -> None:
        response = self.client.get(
            "/api/chat_conversations", headers={"X-MS-CLIENT-PRINCIPAL": "x"}
        )
        self.assertEqual(response.status_code, 401, response.text)

    def test_header_with_arbitrary_json_is_rejected(self) -> None:
        response = self.client.get(
            "/api/chat_conversations",
            headers={"X-MS-CLIENT-PRINCIPAL": json.dumps({"auth_typ": "aad", "claims": []})},
        )
        self.assertEqual(response.status_code, 401, response.text)

    def test_no_credentials_is_rejected(self) -> None:
        self.assertEqual(self.client.get("/api/chat_conversations").status_code, 401)

    def test_valid_login_token_still_works(self) -> None:
        login = self.client.post(
            "/api/login", json={"username": "admin", "password": STRONG_PASSWORD}
        )
        self.assertEqual(login.status_code, 200, login.text)
        response = self.client.get(
            "/api/chat_conversations",
            headers={"Authorization": f"Bearer {login.json()['token']}"},
        )
        self.assertEqual(response.status_code, 200, response.text)


class EasyAuthOptInTests(MainModuleTestCase):
    """显式开启 Easy Auth：必须同时持有反向代理的共享密钥"""

    env_overrides = {
        "AICHAT_TRUST_EASY_AUTH": "1",
        "AICHAT_EASY_AUTH_SECRET": "proxy-shared-secret-abc",
    }

    def test_header_without_shared_secret_is_rejected(self) -> None:
        response = self.client.get(
            "/api/chat_conversations", headers={"X-MS-CLIENT-PRINCIPAL": "x"}
        )
        self.assertEqual(response.status_code, 401, response.text)

    def test_header_with_wrong_shared_secret_is_rejected(self) -> None:
        response = self.client.get(
            "/api/chat_conversations",
            headers={"X-MS-CLIENT-PRINCIPAL": "x", "X-Aichat-Proxy-Secret": "wrong"},
        )
        self.assertEqual(response.status_code, 401, response.text)

    def test_header_with_correct_shared_secret_is_allowed(self) -> None:
        response = self.client.get(
            "/api/chat_conversations",
            headers={
                "X-MS-CLIENT-PRINCIPAL": "x",
                "X-Aichat-Proxy-Secret": "proxy-shared-secret-abc",
            },
        )
        self.assertEqual(response.status_code, 200, response.text)


# ---------------------------------------------------------------------------
# 2. 公开默认 JWT 密钥不能再用来伪造 token
# ---------------------------------------------------------------------------


class JwtSecretTests(MainModuleTestCase):
    env_overrides = {"JWT_SECRET": PUBLIC_DEFAULT_JWT_SECRET}

    def test_public_default_secret_is_not_used(self) -> None:
        self.assertNotEqual(self.main.JWT_SECRET, PUBLIC_DEFAULT_JWT_SECRET)
        self.assertGreaterEqual(len(self.main.JWT_SECRET), 32)

    def test_token_forged_with_public_default_secret_is_rejected(self) -> None:
        forged = forge_hs256(
            PUBLIC_DEFAULT_JWT_SECRET,
            {"username": "admin", "provider": "local", "sub": "local:admin",
             "exp": int(time.time()) + 86400},
        )
        response = self.client.get(
            "/api/chat_conversations", headers={"Authorization": f"Bearer {forged}"}
        )
        self.assertEqual(response.status_code, 401, response.text)

    def test_generated_secret_file_is_owner_only(self) -> None:
        secret_path = os.environ["AICHAT_JWT_SECRET_FILE"]
        self.assertTrue(os.path.exists(secret_path))
        mode = stat.S_IMODE(os.stat(secret_path).st_mode)
        self.assertEqual(mode, 0o600, oct(mode))


# ---------------------------------------------------------------------------
# 3. 管理员口令 fail closed
# ---------------------------------------------------------------------------


class AdminPasswordFailClosedTests(unittest.TestCase):
    """直接跑子进程，避免「导入即抛异常」污染本进程的 sys.modules"""

    def _import_main(self, **env_overrides):
        env = {
            key: value for key, value in os.environ.items()
            if key not in ("ADMIN_PASSWORD", "JWT_SECRET")
        }
        env["AICHAT_ENV_FILE"] = FAKE_ENV_FILE
        env["AICHAT_JWT_SECRET_FILE"] = os.path.join(
            tempfile.gettempdir(), "aichat-failclosed-test.jwt"
        )
        env.update(env_overrides)
        return subprocess.run(
            [sys.executable, "-c", "import main"],
            cwd=PROJECT_DIR, env=env, capture_output=True, text=True, timeout=120,
        )

    def test_missing_admin_password_refuses_to_start(self) -> None:
        result = self._import_main()
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ADMIN_PASSWORD", result.stderr)

    def test_empty_admin_password_refuses_to_start(self) -> None:
        result = self._import_main(ADMIN_PASSWORD="")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ADMIN_PASSWORD", result.stderr)

    def test_legacy_default_password_refuses_to_start(self) -> None:
        result = self._import_main(ADMIN_PASSWORD="password123")
        self.assertNotEqual(result.returncode, 0)

    def test_too_short_password_refuses_to_start(self) -> None:
        result = self._import_main(ADMIN_PASSWORD="short")
        self.assertNotEqual(result.returncode, 0)

    def test_easy_auth_without_shared_secret_refuses_to_start(self) -> None:
        result = self._import_main(
            ADMIN_PASSWORD=STRONG_PASSWORD, AICHAT_TRUST_EASY_AUTH="1"
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("AICHAT_EASY_AUTH_SECRET", result.stderr)


class LoginHardeningTests(MainModuleTestCase):
    def test_legacy_default_password_rejected(self) -> None:
        response = self.client.post(
            "/api/login", json={"username": "admin", "password": "password123"}
        )
        self.assertEqual(response.status_code, 401, response.text)

    def test_empty_password_rejected(self) -> None:
        response = self.client.post(
            "/api/login", json={"username": "admin", "password": ""}
        )
        self.assertEqual(response.status_code, 401, response.text)

    def test_wrong_username_rejected(self) -> None:
        response = self.client.post(
            "/api/login", json={"username": "nobody", "password": STRONG_PASSWORD}
        )
        self.assertEqual(response.status_code, 401, response.text)


# ---------------------------------------------------------------------------
# 4/5. /health 脱敏 + 文档默认关闭
# ---------------------------------------------------------------------------


class HealthRedactionTests(MainModuleTestCase):
    def test_unauthenticated_health_hides_internals(self) -> None:
        body = self.client.get("/api/health").json()
        self.assertEqual(body["status"], "ok")

        # 检查清单仍然可用：只保留布尔/版本类信息
        self.assertIn("version", body["python"])
        self.assertIn("providers", body["ai"])

        # 但内部路径、订阅 ID、内部端点不能出现
        self.assertEqual(body["python"]["executable"], "")
        self.assertNotIn("db_path", body.get("storage_detail", {}))
        self.assertNotIn("subscription_id", body.get("azure", {}))
        self.assertNotIn("base_url", body.get("nexus_dashboard", {}))
        self.assertNotIn("api_key_env", body.get("meraki", {}))

    def test_authenticated_health_keeps_full_detail(self) -> None:
        login = self.client.post(
            "/api/login", json={"username": "admin", "password": STRONG_PASSWORD}
        )
        body = self.client.get(
            "/api/health",
            headers={"Authorization": f"Bearer {login.json()['token']}"},
        ).json()
        self.assertIn("db_path", body["storage_detail"])
        self.assertIn("subscription_id", body["azure"])

    def test_docs_are_disabled_by_default(self) -> None:
        for path in ("/docs", "/redoc", "/openapi.json"):
            self.assertEqual(self.client.get(path).status_code, 404, path)

    def test_docs_can_be_enabled_without_auth(self) -> None:
        with tempfile.TemporaryDirectory(prefix="aichat-docs-") as tmp:
            main = fresh_main(os.path.join(tmp, "docs.db"), AICHAT_ENABLE_DOCS="1")
            client = TestClient(main.app)
            try:
                self.assertEqual(client.get("/openapi.json").status_code, 200)
            finally:
                client.close()
                sys.modules.pop("main", None)


# ---------------------------------------------------------------------------
# 6. webhook SSRF 防护
# ---------------------------------------------------------------------------


class WebhookSsrfTests(MainModuleTestCase):
    def test_internal_targets_are_blocked(self) -> None:
        blocked = [
            "http://169.254.169.254/latest/meta-data/",  # 云元数据
            "http://127.0.0.1:8000/api/health",          # 回环
            "http://localhost/hook",
            "http://[::1]/hook",
            "http://10.0.0.5/hook",
            "http://192.168.1.1/hook",
            "http://172.16.0.1/hook",
        ]
        for url in blocked:
            with self.subTest(url=url):
                self.assertIsNotNone(self.main._webhook_url_error(url), url)

    def test_non_http_schemes_are_blocked(self) -> None:
        for url in ("file:///etc/passwd", "gopher://127.0.0.1:6379/_", "ftp://x/y"):
            with self.subTest(url=url):
                self.assertIsNotNone(self.main._webhook_url_error(url), url)

    def test_public_https_target_is_allowed(self) -> None:
        self.assertIsNone(self.main._webhook_url_error("https://example.com/hook"))


# ---------------------------------------------------------------------------
# 额外：Meraki 路径参数必须编码（防越出固定接口表）
# ---------------------------------------------------------------------------


class MerakiPathEncodingTests(unittest.TestCase):
    def test_path_params_are_url_encoded(self) -> None:
        from tools import meraki_tools

        captured: dict = {}

        def fake_request(path, params=None):
            captured["path"] = path

            class _Resp:
                status_code = 200
                text = "{}"

                def json(self):
                    return {}

            return _Resp()

        original = meraki_tools._request
        meraki_tools._request = fake_request
        try:
            meraki_tools._dispatch("get_device", {"serial": "x/../../organizations"})
        finally:
            meraki_tools._request = original

        path = captured["path"]
        # 斜杠被编码后，注入的值只能待在一个路径段里，无法逃出 /devices/{serial} 的形状
        self.assertIn("%2F", path)
        self.assertEqual(len(path.split("/")), 3, path)
        self.assertTrue(path.startswith("/devices/"), path)


if __name__ == "__main__":
    unittest.main(verbosity=2)
