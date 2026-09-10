from __future__ import annotations

from dataclasses import asdict, dataclass
from contextlib import nullcontext
from typing import Any


OPEN_JOB_ONLINE_STATUS = 1
EXPERIENCE_CODE_LABELS = {
    101: "不限",
    102: "应届生",
    103: "1年以内",
    104: "1-3年",
    105: "3-5年",
    106: "5-10年",
    107: "10年以上",
}
DEGREE_CODE_LABELS = {
    201: "不限",
    202: "高中及以下",
    203: "本科",
    204: "硕士",
    205: "博士",
    206: "大专",
}


def _text(value: Any) -> str:
    return "" if value is None else str(value).strip()


def _integer(value: Any) -> int | None:
    if value in (None, ""):
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _successful_data(response: Any, operation: str) -> Any:
    if not isinstance(response, dict) or response.get("code") != 0:
        raise RuntimeError(f"{operation} failed: {response}")
    return response.get("zpData")


@dataclass(frozen=True)
class RecruiterJob:
    encrypt_job_id: str
    name: str
    online_status: int
    detail_status: int | None
    description: str
    position_name: str
    city: str
    address: str
    salary_low_k: int | None
    salary_high_k: int | None
    salary_months: int | None
    experience_code: int | None
    experience_label: str
    degree_code: int | None
    degree_label: str

    @property
    def is_open(self) -> bool:
        return self.online_status == OPEN_JOB_ONLINE_STATUS

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    def to_jd_text(self) -> str:
        metadata = [
            f"岗位名称：{self.name}",
            f"岗位方向：{self.position_name}" if self.position_name else "",
            f"工作城市：{self.city}" if self.city else "",
            f"平台经验要求：{self.experience_label}" if self.experience_label else "",
            f"平台学历要求：{self.degree_label}" if self.degree_label else "",
            (
                f"薪资范围：{self.salary_low_k}-{self.salary_high_k}K"
                if self.salary_low_k is not None and self.salary_high_k is not None
                else ""
            ),
        ]
        return "\n".join([line for line in metadata if line] + ["", self.description]).strip() + "\n"


def normalize_job_summary(item: Any) -> dict[str, Any]:
    if not isinstance(item, dict):
        raise ValueError(f"job summary must be an object: {item!r}")
    encrypt_job_id = _text(item.get("encryptJobId"))
    name = _text(item.get("jobName"))
    online_status = _integer(item.get("jobOnlineStatus"))
    if not encrypt_job_id or not name or online_status is None:
        raise ValueError(f"job summary is missing identity/status: {item}")
    return {
        "encrypt_job_id": encrypt_job_id,
        "name": name,
        "online_status": online_status,
    }


def normalize_job_detail(summary: dict[str, Any], response: Any) -> RecruiterJob:
    data = _successful_data(response, "job_detail")
    job = data.get("job") if isinstance(data, dict) else None
    if not isinstance(job, dict):
        raise RuntimeError(f"job_detail missing zpData.job: {response}")

    summary_id = _text(summary.get("encrypt_job_id"))
    detail_id = _text(job.get("encryptId"))
    if not detail_id or detail_id != summary_id:
        raise RuntimeError(f"job_detail identity mismatch: summary={summary_id}, detail={detail_id}")

    description = _text(job.get("postDescription"))
    if not description:
        raise RuntimeError(f"open job {summary_id} has no postDescription")

    experience_code = _integer(job.get("experience"))
    degree_code = _integer(job.get("degree"))
    return RecruiterJob(
        encrypt_job_id=detail_id,
        name=_text(job.get("jobName")) or _text(summary.get("name")),
        online_status=int(summary["online_status"]),
        detail_status=_integer(job.get("jobStatus")),
        description=description,
        position_name=_text(job.get("positionName")),
        city=_text(job.get("locationName")),
        address=_text(job.get("addressText")),
        salary_low_k=_integer(job.get("lowSalary")),
        salary_high_k=_integer(job.get("highSalary")),
        salary_months=_integer(job.get("salaryMonth")),
        experience_code=experience_code,
        experience_label=EXPERIENCE_CODE_LABELS.get(experience_code, ""),
        degree_code=degree_code,
        degree_label=DEGREE_CODE_LABELS.get(degree_code, ""),
    )


def fetch_open_jobs(client: Any) -> list[RecruiterJob]:
    data = _successful_data(client.list_jobs(), "list_jobs")
    if not isinstance(data, list):
        raise RuntimeError(f"list_jobs zpData must be a list: {data!r}")

    summaries = [normalize_job_summary(item) for item in data]
    open_summaries = [
        summary
        for summary in summaries
        if summary["online_status"] == OPEN_JOB_ONLINE_STATUS
    ]
    return [
        normalize_job_detail(summary, client.job_detail(summary["encrypt_job_id"]))
        for summary in open_summaries
    ]


def fetch_single_open_job(client: Any) -> RecruiterJob:
    """Fail before job-detail reads unless the account has exactly one open job."""
    operation = getattr(client, "operation", None)
    list_context = operation("metadata:open-jobs") if callable(operation) else nullcontext()
    with list_context:
        list_response = client.list_jobs()
    data = _successful_data(list_response, "list_jobs")
    if not isinstance(data, list):
        raise RuntimeError(f"list_jobs zpData must be a list: {data!r}")
    summaries = [normalize_job_summary(item) for item in data]
    open_summaries = [row for row in summaries if row["online_status"] == OPEN_JOB_ONLINE_STATUS]
    if len(open_summaries) != 1:
        raise RuntimeError(f"single-job run requires exactly one open job; found {len(open_summaries)}")
    summary = open_summaries[0]
    detail_context = (
        operation("metadata:single-open-job-detail") if callable(operation) else nullcontext()
    )
    with detail_context:
        detail_response = client.job_detail(summary["encrypt_job_id"])
    return normalize_job_detail(summary, detail_response)


def fetch_selected_open_job(client: Any, job_id: str) -> RecruiterJob:
    """Read the finite list, then exactly the explicitly selected open job."""
    operation = getattr(client, "operation", None)
    with operation("metadata:open-jobs") if callable(operation) else nullcontext():
        data = _successful_data(client.list_jobs(), "list_jobs")
    if not isinstance(data, list):
        raise ValueError("岗位列表格式无效")
    matches = [row for row in map(normalize_job_summary, data) if row["encrypt_job_id"] == job_id]
    if len(matches) != 1 or matches[0]["online_status"] != OPEN_JOB_ONLINE_STATUS:
        raise ValueError("所选岗位已关闭、不在列表中或身份重复；未读取详情，请重新核对岗位")
    with operation("metadata:selected-job-detail") if callable(operation) else nullcontext():
        return normalize_job_detail(matches[0], client.job_detail(job_id))
