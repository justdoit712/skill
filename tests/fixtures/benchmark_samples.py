"""P1 固定离线样本集（Benchmark Fixtures）。

涵盖四大维度完整正反用例：
1. 查询样本 (QUERY_BENCHMARK_SAMPLES)：中文、英文、技术别名、歧义词、明确技术约束与术语表未命中需求；
2. 证据样本 (EVIDENCE_BENCHMARK_SAMPLES)：正常跨行、合法行号漂移、重复段落、否定句反例、数字变体、代码块、表格与跨文件拼接；
3. 目录文档样本 (DOCUMENT_BENCHMARK_SAMPLES)：短而有效、无 Frontmatter、明确空壳占位、正文含 TODO 但有效、模板型技能与依赖参考材料；
4. 持久化记录样本 (PERSISTENCE_BENCHMARK_SAMPLES)：正常记录、缺字段旧记录、身份冲突、材料变化与中断状态。
"""

from __future__ import annotations

# -------------------------------------------------------------
# 1. 查询样本集 (Query Benchmark Samples)
# -------------------------------------------------------------

QUERY_BENCHMARK_SAMPLES = {
    "chinese_concept": {
        "id": "q_cn_concept",
        "topic": "网页爬取与数据提取",
        "expected_core_concept": "网页爬取",
        "expected_tech_aliases": ["web scraping", "web crawler", "html parsing"],
        "has_negative_constraint": False,
    },
    "english_direct": {
        "id": "q_en_direct",
        "topic": "automated browser testing using playwright",
        "expected_core_concept": "browser testing",
        "expected_tech_aliases": ["playwright", "e2e testing"],
        "has_negative_constraint": False,
    },
    "technical_alias": {
        "id": "q_tech_alias",
        "topic": "提示词优化与评估",
        "expected_core_concept": "提示词优化",
        "expected_tech_aliases": ["prompt optimization", "prompt engineering", "prompt refinement"],
        "has_negative_constraint": False,
    },
    "ambiguous_term": {
        "id": "q_ambiguous",
        "topic": "高效爬虫开发",
        "expected_core_concept": "网络爬虫",
        "expected_tech_aliases": ["web crawler", "spider"],
        "has_negative_constraint": False,
    },
    "explicit_constraint": {
        "id": "q_constraint",
        "topic": "纯 Python requests 网页抓取，不需要 Selenium 或无头浏览器",
        "expected_core_concept": "网页抓取",
        "expected_tech_aliases": ["requests", "python crawler"],
        "has_negative_constraint": True,
        "negative_tokens": ["selenium", "headless browser"],
    },
    "no_terminology_hit": {
        "id": "q_no_hit",
        "topic": "基于量子退火私有架构的超大规模阵列拓扑求解",
        "expected_core_concept": "量子退火拓扑求解",
        "expected_tech_aliases": [],
        "has_negative_constraint": False,
    },
}

# -------------------------------------------------------------
# 2. 证据样本集 (Evidence Benchmark Samples)
# -------------------------------------------------------------

EVIDENCE_BENCHMARK_DOCUMENT = """---
name: web-scraper-pro
description: Professional web scraping toolkit.
---
# Web Scraper Pro

This tool provides high-performance asynchronous HTTP scraping.
Built on top of httpx and beautifulsoup4 for parsing HTML documents.
Supports automatic retry with exponential backoff on 429 and 503.
Features include proxy rotation and session persistence.
Export data directly into JSON or CSV formats.
Notice: This tool does NOT support authenticated login behind CAPTCHA.
Requirements: Python 3.11 or higher is required.
Note: duplicate disclaimer for license compliance.

```python
def scrape_url(url: str, timeout: float = 10.0):
    response = httpx.get(url, timeout=timeout)
    return response.text
```
| Feature | Supported | Notes |
| --- | --- | --- |
| Async Scraping | Yes | Up to 100 concurrent workers |
| Proxy Rotation | Yes | SOCKS5 & HTTP supported |
| CAPTCHA bypass | No | Strictly out of scope |
Note: duplicate disclaimer for license compliance.
End of documentation.
"""

EVIDENCE_BENCHMARK_SAMPLES = {
    "multiline_normal": {
        "id": "ev_multiline_normal",
        "source_path": "SKILL.md",
        "start_line": 7,
        "end_line": 8,
        "quote": "This tool provides high-performance asynchronous HTTP scraping. Built on top of httpx and beautifulsoup4 for parsing HTML documents.",
        "expected_exact_valid": True,
        "is_counterexample": False,
    },
    "line_drift_positive": {
        "id": "ev_line_drift_positive",
        "source_path": "SKILL.md",
        # 实际位于第 8-9 行，模型由于行号统计偏差提供了第 6-8 行 (偏移 <= 3 行)
        "start_line": 6,
        "end_line": 8,
        "quote": "Built on top of httpx and beautifulsoup4 for parsing HTML documents. Supports automatic retry with exponential backoff on 429 and 503.",
        "actual_start_line": 8,
        "actual_end_line": 9,
        "expected_exact_valid": False,  # 精确核验因区间偏移失败
        "expected_drift_recoverable": True,  # 邻近修复应可唯一定位
        "is_counterexample": False,
    },
    "duplicate_paragraph": {
        "id": "ev_duplicate_paragraph",
        "source_path": "SKILL.md",
        "start_line": 20,
        "end_line": 27,
        # 第 14 行与第 26 行完全相同
        "quote": "Note: duplicate disclaimer for license compliance.",
        "occurrences_in_doc": 2,
        "is_counterexample": False,
    },
    "negation_contrast_fabricated": {
        "id": "ev_negation_fabricated",
        "source_path": "SKILL.md",
        "start_line": 12,
        "end_line": 12,
        # 原文包含 NOT，引文被模型去掉了 NOT，构成捏造放行
        "quote": "Notice: This tool does support authenticated login behind CAPTCHA.",
        "expected_exact_valid": False,
        "expected_drift_recoverable": False,
        "is_counterexample": True,
    },
    "number_variant_fabricated": {
        "id": "ev_number_variant",
        "source_path": "SKILL.md",
        "start_line": 13,
        "end_line": 13,
        # 原文为 Python 3.11，引文被篡改为 Python 2.7
        "quote": "Requirements: Python 2.7 or higher is required.",
        "expected_exact_valid": False,
        "expected_drift_recoverable": False,
        "is_counterexample": True,
    },
    "code_block_snippet": {
        "id": "ev_code_block",
        "source_path": "SKILL.md",
        "start_line": 17,
        "end_line": 19,
        "quote": "def scrape_url(url: str, timeout: float = 10.0):\n    response = httpx.get(url, timeout=timeout)\n    return response.text",
        "expected_exact_valid": True,
        "is_counterexample": False,
    },
    "table_snippet": {
        "id": "ev_table_snippet",
        "source_path": "SKILL.md",
        "start_line": 24,
        "end_line": 24,
        "quote": "| Proxy Rotation | Yes | SOCKS5 & HTTP supported |",
        "expected_exact_valid": True,
        "is_counterexample": False,
    },
    "cross_file_spliced_fabricated": {
        "id": "ev_cross_file_spliced",
        "source_path": "SKILL.md",
        "start_line": 7,
        "end_line": 11,
        # 拼接跨段落并省略中间文字
        "quote": "This tool provides high-performance asynchronous HTTP scraping. Export data directly into JSON.",
        "expected_exact_valid": False,
        "expected_drift_recoverable": False,
        "is_counterexample": True,
    },
}

# -------------------------------------------------------------
# 3. 目录与材料样本集 (Document & Catalog Samples)
# -------------------------------------------------------------

DOCUMENT_BENCHMARK_SAMPLES = {
    "short_and_valid": {
        "id": "doc_short_valid",
        "path": "skills/mini-calc/SKILL.md",
        "content": "---\nname: mini-calc\ndescription: A tiny calculator.\n---\n# Calculator\nRun `python calc.py 1 + 1` to compute sum.\n",
        "expected_prescreen_excluded": False,
        "is_boilerplate": False,
    },
    "no_frontmatter": {
        "id": "doc_no_frontmatter",
        "path": "skills/clean-tool/SKILL.md",
        "content": "# Clean Tool\nA tool for cleaning cache.\nRun `python clean.py --all` to execute.\n",
        "expected_prescreen_excluded": False,
        "is_boilerplate": False,
    },
    "explicit_boilerplate": {
        "id": "doc_boilerplate",
        "path": "skills/empty-skill/SKILL.md",
        "content": "---\nname: todo-skill\ndescription: TODO: add description\n---\n# TODO Skill\nTODO: write your skill implementation here.\n",
        "expected_prescreen_excluded": False,  # 预筛不直接根据 TODO 淘汰有效短文
        "is_boilerplate": True,
    },
    "body_with_todo": {
        "id": "doc_body_todo",
        "path": "skills/sync-tool/SKILL.md",
        "content": "---\nname: sync-tool\ndescription: Realtime sync.\n---\n# Sync Tool\nFully working synchronization engine.\nRun `python sync.py`.\n# TODO: add S3 backend in v2.0\n",
        "expected_prescreen_excluded": False,
        "is_boilerplate": False,
    },
    "template_skill": {
        "id": "doc_template",
        "path": "skills/template-runner/SKILL.md",
        "content": "---\nname: template-runner\ndescription: Template tool\n---\n# Runner\nReplace {{INPUT_FILE}} with your path and run `python run.py {{INPUT_FILE}}`.\n",
        "expected_prescreen_excluded": False,
        "is_boilerplate": False,
    },
    "reference_dependent": {
        "id": "doc_reference_dependent",
        "path": "skills/schema-checker/SKILL.md",
        "content": "---\nname: schema-checker\ndescription: Checks json schema\n---\n# Schema Checker\nPlease refer to `./references/schema.json` for validation rules.\n",
        "expected_prescreen_excluded": False,
        "is_boilerplate": False,
    },
}

# -------------------------------------------------------------
# 4. 持久化记录样本集 (Persistence Benchmark Samples)
# -------------------------------------------------------------

PERSISTENCE_BENCHMARK_SAMPLES = {
    "normal_completed_record": {
        "evaluation_id": "eval_normal_001",
        "skill_id": "example/repo:skills/demo/SKILL.md",
        "content_fingerprint": "fp_normal_123",
        "rules_version": "1.0.0",
        "model_config_version": "1.0.0",
        "status": "completed",
        "attempts": 1,
        "max_attempts": 2,
        "outcome": {
            "decision": "recommended",
            "evaluation": {"name": "demo", "summary_zh": "演示技能"},
        },
        "requests": [
            {
                "stage": "evaluation",
                "attempt": 1,
                "status": "completed",
                "usage": {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150},
            }
        ],
    },
    "legacy_missing_fields_record": {
        "evaluation_id": "eval_legacy_002",
        "skill_id": "legacy/repo:skills/old/SKILL.md",
        # 缺失 content_fingerprint、model_config_version、requests 详情
        "status": "failed",
        "error": {"reason_code": "PARSE_ERROR"},
    },
    "identity_conflict_record": {
        "evaluation_id": "eval_conflict_003",
        "skill_id": "different/repo:skills/demo/SKILL.md",  # 与目标候选不一致
        "content_fingerprint": "fp_normal_123",
        "status": "failed",
        "attempts": 2,
        "max_attempts": 2,
    },
    "material_conflict_record": {
        "evaluation_id": "eval_material_conflict_004",
        "skill_id": "example/repo:skills/demo/SKILL.md",
        "content_fingerprint": "fp_old_modified_999",  # 指纹不一致
        "status": "failed",
        "attempts": 2,
        "max_attempts": 2,
    },
    "interrupted_in_progress_record": {
        "evaluation_id": "eval_interrupted_005",
        "skill_id": "example/repo:skills/demo/SKILL.md",
        "content_fingerprint": "fp_normal_123",
        "status": "in_progress",
        "attempts": 1,
        "max_attempts": 2,
    },
}


__all__ = [
    "QUERY_BENCHMARK_SAMPLES",
    "EVIDENCE_BENCHMARK_DOCUMENT",
    "EVIDENCE_BENCHMARK_SAMPLES",
    "DOCUMENT_BENCHMARK_SAMPLES",
    "PERSISTENCE_BENCHMARK_SAMPLES",
]
