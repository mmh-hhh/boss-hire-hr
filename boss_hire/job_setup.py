"""Local job selection state. No authentication or network clients."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from boss_hire.local_security import atomic_write_json


def read_selected_job(work_root: Path, *, account_key: str) -> dict[str, Any] | None:
    path = work_root / 'selected_job.json'
    if not path.exists():
        return None
    value = json.loads(path.read_text())
    if not isinstance(value, dict) or value.get('account_key') != account_key or not value.get('job_id'):
        raise ValueError('所选岗位记录无效或账号命名空间不一致；请人工核对，不能继续旧数据')
    return value


def resolve_prepared_job(work_root: Path, artifacts: dict, *, account_key: str) -> tuple[str, dict]:
    selected = read_selected_job(work_root, account_key=account_key)
    if selected:
        job_id = selected['job_id']
        value = artifacts.get(job_id)
        if not isinstance(value, dict) or (selected.get('source_jd_hash') and value.get('source_jd_hash') != selected['source_jd_hash']):
            raise ValueError('所选岗位尚未准备好或 JD 已变化；请运行 prepare-job，未开始来源读取')
        return job_id, value
    if len(artifacts) == 1:
        return next(iter(artifacts.items()))
    raise ValueError('请先使用 jobs 选择岗位并运行 prepare-job；不会猜测岗位')


def build_job_read_plan(*, account_key: str, board_date: str, summary: dict | None = None) -> dict:
    from boss_hire.state_store import content_hash
    kind = 'job_catalog' if summary is None else 'job_snapshot'
    if summary is not None and (not summary.get('encrypt_job_id') or summary.get('online_status') != 1):
        raise ValueError('必须选择一个开放岗位')
    operation = {
        'operation_key': 'metadata:open-jobs' if summary is None else 'metadata:selected-job-detail',
        'request_class': 'metadata', 'method': 'GET',
        'endpoint_name': 'list_jobs' if summary is None else 'job_detail',
        'binding': {} if summary is None else {'job_id': summary['encrypt_job_id']},
    }
    value = {'schema_version': 2, 'contract': 'single_job_live_run_plan', 'plan_kind': kind,
             'operation_manifest_kind': kind, 'account_key': account_key, 'board_date': board_date,
             'summary': summary, 'operation_manifest': [operation]}
    value['plan_id'] = 'job-setup-' + content_hash(value)[:16]
    return value


def run_job_read(*, plan: dict, client: Any, work_dir: Path, generated_at: str) -> dict:
    from boss_hire.boss_live_authorization import _operation_manifest
    from boss_hire.recruiter_jobs import normalize_job_summary, normalize_job_detail
    from boss_hire.state_store import jd_hash
    manifest = _operation_manifest(plan)
    with client.operation(manifest[0]['operation_key']):
        if plan['plan_kind'] == 'job_catalog':
            response = client.list_jobs()
            if not isinstance(response, dict) or response.get('code') != 0 or not isinstance(response.get('zpData'), list):
                raise ValueError('岗位列表读取失败或格式无效；没有继续请求')
            summaries = [normalize_job_summary(row) for row in response['zpData']]
            if len({row['encrypt_job_id'] for row in summaries}) != len(summaries):
                raise ValueError('岗位列表身份重复；请人工核对')
            artifact = {'jobs': [row for row in summaries if row['online_status'] == 1]}
        else:
            summary = plan['summary']
            job = normalize_job_detail(summary, client.job_detail(summary['encrypt_job_id']))
            artifact = {'job_id': job.encrypt_job_id, 'job': job.to_dict(), 'source_jd_hash': jd_hash(job.to_jd_text())}
    artifact.update(contract=plan['plan_kind'], account_key=plan['account_key'],
                    board_date=plan['board_date'], generated_at=generated_at)
    path = work_dir / 'result.json'
    atomic_write_json(path, artifact, sort_keys=True)
    return {'artifact': artifact, 'artifact_path': str(path)}


def load_catalog(work_root: Path, *, account_key: str, board_date: str) -> dict:
    path = work_root / 'job_catalog.json'
    if not path.is_file():
        raise ValueError('尚无岗位列表；请本人运行 jobs --refresh')
    value = json.loads(path.read_text())
    if (not isinstance(value, dict) or value.get('contract') != 'job_catalog'
        or value.get('account_key') != account_key or value.get('board_date') != board_date
        or not isinstance(value.get('jobs'), list)):
        raise ValueError('岗位列表跨日、格式无效或账号不匹配；请本人重新运行 jobs --refresh')
    return value
