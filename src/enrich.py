"""离线保守结构化增强模块。

在不调用外部大模型、不修改原始 summary_zh 的前提下，
从现有数据中基于明确的事实性语句提取形态分类、示例请求与核心亮点。
严格遵守：
1. 形态分类 (skill_type) 必须以明确描述为第一依据；若无描述或证据不足必须返回 None，绝不凭名称或孤立标签猜测。
2. 示例请求 (example_requests) 与核心亮点 (key_features) 仅提取现有材料中的事实片段，严禁编造未经证明的能力。
3. 原始 summary_zh 保持只读与原貌，绝不做不可逆覆盖。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from .index import CatalogContext, build_catalog, write_catalog
from .schema_utils import normalize_skill_type, normalize_string_list


def extract_example_requests(summary_zh: str | None) -> list[str]:
    """从中文简述中保守提取 0~2 条用户示例请求。

    基于现存事实句式（如“典型场景是用户请求/询问...”、“适用于...等场景”、“典型用于...”），
    绝不扩写未经证明的能力。
    """
    if not summary_zh or not isinstance(summary_zh, str):
        return []
    text = summary_zh.strip()
    if not text:
        return []

    requests: list[str] = []

    # 句式 1：用户请求[“"”](.*?)[”"“]
    m_quoted = re.search(r'用户(?:请求|询问)[“"”]([^“”"]+)[”"“]', text)
    if m_quoted:
        req = m_quoted.group(1).strip()
        if req:
            requests.append(f"“{req}”")

    # 句式 2：适用于用户请求(.+?)(?:等?场景|。|$)
    if not requests:
        m_req = re.search(r'适用于用户请求(.+?)(?:等?场景|。|$)', text)
        if m_req:
            req = m_req.group(1).strip()
            if req:
                req = req.strip("“”\"'")
                requests.append(f"“{req}”")

    # 句式 3：典型场景是用户(?:询问|查找|寻求)(.+?)(?:时|。|$)
    if not requests:
        m_ask = re.search(r'典型场景是用户(?:询问|查找|寻求)(.+?)(?:时|。|$)', text)
        if m_ask:
            target = m_ask.group(1).strip().strip("“”\"'")
            if target:
                requests.append(f"“询问{target}”")

    # 句式 4：适用于/典型用于(.+?)(?:等?场景|。|$)
    if not requests:
        m_apply = re.search(r'(?:适用于|典型用于)(.+?)(?:等?场景|。|$)', text)
        if m_apply:
            target = m_apply.group(1).strip().strip("“”\"'")
            if target.startswith("要求"):
                target = target[2:].strip()
            if target.endswith("的"):
                target = target[:-1].strip()
            if target and len(target) >= 4:
                requests.append(f"“{target}”")

    return normalize_string_list(requests, max_items=2, max_length=100)


def extract_key_features(summary_zh: str | None, tags: list[str] | None = None) -> list[str]:
    """从中文简述与标签中保守提取 0~3 条事实性特征短语。"""
    if not summary_zh or not isinstance(summary_zh, str):
        return []
    text = summary_zh.strip()
    if not text:
        return []

    features: list[str] = []

    # 提取明确的能力陈述短语
    # 1. 支持...
    m_sup = re.search(r'(支持[^，。；]+)', text)
    if m_sup:
        features.append(m_sup.group(1).strip())

    # 2. 覆盖/涵盖...
    m_cov = re.search(r'((?:覆盖|涵盖)[^，。；]+)', text)
    if m_cov:
        features.append(m_cov.group(1).strip())

    # 3. 提供...
    m_prov = re.search(r'(提供[^，。；]+(?:指南|示例|规范|约束|说明|参考))', text)
    if m_prov:
        features.append(m_prov.group(1).strip())

    # 4. 首句核心动词短语（如“创建、编辑、读取和转换 Word 文档”）
    m_act = re.search(r'^([^，。；]+(?:文档|代码|艺术|文件|应用|服务|系统|规范|指南|工具|技能|GIF))', text)
    if m_act:
        act = m_act.group(1).strip()
        if not act.startswith("该技能") and not act.startswith("通过") and len(act) >= 6:
            features.append(act)
        elif act.startswith("该技能依据") or act.startswith("该技能用于") or act.startswith("该技能通过"):
            trimmed = re.sub(r"^该技能(?:依据|用于|通过)", "", act).strip()
            if len(trimmed) >= 4:
                prefix = act[3:5]  # 依据 / 用于 / 通过
                features.append(prefix + trimmed)

    return normalize_string_list(features, max_items=3, max_length=60)


def infer_skill_type(
    *,
    name: str,
    summary_zh: str | None = None,
    tags: list[str] | None = None,
    dependencies: list[str] | None = None,
) -> str | None:
    """保守推断形态分类。必须以明确的描述文本为第一依据，严禁凭孤立名称或标签猜测。

    枚举：'tool_script' | 'guideline' | 'template' | 'reference' | None
    """
    if not summary_zh or not isinstance(summary_zh, str) or not summary_zh.strip():
        # 无明确描述证据，严禁仅凭名称（如 template）猜测，一律返回 None
        return None

    sum_text = summary_zh.strip()

    # 1. 规范指南 (guideline)：描述中明确说明是规范、指南、准则
    if any(k in sum_text for k in ("写作指南", "写作规范", "视觉标准", "设计指导", "开发指南", "操作指引", "实践准则")):
        return "guideline"

    # 2. 查阅参考手册 (reference)：描述中明确说明是参考指南、API字典、速查手册
    if any(k in sum_text for k in ("参考指南", "API 字典", "API字典", "速查手册", "速查表", "参考文档")):
        return "reference"

    # 3. 文档与脚手架模板 (template)：描述中明确说明提供模板、脚手架
    if any(k in sum_text for k in ("项目模板", "配置模板", "文档模板", "代码模板", "脚手架", "代码脚手架", "项目骨架")):
        return "template"

    # 4. 命令行/工具脚本 (tool_script)：描述中明确说明是命令行工具或自动化执行脚本
    if any(k in sum_text for k in ("命令行工具", "自动化执行脚本", "自动化脚本")):
        return "tool_script"

    # 证据不足时一律返回 None（不猜测，不默认查阅手册）
    return None


def enrich_entry(entry: dict) -> dict:
    """对单条索引记录做保守结构化增强（保持 summary_zh 原貌不变）。"""
    res = dict(entry)
    summary_zh = res.get("summary_zh")
    name = res.get("name") or ""
    tags = res.get("tags") or []
    deps = res.get("dependencies_declared") or []

    # 1. 形态分类：已有合法值则保留；若无则保守推断，推断不出留 None
    current_type = normalize_skill_type(res.get("skill_type"))
    if not current_type:
        res["skill_type"] = infer_skill_type(
            name=name, summary_zh=summary_zh, tags=tags, dependencies=deps
        )
    else:
        res["skill_type"] = current_type

    # 2. 示例请求：已有则清洗保留；若无则从 summary_zh 提取
    current_reqs = normalize_string_list(
        res.get("example_requests"), max_items=2, max_length=100
    )
    if not current_reqs:
        res["example_requests"] = extract_example_requests(summary_zh)
    else:
        res["example_requests"] = current_reqs

    # 3. 核心亮点：已有则清洗保留；若无则从 summary_zh 提取
    current_feats = normalize_string_list(
        res.get("key_features"), max_items=3, max_length=60
    )
    if not current_feats:
        res["key_features"] = extract_key_features(summary_zh, tags)
    else:
        res["key_features"] = current_feats

    return res


def enrich_catalog(root: Path | str) -> dict[str, Any]:
    """对主索引文件执行离线结构化增强，并同步更新页面数据。

    完全离线、幂等、不改变 summary_zh，安全可重复执行。
    """
    root_path = Path(root).resolve()
    catalog_file = root_path / "data" / "catalog.json"
    public_file = root_path / "public" / "data" / "catalog.json"

    if not catalog_file.exists():
        raise FileNotFoundError(f"未找到主索引文件：{catalog_file}")

    catalog = json.loads(catalog_file.read_text(encoding="utf-8"))
    raw_entries = catalog.get("entries", [])

    enriched_entries: list[dict] = []
    with_summary = 0
    with_skill_type = 0
    with_example_requests = 0
    with_key_features = 0

    for item in raw_entries:
        enriched = enrich_entry(item)
        enriched_entries.append(enriched)

        if enriched.get("summary_zh"):
            with_summary += 1
        if enriched.get("skill_type"):
            with_skill_type += 1
        if enriched.get("example_requests"):
            with_example_requests += 1
        if enriched.get("key_features"):
            with_key_features += 1

    ctx = CatalogContext(
        rules_version=catalog.get("rules_version"),
        generated_at=catalog.get("generated_at"),
    )
    new_catalog = build_catalog(
        enriched_entries,
        context=ctx,
        overrides=catalog.get("overrides"),
        snoozed=catalog.get("snoozed"),
    )

    write_catalog(new_catalog, data_path=catalog_file, public_path=public_file)

    return {
        "total": len(enriched_entries),
        "with_summary": with_summary,
        "with_skill_type": with_skill_type,
        "with_example_requests": with_example_requests,
        "with_key_features": with_key_features,
        "catalog_path": str(catalog_file),
        "page_path": str(public_file),
    }
