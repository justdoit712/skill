"""公共库单元测试：配置交叉完整性与材料指纹稳定性。

保留核心契约：
1. 预置配置文件预检通过（有效性）。
2. 材料快照集合指纹稳定性与确定性。
"""

from __future__ import annotations

from pathlib import Path
import unittest

from src.catalog.config import load_all_config, precheck
from src.shared.materials import DocumentSnapshot, MaterialBundle
from tests import smoke

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / "config"


class SharedTest(unittest.TestCase):
    """共享层配置与指纹契约测试。"""

    @smoke
    def test_shipped_config_passes_precheck(self) -> None:
        """检查预置全套配置文件的交叉有效性预检。"""
        cfg = load_all_config(CONFIG)
        self.assertEqual(precheck(cfg), [], "配置文件预检应通过")

    def test_material_bundle_fingerprint_determinism(self) -> None:
        """检查同一材料无论何时获取，其集合指纹均确定且稳定不变。"""
        doc1 = DocumentSnapshot(
            path="skills/tool/SKILL.md",
            text="# Tool Skill",
            fingerprint="sha256:aaaa",
            fetched_at="2026-09-23T00:00:00Z",
            source_url="https://example.com/tool",
        )
        bundle_a = MaterialBundle(
            skill_id="owner/repo:skills/tool/SKILL.md",
            primary_doc=doc1,
            referenced_docs=(),
        )
        doc1_later = DocumentSnapshot(
            path="skills/tool/SKILL.md",
            text="# Tool Skill",
            fingerprint="sha256:aaaa",
            fetched_at="2026-09-23T12:00:00Z",
            source_url="https://example.com/tool",
        )
        bundle_b = MaterialBundle(
            skill_id="owner/repo:skills/tool/SKILL.md",
            primary_doc=doc1_later,
            referenced_docs=(),
        )
        self.assertEqual(bundle_a.bundle_fingerprint, bundle_b.bundle_fingerprint)


if __name__ == "__main__":
    unittest.main()
