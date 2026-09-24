"""多厂商模型与 API Key 本地热插拔切换工具。

功能：
1. 维护 config/providers.local.json 中的多厂商模型与 Key 配置库；
2. 一键热插拔切换当前项目生效的 config/model.local.json；
3. 支持命令行直切与交互式编号菜单选择；
4. 切换前自动校验配置格式，切换后原子写入，无损安全。

用法：
    python tools/switch_model.py                 # 交互式菜单选择切换
    python tools/switch_model.py deepseek        # 直接切换到 deepseek
    python tools/switch_model.py qwen            # 直接切换到通义千问
    python tools/switch_model.py --list          # 列出所有可用厂商及状态
    python tools/switch_model.py --show          # 显示当前生效模型信息
    python tools/switch_model.py --check         # 预检当前模型连接配置
"""

from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from src.infra.files import read_json, write_json_atomic
from src.infra.llm import validate_model_config

def _res_model_file(fname: str) -> Path:
    sub = ROOT / "config" / "models" / fname
    if sub.exists():
        return sub
    flat = ROOT / "config" / fname
    if flat.exists():
        return flat
    return sub

PROVIDERS_LOCAL_PATH = _res_model_file("providers.local.json")
PROVIDERS_EXAMPLE_PATH = _res_model_file("providers.example.json")
MODEL_LOCAL_PATH = _res_model_file("model.local.json")
MODEL_EXAMPLE_PATH = _res_model_file("model.example.json")


def mask_key(key: str | None) -> str:
    """脱敏展示 API Key，仅显示前 3 位和后 4 位。"""
    if not key or not str(key).strip():
        return "[未设置 Key]"
    s = str(key).strip()
    if len(s) <= 8:
        return "***"
    return f"{s[:3]}***{s[-4:]}"


def load_providers_catalog() -> dict:
    """加载厂商配置库，若 local 不存在则尝试自动从 example 初始化。"""
    if PROVIDERS_LOCAL_PATH.exists():
        data = read_json(PROVIDERS_LOCAL_PATH, default={})
        if isinstance(data, dict) and "providers" in data:
            return data

    if PROVIDERS_EXAMPLE_PATH.exists():
        example_data = read_json(PROVIDERS_EXAMPLE_PATH, default={})
        if isinstance(example_data, dict):
            # 若 model.local.json 已经有 key，尝试回填到 example 中
            if MODEL_LOCAL_PATH.exists():
                cur = read_json(MODEL_LOCAL_PATH, default={})
                cur_provider = cur.get("provider")
                cur_key = (cur.get("auth") or {}).get("api_key")
                if cur_provider and cur_key and cur_provider in example_data.get("providers", {}):
                    example_data["providers"][cur_provider]["auth"]["api_key"] = cur_key
            write_json_atomic(PROVIDERS_LOCAL_PATH, example_data)
            return example_data

    return {"active": None, "providers": {}}


def list_providers(catalog: dict) -> None:
    """格式化打印厂商列表。"""
    providers = catalog.get("providers", {})
    active_id = catalog.get("active")
    print("\n" + "=" * 65)
    print(f"{'#':<4} {'厂商 ID':<14} {'模型名称':<22} {'状态 / Key':<18}")
    print("-" * 65)
    for idx, (pid, pcfg) in enumerate(providers.items(), 1):
        name = pcfg.get("name", pid)
        model = pcfg.get("model", "unknown")
        key = (pcfg.get("auth") or {}).get("api_key")
        is_active = (pid == active_id)
        active_mark = "[当前激活] " if is_active else "           "
        key_status = mask_key(key)
        print(f"{idx:<4} {pid:<14} {model:<22} {active_mark}{key_status}")
    print("=" * 65 + "\n")


def show_current() -> None:
    """打印当前生效的 model.local.json 配置摘要。"""
    if not MODEL_LOCAL_PATH.exists():
        print(f"当前未激活任何模型：{MODEL_LOCAL_PATH.name} 不存在。")
        return
    cur = read_json(MODEL_LOCAL_PATH, default={})
    auth = cur.get("auth") or {}
    key = auth.get("api_key")
    env_name = auth.get("api_key_env", "LLM_API_KEY")
    env_key = os.environ.get(env_name)

    print("\n" + "=" * 55)
    print("【当前项目生效模型配置 (config/model.local.json)】")
    print(f"  服务标识  : {cur.get('provider')}")
    print(f"  模型名称  : {cur.get('model')}")
    print(f"  服务端点  : {cur.get('endpoint')}")
    print(f"  文件密钥  : {mask_key(key)}")
    print(f"  环境密钥  : {mask_key(env_key)} (变量: {env_name})")
    print(f"  超时时间  : {cur.get('request', {}).get('timeout_seconds')} 秒")
    print(f"  响应格式  : {cur.get('request', {}).get('response_format', 'default')}")
    print("=" * 55 + "\n")


def switch_to_provider(provider_id: str, catalog: dict) -> bool:
    """切换到指定厂商配置并更新 model.local.json。"""
    providers = catalog.get("providers", {})
    if provider_id not in providers:
        print(f"[错误] 未找到厂商 ID '{provider_id}'，可选厂商：{', '.join(providers.keys())}")
        return False

    target_cfg = dict(providers[provider_id])
    name = target_cfg.pop("name", provider_id)

    # 校验合法性
    problems = validate_model_config(target_cfg)
    if problems:
        print(f"[警告] 模型配置预检存在潜在问题：{'；'.join(problems)}")

    auth = target_cfg.get("auth") or {}
    key = auth.get("api_key")
    env_var = auth.get("api_key_env", "LLM_API_KEY")
    has_env = bool(os.environ.get(env_var))

    if not key and not has_env:
        print(f"[提示] 注意：厂商 '{provider_id}' 当前未设置 api_key，环境变量 '{env_var}' 亦为空。")
        print(f"       切换后请在 config/providers.local.json 或 config/model.local.json 中填入 Key。")

    # 写入 model.local.json
    target_cfg["note"] = f"当前由 tools/switch_model.py 激活（厂商: {name}）。禁止提交、禁止写入日志。"
    write_json_atomic(MODEL_LOCAL_PATH, target_cfg)

    # 更新 providers.local.json 中的 active 标记
    catalog["active"] = provider_id
    write_json_atomic(PROVIDERS_LOCAL_PATH, catalog)

    print(f"[成功] 已热插拔切换至模型：{name} ({provider_id})")
    print(f"       生效文件: config/model.local.json")
    print(f"       模型名称: {target_cfg.get('model')}")
    print(f"       端点地址: {target_cfg.get('endpoint')}")
    print(f"       当前密钥: {mask_key(key)}")
    return True


def interactive_select(catalog: dict) -> None:
    """交互式终端选择菜单。"""
    providers = catalog.get("providers", {})
    if not providers:
        print("[错误] 厂商配置库为空！")
        return

    list_providers(catalog)
    keys = list(providers.keys())
    active_id = catalog.get("active")

    try:
        choice = input(f"请输入要激活的厂商编号 [1-{len(keys)}] 或 厂商ID (直接回车保持当前): ").strip()
    except (KeyboardInterrupt, EOFError):
        print("\n已取消操作。")
        return

    if not choice:
        print(f"保持当前模型：{active_id}")
        return

    if choice.isdigit():
        idx = int(choice)
        if 1 <= idx <= len(keys):
            target_id = keys[idx - 1]
            switch_to_provider(target_id, catalog)
            return
        else:
            print("[错误] 输入编号超出范围。")
            return

    if choice in providers:
        switch_to_provider(choice, catalog)
    else:
        print(f"[错误] 无效的厂商 ID: '{choice}'")


def main() -> None:
    parser = argparse.ArgumentParser(description="多厂商大模型与 Key 本地热插拔管理工具")
    parser.add_argument("provider", nargs="?", help="要切换到的目标厂商 ID (如 deepseek, qwen, siliconflow, ollama)")
    parser.add_argument("-l", "--list", action="store_true", help="列出所有已配置的厂商及状态")
    parser.add_argument("-s", "--show", "--current", action="store_true", help="显示当前生效模型详情")
    parser.add_argument("-c", "--check", action="store_true", help="预检当前生效的模型连接配置")
    args = parser.parse_args()

    catalog = load_providers_catalog()

    if args.list:
        list_providers(catalog)
        return

    if args.show:
        show_current()
        return

    if args.check:
        if not MODEL_LOCAL_PATH.exists():
            print(f"[错误] {MODEL_LOCAL_PATH.name} 不存在，请先切换或配置模型。")
            sys.exit(1)
        cur = read_json(MODEL_LOCAL_PATH, default={})
        problems = validate_model_config(cur)
        if problems:
            print("[预检未通过]:\n  - " + "\n  - ".join(problems))
            sys.exit(1)
        print("[OK] 当前模型连接配置格式合法！")
        show_current()
        return

    if args.provider:
        ok = switch_to_provider(args.provider.strip(), catalog)
        sys.exit(0 if ok else 1)

    # 无参数时进入交互选择
    interactive_select(catalog)


if __name__ == "__main__":
    main()
