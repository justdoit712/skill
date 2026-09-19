"""技能验证器"""

import os
import re
from typing import List, Tuple, Optional

from .models import SkillDocument, SkillMetadata, SkillContent, ValidationResult
from .parser import parse_skill_file


class SkillValidator:
    """技能文档验证器"""

    # 标准字段白名单
    STANDARD_FIELDS = {
        "name", "description", "version", "author",
        "tags", "dependencies", "license"
    }

    # name 字段正则: 小写字母、数字、连字符
    NAME_PATTERN = re.compile(r'^[a-z][a-z0-9-]*[a-z0-9]$|^[a-z]$')

    # 最低行数要求
    MIN_LINES = 10
    # 最低描述长度
    MIN_DESC_LENGTH = 10
    # 最高描述长度
    MAX_DESC_LENGTH = 500

    def validate(self, file_path: str) -> Tuple[List[str], List[str]]:
        """
        验证单个技能文件

        Returns:
            (errors, warnings) — errors 为空表示通过
        """
        result = ValidationResult()

        # 1. 文件存在性
        if not os.path.exists(file_path):
            return ([f"文件不存在: {file_path}"], [])

        # 2. 解析
        try:
            doc = parse_skill_file(file_path)
        except Exception as e:
            return ([f"解析失败: {e}"], [])

        # 3. Frontmatter 验证
        self._validate_frontmatter(doc, result)

        # 4. Metadata 验证
        self._validate_metadata(doc.metadata, result)

        # 5. 内容验证
        self._validate_content(doc, result)

        # 6. 目录结构验证
        self._validate_structure(file_path, doc.metadata, result)

        return (result.errors, result.warnings)

    def validate_all(self, skills_dir: str) -> List[dict]:
        """
        批量验证技能目录

        Returns:
            [{"name": ..., "path": ..., "valid": bool, "errors": [...], "warnings": [...]}]
        """
        results = []
        if not os.path.isdir(skills_dir):
            return results

        for entry in sorted(os.listdir(skills_dir)):
            entry_path = os.path.join(skills_dir, entry)
            if not os.path.isdir(entry_path):
                continue
            # 跳过以 _ 开头的目录（模板等特殊目录）
            if entry.startswith("_"):
                continue

            # 查找 SKILL.md（支持双重嵌套）
            skill_root = self._find_skill_root(entry_path)
            if not skill_root:
                results.append({
                    "name": entry,
                    "path": entry_path,
                    "valid": False,
                    "errors": ["Missing required file: SKILL.md"],
                    "warnings": [],
                })
                continue

            errors, warnings = self.validate(skill_root)
            results.append({
                "name": entry,
                "path": skill_root,
                "valid": len(errors) == 0,
                "errors": errors,
                "warnings": warnings,
            })

        return results

    def _find_skill_root(self, dir_path: str) -> Optional[str]:
        """
        查找技能根目录（包含 SKILL.md 的目录）
        支持两种结构:
          1. skills/skill-name/SKILL.md           (单层)
          2. skills/skill-name/skill-name/SKILL.md (双重嵌套)
        """
        # 方案1: 直接在当前目录
        skill_md = os.path.join(dir_path, "SKILL.md")
        if os.path.isfile(skill_md):
            return skill_md

        # 方案2: 在同名子目录中
        dir_name = os.path.basename(dir_path)
        nested = os.path.join(dir_path, dir_name, "SKILL.md")
        if os.path.isfile(nested):
            return nested

        # 方案3: 搜索一级子目录
        for sub in os.listdir(dir_path):
            sub_path = os.path.join(dir_path, sub, "SKILL.md")
            if os.path.isfile(sub_path):
                return sub_path

        return None

    # ── 具体验证逻辑 ──

    def _validate_frontmatter(self, doc: SkillDocument, result: ValidationResult):
        if not doc.has_frontmatter:
            result.add_error("SKILL.md must start with YAML frontmatter (---)")

    def _validate_metadata(self, meta: SkillMetadata, result: ValidationResult):
        # name 必填
        if not meta.name:
            result.add_error("Missing required field: name")
        else:
            # name 格式
            if not self.NAME_PATTERN.match(meta.name):
                result.add_error(
                    f"Skill name '{meta.name}' must be lowercase "
                    f"letters, digits, and hyphens only"
                )
            # name 不能太长
            if len(meta.name) > 64:
                result.add_error(f"Skill name too long (>{64} chars)")

        # description 必填
        if not meta.description:
            result.add_error("Missing required field: description")
        else:
            if len(meta.description) < self.MIN_DESC_LENGTH:
                result.add_error(
                    f"Description too short (<{self.MIN_DESC_LENGTH} chars)"
                )
            if len(meta.description) > self.MAX_DESC_LENGTH:
                result.add_warning(
                    f"Description too long (>{self.MAX_DESC_LENGTH} chars)"
                )

        # version 格式（如果提供）
        if meta.version:
            if not re.match(r'^\d+\.\d+', meta.version):
                result.add_warning(
                    f"Version '{meta.version}' should follow semver (e.g. 1.0.0)"
                )

        # 非标准字段（警告，不是错误）
        for key in meta.extra:
            if key not in self.STANDARD_FIELDS:
                result.add_warning(
                    f"Non-standard field: '{key}' "
                    f"(standard: {', '.join(sorted(self.STANDARD_FIELDS))})"
                )

    def _validate_content(self, doc: SkillDocument, result: ValidationResult):
        content = doc.content

        # 行数检查
        if content.line_count < self.MIN_LINES:
            result.add_error(
                f"Content too short ({content.line_count} lines < {self.MIN_LINES})"
            )

        # 必须有至少一个标题
        if not content.sections:
            result.add_error("Content must have at least one heading")

        # 检查是否有空段落
        empty_sections = [k for k, v in content.sections.items() if not v.strip()]
        if empty_sections:
            result.add_warning(
                f"Empty sections: {', '.join(empty_sections[:3])}"
            )

    def _validate_structure(self, file_path: str, meta: SkillMetadata, result: ValidationResult):
        # 目录名应与 skill name 一致
        dir_path = os.path.dirname(file_path)
        dir_name = os.path.basename(dir_path)

        # 跳过以 _ 开头的目录
        if dir_name.startswith("_"):
            return

        # 对于双重嵌套结构 skills/name/name/SKILL.md
        # 检查内层目录名
        if meta.name and dir_name != meta.name:
            # 检查父目录是否匹配
            parent = os.path.basename(os.path.dirname(dir_path))
            if parent != meta.name:
                result.add_warning(
                    f"Directory name '{dir_name}' does not match skill name '{meta.name}'"
                )
