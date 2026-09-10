#!/usr/bin/env python3
from __future__ import annotations

import sys
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tests.network_guard import install_boss_network_guard  # noqa: E402


def supported_python(version_info=sys.version_info) -> bool:
    return tuple(version_info[:2]) >= (3, 11)


def main() -> int:
    if not supported_python():
        version = f"{sys.version_info.major}.{sys.version_info.minor}"
        print(
            f"离线测试需要 Python 3.11 或更高版本；当前为 {version}。"
            "请先用新版 Python 创建 .venv，再运行 "
            ".venv/bin/python scripts/run_offline_tests.py。",
            file=sys.stderr,
        )
        return 2
    install_boss_network_guard()
    suite = unittest.defaultTestLoader.discover(
        start_dir=str(ROOT / "tests"),
        pattern="test*.py",
    )
    result = unittest.TextTestRunner(verbosity=2).run(suite)
    return 0 if result.wasSuccessful() else 1


if __name__ == "__main__":
    raise SystemExit(main())
