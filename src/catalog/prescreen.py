"""预筛：在调用 AI 之前尽可能排除（§5.1）。

输入是候选与 config/ 下的配置，输出只有两种结论：excluded 或 queued。
**设计上偏向"排队"而非"排除"**——只有在证据明确时才排除；查询词覆盖不到、
或需要读内容才能判断的（实盘下单、临床决策、凭据外传），只落 flag 交给评估层，
避免误杀正常条目。

权威排除项在 config/taxonomy.json 与 config/sources.json，本模块只把它们
落到可匹配的形式，不在代码里另立一份领域或规则。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from src.shared.identity import parse_github_url
from .models import Candidate, PrescreenResult

# DSH 插件：§1 与 §4.2 要求无论由哪条渠道发现均排除
DSH_PATTERNS = (
    re.compile(r"(?i)(^|[/_.-])dsh([/_.-]|$)"),
    re.compile(r"(?i)dsh\s*plugin"),
)

# 模板与占位命名：属"非技能"
PLACEHOLDER_PATTERNS = (
    re.compile(r"(?i)(^|[/_-])_?template([/_-]|$)"),
    re.compile(r"(?i)(^|[/_-])(your|my|example|sample|demo)-skill([/_-]|$)"),
)

# 需评估层重点复核的信号。预筛不据此排除——这些判断需要读内容与证据（§5.2）
SUSPECT_PATTERNS = {
    "LIVE_TRADING_SUSPECT": re.compile(
        r"(?i)实盘|自动下单|下单执行|交易执行|券商接口|"
        r"auto[-_ ]?trad|place[-_ ]?order|order[-_ ]?execution|live[-_ ]?trad|broker[-_ ]?api"
    ),
    "CLINICAL_DECISION_SUSPECT": re.compile(
        r"(?i)临床诊断|治疗决策|开处方|"
        r"clinical[-_ ]?(decision|diagnos)|treatment[-_ ]?decision|prescription[-_ ]?engine"
    ),
    "CREDENTIAL_EXFILTRATION_SUSPECT": re.compile(
        r"(?i)凭据外传|窃取密钥|exfiltrat|steal[-_ ]?credential|harvest[-_ ]?credential"
    ),
}

DECISION_EXCLUDED = "excluded"
DECISION_QUEUED = "queued"


@dataclass
class PrescreenConfig:
    """预筛所需的全部配置，一次加载后复用。"""

    taxonomy: dict = field(default_factory=dict)
    rules: dict = field(default_factory=dict)
    searches: dict = field(default_factory=dict)
    self_repos: set[str] = field(default_factory=set)
    out_of_scope_orgs: set[str] = field(default_factory=set)
    out_of_scope_repos: set[str] = field(default_factory=set)
    domain_terms: dict[str, list[str]] = field(default_factory=dict)
    domain_names: dict[str, str] = field(default_factory=dict)
    manual_exclusions: set[str] = field(default_factory=set)

    @property
    def hard_exclusion_ids(self) -> list[str]:
        return [h["id"] for h in self.taxonomy.get("hard_exclusions", [])]

    @property
    def weekly_quota(self) -> int:
        return int(self.rules.get("weekly_quota", 0))


def load_config(config_dir: str | Path = "config") -> PrescreenConfig:
    """加载并交叉引用四个配置文件及人工覆盖。"""
    base = Path(config_dir)
    def _res(fname: str, sub: str) -> Path:
        sub_p = base / sub / fname
        flat_p = base / fname
        if flat_p.exists() and sub_p.exists():
            try:
                return flat_p if flat_p.stat().st_mtime >= sub_p.stat().st_mtime else sub_p
            except OSError:
                return flat_p
        if flat_p.exists():
            return flat_p
        return sub_p

    taxonomy = json.loads(_res("taxonomy.json", "standards").read_text(encoding="utf-8"))
    rules = json.loads(_res("rules.json", "standards").read_text(encoding="utf-8"))
    searches = json.loads(_res("searches.json", "discovery").read_text(encoding="utf-8"))
    sources = json.loads(_res("sources.json", "discovery").read_text(encoding="utf-8"))

    manual_exclusions: set[str] = set()
    overrides_file = _res("overrides.json", "governance")
    if overrides_file.exists():
        try:
            from .overrides import load_overrides, get_manual_exclusions
            overrides = load_overrides(overrides_file)
            manual_exclusions = set(get_manual_exclusions(overrides).keys())
        except Exception:
            pass

    gx = searches.get("global_exclusions", {})
    self_repos = {r.lower() for r in gx.get("repos", []) if r}
    out_of_scope_orgs = {o.lower() for o in gx.get("orgs", []) if o}
    out_of_scope_repos: set[str] = set()

    # sources.json 中标记排除的来源，其仓库与组织一并排除，防止经其他渠道重新进入
    for source in sources.get("sources", []):
        if not (source.get("exclusion") or {}).get("excluded"):
            continue
        owner, repo, _, _ = parse_github_url(source.get("url") or "")
        if owner:
            out_of_scope_orgs.add(owner)
        if owner and repo:
            out_of_scope_repos.add(f"{owner}/{repo}")

    domain_terms: dict[str, list[str]] = {}
    for domain_id, spec in searches.get("per_domain", {}).items():
        terms = [t for t in (list(spec.get("zh", [])) + list(spec.get("en", []))) if t]
        domain_terms[domain_id] = terms

    domain_names = {
        c["id"]: c["name"] for c in taxonomy.get("main_categories", []) if c.get("id")
    }

    return PrescreenConfig(
        taxonomy=taxonomy,
        rules=rules,
        searches=searches,
        self_repos=self_repos,
        out_of_scope_orgs=out_of_scope_orgs,
        out_of_scope_repos=out_of_scope_repos,
        domain_terms=domain_terms,
        domain_names=domain_names,
        manual_exclusions=manual_exclusions,
    )


def _result(candidate: Candidate, decision: str) -> PrescreenResult:
    return PrescreenResult(skill_id=candidate.skill_id, decision=decision)


def _exclude(result: PrescreenResult, code: str, note: str) -> PrescreenResult:
    result.decision = DECISION_EXCLUDED
    result.reason_codes.append(code)
    result.notes.append(note)
    return result


def match_domains(text: str, cfg: PrescreenConfig) -> tuple[list[str], list[str]]:
    """按 searches.json 的词表匹配领域，返回 (命中的领域 id, 命中的词)。"""
    lowered = text.lower()
    domains: list[str] = []
    matched: list[str] = []
    for domain_id, terms in cfg.domain_terms.items():
        for term in terms:
            if term.lower() in lowered:
                if domain_id not in domains:
                    domains.append(domain_id)
                if term not in matched:
                    matched.append(term)
    return domains, matched


def prescreen(candidate: Candidate, cfg: PrescreenConfig, text: str | None = None) -> PrescreenResult:
    """对单个候选做预筛。

    text 为可选的上游原文；预筛不依赖它也能给出结论。
    """
    result = _result(candidate, DECISION_QUEUED)

    # 0. 人工排除黑名单（manual_exclusions）：最高优先级，直接跳过
    if candidate.skill_id in cfg.manual_exclusions:
        return _exclude(
            result,
            "MANUAL_EXCLUDED",
            "已在 config/overrides.json 中被用户人工排除/删除，直接跳过",
        )

    haystack = " ".join(
        part for part in (candidate.name, candidate.description, candidate.path, candidate.url) if part
    )
    repo_key = f"{candidate.owner}/{candidate.repo}".lower()

    # 1. DSH 插件：最高优先级，无论来源
    if any(p.search(haystack) for p in DSH_PATTERNS):
        return _exclude(result, "DSH_PLUGIN", "命中 DSH 插件特征，§1 与 §4.2 要求一律排除")

    # 2. 自身仓库
    if repo_key in cfg.self_repos:
        return _exclude(result, "SELF_REPO", "指向本导航目录自身仓库")

    # 3. 非技能：模板与占位命名
    for pattern in PLACEHOLDER_PATTERNS:
        if pattern.search(haystack):
            return _exclude(result, "NOT_A_SKILL", f"命中模板或占位命名（{pattern.pattern}）")

    # 4. 范围外来源：sources.json 的排除记录与 global_exclusions.orgs
    if candidate.owner.lower() in cfg.out_of_scope_orgs or repo_key in cfg.out_of_scope_repos:
        return _exclude(
            result,
            "NOT_STANDALONE_DIRECTION",
            "来源已在 config 中标记排除（如 AI 开发类合集），整库不收录",
        )

    # 5. 领域匹配：命中即为线索；未命中不排除，只标记
    domains, matched = match_domains(haystack, cfg)
    result.domains = domains
    result.matched_terms = matched
    if not domains:
        result.flags.append("NO_DOMAIN_TERM_MATCH")
        result.notes.append(
            "未命中任何领域词表；词表不完整时不据此排除（§4.3 允许逐步完善），交由评估层判断"
        )

    # 6. 需读内容才能判断的，只落 flag
    for flag, pattern in SUSPECT_PATTERNS.items():
        if pattern.search(haystack):
            result.flags.append(flag)

    if not candidate.path:
        result.flags.append("COLLECTION_NEEDS_EXPANSION")
        result.notes.append("指向仓库根，合集须展开到具体技能（§4.2）")

    if text is not None and len(text.strip()) < 200:
        result.flags.append("CONTENT_MAY_BE_EMPTY")
        result.notes.append("上游内容过短，可能为空模板")

    return result


def prescreen_all(
    candidates: list[Candidate], cfg: PrescreenConfig, texts: dict[str, str] | None = None
) -> list[PrescreenResult]:
    """批量预筛。texts 以 skill_id 为键，缺省表示未抓取内容。"""
    lookup = texts or {}
    return [prescreen(c, cfg, lookup.get(c.skill_id)) for c in candidates]
