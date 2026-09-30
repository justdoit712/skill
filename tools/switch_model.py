"""多厂商模型与 API Key 本地独立文件热插拔管理工具。

设计原则（配置公开、凭据分离、独立自治）：
1. 模型结构配置公开可提交：config/models/<标识>.json（含端点、模型名、参数与 key_ref）；
2. 密钥凭证独立存放在本地：config/secrets.local.json（被 .gitignore 严格忽略，仅本机保留）；
3. 共享引用：同一厂商多个模型共用同一个 key_ref（如 bailian），无需重复填写；
4. 一键热插拔激活：选中后原子写入 config/models/model.json（当前项目生效模型），不包含明文 Key；
5. 智能别名与模糊匹配（如 kimi-k3 自动定位到 bailian-kimi-k3，qwen 指向百炼）。

用法：
    python tools/switch_model.py                 # 交互式菜单选择切换
    python tools/switch_model.py deepseek        # 切换到 DeepSeek 官方 API
    python tools/switch_model.py qwen3.8-flash   # 切换到阿里云百炼 qwen3.8-flash
    python tools/switch_model.py kimi-k3         # 切换到阿里云百炼 kimi-k3
    python tools/switch_model.py glm-5.3         # 切换到阿里云百炼 glm-5.3
    python tools/switch_model.py --list          # 列出所有已配置的模型及状态
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
from src.infra.llm import validate_model_config, load_secrets, resolve_api_key
from src.infra.model_config import resolve_model_path
from src.infra.model_pool import ModelPool
from src.shared.model_config import parse_model_configs, state_key

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
MODEL_JSON_PATH = MODELS_DIR / "model.json"
MODEL_LOCAL_PATH = MODELS_DIR / "model.local.json"
PROVIDERS_LOCAL_PATH = MODELS_DIR / "providers.local.json"
SECRETS_LOCAL_PATH = ROOT / "config" / "secrets.local.json"


def mask_key(key: str | None) -> str:
    """脱敏展示 API Key，仅显示前 3 位和后 4 位。"""
    if not key or not str(key).strip():
        return "[未设置 Key]"
    s = str(key).strip()
    if len(s) <= 8:
        return "***"
    return f"{s[:3]}***{s[-4:]}"


def load_providers_catalog(models_dir: Path | None = None) -> dict:
    """加载所有独立厂商/模型配置文件。

    自动扫描 models_dir 下的 *.json 与 *.local.json（排除生效文件 model.json/model.local.json 与 secrets 文件）。
    """
    directory = models_dir or MODELS_DIR
    providers: dict[str, dict] = {}
    excluded = {
        "model.json",
        "model.local.json",
        "model.example.json",
        "providers.json",
        "providers.local.json",
        "secrets.local.json",
        "secrets.json",
    }

    if directory.exists():
        # 1. 先扫描公开的 *.json 配置
        for p in sorted(directory.glob("*.json")):
            if p.name in excluded or p.name.endswith(".local.json"):
                continue
            pid = p.stem
            try:
                content = read_json(p, default={})
                if isinstance(content, dict):
                    content.setdefault("name", pid)
                    content.setdefault("provider_id", pid)
                    providers[pid] = content
            except Exception:
                continue

        # 2. 再扫描私有的 *.local.json 配置（可补充或覆盖公开项）
        for p in sorted(directory.glob("*.local.json")):
            if p.name in excluded:
                continue
            pid = p.name[:-11]  # 剔除 .local.json
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
    active_path = MODEL_LOCAL_PATH if MODEL_LOCAL_PATH.exists() else MODEL_JSON_PATH
    if active_path.exists():
        cur = read_json(active_path, default={})
        active_id = cur.get("active_provider")
        if not active_id or (active_id not in providers and not any(str(active_id).startswith(f"{pid}/") for pid in providers)):
            cur_endpoint = cur.get("endpoint")
            cur_model = cur.get("model")
            for pid, pcfg in providers.items():
                if pcfg.get("endpoint") == cur_endpoint:
                    if pcfg.get("model") == cur_model:
                        active_id = pid
                        break
                    avail = pcfg.get("available_models") or {}
                    if isinstance(avail, dict) and cur_model in avail:
                        active_id = f"{pid}/{cur_model}"
                        break

    return {"active": active_id, "providers": providers}


def list_providers(catalog: dict) -> None:
    """格式化打印各独立厂商/平台配置状态列表。"""
    providers = catalog.get("providers", {})
    active_id = catalog.get("active")
    secrets = load_secrets()

    print("\n" + "=" * 94)
    print(f"{'#':<4} {'平台标识 (ID)':<16} {'默认/当前模型':<28} {'子模型':<12} {'状态 / Key':<24}")
    print("-" * 94)
    for idx, (pid, pcfg) in enumerate(providers.items(), 1):
        model = pcfg.get("model", "unknown")
        avail = pcfg.get("available_models") or {}
        sub_count = f"{len(avail)} 个可选" if avail else "-"
        auth = pcfg.get("auth") or {}
        key_ref = auth.get("key_ref")
        direct_key = auth.get("api_key")

        if key_ref:
            sec_val = secrets.get(str(key_ref))
            if sec_val:
                key_status = f"{mask_key(sec_val)} (ref:{key_ref})"
            else:
                key_status = f"[缺密钥 (ref:{key_ref})]"
        elif direct_key:
            key_status = mask_key(direct_key)
        else:
            key_status = "[未配置 Key]"

        is_active = (pid == active_id or (active_id and str(active_id).startswith(f"{pid}/")))
        active_mark = "[当前激活] " if is_active else "           "
        print(f"{idx:<4} {pid:<16} {model:<28} {sub_count:<12} {active_mark}{key_status}")
    print("=" * 94)
    print("提示: 可直接使用 'python tools/switch_model.py <模型名>'（如 kimi-k3、glm-5.3、qwen3.7-flash-2026-07-15）切换。\n")


def show_current() -> None:
    """打印当前生效的 model.json / model.local.json 配置摘要。"""
    active_path = MODEL_LOCAL_PATH if MODEL_LOCAL_PATH.exists() else MODEL_JSON_PATH
    if not active_path.exists():
        print(f"当前未激活任何模型：model.json 与 model.local.json 均不存在。")
        return

    cur = read_json(active_path, default={})
    if 'models' in cur:
        configs = parse_model_configs(cur)
        state = ModelPool(cur, ROOT).inspect()
        print(f"【当前生效模型队列 (来源: {active_path.name})】")
        for cfg in configs:
            exhausted = state_key(cfg['endpoint'], cfg['model']) in state['exhausted_models']
            ref = (cfg.get("auth") or {}).get("key_ref")
            ref_str = f" [ref:{ref}]" if ref else ""
            print(f"  {cfg['model']}{ref_str}：{'已耗尽' if exhausted else '可用'}")
        return

    auth = cur.get("auth") or {}
    key_ref = auth.get("key_ref")
    direct_key = auth.get("api_key")
    env_name = auth.get("api_key_env", "LLM_API_KEY")
    env_key = os.environ.get(env_name)
    secrets = load_secrets()
    ref_key = secrets.get(str(key_ref)) if key_ref else None

    print("\n" + "=" * 64)
    print(f"【当前项目生效模型配置 (来源: {active_path.name})】")
    print(f"  激活来源  : {cur.get('active_provider', 'custom')}")
    print(f"  展示名称  : {cur.get('name')}")
    print(f"  服务标识  : {cur.get('provider')}")
    print(f"  模型名称  : {cur.get('model')}")
    print(f"  服务端点  : {cur.get('endpoint')}")
    if key_ref:
        print(f"  密钥引用  : key_ref = '{key_ref}' -> {mask_key(ref_key)}")
    elif direct_key:
        print(f"  文件密钥  : {mask_key(direct_key)} (直接内联)")
    else:
        print(f"  文件密钥  : [未配置]")
    print(f"  环境密钥  : {mask_key(env_key)} (变量: {env_name})")
    print(f"  超时时间  : {cur.get('request', {}).get('timeout_seconds')} 秒")
    print(f"  响应格式  : {cur.get('request', {}).get('response_format', 'default')}")
    print("=" * 64 + "\n")


def resolve_target(raw_input: str, providers: dict) -> tuple[str | None, str | None, dict]:
    """多级模糊匹配目标模型标识与子模型。
    
    返回: (provider_id, sub_model_id, sub_model_metadata)
    """
    raw = raw_input.strip()
    low = raw.lower()

    # 1. 别名优先查找平台
    if low in ALIASES and ALIASES[low] in providers:
        return ALIASES[low], None, {}

    # 2. 精确匹配平台配置标识 (如 bailian 或 deepseek)
    if raw in providers:
        return raw, None, {}
    if low in providers:
        return low, None, {}

    # 3. 剥离前缀匹配（如 bailian-kimi-k3 -> kimi-k3）
    stripped = raw
    if raw.startswith("bailian-"):
        stripped = raw[8:]
    elif low.startswith("bailian-"):
        stripped = low[8:]

    # 4. 遍历平台，检查 available_models 匹配
    for pid, pcfg in providers.items():
        avail = pcfg.get("available_models") or {}
        if isinstance(avail, dict):
            for mid, minfo in avail.items():
                if mid.lower() == low or mid == raw or mid.lower() == stripped.lower():
                    meta = minfo if isinstance(minfo, dict) else {}
                    return pid, mid, meta

    # 5. 按主 model 字段反向搜索
    for pid, pcfg in providers.items():
        m = (pcfg.get("model") or "").strip()
        if m.lower() == low or m == raw:
            return pid, None, {}

    return None, None, {}


def resolve_target_provider_id(raw_input: str, providers: dict) -> str | None:
    """兼容旧接口：返回解析后的平台或模型复合标识。"""
    pid, sub, _ = resolve_target(raw_input, providers)
    if not pid:
        return None
    return f"{pid}/{sub}" if sub else pid


def switch_to_provider(provider_id: str, catalog: dict | None = None) -> bool:
    """切换到指定独立模型配置，并原子覆写到 model.json（不包含明文 Key）。"""
    active_path = MODEL_LOCAL_PATH if MODEL_LOCAL_PATH.exists() else MODEL_JSON_PATH
    if active_path.exists() and 'models' in (read_json(active_path, default={}) or {}):
        print('[拒绝] 当前使用模型队列，请编辑 model.json 的 models 列表；切换命令不会覆盖队列。')
        return False

    if catalog is None:
        catalog = load_providers_catalog()

    providers = catalog.get("providers", {})
    resolved_pid, sub_model, sub_meta = resolve_target(provider_id, providers)

    if not resolved_pid or resolved_pid not in providers:
        # 回退：检查直接以文件名形式存在的配置
        for suffix in (".json", ".local.json"):
            candidate_file = MODELS_DIR / f"{provider_id}{suffix}"
            if candidate_file.exists():
                providers[provider_id] = read_json(candidate_file, default={})
                resolved_pid = provider_id
                break

    if not resolved_pid or resolved_pid not in providers:
        print(f"[错误] 未找到模型配置 '{provider_id}'！")
        print(f"       请运行 'python tools/switch_model.py --list' 查看所有可选平台与模型。")
        return False

    target_cfg = dict(providers[resolved_pid])
    platform_name = target_cfg.get("name", resolved_pid)

    # 准备公开输出配置：移除明文 api_key，注入规范 key_ref
    out_cfg = json.loads(json.dumps(target_cfg))
    out_cfg.pop("available_models", None)

    if sub_model:
        out_cfg["model"] = sub_model
        if sub_meta.get("name"):
            out_cfg["name"] = sub_meta["name"]
        else:
            out_cfg["name"] = f"{platform_name} · {sub_model}"
        if "limits" in sub_meta and isinstance(sub_meta["limits"], dict):
            limits = dict(out_cfg.get("limits") or {})
            limits.update(sub_meta["limits"])
            out_cfg["limits"] = limits
        active_id = f"{resolved_pid}/{sub_model}"
        display_name = out_cfg["name"]
    else:
        active_id = resolved_pid
        display_name = platform_name

    # 校验合法性
    problems = validate_model_config(out_cfg)
    if problems:
        print(f"[警告] 模型配置预检存在潜在问题：{'；'.join(problems)}")

    auth = dict(out_cfg.get("auth") or {})
    auth.pop("api_key", None)

    # 若未定义 key_ref，则自动推断
    key_ref = auth.get("key_ref")
    if not key_ref:
        provider_val = str(out_cfg.get("provider") or resolved_pid).lower()
        if "bailian" in resolved_pid or "dashscope" in provider_val:
            key_ref = "bailian"
        elif "deepseek" in resolved_pid or "deepseek" in provider_val:
            key_ref = "deepseek"
        elif "gemini" in resolved_pid:
            key_ref = "gemini"
        elif "openai" in resolved_pid:
            key_ref = "openai"
        elif "moonshot" in resolved_pid or "kimi" in resolved_pid:
            key_ref = "moonshot"
        elif "zhipu" in resolved_pid or "glm" in resolved_pid:
            key_ref = "zhipu"
        elif "siliconflow" in resolved_pid:
            key_ref = "siliconflow"
        else:
            key_ref = resolved_pid.split("-")[0]
        auth["key_ref"] = key_ref

    out_cfg["auth"] = auth
    out_cfg["provider_id"] = resolved_pid
    out_cfg["active_provider"] = active_id
    out_cfg["note"] = f"当前由 tools/switch_model.py 从 {resolved_pid}.json 激活（{display_name}）。可提交版本库。"

    # 写入公开的 model.json
    write_json_atomic(MODEL_JSON_PATH, out_cfg)

    # 若之前存在本地私有覆盖 model.local.json，将其移除以确保 model.json 生效
    if MODEL_LOCAL_PATH.exists():
        try:
            MODEL_LOCAL_PATH.unlink()
        except OSError:
            pass

    # 同步更新 active 状态
    catalog["active"] = active_id
    if PROVIDERS_LOCAL_PATH.exists():
        legacy_data = read_json(PROVIDERS_LOCAL_PATH, default={})
        if isinstance(legacy_data, dict):
            legacy_data["active"] = active_id
            write_json_atomic(PROVIDERS_LOCAL_PATH, legacy_data)

    # 预检密钥状态
    secrets = load_secrets()
    actual_key = secrets.get(key_ref)
    env_var = auth.get("api_key_env", "LLM_API_KEY")
    has_env = bool(os.environ.get(env_var))

    print(f"[成功] 已热插拔切换至模型：{display_name}")
    print(f"       配置源文件: config/models/{resolved_pid}.json")
    print(f"       当前生效件: config/models/model.json")
    print(f"       模型代码  : {out_cfg.get('model')}")
    print(f"       端点地址  : {out_cfg.get('endpoint')}")
    if actual_key:
        print(f"       生效密钥  : {mask_key(actual_key)} (引用: secrets.local.json[{key_ref}])")
    elif has_env:
        print(f"       生效密钥  : {mask_key(os.environ.get(env_var))} (来自环境变量: {env_var})")
    else:
        print(f"[提示] 注意：尚未在 config/secrets.local.json 中设置 '{key_ref}' 的 Key。")
        print(f"       请在该文件中补充：{{\"{key_ref}\": \"你的API_Key\"}}")

    return True


def interactive_select(catalog: dict) -> None:
    """交互式终端选择菜单。"""
    providers = catalog.get("providers", {})
    if not providers:
        print("未发现任何可用的模型配置。")
        return

    list_providers(catalog)
    items = list(providers.keys())

    try:
        choice = input(f"请输入要激活的平台序号 (1-{len(items)}) 或模型标识/名称 (q 退出): ").strip()
        if not choice or choice.lower() in ("q", "quit", "exit"):
            print("操作已取消。")
            return

        if choice.isdigit():
            idx = int(choice)
            if 1 <= idx <= len(items):
                target_pid = items[idx - 1]
                pcfg = providers[target_pid]
                avail = pcfg.get("available_models") or {}
                if avail and isinstance(avail, dict):
                    print(f"\n【{pcfg.get('name', target_pid)} 包含 {len(avail)} 个可选模型】：")
                    sub_keys = list(avail.keys())
                    current_cat = None
                    for sidx, smid in enumerate(sub_keys, 1):
                        sinfo = avail[smid]
                        scat = sinfo.get("category", "") if isinstance(sinfo, dict) else ""
                        sbal = sinfo.get("balance", "") if isinstance(sinfo, dict) else ""
                        if scat and scat != current_cat:
                            current_cat = scat
                            print(f"\n  --- {current_cat} ---")
                        bal_str = f" [{sbal}]" if sbal else ""
                        print(f"  [{sidx:<3}] {smid:<34}{bal_str}")

                    sub_choice = input(f"\n请输入子模型序号 (1-{len(sub_keys)})、模型名或直接回车使用默认 ({pcfg.get('model')}): ").strip()
                    if not sub_choice:
                        switch_to_provider(target_pid, catalog)
                        return
                    if sub_choice.isdigit() and 1 <= int(sub_choice) <= len(sub_keys):
                        switch_to_provider(sub_keys[int(sub_choice) - 1], catalog)
                        return
                    matched = [k for k in sub_keys if sub_choice.lower() in k.lower()]
                    if len(matched) == 1:
                        switch_to_provider(matched[0], catalog)
                        return
                    elif sub_choice in matched:
                        switch_to_provider(sub_choice, catalog)
                        return
                    switch_to_provider(sub_choice, catalog)
                    return
            else:
                print(f"[错误] 序号超出有效范围 (1-{len(items)})！")
                return
        else:
            target_id = choice

        switch_to_provider(target_id, catalog)
    except (KeyboardInterrupt, EOFError):
        print("\n操作已取消。")


def check_current_model_connectivity() -> bool:
    """预检当前生效模型的连接性与凭据有效性。"""
    active_path = MODEL_LOCAL_PATH if MODEL_LOCAL_PATH.exists() else MODEL_JSON_PATH
    if not active_path.exists():
        print(f"[错误] 生效模型配置文件不存在！")
        return False

    cfg = read_json(active_path, default={})
    key = resolve_api_key(cfg)

    print("\n" + "=" * 50)
    print("【当前模型配置预检】")
    print(f"  模型名称: {cfg.get('model')}")
    print(f"  端点地址: {cfg.get('endpoint')}")
    print(f"  凭据检测: {'[OK] 已就绪 ' + mask_key(key) if key else '[FAIL] 凭据缺失'}")

    problems = validate_model_config(cfg)
    if problems:
        print(f"  配置合法性: [FAIL] {'；'.join(problems)}")
        return False
    print(f"  配置合法性: [OK] 格式合法")
    print("=" * 50 + "\n")
    return bool(key)


def main() -> int:
    parser = argparse.ArgumentParser(description="多厂商模型本地独立文件热插拔管理工具")
    parser.add_argument("target", nargs="?", default=None, help="目标模型标识或快捷别名（如 deepseek, qwen3.8-flash, kimi-k3）")
    parser.add_argument("--list", "-l", action="store_true", help="列出全部可用的模型配置")
    parser.add_argument("--show", "-s", action="store_true", help="显示当前生效的模型配置详情")
    parser.add_argument("--check", "-c", action="store_true", help="预检当前生效模型的配置与密钥完整性")
    args = parser.parse_args()

    if args.list:
        catalog = load_providers_catalog()
        list_providers(catalog)
        return 0

    if args.show:
        show_current()
        return 0

    if args.check:
        ok = check_current_model_connectivity()
        return 0 if ok else 1

    catalog = load_providers_catalog()
    if args.target:
        ok = switch_to_provider(args.target, catalog)
        return 0 if ok else 1

    interactive_select(catalog)
    return 0


if __name__ == "__main__":
    sys.exit(main())
