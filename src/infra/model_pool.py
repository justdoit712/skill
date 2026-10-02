"""Process-safe exhaustion/cooldown state and individually accounted rotation."""
from copy import deepcopy
from datetime import datetime, timedelta
import json
from pathlib import Path
import threading
from uuid import uuid4

from src.infra.files import file_lock, write_json_atomic
from src.infra.model_config import load_model_config
from src.shared.model_config import parse_model_configs, state_key, normalize_endpoint, model_fingerprint
from src.shared.output_contracts import resolve_response_format
from src.shared.usage import UsageTotals
from src.infra.llm import REASON_RESPONSE_EMPTY

QUOTA_CODE = 'AllocationQuota.FreeTierOnly'
EMPTY_RESPONSE_MAX_ATTEMPTS = 2
MODEL_COOLDOWN_SECONDS = 30 * 60


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
        self._local_cooled = set()
        self.now = now or (lambda: datetime.now().astimezone())
        self.authoritative = authoritative
        self.log = log
        self.timeout = 5
        self.failure = None

    def _read(self):
        if not self.path.exists():
            return {'state_version': '1.0.0', 'updated_at': datetime.now().astimezone().isoformat(), 'exhausted_models': {}}
        state = json.loads(self.path.read_text(encoding='utf-8'))
        if (not isinstance(state, dict) or state.get('state_version') != '1.0.0'
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
                    or row['model'] != row['model'].strip() or row.get('reason_code') != REASON_RESPONSE_EMPTY
                    or type(row.get('http_status')) is not int or not 200 <= row['http_status'] < 300
                    or normalize_endpoint(row.get('endpoint')) != row.get('endpoint')
                    or state_key(row['endpoint'], row['model']) != key):
                raise ValueError('模型冷却记录身份或字段不合法')
            started = datetime.fromisoformat(row['cooled_at'])
            until = datetime.fromisoformat(row['cooldown_until'])
            if started.tzinfo is None or until.tzinfo is None or until <= started:
                raise ValueError('模型冷却时间不合法')
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
            return bool(remove or stale)
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
        cooled = self._cooled_keys(state)
        available = [c for c in available if state_key(c['endpoint'], c['model']) not in cooled]
        if not available:
            raise PoolStopped('models_cooling_down', '可用模型均因空响应暂时冷却，候选保留待处理，冷却结束后可重跑')
        for cfg in available:
            if cfg['model'] not in excluded:
                return deepcopy(cfg)
        raise PoolStopped('input_limit_mismatch', '未耗尽模型均无法接收完整材料')

    def _cooled_keys(self, state):
        return self._local_cooled | {key for key, row in state.get('cooldown_models', {}).items()
                                    if datetime.fromisoformat(row['cooldown_until']) > self.now()}

    def cooldown(self, cfg, result):
        """Skip this model for the session and persist a 30-minute retry window."""
        if result.reason_code != REASON_RESPONSE_EMPTY or not 200 <= (result.http_status or 0) < 300:
            raise ValueError('仅空响应可以触发模型冷却')
        key = state_key(cfg['endpoint'], cfg['model'])
        def merge(state):
            current = self._current()
            names = {state_key(c['endpoint'], c['model']) for c in parse_model_configs(current)}
            if key not in names or key in self._cooled_keys(state):
                return False
            now = self.now()
            state.setdefault('cooldown_models', {})[key] = {
                'endpoint': normalize_endpoint(cfg['endpoint']), 'model': cfg['model'],
                'cooled_at': now.isoformat(),
                'cooldown_until': (now + timedelta(seconds=MODEL_COOLDOWN_SECONDS)).isoformat(),
                'reason_code': REASON_RESPONSE_EMPTY, 'http_status': result.http_status}
            return True
        self._transaction(merge)
        with self._lock:
            self._local_cooled.add(key)
        self.log('模型空响应达到尝试上限，本轮跳过并冷却 30 分钟：' + cfg['model'])

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
        return any(c['model'] == model and state_key(c['endpoint'], model) not in exhausted
                   for c in self._configs)

    def run(self, system, user, stage, invoke, *, stage_limit=None, max_attempts=None, sleep=lambda seconds: None):
        """invoke owns before/after persistence and budget; one invocation = one HTTP attempt."""
        excluded, attempts, selected, tries, empty_tries = set(), [], None, 0, 0
        switch_reason = None
        logical_call_id = uuid4().hex
        while True:
            cfg = self.select(excluded)
            if len((system + user).encode('utf-8')) > cfg.get('limits', {}).get('max_input_bytes', 262144):
                excluded.add(cfg['model'])
                self.log('输入上限不足，本次跳过：' + cfg['model'])
                continue
            if cfg['model'] != selected:
                switch_reason = ({'QUOTA_EXHAUSTED': 'quota_exhausted', REASON_RESPONSE_EMPTY: 'response_empty'}
                                 .get(attempts[-1].reason_code) if attempts else None)
                selected, tries, empty_tries = cfg['model'], 0, 0
                self.log('选择模型：' + selected)
            limit = max_attempts if max_attempts is not None else cfg.get('request', {}).get('max_attempts', 2)
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
            if result.reason_code == 'QUOTA_EXHAUSTED':
                self.exhaust(cfg, result)
                continue
            if result.reason_code == 'QUOTA_RESPONSE_CONFLICT':
                raise PoolStopped('quota_response_conflict', '额度拒绝与实际用量冲突，已记账并停止')
            if result.reason_code == 'ACCOUNT_ERROR':
                return result, attempts
            if result.reason_code == REASON_RESPONSE_EMPTY:
                if UsageTotals().add(result)['total_tokens'] is None:
                    raise PoolStopped('usage_unknown', '空响应用量未知，停止自动重试')
                empty_tries += 1
                if empty_tries < EMPTY_RESPONSE_MAX_ATTEMPTS and tries < limit:
                    self.log(f'模型返回空正文，有限重试 {empty_tries}/{EMPTY_RESPONSE_MAX_ATTEMPTS}：' + selected)
                    sleep(min(2 ** (tries - 1), 8))
                else:
                    self.cooldown(cfg, result)
                continue
            if (result.reason_code == 'NETWORK_ERROR' or result.http_status in (408, 429, 500, 502, 503, 504)) and tries < limit:
                sleep(min(2 ** (tries - 1), 8))
                continue
            return result, attempts
