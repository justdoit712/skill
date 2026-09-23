"""文件系统安全读写基础设施。

提供原子替换写入与安全读取，避免进程崩溃或并发写入留下破损文件。
"""

from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any
from uuid import uuid4
from contextlib import contextmanager
import time


def write_json_atomic(path: Path | str, payload: Any, indent: int = 2) -> None:
    """原子写入 JSON 文件。

    1. 生成同目录下的唯一临时文件名（uuid4 避免多进程/多任务冲突）
    2. 序列化写入临时文件
    3. 调用 os.replace 完成原子重命名替换
    4. 发生异常时确保清理临时文件
    """
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp_path = target.parent / f"{target.name}.{uuid4().hex[:8]}.tmp"
    try:
        content = json.dumps(payload, ensure_ascii=False, indent=indent)
        tmp_path.write_text(content, encoding="utf-8")
        os.replace(tmp_path, target)
    finally:
        if tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass


def read_json(path: Path | str, default: Any = None) -> Any:
    """安全读取 JSON 文件，文件不存在时返回指定默认值。"""
    p = Path(path)
    if not p.exists():
        return default
    return json.loads(p.read_text(encoding="utf-8"))


def write_text_atomic(path: Path | str, text: str) -> None:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f"{target.name}.{uuid4().hex}.tmp")
    try:
        temporary.write_text(text, encoding="utf-8")
        os.replace(temporary, target)
    finally:
        temporary.unlink(missing_ok=True)


class LockConflict(RuntimeError):
    pass


@contextmanager
def file_lock(path: Path | str, timeout: float = 0, retry_interval: float = 0.05):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(target), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if time.monotonic() >= deadline:
                raise LockConflict(f"文件正在被其他任务使用，或存在未处理的遗留锁：{target}")
            time.sleep(retry_interval)
    try:
        os.close(fd)
        yield
    finally:
        target.unlink(missing_ok=True)
