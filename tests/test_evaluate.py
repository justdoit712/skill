"""凭据解析：环境变量优先于配置文件，且任何输出都不得泄漏凭据本身。"""

from __future__ import annotations

import inspect
import os
import unittest
from unittest.mock import Mock

import requests

from src.infra.llm import api_key_source, call_model, resolve_api_key

ENV_NAME = "DSH_TEST_LLM_KEY"
FAKE_ENV_KEY = "sk-env-0000000000000000000000000000"
FAKE_FILE_KEY = "sk-file-111111111111111111111111111"


class ApiKeyResolutionTest(unittest.TestCase):
    def setUp(self) -> None:
        os.environ.pop(ENV_NAME, None)

    def tearDown(self) -> None:
        os.environ.pop(ENV_NAME, None)

    def cfg(self, **auth) -> dict:
        return {"auth": {"api_key_env": ENV_NAME, **auth}}

    def test_env_var_is_used_when_set(self) -> None:
        os.environ[ENV_NAME] = FAKE_ENV_KEY
        self.assertEqual(resolve_api_key(self.cfg()), FAKE_ENV_KEY)

    def test_env_wins_over_config_file(self) -> None:
        """部署环境必须能覆盖本地遗留值，否则陈旧的本地配置会遮蔽 Secrets。"""
        os.environ[ENV_NAME] = FAKE_ENV_KEY
        self.assertEqual(resolve_api_key(self.cfg(api_key=FAKE_FILE_KEY)), FAKE_ENV_KEY)

    def test_falls_back_to_config_literal(self) -> None:
        self.assertEqual(resolve_api_key(self.cfg(api_key=FAKE_FILE_KEY)), FAKE_FILE_KEY)

    def test_none_when_neither_source_has_a_value(self) -> None:
        self.assertIsNone(resolve_api_key(self.cfg()))
        self.assertIsNone(resolve_api_key(self.cfg(api_key=None)))
        self.assertIsNone(resolve_api_key(self.cfg(api_key="   ")))

    def test_source_description_names_env_when_env_is_used(self) -> None:
        os.environ[ENV_NAME] = FAKE_ENV_KEY
        self.assertIn(ENV_NAME, api_key_source(self.cfg(api_key=FAKE_FILE_KEY)))

    def test_source_description_names_config_when_config_is_used(self) -> None:
        self.assertIn("配置", api_key_source(self.cfg(api_key=FAKE_FILE_KEY)))

    def test_source_never_contains_the_key(self) -> None:
        for auth in ({"api_key": FAKE_FILE_KEY}, {}):
            with self.subTest(auth=bool(auth)):
                os.environ.pop(ENV_NAME, None)
                source = api_key_source(self.cfg(**auth))
                self.assertNotIn(FAKE_FILE_KEY, source)
        os.environ[ENV_NAME] = FAKE_ENV_KEY
        source = api_key_source(self.cfg(api_key=FAKE_FILE_KEY))
        self.assertNotIn(FAKE_ENV_KEY, source)
        self.assertNotIn(FAKE_FILE_KEY, source)

    def test_missing_credentials_error_mentions_both_places(self) -> None:
        """报错要能告诉人往哪儿设，而不是只说"缺凭据"。"""
        result = call_model(self.cfg(), "system", "user")
        self.assertFalse(result.ok)
        self.assertIn(ENV_NAME, result.error)
        self.assertIn("api_key", result.error)


class NoLeakInSourceTest(unittest.TestCase):
    """禁止把凭据打进日志或错误信息。用 AST 检查调用点，避免子串误报。"""

    CREDENTIAL_NAMES = {"key", "api_key", "apikey", "token", "secret"}

    def _names_in(self, node) -> set[str]:
        import ast

        names: set[str] = set()
        for child in ast.walk(node):
            if isinstance(child, ast.Name):
                names.add(child.id.lower())
            elif isinstance(child, ast.Attribute):
                names.add(child.attr.lower())
        return names

    def test_no_print_call_mentions_credentials(self) -> None:
        import ast
        from src.infra import llm
        from src.catalog import evaluation

        offenders = []
        for mod in (llm, evaluation):
            tree = ast.parse(inspect.getsource(mod))
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                func = node.func
                is_print = (isinstance(func, ast.Name) and func.id == "print") or (
                    isinstance(func, ast.Attribute) and func.attr in {"print", "write", "debug", "info", "warning", "error"}
                )
                if not is_print:
                    continue
                leaked = self._names_in(node) & self.CREDENTIAL_NAMES
                if leaked:
                    offenders.append((getattr(node, "lineno", "?"), sorted(leaked)))
        self.assertEqual(offenders, [], f"疑似输出凭据的调用点：{offenders}")

    def test_error_and_notes_never_contain_the_key(self) -> None:
        os.environ.pop(ENV_NAME, None)
        cfg = {"auth": {"api_key_env": ENV_NAME}, "endpoint": "https://example.invalid", "model": "x"}
        result = call_model(cfg, "s", "u")
        blob = f"{result.error or ''}{result.notes}"
        self.assertNotIn(FAKE_FILE_KEY, blob)
        self.assertNotIn(ENV_NAME + "=", blob)


class ModelDiagnosticsTest(unittest.TestCase):
    def test_network_exception_keeps_type_for_safe_logging(self):
        session = Mock()
        session.post.side_effect = requests.exceptions.ReadTimeout("sensitive response detail")
        result = call_model({"model": "test", "endpoint": "https://example.invalid",
                             "request": {"max_attempts": 1}}, "system", "user",
                            api_key="test-only", session=session)
        self.assertEqual(result.reason_code, "NETWORK_ERROR")
        self.assertEqual(result.error_type, "ReadTimeout")
        self.assertIsNone(result.http_status)
        self.assertEqual(result.usage, {})
        self.assertEqual(session.post.call_count, 1)

    def test_http_error_keeps_status_without_exposing_response_body(self):
        response = Mock(status_code=429, text="sensitive response detail")
        session = Mock()
        session.post.return_value = response
        result = call_model({"model": "test", "endpoint": "https://example.invalid",
                             "request": {"max_attempts": 1}}, "system", "user",
                            api_key="test-only", session=session)
        self.assertEqual(result.reason_code, "MODEL_ERROR")
        self.assertEqual(result.http_status, 429)
        self.assertIsNone(result.error_type)


if __name__ == "__main__":
    unittest.main()
