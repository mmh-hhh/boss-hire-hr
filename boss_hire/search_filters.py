from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any


SEARCH_FILTER_FIELDS: tuple[dict[str, Any], ...] = (
    {
        "id": "degree",
        "label": "学历",
        "aliases": ("学历要求",),
        "parameter": "degree",
        "selection": "single",
        "status": "available",
        "options": (
            {"id": "any", "label": "不限", "aliases": (), "value": "201,201"},
            {"id": "bachelor_plus", "label": "本科及以上", "aliases": ("本科以上",), "value": "203,201"},
            {"id": "master_plus", "label": "硕士及以上", "aliases": ("硕士以上",), "value": "204,201"},
            {"id": "doctor", "label": "博士", "aliases": (), "value": "205,205"},
        ),
    },
    {
        "id": "school_level",
        "label": "院校要求",
        "aliases": ("院校", "学校层次"),
        "parameter": "school_level",
        "selection": "multiple",
        "status": "available",
        "options": (
            {"id": "any", "label": "不限", "aliases": (), "value": "-1"},
            {"id": "full_time_bachelor", "label": "统招本科", "aliases": (), "value": "1101"},
            {"id": "double_first_class", "label": "双一流", "aliases": (), "value": "1102"},
            {"id": "project_211", "label": "211院校", "aliases": ("211",), "value": "1103"},
            {"id": "project_985", "label": "985院校", "aliases": ("985",), "value": "1104"},
            {"id": "overseas_student", "label": "留学生", "aliases": ("留学",), "value": "1105"},
            {"id": "qs_500", "label": "QS 500", "aliases": ("QS500",), "value": "1106"},
            {"id": "qs_100", "label": "QS 100", "aliases": ("QS100",), "value": "1107"},
        ),
    },
    {
        "id": "experience",
        "label": "工作经验",
        "aliases": ("经验", "经验要求"),
        "parameter": "experience",
        "selection": "single",
        "status": "available",
        "options": (
            {"id": "any", "label": "不限", "aliases": (), "value": "-1,-1"},
            {"id": "student", "label": "在校或应届", "aliases": ("在校/应届", "在校应届", "应届"), "value": "-3,-3"},
            {"id": "one_to_three", "label": "1-3年", "aliases": ("1–3年",), "value": "1,3"},
            {"id": "three_to_five", "label": "3-5年", "aliases": ("3–5年",), "value": "3,5"},
            {"id": "five_to_ten", "label": "5-10年", "aliases": ("5–10年",), "value": "5,10"},
            {"id": "ten_plus", "label": "10年以上", "aliases": (), "value": "10,11"},
            {"id": "five_to_eight", "label": "5-8年", "aliases": ("5–8年",), "value": "5,8"},
            {"id": "eight_plus", "label": "8年以上", "aliases": (), "value": "8,11"},
        ),
    },
    {
        "id": "age",
        "label": "年龄",
        "aliases": ("年龄要求",),
        "parameter": "age",
        "selection": "single",
        "status": "available",
        "options": (
            {"id": "any", "label": "不限", "aliases": (), "value": "-1,-1"},
            {"id": "twenty_to_twenty_five", "label": "20-25岁", "aliases": ("20-25", "20–25岁"), "value": "20,25"},
            {"id": "twenty_five_to_thirty", "label": "25-30岁", "aliases": ("25-30", "25–30岁"), "value": "25,30"},
            {"id": "thirty_to_thirty_five", "label": "30-35岁", "aliases": ("30-35", "30–35岁"), "value": "30,35"},
            {"id": "thirty_five_to_forty", "label": "35-40岁", "aliases": ("35-40", "35–40岁"), "value": "35,40"},
            {"id": "forty_to_fifty", "label": "40-50岁", "aliases": ("40-50", "40–50岁"), "value": "40,50"},
            {"id": "fifty_plus", "label": "50岁以上", "aliases": ("50以上",), "value": "50,-1"},
        ),
    },
    {
        "id": "city",
        "label": "城市",
        "aliases": ("工作地点", "地点"),
        "parameter": "city",
        "selection": "single",
        "status": "available",
        "note": "热门城市",
        "options": (
            {"id": "nationwide", "label": "全国", "aliases": (), "value": "-1"},
            {"id": "beijing", "label": "北京", "aliases": (), "value": "101010100"},
            {"id": "shanghai", "label": "上海", "aliases": (), "value": "101020100"},
            {"id": "guangzhou", "label": "广州", "aliases": (), "value": "101280100"},
            {"id": "shenzhen", "label": "深圳", "aliases": (), "value": "101280600"},
            {"id": "hangzhou", "label": "杭州", "aliases": (), "value": "101210100"},
            {"id": "chengdu", "label": "成都", "aliases": (), "value": "101270100"},
            {"id": "nanjing", "label": "南京", "aliases": (), "value": "101190100"},
            {"id": "wuhan", "label": "武汉", "aliases": (), "value": "101200100"},
            {"id": "xian", "label": "西安", "aliases": (), "value": "101110100"},
            {"id": "suzhou", "label": "苏州", "aliases": (), "value": "101190400"},
            {"id": "changsha", "label": "长沙", "aliases": (), "value": "101250100"},
            {"id": "zhengzhou", "label": "郑州", "aliases": (), "value": "101180100"},
            {"id": "chongqing", "label": "重庆", "aliases": (), "value": "101040100"},
            {"id": "tianjin", "label": "天津", "aliases": (), "value": "101030100"},
            {"id": "hefei", "label": "合肥", "aliases": (), "value": "101220100"},
            {"id": "xiamen", "label": "厦门", "aliases": (), "value": "101230200"},
        ),
    },
    {
        "id": "salary",
        "label": "薪资",
        "aliases": ("薪资区间",),
        "parameter": "salary",
        "selection": "range",
        "status": "available",
        "resolver": "salary_range",
        "description": "不选择为不限-不限；格式如 20K-30K、20K-不限、不限-30K；边界为1K-50K（每1K）及60K/70K/80K/90K/100K/150K/200K",
        "options": (),
    },
    {
        "id": "activeness",
        "label": "活跃度",
        "aliases": ("活跃程度", "牛人活跃度"),
        "parameter": "activeness",
        "selection": "single",
        "status": "available",
        "options": (
            {"id": "any", "label": "不限", "aliases": (), "value": "0"},
            {"id": "just_now", "label": "刚刚活跃", "aliases": (), "value": "1"},
            {"id": "today", "label": "今日活跃", "aliases": (), "value": "2"},
            {"id": "three_days", "label": "3日内活跃", "aliases": ("三日内活跃",), "value": "3"},
            {"id": "one_week", "label": "近一周活跃", "aliases": ("一周内活跃",), "value": "4"},
            {"id": "one_month", "label": "近一个月活跃", "aliases": ("一个月内活跃",), "value": "5"},
        ),
    },
    {
        "id": "gender",
        "label": "性别",
        "aliases": (),
        "parameter": "gender",
        "selection": "single",
        "status": "available",
        "source": "safe_client",
        "options": (
            {"id": "any", "label": "不限", "aliases": (), "value": "-1"},
            {"id": "female", "label": "女", "aliases": (), "value": "0"},
            {"id": "male", "label": "男", "aliases": (), "value": "1"},
        ),
    },
    {
        "id": "apply_status",
        "label": "求职状态",
        "aliases": ("状态",),
        "parameter": "apply_status",
        "selection": "multiple",
        "status": "available",
        "source": "safe_client",
        "options": (
            {"id": "any", "label": "不限", "aliases": (), "value": "-1"},
            {"id": "available", "label": "离职-随时到岗", "aliases": (), "value": "701"},
            {"id": "not_considering", "label": "在职-暂不考虑", "aliases": (), "value": "702"},
            {"id": "considering", "label": "在职-考虑机会", "aliases": (), "value": "703"},
            {"id": "within_month", "label": "在职-月内到岗", "aliases": (), "value": "704"},
        ),
    },
    {
        "id": "switch_frequency",
        "label": "跳槽频率",
        "aliases": (),
        "parameter": "switch_frequency",
        "selection": "single",
        "status": "available",
        "source": "safe_client",
        "options": (
            {"id": "any", "label": "不限", "aliases": (), "value": "0"},
            {"id": "under_three_jobs_five_years", "label": "5年少于3份", "aliases": (), "value": "1"},
            {"id": "average_one_year_plus", "label": "时间大于等于1年", "aliases": ("平均每份工作1年以上",), "value": "2"},
        ),
    },
    {
        "id": "geek_job_requirements",
        "label": "牛人职位要求",
        "aliases": ("职位要求",),
        "parameter": "geek_job_requirements",
        "selection": "multiple",
        "status": "available",
        "source": "safe_client",
        "options": (
            {"id": "any", "label": "不限", "aliases": (), "value": "0"},
            {"id": "worked_before", "label": "仅从事过此职位", "aliases": (), "value": "1"},
            {"id": "most_recent", "label": "最近从事此职位", "aliases": (), "value": "2"},
            {"id": "expected", "label": "牛人期望此职位", "aliases": (), "value": "3"},
        ),
    },
    {
        "id": "recent_view_filter",
        "label": "过滤近14天查看",
        "aliases": ("近14天查看",),
        "parameter": "recent_view_filter",
        "selection": "single",
        "status": "available",
        "source": "project_extension",
        "options": (
            {"id": "disabled", "label": "关闭", "aliases": ("不开启",), "value": "include_all"},
            {"id": "enabled", "label": "开启", "aliases": ("过滤",), "value": "exclude_14d"},
        ),
    },
)

def filter_catalog(fields: Sequence[Mapping[str, Any]] = SEARCH_FILTER_FIELDS) -> list[dict[str, Any]]:
    return [
        {
            "id": str(field["id"]),
            "label": str(field["label"]),
            "selection": str(field["selection"]),
            "status": str(field["status"]),
            "source": str(field.get("source") or "sdk"),
            "note": str(field.get("note") or ""),
            "description": str(field.get("description") or ""),
            "options": [
                {"id": str(option["id"]), "label": str(option["label"])}
                for option in field.get("options") or ()
            ],
        }
        for field in fields
    ]


def resolve_filter_selections(
    selections: Sequence[str],
    fields: Sequence[Mapping[str, Any]] = SEARCH_FILTER_FIELDS,
) -> dict[str, Any]:
    field_index = _alias_index(fields, kind="字段")
    resolved: list[dict[str, Any]] = []
    sdk_kwargs: dict[str, str] = {}
    seen_fields: set[str] = set()

    for raw_selection in selections:
        field_name, separator, raw_options = str(raw_selection or "").partition("=")
        if not separator or not field_name.strip() or not raw_options.strip():
            raise ValueError(f"筛选条件必须使用 字段=选项：{raw_selection}")
        field = field_index.get(_alias_key(field_name))
        if field is None:
            raise ValueError(f"未知筛选字段：{field_name.strip()}")
        field_id = str(field["id"])
        if field_id in seen_fields:
            raise ValueError(f"筛选字段重复设置：{field['label']}")
        seen_fields.add(field_id)
        if field.get("status") != "available":
            raise ValueError(f"筛选字段尚未提供可用选项：{field['label']}")

        if field.get("resolver") == "salary_range":
            options = [_resolve_salary_range(raw_options)]
        else:
            option_index = _alias_index(field.get("options") or (), kind=f"{field['label']}选项")
            option_names = [part.strip() for part in raw_options.split(",") if part.strip()]
            if field.get("selection") == "single" and len(option_names) != 1:
                raise ValueError(f"筛选字段仅支持单选：{field['label']}")
            options = []
            seen_options: set[str] = set()
            for option_name in option_names:
                option = option_index.get(_alias_key(option_name))
                if option is None:
                    raise ValueError(f"未知{field['label']}选项：{option_name}")
                option_id = str(option["id"])
                if option_id not in seen_options:
                    options.append(option)
                    seen_options.add(option_id)
        if len(options) > 1 and any(str(option["id"]) == "any" for option in options):
            raise ValueError(f"不限不能与其他{field['label']}选项同时选择")

        parameter = str(field["parameter"])
        value = ",".join(str(option["value"]) for option in options)
        sdk_kwargs[parameter] = value
        resolved.append(
            {
                "field_id": field_id,
                "field_label": str(field["label"]),
                "parameter": parameter,
                "option_ids": [str(option["id"]) for option in options],
                "option_labels": [str(option["label"]) for option in options],
                "value": value,
            }
        )

    return {"selections": resolved, "sdk_kwargs": sdk_kwargs}


def validate_resolved_filter_payload(
    selections: object,
    sdk_kwargs: object,
    recent_view_filter: object | None = None,
    fields: Sequence[Mapping[str, Any]] = SEARCH_FILTER_FIELDS,
) -> dict[str, Any]:
    if not isinstance(selections, list) or not isinstance(sdk_kwargs, Mapping):
        raise ValueError("搜索筛选冻结数据无效")
    raw: list[str] = []
    for selection in selections:
        if not isinstance(selection, Mapping):
            raise ValueError("搜索筛选冻结条目无效")
        field_id = str(selection.get("field_id") or "").strip()
        option_ids = selection.get("option_ids")
        if not field_id or not isinstance(option_ids, list) or not option_ids:
            raise ValueError("搜索筛选冻结条目不完整")
        field = next((item for item in fields if str(item.get("id")) == field_id), None)
        if field is not None and field.get("resolver") == "salary_range":
            option_labels = selection.get("option_labels")
            if not isinstance(option_labels, list) or len(option_labels) != 1:
                raise ValueError("搜索筛选冻结条目不完整")
            raw.append(f"{field_id}={option_labels[0]}")
        else:
            raw.append(f"{field_id}=" + ",".join(str(option_id) for option_id in option_ids))
    canonical = resolve_filter_selections(raw, fields)
    canonical_kwargs = dict(canonical["sdk_kwargs"])
    canonical_recent_view_filter = canonical_kwargs.pop("recent_view_filter", None)
    if (
        canonical["selections"] != selections
        or validate_search_filter_params(canonical_kwargs, fields) != dict(sdk_kwargs)
        or (
            canonical_recent_view_filter is not None
            and canonical_recent_view_filter != recent_view_filter
        )
    ):
        raise ValueError("搜索筛选冻结数据与当前定义不一致")
    return canonical


def validate_search_filter_params(
    value: object,
    fields: Sequence[Mapping[str, Any]] = SEARCH_FILTER_FIELDS,
) -> dict[str, str]:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValueError("搜索筛选参数必须是对象")
    available = {
        str(field["parameter"]): field
        for field in fields
        if field["status"] == "available" and field.get("parameter") != "recent_view_filter"
    }
    result: dict[str, str] = {}
    for key, raw in value.items():
        parameter = str(key or "").strip()
        parameter_value = str(raw or "").strip()
        field = available.get(parameter)
        if field is None:
            raise ValueError(f"搜索筛选参数不受支持：{parameter}")
        if not parameter_value:
            raise ValueError(f"搜索筛选参数不能为空：{parameter}")
        allowed_values = {str(option["value"]) for option in field.get("options") or ()}
        if field.get("resolver") == "salary_range":
            encoded_range = parameter_value.replace(",", "-", 1)
            if _resolve_salary_range(encoded_range)["value"] != parameter_value:
                raise ValueError(f"搜索筛选参数值无效：{parameter}")
        elif field.get("selection") == "multiple":
            selected_values = parameter_value.split(",")
            if (
                len(selected_values) != len(set(selected_values))
                or not set(selected_values) <= allowed_values
                or (len(selected_values) > 1 and "-1" in selected_values)
            ):
                raise ValueError(f"搜索筛选参数值无效：{parameter}")
        elif parameter_value not in allowed_values:
            raise ValueError(f"搜索筛选参数值无效：{parameter}")
        result[parameter] = parameter_value
    return {key: result[key] for key in sorted(result)}


def city_name_for_code(value: object) -> str:
    code = str(value or "").strip()
    city = next(field for field in SEARCH_FILTER_FIELDS if field["id"] == "city")
    for option in city["options"]:
        if option["value"] == code:
            return str(option["label"])
    if code == "-2":
        return ""
    raise ValueError("城市编码不受支持")


def _resolve_salary_range(value: object) -> dict[str, str]:
    text = str(value or "").strip().upper().replace("–", "-").replace("—", "-")
    parts = [part.strip() for part in text.split("-")]
    if len(parts) != 2 or not all(parts):
        raise ValueError("薪资必须使用 最低-最高，例如 20K-30K")
    boundaries = set(range(1, 51)) | {60, 70, 80, 90, 100, 150, 200}

    def boundary(part: str) -> tuple[int, str]:
        if part == "不限":
            return -1, "不限"
        number = part[:-1] if part.endswith("K") else part
        if not number.isdigit() or int(number) not in boundaries:
            raise ValueError("薪资边界不受支持")
        return int(number), f"{int(number)}K"

    minimum, minimum_label = boundary(parts[0])
    maximum, maximum_label = boundary(parts[1])
    if minimum != -1 and maximum != -1 and minimum > maximum:
        raise ValueError("薪资最低值不能超过最高值")
    return {
        "id": f"range:{minimum}:{maximum}",
        "label": f"{minimum_label}-{maximum_label}",
        "value": f"{minimum},{maximum}",
    }


def _alias_index(values: Sequence[Mapping[str, Any]], *, kind: str) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    for value in values:
        names = (value.get("id"), value.get("label"), *(value.get("aliases") or ()))
        for name in names:
            key = _alias_key(name)
            if not key:
                continue
            if key in result and result[key] is not value:
                raise ValueError(f"{kind}别名重复：{name}")
            result[key] = value
    return result


def _alias_key(value: object) -> str:
    return "".join(str(value or "").strip().lower().split())
