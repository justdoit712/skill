"""One authoritative model configuration loader for all local entry points."""
import json
from pathlib import Path
from src.shared.model_config import parse_model_configs


def resolve_model_path(config_dir, filename='model.local.json'):
    base = Path(config_dir)
    modern, legacy = base / 'models' / filename, base / filename
    if modern.exists() and legacy.exists():
        raise ValueError('模型配置新旧路径冲突，请只保留一份：' + filename)
    return legacy if legacy.exists() else modern


def load_model_config(config_dir, *, allow_example=True):
    path = resolve_model_path(config_dir)
    if not path.exists() and allow_example:
        path = resolve_model_path(config_dir, 'model.example.json')
    config = json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(config, dict):
        raise ValueError('模型配置必须为对象')
    if 'models' in config:
        parse_model_configs(config)
    return config
