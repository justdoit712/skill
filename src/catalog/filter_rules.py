"""阶段过滤规则（filter-rules.json）加载、校验与匹配模块。

依据 docs/收藏与阶段过滤实施方案.md：
1. 检索发现阶段（discovery）：检查 name、path、description 字段是否命中 blocked_keywords；
2. 评估出口阶段（evaluation）：新技能首次评估成功后检查 tags 是否命中 blocked_tags。
"""

from __future__ import annotations

import json
from pathlib import Path
import re
from typing import Any


DEFAULT_FILTER_RULES_PATH = "config/governance/filter-rules.json"

ALLOWED_TOP_KEYS = frozenset({"filter_rules_version", "discovery", "evaluation"})
ALLOWED_DISCOVERY_KEYS = frozenset({"blocked_keywords"})
ALLOWED_EVALUATION_KEYS = frozenset({"blocked_tags"})


class DiscoveryMatchResult:
    __slots__ = ("matched", "keyword", "field")

    def __init__(self, matched: bool, keyword: str | None = None, field: str | None = None) -> None:
        self.matched = matched
        self.keyword = keyword
        self.field = field

    def __bool__(self) -> bool:
        return self.matched

    def __iter__(self):
        return iter((self.matched, self.keyword, self.field))

    def __getitem__(self, index: int):
        return (self.matched, self.keyword, self.field)[index]


class EvaluationMatchResult:
    __slots__ = ("matched", "tag")

    def __init__(self, matched: bool, tag: str | None = None) -> None:
        self.matched = matched
        self.tag = tag

    def __bool__(self) -> bool:
        return self.matched

    def __iter__(self):
        return iter((self.matched, self.tag))

    def __getitem__(self, index: int):
        return (self.matched, self.tag)[index]


def compile_keyword_pattern(keyword: str) -> re.Pattern:
    r"""按方案 §3.2 编译发现阶段关键词正则：
    - 忽略英文大小写；
    - 空格、连字符、下划线作为等价分隔，连续分隔统一处理，允许连写（如 App Store -> app[\s\-_]*store）；
    - 英文和数字词使用边界限制：expo 不匹配 export，App Store 不匹配 app storefront；
    - 中文字面包含匹配。
    """
    kw = keyword.strip()
    # 检查是否包含英文字母或数字
    has_alnum = bool(re.search(r"[a-zA-Z0-9]", kw))
    if not has_alnum:
        return re.compile(re.escape(kw), re.IGNORECASE)

    # 拆分 tokens
    tokens = [t for t in re.split(r"[\s\-_]+", kw) if t]
    if not tokens:
        return re.compile(re.escape(kw), re.IGNORECASE)

    escaped_tokens = [re.escape(t) for t in tokens]
    inner_pattern = r"[\s\-_]*".join(escaped_tokens)

    prefix = r"(?<![a-zA-Z0-9])" if re.match(r"^[a-zA-Z0-9]", tokens[0]) else ""
    suffix = r"(?![a-zA-Z0-9])" if re.search(r"[a-zA-Z0-9]$", tokens[-1]) else ""

    full_pattern = f"{prefix}{inner_pattern}{suffix}"
    return re.compile(full_pattern, re.IGNORECASE)


class FilterRules:
    """阶段过滤规则运行时单例/实例，预编译并线程安全复用。"""

    def __init__(
        self,
        data: dict[str, Any] | None = None,
        blocked_keywords: list[str] | None = None,
        blocked_tags: list[str] | None = None,
    ) -> None:
        data = data or {}
        disc = data.get("discovery") or {}
        eval_cfg = data.get("evaluation") or {}

        raw_keywords = blocked_keywords if blocked_keywords is not None else (disc.get("blocked_keywords") or [])
        raw_tags = blocked_tags if blocked_tags is not None else (eval_cfg.get("blocked_tags") or [])

        self.blocked_keywords: list[str] = [k for k in raw_keywords if isinstance(k, str) and k.strip()]
        self._compiled_keywords: list[tuple[str, re.Pattern]] = [
            (k, compile_keyword_pattern(k)) for k in self.blocked_keywords
        ]

        # 标签规范化集合：首尾去空白，用于精确完整匹配
        self.blocked_tags: list[str] = [t for t in raw_tags if isinstance(t, str) and t.strip()]
        self._blocked_tags_lower: set[str] = {t.strip().lower() for t in self.blocked_tags}

    def matches_discovery(
        self,
        name: str | None = None,
        path: str | None = None,
        description: str | None = None,
    ) -> DiscoveryMatchResult:
        """检查发现结果中的 name、path、description 是否命中关键词。

        各字段独立匹配；不跨字段拼接。
        返回: DiscoveryMatchResult (支持 bool 判断和元组解构: matched, keyword, field)
        """
        fields = [
            ("name", name or ""),
            ("path", path or ""),
            ("description", description or ""),
        ]
        for field_name, val in fields:
            if not val:
                continue
            for kw, pattern in self._compiled_keywords:
                if pattern.search(val):
                    return DiscoveryMatchResult(True, kw, field_name)
        return DiscoveryMatchResult(False, None, None)

    def matches_evaluation(self, tags: list[str] | None = None) -> EvaluationMatchResult:
        """检查本次评估返回的 tags 是否命中过滤标签。

        去掉标签首尾空白后，按完整标签匹配（英文忽略大小写）。
        返回: EvaluationMatchResult (支持 bool 判断和元组解构: matched, tag)
        """
        if not tags or not self._blocked_tags_lower:
            return EvaluationMatchResult(False, None)
        for tag in tags:
            if not isinstance(tag, str):
                continue
            normalized = tag.strip().lower()
            if normalized in self._blocked_tags_lower:
                return EvaluationMatchResult(True, tag.strip())
        return EvaluationMatchResult(False, None)

    @property
    def has_discovery_rules(self) -> bool:
        return bool(self._compiled_keywords)

    @property
    def has_evaluation_rules(self) -> bool:
        return bool(self._blocked_tags_lower)


def validate_filter_rules(data: Any) -> list[str]:
    """校验 filter-rules.json 数据结构（§2.3）。

    返回错误描述列表；为空表示校验通过。
    """
    errors: list[str] = []
    if not isinstance(data, dict):
        return ["filter-rules 根结构必须是 JSON 对象"]

    # 检查顶层键
    unknown_top = set(data.keys()) - ALLOWED_TOP_KEYS
    if unknown_top:
        errors.append(f"filter-rules 包含未识别的顶层键：{', '.join(sorted(unknown_top))}")

    # 校验 discovery 阶段
    if "discovery" in data:
        disc = data["discovery"]
        if not isinstance(disc, dict):
            errors.append("discovery 阶段配置必须是对象")
        else:
            unknown_disc = set(disc.keys()) - ALLOWED_DISCOVERY_KEYS
            if unknown_disc:
                errors.append(f"discovery 包含未识别的规则键：{', '.join(sorted(unknown_disc))}")
            if "blocked_keywords" in disc:
                kws = disc["blocked_keywords"]
                if not isinstance(kws, list):
                    errors.append("discovery.blocked_keywords 必须是数组")
                else:
                    for idx, item in enumerate(kws):
                        if not isinstance(item, str) or not item.strip() or item != item.strip():
                            errors.append(
                                f"discovery.blocked_keywords[{idx}] 必须是去掉首尾空白后非空的字符串：{repr(item)}"
                            )

    # 校验 evaluation 阶段
    if "evaluation" in data:
        eval_cfg = data["evaluation"]
        if not isinstance(eval_cfg, dict):
            errors.append("evaluation 阶段配置必须是对象")
        else:
            unknown_eval = set(eval_cfg.keys()) - ALLOWED_EVALUATION_KEYS
            if unknown_eval:
                errors.append(f"evaluation 包含未识别的规则键：{', '.join(sorted(unknown_eval))}")
            if "blocked_tags" in eval_cfg:
                tags = eval_cfg["blocked_tags"]
                if not isinstance(tags, list):
                    errors.append("evaluation.blocked_tags 必须是数组")
                else:
                    for idx, item in enumerate(tags):
                        if not isinstance(item, str) or not item.strip() or item != item.strip():
                            errors.append(
                                f"evaluation.blocked_tags[{idx}] 必须是去掉首尾空白后非空的字符串：{repr(item)}"
                            )

    return errors


def filter_discovered_candidates(
    candidates: list[Any],
    existing_ids: set[str] | None = None,
    filter_rules: Any = None,
    log: Any = print,
    report: dict[str, Any] | None = None,
) -> list[Any]:
    """对新发现的候选条目执行检索阶段关键词过滤（§3）。

    已有条目（已有索引、已有候选池、已有推荐等）不补查。
    """
    if not filter_rules or not hasattr(filter_rules, "matches_discovery") or not filter_rules.has_discovery_rules:
        return candidates

    known = existing_ids or set()
    accepted = []
    filtered_count = 0
    for c in candidates:
        if isinstance(c, dict):
            sid = c.get("skill_id", "")
            c_name = c.get("name", "")
            c_path = c.get("path") or c.get("skill_path") or ""
            c_desc = c.get("description", "")
        else:
            sid = getattr(c, "skill_id", "")
            c_name = getattr(c, "name", "")
            c_path = getattr(c, "path", "") or getattr(c, "skill_path", "")
            c_desc = getattr(c, "description", "")

        if sid and sid in known:
            accepted.append(c)
            continue

        hit, kw, field_name = filter_rules.matches_discovery(c_name, c_path, c_desc)
        if hit:
            filtered_count += 1
            if callable(log):
                log(f"[阶段过滤] 检索命中关键词 '{kw}'（字段: {field_name}），跳过条目: {sid or c_name}")
            if report is not None and isinstance(report, dict):
                fl = report.setdefault("discovery_filtered", [])
                fl.append({"skill_id": sid or c_name, "keyword": kw, "field": field_name})
        else:
            accepted.append(c)

    if filtered_count > 0 and callable(log):
        log(f"[阶段过滤] 检索阶段共拦截 {filtered_count} 个包含屏蔽关键词的新候选条目。")
    return accepted


def successful_evaluation_skill_ids(*directories: Path) -> set[str]:
    """运行开始时读取成功评估身份，避免把目录尚未恢复的旧结果当作首次评估。"""
    skill_ids = set()
    for directory in set(directories):
        for path in directory.glob("*.json"):
            record = json.loads(path.read_text(encoding="utf-8"))
            if (record.get("status") == "completed"
                    and isinstance((record.get("outcome") or {}).get("evaluation"), dict)
                    and record.get("skill_id")):
                skill_ids.add(record["skill_id"])
    return skill_ids


def filter_new_evaluation(
    decision: dict,
    evaluation: dict,
    filter_rules: FilterRules | None,
    previous_entry: dict | None = None,
    *,
    previously_evaluated: bool = False,
) -> dict:
    """仅在首次成功评估出口过滤标签；已有业务状态和成功历史均豁免。"""
    previous = previous_entry or {}
    if (not filter_rules or not filter_rules.has_evaluation_rules
            or previously_evaluated or previous.get("manual_pick")
            or previous.get("status") in ("recommended", "candidate")
            or previous.get("evaluated_at") or previous.get("last_evaluation_id")):
        return decision

    hit, tag = filter_rules.matches_evaluation(evaluation.get("tags") or [])
    if not hit:
        return decision
    return {
        **decision,
        "original_decision": dict(decision),
        "decision": "excluded",
        "reason_codes": list(dict.fromkeys([*(decision.get("reason_codes") or []), "TAG_FILTERED"])),
        "tag_filtered": True,
        "blocked_tag": tag,
    }


def load_filter_rules(path: str | Path = DEFAULT_FILTER_RULES_PATH) -> FilterRules:
    """加载 filter-rules.json；文件不存在或为空时返回默认空规则。若存在但格式非法则抛出 ValueError。"""
    file_path = Path(path)
    if not file_path.exists():
        # 兼容备选路径
        if "governance" not in file_path.parts:
            alt = file_path.parent / "governance" / file_path.name
            if alt.exists():
                file_path = alt
        else:
            alt = file_path.parent.parent / file_path.name
            if alt.exists():
                file_path = alt

    if not file_path.exists():
        return FilterRules()

    try:
        content = json.loads(file_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise ValueError(f"读取阶段过滤规则失败（{file_path}）：{exc}") from exc

    errs = validate_filter_rules(content)
    if errs:
        raise ValueError(f"阶段过滤规则配置非法（{file_path}）：" + "；".join(errs))

    return FilterRules(content)
