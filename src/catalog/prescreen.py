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
from src.shared.versions import STATIC_HEURISTIC_VERSION
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

# -------------------------------------------------------------
# 静态规则分级观察模式常量与模式定义 (P3.1 / Unit 6)
# -------------------------------------------------------------

TIER_CLEAR_PLACEHOLDER = "tier_clear_placeholder"
TIER_CONTENT_DEFECT = "tier_content_defect"
TIER_SUSPECT = "tier_suspect"
TIER_NORMAL = "tier_normal"
TIER_PENDING = "tier_pending"
TIER_UNASSESSED = "tier_unassessed"

ACTION_SUGGEST_SKIP = "suggest_skip"
ACTION_SUGGEST_REVIEW = "suggest_review"
ACTION_SUGGEST_EVALUATE = "suggest_evaluate"
ACTION_SUGGEST_PENDING = "suggest_pending"

# 明确的空壳占位行模式（全行主要为占位指示，不构成功能说明）
PLACEHOLDER_LINE_PATTERNS = (
    re.compile(r"(?i)^\s*(?:#+\s*)?(?:todo|fixme|tbd|placeholder)\b.*"),
    re.compile(r"(?i)^\s*(?:#+\s*)?(?:write|add|implement)\s+(?:your\s+)?(?:skill|code|description|implementation)\s+(?:here|below).*"),
    re.compile(r"(?i)^\s*(?:#+\s*)?your[-_\s]+skill[-_\s]+(?:here|name|implementation).*"),
    re.compile(r"(?i)^\s*(?:#+\s*)?(?:example|sample|demo)\s+skill\s+template.*"),
)

# 路线图 TODO（未来版本的计划，通常带版本号或未来时态，不等于空壳）
ROADMAP_TODO_PATTERNS = (
    re.compile(r"(?i)\b(?:v\d+|version\s*\d+|roadmap|future|later|next\s+release|v2|v3)\b"),
    re.compile(r"(?i)\b(?:add|support)\s+[\w-]+\s+(?:in|for)\s+(?:v\d+|next|future)\b"),
)

# 常见可执行命令与代码块指示（证明文档具备真实指引）
COMMAND_INSTRUCTION_PATTERNS = (
    re.compile(r"(?i)\b(?:python|node|npm|pnpm|bun|bash|sh|curl|docker|pip|go|cargo)\s+[\w.-]+"),
    re.compile(r"(?i)\b(?:run|execute|start|install|usage|example)[:\s]"),
    re.compile(r"```[\w]*\n[\s\S]+?```"),
)

# 引用入口与相对参考模式（指向未读取的外部或相对参考入口，保留为待判断，严禁直接判为空壳）
REFERENCE_ENTRY_PATTERNS = (
    re.compile(r"\[([^\]]+)\]\(([^)]+)\)"),
    re.compile(r"<a\s+[^>]*href=[\"'][^\"']+[\"']", re.IGNORECASE),
    re.compile(r"(?i)(?:see|refer(?:ence)?|docs?|guide|manual)\s*[:：]\s*\S+"),
)


def analyze_static_tier(
    candidate: Candidate,
    text: str | None = None,
    *,
    is_fetched: bool = True,
    is_truncated: bool = False,
    is_valid_doc: bool = True,
    doc_reason: str = "",
) -> dict[str, Any]:
    """静态规则分级观察模式（纯函数）。

    在不改变任何调用或排队行为的前提下，输出档位、版本、命中信号、建议动作与原因。
    严格保证：
    - 缺 Frontmatter、有效短文本或包含未来路线图 TODO 绝不作为淘汰或跳过依据；
    - 仅明确无任何实质实现的纯占位（如只有 TODO: add description / write implementation）标记为建议跳过；
    - 敏感合规词标记为建议重点复核；
    - 未抓取内容保持待判断（suggest_pending）。
    """
    if text is None or not is_fetched:
        return {
            "heuristic_version": STATIC_HEURISTIC_VERSION,
            "mode": "observation",
            "applied": False,
            "tier": TIER_PENDING,
            "signals": ["UNFETCHED_MATERIAL"],
            "suggested_action": ACTION_SUGGEST_PENDING,
            "reason": "材料尚未抓取，保持待判断",
        }

    clean_text = text.strip()
    if not clean_text:
        return {
            "heuristic_version": STATIC_HEURISTIC_VERSION,
            "mode": "observation",
            "applied": False,
            "tier": TIER_CONTENT_DEFECT,
            "signals": ["EMPTY_CONTENT"],
            "suggested_action": ACTION_SUGGEST_SKIP,
            "reason": "材料为空文件或纯空白",
        }

    if is_truncated:
        return {
            "heuristic_version": STATIC_HEURISTIC_VERSION,
            "mode": "observation",
            "applied": False,
            "tier": TIER_CONTENT_DEFECT,
            "signals": ["FETCH_TRUNCATED"],
            "suggested_action": ACTION_SUGGEST_SKIP,
            "reason": "材料抓取严重截断",
        }

    if not is_valid_doc:
        sig = "HTML_ERROR_PAGE" if "html" in doc_reason.lower() else "INVALID_DOCUMENT_FORMAT"
        return {
            "heuristic_version": STATIC_HEURISTIC_VERSION,
            "mode": "observation",
            "applied": False,
            "tier": TIER_CONTENT_DEFECT,
            "signals": [sig],
            "suggested_action": ACTION_SUGGEST_SKIP,
            "reason": f"材料不是有效文档：{doc_reason}",
        }

    # 检查是否为明确的空壳占位（Explicit Boilerplate / Placeholder）
    # 1. 分离 YAML frontmatter
    body = clean_text
    has_frontmatter = False
    if clean_text.startswith("---"):
        parts = clean_text.split("---", 2)
        if len(parts) >= 3:
            has_frontmatter = True
            body = parts[2].strip()

    # 检查是否有可执行命令或代码块
    has_commands = any(p.search(clean_text) for p in COMMAND_INSTRUCTION_PATTERNS)
    # 检查是否包含尚未读取的外部/相对参考入口（例如 # [Instructions](references/guide.md)）
    has_reference_entry = any(p.search(clean_text) for p in REFERENCE_ENTRY_PATTERNS)

    # 检查 body 中的非空非标题行
    body_lines = [
        line.strip()
        for line in body.splitlines()
        if line.strip() and not line.strip().startswith("#") and not line.strip().startswith("---")
    ]

    # 若没有任何实质正文行但包含参考入口，必须保留为待判断，严禁判定为空壳
    if not body_lines and has_reference_entry:
        return {
            "heuristic_version": STATIC_HEURISTIC_VERSION,
            "mode": "observation",
            "applied": False,
            "tier": TIER_UNASSESSED,
            "signals": ["HAS_REFERENCE_ENTRY"],
            "suggested_action": ACTION_SUGGEST_EVALUATE,
            "reason": "材料仅包含外部/相对参考入口且正文尚未展开，保留为待判断",
        }

    is_pure_placeholder = False
    if not has_commands and not has_reference_entry:
        if not body_lines:
            # 只有标题没有正文且无任何参考入口
            is_pure_placeholder = True
        else:
            # 所有非标题行均匹配占位模式
            all_match_placeholder = all(
                any(p.match(line) for p in PLACEHOLDER_LINE_PATTERNS) for line in body_lines
            )
            if all_match_placeholder:
                is_pure_placeholder = True

    if is_pure_placeholder:
        return {
            "heuristic_version": STATIC_HEURISTIC_VERSION,
            "mode": "observation",
            "applied": False,
            "tier": TIER_CLEAR_PLACEHOLDER,
            "signals": ["EXPLICIT_BOILERPLATE", "TODO_PLACEHOLDER_ONLY"],
            "suggested_action": ACTION_SUGGEST_SKIP,
            "reason": "正文仅包含模板占位提示与 TODO，无实际功能实现或执行指引",
        }

    # 检查敏感高危合规词 (Suspect Patterns)
    haystack = f"{candidate.name} {candidate.description} {candidate.path} {clean_text}"
    matched_suspects = [
        flag for flag, p in SUSPECT_PATTERNS.items() if p.search(haystack)
    ]
    if matched_suspects:
        return {
            "heuristic_version": STATIC_HEURISTIC_VERSION,
            "mode": "observation",
            "applied": False,
            "tier": TIER_SUSPECT,
            "signals": matched_suspects,
            "suggested_action": ACTION_SUGGEST_REVIEW,
            "reason": f"命中了需重点复核的领域敏感信号: {', '.join(matched_suspects)}",
        }

    # 正常候选（Normal Candidate）
    signals = ["VALID_STRUCTURE"]
    if not has_frontmatter:
        signals.append("NO_FRONTMATTER_VALID")
    if len(clean_text) < 200 or len(body_lines) <= 3:
        signals.append("SHORT_VALID_DOCUMENT")
    if re.search(r"(?i)\btodo\b", clean_text) and any(p.search(clean_text) for p in ROADMAP_TODO_PATTERNS):
        signals.append("BODY_CONTAINS_ROADMAP_TODO")

    return {
        "heuristic_version": STATIC_HEURISTIC_VERSION,
        "mode": "observation",
        "applied": False,
        "tier": TIER_NORMAL,
        "signals": signals,
        "suggested_action": ACTION_SUGGEST_EVALUATE,
        "reason": "材料结构有效，具备功能说明或可执行指引",
    }


def should_static_skip(
    observation: dict[str, Any] | None,
    enabled: bool = False,
) -> bool:
    """判断是否应当静态跳过候选（明确空壳占位且开启跳过）。

    严格保证：
    - 仅在 enabled 为 True 且 tier == TIER_CLEAR_PLACEHOLDER 且 suggested_action == ACTION_SUGGEST_SKIP 时返回 True；
    - 有效短文本、缺 Frontmatter、未来路线图 TODO、敏感信号或未抓取内容绝不跳过；
    - 纯观察模式下（enabled=False）一律返回 False。
    """
    if not enabled or not observation:
        return False
    tier = observation.get("tier")
    action = observation.get("suggested_action")
    return tier == TIER_CLEAR_PLACEHOLDER and action == ACTION_SUGGEST_SKIP




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
            manual_exclusions = get_manual_exclusions(overrides)
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
    result.static_observation = analyze_static_tier(candidate, text)

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

    # 7. 静态规则分级观察模式（纯观察，不改变 decision）
    obs = analyze_static_tier(candidate, text)
    result.static_observation = obs

    return result


def prescreen_all(
    candidates: list[Candidate], cfg: PrescreenConfig, texts: dict[str, str] | None = None
) -> list[PrescreenResult]:
    """批量预筛。texts 以 skill_id 为键，缺省表示未抓取内容。"""
    lookup = texts or {}
    return [prescreen(c, cfg, lookup.get(c.skill_id)) for c in candidates]
