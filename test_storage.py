#!/usr/bin/env python3
"""
SQLite 持久化自测（标准库 unittest，无额外依赖）

覆盖 4 件事：
  1. 保存会话 → 重新 import（模拟进程重启）→ 仍能读到
  2. 任务的创建 / 状态更新 / 查询（含 /api/ai_task_status 接口层）
  3. 删除会话后，列表里不再出现
  4. 任务超过 TTL 后被自动清理（TASK_TTL_SECONDS=1，秒级 TTL）

所有用例都在临时数据库上运行，不会碰 ~/Library/Application Support/AIChatApp/aichat.db。

运行：
  .venv/bin/python test_storage.py          # 或 python test_storage.py
  .venv/bin/python test_storage.py -v
"""

from __future__ import annotations

import importlib
import os
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest
import uuid
from contextlib import closing

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

DEFAULT_TTL_HOURS = "24"


def fresh_main(db_path: str, **env_overrides: str):
    """
    以指定环境变量重新 import main —— 等价于「重启进程后重新加载」。

    注意：main 在 import 时读取 AICHAT_DB_PATH / TASK_TTL_SECONDS 等环境变量，
    所以必须先把环境变量设好、再从 sys.modules 里移除后重新导入。
    """
    os.environ["AICHAT_DB_PATH"] = db_path
    os.environ["TASK_TTL_HOURS"] = DEFAULT_TTL_HOURS
    os.environ.pop("TASK_TTL_SECONDS", None)
    os.environ.pop("AI_MOCK_MODE", None)
    os.environ.update(env_overrides)

    sys.modules.pop("main", None)
    return importlib.import_module("main")


class StorageTestBase(unittest.TestCase):
    """每个用例一个全新的临时数据库"""

    def setUp(self) -> None:
        self.temp_dir = tempfile.mkdtemp(prefix="aichat-storage-test-")
        self.db_path = os.path.join(self.temp_dir, "aichat.db")

    def tearDown(self) -> None:
        sys.modules.pop("main", None)
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    # ---------- 辅助 ----------

    def load_main(self, **env_overrides: str):
        return fresh_main(self.db_path, **env_overrides)

    def db_rows(self, table: str) -> list[sqlite3.Row]:
        """直接读数据库文件，确认数据是真落盘了，而不是留在内存里"""
        # 注意：sqlite3.Connection 的 with 只管事务，不会关闭连接，所以用 closing()
        with closing(sqlite3.connect(self.db_path)) as connection:
            connection.row_factory = sqlite3.Row
            return connection.execute(f"SELECT * FROM {table}").fetchall()

    def authed_client(self, main_module) -> TestClient:
        client = TestClient(main_module.app)
        response = client.post(
            f"{main_module.API_PREFIX}/login",
            json={
                "username": os.environ.get("ADMIN_USERNAME", "admin"),
                "password": os.environ.get("ADMIN_PASSWORD", "password123"),
            },
        )
        self.assertEqual(response.status_code, 200, response.text)
        token = response.json()["token"]
        client.headers.update({"Authorization": f"Bearer {token}"})
        return client


class StorageTests(StorageTestBase):
    # ---------------------------------------------------------------
    # 1. 会话：保存 → 重启（重新 import）→ 还能读到
    # ---------------------------------------------------------------
    def test_01_conversation_survives_reimport(self) -> None:
        main = self.load_main()
        self.assertEqual(main._STORAGE_BACKEND, "sqlite", f"未使用 SQLite：{main.storage_status()}")

        conversation_id = f"conv-{uuid.uuid4().hex[:8]}"
        messages = [
            {"role": "system", "content": "你是测试助手"},
            {"role": "user", "content": "重启后我还在吗？"},
            {"role": "assistant", "content": "在的，我存在 SQLite 里。"},
        ]
        main._save_conversation(conversation_id, messages)

        self.assertEqual(len(main._load_conversation(conversation_id)), 3)
        self.assertEqual(len(self.db_rows("conversations")), 1, "会话没有真正写入数据库文件")

        # 重启进程：丢掉模块重新 import
        main = self.load_main()

        restored = main._load_conversation(conversation_id)
        self.assertEqual([m["role"] for m in restored], ["system", "user", "assistant"])
        self.assertEqual(restored[1]["content"], "重启后我还在吗？")
        self.assertEqual(restored[2]["content"], "在的，我存在 SQLite 里。")

        listed_ids = [item["conversation_id"] for item in main.list_conversations()]
        self.assertIn(conversation_id, listed_ids)

        # 顺带确认 /api/chat_history 也读得到（接口层）
        client = self.authed_client(main)
        response = client.get(
            f"{main.API_PREFIX}/chat_history", params={"conversation_id": conversation_id}
        )
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()["message_count"], 3)
        self.assertEqual(response.json()["messages"][1]["content"], "重启后我还在吗？")

    # ---------------------------------------------------------------
    # 2. 任务：创建 → 更新状态 → 查询状态
    # ---------------------------------------------------------------
    def test_02_task_create_update_query(self) -> None:
        main = self.load_main()
        conversation_id = f"conv-{uuid.uuid4().hex[:8]}"

        instance_id = main._create_task(conversation_id)
        task = main._get_task(instance_id)
        self.assertIsNotNone(task)
        self.assertEqual(task["status"], "pending")
        self.assertIsNone(task["result"])
        self.assertIsNone(task["error"])
        self.assertTrue(task["created_at"])
        self.assertEqual(task["updated_at"], task["created_at"])

        main._update_task(instance_id, status="running")
        self.assertEqual(main._get_task(instance_id)["status"], "running")

        payload = {
            "status": "completed",
            "response": "任务完成",
            "conversation_id": conversation_id,
            "instance_id": instance_id,
        }
        main._update_task(instance_id, status="completed", result=payload)

        task = main._get_task(instance_id)
        self.assertEqual(task["status"], "completed")
        self.assertEqual(task["result"], payload, "result 应该以 JSON 形式存取后原样读回")
        self.assertEqual(task["last_updated_at"], task["updated_at"])
        self.assertGreaterEqual(task["updated_at"], task["created_at"])

        # 数据库里只有一条任务记录
        rows = self.db_rows("tasks")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["instance_id"], instance_id)

        # 重启后仍可查询，并且接口层返回同样的状态
        main = self.load_main()
        client = self.authed_client(main)
        response = client.get(
            f"{main.API_PREFIX}/ai_task_status", params={"instance_id": instance_id}
        )
        self.assertEqual(response.status_code, 200, response.text)
        body = response.json()
        self.assertEqual(body["status"], "completed")
        self.assertEqual(body["result"]["response"], "任务完成")
        self.assertEqual(body["result"]["conversation_id"], conversation_id)

        # 不存在的任务返回 404 + not_found
        missing = client.get(
            f"{main.API_PREFIX}/ai_task_status", params={"instance_id": "does-not-exist"}
        )
        self.assertEqual(missing.status_code, 404)
        self.assertEqual(missing.json()["status"], "not_found")

        # 失败状态也能正确落库
        failed_id = main._create_task(conversation_id)
        main._update_task(failed_id, status="failed", error="ValueError: 模拟失败")
        failed = main._get_task(failed_id)
        self.assertEqual(failed["status"], "failed")
        self.assertEqual(failed["error"], "ValueError: 模拟失败")

    # ---------------------------------------------------------------
    # 3. 删除会话后，列表里不再出现
    # ---------------------------------------------------------------
    def test_03_delete_conversation_removes_from_list(self) -> None:
        main = self.load_main()
        client = self.authed_client(main)

        keep_id = f"keep-{uuid.uuid4().hex[:8]}"
        drop_id = f"drop-{uuid.uuid4().hex[:8]}"
        for conversation_id, text in ((keep_id, "保留的会话"), (drop_id, "要删掉的会话")):
            main._save_conversation(
                conversation_id,
                [{"role": "user", "content": text}, {"role": "assistant", "content": "好的"}],
            )

        listed_ids = [item["conversation_id"] for item in main.list_conversations()]
        self.assertCountEqual(listed_ids, [keep_id, drop_id])

        self.assertTrue(main._delete_conversation(drop_id), "删除应返回 True")
        self.assertFalse(main._delete_conversation(drop_id), "重复删除应返回 False")

        listed_ids = [item["conversation_id"] for item in main.list_conversations()]
        self.assertNotIn(drop_id, listed_ids)
        self.assertIn(keep_id, listed_ids)

        # 删除后消息也读不到，数据库里只剩一条
        self.assertEqual(main._load_conversation(drop_id), [])
        self.assertEqual(len(self.db_rows("conversations")), 1)

        # 重启后依然不在列表里，接口层同样查不到
        main = self.load_main()
        client = self.authed_client(main)
        conversations = client.get(f"{main.API_PREFIX}/chat_conversations").json()["conversations"]
        ids = [item["conversation_id"] for item in conversations]
        self.assertNotIn(drop_id, ids)
        self.assertIn(keep_id, ids)

        delete_response = client.delete(
            f"{main.API_PREFIX}/chat_conversation", params={"conversation_id": drop_id}
        )
        self.assertEqual(delete_response.status_code, 404)
        self.assertEqual(delete_response.json()["status"], "not_found")

    # ---------------------------------------------------------------
    # 4. 任务超过 TTL 后被自动清理（TTL 临时改成 1 秒）
    # ---------------------------------------------------------------
    def test_04_expired_tasks_are_cleaned_up(self) -> None:
        # TASK_TTL_SECONDS=1 → 1 秒后即过期
        main = self.load_main(TASK_TTL_SECONDS="1")
        self.assertEqual(main.TASK_TTL_SECONDS, 1.0)

        old_id = main._create_task("conv-old")
        main._update_task(old_id, status="completed", result={"response": "这条应该被清掉"})
        self.assertIsNotNone(main._get_task(old_id), "刚创建的任务不应被清理")

        time.sleep(1.5)

        # 查询会触发清理：过期任务消失，数据库里也不再保留
        self.assertIsNone(main._get_task(old_id), "超过 TTL 的任务应被自动清理")
        remaining = [row["instance_id"] for row in self.db_rows("tasks")]
        self.assertNotIn(old_id, remaining)

        # 新建任务时同样会先清理，且新任务本身不受影响
        fresh_id = main._create_task("conv-fresh")
        self.assertIsNotNone(main._get_task(fresh_id))
        remaining = [row["instance_id"] for row in self.db_rows("tasks")]
        self.assertEqual(remaining, [fresh_id])

        # 同一进程里也没有残留（内存路径的清理逻辑一致）
        self.assertIsNone(main._get_task(old_id))


if __name__ == "__main__":
    print(f"Python: {sys.executable}")
    print("临时数据库目录会在每个用例结束后自动删除\n")
    unittest.main(verbosity=2)
