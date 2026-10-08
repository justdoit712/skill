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

ROOT = Path(__file__).resolve().parents[1]


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

    def test_parameter_rejections_rotate_with_safe_billing_and_configuration_recovery(self):
        from copy import deepcopy
        from src.infra.llm import call_model
        from src.finder.run import FinderRunState
        from src.shared.usage import UsageTotals
        raw = {'provider': 'dashscope',
               'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
               'models': ['model-a', 'model-b'], 'request': {'response_format': 'json_object'}}
        body = {'error': {'code': 'invalid_parameter_error', 'type': 'invalid_request_error',
                         'message': 'parameter.enable_thinking must be set to false for non-streaming calls. sk-sensitive'}}

        def transport(session):
            return lambda cfg, system, user, **kwargs: call_model(cfg, system, user, session=session, **kwargs)

        def session_for(error_body, status=400):
            session = Mock()
            session.post.side_effect = [
                Mock(status_code=status, text=json.dumps(error_body), json=Mock(return_value=error_body)),
                Mock(status_code=200, json=Mock(return_value={'usage': {'total_tokens': 13},
                    'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]})),
            ]
            return session

        for parameter, message in (
                ('enable_thinking', body['error']['message']),
                ('max_tokens', 'Range of max_tokens should be [1, 8192]'),
                ('response_format', 'response_format is not supported for this model')):
            with self.subTest(parameter=parameter):
                root = self.root / parameter
                pool = ModelPool(raw, root, authoritative=False)
                error_body = deepcopy(body)
                error_body['error']['message'] = message
                session = session_for(error_body)
                contexts = []
                def invoke(cfg, fmt, context):
                    contexts.append(context)
                    return call_model(cfg, 's', 'u', api_key='fake', session=session, response_format=fmt)
                result, calls = pool.run('s', 'u', 'catalog_assessment', invoke)
                self.assertTrue(result.ok)
                self.assertEqual([c.requested_model for c in calls], ['model-a', 'model-b'])
                self.assertEqual(contexts[1]['switch_reason'], 'request_incompatible')
                totals = UsageTotals()
                for call in calls:
                    totals.add(call)
                self.assertEqual((totals.requests, totals.unknown_usage_requests, totals.total_tokens), (2, 0, 13))
                self.assertNotIn('sk-sensitive', calls[0].error)
                self.assertEqual(calls[0].incompatible_parameter, parameter)
                restarted = ModelPool(raw, root, authoritative=False)
                restarted.start()
                self.assertEqual(restarted.select()['model'], 'model-b')
                # The standalone preflight switch must not unblock a rejection
                # while production still sends the same request parameters.
                for mode in ('off', 'validate', 'adapt'):
                    with self.subTest(preflight_mode=mode):
                        standalone = deepcopy(raw)
                        standalone['preflight'] = {
                            'mode': mode, 'unknown_spec': 'reject',
                            'uncertain_tokens': 'reject', 'safety_margin_tokens': 512,
                        }
                        unchanged = ModelPool(standalone, root, authoritative=False)
                        unchanged.start()
                        self.assertEqual(unchanged.select()['model'], 'model-b')
                changed = deepcopy(raw)
                changed['models'][0] = {'model': 'model-a', 'limits': {'max_output_tokens': 2000}}
                updated = ModelPool(changed, root, authoritative=False)
                updated.start()
                self.assertEqual(updated.select()['model'], 'model-a')

        # Both callers use the same accounted rotation rather than stopping at
        # the first 400; Finder has an additional HTTP/usage guard to exercise.
        finder = FinderRunState('test', {'max_tokens': 10000}, cfg={'model': raw})
        finder.model_pool = ModelPool(raw, self.root / 'finder', authoritative=False)
        result, unknown = finder.call(transport(session_for(body)), raw, 's', 'u',
                                      api_key='fake', sleep=lambda _: None)
        self.assertTrue(result.ok)
        self.assertFalse(unknown)
        self.assertEqual(finder.report['calls'][1]['switch_reason'], 'request_incompatible')

        for index, (overrides, status, provider, endpoint) in enumerate((
                ({'usage': {'total_tokens': 1}}, 400, 'dashscope', raw['endpoint']),
                ({'usage': {'total_tokens': 'bad'}}, 400, 'dashscope', raw['endpoint']),
                ({'choices': [{'message': {'content': 'partial'}}]}, 400, 'dashscope', raw['endpoint']),
                ({'error': {'code': 'other_error', 'message': 'enable_thinking must be false'}}, 400, 'dashscope', raw['endpoint']),
                ({'error': {'code': 'invalid_parameter_error', 'message': 'request body malformed'}}, 400, 'dashscope', raw['endpoint']),
                ({}, 401, 'dashscope', raw['endpoint']),
                ({}, 403, 'dashscope', raw['endpoint']),
                ({}, 400, 'other', raw['endpoint']),
                ({}, 400, 'dashscope', 'https://untrusted.invalid/chat/completions'))):
            with self.subTest(rejection=index):
                cfg = {**raw, 'provider': provider, 'endpoint': endpoint}
                session = session_for({**deepcopy(body), **overrides}, status)
                pool = None
                if provider == 'dashscope':
                    pool = ModelPool(cfg, self.root / f'negative-{index}', authoritative=False)
                    result, calls = pool.run('s', 'u', 'catalog_assessment',
                        lambda model_cfg, fmt, context: call_model(model_cfg, 's', 'u', api_key='fake', session=session))
                else:
                    single = {key: value for key, value in cfg.items() if key != 'models'}
                    result = call_model({**single, 'model': 'model-a'}, 's', 'u', api_key='fake', session=session)
                self.assertFalse(result.ok)
                self.assertIsNone(result.billing_state)
                self.assertEqual(session.post.call_count, 1)
                if pool is not None:
                    self.assertFalse(pool.inspect().get('incompatible_models'))

    def test_stream_incompatible_rotates_and_marks_incompatible(self):
        from src.infra.llm import call_model
        raw = {'provider': 'dashscope',
               'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
               'models': ['model-a', 'model-b'], 'request': {'response_format': 'json_object'}}
        stream_err = {
            'error': {
                'code': 'invalid_parameter_error',
                'type': 'invalid_request_error',
                'message': 'This model only support stream mode, please enable the stream parameter to access the model.',
            }
        }
        session = Mock()
        session.post.side_effect = [
            Mock(status_code=400, text=json.dumps(stream_err), json=Mock(return_value=stream_err)),
            Mock(status_code=200, json=Mock(return_value={'usage': {'total_tokens': 15},
                'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]})),
        ]
        pool = ModelPool(raw, self.root / 'stream-test', authoritative=False)
        result, calls = pool.run('s', 'u', 'catalog_assessment',
                                 lambda cfg, fmt, ctx: call_model(cfg, 's', 'u', api_key='fake', session=session, response_format=fmt))
        self.assertTrue(result.ok)
        self.assertEqual([c.requested_model for c in calls], ['model-a', 'model-b'])
        self.assertEqual(calls[0].incompatible_parameter, 'stream')
        self.assertEqual(calls[0].billing_state, 'rejected_before_inference')
        state = pool.inspect()
        incomp = state.get('incompatible_models', {})
        self.assertTrue(any(v.get('parameter') == 'stream' for v in incomp.values()))

    def test_transient_failures_cooldown_and_rotate(self):
        from src.infra.llm import call_model
        raw = {'provider': 'dashscope',
               'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
               'models': ['model-a', 'model-b'], 'request': {'max_attempts': 2}}
        session = Mock()
        session.post.side_effect = [
            Mock(status_code=500, text="Internal Server Error", json=Mock(side_effect=ValueError("not JSON"))),
            Mock(status_code=500, text="Internal Server Error", json=Mock(side_effect=ValueError("not JSON"))),
            Mock(status_code=200, json=Mock(return_value={'usage': {'total_tokens': 20},
                'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]})),
        ]
        pool = ModelPool(raw, self.root / 'transient-test', authoritative=False)
        result, calls = pool.run('s', 'u', 'catalog_assessment',
                                 lambda cfg, fmt, ctx: call_model(cfg, 's', 'u', api_key='fake', session=session, response_format=fmt))
        self.assertTrue(result.ok)
        self.assertEqual([c.requested_model for c in calls], ['model-a', 'model-a', 'model-b'])
        self.assertEqual(calls[0].reason_code, 'SERVER_ERROR')
        state = pool.inspect()
        cooldowns = state.get('cooldown_models', {})
        self.assertTrue(any(v.get('reason_code') == 'SERVER_ERROR' for v in cooldowns.values()))

    def test_local_cooldown_respects_expiry(self):
        from datetime import datetime, timedelta, timezone
        from src.infra.llm import ModelCallResult
        raw = {'provider': 'dashscope',
               'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
               'models': ['model-a', 'model-b']}
        now_time = datetime(2026, 10, 8, 12, 0, 0, tzinfo=timezone.utc)
        pool = ModelPool(raw, self.root / 'expiry-test', authoritative=False, now=lambda: now_time)
        m1 = pool.select()
        self.assertEqual(m1['model'], 'model-a')

        res = ModelCallResult(ok=False, reason_code='NETWORK_ERROR')
        pool.cooldown(m1, res, duration=300)

        # Before expiry: model-a is in cooldown, selects model-b
        self.assertEqual(pool.select()['model'], 'model-b')

        # After expiry: model-a becomes available again
        now_time = now_time + timedelta(seconds=301)
        self.assertEqual(pool.select()['model'], 'model-a')

    def test_output_validation_failure_rotates_model_and_preserves_usage(self):
        from src.infra.llm import call_model
        raw = {'provider': 'dashscope',
               'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
               'models': ['model-a', 'model-b']}
        session = Mock()
        session.post.side_effect = [
            Mock(status_code=200, json=Mock(return_value={'usage': {'total_tokens': 12},
                'choices': [{'finish_reason': 'stop', 'message': {'content': 'invalid json output'}}]})),
            Mock(status_code=200, json=Mock(return_value={'usage': {'total_tokens': 18},
                'choices': [{'finish_reason': 'stop', 'message': {'content': '{"parsed": true}'}}]})),
        ]
        def validate(result, cfg):
            if "invalid" in (result.content or ""):
                return False, None, {"error": "JSON parse error"}
            return True, {"parsed": True}, None

        updates = []
        def on_update(res):
            updates.append(res)

        pool = ModelPool(raw, self.root / 'val-test', authoritative=False)
        result, calls = pool.run('s', 'u', 'catalog_assessment',
                                 lambda cfg, fmt, ctx: call_model(cfg, 's', 'u', api_key='fake', session=session, response_format=fmt),
                                 validate_result=validate,
                                 on_request_update=on_update)
        self.assertTrue(result.ok)
        self.assertEqual([c.requested_model for c in calls], ['model-a', 'model-b'])
        self.assertEqual(result.parsed_data, {"parsed": True})
        self.assertEqual(calls[0].usage.get('total_tokens'), 12)
        self.assertEqual(calls[1].usage.get('total_tokens'), 18)
        self.assertEqual(len(updates), 1)
        self.assertEqual(updates[0].reason_code, 'OUTPUT_FORMAT_INVALID')

    def test_candidate_exhaustion_preserves_output_failure(self):
        from src.infra.llm import ModelCallResult
        from src.catalog.failure_policy import classify_result
        for reason in ('OUTPUT_FORMAT_INVALID', 'LENGTH_EXCEEDED'):
            for small_last in (False, True):
                with self.subTest(reason=reason, small_last=small_last):
                    raw = {'provider': 'dashscope',
                           'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
                           'models': ['model-a', {'model': 'model-b', 'limits': {
                               'max_input_bytes': 1 if small_last else 262144}}]}
                    pool = ModelPool(raw, self.root, authoritative=False)
                    def invoke(cfg, fmt, context):
                        return ModelCallResult(
                            ok=reason == 'OUTPUT_FORMAT_INVALID',
                            content='invalid json', http_status=200,
                            reason_code=None if reason == 'OUTPUT_FORMAT_INVALID' else reason,
                            finish_reason='length' if reason == 'LENGTH_EXCEEDED' else 'stop',
                            requested_model=cfg['model'], usage={'total_tokens': 12})
                    result, attempts = pool.run(
                        'system', 'user', 'catalog_assessment', invoke,
                        validate_result=lambda *_: (False, None, {'error': 'invalid JSON'}))
                    self.assertEqual(len(attempts), 1 if small_last else 2)
                    self.assertIs(result, attempts[-1])
                    self.assertEqual(result.reason_code, reason)
                    self.assertEqual(result.usage['total_tokens'], 12)
                    decision = classify_result({'ok': False, 'call': result,
                                                'reason_code': result.reason_code})
                    self.assertEqual(decision.is_format_error, reason == 'OUTPUT_FORMAT_INVALID')
                    self.assertEqual(decision.is_length_exceeded, reason == 'LENGTH_EXCEEDED')

    def test_candidate_input_limit_makes_no_request(self):
        from src.catalog.failure_policy import classify_result
        raw = {'provider': 'dashscope',
               'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
               'models': ['model-a', 'model-b'], 'limits': {'max_input_bytes': 1}}
        pool = ModelPool(raw, self.root, authoritative=False)
        invoke = Mock(side_effect=AssertionError('Oversized input must not be sent'))
        result, attempts = pool.run('system', 'user', 'catalog_assessment', invoke)
        invoke.assert_not_called()
        self.assertEqual(attempts, [])
        self.assertEqual(result.reason_code, 'CANDIDATE_NO_CAPABLE_MODEL')
        decision = classify_result({'ok': False, 'call': result,
                                    'reason_code': result.reason_code})
        self.assertFalse(decision.is_service_failure)

    def test_cooldown_excludes_failed_model_for_candidate(self):
        from src.infra.llm import call_model
        raw = {'provider': 'dashscope',
               'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
               'models': ['model-a', 'model-b', 'model-c'], 'request': {'max_attempts': 1}}
        session = Mock()
        session.post.side_effect = [
            Mock(status_code=500, text="err", json=Mock(side_effect=ValueError())),
            Mock(status_code=500, text="err", json=Mock(side_effect=ValueError())),
            Mock(status_code=200, json=Mock(return_value={'usage': {'total_tokens': 10},
                'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]})),
        ]
        pool = ModelPool(raw, self.root / 'loop-test', authoritative=False)
        result, calls = pool.run('s', 'u', 'catalog_assessment',
                                 lambda cfg, fmt, ctx: call_model(cfg, 's', 'u', api_key='fake', session=session, response_format=fmt))
        self.assertTrue(result.ok)
        # model-a failed -> cooled down & excluded -> model-b failed -> cooled down & excluded -> model-c succeeded
        self.assertEqual([c.requested_model for c in calls], ['model-a', 'model-b', 'model-c'])

    def test_rate_limit_retry_after_cooldown_and_rotate(self):
        from src.infra.llm import call_model
        raw = {'provider': 'dashscope',
               'endpoint': 'https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions',
               'models': ['model-a', 'model-b'], 'request': {'max_attempts': 2}}
        session = Mock()
        resp_429 = Mock(status_code=429, text="Rate limit exceeded", headers={"Retry-After": "60"}, json=Mock(side_effect=ValueError()))
        session.post.side_effect = [
            resp_429,
            Mock(status_code=200, json=Mock(return_value={'usage': {'total_tokens': 10},
                'choices': [{'finish_reason': 'stop', 'message': {'content': '{"ok":true}'}}]})),
        ]
        pool = ModelPool(raw, self.root / 'rate-limit-test', authoritative=False)
        result, calls = pool.run('s', 'u', 'catalog_assessment',
                                 lambda cfg, fmt, ctx: call_model(cfg, 's', 'u', api_key='fake', session=session, response_format=fmt))
        self.assertTrue(result.ok)
        self.assertEqual([c.requested_model for c in calls], ['model-a', 'model-b'])
        self.assertEqual(calls[0].retry_after, 60)
        state = pool.inspect()
        cooldowns = state.get('cooldown_models', {})
        self.assertTrue(any(v.get('reason_code') == 'RATE_LIMIT' for v in cooldowns.values()))


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
    @smoke
    def test_unrecognized_http_error_preserves_redacted_detail_without_rotation(self):
        from src.infra.llm import call_model
        message = ('Unsupported mode for glm. key=actual-secret '
                   'Bearer other-secret sk-hidden api_key=third-secret '
                   'https://example.invalid/?token=hidden \x1b[31m' + 'x' * 800)
        for body in ({"error": {"code": "new_code", "type": "new_type", "message": message}},
                     {"code": "new_code", "message": message}, message):
            with self.subTest(body_type=type(body).__name__):
                response = Mock(status_code=400, text=body if isinstance(body, str) else json.dumps(body))
                if isinstance(body, str):
                    response.json.side_effect = ValueError("not JSON")
                else:
                    response.json.return_value = body
                session = Mock()
                session.post.return_value = response
                result = call_model({"model": "glm-4.5", "provider": "dashscope",
                    "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"},
                    "s", "u", api_key="actual-secret", session=session)
                self.assertIn("Unsupported mode for glm", result.error)
                self.assertEqual(result.reason_code, "MODEL_ERROR")
                self.assertIsNone(result.billing_state)
                self.assertEqual(session.post.call_count, 1)
                self.assertLessEqual(len(result.error), 610)
                for secret in ("actual-secret", "other-secret", "sk-hidden", "third-secret", "https://", "\x1b"):
                    self.assertNotIn(secret, result.error)

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


@smoke
class HttpFetchTest(unittest.TestCase):
    """HTTP 文本抓取与流式正文异常处理。"""

    def test_fetch_text_stream_read_timeout_retries_and_cleans_response(self):
        import requests
        from src.infra.http import fetch_text

        session = Mock()
        resp1 = Mock(status_code=200)
        resp1.iter_content.side_effect = requests.exceptions.ConnectionError("read timeout midway")

        resp2 = Mock(status_code=200)
        resp2.iter_content.return_value = [b"valid content"]

        session.get.side_effect = [resp1, resp2]
        result = fetch_text("https://example.com/SKILL.md", session=session, max_attempts=3, sleep=lambda _: None)
        self.assertTrue(result.ok)
        self.assertEqual(result.text, "valid content")
        self.assertEqual(result.attempts, 2)
        resp1.close.assert_called_once()
        resp2.close.assert_called_once()

    def test_fetch_text_stream_exhausted_retries_returns_network_error(self):
        import requests
        from src.infra.http import fetch_text, REASON_NETWORK_ERROR

        session = Mock()
        resp = Mock(status_code=200)
        resp.iter_content.side_effect = requests.exceptions.ConnectionError("stream dropped")
        session.get.return_value = resp

        result = fetch_text("https://example.com/SKILL.md", session=session, max_attempts=2, sleep=lambda _: None)
        self.assertFalse(result.ok)
        self.assertEqual(result.reason_code, REASON_NETWORK_ERROR)
        self.assertEqual(result.attempts, 2)
        self.assertEqual(resp.close.call_count, 2)


@smoke
class PreflightAndAdaptationTest(unittest.TestCase):
    """模型请求预检与适配闭环验证（Section 11 验收矩阵）。"""

    def setUp(self):
        from src.infra.model_specs import load_specs
        from src.infra.llm_gateway import build_runtime
        self.specs_path = ROOT / "config" / "models" / "model_specs.json"
        self.snapshot = load_specs(self.specs_path)

    def test_preflight_mode_off_bypasses_specs_and_counter(self):
        from src.infra.llm_gateway import prepare
        from src.shared.llm_contracts import RequestIntent
        cfg = {
            "model": "completely-unknown-custom-model",
            "endpoint": "https://custom.endpoint/v1/chat/completions",
            "limits": {"max_output_tokens": 12345},
            "preflight": {"mode": "off"},
        }
        intent = RequestIntent(
            messages=[{"role": "user", "content": "hello"}],
            requested_output_tokens=12345,
        )
        prep = prepare(intent, cfg)
        self.assertIsNone(prep.rejection)
        self.assertIsNotNone(prep.plan)
        plan = prep.plan
        self.assertEqual(plan.preflight_mode, "off")
        self.assertEqual(plan.preflight_outcome, "bypassed")
        self.assertEqual(plan.effective_output_tokens, 12345)
        self.assertEqual(len(plan.adjustments), 0)

    def test_preflight_mode_validate_rejects_output_budget_exceeded(self):
        from src.infra.llm_gateway import build_runtime, prepare
        from src.shared.llm_contracts import RequestIntent, PREFLIGHT_OUTPUT_LIMIT_EXCEEDED
        cfg = {
            "model": "qwen-turbo",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "limits": {"max_output_tokens": 12000},
            "preflight": {"mode": "validate", "unknown_spec": "reject"},
        }
        rt = build_runtime(cfg, spec_snapshot=self.snapshot)
        intent = RequestIntent(
            messages=[{"role": "user", "content": "hello"}],
            requested_output_tokens=12000,
        )
        prep = prepare(intent, cfg, rt)
        self.assertIsNone(prep.plan)
        self.assertIsNotNone(prep.rejection)
        self.assertEqual(prep.rejection.reason_code, PREFLIGHT_OUTPUT_LIMIT_EXCEEDED)

    def test_preflight_mode_adapt_clips_output_and_preserves_prompt(self):
        import json
        from src.infra.llm_gateway import build_runtime, prepare
        from src.shared.llm_contracts import RequestIntent
        cfg = {
            "model": "qwen-turbo",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "limits": {"max_output_tokens": 12000},
            "preflight": {"mode": "adapt", "unknown_spec": "reject"},
        }
        rt = build_runtime(cfg, spec_snapshot=self.snapshot)
        user_prompt = "请详细分析以下代码结构，保持原意不变"
        intent = RequestIntent(
            messages=[{"role": "user", "content": user_prompt}],
            requested_output_tokens=12000,
            response_format={"type": "json_object"},
        )
        prep = prepare(intent, cfg, rt)
        self.assertIsNone(prep.rejection)
        self.assertIsNotNone(prep.plan)
        plan = prep.plan
        self.assertEqual(plan.preflight_mode, "adapt")
        self.assertEqual(plan.preflight_outcome, "adapted")
        self.assertEqual(plan.effective_output_tokens, 8192)
        self.assertEqual(len(plan.adjustments), 1)
        self.assertEqual(plan.adjustments[0]["field"], "max_tokens")
        self.assertEqual(plan.adjustments[0]["effective"], 8192)

        # 验证 payload 严格与计划一致，且 prompt/schema 未被截断或降级
        body = json.loads(plan.encoded_body.decode("utf-8"))
        self.assertEqual(body["max_tokens"], 8192)
        self.assertEqual(body["messages"][0]["content"], user_prompt)
        self.assertEqual(body["response_format"], {"type": "json_object"})

    def test_context_window_and_min_output_tokens_guard(self):
        from src.infra.llm_gateway import build_runtime, prepare
        from src.infra.model_specs import ModelSpec, SpecSnapshot
        from src.shared.llm_contracts import RequestIntent, PREFLIGHT_OUTPUT_BUDGET_TOO_SMALL
        custom_spec = ModelSpec.from_dict({
            "spec_id": "test-tiny",
            "revision": "2026-10-05.1",
            "provider": "dashscope",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "model": "tiny-context-model",
            "mode": "chat",
            "context_window": 200,
            "max_input_tokens": 150,
            "max_output_tokens": 100,
            "context_accounting": "shared",
            "output_budget_semantics": "clipped_to_remaining_context",
            "response_formats": {"json_object": True},
            "token_counter": "conservative_fallback",
            "source": "official_doc",
            "verified_at": "2026-10-05",
        })
        snapshot = SpecSnapshot(
            schema_version="1.0.0",
            revision="test",
            digest="sha256:test",
            specs={custom_spec.match_key: custom_spec},
        )
        cfg = {
            "model": "tiny-context-model",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "limits": {"max_output_tokens": 50},
            "preflight": {"mode": "adapt", "safety_margin_tokens": 50},
        }
        rt = build_runtime(cfg, spec_snapshot=snapshot)

        # 输入 + 安全边际 (50) 占满 200，导致剩余空间不足 min_output_tokens (60)
        intent = RequestIntent(
            messages=[{"role": "user", "content": "A" * 120}],
            requested_output_tokens=80,
            min_output_tokens=60,
        )
        prep = prepare(intent, cfg, rt)
        self.assertIsNotNone(prep.rejection)
        self.assertEqual(prep.rejection.reason_code, PREFLIGHT_OUTPUT_BUDGET_TOO_SMALL)

    def test_unknown_spec_passthrough_vs_reject(self):
        from src.infra.llm_gateway import build_runtime, prepare
        from src.shared.llm_contracts import RequestIntent, PREFLIGHT_SPEC_UNKNOWN
        cfg_pass = {
            "model": "novel-unseen-model",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "limits": {"max_output_tokens": 2048},
            "preflight": {"mode": "validate", "unknown_spec": "passthrough"},
        }
        rt_pass = build_runtime(cfg_pass, spec_snapshot=self.snapshot)
        intent = RequestIntent(
            messages=[{"role": "user", "content": "hi"}],
            requested_output_tokens=2048,
        )
        prep_pass = prepare(intent, cfg_pass, rt_pass)
        self.assertIsNone(prep_pass.rejection)
        self.assertIsNotNone(prep_pass.plan)

        cfg_rej = {
            "model": "novel-unseen-model",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "limits": {"max_output_tokens": 2048},
            "preflight": {"mode": "validate", "unknown_spec": "reject"},
        }
        rt_rej = build_runtime(cfg_rej, spec_snapshot=self.snapshot)
        prep_rej = prepare(intent, cfg_rej, rt_rej)
        self.assertIsNotNone(prep_rej.rejection)
        self.assertEqual(prep_rej.rejection.reason_code, PREFLIGHT_SPEC_UNKNOWN)

    def test_capability_unsupported_rejection(self):
        from src.infra.llm_gateway import build_runtime, prepare
        from src.infra.model_specs import ModelSpec, SpecSnapshot
        from src.shared.llm_contracts import RequestIntent, PREFLIGHT_CAPABILITY_UNSUPPORTED
        custom_spec = ModelSpec.from_dict({
            "spec_id": "test-no-json",
            "revision": "2026-10-05.1",
            "provider": "dashscope",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "model": "no-json-model",
            "mode": "chat",
            "context_window": 8192,
            "max_input_tokens": 6000,
            "max_output_tokens": 2048,
            "context_accounting": "shared",
            "output_budget_semantics": "clipped_to_remaining_context",
            "response_formats": {"json_object": False},
            "token_counter": "conservative_fallback",
            "source": "official_doc",
            "verified_at": "2026-10-05",
        })
        snapshot = SpecSnapshot(
            schema_version="1.0.0",
            revision="test",
            digest="sha256:test",
            specs={custom_spec.match_key: custom_spec},
        )
        cfg = {
            "model": "no-json-model",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "limits": {"max_output_tokens": 1000},
            "preflight": {"mode": "validate"},
        }
        rt = build_runtime(cfg, spec_snapshot=snapshot)
        intent = RequestIntent(
            messages=[{"role": "user", "content": "hi"}],
            requested_output_tokens=1000,
            response_format={"type": "json_object"},
        )
        prep = prepare(intent, cfg, rt)
        self.assertIsNotNone(prep.rejection)
        self.assertEqual(prep.rejection.reason_code, PREFLIGHT_CAPABILITY_UNSUPPORTED)

    def test_preflight_mode_off_true_bypass_without_calling_snapshot(self):
        from src.infra.llm_gateway import PreflightRuntime, prepare
        from src.infra.preflight import PreflightPolicy
        from src.shared.llm_contracts import RequestIntent
        mock_snapshot = Mock()
        mock_counter = Mock()
        rt = PreflightRuntime(
            policy=PreflightPolicy(mode="off"),
            counter=mock_counter,
            spec_snapshot=mock_snapshot,
        )
        cfg = {
            "model": "any-model",
            "endpoint": "https://custom.endpoint/v1/chat/completions",
            "limits": {"max_output_tokens": 12345},
            "preflight": {"mode": "off"},
        }
        intent = RequestIntent(
            messages=[{"role": "user", "content": "hello"}],
            requested_output_tokens=12345,
        )
        prep = prepare(intent, cfg, rt)
        self.assertIsNone(prep.rejection)
        self.assertIsNotNone(prep.plan)
        # 3. 确保关闭时真正旁路：即使传入规格快照，也不调用其查询方法，亦不调用计数器
        self.assertEqual(mock_snapshot.find.call_count, 0)
        self.assertEqual(mock_counter.count_tokens.call_count, 0)
        self.assertEqual(prep.plan.preflight_mode, "off")
        self.assertEqual(prep.plan.preflight_outcome, "bypassed")
        self.assertEqual(prep.plan.effective_output_tokens, 12345)

    def test_configuration_errors_raise_value_error_without_guessing_defaults(self):
        from src.infra.llm_gateway import build_runtime, prepare
        from src.shared.llm_contracts import RequestIntent
        intent = RequestIntent(messages=[{"role": "user", "content": "hi"}], requested_output_tokens=100)
        # 5. 配置错误明确报错：缺少 endpoint 时不猜测 DashScope
        with self.assertRaises(ValueError):
            prepare(intent, {"model": "test-model"})
        # 5. 缺少 model 时不填入 "default"
        with self.assertRaises(ValueError):
            prepare(intent, {"endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions"})

        cfg = {'model': 'test', 'endpoint': 'https://test.invalid/chat/completions'}
        for invalid in (False, [], None, {'safety_margin_token': 123}, {'mode': 'typo'}):
            with self.subTest(invalid_preflight=invalid):
                bad = {**cfg, 'preflight': invalid}
                for validate in (parse_model_configs, build_runtime, lambda c: prepare(intent, c)):
                    with self.assertRaises(ValueError):
                        validate(bad)

        for content in (None, {}, [], 123):
            with self.subTest(content=content), self.assertRaises(ValueError):
                RequestIntent(messages=[{'role': 'user', 'content': content}], requested_output_tokens=100)
        for temperature in (True, '1', float('nan'), float('inf'), -1, 3):
            with self.subTest(temperature=temperature), self.assertRaises(ValueError):
                RequestIntent(messages=[{'role': 'user', 'content': 'hi'}],
                              requested_output_tokens=100, temperature=temperature)

        # Runtime construction must enforce the same integer contract as config parsing.
        for mode in ('off', 'validate', 'adapt'):
            for margin in (True, False, 0.9, '512', None, -1):
                with self.subTest(mode=mode, invalid_margin=margin):
                    with self.assertRaisesRegex(ValueError, 'safety_margin_tokens'):
                        build_runtime({'preflight': {
                            'mode': mode, 'safety_margin_tokens': margin,
                        }}, spec_snapshot=Mock())
            for margin in (0, 512):
                with self.subTest(mode=mode, valid_margin=margin):
                    runtime = build_runtime({'preflight': {
                        'mode': mode, 'safety_margin_tokens': margin,
                    }}, spec_snapshot=Mock())
                    self.assertEqual(runtime.policy.safety_margin_tokens, margin)

    def test_invalid_response_format_raises_error(self):
        from src.infra.llm_gateway import prepare
        from src.shared.llm_contracts import RequestIntent
        # 6. 非法 response_format 严格报错，不能静默删除
        for fmt in (
            {"type": "invalid_type"}, "unsupported_str_type", 123, "json_schema",
            {"type": "json_schema"},
            {"type": "json_schema", "json_schema": {"schema": {}}},
            {"type": "json_schema", "json_schema": {"name": "result"}},
            {"type": "json_schema", "json_schema": {"name": "  ", "schema": {}}},
        ):
            with self.subTest(invalid_format=fmt):
                with self.assertRaises(ValueError):
                    RequestIntent(
                        messages=[{"role": "user", "content": "hi"}],
                        requested_output_tokens=100,
                        response_format=fmt,
                    )

        schema = {"type": "json_schema", "json_schema": {
            "name": "result", "schema": {"type": "object"}, "strict": True,
        }}
        for fmt in ('text', 'json_object', schema):
            with self.subTest(valid_format=fmt):
                intent = RequestIntent(
                    messages=[{"role": "user", "content": "hi"}],
                    requested_output_tokens=100, response_format=fmt,
                )
                plan = prepare(intent, {
                    'model': 'test-model', 'endpoint': 'https://test.invalid/chat/completions',
                    'preflight': {'mode': 'off'},
                }).plan
                self.assertEqual(json.loads(plan.encoded_body)['response_format'],
                                 {'type': fmt} if isinstance(fmt, str) else fmt)

    def test_spec_capability_three_state_distinction(self):
        from src.infra.model_specs import ModelSpec
        # 6. 规格中能力声明严格区分支持、不支持、未知，不能通过 bool(value) 强制转换
        spec = ModelSpec.from_dict({
            "spec_id": "test-tri-state",
            "revision": "2026-10-05.1",
            "provider": "dashscope",
            "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
            "model": "tri-state-model",
            "mode": "chat",
            "context_window": 8192,
            "max_output_tokens": 2048,
            "context_accounting": "shared",
            "output_budget_semantics": "clipped_to_remaining_context",
            "response_formats": {
                "json_object": True,
                "text": False,
                "json_schema": None,  # 未知状态
            },
            "token_counter": "conservative_fallback",
            "source": "test",
            "verified_at": "2026-10-05",
        })
        self.assertIs(spec.response_formats["json_object"], True)
        self.assertIs(spec.response_formats["text"], False)
        self.assertIsNone(spec.response_formats["json_schema"])
        # 非法布尔/None 格式值应报错
        with self.assertRaises(ValueError):
            ModelSpec.from_dict({
                "spec_id": "test-bad",
                "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                "model": "bad-model",
                "context_window": 8192,
                "max_output_tokens": 2048,
                "context_accounting": "shared",
                "output_budget_semantics": "clipped_to_remaining_context",
                "response_formats": {"json_object": "not_a_bool"},
            })

    def test_request_snapshot_and_runtime_are_stable_across_modes(self):
        from dataclasses import replace
        from src.infra.llm_gateway import build_runtime, prepare
        from src.infra.model_specs import ModelSpec, SpecSnapshot
        from src.infra.preflight import PreflightPolicy
        from src.shared.llm_contracts import RequestIntent

        cfg = {'model': 'test', 'provider': 'dashscope',
               'endpoint': 'https://test.invalid/chat/completions',
               'preflight': {'mode': 'adapt', 'safety_margin_tokens': 0}}
        row = {'spec_id': 'test', 'provider': cfg['provider'], 'endpoint': cfg['endpoint'],
               'model': cfg['model'], 'context_window': 10000, 'max_output_tokens': 1000,
               'context_accounting': 'shared', 'output_budget_semantics': 'shared_with_reasoning',
               'response_formats': {'json_schema': True}, 'token_counter': 'conservative'}
        spec = ModelSpec.from_dict(row)
        rows = {spec.match_key: spec}
        snapshot = SpecSnapshot('1.0.0', '1', 'test', rows)
        rows.clear()
        self.assertIs(snapshot.find(cfg['provider'], cfg['endpoint'], cfg['model']), spec)
        with self.assertRaises(TypeError):
            snapshot.specs[spec.match_key] = spec

        schema = {'type': 'json_schema', 'json_schema': {
            'name': 'answer', 'schema': {'type': 'object', 'required': ['answer']}}}
        messages = [{'role': 'user', 'content': 'original'}]
        expected_schema = json.loads(json.dumps(schema))
        intent = RequestIntent(messages=messages, requested_output_tokens=100, response_format=schema)
        schema['json_schema'].clear()
        messages[0]['content'] = 'changed'
        with self.assertRaises(TypeError):
            intent.messages[0]['content'] = 'changed'
        with self.assertRaises(TypeError):
            intent.response_format['json_schema']['schema']['required'][0] = 'changed'

        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'model_specs.json'
            path.write_text(json.dumps({'schema_version': '1.0.0', 'specs': [row]}), encoding='utf-8')
            runtime = build_runtime(cfg, config_dir=tmp)
            path.write_text('invalid JSON after runtime construction', encoding='utf-8')
            with patch('src.infra.llm_gateway.load_specs', side_effect=AssertionError('unexpected I/O')):
                with self.assertRaisesRegex(ValueError, 'build_runtime'):
                    prepare(intent, cfg)
                fingerprints = []
                for _ in range(2):
                    fingerprints.append(prepare(intent, cfg, runtime).plan.compatibility_fingerprint)
                self.assertEqual(*fingerprints)
                for mode in ('off', 'validate', 'adapt'):
                    estimator = Mock(return_value=9000)
                    rt = replace(runtime, policy=PreflightPolicy(mode=mode, safety_margin_tokens=0),
                                 estimate_reservation_fn=estimator, reservation_method='custom-v1')
                    plan = prepare(intent, cfg, rt).plan
                    estimator.assert_called_once_with(intent, 100)
                    self.assertEqual((plan.reservation_tokens, plan.reservation_method), (9000, 'custom-v1'))
                    body = json.loads(plan.encoded_body)
                    self.assertEqual(body['response_format'], expected_schema)
                    self.assertEqual(body['messages'], [{'role': 'user', 'content': 'original'}])

    def test_incomplete_specs_bypass_or_reject_before_counting(self):
        from src.infra.llm_gateway import PreflightRuntime, prepare
        from src.infra.model_specs import ModelSpec, SpecSnapshot, load_specs
        from src.infra.preflight import PreflightPolicy
        from src.shared.llm_contracts import RequestIntent, TokenAssessment, PREFLIGHT_SPEC_UNKNOWN

        cfg = {'model': 'test', 'provider': 'dashscope', 'endpoint': 'https://test.invalid/chat/completions'}
        base = {'spec_id': 'test', 'provider': cfg['provider'], 'endpoint': cfg['endpoint'],
                'model': cfg['model'], 'context_window': 1000, 'max_input_tokens': 1,
                'max_output_tokens': 40, 'context_accounting': 'shared',
                'output_budget_semantics': 'shared_with_reasoning', 'token_counter': 'conservative'}
        intent = RequestIntent(messages=[{'role': 'user', 'content': 'larger than input limit'}],
                               requested_output_tokens=100)
        variants = [{key: value for key, value in base.items() if key != missing}
                    for missing in ('context_window', 'max_output_tokens', 'context_accounting',
                                    'output_budget_semantics', 'token_counter')]
        variants += [{**base, 'context_accounting': 'unsupported'},
                     {**base, 'output_budget_semantics': 'unsupported'},
                     {**base, 'token_counter': 'unknown-counter'}]
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'model_specs.json'
            for row in variants:
                # File loading and direct construction must preserve the same unknowns.
                spec = ModelSpec.from_dict(row)
                path.write_text(json.dumps({'schema_version': '1.0.0', 'specs': [row]}), encoding='utf-8')
                self.assertEqual(load_specs(path).find(cfg['provider'], cfg['endpoint'], cfg['model']), spec)
                snapshot = SpecSnapshot('1.0.0', '1', 'test', {spec.match_key: spec})
                for mode in ('validate', 'adapt'):
                    for unknown in ('passthrough', 'reject'):
                        with self.subTest(row=row, mode=mode, unknown=unknown):
                            counter = Mock(spec_ids=frozenset({'conservative'}))
                            counter.count_tokens.side_effect = AssertionError('must not count')
                            rt = PreflightRuntime(policy=PreflightPolicy(mode=mode, unknown_spec=unknown),
                                                  counter=counter, spec_snapshot=snapshot)
                            result = prepare(intent, cfg, rt)
                            counter.count_tokens.assert_not_called()
                            if unknown == 'passthrough':
                                self.assertEqual((result.plan.preflight_outcome, result.plan.effective_output_tokens),
                                                 ('bypassed', 100))
                            else:
                                self.assertEqual(result.rejection.reason_code, PREFLIGHT_SPEC_UNKNOWN)

        # A declared injected counter is used; no silent fallback to the default.
        spec = ModelSpec.from_dict({**base, 'token_counter': 'custom-exact', 'max_input_tokens': 100})
        counter = Mock(spec_ids=frozenset({'custom-exact'}))
        counter.count_tokens.return_value = TokenAssessment(20, 'exact', 'custom', '1')
        rt = PreflightRuntime(policy=PreflightPolicy(mode='adapt', uncertain_tokens='reject', safety_margin_tokens=0),
                              counter=counter, spec_snapshot=SpecSnapshot('1.0.0', '1', 'test', {spec.match_key: spec}))
        plan = prepare(intent, cfg, rt).plan
        self.assertEqual(plan.effective_output_tokens, 40)
        counter.count_tokens.assert_called_once()


if __name__ == "__main__":
    unittest.main()
