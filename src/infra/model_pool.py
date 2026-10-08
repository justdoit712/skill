"""Process-safe exhaustion/cooldown state and individually accounted rotation."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
import re
from pathlib import Path
import threading
from uuid import uuid4

from src.infra.files import file_lock, write_json_atomic
from src.infra.model_config import load_model_config
from src.shared.model_config import parse_model_configs, state_key, normalize_endpoint, model_fingerprint
from src.shared.output_contracts import resolve_response_format
from src.shared.usage import UsageTotals
from src.infra.llm import (
    REASON_RESPONSE_EMPTY,
    REASON_MODEL_REQUEST_INCOMPATIBLE,
    INCOMPATIBLE_PARAMETERS,
    REASON_LENGTH_EXCEEDED,
    REASON_MODEL_ERROR,
    REASON_NETWORK_ERROR,
    ModelCallResult,
)

QUOTA_CODE = 'AllocationQuota.FreeTierOnly'
EMPTY_RESPONSE_MAX_ATTEMPTS = 2
MODEL_COOLDOWN_SECONDS = 30 * 60
TRANSIENT_COOLDOWN_SECONDS = 5 * 60
ALLOWED_COOLDOWN_REASONS = frozenset({
    REASON_RESPONSE_EMPTY,
    'NETWORK_ERROR',
    'SERVER_ERROR',
    'RATE_LIMIT',
    'TIMEOUT',
    'MODEL_ERROR',
})


class PoolStopped(RuntimeError):
    def __init__(self, reason, message=None):
        self.reason = reason
        super().__init__(message or reason)


class PoolReselect(Exception):
    """Admission waited; selected model was exhausted before request started."""


class ModelPool:
    def __init__(self, config, root, *, authoritative=True, log=lambda message: None, now=None):
        self._snapshot = deepcopy(config)
        self._configs = parse_model_configs(config)
        self.root = Path(root).resolve()
        self.path = self.root / 'data/local/model-pool-state.json'
        self.lock_path = self.path.with_suffix('.lock')
        self._lock = threading.RLock()
        self._local_exhausted = set()
        self._local_cooled = {}
        self._local_incompatible = set()
        self.now = now or (lambda: datetime.now().astimezone())
        self.authoritative = authoritative
        self.log = log
        self.timeout = 5
        self.failure = None

    def _read(self):
        if not self.path.exists():
            return {'state_version': '1.0.0', 'updated_at': datetime.now().astimezone().isoformat(), 'exhausted_models': {}}
        state = json.loads(self.path.read_text(encoding='utf-8'))
        if (not isinstance(state, dict) or state.get('state_version') not in ('1.0.0', '1.1.0')
                or not isinstance(state.get('updated_at'), str)
                or not isinstance(state.get('exhausted_models'), dict)):
            raise ValueError('模型池状态损坏或版本不支持')
        datetime.fromisoformat(state['updated_at'])
        for key, row in state['exhausted_models'].items():
            if (not isinstance(row, dict) or not isinstance(row.get('model'), str) or not row['model'].strip()
                    or row['model'] != row['model'].strip() or row.get('error_code') != QUOTA_CODE
                    or type(row.get('http_status')) is not int or not 400 <= row['http_status'] <= 599
                    or normalize_endpoint(row.get('endpoint')) != row.get('endpoint')
                    or state_key(row['endpoint'], row['model']) != key
                    or not isinstance(row.get('exhausted_at'), str)):
                raise ValueError('模型池状态记录身份或字段不合法')
            datetime.fromisoformat(row['exhausted_at'])
        cooldowns = state.setdefault('cooldown_models', {})
        if not isinstance(cooldowns, dict):
            raise ValueError('模型冷却状态必须为对象')
        for key, row in cooldowns.items():
            if (not isinstance(row, dict) or not isinstance(row.get('model'), str) or not row['model'].strip()
                    or row['model'] != row['model'].strip()
                    or row.get('reason_code') not in ALLOWED_COOLDOWN_REASONS
                    or (row.get('http_status') is not None and (type(row.get('http_status')) is not int or not 100 <= row['http_status'] <= 599))
                    or normalize_endpoint(row.get('endpoint')) != row.get('endpoint')
                    or state_key(row['endpoint'], row['model']) != key):
                raise ValueError('模型冷却记录身份或字段不合法')
            started = datetime.fromisoformat(row['cooled_at'])
            until = datetime.fromisoformat(row['cooldown_until'])
            if started.tzinfo is None or until.tzinfo is None or until <= started:
                raise ValueError('模型冷却时间不合法')
        incompatible = state.setdefault('incompatible_models', {})
        if not isinstance(incompatible, dict):
            raise ValueError('模型参数不兼容状态必须为对象')
        for key, row in incompatible.items():
            if (not isinstance(row, dict) or not isinstance(row.get('model'), str) or not row['model'].strip()
                    or row['model'] != row['model'].strip() or row.get('reason_code') != REASON_MODEL_REQUEST_INCOMPATIBLE
                    or type(row.get('http_status')) is not int or row['http_status'] != 400
                    or normalize_endpoint(row.get('endpoint')) != row.get('endpoint')
                    or state_key(row['endpoint'], row['model']) != key
                    or not isinstance(row.get('config_fingerprint'), str)
                    or not re.fullmatch(r'sha256:[0-9a-f]{16}', row['config_fingerprint'])
                    or row.get('parameter') not in INCOMPATIBLE_PARAMETERS):
                raise ValueError('模型参数不兼容记录身份或字段不合法')
            datetime.fromisoformat(row['rejected_at'])
        return state

    def _current(self):
        return load_model_config(self.root / 'config', allow_example=False) if self.authoritative else deepcopy(self._snapshot)

    def _transaction(self, operation):
        try:
            with self._lock, file_lock(self.lock_path, timeout=self.timeout):
                state = self._read()
                if operation(state):
                    state['updated_at'] = datetime.now().astimezone().isoformat()
                    write_json_atomic(self.path, state)
                return state
        except (OSError, ValueError, RuntimeError, TypeError, KeyError) as exc:
            self.failure = 'storage_error'
            raise PoolStopped('storage_error', '模型池状态/锁操作失败：' + str(exc)) from exc

    def inspect(self):
        """Read-only: no lock creation, cleanup, or state mutation."""
        return self._read()

    def start(self):
        def cleanup(state):
            current = self._current()
            if current != self._snapshot:
                raise ValueError('启动期间模型配置发生变化，请重新启动')
            endpoint = normalize_endpoint(self._snapshot['endpoint'])
            names = {c['model'] for c in self._configs}
            rows = state['exhausted_models']
            remove = [k for k, v in rows.items() if v['endpoint'] == endpoint and v['model'] not in names]
            for key in remove:
                del rows[key]
            cooldowns = state.setdefault('cooldown_models', {})
            stale = [k for k, v in cooldowns.items()
                     if datetime.fromisoformat(v['cooldown_until']) <= self.now()
                     or (v['endpoint'] == endpoint and v['model'] not in names)]
            for key in stale:
                del cooldowns[key]
            incompatible = state.setdefault('incompatible_models', {})
            fingerprints = {state_key(c['endpoint'], c['model']): model_fingerprint(c) for c in self._configs}
            changed = [key for key, row in incompatible.items() if row['endpoint'] == endpoint
                       and row['config_fingerprint'] != fingerprints.get(key)]
            for key in changed:
                del incompatible[key]
            return bool(remove or stale or changed)
        self._transaction(cleanup)
        self.select()

    def select(self, excluded=()):
        if self.failure:
            raise PoolStopped(self.failure)
        state = self._transaction(lambda state: False)
        exhausted = set(state['exhausted_models']) | self._local_exhausted
        available = [c for c in self._configs if state_key(c['endpoint'], c['model']) not in exhausted]
        if not available:
            raise PoolStopped('models_exhausted', '所有模型已耗尽，请补充模型后恢复任务')
        incompatible = self._incompatible_keys(state)
        available = [c for c in available if state_key(c['endpoint'], c['model']) not in incompatible]
        if not available:
            raise PoolStopped('models_incompatible', '剩余模型均因参数不兼容被跳过，请调整模型配置后重跑；候选保留待处理')
        cooled = self._cooled_keys(state)
        available = [c for c in available if state_key(c['endpoint'], c['model']) not in cooled]
        if not available:
            raise PoolStopped('models_cooling_down', '可用模型均因空响应或服务异常暂时冷却，候选保留待处理，冷却结束后可重跑')
        for cfg in available:
            if cfg['model'] not in excluded:
                return deepcopy(cfg)
        raise PoolStopped('input_limit_mismatch', '未耗尽模型均无法接收完整材料或已被当前候选排除')

    def _cooled_keys(self, state):
        now = self.now()
        local_keys = {key for key, until in self._local_cooled.items() if until > now}
        state_keys = {key for key, row in state.get('cooldown_models', {}).items()
                      if datetime.fromisoformat(row['cooldown_until']) > now}
        return local_keys | state_keys

    def _incompatible_keys(self, state):
        rows = state.get('incompatible_models', {})
        return {key for cfg in self._configs
                for key, fingerprint in [(state_key(cfg['endpoint'], cfg['model']), model_fingerprint(cfg))]
                if (key, fingerprint) in self._local_incompatible
                or rows.get(key, {}).get('config_fingerprint') == fingerprint}

    def skip_incompatible(self, cfg, result):
        """Persist rejection for this configuration; parameter changes unblock it."""
        if (result.reason_code != REASON_MODEL_REQUEST_INCOMPATIBLE
                or result.http_status != 400 or result.billing_state != 'rejected_before_inference'
                or result.incompatible_parameter not in INCOMPATIBLE_PARAMETERS):
            raise ValueError('仅已确认的模型参数拒绝可以跳过模型')
        key = state_key(cfg['endpoint'], cfg['model'])
        original = next(c for c in self._configs if state_key(c['endpoint'], c['model']) == key)
        fingerprint = model_fingerprint(original)
        def merge(state):
            current = {state_key(c['endpoint'], c['model']): model_fingerprint(c)
                       for c in parse_model_configs(self._current())}
            rows = state.setdefault('incompatible_models', {})
            if current.get(key) != fingerprint or rows.get(key, {}).get('config_fingerprint') == fingerprint:
                return False
            rows[key] = {'endpoint': normalize_endpoint(cfg['endpoint']), 'model': cfg['model'],
                         'config_fingerprint': fingerprint, 'reason_code': REASON_MODEL_REQUEST_INCOMPATIBLE,
                         'http_status': 400, 'parameter': result.incompatible_parameter,
                         'rejected_at': self.now().isoformat()}
            return True
        self._transaction(merge)
        with self._lock:
            already_skipped = (key, fingerprint) in self._local_incompatible
            self._local_incompatible.add((key, fingerprint))
        if not already_skipped:
            self.log(f'模型参数不兼容，跳过并选择下一个：{cfg["model"]}（{result.incompatible_parameter}）')

    def cooldown(self, cfg, result, *, duration=None, reason_code=None):
        """Skip this model for the session and persist a cooldown retry window."""
        code = reason_code or getattr(result, 'reason_code', None) or 'MODEL_ERROR'
        if code not in ALLOWED_COOLDOWN_REASONS:
            raise ValueError(f'仅合法原因可以触发模型冷却：{code}')
        if duration is not None:
            cooldown_seconds = duration
        elif code == REASON_RESPONSE_EMPTY:
            cooldown_seconds = MODEL_COOLDOWN_SECONDS
        elif code == 'RATE_LIMIT':
            retry_after = getattr(result, 'retry_after', None)
            cooldown_seconds = max(5, min(int(retry_after), 3600)) if retry_after else TRANSIENT_COOLDOWN_SECONDS
        else:
            cooldown_seconds = TRANSIENT_COOLDOWN_SECONDS

        key = state_key(cfg['endpoint'], cfg['model'])
        now = self.now()
        until_dt = now + timedelta(seconds=cooldown_seconds)

        def merge(state):
            current = self._current()
            names = {state_key(c['endpoint'], c['model']) for c in parse_model_configs(current)}
            if key not in names:
                return False
            cooldowns = state.setdefault('cooldown_models', {})
            existing = cooldowns.get(key)
            if existing:
                try:
                    existing_until = datetime.fromisoformat(existing['cooldown_until'])
                    if existing_until >= until_dt:
                        return False
                except Exception:
                    pass
            cooldowns[key] = {
                'endpoint': normalize_endpoint(cfg['endpoint']), 'model': cfg['model'],
                'cooled_at': now.isoformat(),
                'cooldown_until': until_dt.isoformat(),
                'reason_code': code, 'http_status': getattr(result, 'http_status', None)}
            return True
        self._transaction(merge)
        with self._lock:
            self._local_cooled[key] = until_dt
        if code == REASON_RESPONSE_EMPTY:
            self.log('模型空响应达到尝试上限，本轮跳过并冷却 30 分钟：' + cfg['model'])
        elif code == 'RATE_LIMIT':
            self.log(f'模型触发限流，本轮跳过并冷却 {int(cooldown_seconds)} 秒：' + cfg['model'])
        else:
            self.log(f'模型服务/网络异常达到尝试上限，本轮跳过并冷却 {int(cooldown_seconds // 60)} 分钟：{cfg["model"]}（{code}）')

    def exhaust(self, cfg, result):
        key = state_key(cfg['endpoint'], cfg['model'])
        def merge(state):
            current = self._current()
            names = {state_key(c['endpoint'], c['model']) for c in parse_model_configs(current)}
            if key not in names or key in state['exhausted_models']:
                return False
            state['exhausted_models'][key] = {
                'endpoint': normalize_endpoint(cfg['endpoint']), 'model': cfg['model'],
                'exhausted_at': datetime.now().astimezone().isoformat(),
                'error_code': QUOTA_CODE, 'http_status': result.http_status}
            return True
        self._transaction(merge)
        with self._lock:
            self._local_exhausted.add(key)
        self.log('模型已耗尽：' + cfg['model'])

    def model_available(self, model):
        if self.failure:
            raise PoolStopped(self.failure)
        state = self._transaction(lambda state: False)
        exhausted = set(state['exhausted_models']) | self._local_exhausted
        exhausted |= self._cooled_keys(state)
        exhausted |= self._incompatible_keys(state)
        return any(c['model'] == model and state_key(c['endpoint'], model) not in exhausted
                   for c in self._configs)

    def run(self, system, user, stage, invoke, *, stage_limit=None, max_attempts=None, sleep=lambda seconds: None,
            validate_result=None, max_models_per_candidate=3, on_request_update=None):
        """invoke owns before/after persistence and budget; one invocation = one HTTP attempt."""
        excluded, attempts, selected, tries, empty_tries = set(), [], None, 0, 0
        switch_reason = None
        logical_call_id = uuid4().hex
        attempted_inference_models = 0
        attempted_model_keys = set()
        while True:
            try:
                cfg = self.select(excluded)
            except PoolStopped as exc:
                if exc.reason == 'input_limit_mismatch':
                    # Keep the actual failure when requests were made. A later
                    # capacity skip or pre-inference rejection must not replace
                    # a format/truncation failure with an input-limit diagnosis.
                    for previous in reversed(attempts):
                        if previous.billing_state != 'rejected_before_inference':
                            return previous, attempts
                    failed_res = ModelCallResult(
                        ok=False,
                        reason_code='CANDIDATE_NO_CAPABLE_MODEL',
                        error='当前候选超出所有可用模型输入上限',
                        requested_model=selected,
                    )
                    return failed_res, attempts
                raise

            if len((system + user).encode('utf-8')) > cfg.get('limits', {}).get('max_input_bytes', 262144):
                excluded.add(cfg['model'])
                self.log('输入上限不足，本次跳过：' + cfg['model'])
                continue

            if cfg['model'] != selected:
                switch_reason = ({
                    'QUOTA_EXHAUSTED': 'quota_exhausted',
                    REASON_RESPONSE_EMPTY: 'response_empty',
                    REASON_MODEL_REQUEST_INCOMPATIBLE: 'request_incompatible',
                    'RATE_LIMIT': 'rate_limit',
                    'NETWORK_ERROR': 'network_error',
                    'TIMEOUT': 'timeout',
                    'SERVER_ERROR': 'server_error',
                    'OUTPUT_FORMAT_INVALID': 'format_invalid',
                    'LENGTH_EXCEEDED': 'length_exceeded',
                }.get(attempts[-1].reason_code) if attempts else None)
                selected, tries, empty_tries = cfg['model'], 0, 0
                self.log('选择模型：' + selected)

            limit = cfg.get('request', {}).get('max_attempts') or max_attempts or 2
            if stage_limit is not None:
                cfg.setdefault('limits', {})['max_output_tokens'] = min(stage_limit, cfg.get('limits', {}).get('max_output_tokens', 4000))
            fmt = resolve_response_format(cfg, stage)
            cfg.setdefault('request', {})['response_format'] = deepcopy(fmt)
            fingerprint = model_fingerprint(cfg)
            cfg.setdefault('request', {})['max_attempts'] = 1
            context = {'request_id': uuid4().hex, 'requested_model': selected,
                       'logical_call_id': logical_call_id,
                       'model_attempt': tries + 1, 'model_max_attempts': limit,
                       'model_config_fingerprint': fingerprint, 'stage': stage,
                       'switch_reason': switch_reason}
            try:
                result = invoke(cfg, fmt, context)
            except PoolReselect:
                continue
            attempts.append(result)
            tries += 1
            switch_reason = None

            is_pre_inference_rejection = getattr(result, 'billing_state', None) == 'rejected_before_inference'
            if not is_pre_inference_rejection and cfg['model'] not in attempted_model_keys:
                attempted_model_keys.add(cfg['model'])
                attempted_inference_models += 1

            if result.reason_code == 'QUOTA_EXHAUSTED':
                self.exhaust(cfg, result)
                excluded.add(cfg['model'])
                continue
            if result.reason_code == 'QUOTA_RESPONSE_CONFLICT':
                raise PoolStopped('quota_response_conflict', '额度拒绝与实际用量冲突，已记账并停止')
            if result.reason_code == 'ACCOUNT_ERROR':
                return result, attempts
            if result.reason_code == REASON_MODEL_REQUEST_INCOMPATIBLE:
                if result.billing_state != 'rejected_before_inference':
                    raise PoolStopped('usage_unknown', '模型参数拒绝用量未确认，停止自动轮换')
                self.skip_incompatible(cfg, result)
                excluded.add(cfg['model'])
                continue
            if result.reason_code == REASON_RESPONSE_EMPTY:
                if UsageTotals().add(result)['total_tokens'] is None:
                    raise PoolStopped('usage_unknown', '空响应用量未知，停止自动重试')
                empty_tries += 1
                if empty_tries < EMPTY_RESPONSE_MAX_ATTEMPTS and tries < limit:
                    self.log(f'模型返回空正文，有限重试 {empty_tries}/{EMPTY_RESPONSE_MAX_ATTEMPTS}：' + selected)
                    sleep(min(2 ** (tries - 1), 8))
                    continue
                self.cooldown(cfg, result)
                excluded.add(cfg['model'])
                if attempted_inference_models >= max_models_per_candidate:
                    return result, attempts
                continue
            if (result.reason_code in ('NETWORK_ERROR', 'TIMEOUT', 'SERVER_ERROR', 'RATE_LIMIT')
                    or (result.http_status is not None and result.http_status in (408, 429, 500, 502, 503, 504))):
                is_rate_limit = (result.reason_code == 'RATE_LIMIT' or result.http_status == 429)
                retry_after = getattr(result, 'retry_after', None)
                if is_rate_limit and retry_after is not None and retry_after > 10:
                    self.cooldown(cfg, result, duration=retry_after)
                    excluded.add(cfg['model'])
                    if attempted_inference_models >= max_models_per_candidate:
                        return result, attempts
                    continue

                if tries < limit:
                    delay = retry_after if (is_rate_limit and retry_after is not None) else min(2 ** (tries - 1), 8)
                    self.log(f'模型请求临时异常（{result.reason_code or result.http_status}），重试 {tries}/{limit}：{selected}')
                    sleep(delay)
                    continue
                self.cooldown(cfg, result)
                excluded.add(cfg['model'])
                if attempted_inference_models >= max_models_per_candidate:
                    return result, attempts
                continue
            if result.reason_code == REASON_LENGTH_EXCEEDED:
                excluded.add(cfg['model'])
                self.log(f'模型输出被截断，排除该模型并切换：{cfg["model"]}')
                if attempted_inference_models >= max_models_per_candidate:
                    return result, attempts
                continue

            if result.ok:
                if validate_result is not None:
                    valid, parsed_data, err_detail = validate_result(result, cfg)
                    if not valid:
                        result.ok = False
                        result.reason_code = 'OUTPUT_FORMAT_INVALID'
                        result.error = err_detail.get('error') if isinstance(err_detail, dict) else str(err_detail or '模型输出格式或结构不合法')
                        result.error_kind = err_detail.get('error_kind') if isinstance(err_detail, dict) else 'OUTPUT_FORMAT_INVALID'
                        result.parsed_data = None
                        excluded.add(cfg['model'])
                        if on_request_update is not None:
                            on_request_update(result)
                        self.log(f'模型输出校验未通过，排除该模型并切换：{cfg["model"]}（{result.error}）')
                        if attempted_inference_models >= max_models_per_candidate:
                            return result, attempts
                        continue
                    else:
                        result.parsed_data = parsed_data
                return result, attempts

            return result, attempts
