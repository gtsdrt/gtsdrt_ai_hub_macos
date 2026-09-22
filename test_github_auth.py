#!/usr/bin/env python3
"""GitHub Device Flow 回归测试（全部 HTTP 调用均为 mock）。"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
from unittest.mock import patch

from fastapi.testclient import TestClient


class _FakeResponse:
    def __init__(self, payload: object):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return self._payload


class _FakeGitHubClient:
    pending = False

    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, data: dict, headers: dict):
        if url.endswith("/login/device/code"):
            self.__class__.pending = False
            return _FakeResponse({
                "device_code": "device-secret",
                "user_code": "ABCD-EFGH",
                "verification_uri": "https://github.com/login/device",
                "expires_in": 900,
                "interval": 5,
            })
        if self.__class__.pending:
            return _FakeResponse({"error": "authorization_pending"})
        return _FakeResponse({"access_token": "github-access-token", "token_type": "bearer"})

    async def get(self, url: str, headers: dict):
        if url.endswith("/user/emails"):
            return _FakeResponse([
                {"email": "developer@example.com", "primary": True, "verified": True}
            ])
        return _FakeResponse({"id": 123456, "login": "octocat", "email": None})


class GitHubAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="aichat-github-auth-")
        os.environ.update({
            "AICHAT_DB_PATH": os.path.join(cls.temp_dir.name, "auth.db"),
            "GITHUB_CLIENT_ID": "github-test-client",
            "GITHUB_ALLOWED_LOGINS": "octocat",
            "JWT_SECRET": "unit-test-secret-at-least-32-bytes-long",
        })
        sys.modules.pop("main", None)
        cls.main = importlib.import_module("main")
        cls.client = TestClient(cls.main.app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        sys.modules.pop("main", None)
        cls.temp_dir.cleanup()

    def start(self) -> dict:
        with patch.object(self.main.httpx, "AsyncClient", _FakeGitHubClient):
            response = self.client.post("/api/auth/github/start")
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_start_returns_device_code_for_browser_flow(self) -> None:
        body = self.start()
        self.assertEqual(body["user_code"], "ABCD-EFGH")
        self.assertEqual(body["verification_uri"], "https://github.com/login/device")
        self.assertTrue(body["state"])

    def test_success_returns_one_time_app_jwt(self) -> None:
        started = self.start()
        with patch.object(self.main.httpx, "AsyncClient", _FakeGitHubClient):
            response = self.client.get("/api/auth/github/status", params={"state": started["state"]})
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        claims = self.main._decode_token(body["token"])
        self.assertEqual(body["status"], "complete")
        self.assertEqual(body["login"], "octocat")
        self.assertEqual(claims["provider"], "github")
        self.assertEqual(claims["sub"], "github:123456")

        consumed = self.client.get("/api/auth/github/status", params={"state": started["state"]})
        self.assertEqual(consumed.status_code, 404)

    def test_authorization_pending_is_not_an_error(self) -> None:
        started = self.start()
        _FakeGitHubClient.pending = True
        with patch.object(self.main.httpx, "AsyncClient", _FakeGitHubClient):
            response = self.client.get("/api/auth/github/status", params={"state": started["state"]})
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["status"], "waiting")


if __name__ == "__main__":
    unittest.main(verbosity=2)
