"""单元测试：多厂商模型独立文件热插拔管理工具 tools/switch_model.py。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from tools.switch_model import (
    load_providers_catalog,
    mask_key,
    switch_to_provider,
)


class SwitchModelTest(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.root = Path(self.temp_dir.name)
        self.models_dir = self.root / "config" / "models"
        self.model_local = self.models_dir / "model.local.json"
        self.models_dir.mkdir(parents=True, exist_ok=True)

        # 创建两个独立的厂商私有配置文件
        (self.models_dir / "deepseek.local.json").write_text(
            json.dumps({
                "model_config_version": "1.0.0",
                "name": "DeepSeek",
                "provider": "deepseek",
                "endpoint": "https://api.deepseek.com/chat/completions",
                "model": "deepseek-chat",
                "auth": {"api_key": "sk-deepseek-key-1234"},
            }),
            encoding="utf-8",
        )
        (self.models_dir / "qwen.local.json").write_text(
            json.dumps({
                "model_config_version": "1.0.0",
                "name": "Qwen",
                "provider": "dashscope",
                "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                "model": "qwen-plus",
                "auth": {"api_key": "sk-qwen-key-5678"},
            }),
            encoding="utf-8",
        )

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_mask_key(self):
        self.assertEqual(mask_key(None), "[未设置 Key]")
        self.assertEqual(mask_key(""), "[未设置 Key]")
        self.assertEqual(mask_key("12345"), "***")
        self.assertEqual(mask_key("sk-1234567890abcd"), "sk-***abcd")

    def test_load_independent_provider_files(self):
        catalog = load_providers_catalog(models_dir=self.models_dir)
        self.assertIn("deepseek", catalog["providers"])
        self.assertIn("qwen", catalog["providers"])
        self.assertEqual(catalog["providers"]["deepseek"]["model"], "deepseek-chat")
        self.assertEqual(catalog["providers"]["qwen"]["model"], "qwen-plus")

    def test_switch_to_independent_provider(self):
        catalog = load_providers_catalog(models_dir=self.models_dir)
        with patch("tools.switch_model.MODELS_DIR", self.models_dir), \
             patch("tools.switch_model.MODEL_LOCAL_PATH", self.model_local):
            
            # 切换到 qwen
            ok = switch_to_provider("qwen", catalog)
            self.assertTrue(ok)
            self.assertEqual(catalog["active"], "qwen")

            # 验证 model.local.json 已被原子写入且内容正确
            self.assertTrue(self.model_local.exists())
            model_data = json.loads(self.model_local.read_text(encoding="utf-8"))
            self.assertEqual(model_data["provider"], "dashscope")
            self.assertEqual(model_data["model"], "qwen-plus")
            self.assertEqual(model_data["auth"]["api_key"], "sk-qwen-key-5678")
            self.assertEqual(model_data["active_provider"], "qwen")

            # 切换到不存在的厂商
            fail_ok = switch_to_provider("non_existent", catalog)
            self.assertFalse(fail_ok)


if __name__ == "__main__":
    unittest.main()
