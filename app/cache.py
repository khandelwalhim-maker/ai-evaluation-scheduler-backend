from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Callable, TypeVar

from pydantic import BaseModel

T = TypeVar("T", bound=BaseModel)

CACHE_DIR = Path("/tmp/parse_cache")


def file_hash(path: str) -> str:
    with open(path, "rb") as f:
        return hashlib.sha256(f.read()).hexdigest()


def cache_path(path: str, kind: str) -> Path:
    return CACHE_DIR / f"{file_hash(path)}_{kind}.json"


def cached_parse(path: str, kind: str, schema_model: type[T], compute: Callable[[], T]) -> T:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    target = cache_path(path, kind)

    if target.is_file():
        return schema_model.model_validate_json(target.read_text(encoding="utf-8"))

    result = compute()
    target.write_text(result.model_dump_json(), encoding="utf-8")
    return result
