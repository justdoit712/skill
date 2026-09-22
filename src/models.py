"""候选与评估的共享数据结构。

字段对应需求 §6 的索引条目与 §7.3 的评估 ID。发现阶段只有其中一部分可用，
其余字段由后续阶段填充。
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.shared.models import CandidateIdentity


@dataclass
class Candidate(CandidateIdentity):
    """一个待评估的技能候选（包含目录业务属性与发现线索）。"""

    # 发现依据：来自哪个来源、用什么方式、命中哪些查询词（§4.2 要求保留全部发现依据）
    source_ids: list[str] = field(default_factory=list)
    discovery_methods: list[str] = field(default_factory=list)
    search_terms: list[str] = field(default_factory=list)

    # 领域线索：命中的主分类 id，可能为空（预筛不做结论）
    domain_hints: list[str] = field(default_factory=list)


@dataclass
class PrescreenResult:
    """预筛结论。decision 只有两种，不确定一律排队而不是排除（§5.1）。"""

    skill_id: str
    decision: str
    reason_codes: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    domains: list[str] = field(default_factory=list)
    matched_terms: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    @property
    def excluded(self) -> bool:
        return self.decision == "excluded"

    @property
    def queued(self) -> bool:
        return self.decision == "queued"


__all__ = ["Candidate", "CandidateIdentity", "PrescreenResult"]
