#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from boss_hire.local_security import (  # noqa: E402
    PRIVATE_DIRECTORY_MODE,
    PRIVATE_FILE_MODE,
    atomic_write_json,
    atomic_write_text,
)


RETENTION_RECOMMENDATIONS = {
    "auth": "按登录生命周期保留；失效后人工确认再删除",
    "raw_response": "建议保留30天",
    "resume": "建议保留30天",
    "evaluation": "建议保留90天",
    "aggregate_report": "建议保留90天",
    "other": "按用途人工复核，不自动删除",
}


def classify_file(relative_path: Path) -> str:
    marker = "/".join(part.lower() for part in relative_path.parts)
    if any(token in marker for token in ("auth", "cookie", "token", "session", "stoken")):
        return "auth"
    if any(token in marker for token in ("resume", "candidate_cv")):
        return "resume"
    if any(token in marker for token in ("evaluation", "ranking", "audit", "rubric")):
        return "evaluation"
    if any(token in marker for token in ("report", "summary", "batch", "index", "conclusion")):
        return "aggregate_report"
    if any(token in marker for token in ("raw", "response", "snapshot", "card", "prepared")):
        return "raw_response"
    return "other"


def secure_existing_tree(root: Path) -> dict[str, int]:
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"local data root must be a real directory: {root}")
    result = {"directories_changed": 0, "files_changed": 0, "symlinks_skipped": 0}
    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        if stat.S_IMODE(current_path.stat().st_mode) != PRIVATE_DIRECTORY_MODE:
            current_path.chmod(PRIVATE_DIRECTORY_MODE)
            result["directories_changed"] += 1

        retained_directories: list[str] = []
        for name in directory_names:
            path = current_path / name
            if path.is_symlink():
                result["symlinks_skipped"] += 1
            else:
                retained_directories.append(name)
        directory_names[:] = retained_directories

        for name in file_names:
            path = current_path / name
            if path.is_symlink():
                result["symlinks_skipped"] += 1
                continue
            if not path.is_file():
                continue
            if stat.S_IMODE(path.stat().st_mode) != PRIVATE_FILE_MODE:
                path.chmod(PRIVATE_FILE_MODE)
                result["files_changed"] += 1
    return result


def build_inventory(root: Path, *, excluded: set[Path] | None = None) -> dict[str, Any]:
    excluded_paths = {path.resolve() for path in (excluded or set())}
    category_counts: Counter[str] = Counter()
    category_bytes: Counter[str] = Counter()
    extension_counts: Counter[str] = Counter()
    total_files = 0
    total_directories = 0
    total_bytes = 0
    symlinks = 0

    for current, directory_names, file_names in os.walk(root, topdown=True, followlinks=False):
        current_path = Path(current)
        total_directories += 1
        retained_directories: list[str] = []
        for name in directory_names:
            path = current_path / name
            if path.is_symlink():
                symlinks += 1
            else:
                retained_directories.append(name)
        directory_names[:] = retained_directories

        for name in file_names:
            path = current_path / name
            if path.is_symlink():
                symlinks += 1
                continue
            if not path.is_file() or path.resolve() in excluded_paths:
                continue
            size = path.stat().st_size
            category = classify_file(path.relative_to(root))
            suffix = path.suffix.lower() or "[no_extension]"
            total_files += 1
            total_bytes += size
            category_counts[category] += 1
            category_bytes[category] += size
            extension_counts[suffix] += 1

    categories = {
        category: {
            "files": category_counts[category],
            "bytes": category_bytes[category],
            "recommended_retention": recommendation,
        }
        for category, recommendation in RETENTION_RECOMMENDATIONS.items()
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).astimezone().isoformat(),
        "root": root.as_posix(),
        "contains_file_names": False,
        "deletions_performed": 0,
        "totals": {
            "directories": total_directories,
            "files": total_files,
            "bytes": total_bytes,
            "symlinks_skipped": symlinks,
        },
        "categories": categories,
        "extensions": dict(sorted(extension_counts.items())),
    }


def render_inventory_markdown(inventory: dict[str, Any], permission_changes: dict[str, int]) -> str:
    totals = inventory["totals"]
    lines = [
        "# BOSS 本地数据安全盘点",
        "",
        f"- 生成时间：{inventory['generated_at']}",
        f"- 根目录：`{inventory['root']}`",
        f"- 目录：{totals['directories']}；文件：{totals['files']}；体积：{totals['bytes']} bytes",
        f"- 权限收紧：目录 {permission_changes['directories_changed']}，文件 {permission_changes['files_changed']}",
        f"- 跳过符号链接：{permission_changes['symlinks_skipped']}",
        "- 删除操作：0",
        "- 报告不包含文件名、候选人标识、Cookie、Token 或文件正文。",
        "",
        "| 类型 | 文件数 | 体积(bytes) | 建议保留期 |",
        "|---|---:|---:|---|",
    ]
    for category, row in inventory["categories"].items():
        lines.append(
            f"| {category} | {row['files']} | {row['bytes']} | {row['recommended_retention']} |"
        )
    lines.extend(
        [
            "",
            "以上保留期仅为建议；本工具不会删除或移动任何数据，清理必须另行确认精确目录。",
            "",
        ]
    )
    return "\n".join(lines)


def secure_and_inventory(root: Path, report_json: Path, report_markdown: Path) -> dict[str, Any]:
    permission_changes = secure_existing_tree(root)
    inventory = build_inventory(root, excluded={report_json, report_markdown})
    inventory["permission_changes"] = permission_changes
    atomic_write_json(report_json, inventory, sort_keys=True)
    atomic_write_text(report_markdown, render_inventory_markdown(inventory, permission_changes))
    return inventory


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Secure and inventory local BOSS artifacts without deleting data.")
    parser.add_argument("--root", type=Path, default=Path("data/local"))
    parser.add_argument("--report-json", type=Path)
    parser.add_argument("--report-markdown", type=Path)
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    report_json = args.report_json or args.root / "security_inventory.json"
    report_markdown = args.report_markdown or args.root / "security_inventory.md"
    inventory = secure_and_inventory(args.root, report_json, report_markdown)
    print(
        json.dumps(
            {
                "root": inventory["root"],
                "totals": inventory["totals"],
                "permission_changes": inventory["permission_changes"],
                "deletions_performed": 0,
                "report_json": str(report_json),
                "report_markdown": str(report_markdown),
            },
            ensure_ascii=False,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
