"""基础设施与模型层单元测试：文件原子读写、模型切换、模型队列自动轮换。

合并自原有 test_files.py、test_switch_model.py 及模型池契约测试。
"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

from src.infra.files import read_json, write_json_atomic
from src.infra.model_config import resolve_model_path
from src.infra.model_pool import ModelPool
from src.shared.model_config import parse_model_configs
from tests import smoke
from tools.switch_model import mask_key, switch_to_provider


@smoke
class FilesTest(unittest.TestCase):
    """文件系统原子写入与安全读取。"""
    def setUp(self):
        self.tmp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp_dir.name)

    def tearDown(self):
        self.tmp_dir.cleanup()

    def test_write_and_read_json(self):
        target = self.root / "sample.json"
        data = {"key": "value", "list": [1, 2, 3]}
        write_json_atomic(target, data)
        self.assertTrue(target.exists())
        loaded = read_json(target)
        self.assertEqual(loaded, data)

    def test_write_creates_parent_directories(self):
        target = self.root / "deep" / "nested" / "dir" / "data.json"
        self.assertFalse(target.parent.exists())
        write_json_atomic(target, {"hello": "world"})
        self.assertTrue(target.exists())
        self.assertEqual(read_json(target), {"hello": "world"})

    def test_read_json_default_when_not_found(self):
        missing = self.root / "does_not_exist.json"
        self.assertIsNone(read_json(missing))
        self.assertEqual(read_json(missing, default={}), {})
        self.assertEqual(read_json(missing, default="fallback"), "fallback")

    def test_write_json_atomic_cleans_tmp_on_serialization_failure(self):
        target = self.root / "bad.json"
        bad_payload = {"unserializable": lambda x: x}
        with self.assertRaises(TypeError):
            write_json_atomic(target, bad_payload)
        self.assertFalse(target.exists())
        tmp_files = list(self.root.glob("*.tmp"))
        self.assertEqual(len(tmp_files), 0, f"Found lingering temporary files: {tmp_files}")

    def test_write_json_atomic_replaces_existing(self):
        target = self.root / "update.json"
        write_json_atomic(target, {"v": 1})
        self.assertEqual(read_json(target)["v"], 1)
        write_json_atomic(target, {"v": 2})
        self.assertEqual(read_json(target)["v"], 2)


@smoke
class SwitchModelTest(unittest.TestCase):
    """多厂商模型配置切换与保护。"""
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.temp_dir.name)
        self.models_dir = self.root / "config" / "models"
        self.model_json = self.models_dir / "model.json"
        self.model_local = self.models_dir / "model.local.json"
        self.models_dir.mkdir(parents=True, exist_ok=True)
        for name, value in [
            ('ROOT', self.root),
            ('MODELS_DIR', self.models_dir),
            ('MODEL_JSON_PATH', self.model_json),
            ('MODEL_LOCAL_PATH', self.model_local),
            ('PROVIDERS_LOCAL_PATH', self.models_dir / 'providers.local.json'),
        ]:
            patched = patch('tools.switch_model.' + name, value)
            patched.start()
            self.addCleanup(patched.stop)

        (self.models_dir / "deepseek.json").write_text(
            json.dumps({
                "model_config_version": "1.0.0",
                "name": "DeepSeek",
                "provider": "deepseek",
                "endpoint": "https://api.deepseek.com/chat/completions",
                "model": "deepseek-chat",
                "auth": {"key_ref": "deepseek"},
            }),
            encoding="utf-8",
        )
        (self.models_dir / "bailian.json").write_text(
            json.dumps({
                "model_config_version": "1.0.0",
                "name": "阿里云百炼平台",
                "provider": "dashscope",
                "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                "model": "qwen3.8-max",
                "auth": {"key_ref": "bailian"},
                "available_models": {
                    "kimi-k3": {"name": "Kimi-K3", "limits": {"max_output_tokens": 8000}},
                    "glm-5.3": {"name": "GLM-5.3", "limits": {"max_output_tokens": 16000}},
                },
            }),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_mask_key(self):
        self.assertEqual(mask_key(None), "[未设置 Key]")
        self.assertEqual(mask_key(""), "[未设置 Key]")
        self.assertEqual(mask_key("short"), "***")
        self.assertEqual(mask_key("sk-12345678"), "sk-***5678")

    def test_switch_to_provider(self):
        self.assertTrue(switch_to_provider("deepseek"))
        self.assertTrue(self.model_json.exists())
        cur = json.loads(self.model_json.read_text(encoding="utf-8"))
        self.assertEqual(cur["provider"], "deepseek")
        self.assertEqual(cur["model"], "deepseek-chat")
        self.assertEqual(cur["auth"]["key_ref"], "deepseek")
        self.assertNotIn("api_key", cur["auth"])

    def test_switch_to_sub_model_in_platform(self):
        self.assertTrue(switch_to_provider("kimi-k3"))
        cur = json.loads(self.model_json.read_text(encoding="utf-8"))
        self.assertEqual(cur["provider"], "dashscope")
        self.assertEqual(cur["model"], "kimi-k3")
        self.assertEqual(cur["name"], "Kimi-K3")
        self.assertEqual(cur["limits"]["max_output_tokens"], 8000)
        self.assertEqual(cur["auth"]["key_ref"], "bailian")
        self.assertNotIn("available_models", cur)

    def test_switch_unlinks_legacy_model_local(self):
        self.model_local.write_text(json.dumps({"provider": "legacy"}), encoding="utf-8")
        self.assertTrue(self.model_local.exists())
        self.assertTrue(switch_to_provider("deepseek"))
        self.assertFalse(self.model_local.exists())
        self.assertTrue(self.model_json.exists())

    def test_refuse_switch_when_queue_active(self):
        self.model_json.write_text(json.dumps({"models": ["qwen-plus", "qwen-max"]}), encoding="utf-8")
        self.assertFalse(switch_to_provider("deepseek"))

    def test_resolve_model_path_hierarchy(self):
        d = self.root / "test_cfg"
        d.mkdir(parents=True, exist_ok=True)
        (d / "models").mkdir(parents=True, exist_ok=True)

        # 1. 默认降级为 models/model.json
        self.assertEqual(resolve_model_path(d), d / "models" / "model.json")

        # 2. 存在根目录 model.json
        (d / "model.json").write_text("{}", encoding="utf-8")
        self.assertEqual(resolve_model_path(d), d / "model.json")

        # 3. 存在 models/model.json 优先于根目录 model.json
        (d / "models" / "model.json").write_text("{}", encoding="utf-8")
        self.assertEqual(resolve_model_path(d), d / "models" / "model.json")

        # 4. 存在根目录 model.local.json 优先于公开 model.json
        (d / "model.local.json").write_text("{}", encoding="utf-8")
        self.assertEqual(resolve_model_path(d), d / "model.local.json")

        # 5. models/model.local.json 优先级最高
        (d / "models" / "model.local.json").write_text("{}", encoding="utf-8")
        self.assertEqual(resolve_model_path(d), d / "models" / "model.local.json")


@smoke
class ModelPoolTest(unittest.TestCase):
    """模型队列解析、自动轮换与状态管理。"""
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def test_parse_model_configs(self):
        raw = {
            "provider": "dashscope",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "auth": {"api_key": "sk-test"},
            "models": [
                "qwen-plus",
                {"model": "qwen-max", "limits": {"max_output_tokens": 4000}}
            ]
        }
        configs = parse_model_configs(raw)
        self.assertEqual(len(configs), 2)
        self.assertEqual(configs[0]["model"], "qwen-plus")
        self.assertEqual(configs[1]["model"], "qwen-max")
        self.assertEqual(configs[1]["limits"]["max_output_tokens"], 4000)

    def test_pool_exhaustion_flow(self):
        from src.infra.model_pool import QUOTA_CODE, PoolStopped
        from src.infra.llm import ModelCallResult
        raw = {
            "provider": "dashscope",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "auth": {"api_key": "sk-test"},
            "models": ["model-a", "model-b"]
        }
        pool = ModelPool(raw, self.root, authoritative=False)
        m1 = pool.select()
        self.assertEqual(m1["model"], "model-a")

        # 标记 model-a 耗尽
        result = ModelCallResult(ok=False, http_status=403, error={"code": QUOTA_CODE})
        pool.exhaust(m1, result)

        m2 = pool.select()
        self.assertEqual(m2["model"], "model-b")

        # 标记 model-b 耗尽 -> 全池耗尽
        pool.exhaust(m2, result)
        with self.assertRaises(PoolStopped) as ctx:
            pool.select()
        self.assertEqual(ctx.exception.reason, "models_exhausted")

    def test_empty_responses_retry_rotate_and_survive_restart(self):
        from datetime import datetime, timedelta, timezone
        from src.infra.llm import call_model
        from src.infra.model_pool import MODEL_COOLDOWN_SECONDS
        raw = {'provider': 'dashscope',
               'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
               'models': ['model-a', 'model-b'], 'request': {'max_attempts': 6}}
        moment = datetime(2026, 10, 2, tzinfo=timezone.utc)
        pool = ModelPool(raw, self.root, authoritative=False, now=lambda: moment)
        session = Mock()
        session.post.side_effect = [Mock(status_code=200, json=Mock(return_value={
            'usage': {'total_tokens': 10},
            'choices': [{'finish_reason': 'stop', 'message': {'content': content}}]}))
            for content in (' \n', '', '{"ok":true}')]
        result, attempts = pool.run('s', 'u', 'catalog_assessment',
            lambda cfg, fmt, context: call_model(cfg, 's', 'u', api_key='fake',
                                                session=session, response_format=fmt), sleep=lambda _: None)
        self.assertEqual((result.ok, [call.requested_model for call in attempts]),
                         (True, ['model-a', 'model-a', 'model-b']))
        restarted = ModelPool(raw, self.root, authoritative=False, now=lambda: moment)
        restarted.start()
        self.assertEqual(restarted.select()['model'], 'model-b')
        expired = ModelPool(raw, self.root, authoritative=False,
                            now=lambda: moment + timedelta(seconds=MODEL_COOLDOWN_SECONDS))
        expired.start()
        self.assertEqual(expired.select()['model'], 'model-a')


@smoke
class ApiKeyResolutionTest(unittest.TestCase):
    """凭据解析：环境变量优先于独立密钥文件与配置文件，且任何输出都不得泄漏凭据本身。"""
    ENV_NAME = "DSH_TEST_LLM_KEY"
    FAKE_ENV_KEY = "sk-env-0000000000000000000000000000"
    FAKE_FILE_KEY = "sk-file-111111111111111111111111111"
    FAKE_SECRET_KEY = "sk-secret-2222222222222222222222222"

    def setUp(self) -> None:
        import os
        os.environ.pop(self.ENV_NAME, None)
        os.environ.pop("SKILL_SECRET_TESTREF", None)
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.tmp_path = Path(self.tmp.name)
        self.secrets_file = self.tmp_path / "secrets.local.json"
        self.secrets_file.write_text(
            json.dumps({"testref": self.FAKE_SECRET_KEY}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        import os
        os.environ.pop(self.ENV_NAME, None)
        os.environ.pop("SKILL_SECRET_TESTREF", None)
        self.tmp.cleanup()

    def cfg(self, **auth) -> dict:
        return {"auth": {"api_key_env": self.ENV_NAME, **auth}}

    def test_env_var_is_used_when_set(self) -> None:
        import os
        from src.infra.llm import resolve_api_key
        os.environ[self.ENV_NAME] = self.FAKE_ENV_KEY
        self.assertEqual(resolve_api_key(self.cfg()), self.FAKE_ENV_KEY)

    def test_env_wins_over_key_ref(self) -> None:
        import os
        from src.infra.llm import resolve_api_key
        os.environ[self.ENV_NAME] = self.FAKE_ENV_KEY
        cfg = self.cfg(key_ref="testref")
        self.assertEqual(resolve_api_key(cfg, config_dir=self.tmp_path), self.FAKE_ENV_KEY)

    def test_env_wins_over_config_file(self) -> None:
        import os
        from src.infra.llm import resolve_api_key
        os.environ[self.ENV_NAME] = self.FAKE_ENV_KEY
        self.assertEqual(resolve_api_key(self.cfg(api_key=self.FAKE_FILE_KEY)), self.FAKE_ENV_KEY)

    def test_key_ref_resolved_from_secrets_local(self) -> None:
        from src.infra.llm import resolve_api_key, api_key_source
        cfg = {"auth": {"key_ref": "testref"}}
        self.assertEqual(resolve_api_key(cfg, config_dir=self.tmp_path), self.FAKE_SECRET_KEY)
        src = api_key_source(cfg, config_dir=self.tmp_path)
        self.assertIn("secrets.local.json[testref]", src)

    def test_key_ref_wins_over_legacy_api_key(self) -> None:
        from src.infra.llm import resolve_api_key
        cfg = {"auth": {"key_ref": "testref", "api_key": self.FAKE_FILE_KEY}}
        self.assertEqual(resolve_api_key(cfg, config_dir=self.tmp_path), self.FAKE_SECRET_KEY)

    def test_falls_back_to_config_literal(self) -> None:
        from src.infra.llm import resolve_api_key
        self.assertEqual(resolve_api_key(self.cfg(api_key=self.FAKE_FILE_KEY)), self.FAKE_FILE_KEY)

    def test_missing_credentials_error_mentions_both_places(self) -> None:
        from src.infra.llm import call_model
        result = call_model(self.cfg(), "system", "user")
        self.assertFalse(result.ok)
        self.assertIn(self.ENV_NAME, result.error)
        self.assertIn("api_key", result.error)


class ModelDiagnosticsTest(unittest.TestCase):
    """LLM 调用异常与诊断信息安全。"""
    def test_length_is_non_retryable_sample_error_with_usage(self):
        from src.infra.llm import call_model
        usage = {"prompt_tokens": 120, "completion_tokens": 8000, "total_tokens": 8120,
                 "completion_tokens_details": {"reasoning_tokens": 7000}}
        response = Mock(status_code=200)
        response.json.return_value = {"choices": [{"finish_reason": "length",
            "message": {"content": "partial"}}], "usage": usage}
        session = Mock()
        session.post.return_value = response
        result = call_model({"model": "test", "limits": {"max_output_tokens": 8000},
            "request": {"max_attempts": 6}}, "s", "u", api_key="fake", session=session)
        self.assertFalse(result.ok)
        self.assertTrue(result.is_sample_error)
        self.assertEqual(result.reason_code, "LENGTH_EXCEEDED")
        self.assertEqual(result.http_status, 200)
        self.assertEqual(result.usage, usage)

    def test_network_exception_keeps_type_for_safe_logging(self):
        import requests
        from src.infra.llm import call_model
        session = Mock()
        session.post.side_effect = requests.exceptions.ReadTimeout("sensitive response detail")
        result = call_model({"model": "test", "endpoint": "https://example.invalid",
                             "request": {"max_attempts": 1}}, "system", "user",
                            api_key="test-only", session=session)
        self.assertEqual(result.reason_code, "NETWORK_ERROR")
        self.assertEqual(result.error_type, "ReadTimeout")
        self.assertIsNone(result.http_status)


if __name__ == "__main__":
    unittest.main()
