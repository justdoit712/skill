"""
技能验证工具包

功能:
- 解析 SKILL.md 的 YAML frontmatter
- 验证技能格式规范
- 批量验证技能目录
- 生成技能 XML prompt
"""

from .models import SkillMetadata, SkillContent, SkillDocument, ValidationResult
from .parser import parse_skill_file, parse_skill_text, read_properties, to_prompt
from .validator import SkillValidator
from .errors import ValidationError, ParseError, FormatError, ContentError

__version__ = "1.0.0"

__all__ = [
    "SkillMetadata",
    "SkillContent",
    "SkillDocument",
    "ValidationResult",
    "parse_skill_file",
    "parse_skill_text",
    "read_properties",
    "to_prompt",
    "SkillValidator",
    "ValidationError",
    "ParseError",
    "FormatError",
    "ContentError",
]
