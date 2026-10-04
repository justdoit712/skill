"""单候选评估与受控处理模块。

负责单候选预筛、材料抓取、规范化/精确缓存判定、受控模型评估与结果分类。
"""

from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict
import json
from pathlib import Path
from typing import Any

from src.infra.http import fetch_text
from src.infra.llm import REASON_LENGTH_EXCEEDED
from src.shared.materials import primary_material_bundle, validate_document
from src.shared.model_config import material_fetch_limit
from src.shared.normalization import (
    NORMALIZATION_VERSION,
    create_reuse_audit,
    inspect_record_for_normalized_reuse,
    normalized_content_fingerprint,
)
from src.shared.owned import is_skill_owned
from src.shared.runtime import now_local
from src.shared.usage import recompute_usage_from_calls
from src.shared.versions import LLM_OUTPUT_CONTRACT_VERSION
from .budget import evaluation_filename
from .decide import decide
from .dedupe import content_fingerprint
from .entry_state import (
    EntryUpdateEvent,
    STATUS_RECOMMENDED,
)
from .evaluation import (
    RETRYABLE_STATUS,
    build_prompt,
    evaluation_id,
    resolve_api_key,
    validate_pending_evaluation,
)
from .failure_policy import (
    STOP_ACCESS_DENIED,
    STOP_EVALUATION_LIMIT,
    STOP_FORMAT_FAILURES,
    STOP_MODEL_FAILURES,
    STOP_REQUEST_CONFIG_ERROR,
    STOP_RESUME_STATE_INVALID,
    STOP_TARGET_REACHED,
    STOP_TOKEN_LIMIT,
    STOP_USAGE_UNKNOWN,
    classify_result,
    resolve_primary_stop_reason,
    update_failure_counters,
)
from .filter_rules import eligible_for_topic_filter, filter_new_evaluation
from .local_state import (
    LocalCollection,
    _read,
    _record_static_observation,
    _unknown_usage_reserve,
)
from .pool import (
    STATUS_BLOCKED,
    STATUS_DONE,
    STATUS_EXCLUDED as POOL_STATUS_EXCLUDED,
    STATUS_FETCH_FAILED,
    STATUS_LENGTH_EXCEEDED,
    STATUS_NOT_SKILL,
    STATUS_STATIC_SKIPPED,
    save_pool,
    update_candidate_status,
)
from .prescreen import prescreen, should_static_skip


def _retryable(result: dict) -> bool:
    """判定模型响应是否属于可自动重试的网络或 HTTP 临时故障。"""
    return not result["ok"] and (
        result.get("reason_code") == "NETWORK_ERROR"
        or getattr(result.get("call"), "http_status", None) in RETRYABLE_STATUS
    )


def _topic_evaluation_options(state: LocalCollection, candidate: Any) -> dict[str, Any]:
    """生成适用主题过滤的附加配置选项。"""
    filter_rules = state.cfg.get("filter_rules")
    if eligible_for_topic_filter(
        filter_rules, state.entries.get(candidate.skill_id),
        previously_evaluated=candidate.skill_id in state.evaluated_skill_ids,
    ):
        return {"filter_rules": filter_rules}
    return {}


def _record_request_usage(state: LocalCollection, call: Any, *, retryable: bool) -> None:
    """真实回调和单次适配器共用记账与未知用量策略。"""
    unknown_before = state.usage.unknown_usage_requests
    state.active_call['usage'] = state.usage.add(call)
    reserved = (state.usage.unknown_usage_requests - unknown_before) * state.unknown_reserve
    state.report['unknown_usage_reserved_tokens'] += reserved
    state.active_call['unknown_usage_reserved_tokens'] = reserved
    state.active_call['diagnostics'] = {
        'error_type': getattr(call, 'error_type', None),
        'http_status': getattr(call, 'http_status', None),
        'latency_ms': getattr(call, 'latency_ms', None),
    }
    if (state.active_call['usage']['total_tokens'] is None
            and (not retryable or getattr(call, 'reason_code', None) == 'RESPONSE_EMPTY')
            and getattr(call, 'billing_state', None) != 'rejected_before_inference'):
        state.stop_causes.add(STOP_USAGE_UNKNOWN)
        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)


def find_normalized_candidate_record(
    state: LocalCollection,
    candidate: Any,
    text: str,
) -> tuple[dict | None, str | None]:
    """在账本与历史评估中查找符合受限规范化复用条件的候选记录。"""
    if 'models' in state.cfg['model']:
        return None, 'pool_cache_requires_explicit_model'
    current_norm_fp = getattr(candidate, "normalized_content_fingerprint", None) or normalized_content_fingerprint(text)
    if not current_norm_fp:
        return None, "no_current_fingerprint"

    if not hasattr(state, "_skill_eval_records_index") or state._skill_eval_records_index is None:
        index: dict[str, list[dict]] = {}
        eval_dirs = []
        if hasattr(state, "ledger") and hasattr(state.ledger, "evaluations_dir"):
            eval_dirs.append(state.ledger.evaluations_dir)
        root_eval_dir = state.root / "data" / "state" / "evaluations"
        if root_eval_dir.exists() and root_eval_dir not in eval_dirs:
            eval_dirs.append(root_eval_dir)
        for ed in eval_dirs:
            if ed.exists():
                for p in ed.glob("*.json"):
                    try:
                        rec = json.loads(p.read_text(encoding="utf-8"))
                        sid = rec.get("skill_id")
                        if sid:
                            index.setdefault(sid, []).append(rec)
                    except Exception:
                        continue
        state._skill_eval_records_index = index

    candidate_records = list(state._skill_eval_records_index.get(candidate.skill_id, []))
    if not candidate_records:
        return None, "no_history_records"

    expected_rules_v = str(state.cfg.get("rules", {}).get("rules_version") or "")
    expected_model_cfg_v = str(state.cfg.get("model", {}).get("model_config_version") or "")

    last_rejection_reason = "no_matching_candidate"
    for rec in candidate_records:
        rec_norm_fp = rec.get("normalized_content_fingerprint")
        if not rec_norm_fp:
            docs = (rec.get("outcome") or {}).get("materials", {}).get("documents") or []
            if docs and isinstance(docs, list) and isinstance(docs[0], dict):
                rec_norm_fp = docs[0].get("normalized_fingerprint")

        if not rec_norm_fp or rec_norm_fp != current_norm_fp:
            continue

        ok, reason = inspect_record_for_normalized_reuse(
            rec,
            text,
            expected_rules_version=expected_rules_v,
            expected_model_config_version=expected_model_cfg_v,
        )
        if ok:
            return rec, None
        else:
            last_rejection_reason = reason

    return None, last_rejection_reason


def _update_blocked(pool: Any, seq: int, block_info: dict) -> None:
    """把候选池条目推进为 blocked 状态并记录阻止元数据。"""
    item = next((it for it in pool.items if it.seq == seq), None)
    if item is not None:
        item.block_info = block_info
    update_candidate_status(pool, seq, STATUS_BLOCKED)


def _record_blocked_candidate(state: LocalCollection, seq: int, block_info: dict, *, reconciled: bool = False) -> None:
    """保存阻止状态，区分历史记录对账与本轮新失败。"""
    _update_blocked(state.pool, seq, block_info)
    save_pool(state.pool_path, state.pool)
    state.report['blocked_records'] += 1
    state.report['reconciled_blocked' if reconciled else 'blocked_new'] += 1


def _build_block_info(
    eid: str,
    reason: str,
    *,
    decision: Any = None,
    result: dict | None = None,
    stage: str | None = None,
    http_status: Any = None,
    error_detail: str | None = None,
    model: str | None = None,
    model_config_version: str | None = None,
) -> dict:
    """构建统一格式的候选阻止元数据。"""
    info = {
        'evaluation_id': eid,
        'reason': reason,
        'reason_code': getattr(decision, 'reason_code', None) or (result or {}).get('reason_code'),
        'error_kind': getattr(decision, 'error_kind', None) or (result or {}).get('error_kind'),
        'stage': stage or getattr(decision, 'stage', None) or (result or {}).get('stage'),
        'http_status': http_status if http_status is not None else getattr(decision, 'http_status', None),
        'blocked_at': now_local().isoformat(),
        'source': 'local_ledger',
        'model': model,
        'model_config_version': model_config_version,
    }
    if error_detail:
        info['error_detail'] = error_detail
    return info


def _evaluate_with_pool(state: LocalCollection, candidate: Any, text: str, eid: str, record: dict) -> dict:
    """使用百炼多模型队列受控调度评估单个候选。"""
    from src.infra.model_pool import PoolStopped, PoolReselect
    state.active_eid = eid
    observed = []

    def on_request(event, stage, request_call):
        if event == 'before':
            if state.report.get('stop_reason'):
                raise PoolStopped(state.report['stop_reason'])
            if not state.model_pool.model_available(request_call['requested_model']):
                raise PoolReselect()
            reserve = request_call['reserved_tokens']
            others = sum(c.get('reserved_tokens', 0) for c in state.report['calls']
                         if c.get('reservation_state') == 'active')
            if state.report['budget_tokens'] + others + reserve > state.settings['max_total_tokens']:
                raise PoolStopped('token_limit')
            state.unknown_reserve = reserve
            state.active_call = {
                'skill_id': candidate.skill_id, 'logical_task_id': eid,
                'state': 'started', 'status': 'in_progress', 'usage': None,
                'reservation_state': 'active', **request_call,
            }
            state.report['calls'].append(state.active_call)
            checkpoint = state.ledger.get(eid)
            checkpoint['status'] = 'in_progress'
            checkpoint.setdefault('requests', []).append(dict(state.active_call))
            try:
                state.ledger.save_record(eid, checkpoint)
                state.save()
            except OSError as exc:
                state.active_call.update(state='not_sent', status='not_sent', reservation_state='released')
                state.active_eid = None
                raise PoolStopped('storage_error', str(exc)) from exc
            return True
        decision = classify_result({'ok': request_call.ok, 'call': request_call})
        _record_request_usage(state, request_call, retryable=decision.retryable)
        call = state.active_call
        rejected = getattr(request_call, 'billing_state', None) == 'rejected_before_inference'
        if not rejected and not request_call.ok:
            state.report['failed_requests'] += 1
        call.update(
            state='received' if request_call.ok else 'error',
            status='completed' if request_call.ok else 'failed',
            reservation_state='settled' if rejected or call['usage']['total_tokens'] is not None else 'unknown',
            billing_state=getattr(request_call, 'billing_state', None),
            raw_usage=getattr(request_call, 'usage', None),
            provider_error_code=getattr(request_call, 'provider_error_code', None),
            returned_model=getattr(request_call, 'returned_model', None),
            response={k: getattr(request_call, k, None) for k in
                      ('ok', 'content', 'reason_code', 'http_status', 'finish_reason', 'error')},
        )
        used = state.report.setdefault('models_used', [])
        if request_call.attempts and request_call.requested_model not in used:
            used.append(request_call.requested_model)
        checkpoint = state.ledger.get(eid)
        checkpoint['requests'][-1] = dict(call)
        checkpoint['status'] = 'reserved' if rejected else 'in_progress'
        checkpoint['last_request_model'] = request_call.requested_model
        observed.append(request_call)
        try:
            state.ledger.save_record(eid, checkpoint)
            state.save()
        except OSError as exc:
            state.model_pool.failure = 'storage_error'
            raise PoolStopped('storage_error', str(exc)) from exc

    result = state.evaluate_fn(
        candidate, text, model_cfg=state.cfg['model'],
        rules=state.cfg['rules'], taxonomy=state.cfg['taxonomy'], sleep=state.sleep,
        on_request=on_request, pending_evaluation=record.get('pending_evaluation'),
        model_pool=state.model_pool, pool_max_attempts=state.max_attempts,
        **_topic_evaluation_options(state, candidate),
    )
    checkpoint = state.ledger.get(eid)
    if result.get('pool_stop'):
        if all(getattr(c, 'billing_state', None) == 'rejected_before_inference' for c in observed):
            state.report['evaluations'] -= 1
        requests = checkpoint.get('requests', [])
        if state.active_call and state.active_call.get('state') == 'not_sent' and requests:
            requests[-1] = dict(state.active_call)
        unsettled = any(c.get('state') in ('started', 'unknown') for c in requests)
        unsafe_storage = result['pool_stop'] == 'storage_error' and any(
            getattr(c, 'billing_state', None) != 'rejected_before_inference' for c in observed
        )
        own_unknown_usage = recompute_usage_from_calls(requests).unknown_usage_requests
        checkpoint['status'] = 'needs_recovery' if (own_unknown_usage or unsettled or unsafe_storage
                                                    or result['pool_stop'] == 'quota_response_conflict') else 'reserved'
        if state.usage.unknown_usage_requests:
            state.stop_causes.add(STOP_USAGE_UNKNOWN)
        checkpoint['pause_reason'] = result['pool_stop']
        state.stop_causes.add(result['pool_stop'])
        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
    else:
        checkpoint['attempts'] = int(checkpoint.get('attempts') or 0) + 1
        if not result['ok']:
            checkpoint['retry_exhausted'] = classify_result(result).retryable
            checkpoint.update(status='failed', error={'reason_code': result.get('reason_code'),
                                                      'message': result.get('error')})
    state.ledger.save_record(eid, checkpoint)
    state.active_eid = None
    return result


def _evaluate_with_retries(state: LocalCollection, candidate: Any, text: str, eid: str, record: dict) -> dict | None:
    """单模型评估重试循环，处理超时、空响应冷却与阶段记账。"""
    from src.infra.model_pool import EMPTY_RESPONSE_MAX_ATTEMPTS
    if getattr(state, "model_pool", None) is not None:
        return _evaluate_with_pool(state, candidate, text, eid, record)
    resume_error = validate_pending_evaluation(
        candidate, text, state.cfg['rules'],
        state.cfg['taxonomy'], record.get('pending_evaluation'),
    )
    if resume_error:
        state.ledger.fail(eid, resume_error['reason_code'], resume_error['error'])
        checkpoint = state.ledger.get(eid)
        checkpoint.update(error_kind=resume_error['error_kind'], stage='resume', retryable=False)
        state.ledger.save_record(eid, checkpoint)
        return resume_error
    result = None
    empty_attempts = 0
    attempt_limit = int(record.get('max_attempts') or state.max_attempts)
    initial_attempts = int(record.get('attempts') or 0)
    for index in range(initial_attempts, attempt_limit):
        if hasattr(state, 'wait_for_budget') and not state.wait_for_budget():
            break
        if state.report['budget_tokens'] >= state.settings['max_total_tokens']:
            state.stop_causes.add(STOP_TOKEN_LIMIT)
            state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
            break
        if index and (not record.get('pending_evaluation') or index > initial_attempts):
            delay = min(2 ** (index - 1), 8)
            state.log(f'重连 {index}/{state.max_retries}：{candidate.name}，{delay} 秒后重试。')
            state.sleep(delay)
        attempt = state.ledger.begin_attempt(eid)
        state.active_eid = eid
        state.active_call = {
            'skill_id': candidate.skill_id, 'attempt': attempt,
            'max_attempts': attempt_limit, 'status': 'in_progress', 'usage': None,
        }
        state.report['calls'].append(state.active_call)
        state.save()
        observed = []

        def on_request(event, stage, request_call):
            if event == 'before':
                if stage == 'review':
                    if state.report.get('stop_reason'):
                        return False
                    if state.report['budget_tokens'] >= state.settings['max_total_tokens']:
                        state.stop_causes.add(STOP_TOKEN_LIMIT)
                        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
                        return False
                    if observed:
                        state.active_call = {
                            'skill_id': candidate.skill_id, 'attempt': attempt,
                            'max_attempts': attempt_limit, 'status': 'in_progress', 'usage': None,
                        }
                        state.report['calls'].append(state.active_call)
                state.active_call['stage'] = stage
                checkpoint = state.ledger.get(eid)
                checkpoint.setdefault('requests', []).append(dict(state.active_call))
                state.ledger.save_record(eid, checkpoint)
                state.save()
                return True
            request_decision = classify_result({
                'ok': request_call.ok, 'call': request_call,
                'reason_code': request_call.reason_code,
            })
            _record_request_usage(state, request_call, retryable=request_decision.retryable)
            state.active_call['status'] = 'completed' if request_call.ok else 'failed'
            if getattr(request_call, 'reason_code', None) == REASON_LENGTH_EXCEEDED:
                state.active_call.update(status=STATUS_LENGTH_EXCEEDED,
                                         reason_code=REASON_LENGTH_EXCEEDED, is_sample_error=True)
            checkpoint = state.ledger.get(eid)
            checkpoint['requests'][-1] = dict(state.active_call)
            state.ledger.save_record(eid, checkpoint)
            observed.append(request_call)
            state.save()

        result = state.evaluate_fn(
            candidate, text, model_cfg=state.cfg['model'], rules=state.cfg['rules'],
            taxonomy=state.cfg['taxonomy'], sleep=state.sleep, on_request=on_request,
            pending_evaluation=record.get('pending_evaluation'),
            **_topic_evaluation_options(state, candidate),
        )
        call = result.get('call')
        key = resolve_api_key(state.cfg['model'])
        if key and result.get('error'):
            result['error'] = str(result['error']).replace(key, '[REDACTED]')
        decision = classify_result(result)
        if not observed and call is not None:
            _record_request_usage(state, call, retryable=decision.retryable)
        if state.active_call:
            state.active_call['stage'] = result.get('stage') or state.active_call.get('stage')
            state.active_call['status'] = (
                STATUS_LENGTH_EXCEEDED if decision.is_length_exceeded
                else 'completed' if result['ok'] else 'failed'
            )
            if decision.is_length_exceeded:
                state.active_call['reason_code'] = REASON_LENGTH_EXCEEDED
                state.active_call['is_sample_error'] = True
            elif decision.is_format_error:
                state.active_call['reason_code'] = decision.reason_code
                state.active_call['error_kind'] = decision.error_kind
                state.active_call['is_sample_error'] = True
            elif not result['ok']:
                state.active_call['reason_code'] = decision.reason_code
                state.active_call['error_kind'] = decision.error_kind
        state.save()
        if result.get('pending_evaluation'):
            checkpoint = state.ledger.get(eid)
            checkpoint.update(
                status='reserved', pending_evaluation=result['pending_evaluation'],
                max_attempts=(attempt_limit if checkpoint.get('resume_history') else
                              max(int(checkpoint.get('max_attempts') or 0), state.max_attempts + 1)),
            )
            state.ledger.save_record(eid, checkpoint)
            if state.active_call:
                state.active_call['status'] = 'completed'
            state.active_eid = None
            break
        if result['ok']:
            break
        code = result.get('reason_code') or decision.reason_code or 'MODEL_ERROR'
        if code == 'RESPONSE_EMPTY':
            empty_attempts += 1
        diagnostic = state.active_call.get('diagnostics', {}) if state.active_call else {}
        details = [code]
        if result.get('error_kind'):
            details.append(result['error_kind'])
        if result.get('error'):
            err_str = str(result['error']).strip().replace('\r', '').replace('\n', ' ')
            if len(err_str) > 160:
                err_str = err_str[:157] + '...'
            details.append(err_str)
        if diagnostic.get('error_type'):
            details.append(diagnostic['error_type'])
        if diagnostic.get('http_status') is not None:
            details.append(f"HTTP {diagnostic['http_status']}")
        if diagnostic.get('latency_ms') is not None:
            details.append(f"耗时 {diagnostic['latency_ms'] / 1000:.1f} 秒")
        message = '；'.join(details)
        state.ledger.fail(eid, code, message)
        failure_record = state.ledger.get(eid)
        failure_record['retryable'] = decision.retryable
        if result.get('error_kind'):
            failure_record['error_kind'] = result.get('error_kind')
        if result.get('stage'):
            failure_record['stage'] = result.get('stage')
        if result.get('error'):
            failure_record['error_detail'] = str(result.get('error'))
        state.ledger.save_record(eid, failure_record)
        state.active_eid = None
        if state.active_call:
            state.active_call['reason_code'] = code
        if decision.is_length_exceeded:
            if state.active_call:
                state.active_call['status'] = STATUS_LENGTH_EXCEEDED
                state.active_call['is_sample_error'] = True
            state.save()
            break
        if decision.is_format_error:
            state.save()
            break
        if decision.stop_cause in (STOP_ACCESS_DENIED, STOP_RESUME_STATE_INVALID, STOP_REQUEST_CONFIG_ERROR):
            state.stop_causes.add(decision.stop_cause)
            state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
            state.save()
            break
        state.report['failed_requests'] += 1
        state.save()
        state.log(f'请求失败：{candidate.name}；{message}。')
        if empty_attempts >= EMPTY_RESPONSE_MAX_ATTEMPTS:
            break
        if not decision.retryable or state.report['stop_reason']:
            break
    return result


def process_candidate(state: LocalCollection, item: Any) -> bool:
    """按标准 6 步受控流程评估单个候选条目。"""
    candidate = item.candidate
    seq = item.seq

    # 1. 资格与停止边界检查
    if is_skill_owned(candidate.skill_id, state.owned_ids):
        state.skipped_owned_ids.add(candidate.skill_id)
        state.report["skipped_owned"] = len(state.skipped_owned_ids)
        return True
    if candidate.skill_id in state.active_snoozed or candidate.skill_id in state.manual_exclusions:
        return True
    if state.report.get('stop_reason'):
        return False
    if state.report['new_recommended'] >= state.settings['target_recommended']:
        state.stop_causes.add(STOP_TARGET_REACHED)
        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
        return False
    if state.report['budget_tokens'] >= state.settings['max_total_tokens']:
        state.stop_causes.add(STOP_TOKEN_LIMIT)
        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
        return False
    if state.settings.get('max_evaluations') and state.report['evaluations'] >= state.settings['max_evaluations']:
        state.stop_causes.add(STOP_EVALUATION_LIMIT)
        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
        return False

    state.report['checked'] += 1

    # 2. 材料存在性与抓取前预筛
    if candidate.path.split('/')[-1] != 'SKILL.md':
        state.report['not_skill_files'] += 1
        update_candidate_status(state.pool, seq, STATUS_NOT_SKILL)
        save_pool(state.pool_path, state.pool)
        return True

    pres = prescreen(candidate, state.cfg['prescreen'], None)
    if pres.excluded:
        _record_static_observation(state.report, pres.static_observation)
        state.report['prescreen_excluded'] += 1
        state.publish(candidate, pres)
        update_candidate_status(state.pool, seq, POOL_STATUS_EXCLUDED)
        save_pool(state.pool_path, state.pool)
        state.save()
        return True

    state.log(f"检查 #{seq}（本轮进度 {state.report['checked']}/{len(state.pending_items)}，全池 {len(state.pool)}）：{candidate.skill_id}")

    # 3. 内容抓取与校验
    if '/blob/' not in candidate.url:
        candidate.url = f'https://github.com/{candidate.owner}/{candidate.repo}/blob/HEAD/{candidate.path}'
    if hasattr(state, 'batch_materials') and candidate.skill_id in state.batch_materials:
        fetched = state.batch_materials[candidate.skill_id]
    else:
        url = candidate.url.replace('https://github.com/', 'https://raw.githubusercontent.com/', 1).replace('/blob/', '/', 1)
        fetched = state.fetch_fn(url, sleep=state.sleep, max_bytes=material_fetch_limit(state.cfg['model']))

    if not fetched.ok or not fetched.text or fetched.truncated or (not validate_document(candidate.path, fetched.text)[0]):
        state.report['fetch_failed'] += 1
        update_candidate_status(state.pool, seq, STATUS_FETCH_FAILED)
        save_pool(state.pool_path, state.pool)
        state.save()
        return True

    text = fetched.text
    candidate.content_fingerprint = content_fingerprint(text)
    candidate.normalized_content_fingerprint = normalized_content_fingerprint(text)

    # 4. 抓取后预筛与静态规则跳过
    pres = prescreen(candidate, state.cfg['prescreen'], text)
    _record_static_observation(state.report, pres.static_observation)
    if pres.excluded:
        state.report['prescreen_excluded'] += 1
        state.publish(candidate, pres)
        update_candidate_status(state.pool, seq, POOL_STATUS_EXCLUDED)
        save_pool(state.pool_path, state.pool)
        state.save()
        return True

    enable_static_skip = state.settings.get('enable_static_skip', False) or (state.cfg.get('rules', {}).get('static_heuristics', {}).get('enable_skip', False))
    if should_static_skip(pres.static_observation, enable_static_skip):
        state.report['static_skipped'] += 1
        state.report.setdefault('static_heuristics', {}).setdefault('skipped_count', 0)
        state.report['static_heuristics']['skipped_count'] += 1
        state.log(f"跳过 #{seq}（静态规则明确空壳占位，不调用模型）：{candidate.skill_id}")
        update_candidate_status(state.pool, seq, STATUS_STATIC_SKIPPED)
        save_pool(state.pool_path, state.pool)
        state.save()
        return True

    # 5. 历史结果检查（人工收藏、精确缓存、受限规范化缓存）
    eid = evaluation_id(candidate, state.cfg['model'], state.cfg['rules'])
    local_record = state.ledger.get(eid)
    record = local_record or ({} if 'models' in state.cfg['model'] else
        _read(state.root / 'data' / 'state' / 'evaluations' / evaluation_filename(eid), {}))

    if candidate.skill_id in state.manual_picks:
        outcome = record.get('outcome') or ({'decision': (state.entries.get(candidate.skill_id) or {}).get('status')} if candidate.skill_id in state.entries else None)
        state.publish(candidate, pres, outcome=outcome)
        update_candidate_status(state.pool, seq, STATUS_DONE)
        save_pool(state.pool_path, state.pool)
        state.save()
        return True

    if record.get('status') == 'completed':
        state.report['cached'] += 1
        cached_outcome = dict(record['outcome']) if isinstance(record['outcome'], dict) else {}
        cached_outcome['cached'] = True
        state.publish(candidate, pres, cached_outcome)
        update_candidate_status(state.pool, seq, STATUS_DONE)
        save_pool(state.pool_path, state.pool)
        state.save()
        return True

    cache_obs = state.report.setdefault("cache_observation", {
        "version": NORMALIZATION_VERSION,
        "enabled": False,
        "observed_count": 0,
        "potential_hits": 0,
        "actual_reused": 0,
        "rejection_reasons": {},
    })
    enable_norm_cache = bool(
        state.settings.get("enable_normalized_cache", False)
        or state.cfg.get("rules", {}).get("cache", {}).get("enable_normalized_reuse", False)
    )
    cache_obs["enabled"] = enable_norm_cache

    norm_record, norm_rejection = find_normalized_candidate_record(state, candidate, text)
    if norm_record is not None:
        cache_obs["observed_count"] += 1
        cache_obs["potential_hits"] += 1
        if enable_norm_cache:
            cache_obs["actual_reused"] += 1
            state.report["cached"] += 1
            reuse_audit = create_reuse_audit(norm_record, text, exact_match=False)
            reused_outcome = deepcopy(norm_record["outcome"]) if isinstance(norm_record["outcome"], dict) else {}
            reused_outcome["cached"] = True
            reused_outcome["reuse_audit"] = reuse_audit
            reused_outcome["candidate"] = asdict(candidate)
            reused_outcome["materials"] = primary_material_bundle(candidate, text, now_local().isoformat()).manifest()
            if isinstance(reused_outcome.get("evaluation"), dict):
                reused_outcome["evaluation"]["source_fingerprint"] = candidate.content_fingerprint
            reused_outcome["normalized_content_fingerprint"] = candidate.normalized_content_fingerprint
            reused_outcome["normalization_version"] = NORMALIZATION_VERSION
            reused_outcome["output_contract_version"] = LLM_OUTPUT_CONTRACT_VERSION
            reused_outcome["evaluated_at"] = now_local().isoformat()
            state.ledger.reserve([{
                "evaluation_id": eid,
                "skill_id": candidate.skill_id,
                "content_fingerprint": candidate.content_fingerprint,
                "normalized_content_fingerprint": candidate.normalized_content_fingerprint,
                "normalization_version": NORMALIZATION_VERSION,
                "output_contract_version": LLM_OUTPUT_CONTRACT_VERSION,
                "rules_version": state.cfg["rules"]["rules_version"],
                "model_config_version": state.cfg["model"].get("model_config_version"),
            }])
            state.ledger.complete(eid, reused_outcome)
            ledger_rec = state.ledger.get(eid)
            if ledger_rec:
                ledger_rec['normalized_content_fingerprint'] = candidate.normalized_content_fingerprint
                ledger_rec['normalization_version'] = NORMALIZATION_VERSION
                ledger_rec['output_contract_version'] = LLM_OUTPUT_CONTRACT_VERSION
                state.ledger.save_record(eid, ledger_rec)
                if hasattr(state, '_skill_eval_records_index') and state._skill_eval_records_index is not None:
                    state._skill_eval_records_index.setdefault(candidate.skill_id, []).append(ledger_rec)
            state.publish(candidate, pres, reused_outcome)
            update_candidate_status(state.pool, seq, STATUS_DONE)
            save_pool(state.pool_path, state.pool)
            state.log(
                f"[受限规范化复用] #{seq} {candidate.skill_id}：成功复用历史评估（来源：{norm_record.get('evaluation_id')}，版本：{NORMALIZATION_VERSION}），0 Token 消耗。"
            )
            state.save()
            return True
        else:
            state.log(
                f"[受限规范化观察] #{seq} {candidate.skill_id}：检测到潜在复用记录（来源：{norm_record.get('evaluation_id')}），观察模式下不阻断模型评估。"
            )
    elif norm_rejection and norm_rejection not in ("no_matching_candidate", "no_history_records", "no_current_fingerprint"):
        cache_obs["observed_count"] += 1
        reason_key = norm_rejection.split(":")[0]
        cache_obs["rejection_reasons"][reason_key] = cache_obs["rejection_reasons"].get(reason_key, 0) + 1

    # 6. 不可重试失败与历史异常检查
    if (record.get('error') or {}).get('reason_code') == REASON_LENGTH_EXCEEDED:
        update_candidate_status(state.pool, seq, STATUS_LENGTH_EXCEEDED)
        save_pool(state.pool_path, state.pool)
        state.report['skipped_length_exceeded'] += 1
        state.report['reconciled_blocked'] += 1
        state.save()
        return True

    effective_attempt_limit = (int(record.get('max_attempts') or state.max_attempts)
                               if record.get('resume_history') else state.max_attempts)
    resumable_failure = bool(
        local_record and not record.get('retry_exhausted') and record.get('status') == 'failed'
        and ((record.get('error') or {}).get('reason_code') == 'NETWORK_ERROR' or record.get('retryable'))
        and (int(record.get('attempts') or 0) < effective_attempt_limit)
    )
    if record.get('status') in ('failed', 'in_progress', 'needs_recovery') and (not resumable_failure):
        reason = 'NON_RETRYABLE_FAILURE' if record.get('status') == 'failed' else 'UNKNOWN_IN_PROGRESS'
        block_info = _build_block_info(
            eid, reason,
            stage=record.get('stage'),
            http_status=(record.get('error') or {}).get('http_status'),
            model=state.cfg['model'].get('model'),
            model_config_version=state.cfg['model'].get('model_config_version'),
        )
        block_info['reason_code'] = (record.get('error') or {}).get('reason_code')
        block_info['error_kind'] = (record.get('error') or {}).get('error_kind') or record.get('error_kind')
        _record_blocked_candidate(state, seq, block_info, reconciled=True)
        state.log(f"[阻止] #{seq} {candidate.skill_id}：存在不可重试评估记录（{reason}），已持久化为 blocked，本次未调用模型。")
        state.save()
        return True

    # 7. 评估预算检查与预留
    norm_fp = candidate.normalized_content_fingerprint or normalized_content_fingerprint(text)
    if hasattr(state, 'wait_for_budget'):
        state.unknown_reserve = _unknown_usage_reserve(
            candidate, text, state.cfg, **_topic_evaluation_options(state, candidate),
        )
        if not state.wait_for_budget():
            return False

    state.ledger.reserve([{
        'evaluation_id': eid,
        'skill_id': candidate.skill_id,
        'content_fingerprint': candidate.content_fingerprint,
        'normalized_content_fingerprint': norm_fp,
        'normalization_version': NORMALIZATION_VERSION,
        'rules_version': state.cfg['rules']['rules_version'],
        'model_config_version': state.cfg['model'].get('model_config_version'),
    }])
    record = state.ledger.get(eid) or {}
    record['max_attempts'] = (int(record.get('max_attempts') or effective_attempt_limit)
                             if record.get('resume_history')
                             else state.max_attempts + int(bool(record.get('pending_evaluation'))))
    state.ledger.save_record(eid, record)
    state.report['evaluations'] += 1
    state.log(f"评估 #{state.report['evaluations']}：{candidate.name}（累计 {state.usage.total_tokens:,} Token）")
    state.unknown_reserve = _unknown_usage_reserve(
        candidate, text, state.cfg, **_topic_evaluation_options(state, candidate),
    )

    # 8. 受控模型评估调用
    result = _evaluate_with_retries(state, candidate, text, eid, record)
    if result is None:
        return False

    # 9. 结果分类与条目持久化结算
    decision = classify_result(result)
    if decision.category == 'resource_pause':
        state.stop_causes.add(decision.stop_cause)
        state.report['stop_reason'] = resolve_primary_stop_reason(state.stop_causes)
        state.active_eid = state.active_call = None
        state.save()
        return False

    if result['ok']:
        evaluation = result['evaluation']
        filtered_decision = filter_new_evaluation(
            decide(evaluation, state.cfg['rules']), evaluation, state.cfg.get("filter_rules"),
            state.entries.get(candidate.skill_id),
            previously_evaluated=candidate.skill_id in state.evaluated_skill_ids,
        )
        outcome = {
            **filtered_decision,
            'evaluation': evaluation,
            'model': getattr(result.get('call'), 'requested_model', None),
            'model_config_fingerprint': getattr(result.get('call'), 'model_config_fingerprint', None),
            'materials': primary_material_bundle(candidate, text, now_local().isoformat()).manifest(),
            'main_category': evaluation.get('main_category'),
            'usage': state.active_call['usage'] if state.active_call else None,
            'normalized_content_fingerprint': candidate.normalized_content_fingerprint,
            'normalization_version': NORMALIZATION_VERSION,
            'output_contract_version': LLM_OUTPUT_CONTRACT_VERSION,
        }
        if outcome.get("topic_filtered"):
            topics = "、".join(outcome["blocked_topics"])
            state.log(f"[主题屏蔽] #{seq} {candidate.skill_id}：主要用途属于 {topics}，不进入推荐或候选。")
            state.report["topic_filtered"] = state.report.get("topic_filtered", 0) + 1
        outcome['candidate'] = asdict(candidate)
        outcome['request_usage'] = (state.ledger.get(eid) or {}).get('requests', [])
        outcome['prescreen'] = asdict(pres)
        outcome['evaluated_at'] = now_local().isoformat()
        state.ledger.complete(eid, outcome)
        state.evaluated_skill_ids.add(candidate.skill_id)
        ledger_rec = state.ledger.get(eid)
        if ledger_rec:
            ledger_rec['normalized_content_fingerprint'] = candidate.normalized_content_fingerprint
            ledger_rec['normalization_version'] = NORMALIZATION_VERSION
            ledger_rec['output_contract_version'] = LLM_OUTPUT_CONTRACT_VERSION
            state.ledger.save_record(eid, ledger_rec)
            if hasattr(state, '_skill_eval_records_index') and state._skill_eval_records_index is not None:
                state._skill_eval_records_index.setdefault(candidate.skill_id, []).append(ledger_rec)
        if state.active_call:
            state.active_call['decision'] = outcome['decision']
        state.publish(candidate, pres, outcome)
        state.consecutive_failures, state.format_failures = update_failure_counters(
            result, decision, state.consecutive_failures, state.format_failures
        )
        update_candidate_status(state.pool, seq, STATUS_DONE)
        save_pool(state.pool_path, state.pool)

    elif decision.is_length_exceeded:
        update_candidate_status(state.pool, seq, STATUS_LENGTH_EXCEEDED)
        save_pool(state.pool_path, state.pool)
        state.consecutive_failures, state.format_failures = update_failure_counters(
            result, decision, state.consecutive_failures, state.format_failures
        )
        state.report['skipped_length_exceeded'] += 1
        limit = state.cfg['model'].get('limits', {}).get('max_output_tokens', 4000)
        state.log(f"[自动跳过] #{seq} {candidate.name}：输出达到 {limit} Token 上限，已保存 length_exceeded。")

    elif decision.is_format_error:
        block_info = _build_block_info(
            eid, decision.block_reason or 'OUTPUT_FORMAT_INVALID',
            decision=decision,
            http_status=decision.http_status or 200,
            error_detail=str(result.get('error')) if result.get('error') else None,
            model=state.cfg['model'].get('model'),
            model_config_version=state.cfg['model'].get('model_config_version'),
        )
        _record_blocked_candidate(state, seq, block_info)
        state.consecutive_failures, state.format_failures = update_failure_counters(
            result, decision, state.consecutive_failures, state.format_failures
        )
        state.report['skipped_output_format'] += 1
        stage_desc = "复核" if decision.stage == "review" else "初评"
        err_detail = f"：{result.get('error')}" if result.get('error') else ""
        state.log(f"[格式异常] #{seq} {candidate.name}：{stage_desc}输出格式不合法（{decision.error_kind}{err_detail}），已保存 blocked（距上次有效结果累计 {state.format_failures}/{state.max_format_failures}）。")
        if state.format_failures >= state.max_format_failures:
            state.stop_causes.add(STOP_FORMAT_FAILURES)
            state.log("[停止] 输出格式异常达到阈值，请检查模型与评估输出契约；后续候选未调用。")

    elif decision.category == 'access_denied':
        block_info = _build_block_info(
            eid, decision.block_reason or 'ACCESS_DENIED',
            decision=decision,
            model=state.cfg['model'].get('model'),
            model_config_version=state.cfg['model'].get('model_config_version'),
        )
        _record_blocked_candidate(state, seq, block_info)
        state.stop_causes.add(STOP_ACCESS_DENIED)
        state.log(f"[停止] 访问认证或权限失败 (HTTP {decision.http_status})，已将本条标记 blocked 并终止运行。")

    elif decision.category == 'resume_state_invalid':
        block_info = _build_block_info(
            eid, decision.block_reason or 'RESUME_STATE_INVALID',
            decision=decision,
            model=state.cfg['model'].get('model'),
            model_config_version=state.cfg['model'].get('model_config_version'),
        )
        _record_blocked_candidate(state, seq, block_info)
        state.stop_causes.add(STOP_RESUME_STATE_INVALID)
        state.log(f"[停止] 恢复状态冲突：{result.get('error')}，已停止运行。")

    elif decision.category == 'request_config_error':
        block_info = _build_block_info(
            eid, decision.block_reason or 'REQUEST_CONFIG_ERROR',
            decision=decision,
            model=state.cfg['model'].get('model'),
            model_config_version=state.cfg['model'].get('model_config_version'),
        )
        _record_blocked_candidate(state, seq, block_info)
        state.stop_causes.add(STOP_REQUEST_CONFIG_ERROR)
        state.log(f"[停止] 请求配置错误 (HTTP {decision.http_status})，已停止运行。")

    elif not result.get('pending_evaluation'):
        state.report['failed_evaluations'] += 1
        state.consecutive_failures, state.format_failures = update_failure_counters(
            result, decision, state.consecutive_failures, state.format_failures
        )
        if decision.retryable:
            final_record = state.ledger.get(eid) or {}
            if final_record.get('retry_exhausted') or int(final_record.get('attempts') or 0) >= int(final_record.get('max_attempts') or state.max_attempts):
                _update_blocked(state.pool, seq, {
                    'evaluation_id': eid, 'reason': 'RETRY_EXHAUSTED',
                    'reason_code': result.get('reason_code'),
                    'attempts': final_record.get('attempts'),
                    'max_attempts': final_record.get('max_attempts'),
                    'blocked_at': now_local().isoformat(), 'source': 'local_ledger',
                })
                save_pool(state.pool_path, state.pool)
                state.report['blocked_records'] += 1
                state.report['blocked_new'] += 1
                state.log(f"[跳过] {candidate.skill_id} 重试次数已耗尽，继续下一个候选。")
        else:
            block_info = _build_block_info(
                eid, 'NON_RETRYABLE_FAILURE',
                decision=decision,
                result=result,
                http_status=getattr(result.get('call'), 'http_status', None),
                model=state.cfg['model'].get('model'),
                model_config_version=state.cfg['model'].get('model_config_version'),
            )
            _record_blocked_candidate(state, seq, block_info)
            state.log(f"[阻止] #{seq} {candidate.skill_id}：评估失败且不可重试（{result.get('reason_code')}），已持久化为 blocked。")

        if state.consecutive_failures >= state.settings['max_consecutive_failures']:
            state.stop_causes.add(STOP_MODEL_FAILURES)

    # 10. 响应用量检查与收尾落盘
    if (state.active_call and (state.active_call.get('usage') or {}).get('total_tokens') is None
            and not decision.retryable
            and state.active_call.get('billing_state') != 'rejected_before_inference'):
        state.stop_causes.add(STOP_USAGE_UNKNOWN)
        state.log("[停止] 当前响应用量未知；本条状态已保存，后续付费调用已停止。")

    state.active_eid = state.active_call = None
    state.report['format_failures_without_valid_result'] = state.format_failures
    state.report['blocked_total'] = state.pool.stats().get('blocked', 0) if state.pool else 0
    state.report['stop_causes'] = sorted(list(state.stop_causes))
    primary_stop = resolve_primary_stop_reason(state.stop_causes)
    if primary_stop:
        state.report['stop_reason'] = primary_stop

    state.save()
    state.log(f"新增推荐 {state.report['new_recommended']}/{state.settings['target_recommended']}；输入 {state.usage.prompt_tokens:,}，输出 {state.usage.completion_tokens:,}，合计 {state.usage.total_tokens:,} Token。")
    if state.report['stop_reason']:
        return False
    return True
