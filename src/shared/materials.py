"""纯材料契约与集合指纹。

杜绝在内存和磁盘中使用松散字典传递评估材料，确立强类型快照与集合指纹。
"""

from __future__ import annotations

from dataclasses import dataclass, field
import hashlib
import re
import json
from collections.abc import Mapping, Iterator
from pathlib import PurePosixPath
from typing import Optional


@dataclass(frozen=True)
class DocumentSnapshot:
    """单个文件的不可变材料快照（评估与证据核验必须使用同一份快照）。"""

    path: str                           # 规范化仓库相对路径（如 "skills/foo/SKILL.md"）
    text: str                           # 实际抓取到的文本内容（已按字节上限截断）
    fingerprint: str                    # 该文本的 SHA-256 指纹（"sha256:..."）
    fetched_at: str                     # ISO 格式抓取时间
    source_url: str                     # 原始下载链接
    resolved_ref: Optional[str] = None  # 确认的 Git Commit SHA（若无法解析则为 None）
    truncated: bool = False             # 是否超过单文件大小上限被截断


@dataclass(frozen=True)
class MaterialBundle(Mapping[str, str]):
    """一个候选技能送审的完整材料包（包含集合指纹与审计摘要）。"""

    skill_id: str
    primary_doc: DocumentSnapshot
    referenced_docs: tuple[DocumentSnapshot, ...] = ()
    fetch_errors: dict[str, str] = field(default_factory=dict)

    @property
    def bundle_fingerprint(self) -> str:
        """基于实际送审的同一批材料文本计算集合指纹，时间不参与摘要。"""
        all_docs = sorted([self.primary_doc] + list(self.referenced_docs), key=lambda d: d.path)
        combined = json.dumps([{ "path": d.path, "fingerprint": d.fingerprint } for d in all_docs], sort_keys=True)
        return "sha256:" + hashlib.sha256(combined.encode("utf-8")).hexdigest()[:32]

    @property
    def all_materials(self) -> dict[str, str]:
        """供评估与证据核验调用的纯路径-文本映射表。"""
        res = {self.primary_doc.path: self.primary_doc.text}
        for doc in self.referenced_docs:
            res[doc.path] = doc.text
        return res

    def __getitem__(self, key: str) -> str:
        return self.all_materials[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self.all_materials)

    def __len__(self) -> int:
        return 1 + len(self.referenced_docs)

    def manifest(self) -> dict:
        docs = [self.primary_doc, *self.referenced_docs]
        return {"identity_version": "bundle-v1", "bundle_fingerprint": self.bundle_fingerprint,
                "revision_consistent": bool(docs[0].resolved_ref) and all(d.resolved_ref == docs[0].resolved_ref for d in docs),
                "documents": [{k: getattr(d, k) for k in ("path", "fingerprint", "fetched_at", "source_url", "resolved_ref")} for d in docs],
                "fetch_errors": dict(self.fetch_errors)}


def validate_document(path: str, text: str, max_bytes: int = 0) -> tuple[bool, str]:
    """对已读取材料校验路径、非空/非 HTML/完整性与截断（纯函数，不发网络请求）。"""
    clean_path = path.strip("/")
    if not path or path.startswith(("/", "\\")) or "\\" in path or ":" in path or ".." in PurePosixPath(path).parts:
        return False, "文档路径必须位于仓库内"
    if not clean_path.endswith("SKILL.md") and not any(clean_path.endswith(ext) for ext in [".md", ".txt"]):
        return False, "非受支持的说明文档类型"
    if not text or not text.strip():
        return False, "文档内容为空"
    # 拒绝 HTML 登录/跳转页面
    if re.search(r"(?i)<!DOCTYPE\s+html|<html[\s>]", text[:500]):
        return False, "内容为 HTML 网页而非有效 Markdown"
    if max_bytes > 0 and len(text.encode("utf-8")) > max_bytes:
        return False, "文档内容超过大小上限"
    return True, "验证通过"


def primary_material_bundle(candidate, text, fetched_at):
    """Catalog currently evaluates the primary file only; do not invent references."""
    from .identity import content_fingerprint
    source = candidate.url or candidate.repo_url
    revision = None
    if "/blob/" in source:
        part = source.split("/blob/", 1)[1].split("/", 1)[0]
        revision = part if re.fullmatch(r"[0-9a-fA-F]{40}", part) else None
    doc = DocumentSnapshot(candidate.path or "SKILL.md", text, content_fingerprint(text), fetched_at, source, revision)
    return MaterialBundle(candidate.skill_id, doc)
