"""测试 Finder 诊断基线、错误分类与用量复算对账。"""

import unittest
from types import SimpleNamespace

from src.finder.run import (
    classify_evaluation_error,
    FinderRunState,
    STATUS_QUOTA_EXHAUSTED,
)
from src.shared.usage import (
    UsageTotals,
    recompute_usage_from_calls,
    audit_usage_reconciliation,
)
from src.shared.versions import (
    build_config_fingerprint,
    get_git_commit_hash,
)
from src.infra.llm import ModelCallResult, REASON_NETWORK_ERROR, REASON_LENGTH_EXCEEDED


class TestFinderDiagnosticsAndAccounting(unittest.TestCase):
    def test_plain_403_is_not_quota_exhausted(self):
        # HTTP 403 免费额度耗尽
        res = ModelCallResult(
            ok=False,
            http_status=403,
            error='HTTP 403: {"error":{"message":"Free quota exhausted.","type":"AllocationQuota.FreeTierOnly"}}',
        )
        code, msg = classify_evaluation_error(res)
        self.assertEqual(code, "auth_error")
        self.assertIn("Free quota exhausted", msg)

    def test_classify_auth_error(self):
        res = ModelCallResult(
            ok=False,
            http_status=401,
            error="HTTP 401: Invalid API Key provided",
        )
        code, msg = classify_evaluation_error(res)
        self.assertEqual(code, "auth_error")

    def test_classify_network_timeout(self):
        res = ModelCallResult(
            ok=False,
            reason_code=REASON_NETWORK_ERROR,
            error="ReadTimeout: HTTPSConnectionPool(host='dashscope.aliyuncs.com', port=443): Read timed out. (read timeout=180.0)",
        )
        code, msg = classify_evaluation_error(res)
        self.assertEqual(code, "network_timeout")

    def test_classify_length_exceeded(self):
        res = ModelCallResult(
            ok=False,
            reason_code=REASON_LENGTH_EXCEEDED,
            finish_reason="length",
            error="输出被截断（finish_reason=length，max_tokens=10000，reasoning_tokens=10000）",
        )
        code, msg = classify_evaluation_error(res)
        self.assertEqual(code, "length_exceeded")

    def test_classify_format_error(self):
        res = ModelCallResult(ok=True, content="{}")
        exc = ValueError("dependencies 必须为最多 5 项的数组")
        code, msg = classify_evaluation_error(res, exc)
        self.assertEqual(code, "format_error")
        self.assertIn("dependencies", msg)

    def test_recompute_usage_from_calls_and_reconcile(self):
        # 构造模拟调用明细：48 次成功，2 次网络超时 (attempts=1, tokens=None)，1 次截断 (total_tokens=10000)
        calls = []
        for i in range(48):
            calls.append({
                "stage": "evaluation",
                "state": "received",
                "usage": {
                    "prompt_tokens": 100,
                    "completion_tokens": 50,
                    "reasoning_tokens": 20,
                    "total_tokens": 150,
                    "attempts": 1,
                },
                "response": {"ok": True},
            })
        # 2 次超时/403
        for i in range(2):
            calls.append({
                "stage": "evaluation",
                "state": "error",
                "usage": {
                    "prompt_tokens": None,
                    "completion_tokens": None,
                    "reasoning_tokens": None,
                    "total_tokens": None,
                    "attempts": 1,
                },
                "response": {"ok": False},
            })

        recomputed = recompute_usage_from_calls(calls).snapshot()
        self.assertEqual(recomputed["requests"], 50)
        self.assertEqual(recomputed["total_tokens"], 48 * 150)
        self.assertEqual(recomputed["unknown_usage_requests"], 2)
        self.assertEqual(recomputed["incomplete_breakdown_requests"], 2)

        # 校验对账差异发现：若持久化记录中仅记录了 1 次未知用量
        recorded_with_discrepancy = dict(recomputed)
        recorded_with_discrepancy["unknown_usage_requests"] = 1

        audit = audit_usage_reconciliation(calls, recorded_with_discrepancy)
        self.assertIsNotNone(audit)
        self.assertEqual(audit["status"], "discrepancy_detected")
        self.assertIn("unknown_usage_requests", audit["discrepancies"])
        self.assertEqual(audit["discrepancies"]["unknown_usage_requests"]["recorded"], 1)
        self.assertEqual(audit["discrepancies"]["unknown_usage_requests"]["recomputed"], 2)

        # 若持久化记录与明细一致
        audit_ok = audit_usage_reconciliation(calls, recomputed)
        self.assertIsNotNone(audit_ok)
        self.assertEqual(audit_ok["status"], "matched")

    def test_environment_and_fingerprint(self):
        cfg = {
            "model": "qwen-max",
            "endpoint": "https://example.com/v1",
            "auth": {"api_key": "sk-secret-12345"},
            "limits": {"max_output_tokens": 4000},
            "request": {"temperature": 0.0, "api_key_header": "Bearer secret"},
        }
        fp = build_config_fingerprint(cfg)
        self.assertTrue(fp.startswith("sha256:"))
        # 敏感信息绝不在指纹原串中被泄露
        self.assertNotIn("sk-secret-12345", fp)

        state = FinderRunState("测试意图", {"limit": 5}, cfg=cfg)
        env = state.report.get("environment", {})
        self.assertEqual(env.get("config_fingerprint"), fp)
        self.assertIsNotNone(env.get("schema_version"))
        self.assertIsNotNone(env.get("output_contract_version"))


if __name__ == "__main__":
    unittest.main()
