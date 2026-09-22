#!/usr/bin/env python3
"""Google OAuth 与本地应急登录回归测试（不访问 Google 网络）。"""

from __future__ import annotations

import importlib
import os
import sys
import tempfile
import unittest
import urllib.parse
from unittest.mock import patch

from fastapi.testclient import TestClient


class _FakeResponse:
    def __init__(self, payload: dict):
        self._payload = payload

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return self._payload


class _FakeGoogleClient:
    def __init__(self, *args, **kwargs):
        pass

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url: str, data: dict):
        assert url == "https://oauth2.googleapis.com/token"
        assert data["code_verifier"]
        return _FakeResponse({"access_token": "google-access-token"})

    async def get(self, url: str, headers: dict):
        assert url == "https://openidconnect.googleapis.com/v1/userinfo"
        assert headers["Authorization"] == "Bearer google-access-token"
        return _FakeResponse({"sub": "google-user-123", "email": "person@example.com", "email_verified": True})


class GoogleAuthTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        cls.temp_dir = tempfile.TemporaryDirectory(prefix="aichat-google-auth-")
        os.environ.update({
            "AICHAT_DB_PATH": os.path.join(cls.temp_dir.name, "auth.db"),
            "GOOGLE_CLIENT_ID": "test-client.apps.googleusercontent.com",
            "GOOGLE_ALLOWED_EMAILS": "person@example.com",
            "JWT_SECRET": "unit-test-secret-at-least-32-bytes-long",
            "ADMIN_USERNAME": "admin",
            "ADMIN_PASSWORD": "password123",
        })
        sys.modules.pop("main", None)
        cls.main = importlib.import_module("main")
        cls.client = TestClient(cls.main.app)

    @classmethod
    def tearDownClass(cls) -> None:
        cls.client.close()
        sys.modules.pop("main", None)
        cls.temp_dir.cleanup()

    def test_local_admin_login_is_preserved(self) -> None:
        response = self.client.post("/api/login", json={"username": "admin", "password": "password123"})
        self.assertEqual(response.status_code, 200, response.text)
        claims = self.main._decode_token(response.json()["token"])
        self.assertEqual(claims["provider"], "local")
        self.assertEqual(claims["username"], "admin")

    def test_google_start_uses_state_and_pkce(self) -> None:
        response = self.client.post("/api/auth/google/start")
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        query = urllib.parse.parse_qs(urllib.parse.urlparse(body["authorization_url"]).query)
        self.assertEqual(query["client_id"], ["test-client.apps.googleusercontent.com"])
        self.assertEqual(query["state"], [body["state"]])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertTrue(query["code_challenge"][0])

    def test_google_callback_exchanges_for_one_time_app_jwt(self) -> None:
        started = self.client.post("/api/auth/google/start").json()
        with patch.object(self.main.httpx, "AsyncClient", _FakeGoogleClient):
            callback = self.client.get(
                "/api/auth/google/callback",
                params={"state": started["state"], "code": "authorization-code"},
            )
        self.assertEqual(callback.status_code, 200, callback.text)

        status = self.client.get("/api/auth/google/status", params={"state": started["state"]})
        self.assertEqual(status.status_code, 200, status.text)
        body = status.json()
        claims = self.main._decode_token(body["token"])
        self.assertEqual(body["status"], "complete")
        self.assertEqual(body["email"], "person@example.com")
        self.assertEqual(claims["provider"], "google")
        self.assertEqual(claims["sub"], "google:google-user-123")

        consumed = self.client.get("/api/auth/google/status", params={"state": started["state"]})
        self.assertEqual(consumed.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)
