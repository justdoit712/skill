"""Pure model-pool configuration and safe identities (no filesystem or transport)."""
from copy import deepcopy
import hashlib
import json
import math
from urllib.parse import urlsplit, urlunsplit

REQUEST_FIELDS = frozenset({'timeout_seconds', 'max_attempts', 'temperature', 'response_format'})
SINGLE_LIMITS = frozenset({'max_input_bytes', 'max_output_tokens'})
TASK_LIMITS = frozenset({'max_calls_per_week', 'max_total_tokens_per_run'})
PREFLIGHT_FIELDS = frozenset({'mode', 'unknown_spec', 'uncertain_tokens', 'safety_margin_tokens'})
DEFAULT_PREFLIGHT = {
    'mode': 'off',
    'unknown_spec': 'passthrough',
    'uncertain_tokens': 'conservative',
    'safety_margin_tokens': 1024,
}


def normalize_endpoint(value):
    if not isinstance(value, str):
        raise ValueError('endpoint 必须为 HTTP(S) 地址')
    p = urlsplit(value)
    if (p.scheme.lower() not in ('http', 'https') or not p.hostname or p.username is not None
            or p.password is not None or '?' in value or '#' in value or any(c.isspace() for c in value)):
        raise ValueError('endpoint 不得包含凭据、查询串、片段或空白')
    host = p.hostname.lower()
    if ':' in host:
        host = '[' + host + ']'
    port = p.port
    if port and (p.scheme.lower(), port) not in (('https', 443), ('http', 80)):
        host += ':' + str(port)
    return urlunsplit((p.scheme.lower(), host, p.path, '', ''))


def state_key(endpoint, model):
    raw = json.dumps([normalize_endpoint(endpoint), model], ensure_ascii=False, separators=(',', ':'))
    return hashlib.sha256(raw.encode('utf-8')).hexdigest()


def _positive(value, name, integer=True):
    if (isinstance(value, bool) or not isinstance(value, int if integer else (int, float))
            or not math.isfinite(value) or value <= 0):
        raise ValueError(name + ' 必须为正' + ('整数' if integer else '数'))


def parse_preflight_config(value):
    """Validate the standalone policy without coercing values or ignoring fields."""
    if not isinstance(value, dict) or set(value) - PREFLIGHT_FIELDS:
        raise ValueError('preflight 包含不支持的字段或类型错误')
    pf = {**DEFAULT_PREFLIGHT, **value}
    if pf['mode'] not in ('off', 'validate', 'adapt'):
        raise ValueError('preflight.mode 必须为 off、validate 或 adapt')
    if pf['unknown_spec'] not in ('passthrough', 'reject'):
        raise ValueError('preflight.unknown_spec 必须为 passthrough 或 reject')
    if pf['uncertain_tokens'] not in ('conservative', 'reject'):
        raise ValueError('preflight.uncertain_tokens 必须为 conservative 或 reject')
    margin = pf['safety_margin_tokens']
    if type(margin) is not int or margin < 0:
        raise ValueError('preflight.safety_margin_tokens 必须为非负整数')
    return pf


def _validate_sections(cfg):
    for section, allowed in (('request', REQUEST_FIELDS), ('limits', SINGLE_LIMITS | TASK_LIMITS),
                             ('capabilities', {'json_schema'}), ('preflight', PREFLIGHT_FIELDS)):
        data = cfg.get(section, {})
        if not isinstance(data, dict) or set(data) - allowed:
            raise ValueError(section + ' 包含不支持的字段或类型错误')
    for k, v in cfg.get('limits', {}).items():
        _positive(v, k)
    req = cfg.get('request', {})
    for k in ('timeout_seconds', 'max_attempts'):
        if k in req:
            _positive(req[k], k, k == 'max_attempts')
    if 'temperature' in req and (isinstance(req['temperature'], bool)
            or not isinstance(req['temperature'], (int, float))
            or not math.isfinite(req['temperature']) or not 0 <= req['temperature'] <= 2):
        raise ValueError('temperature 必须在 0 到 2 之间')
    caps = cfg.get('capabilities', {})
    if any(type(v) is not bool for v in caps.values()):
        raise ValueError('capabilities 必须使用布尔值')
    parse_preflight_config(cfg.get('preflight', {}))
    fmt = req.get('response_format')
    if fmt is not None:
        if isinstance(fmt, str):
            kind = fmt
        elif isinstance(fmt, dict) and not set(fmt) - {'type', 'json_schema'}:
            kind = fmt.get('type')
            if kind == 'json_schema':
                schema = fmt.get('json_schema')
                if (not isinstance(schema, dict) or not isinstance(schema.get('schema'), dict)
                        or not isinstance(schema.get('name'), str) or not schema['name'].strip()
                        or set(schema) - {'name', 'schema', 'strict', 'description'}
                        or ('strict' in schema and type(schema['strict']) is not bool)):
                    raise ValueError('response_format json_schema 无效')
            elif set(fmt) != {'type'}:
                raise ValueError('response_format 包含不支持字段')
        else:
            raise ValueError('response_format 无效')
        if kind not in ('json_object', 'json_schema', 'text'):
            raise ValueError('response_format 类型不支持')
        if kind == 'json_schema' and caps.get('json_schema') is False:
            raise ValueError('json_schema 与模型能力声明冲突')


def parse_model_configs(config):
    """Return independent effective model dictionaries; never mutate input."""
    if not isinstance(config, dict):
        raise ValueError('模型配置必须为对象')
    if 'models' not in config:
        if not isinstance(config.get('model'), str) or not config['model'].strip():
            raise ValueError('模型配置缺 model')
        _validate_sections(config)
        effective = deepcopy(config)
        effective['preflight'] = {**DEFAULT_PREFLIGHT, **effective.get('preflight', {})}
        return (effective,)
    if 'model' in config:
        raise ValueError('model 与 models 字段互斥')
    items = config['models']
    if not isinstance(items, list) or not items:
        raise ValueError('models 必须为非空数组')
    if config.get('provider') != 'dashscope':
        raise ValueError('模型池第一版仅支持 dashscope')
    endpoint = normalize_endpoint(config.get('endpoint'))
    auth = config.get('auth', {})
    if (not isinstance(auth, dict) or set(auth) - {'api_key', 'api_key_env', 'key_ref', 'type'}
            or any(not isinstance(v, str) for v in auth.values())):
        raise ValueError('auth 配置无效')
    _validate_sections(config)
    root_preflight = {**DEFAULT_PREFLIGHT, **config.get('preflight', {})}
    seen, result = set(), []
    for item in items:
        entry = {'model': item} if isinstance(item, str) else item
        if not isinstance(entry, dict) or set(entry) - {'model', 'request', 'limits', 'capabilities'}:
            raise ValueError('模型项包含未知字段或类型错误')
        name = entry.get('model')
        if not isinstance(name, str) or not name.strip() or name.strip() in seen:
            raise ValueError('模型 ID 为空、类型错误或重复')
        name = name.strip()
        seen.add(name)
        effective = deepcopy({k: v for k, v in config.items() if k != 'models'})
        effective.update(model=name, endpoint=endpoint)
        effective['preflight'] = deepcopy(root_preflight)
        for section in ('request', 'limits', 'capabilities'):
            override = entry.get(section, {})
            if not isinstance(override, dict):
                raise ValueError(section + ' 必须为对象')
            if section == 'limits' and set(override) - SINGLE_LIMITS:
                raise ValueError('模型项不得覆盖任务预算或周配额')
            effective[section] = {**deepcopy(config.get(section, {})), **deepcopy(override)}
        _validate_sections(effective)
        result.append(effective)
    return tuple(result)


def model_fingerprint(config):
    # Preflight is standalone and does not change production requests yet.
    # Keep its compatibility identity separate from the live pool cache key.
    safe = {'endpoint': normalize_endpoint(config['endpoint']), 'model': config.get('model'),
            'provider': config.get('provider'),
            'request': {k: v for k, v in config.get('request', {}).items() if k in REQUEST_FIELDS},
            'limits': {k: v for k, v in config.get('limits', {}).items() if k in SINGLE_LIMITS},
            'capabilities': {k: v for k, v in config.get('capabilities', {}).items() if k == 'json_schema'}}
    return 'sha256:' + hashlib.sha256(json.dumps(safe, sort_keys=True, ensure_ascii=False).encode()).hexdigest()[:16]


def queue_fingerprint(config):
    identities = {'models': [model_fingerprint(c) for c in parse_model_configs(config)],
                  'limits': {k: v for k, v in config.get('limits', {}).items() if k in TASK_LIMITS},
                  'model_config_version': config.get('model_config_version')}
    return 'sha256:' + hashlib.sha256(json.dumps(identities, sort_keys=True).encode()).hexdigest()[:16]


def evaluation_identity(skill_id, fingerprint, rules, model_config):
    # Queue membership is audit data, not the stable primary key of a checkpoint.
    version = 'pool-business-v1' if 'models' in model_config else str(model_config.get('model_config_version') or '')
    return '|'.join([skill_id, fingerprint or 'nofingerprint', str(rules.get('rules_version') or ''), version])


def material_fetch_limit(config):
    return max(c.get('limits', {}).get('max_input_bytes', 262144) for c in parse_model_configs(config))
