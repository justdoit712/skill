"""基础设施核心单元测试：原子写入、凭据脱敏、模型池故障轮换、重试上限与限额保护。

保留 6 项核心行为：
1. 原子写入（覆盖更新与原子性）。
2. 密钥解析与脱敏（优先度与日志隐藏）。
3. 模型故障轮换（冷冻与切新模型）。
4. 重试上限（超出最大重试返回网络错误）。
5. 输入限额（超限拒绝发请求防费用泄露）。
6. 用量保留（输出校验失败轮换但保留 Token 统计）。
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import requests

from src.catalog.failure_policy import classify_result
from src.infra.files import read_json, write_json_atomic
from src.infra.http import fetch_text, REASON_NETWORK_ERROR
from src.infra.llm import api_key_source, call_model, resolve_api_key
from tools.switch_model import mask_key
from src.infra.model_pool import ModelPool
from tests import smoke


class InfraTest(unittest.TestCase):
    """基础设施关键契约测试。"""

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)

    def tearDown(self) -> None:
        self.tmp.cleanup()

    @smoke
    def test_write_json_atomic_replaces_existing(self):
        """原子写入安全覆盖既有文件且保持内容一致。"""
        target = self.root / "update.json"
        write_json_atomic(target, {"v": 1})
        self.assertEqual(read_json(target)["v"], 1)
        write_json_atomic(target, {"v": 2})
        self.assertEqual(read_json(target)["v"], 2)

    def test_key_resolution_and_masking(self):
        """密钥解析优先度与密钥脱敏保护。"""
        secrets_file = self.root / "secrets.local.json"
        secrets_file.write_text(json.dumps({"testref": "sk-secret-12345678"}), encoding="utf-8")
        cfg = {"auth": {"key_ref": "testref"}}

        resolved = resolve_api_key(cfg, config_dir=self.root)
        self.assertEqual(resolved, "sk-secret-12345678")
        self.assertIn("secrets.local.json[testref]", api_key_source(cfg, config_dir=self.root))

        # 脱敏遮蔽断言
        self.assertEqual(mask_key(None), "[未设置 Key]")
        self.assertEqual(mask_key(""), "[未设置 Key]")
        self.assertEqual(mask_key("short"), "***")
        self.assertEqual(mask_key("sk-secret-12345678"), "sk-***5678")

    def test_transient_failures_cooldown_and_rotate(self):
        """模型瞬时故障触发 Cooldown 并自动轮换到后续可用模型。"""
        raw = {
            "provider": "dashscope",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "models": ["model-a", "model-b"],
            "request": {"max_attempts": 2},
        }
        session = Mock()
        session.post.side_effect = [
            Mock(status_code=500, text="Internal Server Error", json=Mock(side_effect=ValueError("not JSON"))),
            Mock(status_code=500, text="Internal Server Error", json=Mock(side_effect=ValueError("not JSON"))),
            Mock(
                status_code=200,
                json=Mock(
                    return_value={
                        "usage": {"total_tokens": 20},
                        "choices": [{"finish_reason": "stop", "message": {"content": '{"ok":true}'}}],
                    }
                ),
            ),
        ]
        pool = ModelPool(raw, self.root / "transient-test", authoritative=False)
        result, calls = pool.run(
            "s",
            "u",
            "catalog_assessment",
            lambda cfg, fmt, ctx: call_model(cfg, "s", "u", api_key="fake", session=session, response_format=fmt),
        )
        self.assertTrue(result.ok)
        self.assertEqual([c.requested_model for c in calls], ["model-a", "model-a", "model-b"])
        self.assertEqual(calls[0].reason_code, "SERVER_ERROR")
        state = pool.inspect()
        cooldowns = state.get("cooldown_models", {})
        self.assertTrue(any(v.get("reason_code") == "SERVER_ERROR" for v in cooldowns.values()))

    def test_fetch_text_stream_exhausted_retries_returns_network_error(self):
        """流式抓取在重试上限耗尽后返回网络异常且关闭连接。"""
        session = Mock()
        resp = Mock(status_code=200)
        resp.iter_content.side_effect = requests.exceptions.ConnectionError("stream dropped")
        session.get.return_value = resp

        result = fetch_text("https://example.com/SKILL.md", session=session, max_attempts=2, sleep=lambda _: None)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, REASON_NETWORK_ERROR)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(resp.close.call_count, 2)

    def test_candidate_input_limit_makes_no_request(self):
        """输入超出字节预算时直接拒绝派发请求，不产生任何网络调用。"""
        raw = {
            "provider": "dashscope",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "models": ["model-a", "model-b"],
            "limits": {"max_input_bytes": 1},
        }
        pool = ModelPool(raw, self.root, authoritative=False)
        invoke = Mock(side_effect=AssertionError("超限输入绝对不得发出网络请求"))
        result, attempts = pool.run("system", "user", "catalog_assessment", invoke)
        invoke.assert_not_called()
        self.assertEqual(attempts, [])
        self.assertEqual(result.reason_code, "CANDIDATE_NO_CAPABLE_MODEL")
        decision = classify_result({"ok": False, "call": result, "reason_code": result.reason_code})
        self.assertFalse(decision.is_service_failure)

    def test_output_validation_failure_rotates_model_and_preserves_usage(self):
        """输出校验失败时轮换模型，且如实累计并保留消耗的 Token 用量。"""
        raw = {
            "provider": "dashscope",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "models": ["model-a", "model-b"],
        }
        session = Mock()
        session.post.side_effect = [
            Mock(
                status_code=200,
                json=Mock(
                    return_value={
                        "usage": {"total_tokens": 12},
                        "choices": [{"finish_reason": "stop", "message": {"content": "invalid json output"}}],
                    }
                ),
            ),
            Mock(
                status_code=200,
                json=Mock(
                    return_value={
                        "usage": {"total_tokens": 18},
                        "choices": [{"finish_reason": "stop", "message": {"content": '{"parsed": true}'}}],
                    }
                ),
            ),
        ]

        def validate(result, cfg):
            if "invalid" in (result.content or ""):
                return False, None, {"error": "JSON parse error"}
            return True, {"parsed": True}, None

        updates = []
        pool = ModelPool(raw, self.root / "val-test", authoritative=False)
        result, calls = pool.run(
            "s",
            "u",
            "catalog_assessment",
            lambda cfg, fmt, ctx: call_model(cfg, "s", "u", api_key="fake", session=session, response_format=fmt),
            validate_result=validate,
            on_request_update=lambda res: updates.append(res),
        )
        self.assertTrue(result.ok)
        self.assertEqual([c.requested_model for c in calls], ["model-a", "model-b"])
        self.assertEqual(result.parsed_data, {"parsed": True})
        self.assertEqual(calls[0].usage.get("total_tokens"), 12)
        self.assertEqual(calls[1].usage.get("total_tokens"), 18)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].reason_code, "OUTPUT_FORMAT_INVALID")


if __name__ == "__main__":
    unittest.main()
