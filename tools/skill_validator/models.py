"""技能数据模型"""

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple


@dataclass
class SkillMetadata:
    """技能元数据 (YAML frontmatter)"""
    name: str = ""
    description: str = ""
    version: str = ""
    author: str = ""
    tags: List[str] = field(default_factory=list)
    dependencies: List[str] = field(default_factory=list)
    homepage: str = ""
    license: str = ""
    extra: Dict = field(default_factory=dict)  # 非标准字段


@dataclass
class SkillContent:
    """技能正文内容"""
    raw: str = ""                        # 原始 Markdown 正文
    sections: Dict[str, str] = field(default_factory=dict)  # 按标题拆分的段落
    code_blocks: List[str] = field(default_factory=list)    # 代码块列表
    line_count: int = 0


@dataclass
class SkillDocument:
    """完整的技能文档"""
    metadata: SkillMetadata = field(default_factory=SkillMetadata)
    content: SkillContent = field(default_factory=SkillContent)
    raw_text: str = ""        # 完整原始文本
    file_path: str = ""       # 文件路径
    has_frontmatter: bool = False
    frontmatter_raw: str = "" # 原始 frontmatter 文本


@dataclass
class ValidationResult:
    """验证结果"""
    valid: bool = True
    errors: List[str] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)

    def add_error(self, msg: str):
        self.errors.append(msg)
        self.valid = False

    def add_warning(self, msg: str):
        self.warnings.append(msg)

    def merge(self, other: "ValidationResult"):
        self.errors.extend(other.errors)
        self.warnings.extend(other.warnings)
        if other.errors:
            self.valid = False

    def __str__(self) -> str:
        parts = []
        if self.valid:
            parts.append("✅ 通过")
        else:
            parts.append(f"❌ 失败 ({len(self.errors)} 个错误)")
        if self.warnings:
            parts.append(f"⚠️ {len(self.warnings)} 个警告")
        return " | ".join(parts)
