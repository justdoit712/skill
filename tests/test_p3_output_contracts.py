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
from src.catalog.quality import check_quality
from src.finder.plan import parse_query_plan, parse_reflection_queries, parse_clarification_question
from src.finder.evaluation import parse_skill_evaluation


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

    def test_catalog_assessment_and_review_conforming_payload(self):
        """测试符合 CATALOG_ASSESSMENT_CONTRACT 与 CATALOG_REVIEW_CONTRACT 的载荷能被真实解析器成功解析与核验。"""
        text = "# Sample Skill\nProvides code generation."
        rules = {
            "rules_version": "1.0.0",
            "checks": [
                {"id": "scope_match"},
                {"id": "purpose_clarity"},
                {"id": "instruction_completeness"},
                {"id": "evidence_traceability"},
                {"id": "dependency_transparency"},
                {"id": "risk_review"},
            ],
            "quality_review": {"enabled": True},
        }
        taxonomy = {"main_categories": [{"id": "dev", "name": "编程开发"}]}

        payload = {
            "scope_match": {"value": "pass", "evidence": "属于编程开发收录范围", "citations": [{"start_line": 1, "end_line": 1, "quote": "# Sample Skill"}]},
            "purpose_clarity": {"value": "pass", "evidence": "用途明确", "citations": [{"start_line": 1, "end_line": 1, "quote": "# Sample Skill"}]},
            "instruction_completeness": {"value": "pass", "evidence": "包含说明", "citations": [{"start_line": 2, "end_line": 2, "quote": "Provides code generation."}]},
            "evidence_traceability": {"value": "pass", "evidence": "内容一致", "citations": [{"start_line": 1, "end_line": 1, "quote": "# Sample Skill"}]},
            "dependency_transparency": {"value": "pass", "evidence": "无特殊依赖", "citations": [{"start_line": 1, "end_line": 1, "quote": "# Sample Skill"}]},
            "risk_review": {"value": "pass", "evidence": "未见明显风险", "citations": [{"start_line": 1, "end_line": 1, "quote": "# Sample Skill"}]},
            "domain_checks": {},
            "quality_checks": {
                "practical_value": {"value": "pass", "evidence": "实际价值清晰", "citations": [{"start_line": 1, "end_line": 1, "quote": "# Sample Skill"}]},
                "actionability": {"value": "pass", "evidence": "可直接执行", "citations": [{"start_line": 2, "end_line": 2, "quote": "Provides code generation."}]},
                "verification": {"value": "pass", "evidence": "提供输出验证", "citations": [{"start_line": 2, "end_line": 2, "quote": "Provides code generation."}]},
            },
            "summary_zh": "这是一个用于代码生成的实用技能示例。",
            "main_category": "编程开发",
            "skill_type": "tool_script",
            "example_requests": ["生成一个快速排序脚本"],
            "key_features": ["自动化代码生成"],
            "tags": ["python", "codegen"],
            "reason_codes": [],
        }

        # 1. 模拟主评估阶段解析
        parsed = parse_evaluation(json.dumps(payload), rules, "fp_test", taxonomy)
        self.assertEqual(parsed["main_category"], "dev")
        self.assertEqual(parsed["summary_zh"], "这是一个用于代码生成的实用技能示例。")
        self.assertEqual(parsed["skill_type"], "tool_script")

        # 2. 模拟质量核验
        verified = check_quality(parsed, text, rules)
        self.assertEqual(verified["quality_audit"]["review_status"], "not_required")
        self.assertEqual(len(verified["quality_audit"]["invalid_citations"]), 0)

        # 3. 模拟独立复核阶段解析（生产中走相同的 parse_evaluation + check_quality 链路）
        review_parsed = parse_evaluation(json.dumps(payload), rules, "fp_test", taxonomy)
        review_verified = check_quality(review_parsed, text, rules)
        self.assertEqual(review_verified["scope_match"]["value"], "pass")

    def test_finder_reflect_conforming_payload(self):
        """测试符合 FINDER_REFLECT_CONTRACT 的载荷被 parse_reflection_queries 正确解析。"""
        payload = {
            "queries": [
                "python ast refactoring tool",
                "code analysis assistant",
                "automated test generator",
            ]
        }
        res = parse_reflection_queries(json.dumps(payload), previous=["initial query"])
        self.assertEqual(len(res), 3)
        self.assertIn("python ast refactoring tool", res)

    def test_finder_clarify_conforming_payload(self):
        """测试符合 FINDER_CLARIFY_CONTRACT 的载荷被 parse_clarification_question 正确解析。"""
        payload = {
            "focus": "技术栈与目标语言",
            "question": "请问您期望该技能支持哪种编程语言？",
            "options": ["Python", "TypeScript", "Go / Rust", "通用不限"],
            "summary": "已明确需要代码分析与重构工具",
        }
        res = parse_clarification_question(json.dumps(payload))
        self.assertEqual(res["focus"], "技术栈与目标语言")
        self.assertEqual(res["question"], "请问您期望该技能支持哪种编程语言？")
        self.assertEqual(len(res["options"]), 4)
        self.assertEqual(res["summary"], "已明确需要代码分析与重构工具")

    def test_finder_plan_and_evaluation_conforming_payloads(self):
        """测试符合 FINDER_PLAN_CONTRACT 与 FINDER_EVALUATION_CONTRACT 的载荷被真实解析器成功解析。"""
        # 1. Plan 契约
        plan_payload = {
            "intent": "查找代码重构与质量分析技能",
            "queries": ["code refactor tool", "ast refactoring python", "clean code analysis"],
            "criteria": [
                {"id": "ast_support", "kind": "required", "description": "支持基于 AST 的语法树分析与转换"},
                {"id": "type_check", "kind": "quality_signal", "description": "具备类型检查辅助能力"},
            ],
        }
        plan_res = parse_query_plan(json.dumps(plan_payload), topic="代码重构")
        self.assertEqual(plan_res["intent"], "查找代码重构与质量分析技能")
        self.assertEqual(len(plan_res["criteria"]), 2)

        # 2. Evaluation 契约
        eval_payload = {
            "match": "strong",
            "summary_zh": "功能强大的 AST 重构与代码清理技能",
            "criteria_results": [
                {
                    "criterion_id": "ast_support",
                    "status": "supported",
                    "explanation": "明确支持 AST 分析",
                    "evidence": [
                        {
                            "source_path": "SKILL.md",
                            "start_line": 1,
                            "end_line": 2,
                            "quote": "Supports AST refactoring for python projects.",
                        }
                    ],
                },
                {
                    "criterion_id": "type_check",
                    "status": "supported",
                    "explanation": "集成 mypy 检查",
                    "evidence": [],
                },
            ],
            "documentation": "clear",
            "usage_zh": "运行 python -m refactor 进行批量清理",
            "dependencies": ["mypy", "libcst"],
            "limitations": ["目前仅支持 Python 3.10+"],
            "why_consider": "直接利用 AST 避免正则替换的副作用",
        }
        eval_res = parse_skill_evaluation(json.dumps(eval_payload), plan_res["criteria"])
        self.assertEqual(eval_res["match"], "strong")
        self.assertEqual(eval_res["documentation"], "clear")
        self.assertEqual(len(eval_res["criteria_results"]), 2)


if __name__ == "__main__":
    unittest.main()
