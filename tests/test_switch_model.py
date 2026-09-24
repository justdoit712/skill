"""单元测试：多厂商模型热插拔管理工具 tools/switch_model.py。"""

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
        self.providers_local = self.root / "config" / "models" / "providers.local.json"
        self.providers_example = self.root / "config" / "models" / "providers.example.json"
        self.model_local = self.root / "config" / "models" / "model.local.json"
        (self.root / "config" / "models").mkdir(parents=True, exist_ok=True)

    def tearDown(self):
        self.temp_dir.cleanup()

    def test_mask_key(self):
        self.assertEqual(mask_key(None), "[未设置 Key]")
        self.assertEqual(mask_key(""), "[未设置 Key]")
        self.assertEqual(mask_key("12345"), "***")
        self.assertEqual(mask_key("sk-1234567890abcd"), "sk-***abcd")

    def test_switch_to_provider(self):
        catalog = {
            "active": "deepseek",
            "providers": {
                "deepseek": {
                    "name": "DeepSeek",
                    "provider": "deepseek",
                    "endpoint": "https://api.deepseek.com/chat/completions",
                    "model": "deepseek-chat",
                    "auth": {"api_key": "sk-deepseek-key-1234"},
                },
                "qwen": {
                    "name": "Qwen",
                    "provider": "dashscope",
                    "endpoint": "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
                    "model": "qwen-plus",
                    "auth": {"api_key": "sk-qwen-key-5678"},
                },
            },
        }
        with patch("tools.switch_model.PROVIDERS_LOCAL_PATH", self.providers_local), \
             patch("tools.switch_model.MODEL_LOCAL_PATH", self.model_local):
            
            # 切换到 qwen
            ok = switch_to_provider("qwen", catalog)
            self.assertTrue(ok)
            self.assertEqual(catalog["active"], "qwen")

            # 验证 model.local.json 已被原子写入
            self.assertTrue(self.model_local.exists())
            model_data = json.loads(self.model_local.read_text(encoding="utf-8"))
            self.assertEqual(model_data["provider"], "dashscope")
            self.assertEqual(model_data["model"], "qwen-plus")
            self.assertEqual(model_data["auth"]["api_key"], "sk-qwen-key-5678")

            # 验证 providers.local.json 的 active 已更新
            providers_data = json.loads(self.providers_local.read_text(encoding="utf-8"))
            self.assertEqual(providers_data["active"], "qwen")

            # 切换到不存在的厂商
            fail_ok = switch_to_provider("non_existent", catalog)
            self.assertFalse(fail_ok)


if __name__ == "__main__":
    unittest.main()
