"""Single-writer catalog sessions, atomic persistence and offline recovery."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from pathlib import Path
import hashlib
from src.infra.files import read_json, write_json_atomic, file_lock, LockConflict
from src.shared.runtime import is_test_environment

_sessions = ContextVar("catalog_sessions", default=frozenset())
catalog_lock = file_lock


@contextmanager
def catalog_session(data_dir, timeout=0):
    path = Path(data_dir).resolve()
    project_data = (Path(__file__).resolve().parents[2] / "data").resolve()
    if path == project_data and is_test_environment():
        raise RuntimeError(
            "测试环境中禁止直接写入工程生产 data/local 目录！"
            "请在测试用例中显式提供临时隔离目录（如 tempfile.TemporaryDirectory）。"
        )
    if path in _sessions.get():
        yield
        return
    with catalog_lock(path / ".catalog.lock", timeout=timeout):
        token = _sessions.set(_sessions.get() | {path})
        try:
            yield
        finally:
            _sessions.reset(token)


def catalog_task(function):
    """Hold one lock for the entire use case, including the initial read."""
    spec = signature(function)
    @wraps(function)
    def guarded(*args, **kwargs):
        values = spec.bind(*args, **kwargs)
        values.apply_defaults()
        args_map = values.arguments
        catalog_path = args_map.get("catalog_path")
        data = Path(catalog_path).parent if catalog_path else args_map.get("data_dir")
        if data is None:
            data = Path(args_map.get("root_dir", args_map.get("root", "."))) / "data"
        with catalog_session(data):
            return function(*args, **kwargs)
    return guarded


def write_catalog(catalog, *, data_path, public_path, special_ids: set[str]):
    from .index import build_page_data
    from src.shared.special_skills import filter_out_special_skills
    data, public = Path(data_path), Path(public_path)
    with catalog_session(data.parent):
        if "entries" in catalog and special_ids:
            filtered_entries, _ = filter_out_special_skills(catalog["entries"], special_ids)
            catalog["entries"] = filtered_entries
            rec = sum(1 for e in filtered_entries if e.get("status") == "recommended" and not e.get("manual_pick"))
            cand = sum(1 for e in filtered_entries if e.get("status") == "candidate" and not e.get("manual_pick"))
            manual = sum(1 for e in filtered_entries if e.get("manual_pick"))
            excl = sum(1 for e in filtered_entries if e.get("status") == "excluded")
            catalog["counts"] = {
                "recommended": rec,
                "candidate": cand,
                "manual": manual,
                "excluded": excl,
            }
        page = build_page_data(catalog, special_ids=special_ids)
        write_json_atomic(data, catalog)
        # If this fails, the committed source remains sufficient to rebuild.
        # public 页面面向 GitHub Pages 静态分发，采用 indent=None 紧凑压缩以减少传输开销
        write_json_atomic(public, page, indent=None)
        return {"catalog_path": str(data), "page_path": str(public),
                "catalog_digest": "sha256:" + hashlib.sha256(data.read_bytes()).hexdigest()[:32],
                "page_digest": "sha256:" + hashlib.sha256(public.read_bytes()).hexdigest()[:32],
                "counts": page["counts"], "generated_at": catalog.get("generated_at")}


def mutate_catalog(root_dir, mutator_fn, *, data_dir=None, public_dir=None, timeout=0, special_ids=None):
    from src.infra.special_skills import load_special_skill_ids
    root = Path(root_dir)
    data = Path(data_dir) if data_dir else root / "data"
    public = Path(public_dir) if public_dir else root / "public"
    target_special_ids = special_ids if special_ids is not None else load_special_skill_ids(root_dir=root)
    with catalog_session(data, timeout=timeout):
        current = read_json(data / "catalog.json", default={"entries": []})
        updated = mutator_fn(current)
        write_catalog(updated, data_path=data / "catalog.json", public_path=public / "data" / "catalog.json", special_ids=target_special_ids)
        return updated


def recover_catalog_projections(root_dir, *, data_dir=None, public_dir=None):
    from .index import build_page_data
    from src.infra.special_skills import load_special_skill_ids
    root = Path(root_dir)
    data = Path(data_dir) if data_dir else root / "data"
    public = Path(public_dir) if public_dir else root / "public"
    special_ids = load_special_skill_ids(root_dir=root)
    with catalog_session(data):
        source = data / "catalog.json"
        if not source.exists():
            return False
        expected = build_page_data(read_json(source), special_ids=special_ids)
        target = public / "data" / "catalog.json"
        try:
            current = read_json(target)
        except (OSError, ValueError):
            current = None
        if current == expected:
            return False
        write_json_atomic(target, expected)
        return True
