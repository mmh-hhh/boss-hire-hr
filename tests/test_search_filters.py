from __future__ import annotations

import unittest
import json
import subprocess
import sys
from pathlib import Path

from boss_hire.search_filters import (
    SEARCH_FILTER_FIELDS,
    filter_catalog,
    resolve_filter_selections,
    validate_resolved_filter_payload,
    validate_search_filter_params,
)


class SearchFilterTests(unittest.TestCase):
    def test_resolves_confirmed_chinese_options_to_sdk_kwargs(self) -> None:
        result = resolve_filter_selections(
            [
                "学历=本科以上",
                "院校=211,985院校",
                "经验=8年以上",
                "年龄=50以上",
                "城市=上海",
                "薪资=20K-30K",
                "活跃度=近一周活跃",
                "性别=女",
                "求职状态=离职-随时到岗,在职-考虑机会",
                "跳槽频率=5年少于3份",
                "职位要求=仅从事过此职位,最近从事此职位",
            ]
        )

        self.assertEqual(
            result["sdk_kwargs"],
            {
                "degree": "203,201",
                "school_level": "1103,1104",
                "experience": "8,11",
                "age": "50,-1",
                "city": "101020100",
                "salary": "20,30",
                "activeness": "4",
                "gender": "0",
                "apply_status": "701,703",
                "switch_frequency": "1",
                "geek_job_requirements": "1,2",
            },
        )
        self.assertEqual(result["selections"][0]["option_labels"], ["本科及以上"])

    def test_rejects_unknown_pending_repeated_and_conflicting_options(self) -> None:
        cases = (
            (["专业=汽配"], "未知筛选字段"),
            (["薪资=30K-20K"], "最低值不能超过"),
            (["薪资=55K-不限"], "边界不受支持"),
            (["学历=本科及以上", "学历=硕士及以上"], "重复设置"),
            (["院校=不限,985"], "不限不能与其他"),
            (["学历=本科及以上,硕士及以上"], "仅支持单选"),
            (["学历本科及以上"], "字段=选项"),
        )
        for selections, error in cases:
            with self.subTest(selections=selections), self.assertRaisesRegex(ValueError, error):
                resolve_filter_selections(selections)

    def test_catalog_and_resolver_share_extensible_field_definition(self) -> None:
        extra = {
            "id": "sample",
            "label": "示例字段",
            "aliases": (),
            "parameter": "sample_parameter",
            "selection": "single",
            "status": "available",
            "options": (
                {"id": "sample_option", "label": "示例选项", "aliases": (), "value": "sample-value"},
            ),
        }
        fields = (*SEARCH_FILTER_FIELDS, extra)

        self.assertEqual(filter_catalog(fields)[-1]["label"], "示例字段")
        self.assertEqual(
            resolve_filter_selections(["示例字段=示例选项"], fields)["sdk_kwargs"],
            {"sample_parameter": "sample-value"},
        )

    def test_recent_view_filter_reuses_existing_semantic_values(self) -> None:
        self.assertEqual(
            resolve_filter_selections(["过滤近14天查看=开启"])["sdk_kwargs"],
            {"recent_view_filter": "exclude_14d"},
        )

    def test_frozen_payload_rejects_label_or_parameter_tampering(self) -> None:
        resolved = resolve_filter_selections(["学历=本科及以上"])
        self.assertEqual(
            validate_resolved_filter_payload(resolved["selections"], resolved["sdk_kwargs"]),
            resolved,
        )
        tampered = [{**resolved["selections"][0], "value": "204,201"}]
        with self.assertRaisesRegex(ValueError, "当前定义不一致"):
            validate_resolved_filter_payload(tampered, resolved["sdk_kwargs"])
        with self.assertRaisesRegex(ValueError, "参数值无效"):
            validate_search_filter_params({"degree": "999,999"})

    def test_cli_lists_all_options_at_once_and_resolves_json(self) -> None:
        script = Path(__file__).resolve().parents[1] / "scripts" / "search_filters.py"
        listed = subprocess.run(
            [sys.executable, str(script), "list"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        self.assertIn("├─ 学历〔单选〕", listed)
        self.assertIn("本科及以上 / 硕士及以上 / 博士", listed)
        self.assertIn("院校要求〔多选；不限不能与其他项并选〕", listed)
        self.assertIn("城市〔单选；热门城市〕", listed)
        self.assertIn("薪资〔范围〕", listed)
        self.assertIn("不选择为不限-不限", listed)
        self.assertIn("性别〔单选〕", listed)
        self.assertIn("求职状态〔多选；不限不能与其他项并选〕", listed)
        self.assertIn("选择示例：学历=本科及以上；活跃度=近一周活跃", listed)
        self.assertNotIn("--options", listed)

        resolved = subprocess.run(
            [sys.executable, str(script), "resolve", "--filter", "学历=本科及以上", "--json"],
            check=True,
            capture_output=True,
            text=True,
        ).stdout
        payload = json.loads(resolved)
        self.assertEqual(payload["contract"], "resolved_recruiter_search_filters")
        self.assertEqual(payload["sdk_kwargs"], {"degree": "203,201"})

    def test_salary_unselected_is_omitted_and_explicit_unlimited_is_supported(self) -> None:
        self.assertEqual(resolve_filter_selections([])["sdk_kwargs"], {})
        self.assertEqual(
            resolve_filter_selections(["薪资=不限-不限"])["sdk_kwargs"],
            {"salary": "-1,-1"},
        )


if __name__ == "__main__":
    unittest.main()
