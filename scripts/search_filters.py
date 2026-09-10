#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from boss_hire.search_filters import filter_catalog, resolve_filter_selections  # noqa: E402


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="查看并解析 BOSS 候选人搜索筛选条件；不访问 BOSS。")
    subparsers = parser.add_subparsers(dest="command", required=True)
    list_parser = subparsers.add_parser("list", help="一次列出字段、选项和选择方式")
    list_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    resolve_parser = subparsers.add_parser("resolve", help="把明确的中文选择转换为搜索参数")
    resolve_parser.add_argument("--filter", action="append", required=True, dest="filters", help="字段=选项；可重复")
    resolve_parser.add_argument("--json", action="store_true", help="输出机器可读 JSON")
    return parser.parse_args(argv)


def render_catalog() -> str:
    fields = filter_catalog()
    available = [field for field in fields if field["status"] == "available"]
    pending = [field["label"] for field in fields if field["status"] != "available"]
    lines = ["搜索筛选条件", "未指定的条件默认不做筛选", ""]
    for index, field in enumerate(available):
        branch = "└─" if index == len(available) - 1 else "├─"
        continuation = "  " if index == len(available) - 1 else "│ "
        selection = {
            "multiple": "多选；不限不能与其他项并选",
            "range": "范围",
        }.get(field["selection"], "单选")
        if field["note"]:
            selection += f"；{field['note']}"
        source = "；项目扩展" if field["source"] == "project_extension" else ""
        lines.append(f"{branch} {field['label']}〔{selection}{source}〕")
        detail = field["description"] or " / ".join(option["label"] for option in field["options"])
        lines.append(f"{continuation} {detail}")
    if pending:
        lines.extend(("", "SDK 支持、中文选项映射待补充：" + "、".join(pending)))
    lines.extend(("", "选择示例：学历=本科及以上；活跃度=近一周活跃"))
    return "\n".join(lines)


def render_resolution(result: dict[str, object]) -> str:
    lines = ["本次搜索条件"]
    for selection in result["selections"]:  # type: ignore[index]
        labels = "、".join(selection["option_labels"])
        lines.append(f"  {selection['field_label']:<10} {labels}")
    lines.append("  其他条件   沿用当前设置")
    return "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if args.command == "list":
        payload = {
            "schema_version": 1,
            "contract": "recruiter_search_filter_catalog",
            "fields": filter_catalog(),
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else render_catalog())
        return 0
    result = resolve_filter_selections(args.filters)
    payload = {
        "schema_version": 1,
        "contract": "resolved_recruiter_search_filters",
        **result,
    }
    print(json.dumps(payload, ensure_ascii=False, indent=2) if args.json else render_resolution(result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
