import json
import unittest
from unittest.mock import MagicMock

from src.infra.llm import (
    REASON_RESPONSE_FORMAT_UNSUPPORTED,
    call_model,
    validate_response_format,
)
from src.shared.output_contracts import (
    CONTRACT_VERSION,
    STAGE_CATALOG_ASSESSMENT,
    STAGE_CATALOG_REVIEW,
    STAGE_FINDER_PLAN,
    STAGE_FINDER_CLARIFY,
    STAGE_FINDER_REFLECT,
    STAGE_FINDER_EVALUATION,
    CATALOG_ASSESSMENT_CONTRACT,
    CATALOG_REVIEW_CONTRACT,
    FINDER_PLAN_CONTRACT,
    FINDER_CLARIFY_CONTRACT,
    FINDER_REFLECT_CONTRACT,
    FINDER_EVALUATION_CONTRACT,
    STAGE_CONTRACT_MAP,
    get_stage_contract,
    resolve_response_format,
)
from src.catalog.evaluation import parse_evaluation, OutputSchemaError, OutputJsonError
from src.finder.plan import parse_query_plan


class TestOutputContracts(unittest.TestCase):
    """Unit 8 / P3.3: 严格输出契约与离线载荷测试。"""

    def test_output_contract_schemas_and_version(self):
        """测试各业务阶段 Schema 契约完备性与版本定义。"""
        self.assertEqual(CONTRACT_VERSION, "1.0.0")

        all_contracts = [
            CATALOG_ASSESSMENT_CONTRACT,
            CATALOG_REVIEW_CONTRACT,
            FINDER_PLAN_CONTRACT,
            FINDER_CLARIFY_CONTRACT,
            FINDER_REFLECT_CONTRACT,
            FINDER_EVALUATION_CONTRACT,
        ]

        for contract in all_contracts:
            self.assertEqual(contract["type"], "json_schema")
            js = contract["json_schema"]
            self.assertTrue(isinstance(js["name"], str) and js["name"])
            self.assertTrue(js["strict"])
            schema = js["schema"]
            self.assertEqual(schema["type"], "object")
            self.assertTrue(isinstance(schema["properties"], dict))
            self.assertTrue(isinstance(schema.get("required"), list))

    def test_validate_response_format(self):
        """测试 response_format 格式对象的合法性强校验。"""
        # 合法情况
        self.assertEqual(validate_response_format(None), [])
        self.assertEqual(validate_response_format("json_object"), [])
        self.assertEqual(validate_response_format("text"), [])
        self.assertEqual(validate_response_format({"type": "json_object"}), [])
        self.assertEqual(validate_response_format(FINDER_PLAN_CONTRACT), [])

        # 非法情况
        self.assertTrue(len(validate_response_format("unsupported_type")) > 0)
        self.assertTrue(len(validate_response_format(123)) > 0)
        self.assertTrue(len(validate_response_format({})) > 0)
        self.assertTrue(len(validate_response_format({"type": "json_schema"})) > 0)
        self.assertTrue(len(validate_response_format({"type": "json_schema", "json_schema": {}})) > 0)
        self.assertTrue(
            len(validate_response_format({"type": "json_schema", "json_schema": {"name": "test"}})) > 0
        )

    def test_resolve_response_format_decoupling(self):
        """测试后端能力声明与业务 Schema 完全解耦。"""
        # 1. 后端声明仅支持传统 json_object
        legacy_cfg = {
            "model": "qwen-plus",
            "request": {"response_format": "json_object"},
        }
        res = resolve_response_format(legacy_cfg, STAGE_CATALOG_ASSESSMENT)
        self.assertEqual(res, {"type": "json_object"})

        # 2. 后端声明支持 json_schema
        schema_cfg = {
            "model": "qwen-max",
            "capabilities": {"json_schema": True},
        }
        self.assertEqual(
            resolve_response_format(schema_cfg, STAGE_CATALOG_ASSESSMENT),
            CATALOG_ASSESSMENT_CONTRACT,
        )
        self.assertEqual(
            resolve_response_format(schema_cfg, STAGE_CATALOG_REVIEW),
            CATALOG_REVIEW_CONTRACT,
        )
        self.assertEqual(
            resolve_response_format(schema_cfg, STAGE_FINDER_PLAN),
            FINDER_PLAN_CONTRACT,
        )
        self.assertEqual(
            resolve_response_format(schema_cfg, STAGE_FINDER_EVALUATION),
            FINDER_EVALUATION_CONTRACT,
        )

        # 3. 未指定 stage 时回退为安全的 json_object
        self.assertEqual(
            resolve_response_format(schema_cfg, None),
            {"type": "json_object"},
        )

        # 4. 未声明任何 response_format
        plain_cfg = {"model": "plain-model"}
        self.assertIsNone(resolve_response_format(plain_cfg, None))

    def test_call_model_payload_with_response_format(self):
        """测试 call_model 正确构造并传递 response_format 载荷。"""
        captured_payload = {}

        def mock_post(url, headers=None, data=None, timeout=None):
            nonlocal captured_payload
            captured_payload = json.loads(data.decode("utf-8"))
            resp = MagicMock()
            resp.status_code = 200
            resp.json.return_value = {
                "choices": [{"finish_reason": "stop", "message": {"content": "{}"}}],
                "usage": {"total_tokens": 10},
            }
            return resp

        session = MagicMock()
        session.post = mock_post

        model_cfg = {
            "endpoint": "https://api.openai.com/v1/chat/completions",
            "model": "gpt-4o",
            "auth": {"api_key": "test-key"},
        }

        # 传递严格契约
        res = call_model(
            model_cfg,
            "system",
            "user",
            response_format=FINDER_PLAN_CONTRACT,
            session=session,
        )
        self.assertTrue(res.ok)
        self.assertIn("response_format", captured_payload)
        self.assertEqual(captured_payload["response_format"], FINDER_PLAN_CONTRACT)

        # 传递字符串 json_object
        res2 = call_model(
            model_cfg,
            "system",
            "user",
            response_format="json_object",
            session=session,
        )
        self.assertTrue(res2.ok)
        self.assertEqual(captured_payload["response_format"], {"type": "json_object"})

    def test_unsupported_response_format_explicit_failure(self):
        """测试后端不支持格式参数时明确报错，绝不隐式重发降级。"""
        def mock_post_err(url, headers=None, data=None, timeout=None):
            resp = MagicMock()
            resp.status_code = 400
            resp.text = '{"error": {"message": "response_format of type json_schema is not supported by model"}}'
            return resp

        session = MagicMock()
        session.post = mock_post_err

        model_cfg = {
            "endpoint": "https://api.openai.com/v1/chat/completions",
            "model": "gpt-3.5-turbo",
            "auth": {"api_key": "test-key"},
            "request": {"max_attempts": 3},  # 配置了最多重试 3 次
        }

        res = call_model(
            model_cfg,
            "system",
            "user",
            response_format=FINDER_PLAN_CONTRACT,
            session=session,
        )

        self.assertFalse(res.ok)
        self.assertEqual(res.reason_code, REASON_RESPONSE_FORMAT_UNSUPPORTED)
        # 必须明确立即失败，不浪费重试次数
        self.assertEqual(res.attempts, 1)

    def test_local_parsers_defense_in_depth(self):
        """测试本地解析器作为第二道防线，持续校验业务规则与合法性。"""
        # 即使模型给出了 JSON，但字段不满足目录评估契约时，本地解析器依然拦截
        invalid_assessment_json = json.dumps({"name": "foo"})  # 缺少 checks, summary_zh 等
        with self.assertRaises(OutputSchemaError):
            parse_evaluation(
                invalid_assessment_json,
                rules={"rules_version": "1.0.0"},
                source_fingerprint="fp1",
                taxonomy={"main_categories": []},
            )

        # Finder 规划缺少 criteria 准则
        invalid_plan_json = json.dumps({"intent": "test", "queries": ["q1"]})
        with self.assertRaises(ValueError):
            parse_query_plan(invalid_plan_json)


if __name__ == "__main__":
    unittest.main()
