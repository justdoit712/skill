"""Finder 领域相关性评分与候选调度模块。

职责：
1. 多源关键词与领域短语提取（结合 topic、intent、required 准则及双语 queries）；
2. 停用词与通用高频词降权，领域核心词与短语加权；
3. 技能候选多维度打分（路径/名称主导 + 仓库描述辅助 + 单仓库饱和度平滑）；
4. 待展开仓库相关性排序与探索均衡；
5. 80% 相关性优先 + 20% 探索轮转调度器（确保排序确定、续跑不洗牌）。
"""

from __future__ import annotations

import re
from typing import Any

RELEVANCE_RULES_VERSION = "1.0.0"

GENERIC_STOP_WORDS = frozenset(
    {
        "ai", "prompt", "prompts", "skill", "skills", "agent", "agents", "llm", "llms",
        "tool", "tools", "model", "models", "github", "repo", "repository", "code",
        "plugin", "plugins", "app", "apps", "assistant", "system", "framework",
        "library", "cli", "the", "and", "for", "with", "from", "that", "this", "which",
        "into", "best", "good", "new", "free", "open", "source", "pack", "package",
        "kit", "suite", "高质量", "使用", "支持", "具备", "提供", "可以", "各种", "相关",
        "生成", "优化", "实现", "以及", "通过", "进行", "一个", "用于", "包含", "要求",
        "能够", "用户", "核心", "能力", "方案", "提示词", "技能", "助手", "助手类", "工具",
        "模板", "样例", "指南", "规范",
    }
)


def _tokenize_text(text: str) -> list[str]:
    """将文本切分为单词（英文单词与中文 2~4 字切片）。"""
    if not text:
        return []
    cleaned = text.lower()
    tokens = []
    # 提取连续英文单词 (长度 >= 2)
    en_words = re.findall(r"[a-z0-9]{2,}", cleaned)
    tokens.extend(en_words)
    # 提取中文词组 (2 到 4 字)
    zh_chunks = re.findall(r"[\u4e00-\u9fa5]+", cleaned)
    for chunk in zh_chunks:
        c_len = len(chunk)
        if c_len <= 4:
            tokens.append(chunk)
        else:
            for sz in (2, 3, 4):
                for i in range(c_len - sz + 1):
                    tokens.append(chunk[i : i + sz])
    return tokens


def extract_relevance_terms(topic: str, plan: dict[str, Any] | None = None) -> dict[str, float]:
    """从原始需求、意图、评估准则和生成的查询短语中提取加权关键词与短语。"""
    weights: dict[str, float] = {}
    plan_dict = plan or {}

    # 1. 提取来自规划 queries 的短语与单词（高信息量双语词）
    for q in plan_dict.get("queries", []):
        if not isinstance(q, str):
            continue
        q_norm = q.lower().strip()
        # 拆分查询中的单词
        words = [w for w in re.split(r"[\s_/-]+", q_norm) if len(w) >= 2]
        # 提取 2-gram 复合短语 (排除纯通用词组合)
        for i in range(len(words) - 1):
            bg = f"{words[i]} {words[i+1]}"
            if not any(sw == words[i] or sw == words[i+1] for sw in ("ai", "prompt", "skill")):
                weights[bg] = max(weights.get(bg, 0.0), 3.0)
        # 单个查询词
        for w in words:
            if w in GENERIC_STOP_WORDS:
                weights[w] = min(weights.get(w, 0.05), 0.05)
            else:
                weights[w] = max(weights.get(w, 0.0), 2.2)

    # 2. 提取必需准则 (required criteria) 中的领域描述词
    for c in plan_dict.get("criteria", []):
        if isinstance(c, dict) and c.get("kind") == "required":
            desc = c.get("description", "")
            for tok in _tokenize_text(desc):
                if tok in GENERIC_STOP_WORDS:
                    continue
                weights[tok] = max(weights.get(tok, 0.0), 2.5)

    # 3. 提取用户意图 (intent) 与原始主题 (topic)
    combined_topic = f"{topic} {plan_dict.get('intent', '')}".strip()
    for tok in _tokenize_text(combined_topic):
        if tok in GENERIC_STOP_WORDS:
            continue
        weights[tok] = max(weights.get(tok, 0.0), 1.8)

    return weights


def score_candidate_relevance(
    candidate: Any,
    term_weights: dict[str, float],
    repo_counts: dict[str, int] | None = None,
) -> dict[str, Any]:
    """对单个候选技能计算相关性打分。
    
    打分维度：
    - 技能名称与目录路径匹配 (权重 0.70)：直接领域匹配强信号；
    - 上游仓库描述与仓库名语义匹配 (权重 0.30)：辅助信号，设定上限防单仓库穿透；
    - 单仓库候选累积数量惩罚：抑制大型合集仓库垄断。
    """
    path = getattr(candidate, "path", None) or (candidate.get("path", "") if isinstance(candidate, dict) else "")
    name = getattr(candidate, "name", None) or (candidate.get("name", "") if isinstance(candidate, dict) else "")
    repo = getattr(candidate, "repo", None) or (candidate.get("repo", "") if isinstance(candidate, dict) else "")
    desc = getattr(candidate, "description", None) or (candidate.get("description", "") if isinstance(candidate, dict) else "")
    owner = getattr(candidate, "owner", None) or (candidate.get("owner", "") if isinstance(candidate, dict) else "")
    repo_key = f"{owner}/{repo}"

    norm_path_name = f"{path} {name}".lower().replace("-", " ").replace("_", " ").replace("/", " ")
    norm_repo_desc = f"{repo} {desc}".lower().replace("-", " ").replace("_", " ")

    path_score = 0.0
    repo_score = 0.0
    matched_terms: list[str] = []

    for term, w in term_weights.items():
        if w <= 0.1:
            continue
        # 路径与名称匹配
        if term in norm_path_name:
            # 完整短语匹配给更高加成
            bonus = 2.5 if " " in term else 1.5
            path_score += w * bonus
            if term not in matched_terms:
                matched_terms.append(term)
        # 仓库名与简介匹配
        elif term in norm_repo_desc:
            repo_score += w * 0.4
            tag = f"repo:{term}"
            if tag not in matched_terms:
                matched_terms.append(tag)

    # 仓库描述辅助分封顶在 4.0，避免泛化描述让几百个无关子技能全部拿到高分
    repo_score = min(repo_score, 4.0)

    # 综合基础分：路径/名称占 70%，仓库描述占 30%
    base_score = path_score * 0.7 + repo_score * 0.3

    # 单仓库饱和度平滑：同仓库已有候选超过 4 个后，后续候选逐渐衰减
    count = (repo_counts or {}).get(repo_key, 0)
    saturation_factor = 1.0 / (1.0 + 0.12 * max(0, count - 4))
    final_score = base_score * saturation_factor

    return {
        "score": round(final_score, 4),
        "matched_terms": matched_terms,
        "score_breakdown": {
            "path_score": round(path_score, 4),
            "repo_score": round(repo_score, 4),
            "saturation_factor": round(saturation_factor, 4),
        },
        "rule_version": RELEVANCE_RULES_VERSION,
    }


def score_repository_relevance(
    repo: dict[str, Any],
    term_weights: dict[str, float],
) -> dict[str, Any]:
    """对待展开仓库计算相关性打分。"""
    owner = str(repo.get("owner") or "").lower()
    repo_name = str(repo.get("repo") or "").lower()
    desc = str(repo.get("description") or "").lower()

    text = f"{owner} {repo_name} {desc}".replace("-", " ").replace("_", " ")

    score = 0.0
    matched_terms: list[str] = []

    for term, w in term_weights.items():
        if w <= 0.1:
            continue
        if term in text:
            bonus = 2.0 if " " in term else 1.0
            score += w * bonus
            matched_terms.append(term)

    return {
        "score": round(score, 4),
        "matched_terms": matched_terms,
        "rule_version": RELEVANCE_RULES_VERSION,
    }


def rank_pending_repositories(
    pending_repos: list[dict[str, Any]],
    term_weights: dict[str, float],
    *,
    max_repos: int = 5,
) -> list[dict[str, Any]]:
    """对待展开仓库按相关性得分排序，并在高分与轮转间均衡。"""
    if not pending_repos:
        return []

    scored_repos = []
    for idx, r in enumerate(pending_repos):
        res = score_repository_relevance(r, term_weights)
        scored_repos.append((res["score"], idx, r, res))

    # 按分数降序，相同分数保持原序稳定
    scored_repos.sort(key=lambda x: (-x[0], x[1]))

    # 取前 max_repos 个
    return [item[2] for item in scored_repos[:max_repos]]


def schedule_candidates_by_relevance_and_fairness(
    candidates: list[Any],
    term_weights: dict[str, float],
    *,
    relevance_ratio: float = 0.8,
    max_total: int | None = None,
    max_per_repo: int | None = None,
) -> list[Any]:
    """混合相关性优先与跨仓库轮转探索的确定性调度器。
    
    规则：
    1. 计算每个候选的相关性得分；
    2. 按分数高低构建高相关优先队列；
    3. 按仓库维护轮转探索队列；
    4. 按 80% 相关性优先 + 20% 探索轮转交叉输出；
    5. 严格保证输入相同时输出顺序 100% 确定可复现。
    """
    if not candidates:
        return []

    # 1. 计算每个候选的得分
    repo_counts: dict[str, int] = {}
    scored_cands = []
    for idx, c in enumerate(candidates):
        r_key = f"{getattr(c, 'owner', '')}/{getattr(c, 'repo', '')}" if hasattr(c, "owner") else f"{c.get('owner', '')}/{c.get('repo', '')}"
        repo_counts[r_key] = repo_counts.get(r_key, 0) + 1
        res = score_candidate_relevance(c, term_weights, repo_counts)
        scored_cands.append((res["score"], idx, c))

    # 2. 构建按相关性得分降序的候选列表（稳定保持原有相对次序）
    relevance_pool = sorted(scored_cands, key=lambda x: (-x[0], x[1]))
    relevance_list = [item[2] for item in relevance_pool]

    # 3. 构建按仓库轮转的公平列表
    by_repo: dict[str, list[Any]] = {}
    for c in relevance_list:
        r_key = f"{getattr(c, 'owner', '')}/{getattr(c, 'repo', '')}" if hasattr(c, "owner") else f"{c.get('owner', '')}/{c.get('repo', '')}"
        by_repo.setdefault(r_key, []).append(c)

    fair_round_robin: list[Any] = []
    repo_keys = sorted(by_repo.keys())  # 保证确定性
    max_depth = max((len(lst) for lst in by_repo.values()), default=0)
    for depth in range(max_depth):
        for r_key in repo_keys:
            c_list = by_repo[r_key]
            if depth < len(c_list):
                fair_round_robin.append(c_list[depth])

    # 4. 80% / 20% 混合调度
    scheduled: list[Any] = []
    seen_ids: set[str] = set()
    repo_scheduled_counts: dict[str, int] = {}

    rel_idx = 0
    fair_idx = 0
    total_len = len(candidates)

    def _get_sid(item: Any) -> str:
        return getattr(item, "skill_id", "") if hasattr(item, "skill_id") else item.get("skill_id", "")

    def _get_repo(item: Any) -> str:
        return f"{getattr(item, 'owner', '')}/{getattr(item, 'repo', '')}" if hasattr(item, "owner") else f"{item.get('owner', '')}/{item.get('repo', '')}"

    def _can_schedule_repo(item: Any) -> bool:
        if max_per_repo is None:
            return True
        r = _get_repo(item)
        return repo_scheduled_counts.get(r, 0) < max_per_repo

    while len(scheduled) < total_len and (rel_idx < len(relevance_list) or fair_idx < len(fair_round_robin)):
        # 5 个槽位：前 4 个（80%）尝试从相关性高分队列取
        for _ in range(4):
            while rel_idx < len(relevance_list):
                cand = relevance_list[rel_idx]
                rel_idx += 1
                sid = _get_sid(cand)
                if sid not in seen_ids and _can_schedule_repo(cand):
                    seen_ids.add(sid)
                    r = _get_repo(cand)
                    repo_scheduled_counts[r] = repo_scheduled_counts.get(r, 0) + 1
                    scheduled.append(cand)
                    break

        # 第 5 个（20%）尝试从轮转探索队列取
        while fair_idx < len(fair_round_robin):
            cand = fair_round_robin[fair_idx]
            fair_idx += 1
            sid = _get_sid(cand)
            if sid not in seen_ids and _can_schedule_repo(cand):
                seen_ids.add(sid)
                r = _get_repo(cand)
                repo_scheduled_counts[r] = repo_scheduled_counts.get(r, 0) + 1
                scheduled.append(cand)
                break

        # 如果受 max_per_repo 限制两边都无法放入新候选，但队列未满，放宽限制排完剩余
        if rel_idx >= len(relevance_list) and fair_idx >= len(fair_round_robin):
            for cand in relevance_list:
                sid = _get_sid(cand)
                if sid not in seen_ids:
                    seen_ids.add(sid)
                    scheduled.append(cand)

    if max_total is not None and len(scheduled) > max_total:
        return scheduled[:max_total]
    return scheduled


__all__ = [
    "RELEVANCE_RULES_VERSION",
    "GENERIC_STOP_WORDS",
    "extract_relevance_terms",
    "score_candidate_relevance",
    "score_repository_relevance",
    "rank_pending_repositories",
    "schedule_candidates_by_relevance_and_fairness",
]
