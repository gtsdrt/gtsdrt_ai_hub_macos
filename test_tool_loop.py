#!/usr/bin/env python3
"""
工具调用循环测试（mock OpenAI 客户端，不消耗 token、不打真实 API）。

覆盖：
  1. 正常一轮工具调用后给出最终答案 -> stop_reason=completed
  2. 模型每轮都调工具 -> 达到轮次上限时**不抛异常**，改成不带工具的收尾调用
  3. 收尾调用也失败 -> 退化成工具结果摘要（依然不抛异常）
  4. 同一个 (工具, 参数) 反复调用 -> 超过 MAX_IDENTICAL_TOOL_CALLS 后不再执行，提前收尾
  5. 同一轮里完全相同的调用只执行一次（结果复用，但每条 tool_call 都要有 tool 消息）
  6. 请求字段 max_tool_iterations 覆盖默认上限，且被夹到合法区间
  7. 失败的工具结果状态是 error（供前端展示）

运行：
  .venv/bin/python test_tool_loop.py
"""

from __future__ import annotations

import json
import os
import sys
import types
import unittest

PROJECT_DIR = os.path.dirname(os.path.abspath(__file__))
if PROJECT_DIR not in sys.path:
    sys.path.insert(0, PROJECT_DIR)

os.environ.setdefault("AICHAT_DB_PATH", "/tmp/aichat-toolloop-test.db")

import main  # noqa: E402


# ------------------------------------------------------------------ 假 OpenAI 对象


class FakeFunction:
    def __init__(self, name: str, arguments: str):
        self.name = name
        self.arguments = arguments


class FakeToolCall:
    def __init__(self, identifier: str, name: str, arguments: str):
        self.id = identifier
        self.type = "function"
        self.function = FakeFunction(name, arguments)


class FakeMessage:
    def __init__(self, content=None, tool_calls=None):
        self.content = content
        self.tool_calls = tool_calls


class FakeUsage:
    prompt_tokens = 10
    completion_tokens = 5
    total_tokens = 15


class FakeResponse:
    def __init__(self, message: FakeMessage):
        self.choices = [types.SimpleNamespace(message=message, finish_reason="stop")]
        self.usage = FakeUsage()


class FakeCompletions:
    """按脚本依次返回响应；脚本用完后返回一个纯文本回复"""

    def __init__(self, script):
        self.script = list(script)
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        return FakeResponse(FakeMessage(content="(兜底回复)"))


class FakeClient:
    def __init__(self, script):
        self.chat = types.SimpleNamespace(completions=FakeCompletions(script))


def tool_round(*calls):
    """构造一轮「模型请求调用工具」的响应：calls = (name, args_dict) 列表"""
    tool_calls = [
        FakeToolCall(f"call_{index}", name, json.dumps(args, ensure_ascii=False))
        for index, (name, args) in enumerate(calls)
    ]
    return FakeResponse(FakeMessage(content=None, tool_calls=tool_calls))


def final_round(text):
    return FakeResponse(FakeMessage(content=text))


class ToolLoopTestBase(unittest.TestCase):
    def setUp(self) -> None:
        self.executed: list[tuple[str, dict]] = []
        self.finalize_calls: list[dict] = []
        self.fail_tools = {"boom_tool"}

        self._orig_should_mock = main._should_mock
        self._orig_get_client = main.get_ai_client
        self._orig_execute_tool = main._execute_tool
        self._orig_record_usage = main._record_usage

        main._should_mock = lambda provider: False
        main._record_usage = lambda provider, model, usage: None
        main._execute_tool = self._fake_execute_tool

        self.addCleanup(self._restore)

    def _restore(self) -> None:
        main._should_mock = self._orig_should_mock
        main.get_ai_client = self._orig_get_client
        main._execute_tool = self._orig_execute_tool
        main._record_usage = self._orig_record_usage

    def _fake_execute_tool(self, name, arguments):
        self.executed.append((name, dict(arguments or {})))
        if name in self.fail_tools:
            return json.dumps({"status": "error", "message": "工具炸了"}, ensure_ascii=False)
        return json.dumps({"status": "success", "provider": "test", "name": name}, ensure_ascii=False)

    def install_client(self, script):
        client = FakeClient(script)
        main.get_ai_client = lambda provider: client
        return client

    def run_chat(self, script, tools="default", **kwargs):
        client = self.install_client(script)
        outcome = main.run_ai_with_tools(
            [{"role": "user", "content": "帮我查一下"}],
            ai_provider="deepseek",
            tools=tools,
            **kwargs,
        )
        return outcome, client


class ToolLoopTests(ToolLoopTestBase):
    def test_01_single_tool_round_then_answer(self) -> None:
        outcome, client = self.run_chat([
            tool_round(("demo_tool", {"x": 1})),
            final_round("查完了：结果是 42"),
        ])

        self.assertEqual(outcome["response"], "查完了：结果是 42")
        self.assertEqual(outcome["stop_reason"], "completed")
        self.assertEqual(outcome["iterations_used"], 2)
        self.assertEqual(self.executed, [("demo_tool", {"x": 1})])
        self.assertEqual(len(outcome["tool_calls_log"]), 1)
        self.assertEqual(outcome["tool_calls_log"][0]["result_status"], "success")

    def test_02_iteration_limit_finalizes_without_tools(self) -> None:
        main.MAX_TOOL_ITERATIONS = 3  # 临时把上限压到 3 轮方便测试
        self.addCleanup(lambda: setattr(main, "MAX_TOOL_ITERATIONS", 10))

        outcome, client = self.run_chat([
            tool_round(("demo_tool", {"n": 1})),
            tool_round(("demo_tool", {"n": 2})),
            tool_round(("demo_tool", {"n": 3})),
            final_round("已达到上限，我先把已查到的信息给你：……"),
        ])

        self.assertEqual(outcome["stop_reason"], "iteration_limit")
        self.assertEqual(outcome["iterations_used"], 3)
        self.assertIn("已达到上限", outcome["response"])
        self.assertEqual(len(self.executed), 3)

        # 收尾那次必须是「不带工具」的请求，并带一条提醒用户的消息
        final_kwargs = client.chat.completions.calls[-1]
        self.assertNotIn("tools", final_kwargs)
        self.assertNotIn("tool_choice", final_kwargs)
        self.assertIn("已达到工具调用上限", final_kwargs["messages"][-1]["content"])

    def test_03_finalize_failure_falls_back_to_summary(self) -> None:
        main.MAX_TOOL_ITERATIONS = 2
        self.addCleanup(lambda: setattr(main, "MAX_TOOL_ITERATIONS", 10))

        outcome, client = self.run_chat([
            tool_round(("demo_tool", {"n": 1})),
            tool_round(("demo_tool", {"n": 2})),
            RuntimeError("收尾调用也挂了"),
        ])

        self.assertEqual(outcome["stop_reason"], "iteration_limit")
        self.assertIn("工具调用上限", outcome["response"])       # 兜底摘要
        self.assertIn("demo_tool", outcome["response"])           # 带上了工具记录
        self.assertEqual(len(outcome["tool_calls_log"]), 2)

    def test_04_repeated_identical_call_stops_early(self) -> None:
        main.MAX_IDENTICAL_TOOL_CALLS = 2
        self.addCleanup(lambda: setattr(main, "MAX_IDENTICAL_TOOL_CALLS", 2))

        same = ("demo_tool", {"serial": "Q2XX"})
        outcome, client = self.run_chat([
            tool_round(same),      # 第 1 次执行
            tool_round(same),      # 第 2 次执行（达到上限）
            tool_round(same),      # 第 3 次：不再执行 -> 收尾
            final_round("同一个调用重复了，我先给结论：……"),
        ])

        self.assertEqual(outcome["stop_reason"], "repeated_tool_calls")
        self.assertEqual(len(self.executed), 2)                   # 只执行了两次
        statuses = [entry["result_status"] for entry in outcome["tool_calls_log"]]
        self.assertEqual(statuses, ["success", "success", "skipped"])
        self.assertIn("重复", outcome["response"])

    def test_05_same_round_duplicates_executed_once(self) -> None:
        outcome, client = self.run_chat([
            tool_round(("demo_tool", {"a": 1}), ("demo_tool", {"a": 1}), ("other_tool", {})),
            final_round("好了"),
        ])

        self.assertEqual(len(self.executed), 2)                   # 去重后只执行 2 次
        self.assertEqual(len(outcome["tool_calls_log"]), 3)        # 日志仍记 3 条
        self.assertTrue(outcome["tool_calls_log"][1].get("deduplicated"))
        # 每条 tool_call 都必须有对应的 tool 消息，否则下一次请求会被模型 API 拒绝
        first_request = client.chat.completions.calls[1]
        tool_messages = [m for m in first_request["messages"] if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 3)

    def test_06_request_max_iterations_overrides_and_clamps(self) -> None:
        outcome, client = self.run_chat(
            [tool_round(("demo_tool", {"n": 1})), tool_round(("demo_tool", {"n": 2})), final_round("收尾")],
            max_iterations=2,
        )
        self.assertEqual(outcome["iterations_used"], 2)
        self.assertEqual(outcome["stop_reason"], "iteration_limit")

        # 非法值不应炸：夹到 [1, 50]
        outcome2, _ = self.run_chat([final_round("直接回答")], max_iterations="不是数字")
        self.assertEqual(outcome2["stop_reason"], "completed")
        self.assertEqual(outcome2["iterations_used"], 1)

    def test_07_failed_tool_result_marked_error(self) -> None:
        outcome, _ = self.run_chat([
            tool_round(("boom_tool", {})),
            final_round("工具报错了，我换个说法告诉你"),
        ])
        self.assertEqual(outcome["tool_calls_log"][0]["result_status"], "error")

    def test_08_no_tools_means_single_call(self) -> None:
        outcome, client = self.run_chat([final_round("无需工具的普通回答")], tools=None)
        self.assertEqual(outcome["stop_reason"], "completed")
        self.assertEqual(outcome["iterations_used"], 1)
        self.assertNotIn("tools", client.chat.completions.calls[0])

    def test_09_repeated_tool_errors_get_a_stop_retrying_notice(self) -> None:
        main.MAX_TOOL_ERRORS_BEFORE_NOTICE = 3
        self.addCleanup(lambda: setattr(main, "MAX_TOOL_ERRORS_BEFORE_NOTICE", 3))

        outcome, client = self.run_chat([
            tool_round(("boom_tool", {"n": 1})),
            tool_round(("boom_tool", {"n": 2})),
            tool_round(("boom_tool", {"n": 3})),
            final_round("工具一直失败，我直接说明情况"),
        ])

        self.assertEqual(outcome["stop_reason"], "completed")
        # 第 3 次失败时，发给模型的 tool 消息里必须带上「别再用这个工具」的提示
        last_request_messages = client.chat.completions.calls[-1]["messages"]
        tool_messages = [m for m in last_request_messages if m.get("role") == "tool"]
        self.assertEqual(len(tool_messages), 3)
        self.assertNotIn("系统提示", tool_messages[1]["content"])
        self.assertIn("已连续失败 3 次", tool_messages[2]["content"])
        self.assertIn("不要再重试", tool_messages[2]["content"])

    def test_10_other_tools_do_not_reset_the_error_counter(self) -> None:
        main.MAX_TOOL_ERRORS_BEFORE_NOTICE = 2
        self.addCleanup(lambda: setattr(main, "MAX_TOOL_ERRORS_BEFORE_NOTICE", 3))

        outcome, client = self.run_chat([
            tool_round(("boom_tool", {"n": 1})),    # boom 失败第 1 次
            tool_round(("demo_tool", {"n": 2})),    # 别的工具成功，不影响 boom 的计数
            tool_round(("boom_tool", {"n": 3})),    # boom 第 2 次失败 -> 加提示
            final_round("好"),
        ])

        tool_messages = [m for m in client.chat.completions.calls[-1]["messages"] if m.get("role") == "tool"]
        self.assertIn("已连续失败 2 次", tool_messages[-1]["content"])

    def test_11_same_tool_success_resets_its_error_counter(self) -> None:
        main.MAX_TOOL_ERRORS_BEFORE_NOTICE = 2
        self.addCleanup(lambda: setattr(main, "MAX_TOOL_ERRORS_BEFORE_NOTICE", 3))

        # boom 失败 1 次 -> 中间成功一次（计数归零）-> 再失败 1 次：不该出现"连续失败"提示
        self.install_client_script = [
            tool_round(("boom_tool", {"n": 1})),
        ]
        client_holder = {}

        def run():
            return self.run_chat([
                tool_round(("boom_tool", {"n": 1})),
                tool_round(("boom_tool", {"n": 2})),   # 这一轮前把 boom 变成会成功的
                tool_round(("boom_tool", {"n": 3})),
                final_round("好"),
            ])

        # 用一个包装：第 2 次调用 boom 前切掉 fail_tools，第 3 次再打开
        execute_original = self._fake_execute_tool
        call_counter = {"n": 0}

        def wrapped(name, arguments):
            if name == "boom_tool":
                call_counter["n"] += 1
                if call_counter["n"] == 2:
                    self.fail_tools = set()      # 这一次让它成功 -> 计数重置
                else:
                    self.fail_tools = {"boom_tool"}
            return execute_original(name, arguments)

        main._execute_tool = wrapped
        outcome, client = run()
        tool_messages = [m for m in client.chat.completions.calls[-1]["messages"] if m.get("role") == "tool"]
        self.assertNotIn("不要再重试", tool_messages[-1]["content"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
