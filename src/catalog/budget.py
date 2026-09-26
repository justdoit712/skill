"""周额度账本（§7.3）。

核心语义，全部按"保守、防重复计费"设计：

- 以 Asia/Shanghai 周一 00:00 起算一周，用带年份的 ISO 周标识（如 2026-W38）
- **先持久化预留，再调用模型**；预留失败就停止付费调用
- 批处理崩溃或 runner 被终止**不释放**已预留名额，防止再次拿到 50 个
- 每个名额最多 max_attempts 次尝试，**调用前**持久化尝试计数；手动重跑不重置
- 完成的评估 ID 直接复用结果；调用结果不明的标为 needs_recovery，不静默免费重跑
- 网络请求与来源检查不占 AI 名额，因此只有模型调用进入本账本
- dry_run 不写账本

记录中不含密钥，也不含个人健康或交易账户信息。
"""

from __future__ import annotations

import hashlib
import json
import os
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path

from src.infra.files import write_json_atomic
from src.shared.runtime import (
    SHANGHAI_TZ,
    TZ_SOURCE,
    iso_now,
    now_local,
    week_id,
)

BUDGET_VERSION = "1.0.0"
LEDGER_FILENAME = "budget.json"
EVALUATIONS_DIRNAME = "evaluations"
HISTORY_DIRNAME = "history"

STATUS_RESERVED = "reserved"
STATUS_IN_PROGRESS = "in_progress"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"
STATUS_NEEDS_RECOVERY = "needs_recovery"

REUSABLE_STATUSES = (STATUS_COMPLETED,)
BLOCKING_STATUSES = (STATUS_NEEDS_RECOVERY,)


class QuotaExceeded(RuntimeError):
    """本周剩余额度不足以完成本批预留。"""


def evaluation_filename(evaluation_id: str) -> str:
    """评估 ID 含 | 与 : 等字符，落盘用其摘要作文件名。"""
    digest = hashlib.sha256(evaluation_id.encode("utf-8")).hexdigest()[:24]
    return f"{digest}.json"


_iso = iso_now
_write_json_atomic = write_json_atomic


@dataclass
class BudgetLedger:
    """一周的额度账本，与每个评估的状态记录。"""

    state_dir: Path
    cap: int
    week: str
    reserved: list[str] = field(default_factory=list)
    updated_at: str = ""
    rollover_from: str | None = None
    max_attempts: int = 2

    # ---------- 加载与保存 ----------

    @property
    def ledger_path(self) -> Path:
        return self.state_dir / LEDGER_FILENAME

    @property
    def evaluations_dir(self) -> Path:
        return self.state_dir / EVALUATIONS_DIRNAME

    @classmethod
    def load(cls, state_dir: str | Path, cap: int, *, max_attempts: int = 2, moment: datetime | None = None) -> "BudgetLedger":
        base = Path(state_dir)
        current = week_id(moment)
        ledger = cls(state_dir=base, cap=int(cap), week=current, max_attempts=max_attempts)

        if not ledger.ledger_path.exists():
            ledger.updated_at = _iso(moment)
            return ledger

        payload = json.loads(ledger.ledger_path.read_text(encoding="utf-8"))
        stored_week = str(payload.get("week") or "")
        if stored_week == current:
            ledger.reserved = list(payload.get("reserved") or [])
            ledger.cap = int(payload.get("cap") or cap)
            ledger.updated_at = str(payload.get("updated_at") or _iso(moment))
            return ledger

        ledger.rollover_from = stored_week or None
        ledger.updated_at = _iso(moment)
        return ledger

    def rollover(self, moment=None):
        """Explicit mutation; production callers hold their catalog session."""
        from src.infra.files import file_lock
        with file_lock(self.state_dir / ".budget.lock"):
            if self.ledger_path.exists():
                payload = json.loads(self.ledger_path.read_text(encoding="utf-8"))
                stored = payload.get("week")
                if stored and stored != self.week:
                    history = self.state_dir / HISTORY_DIRNAME
                    history.mkdir(parents=True, exist_ok=True)
                    archived = history / f"budget-{stored}.json"
                    if not archived.exists():
                        write_json_atomic(archived, payload)
                    self.rollover_from = stored
                    self.save(moment)
        return self

    def save(self, moment: datetime | None = None) -> None:
        self.updated_at = _iso(moment)
        write_json_atomic(
            self.ledger_path,
            {
                "budget_version": BUDGET_VERSION,
                "week": self.week,
                "cap": self.cap,
                "reserved_count": len(self.reserved),
                "reserved": list(self.reserved),
                "updated_at": self.updated_at,
                "timezone": TZ_SOURCE,
                "rollover_from": self.rollover_from,
            },
        )

    # ---------- 评估记录 ----------

    def record_path(self, evaluation_id: str) -> Path:
        return self.evaluations_dir / evaluation_filename(evaluation_id)

    def get(self, evaluation_id: str) -> dict | None:
        path = self.record_path(evaluation_id)
        if not path.exists():
            return None
        return json.loads(path.read_text(encoding="utf-8"))

    def save_record(self, evaluation_id: str, record: dict, moment: datetime | None = None) -> None:
        record["evaluation_id"] = evaluation_id
        record["updated_at"] = _iso(moment)
        write_json_atomic(self.record_path(evaluation_id), record)

    # ---------- 额度 ----------

    @property
    def reserved_count(self) -> int:
        return len(self.reserved)

    @property
    def remaining(self) -> int:
        return max(0, self.cap - len(self.reserved))

    def _sync_from_disk(self) -> None:
        """预留前重新读取账本，避免使用陈旧视图导致超额。

        真正的并发串行化仍靠 workflow 的 concurrency group（§7.2）；
        这里只是把"同一次运行内多处读取"造成的偏差消掉。
        """
        if not self.ledger_path.exists():
            return
        try:
            payload = json.loads(self.ledger_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if str(payload.get("week") or "") != self.week:
            return
        for evaluation_id in payload.get("reserved") or []:
            if evaluation_id not in self.reserved:
                self.reserved.append(evaluation_id)

    def reserve(self, entries: list[dict], moment: datetime | None = None) -> list[str]:
        """预留名额并**立即落盘**。超额直接拒绝，不做部分预留。

        entries 每项需含 evaluation_id，可另带 skill_id、content_fingerprint、
        rules_version、model_config_version。
        """
        self._sync_from_disk()
        pending = [
            e
            for e in entries
            if e["evaluation_id"] not in self.reserved
            and (self.get(e["evaluation_id"]) or {}).get("status") not in REUSABLE_STATUSES
        ]
        if len(self.reserved) + len(pending) > self.cap:
            raise QuotaExceeded(
                f"本周（{self.week}）剩余 {self.remaining} 个名额，本批需 {len(pending)} 个"
            )

        stamp = _iso(moment)
        for entry in pending:
            evaluation_id = entry["evaluation_id"]
            existing = self.get(evaluation_id) or {}
            record = {
                **existing,
                "skill_id": entry.get("skill_id"),
                "content_fingerprint": entry.get("content_fingerprint"),
                "rules_version": entry.get("rules_version"),
                "model_config_version": entry.get("model_config_version"),
                "week": self.week,
                "status": STATUS_RESERVED,
                "attempts": int(existing.get("attempts") or 0),
                "max_attempts": (int(existing.get("max_attempts") or self.max_attempts)
                                 if existing.get("resume_history") else self.max_attempts),
                "reserved_at": existing.get("reserved_at") or stamp,
                "outcome": existing.get("outcome"),
                "error": existing.get("error") if existing.get("resume_history") else None,
            }
            self.save_record(evaluation_id, record, moment)
            self.reserved.append(evaluation_id)

        self.save(moment)
        return [e["evaluation_id"] for e in pending]

    def can_attempt(self, evaluation_id: str) -> tuple[bool, str]:
        """是否还能为这个名额发起调用。"""
        record = self.get(evaluation_id)
        if record is None:
            return False, "没有预留记录，必须先预留"
        status = record.get("status")
        if status in REUSABLE_STATUSES:
            return False, "已完成，复用结果，不重复调用"
        if status in BLOCKING_STATUSES:
            return False, "上次调用结果不明，需人工确认，不静默免费重跑"
        if int(record.get("attempts") or 0) >= int(record.get("max_attempts") or self.max_attempts):
            return False, "已达尝试上限，失败任务下周重新排队并占新周名额"
        return True, ""

    def begin_attempt(self, evaluation_id: str, moment: datetime | None = None) -> int:
        """**调用前**把尝试计数落盘，防止重跑绕过上限。"""
        allowed, reason = self.can_attempt(evaluation_id)
        if not allowed:
            raise RuntimeError(reason)
        record = self.get(evaluation_id) or {}
        record["attempts"] = int(record.get("attempts") or 0) + 1
        record["status"] = STATUS_IN_PROGRESS
        record["week"] = record.get("week") or self.week
        self.save_record(evaluation_id, record, moment)
        return record["attempts"]

    def complete(self, evaluation_id: str, outcome: dict, moment: datetime | None = None) -> None:
        record = self.get(evaluation_id) or {}
        record["status"] = STATUS_COMPLETED
        record["outcome"] = outcome
        record["error"] = None
        self.save_record(evaluation_id, record, moment)

    def fail(self, evaluation_id: str, reason_code: str, error: str, moment: datetime | None = None) -> None:
        """标记失败。名额**不释放**——失败仍占本周额度。"""
        record = self.get(evaluation_id) or {}
        record["status"] = STATUS_FAILED
        record["error"] = {"reason_code": reason_code, "message": error}
        self.save_record(evaluation_id, record, moment)

    def mark_needs_recovery(self, evaluation_id: str, note: str, moment: datetime | None = None) -> None:
        """调用结果不明（如进程被杀在返回之前），不静默重跑。"""
        record = self.get(evaluation_id) or {}
        record["status"] = STATUS_NEEDS_RECOVERY
        record["error"] = {"reason_code": "UNKNOWN_OUTCOME", "message": note}
        self.save_record(evaluation_id, record, moment)

    def mark_in_progress_as_needs_recovery(self, moment: datetime | None = None) -> list[str]:
        """运行开始时调用：上次留下的 in_progress 说明进程在返回前中断。"""
        recovered: list[str] = []
        for evaluation_id in list(self.reserved):
            record = self.get(evaluation_id) or {}
            if record.get("status") == STATUS_IN_PROGRESS:
                self.mark_needs_recovery(evaluation_id, "上次运行在返回前中断", moment)
                recovered.append(evaluation_id)
        return recovered

    def snapshot(self) -> dict:
        return {
            "week": self.week,
            "cap": self.cap,
            "used": self.reserved_count,
            "remaining": self.remaining,
        }


__all__ = [
    "BUDGET_VERSION",
    "LEDGER_FILENAME",
    "EVALUATIONS_DIRNAME",
    "HISTORY_DIRNAME",
    "STATUS_RESERVED",
    "STATUS_IN_PROGRESS",
    "STATUS_COMPLETED",
    "STATUS_FAILED",
    "STATUS_NEEDS_RECOVERY",
    "REUSABLE_STATUSES",
    "BLOCKING_STATUSES",
    "QuotaExceeded",
    "evaluation_filename",
    "BudgetLedger",
    "week_id",
    "now_local",
    "iso_now",
]
