"""多厂商模型与 API Key 本地独立文件热插拔管理工具。

设计原则（分离存储、独立自治）：
1. 每个厂商拥有独立的私有配置文件：config/models/<厂商ID>.local.json（如 deepseek.local.json、bailian.local.json）；
2. 相互独立、互不混合，各自存放端点、模型名与私有 API Key；
3. 一键热插拔激活：选中后原子写入 config/models/model.local.json（当前项目生效模型）；
4. 支持厂商别名快捷切换（如 qwen、aliyun 自动指向 bailian）；
5. 所有 *.local.json 均被 .gitignore 严格忽略，杜绝凭据泄漏风险。

用法：
    python tools/switch_model.py                 # 交互式菜单选择切换
    python tools/switch_model.py deepseek        # 直接切换到 deepseek.local.json
    python tools/switch_model.py bailian         # 直接切换到 bailian.local.json (阿里云百炼平台)
    python tools/switch_model.py qwen            # 别名切换：自动定位到 bailian.local.json
    python tools/switch_model.py --list          # 列出所有可用厂商独立配置及状态
    python tools/switch_model.py --show          # 显示当前生效模型信息
    python tools/switch_model.py --check         # 预检当前生效的模型连接配置
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

# 厂商快捷别名映射
ALIASES: dict[str, str] = {
    "qwen": "bailian",
    "aliyun": "bailian",
    "dashscope": "bailian",
}


def _get_models_dir() -> Path:
    sub = ROOT / "config" / "models"
    if sub.exists():
        return sub
    flat = ROOT / "config"
    return flat


MODELS_DIR = _get_models_dir()
MODEL_LOCAL_PATH = MODELS_DIR / "model.local.json"
PROVIDERS_LOCAL_PATH = MODELS_DIR / "providers.local.json"


def mask_key(key: str | None) -> str:
    """脱敏展示 API Key，仅显示前 3 位和后 4 位。"""
    if not key or not str(key).strip():
        return "[未设置 Key]"
    s = str(key).strip()
    if len(s) <= 8:
        return "***"
    return f"{s[:3]}***{s[-4:]}"


def load_providers_catalog(models_dir: Path | None = None) -> dict:
    """加载所有独立厂商配置文件。
    
    自动扫描 models_dir 下的 *.local.json（排除当前生效的 model.local.json 与旧版 providers.local.json）。
    """
    directory = models_dir or MODELS_DIR
    providers: dict[str, dict] = {}

    if directory.exists():
        for p in sorted(directory.glob("*.local.json")):
            if p.name in ("model.local.json", "providers.local.json"):
                continue
            # pid: deepseek.local.json -> deepseek
            pid = p.name[:-11] if p.name.endswith(".local.json") else p.stem
            try:
                content = read_json(p, default={})
                if isinstance(content, dict):
                    content.setdefault("name", pid)
                    content.setdefault("provider_id", pid)
                    providers[pid] = content
            except Exception:
                continue

    # 兼容回退：若未扫描到独立文件但存在 providers.local.json
    if not providers and PROVIDERS_LOCAL_PATH.exists():
        legacy_data = read_json(PROVIDERS_LOCAL_PATH, default={})
        if isinstance(legacy_data, dict) and "providers" in legacy_data:
            providers = legacy_data["providers"]

    # 检测当前激活厂商
    active_id: str | None = None
    if MODEL_LOCAL_PATH.exists():
        cur = read_json(MODEL_LOCAL_PATH, default={})
        active_id = cur.get("active_provider")
        if not active_id:
            cur_endpoint = cur.get("endpoint")
            cur_model = cur.get("model")
            for pid, pcfg in providers.items():
                if pcfg.get("endpoint") == cur_endpoint and pcfg.get("model") == cur_model:
                    active_id = pid
                    break

    return {"active": active_id, "providers": providers}


def list_providers(catalog: dict) -> None:
    """格式化打印各独立厂商配置状态列表。"""
    providers = catalog.get("providers", {})
    active_id = catalog.get("active")
    print("\n" + "=" * 68)
    print(f"{'#':<4} {'厂商 ID':<14} {'模型名称':<24} {'状态 / Key':<18}")
    print("-" * 68)
    for idx, (pid, pcfg) in enumerate(providers.items(), 1):
        name = pcfg.get("name", pid)
        model = pcfg.get("model", "unknown")
        key = (pcfg.get("auth") or {}).get("api_key")
        is_active = (pid == active_id)
        active_mark = "[当前激活] " if is_active else "           "
        key_status = mask_key(key)
        print(f"{idx:<4} {pid:<14} {model:<24} {active_mark}{key_status}")
    print("=" * 68 + "\n")


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
    print("【当前项目生效模型配置 (config/models/model.local.json)】")
    print(f"  激活来源  : {cur.get('active_provider', 'custom')}")
    print(f"  服务标识  : {cur.get('provider')}")
    print(f"  模型名称  : {cur.get('model')}")
    print(f"  服务端点  : {cur.get('endpoint')}")
    print(f"  文件密钥  : {mask_key(key)}")
    print(f"  环境密钥  : {mask_key(env_key)} (变量: {env_name})")
    print(f"  超时时间  : {cur.get('request', {}).get('timeout_seconds')} 秒")
    print(f"  响应格式  : {cur.get('request', {}).get('response_format', 'default')}")
    print("=" * 55 + "\n")


def switch_to_provider(provider_id: str, catalog: dict | None = None) -> bool:
    """切换到指定独立厂商配置文件，并原子覆写到 model.local.json。"""
    if catalog is None:
        catalog = load_providers_catalog()

    providers = catalog.get("providers", {})
    resolved_id = ALIASES.get(provider_id.lower(), provider_id)
    target_cfg = None

    # 1. 优先从已加载 catalog 中获取
    if resolved_id in providers:
        target_cfg = dict(providers[resolved_id])
    elif provider_id in providers:
        target_cfg = dict(providers[provider_id])
        resolved_id = provider_id
    else:
        # 2. 尝试直接从独立文件读取
        candidate_file = MODELS_DIR / f"{resolved_id}.local.json"
        if candidate_file.exists():
            target_cfg = read_json(candidate_file, default={})

    if not target_cfg:
        available = list(providers.keys())
        alias_hints = [f"{k}->{v}" for k, v in ALIASES.items() if v in providers]
        hint_str = f"可选厂商：{', '.join(available)}"
        if alias_hints:
            hint_str += f"（支持别名：{', '.join(alias_hints)}）"
        print(f"[错误] 未找到厂商 ID '{provider_id}'，{hint_str}")
        return False

    name = target_cfg.get("name", resolved_id)

    # 校验合法性
    problems = validate_model_config(target_cfg)
    if problems:
        print(f"[警告] 模型配置预检存在潜在问题：{'；'.join(problems)}")

    auth = target_cfg.get("auth") or {}
    key = auth.get("api_key")
    env_var = auth.get("api_key_env", "LLM_API_KEY")
    has_env = bool(os.environ.get(env_var))

    if not key and not has_env:
        print(f"[提示] 注意：厂商 '{resolved_id}' 当前未设置 api_key，环境变量 '{env_var}' 亦为空。")
        print(f"       请在 config/models/{resolved_id}.local.json 中填入私有 Key。")

    # 记录 active 标记并原子写入 model.local.json
    target_cfg["active_provider"] = resolved_id
    target_cfg["note"] = f"当前由 tools/switch_model.py 从 {resolved_id}.local.json 激活（厂商: {name}）。禁止提交、禁止写入日志。"
    write_json_atomic(MODEL_LOCAL_PATH, target_cfg)

    # 若内存目录传入，同步更新 active 状态
    catalog["active"] = resolved_id
    if PROVIDERS_LOCAL_PATH.exists():
        legacy_data = read_json(PROVIDERS_LOCAL_PATH, default={})
        if isinstance(legacy_data, dict):
            legacy_data["active"] = resolved_id
            write_json_atomic(PROVIDERS_LOCAL_PATH, legacy_data)

    print(f"[成功] 已热插拔切换至模型：{name} ({resolved_id})")
    print(f"       独立源文件: config/models/{resolved_id}.local.json")
    print(f"       当前生效件: config/models/model.local.json")
    print(f"       模型名称  : {target_cfg.get('model')}")
    print(f"       端点地址  : {target_cfg.get('endpoint')}")
    print(f"       当前密钥  : {mask_key(key)}")
    return True


def interactive_select(catalog: dict) -> None:
    """交互式终端选择菜单。"""
    providers = catalog.get("providers", {})
    if not providers:
        print("[错误] 未扫描到任何独立模型配置文件 (config/models/*.local.json)！")
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

    target_id = ALIASES.get(choice.lower(), choice)
    if target_id in providers or (MODELS_DIR / f"{target_id}.local.json").exists():
        switch_to_provider(target_id, catalog)
    else:
        print(f"[错误] 无效的厂商 ID: '{choice}'")


def main() -> None:
    parser = argparse.ArgumentParser(description="多厂商大模型与 Key 本地独立文件热插拔管理工具")
    parser.add_argument("provider", nargs="?", help="要切换到的目标厂商 ID (如 deepseek, bailian, siliconflow, ollama；支持别名 qwen, aliyun)")
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
        target_id = ALIASES.get(args.provider.strip().lower(), args.provider.strip())
        ok = switch_to_provider(target_id, catalog)
        sys.exit(0 if ok else 1)

    # 无参数时进入交互选择
    interactive_select(catalog)


if __name__ == "__main__":
    main()
