from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any


PRIVATE_UMASK = 0o077
PRIVATE_DIRECTORY_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


def apply_private_umask() -> None:
    os.umask(PRIVATE_UMASK)


def ensure_private_directory(path: Path) -> Path:
    existed = path.exists()
    path.mkdir(parents=True, exist_ok=True, mode=PRIVATE_DIRECTORY_MODE)
    if not existed:
        path.chmod(PRIVATE_DIRECTORY_MODE)
    return path


def ensure_private_file(path: Path) -> Path:
    path.chmod(PRIVATE_FILE_MODE)
    return path


def atomic_write_text(path: Path, value: str, *, encoding: str = "utf-8") -> None:
    ensure_private_directory(path.parent)
    temporary = path.with_suffix(path.suffix + ".tmp")
    temporary.write_text(value, encoding=encoding)
    ensure_private_file(temporary)
    temporary.replace(path)
    ensure_private_file(path)


def atomic_write_json(path: Path, value: Any, *, sort_keys: bool = False) -> None:
    atomic_write_text(
        path,
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=sort_keys),
    )


apply_private_umask()
