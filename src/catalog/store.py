"""目录读改写排他锁协调函数与恢复规则（落实 T19、T20）。

遵循总方案“不另造存储服务类”原则，采用普通协调函数：
1. 跨平台排他文件锁（data/.catalog.lock）保护读-改-写全过程
2. 严格两步顺序持久化：先写真实数据 data/catalog.json，再写前端投影 public/data/catalog.json
3. 发生异常或页面丢失时，通过 0-Token 恢复函数重新投影页面
"""

from __future__ import annotations

from contextlib import contextmanager
import os
from pathlib import Path
import time
from typing import Any, Callable, Generator

from src.infra.files import read_json, write_json_atomic


class LockConflict(RuntimeError):
    """目录排他锁冲突异常。"""


@contextmanager
def catalog_lock(lock_path: Path | str, timeout: float = 0.0, retry_interval: float = 0.05) -> Generator[None, None, None]:
    """获取目录排他锁的上下文管理器。

    使用 os.O_CREAT | os.O_EXCL 保证多进程/多任务原子竞争：
    - 若锁已被占用且在 timeout 时间内未释放，立即抛出 LockConflict 异常，绝不静默覆盖。
    """
    path = Path(lock_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    start = time.monotonic()
    fd: int | None = None

    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_RDWR)
            break
        except FileExistsError:
            if time.monotonic() - start >= timeout:
                raise LockConflict(f"无法获取目录锁 {path}：被其他进程占用")
            time.sleep(retry_interval)

    try:
        yield
    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        try:
            path.unlink(missing_ok=True)
        except OSError:
            pass


def mutate_catalog(
    root_dir: Path | str,
    mutator_fn: Callable[[dict], dict],
    *,
    data_dir: Path | str | None = None,
    public_dir: Path | str | None = None,
    timeout: float = 0.0,
) -> dict:
    """读-改-写全过程排他锁协调函数（落实 T19 规范）。

    1. 获取同根目录排他锁（data/.catalog.lock）
    2. 读取最新 data/catalog.json
    3. 执行纯内存修改回调 mutator_fn
    4. 严格顺序持久化：
       - 第 1 步：先写主索引 data/catalog.json
       - 第 2 步：再写前端页面数据 public/data/catalog.json
    5. 释放文件锁并返回更新后的目录字典
    """
    root = Path(root_dir)
    data_path = Path(data_dir) if data_dir else root / "data"
    public_path = Path(public_dir) if public_dir else root / "public"
    lock_file = data_path / ".catalog.lock"

    with catalog_lock(lock_file, timeout=timeout):
        catalog_file = data_path / "catalog.json"
        current_catalog = read_json(catalog_file, default={"entries": []})

        updated_catalog = mutator_fn(current_catalog)

        # 严格顺序持久化：第一步先写源数据
        write_json_atomic(catalog_file, updated_catalog)

        # 第二步写页面公开数据（使用 build_page_data 投影）
        from .index import build_page_data
        page_file = public_path / "data" / "catalog.json"
        write_json_atomic(page_file, build_page_data(updated_catalog))

        return updated_catalog


def recover_catalog_projections(
    root_dir: Path | str,
    *,
    data_dir: Path | str | None = None,
    public_dir: Path | str | None = None,
) -> bool:
    """部分写入失败时的 0-Token 恢复函数（落实 T20 规范）。

    若主索引 data/catalog.json 完好，但 public/data/catalog.json 缺失或不一致，
    直接读取主索引重新投影页面文件，绝不发起任何模型调用。
    """
    root = Path(root_dir)
    data_path = Path(data_dir) if data_dir else root / "data"
    public_path = Path(public_dir) if public_dir else root / "public"

    source_file = data_path / "catalog.json"
    if not source_file.exists():
        return False

    page_file = public_path / "data" / "catalog.json"
    from .index import build_page_data
    source_catalog = read_json(source_file)
    expected_page_data = build_page_data(source_catalog)

    needs_recovery = False
    if not page_file.exists():
        needs_recovery = True
    else:
        try:
            current_page_data = read_json(page_file)
            if current_page_data != expected_page_data:
                needs_recovery = True
        except Exception:
            needs_recovery = True

    if needs_recovery:
        write_json_atomic(page_file, expected_page_data)
        return True

    return False


__all__ = [
    "LockConflict",
    "catalog_lock",
    "mutate_catalog",
    "recover_catalog_projections",
]
