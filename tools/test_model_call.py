"""直接测试当前或指定的大模型连通性与评估效果（零网络爬取，单次调用快速验证）。

用法：
    # 1. 测试当前已激活的模型
    python tools/test_model_call.py

    # 2. 指定测试某个模型（无需先切换）
    python tools/test_model_call.py --model kimi-k3
    python tools/test_model_call.py --model qwen3.8-flash
    python tools/test_model_call.py --model glm-5.3

    # 3. 逐个一键测试所有配置了 API Key 的百炼模型
    python tools/test_model_call.py --all
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
import sys
import time

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src.catalog.config import load_all_config
from src.catalog.models import Candidate
from src.catalog.evaluation import evaluate, resolve_api_key
from src.catalog.decide import decide
from tools.switch_model import load_providers_catalog, resolve_target_provider_id

SAMPLE_SKILL_TEXT = """---
name: pdf-extractor
description: Extract text, tables and metadata from PDF files using PyMuPDF.
---
# PDF Extractor Skill

A skill for extracting structured text and tables from PDF documents.
Supports batch processing, OCR for scanned documents, and exporting to Markdown.

## Usage
Provide a path to a PDF file to extract its contents.
"""


def run_single_test(model_cfg: dict, rules: dict, taxonomy: dict, label: str = "") -> dict:
    model_name = model_cfg.get("model", "unknown")
    api_key = resolve_api_key(model_cfg)
    
    print(f"\n==================================================")
    print(f"[*] 正在测试模型: {label or model_name}")
    print(f"    - 模型代码: {model_name}")
    print(f"    - 接口端点: {model_cfg.get('endpoint')}")
    print(f"    - 密钥状态: {'已配置 (sk-***)' if api_key else '未配置'}")
    
    if not api_key:
        print("    [!] 错误: 未检测到有效 API Key，跳过测试。")
        return {"model": model_name, "ok": False, "error": "Missing API Key"}

    candidate = Candidate(
        skill_id="test-owner/pdf-extractor",
        owner="test-owner",
        repo="pdf-extractor",
        path="SKILL.md",
        content_fingerprint="mock-fingerprint-12345",
    )

    start_t = time.perf_counter()
    res = evaluate(
        candidate,
        SAMPLE_SKILL_TEXT,
        model_cfg=model_cfg,
        rules=rules,
        taxonomy=taxonomy,
        api_key=api_key,
    )
    elapsed = time.perf_counter() - start_t

    call = res.get("call")
    usage = getattr(call, "usage", {}) or {}
    p_tokens = usage.get("prompt_tokens", 0)
    c_tokens = usage.get("completion_tokens", 0)
    t_tokens = usage.get("total_tokens", 0)

    if not res.get("ok"):
        print(f"    [X] 调用失败 (耗时 {elapsed:.2f}s):")
        print(f"        错误信息: {res.get('error')}")
        print(f"        错误码: {res.get('reason_code')}")
        if call and getattr(call, "content", None):
            print(f"        模型原始返回截取: {call.content[:300]}...")
        return {"model": model_name, "ok": False, "error": res.get("error"), "elapsed": elapsed}

    ev = res["evaluation"]
    dec = decide(ev, rules)
    decision = dec.get("decision")
    print(f"    [+] 调用成功! (耗时 {elapsed:.2f}s)")
    print(f"        Token 消耗: 提示词 {p_tokens} + 生成 {c_tokens} = 共 {t_tokens} tokens")
    print(f"        主分类判定: {ev.get('main_category')}")
    print(f"        技能形态:   {ev.get('skill_type')}")
    print(f"        准入决策:   {decision}")
    print(f"        中文摘要:   {ev.get('summary_zh')}")
    return {
        "model": model_name,
        "ok": True,
        "decision": decision,
        "tokens": t_tokens,
        "elapsed": elapsed,
        "summary": ev.get("summary_zh"),
    }


def main():
    parser = argparse.ArgumentParser(description="测试 LLM 模型连通性与评估效果")
    parser.add_argument("--model", type=str, default=None, help="指定模型名称或代码（如 kimi-k3, qwen3.8-flash 等）")
    parser.add_argument("--all", action="store_true", help="逐一测试所有百炼模型")
    args = parser.parse_args()

    cfg = load_all_config(ROOT / "config")
    rules = cfg.get("rules", {})
    taxonomy = cfg.get("taxonomy", {})

    catalog = load_providers_catalog()
    providers = catalog.get("providers", {})

    if args.all:
        bailian_models = {
            pid: pcfg for pid, pcfg in providers.items()
            if pid.startswith("bailian-") and resolve_api_key(pcfg)
        }
        print(f"即将逐个测试全部 {len(bailian_models)} 个已配置 Key 的百炼模型...")
        results = []
        for pid, pcfg in bailian_models.items():
            r = run_single_test(pcfg, rules, taxonomy, label=f"{pcfg.get('name', pid)} ({pid})")
            results.append(r)
            time.sleep(1)

        print("\n" + "=" * 60)
        print("全部百炼模型测试汇总:")
        print(f"{'模型':<30} {'状态':<8} {'耗时':<8} {'消耗Token':<10} {'准入判定'}")
        print("-" * 60)
        for r in results:
            status = "PASS" if r.get("ok") else "FAIL"
            time_str = f"{r.get('elapsed', 0):.2f}s" if "elapsed" in r else "-"
            tokens_str = str(r.get("tokens", "-"))
            dec_str = r.get("decision", "-") if r.get("ok") else str(r.get("error", "Error"))[:20]
            print(f"{r['model']:<30} {status:<8} {time_str:<8} {tokens_str:<10} {dec_str}")
        print("=" * 60)
        return

    if args.model:
        target_id = resolve_target_provider_id(args.model, providers)
        if not target_id or target_id not in providers:
            print(f"[!] 未找到匹配的模型: {args.model}")
            return
        model_cfg = providers[target_id]
        run_single_test(model_cfg, rules, taxonomy, label=f"{model_cfg.get('name', target_id)} ({target_id})")
    else:
        # 默认当前激活模型
        model_cfg = cfg.get("model", {})
        run_single_test(model_cfg, rules, taxonomy, label=f"当前激活模型 ({model_cfg.get('model', 'unknown')})")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    sys.stderr.reconfigure(encoding="utf-8")
    main()
