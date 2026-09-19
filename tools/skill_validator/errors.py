"""技能验证错误类型"""


class ValidationError(Exception):
    """验证错误基类"""

    def __init__(self, message: str, field: str = "", path: str = ""):
        super().__init__(message)
        self.message = message
        self.field = field
        self.path = path

    def __str__(self) -> str:
        parts = [self.message]
        if self.field:
            parts.append(f"字段: {self.field}")
        if self.path:
            parts.append(f"路径: {self.path}")
        return " | ".join(parts)


class ParseError(ValidationError):
    """YAML/Markdown 解析错误"""
    pass


class FormatError(ValidationError):
    """格式规范错误"""
    pass


class ContentError(ValidationError):
    """内容质量错误"""
    pass
