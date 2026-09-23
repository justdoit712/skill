"""Single-writer catalog sessions, atomic persistence and offline recovery."""
from contextlib import contextmanager
from contextvars import ContextVar
from functools import wraps
from inspect import signature
from pathlib import Path
import hashlib
from src.infra.files import read_json, write_json_atomic, file_lock, LockConflict

_sessions = ContextVar("catalog_sessions", default=frozenset())
catalog_lock = file_lock


@contextmanager
def catalog_session(data_dir, timeout=0):
    path = Path(data_dir).resolve()
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


def write_catalog(catalog, *, data_path, public_path):
    from .index import build_page_data
    data, public = Path(data_path), Path(public_path)
    with catalog_session(data.parent):
        page = build_page_data(catalog)
        write_json_atomic(data, catalog)
        # If this fails, the committed source remains sufficient to rebuild.
        write_json_atomic(public, page)
        return {"catalog_path": str(data), "page_path": str(public),
                "catalog_digest": "sha256:" + hashlib.sha256(data.read_bytes()).hexdigest()[:32],
                "page_digest": "sha256:" + hashlib.sha256(public.read_bytes()).hexdigest()[:32],
                "counts": page["counts"], "generated_at": catalog.get("generated_at")}


def mutate_catalog(root_dir, mutator_fn, *, data_dir=None, public_dir=None, timeout=0):
    root = Path(root_dir)
    data = Path(data_dir) if data_dir else root / "data"
    public = Path(public_dir) if public_dir else root / "public"
    with catalog_session(data, timeout=timeout):
        current = read_json(data / "catalog.json", default={"entries": []})
        updated = mutator_fn(current)
        write_catalog(updated, data_path=data / "catalog.json", public_path=public / "data" / "catalog.json")
        return updated


def recover_catalog_projections(root_dir, *, data_dir=None, public_dir=None):
    from .index import build_page_data
    root = Path(root_dir)
    data = Path(data_dir) if data_dir else root / "data"
    public = Path(public_dir) if public_dir else root / "public"
    with catalog_session(data):
        source = data / "catalog.json"
        if not source.exists():
            return False
        expected = build_page_data(read_json(source))
        target = public / "data" / "catalog.json"
        try:
            current = read_json(target)
        except (OSError, ValueError):
            current = None
        if current == expected:
            return False
        write_json_atomic(target, expected)
        return True
