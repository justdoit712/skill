"""测试 shared/materials.py 纯材料契约与集合指纹。"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
import unittest

from src.shared.materials import (
    DocumentSnapshot,
    MaterialBundle,
    validate_document,
)


class TestMaterials(unittest.TestCase):
    def test_document_snapshot_immutability(self):
        doc = DocumentSnapshot(
            path="skills/math/SKILL.md",
            text="# Math Skill\nUseful for math.",
            fingerprint="sha256:11111111111111111111111111111111",
            fetched_at="2026-09-23T00:00:00Z",
            source_url="https://example.com/math",
        )
        with self.assertRaises(FrozenInstanceError):
            doc.text = "modified text"
        with self.assertRaises(FrozenInstanceError):
            doc.path = "other/path"

    def test_material_bundle_fingerprint_determinism(self):
        doc1 = DocumentSnapshot(
            path="skills/tool/SKILL.md",
            text="# Tool Skill",
            fingerprint="sha256:aaaa",
            fetched_at="2026-09-23T00:00:00Z",
            source_url="https://example.com/tool",
        )
        doc2 = DocumentSnapshot(
            path="docs/guide.md",
            text="# User Guide",
            fingerprint="sha256:bbbb",
            fetched_at="2026-09-23T00:00:00Z",
            source_url="https://example.com/guide",
        )
        bundle_a = MaterialBundle(
            skill_id="owner/repo:skills/tool/SKILL.md",
            primary_doc=doc1,
            referenced_docs=(doc2,),
        )
        # 即使 fetched_at 发生变化，集合指纹保持严格一致
        doc1_later = DocumentSnapshot(
            path="skills/tool/SKILL.md",
            text="# Tool Skill",
            fingerprint="sha256:aaaa",
            fetched_at="2026-09-23T12:00:00Z",
            source_url="https://example.com/tool",
        )
        doc2_later = DocumentSnapshot(
            path="docs/guide.md",
            text="# User Guide",
            fingerprint="sha256:bbbb",
            fetched_at="2026-09-23T12:00:00Z",
            source_url="https://example.com/guide",
        )
        bundle_b = MaterialBundle(
            skill_id="owner/repo:skills/tool/SKILL.md",
            primary_doc=doc1_later,
            referenced_docs=(doc2_later,),
        )
        self.assertEqual(bundle_a.bundle_fingerprint, bundle_b.bundle_fingerprint)

    def test_material_bundle_fingerprint_sensitivity(self):
        doc1 = DocumentSnapshot(
            path="skills/tool/SKILL.md",
            text="# Tool Skill",
            fingerprint="sha256:aaaa",
            fetched_at="2026-09-23T00:00:00Z",
            source_url="https://example.com/tool",
        )
        doc2 = DocumentSnapshot(
            path="skills/tool/SKILL.md",
            text="# Tool Skill Changed",
            fingerprint="sha256:cccc",
            fetched_at="2026-09-23T00:00:00Z",
            source_url="https://example.com/tool",
        )
        bundle1 = MaterialBundle(skill_id="s1", primary_doc=doc1)
        bundle2 = MaterialBundle(skill_id="s1", primary_doc=doc2)
        self.assertNotEqual(bundle1.bundle_fingerprint, bundle2.bundle_fingerprint)

    def test_all_materials_property(self):
        doc1 = DocumentSnapshot(
            path="SKILL.md",
            text="Primary Content",
            fingerprint="sha256:p",
            fetched_at="2026-01-01T00:00:00Z",
            source_url="u1",
        )
        doc2 = DocumentSnapshot(
            path="ref.md",
            text="Referenced Content",
            fingerprint="sha256:r",
            fetched_at="2026-01-01T00:00:00Z",
            source_url="u2",
        )
        bundle = MaterialBundle(skill_id="test", primary_doc=doc1, referenced_docs=(doc2,))
        mapping = bundle.all_materials
        self.assertEqual(mapping, {"SKILL.md": "Primary Content", "ref.md": "Referenced Content"})

    def test_validate_document_valid_types(self):
        valid_paths = ["SKILL.md", "skills/foo/SKILL.md", "README.md", "notes/doc.txt"]
        for p in valid_paths:
            ok, msg = validate_document(p, "# Hello World\nSome valid markdown.", max_bytes=1000)
            self.assertTrue(ok, f"Expected {p} to be valid, got {msg}")

    def test_validate_document_rejects_empty(self):
        ok, msg = validate_document("SKILL.md", "", max_bytes=1000)
        self.assertFalse(ok)
        self.assertIn("为空", msg)

        ok2, msg2 = validate_document("SKILL.md", "   \n\t  \n  ", max_bytes=1000)
        self.assertFalse(ok2)
        self.assertIn("为空", msg2)

    def test_validate_document_rejects_html(self):
        html_samples = [
            "<!DOCTYPE html><html><head><title>Login</title></head></html>",
            "<!doctype html>\n<html><body>Please login</body></html>",
            "<html>\n<head><title>404</title></head></html>",
            "<HTML lang='en'>Something</HTML>",
        ]
        for html in html_samples:
            ok, msg = validate_document("SKILL.md", html, max_bytes=1000)
            self.assertFalse(ok)
            self.assertIn("HTML", msg)

    def test_validate_document_rejects_invalid_extension(self):
        invalid_paths = ["main.py", "script.sh", "program.exe", "data.json", "SKILL.py"]
        for p in invalid_paths:
            ok, msg = validate_document(p, "print('hello')", max_bytes=1000)
            self.assertFalse(ok)
            self.assertIn("非受支持", msg)

    def test_validate_document_max_bytes(self):
        text = "a" * 100
        ok, _ = validate_document("SKILL.md", text, max_bytes=100)
        self.assertTrue(ok)

        ok_over, msg_over = validate_document("SKILL.md", text, max_bytes=50)
        self.assertFalse(ok_over)
        self.assertIn("上限", msg_over)


if __name__ == "__main__":
    unittest.main()
