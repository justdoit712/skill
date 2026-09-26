"""错误分类器、计数器与批处理容错策略的单元测试（§10）。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from src.catalog.failure_policy import (
    ACTION_BLOCKED,
    ACTION_DONE,
    ACTION_LENGTH_EXCEEDED,
    ACTION_RETRY,
    ERROR_KIND_OUTPUT_JSON_INVALID,
    ERROR_KIND_OUTPUT_SCHEMA_INVALID,
    ERROR_KIND_RESUME_STATE_INVALID,
    REASON_ACCESS_DENIED,
    REASON_LENGTH_EXCEEDED,
    REASON_MODEL_ERROR,
    REASON_NETWORK_ERROR,
    REASON_PARSE_ERROR,
    REASON_REQUEST_CONFIG_ERROR,
    REASON_RESUME_STATE_INVALID,
    STOP_ACCESS_DENIED,
    STOP_CANDIDATES_EXHAUSTED,
    STOP_EVALUATION_LIMIT,
    STOP_FORMAT_FAILURES,
    STOP_INTERRUPTED,
    STOP_MODEL_FAILURES,
    STOP_REQUEST_CONFIG_ERROR,
    STOP_RESUME_STATE_INVALID,
    STOP_RETRY_EXHAUSTED,
    STOP_STORAGE_ERROR,
    STOP_TARGET_REACHED,
    STOP_TOKEN_LIMIT,
    STOP_USAGE_UNKNOWN,
    classify_result,
    resolve_primary_stop_reason,
    update_failure_counters,
)
from src.infra.llm import ModelCallResult


class FailurePolicyTest(unittest.TestCase):
    def test_classify_success(self):
        result = {"ok": True, "evaluation": {"some": "data"}, "stage": "review"}
        decision = classify_result(result)
        self.assertEqual(decision.action, ACTION_DONE)
        self.assertEqual(decision.category, "success")
        self.assertFalse(decision.is_format_error)
        self.assertFalse(decision.is_length_exceeded)
        self.assertFalse(decision.is_service_failure)

    def test_classify_length_exceeded(self):
        call = ModelCallResult(finish_reason="length", reason_code=REASON_LENGTH_EXCEEDED)
        result = {"ok": False, "reason_code": REASON_LENGTH_EXCEEDED, "call": call}
        decision = classify_result(result)
        self.assertEqual(decision.action, ACTION_LENGTH_EXCEEDED)
        self.assertTrue(decision.is_length_exceeded)
        self.assertFalse(decision.is_format_error)
        self.assertFalse(decision.is_service_failure)

    def test_classify_output_json_invalid(self):
        call = ModelCallResult(ok=True, http_status=200)
        result = {
            "ok": False,
            "reason_code": REASON_PARSE_ERROR,
            "error_kind": ERROR_KIND_OUTPUT_JSON_INVALID,
            "stage": "assessment",
            "call": call,
        }
        decision = classify_result(result)
        self.assertEqual(decision.action, ACTION_BLOCKED)
        self.assertEqual(decision.category, "format_failure")
        self.assertEqual(decision.block_reason, "OUTPUT_FORMAT_INVALID")
        self.assertTrue(decision.is_format_error)
        self.assertFalse(decision.is_service_failure)

    def test_classify_output_schema_invalid(self):
        call = ModelCallResult(ok=True, http_status=200)
        result = {
            "ok": False,
            "reason_code": REASON_PARSE_ERROR,
            "error_kind": ERROR_KIND_OUTPUT_SCHEMA_INVALID,
            "stage": "review",
            "call": call,
        }
        decision = classify_result(result)
        self.assertEqual(decision.action, ACTION_BLOCKED)
        self.assertEqual(decision.category, "format_failure")
        self.assertTrue(decision.is_format_error)
        self.assertEqual(decision.stage, "review")

    def test_classify_access_denied_401_403(self):
        for status in (401, 403):
            call = ModelCallResult(http_status=status)
            result = {"ok": False, "call": call}
            decision = classify_result(result)
            self.assertEqual(decision.action, ACTION_BLOCKED)
            self.assertEqual(decision.category, "access_denied")
            self.assertEqual(decision.stop_cause, STOP_ACCESS_DENIED)

    def test_classify_resume_state_invalid(self):
        result = {
            "ok": False,
            "reason_code": REASON_RESUME_STATE_INVALID,
            "error_kind": ERROR_KIND_RESUME_STATE_INVALID,
            "call": None,
        }
        decision = classify_result(result)
        self.assertEqual(decision.action, ACTION_BLOCKED)
        self.assertEqual(decision.category, "resume_state_invalid")
        self.assertEqual(decision.stop_cause, STOP_RESUME_STATE_INVALID)

    def test_classify_request_config_error(self):
        call = ModelCallResult(http_status=400)
        result = {"ok": False, "call": call}
        decision = classify_result(result)
        self.assertEqual(decision.action, ACTION_BLOCKED)
        self.assertEqual(decision.category, "request_config_error")
        self.assertEqual(decision.stop_cause, STOP_REQUEST_CONFIG_ERROR)

    def test_classify_retryable_network_and_server_error(self):
        for status in (429, 500, 502, 503):
            call = ModelCallResult(http_status=status)
            result = {"ok": False, "call": call}
            decision = classify_result(result)
            self.assertEqual(decision.action, ACTION_RETRY)
            self.assertTrue(decision.retryable)
            self.assertTrue(decision.is_service_failure)

    def test_counter_transitions(self):
        # 1. 成功评估：两计数器均清零
        res_ok = {"ok": True}
        dec_ok = classify_result(res_ok)
        self.assertEqual(update_failure_counters(res_ok, dec_ok, 2, 5), (0, 0))

        # 2. 明确 length 截断：通用失败清零，格式异常保持原值
        res_len = {"ok": False, "reason_code": REASON_LENGTH_EXCEEDED}
        dec_len = classify_result(res_len)
        self.assertEqual(update_failure_counters(res_len, dec_len, 2, 5), (0, 5))

        # 3. 格式异常：通用失败清零，格式异常自增 1
        res_fmt = {
            "ok": False,
            "reason_code": REASON_PARSE_ERROR,
            "error_kind": ERROR_KIND_OUTPUT_JSON_INVALID,
        }
        dec_fmt = classify_result(res_fmt)
        self.assertEqual(update_failure_counters(res_fmt, dec_fmt, 2, 5), (0, 6))

        # 4. 服务/模型失败：通用失败加 1，格式异常保持原值
        res_srv = {"ok": False, "reason_code": REASON_MODEL_ERROR}
        dec_srv = classify_result(res_srv)
        self.assertEqual(update_failure_counters(res_srv, dec_srv, 1, 5), (2, 5))

    def test_stop_priority_resolution(self):
        # 验证优先级排序
        causes = {STOP_MODEL_FAILURES, STOP_FORMAT_FAILURES, STOP_TOKEN_LIMIT}
        self.assertEqual(resolve_primary_stop_reason(causes), STOP_TOKEN_LIMIT)

        causes2 = {STOP_FORMAT_FAILURES, STOP_MODEL_FAILURES}
        self.assertEqual(resolve_primary_stop_reason(causes2), STOP_FORMAT_FAILURES)

        causes3 = {STOP_INTERRUPTED, STOP_TOKEN_LIMIT, STOP_USAGE_UNKNOWN}
        self.assertEqual(resolve_primary_stop_reason(causes3), STOP_INTERRUPTED)

        causes4 = {STOP_ACCESS_DENIED, STOP_RETRY_EXHAUSTED}
        self.assertEqual(resolve_primary_stop_reason(causes4), STOP_ACCESS_DENIED)


if __name__ == "__main__":
    unittest.main()
