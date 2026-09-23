"""测试 infra/files.py 原子文件读写与安全读取契约。"""

from __future__ import annotations

import json
from pathlib import Path
import tempfile
import unittest

from src.infra.files import read_json, write_json_atomic


class TestFiles(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
