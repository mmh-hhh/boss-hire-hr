#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from boss_hire.boss_access import BossLiveAccessDenied, account_key_for, preflight_boss_access  # noqa: E402
from boss_hire.boss_auth_sync import load_saved_session_fingerprint, sync_auth_from_chrome  # noqa: E402
from boss_hire.boss_live_authorization import (  # noqa: E402
    FIXED_AUTH_DIR,
    FIXED_GUARD_DIR,
    LiveAuthorizationStore,
    favorite_account_state_dir,
)
from boss_hire.favorite_delivery import (  # noqa: E402
    FavoriteDeliveryLedger,
    build_synced_favorite_delivery_plan,
    migrate_favorite_delivery_ledger,
)
from boss_hire.favorite_registry import FavoriteRegistry  # noqa: E402
from boss_hire.local_security import atomic_write_json  # noqa: E402
from boss_hire.run_reporting import build_single_job_report, format_single_job_report  # noqa: E402
from boss_hire.llm_confirmation import (  # noqa: E402
    build_llm_confirmation_preview,
    build_llm_confirmation_receipt,
    verify_llm_confirmation_preview,
)
from boss_hire.local_scoring import DEFAULT_LLM_WORKERS, score_inventory_resumes  # noqa: E402
from boss_hire.single_job_llm import OpenAICompatibleJsonLlm  # noqa: E402
from boss_hire.single_job_live import (  # noqa: E402
    execute_favorite_candidate,
    run_candidate_detail_collection,
    run_favorite_delivery,
    run_favorite_registry_sync,
    run_single_job_source_collection,
)
from boss_hire.single_job_run_plan import (  # noqa: E402
    build_favorite_delivery_operation_manifest,
    build_single_job_run_plan,
    load_single_job_run_plan,
    read_single_job_run_config,
    write_single_job_run_plan,
)
from boss_hire.supply_inventory import CandidateInventory  # noqa: E402
from boss_hire.state_store import content_hash  # noqa: E402
from boss_hire.workflow_run import (  # noqa: E402
    TERMINAL_RUN_STATUSES,
    LoadedWorkflowRun,
    ResolvedRunConfig,
    WorkflowPaths,
    bind_workflow_run_artifacts,
    build_workflow_run_state,
    create_workflow_run_with_artifacts,
    list_workflow_run_ids,
    load_active_workflow_run,
    load_workflow_run,
    resolve_run_config,
    save_active_workflow_run,
    save_workflow_run,
    transition_workflow_run_state,
    workflow_paths,
)
from boss_hire.workflow_status import build_workflow_status  # noqa: E402
from boss_hire.workflow_status import (  # noqa: E402
    freeze_detail_plan,
    prepare_detail_action,
    verify_frozen_detail_plan,
)


SHANGHAI = ZoneInfo("Asia/Shanghai")
Clock = Callable[[], datetime]


def _report_hints(*, scoring_complete: bool) -> tuple[str, ...]:
    hints = [
        "查看本轮供给漏斗与评分（本地只读，BOSS 0 次、LLM 0 次）："
        ".venv/bin/python scripts/run_single_job_live.py report",
    ]
    if scoring_complete:
        hints.append(
            "查看当前活动岗位总池评分与本地收藏状态："
            ".venv/bin/python scripts/run_single_job_live.py pool"
        )
    return tuple(hints)


def _load_env_file(path: Path) -> None:
    if not path.is_file():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


def _scoring_settings() -> dict[str, Any]:
    _load_env_file(ROOT / ".env")
    base_url = str(os.environ.get("OPENAI_BASE_URL") or "").strip()
    api_key = str(os.environ.get("OPENAI_API_KEY") or "").strip()
    model = str(os.environ.get("OPENAI_MODEL") or "gpt-5.4-mini").strip()
    try:
        workers = int(os.environ.get("BOSS_HIRE_LLM_WORKERS") or DEFAULT_LLM_WORKERS)
    except ValueError as exc:
        raise ValueError("BOSS_HIRE_LLM_WORKERS 必须是整数") from exc
    if not base_url or not api_key:
        raise ValueError("本地评分需要在 .env 配置 OPENAI_BASE_URL 和 OPENAI_API_KEY")
    return {
        "base_url": base_url,
        "api_key": api_key,
        "model": model,
        "workers": workers,
    }


@dataclass(frozen=True)
class StartPreparation:
    paths: WorkflowPaths
    config: ResolvedRunConfig
    plan: dict[str, Any]
    job_id: str
    source_jd_hash: str
    rubric: dict[str, Any]
    search_plan: dict[str, Any]


def _prepare_start(
    *,
    config_name: str,
    paths: WorkflowPaths,
    board_date: str,
    search_filter_inputs: Sequence[str] = (),
) -> StartPreparation:
    config = resolve_run_config(config_name, config_root=paths.config_root)
    active = load_active_workflow_run(paths)
    if active is not None and active.state["status"] not in TERMINAL_RUN_STATUSES:
        raise ValueError(f"已有活动运行：{active.state['run_id']}；请先使用 status/continue")
    inventory = CandidateInventory.load(paths.inventory_path).to_dict()
    job_artifacts = inventory.get("job_artifacts") or {}
    from boss_hire.job_setup import resolve_prepared_job
    if not isinstance(job_artifacts, dict):
        raise ValueError("岗位库存无效")
    job_id, artifacts = resolve_prepared_job(
        paths.work_root, job_artifacts, account_key=account_key_for(FIXED_AUTH_DIR),
    )
    if not isinstance(artifacts, dict):
        raise ValueError("当前岗位产物无效")
    source_jd_hash = str(artifacts.get("source_jd_hash") or "").strip()
    rubric = artifacts.get("rubric")
    search_plan = artifacts.get("search_plan")
    if not source_jd_hash or not isinstance(rubric, dict) or not isinstance(search_plan, dict):
        raise ValueError("当前岗位缺少 JD/rubric/search plan 产物")
    if rubric.get("source_jd_hash") != source_jd_hash:
        raise ValueError("当前 rubric 与 JD 摘要不一致")
    if search_plan.get("source_jd_hash") != source_jd_hash:
        raise ValueError("当前 search plan 与 JD 摘要不一致")
    plan = build_single_job_run_plan(
        board_date=board_date,
        config=read_single_job_run_config(config.path),
        auth_dir=FIXED_AUTH_DIR,
        search_job_id=str(job_id),
        selected_job_id=str(job_id),
        persisted_search_plan=search_plan,
        search_filter_inputs=search_filter_inputs,
    )
    plan["selected_job_jd_hash"] = source_jd_hash
    work_root = str(paths.work_root.resolve(strict=False))
    plan["execution_policy"] = {
        **dict(plan.get("execution_policy") or {}),
        "fixed_auth_namespace": True,
        "fixed_guard_namespace": True,
        "authorization_mode": "one_time_inline",
        "work_root_digest": hashlib.sha256(work_root.encode("utf-8")).hexdigest()[:16],
    }
    return StartPreparation(
        paths=paths,
        config=config,
        plan=plan,
        job_id=str(job_id),
        source_jd_hash=source_jd_hash,
        rubric=dict(rubric),
        search_plan=dict(search_plan),
    )


def _render_start_preview(preparation: StartPreparation, output_fn: Callable[[str], None]) -> str:
    plan = preparation.plan
    operation_count = len(plan["operation_manifest"])
    output_fn(f"配置：{preparation.config.name}")
    output_fn(f"岗位：{preparation.job_id}")
    output_fn(
        "来源："
        f"推荐 {'开' if plan['recommendation_source_enabled'] else '关'}，"
        f"搜索词 {plan['selected_search_query_count']}/"
        f"{plan['top_priority_search_query_count']} 个，"
        f"第二页路线 {plan['selected_second_page_search_query_count']}/"
        f"{plan['second_page_search_query_count']} 个，"
        f"近14天已查看过滤 {plan.get('recent_view_filter', 'include_all')}"
    )
    if plan.get("search_filters"):
        output_fn(
            "搜索条件："
            + "；".join(
                f"{row['field_label']}={','.join(row['option_labels'])}"
                for row in plan["search_filters"]
            )
        )
    output_fn(f"本阶段将严格串行执行 {operation_count} 次 BOSS 只读操作；不重试、不跟随 hasMore、不请求第 3 页。")
    expected = f"确认来源{operation_count}次"
    output_fn(f"确认文本：{expected}")
    return expected


def _complete_start_after_confirmation(_preparation: StartPreparation, _current: datetime) -> int:
    preparation = _preparation
    current = _current
    from boss_hire.job_setup import resolve_prepared_job
    current_inventory = CandidateInventory.load(preparation.paths.inventory_path).to_dict()
    current_job, current_artifacts = resolve_prepared_job(
        preparation.paths.work_root, current_inventory.get("job_artifacts", {}),
        account_key=account_key_for(FIXED_AUTH_DIR),
    )
    if (current_job != preparation.job_id or current_artifacts.get("source_jd_hash") != preparation.source_jd_hash
        or current_artifacts.get("rubric") != preparation.rubric or current_artifacts.get("search_plan") != preparation.search_plan):
        raise BossLiveAccessDenied("确认后岗位材料变化；未同步登录态、未创建客户端")
    created = _freeze_source_run(preparation, current)
    plan = preparation.plan
    operation_count = len(plan["operation_manifest"])
    access = preflight_boss_access(
        live=True,
        confirm_live=current.date().isoformat(),
        auth_dir=FIXED_AUTH_DIR,
        operation_count=operation_count,
        now=current,
    )
    try:
        auth_sync = sync_auth_from_chrome(FIXED_AUTH_DIR)
        session_fingerprint = str(auth_sync.get("session_fingerprint") or "").strip()
        if not session_fingerprint:
            raise BossLiveAccessDenied("登录态同步未返回 session fingerprint，未创建 BOSS 客户端")
        store = LiveAuthorizationStore(FIXED_GUARD_DIR)
        issued = store.issue(
            plan=plan,
            session_fingerprint=session_fingerprint,
            confirm_live=current.date().isoformat(),
            note=f"终端确认来源 {operation_count} 次只读操作",
            now=lambda: current,
        )
        reloaded = load_active_workflow_run(preparation.paths)
        if reloaded is None or reloaded.state["run_id"] != created.state["run_id"]:
            raise BossLiveAccessDenied("冻结运行实例在授权后发生变化，未创建 BOSS 客户端")
        source_plan_path = reloaded.state_path.parent / reloaded.state["artifacts"]["source_plan"]["path"]
        try:
            execution_plan = json.loads(source_plan_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise BossLiveAccessDenied("冻结来源计划在授权后无法读取，未创建 BOSS 客户端") from exc
        if not isinstance(execution_plan, dict) or content_hash(execution_plan) != content_hash(plan):
            raise BossLiveAccessDenied("冻结来源计划在授权后发生变化，未创建 BOSS 客户端")
        authorization = store.consume(
            authorization_id=issued.authorization_id,
            plan=execution_plan,
            session_fingerprint=session_fingerprint,
            now=lambda: current,
        )
        result = _execute_read_live_plan(
            plan=execution_plan,
            execution_id=f"{created.state['run_id']}-source",
            work_dir=created.state_path.parent / "source",
            inventory_path=preparation.paths.inventory_path,
            access=access,
            generated_at=current.isoformat(),
        )
        source_collection_path = Path(result["artifact_path"])
        source_receipt = {
            "schema_version": 1,
            "contract": "boss_hire_workflow_source_receipt",
            "run_id": created.state["run_id"],
            "plan_id": execution_plan["plan_id"],
            "status": "complete",
            "operation_count": operation_count,
            "candidate_card_count": result["candidate_cards"],
            "source_collection_digest": content_hash(result["artifact"]),
            "completed_at": current.isoformat(),
        }
        source_receipt_path = created.state_path.parent / "source_receipt.json"
        atomic_write_json(source_receipt_path, source_receipt, sort_keys=True)
        next_state = transition_workflow_run_state(
            created.state,
            status="source_complete",
            updated_at=current.isoformat(),
            artifacts={
                "source_collection": {
                    "path": source_collection_path.relative_to(created.state_path.parent).as_posix(),
                    "digest": content_hash(result["artifact"]),
                },
                "source_receipt": {
                    "path": source_receipt_path.relative_to(created.state_path.parent).as_posix(),
                    "digest": content_hash(source_receipt),
                },
            },
        )
        save_active_workflow_run(
            preparation.paths,
            next_state,
            expected_state_digest=created.state_digest,
        )
        print(f"来源阶段完成：{result['candidate_cards']} 张候选卡片。")
        for hint in _report_hints(scoring_complete=False):
            print(hint)
        print("下一步：.venv/bin/python scripts/run_single_job_live.py continue")
        return 0
    except Exception as exc:
        blocked = transition_workflow_run_state(
            created.state,
            status="blocked",
            updated_at=current.isoformat(),
            last_error=f"source_collection: {type(exc).__name__}: {exc}",
        )
        try:
            save_active_workflow_run(
                preparation.paths,
                blocked,
                expected_state_digest=created.state_digest,
            )
        except ValueError:
            pass
        raise


def _freeze_source_run(
    preparation: StartPreparation,
    current: datetime,
) -> LoadedWorkflowRun:
    timestamp = current.strftime("%Y%m%d-%H%M%S")
    run_id = f"{timestamp}-{preparation.config.name}-{content_hash(preparation.plan)[:8]}"
    source_plan_digest = content_hash(preparation.plan)
    state = build_workflow_run_state(
        run_id=run_id,
        config_name=preparation.config.name,
        config=preparation.config.value,
        config_digest=preparation.config.digest,
        board_date=current.date().isoformat(),
        account_key=str(preparation.plan["account_key"]),
        job_id=preparation.job_id,
        source_jd_hash=preparation.source_jd_hash,
        search_plan_version=str(preparation.search_plan.get("version") or ""),
        search_plan_digest=content_hash(preparation.search_plan),
        rubric_version=str(preparation.rubric.get("version") or ""),
        rubric_digest=content_hash(preparation.rubric),
        created_at=current.isoformat(),
    )
    state["artifacts"] = {
        "source_plan": {
            "path": "source_plan.json",
            "digest": source_plan_digest,
        }
    }
    return create_workflow_run_with_artifacts(
        preparation.paths,
        state,
        artifact_values={"source_plan": preparation.plan},
    )


def start_command(
    args: argparse.Namespace,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    now: Clock | None = None,
    after_confirmation: Callable[[StartPreparation, datetime], int] = _complete_start_after_confirmation,
) -> int:
    current = _local_now(now)
    preparation = _prepare_start(
        config_name=args.config,
        paths=workflow_paths(ROOT),
        board_date=current.date().isoformat(),
        search_filter_inputs=args.filter,
    )
    expected = _render_start_preview(preparation, output_fn)
    confirmation = input_fn(
        "请先在 BOSS 官方页面确认账号正常且没有验证码或安全验证。"
        f"输入“{expected}”继续："
    )
    if confirmation.strip() != expected:
        raise BossLiveAccessDenied("来源确认文本不匹配，未同步登录态、未访问 BOSS")
    return int(after_confirmation(preparation, current))


def _continue_detail_stage(
    *,
    args: argparse.Namespace,
    paths: WorkflowPaths,
    current: datetime,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> int:
    selection = args.select if args.select is not None else "all"
    action = prepare_detail_action(
        paths,
        selection=selection,
        updated_at=current.isoformat(),
    )
    if action["status"] == "skipped_no_pending":
        output_fn("没有待抓取详情；未签发授权、未访问 BOSS。")
        output_fn("下一步：.venv/bin/python scripts/run_single_job_live.py continue")
        return 0
    plan, frozen = freeze_detail_plan(
        paths,
        selection=selection,
        auth_dir=FIXED_AUTH_DIR,
        updated_at=current.isoformat(),
        action=action,
    )
    candidate_ids = "、".join(action["candidate_ids"])
    output_fn(
        f"详情：选择 {plan['selected_count']}/{plan['pending_count']} 人；"
        f"本轮后仍待处理 {plan['remaining_pending_count']} 人。"
    )
    output_fn(f"匿名候选：{candidate_ids}")
    expected = f"确认详情{plan['selected_count']}人"
    confirmation = input_fn(
        "请先在 BOSS 官方页面确认账号正常且没有验证码或安全验证。"
        f"输入“{expected}”继续："
    )
    if confirmation.strip() != expected:
        raise BossLiveAccessDenied("详情确认文本不匹配，未同步登录态、未访问 BOSS")
    try:
        execution_plan, verified = verify_frozen_detail_plan(paths, auth_dir=FIXED_AUTH_DIR)
        operation_count = len(execution_plan["operation_manifest"])
        access = preflight_boss_access(
            live=True,
            confirm_live=current.date().isoformat(),
            auth_dir=FIXED_AUTH_DIR,
            operation_count=operation_count,
            now=current,
        )
        auth_sync = sync_auth_from_chrome(FIXED_AUTH_DIR)
        session_fingerprint = str(auth_sync.get("session_fingerprint") or "").strip()
        if not session_fingerprint:
            raise BossLiveAccessDenied("登录态同步未返回 session fingerprint，未创建 BOSS 客户端")
        store = LiveAuthorizationStore(FIXED_GUARD_DIR)
        issued = store.issue(
            plan=execution_plan,
            session_fingerprint=session_fingerprint,
            confirm_live=current.date().isoformat(),
            note=f"终端确认详情 {execution_plan['selected_count']} 人",
            now=lambda: current,
        )
        execution_plan, verified_after_issue = verify_frozen_detail_plan(
            paths,
            auth_dir=FIXED_AUTH_DIR,
        )
        if verified_after_issue.state_digest != verified.state_digest:
            raise BossLiveAccessDenied("详情运行状态在授权后发生变化，未创建 BOSS 客户端")
        store.consume(
            authorization_id=issued.authorization_id,
            plan=execution_plan,
            session_fingerprint=session_fingerprint,
            now=lambda: current,
        )
        result = _execute_read_live_plan(
            plan=execution_plan,
            execution_id=f"{verified.state['run_id']}-detail",
            work_dir=verified.state_path.parent / "detail",
            inventory_path=paths.inventory_path,
            access=access,
            generated_at=current.isoformat(),
        )
        detail_collection_path = Path(result["artifact_path"])
        detail_receipt = {
            "schema_version": 1,
            "contract": "boss_hire_workflow_detail_receipt",
            "run_id": verified.state["run_id"],
            "plan_id": execution_plan["plan_id"],
            "status": "complete",
            "selected_count": execution_plan["selected_count"],
            "detail_request_count": result["artifact"]["detail_request_count"],
            "cached_resume_count": result["artifact"]["cached_resume_count"],
            "remaining_pending_count": result["artifact"]["remaining_pending_count"],
            "detail_collection_digest": content_hash(result["artifact"]),
            "completed_at": current.isoformat(),
        }
        receipt_path = verified.state_path.parent / "detail_receipt.json"
        atomic_write_json(receipt_path, detail_receipt, sort_keys=True)
        next_state = transition_workflow_run_state(
            verified.state,
            status="detail_complete",
            updated_at=current.isoformat(),
            artifacts={
                "detail_collection": {
                    "path": detail_collection_path.relative_to(verified.state_path.parent).as_posix(),
                    "digest": content_hash(result["artifact"]),
                },
                "detail_receipt": {
                    "path": receipt_path.relative_to(verified.state_path.parent).as_posix(),
                    "digest": content_hash(detail_receipt),
                },
            },
        )
        save_active_workflow_run(
            paths,
            next_state,
            expected_state_digest=verified.state_digest,
        )
        output_fn(
            f"详情阶段完成：新增 {detail_receipt['detail_request_count']}，"
            f"缓存 {detail_receipt['cached_resume_count']}。"
        )
        for hint in _report_hints(scoring_complete=False):
            output_fn(hint)
        output_fn("下一步：.venv/bin/python scripts/run_single_job_live.py continue")
        return 0
    except Exception as exc:
        blocked = transition_workflow_run_state(
            frozen.state,
            status="blocked",
            updated_at=current.isoformat(),
            last_error=f"candidate_details: {type(exc).__name__}: {exc}",
        )
        try:
            save_active_workflow_run(
                paths,
                blocked,
                expected_state_digest=frozen.state_digest,
            )
        except ValueError:
            pass
        raise


def _continue_scoring_stage(
    *,
    paths: WorkflowPaths,
    current: datetime,
    input_fn: Callable[[str], str],
    output_fn: Callable[[str], None],
) -> int:
    active = load_active_workflow_run(paths)
    if active is None or active.state["status"] != "detail_complete":
        raise ValueError("当前状态不能执行本地评分")
    build_workflow_status(paths)
    inventory = CandidateInventory.load(paths.inventory_path)
    job_artifacts = inventory.get_job_artifacts(
        active.state["job_id"],
        active.state["source_jd_hash"],
    )
    if not isinstance(job_artifacts, dict) or not isinstance(job_artifacts.get("rubric"), dict):
        raise ValueError("当前运行缺少评分 rubric")
    rubric = dict(job_artifacts["rubric"])
    settings = _scoring_settings()
    preview = build_llm_confirmation_preview(
        inventory=inventory,
        job_id=active.state["job_id"],
        rubric=rubric,
        model=settings["model"],
        workers=settings["workers"],
        created_at=current.isoformat(),
    )
    if preview["pending_count"] == 0:
        next_state = transition_workflow_run_state(
            active.state,
            status="scoring_complete",
            updated_at=current.isoformat(),
        )
        save_active_workflow_run(
            paths,
            next_state,
            expected_state_digest=active.state_digest,
        )
        output_fn(f"现有有效评分已全部复用：{preview['reuse_count']} 人；未调用 LLM。")
        for hint in _report_hints(scoring_complete=True):
            output_fn(hint)
        output_fn("当前自动化流程已完成；发布/收藏仍使用原独立入口。")
        return 0
    output_fn(
        f"评分：复用 {preview['reuse_count']} 人，新增 {preview['pending_count']} 人；"
        f"模型 {preview['llm_model']}，并发 {preview['workers']}。"
    )
    expected = f"确认评分{preview['pending_count']}人"
    confirmation = input_fn(f"输入“{expected}”继续：")
    if confirmation.strip() != expected:
        raise ValueError("评分确认文本不匹配，未创建 LLM 客户端")
    fresh_settings = _scoring_settings()
    fresh_inventory = CandidateInventory.load(paths.inventory_path)
    fresh_artifacts = fresh_inventory.get_job_artifacts(
        active.state["job_id"],
        active.state["source_jd_hash"],
    )
    fresh_rubric = fresh_artifacts.get("rubric") if isinstance(fresh_artifacts, dict) else None
    if not isinstance(fresh_rubric, dict):
        raise ValueError("确认后当前 rubric 不存在，未创建 LLM 客户端")
    verify_llm_confirmation_preview(
        preview,
        inventory=fresh_inventory,
        job_id=active.state["job_id"],
        rubric=fresh_rubric,
        model=fresh_settings["model"],
        workers=fresh_settings["workers"],
        checked_at=current.isoformat(),
    )
    receipt = build_llm_confirmation_receipt(preview, confirmed_at=current.isoformat())
    attempt_id = f"{current.strftime('%Y%m%d-%H%M%S')}-{preview['confirmation_id']}"
    attempt_dir = active.state_path.parent / "scoring" / "attempts" / attempt_id
    receipt_path = attempt_dir / "confirmation_receipt.json"
    atomic_write_json(receipt_path, receipt, sort_keys=True)
    confirmed_state = dict(active.state)
    confirmed_state["artifacts"] = {
        **dict(active.state["artifacts"]),
        "llm_confirmation_receipt": {
            "path": receipt_path.relative_to(active.state_path.parent).as_posix(),
            "digest": content_hash(receipt),
        },
    }
    confirmed_state["updated_at"] = current.isoformat()
    confirmed = save_active_workflow_run(
        paths,
        confirmed_state,
        expected_state_digest=active.state_digest,
    )
    llm = OpenAICompatibleJsonLlm(
        base_url=fresh_settings["base_url"],
        api_key=fresh_settings["api_key"],
        model=fresh_settings["model"],
    )
    result = score_inventory_resumes(
        inventory_path=paths.inventory_path,
        job_id=active.state["job_id"],
        rubric=fresh_rubric,
        llm=llm,
        output_dir=attempt_dir,
        workers=fresh_settings["workers"],
    )
    summary = result["summary"]
    summary_path = Path(result["summary_path"])
    summary_reference = {
        "score_summary": {
            "path": summary_path.relative_to(active.state_path.parent).as_posix(),
            "digest": content_hash(summary),
        }
    }
    if summary["remaining_pending_count"] > 0 or summary["failed_count"] > 0:
        next_state = bind_workflow_run_artifacts(
            confirmed.state,
            updated_at=current.isoformat(),
            artifacts=summary_reference,
        )
        next_state["last_error"] = (
            "llm_scoring_incomplete: "
            f"failed={summary['failed_count']} remaining={summary['remaining_pending_count']} "
            f"backpressure={summary['backpressure_stopped']}"
        )
    else:
        next_state = transition_workflow_run_state(
            confirmed.state,
            status="scoring_complete",
            updated_at=current.isoformat(),
            artifacts=summary_reference,
        )
    save_active_workflow_run(
        paths,
        next_state,
        expected_state_digest=confirmed.state_digest,
    )
    output_fn(
        f"评分阶段完成：新增 {summary['scored_count']}，失败 {summary['failed_count']}，"
        f"仍待评分 {summary['remaining_pending_count']}。"
    )
    for hint in _report_hints(scoring_complete=next_state["status"] == "scoring_complete"):
        output_fn(hint)
    if next_state["status"] == "scoring_complete":
        output_fn("当前自动化流程已完成；发布/收藏仍使用原独立入口。")
        return 0
    output_fn("评分尚未完成；下次 continue 只处理仍 pending 的候选。")
    return 2


def continue_command(
    args: argparse.Namespace,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    now: Clock | None = None,
) -> int:
    current = _local_now(now)
    paths = workflow_paths(ROOT)
    active = load_active_workflow_run(paths)
    if active is None:
        raise ValueError("没有活动运行；请先使用 start --config <名称>")
    if active.state["status"] == "source_complete":
        return _continue_detail_stage(
            args=args,
            paths=paths,
            current=current,
            input_fn=input_fn,
            output_fn=output_fn,
        )
    if active.state["status"] == "detail_complete":
        return _continue_scoring_stage(
            paths=paths,
            current=current,
            input_fn=input_fn,
            output_fn=output_fn,
        )
    raise ValueError(f"当前状态暂不能 continue：{active.state['status']}")


def close_command(
    args: argparse.Namespace,
    *,
    output_fn: Callable[[str], None] = print,
    now: Clock | None = None,
) -> int:
    current = _local_now(now)
    paths = workflow_paths(ROOT)
    active = load_active_workflow_run(paths)
    if active is not None:
        if args.run is not None and args.run != active.state["run_id"]:
            raise ValueError(f"--run 与活动运行不一致：{active.state['run_id']}")
        target = active
        save_fn = save_active_workflow_run
    else:
        if args.run is None:
            run_ids = list_workflow_run_ids(paths)
            hint = "、".join(run_ids) if run_ids else "无"
            raise ValueError(f"没有活动运行指针；可用运行：{hint}；请显式传入 close --run <id>")
        target = load_workflow_run(paths, args.run)
        save_fn = save_workflow_run
    if target.state["status"] == "closed":
        output_fn(f"运行已关闭：{target.state['run_id']}")
        return 0
    receipt = {
        "schema_version": 1,
        "contract": "boss_hire_workflow_close_receipt",
        "run_id": target.state["run_id"],
        "note": str(args.note).strip(),
        "closed_at": current.isoformat(),
        "external_actions": 0,
    }
    if not receipt["note"]:
        raise ValueError("close --note 不能为空")
    receipt_path = target.state_path.parent / "close_receipt.json"
    atomic_write_json(receipt_path, receipt, sort_keys=True)
    closed = transition_workflow_run_state(
        target.state,
        status="closed",
        updated_at=current.isoformat(),
        artifacts={
            "close_receipt": {
                "path": "close_receipt.json",
                "digest": content_hash(receipt),
            }
        },
    )
    save_fn(paths, closed, expected_state_digest=target.state_digest)
    output_fn(f"已关闭本地运行：{target.state['run_id']}")
    output_fn("未删除产物、未生成授权、未清除 BOSS circuit。")
    return 0


def _config_rows(config_root: Path) -> list[dict[str, Any]]:
    root = Path(config_root)
    if not root.exists():
        return []
    rows: list[dict[str, Any]] = []
    for path in sorted(root.glob("*.json"), key=lambda item: item.name):
        name = path.stem
        try:
            resolved = resolve_run_config(name, config_root=root)
        except ValueError as exc:
            rows.append(
                {
                    "name": name,
                    "valid": False,
                    "recommendation_source_enabled": None,
                    "top_priority_search_query_count": None,
                    "second_page_search_query_count": None,
                    "recent_view_filter": None,
                    "source_summary": None,
                    "error": str(exc),
                }
            )
            continue
        recommendation = resolved.value["recommendation_source_enabled"]
        query_count = resolved.value["top_priority_search_query_count"]
        second_page_count = resolved.value["second_page_search_query_count"]
        recent_view_filter = resolved.value["recent_view_filter"]
        rows.append(
            {
                "name": resolved.name,
                "valid": True,
                "recommendation_source_enabled": recommendation,
                "top_priority_search_query_count": query_count,
                "second_page_search_query_count": second_page_count,
                "recent_view_filter": recent_view_filter,
                "source_summary": {
                    "recommendation": "enabled" if recommendation else "disabled",
                    "search_query_count": query_count,
                    "second_page_search_query_count": second_page_count,
                    "recent_view_filter": recent_view_filter,
                },
                "error": None,
            }
        )
    return rows


def configs_command(
    args: argparse.Namespace,
    *,
    output_fn: Callable[[str], None] = print,
) -> int:
    rows = _config_rows(workflow_paths(ROOT).config_root)
    payload = {
        "schema_version": 1,
        "contract": "boss_hire_run_configs",
        "configs": rows,
    }
    if args.json:
        output_fn(json.dumps(payload, ensure_ascii=False))
        return 0
    output_fn("运行配置（固定目录 data/local/run_configs）：")
    if not rows:
        output_fn("  暂无配置。")
        return 0
    for row in rows:
        if not row["valid"]:
            output_fn(f"  - {row['name']}：无效（{row['error']}）")
            continue
        recommendation = "开" if row["recommendation_source_enabled"] else "关"
        output_fn(
            f"  - {row['name']}：推荐 {recommendation}，"
            f"优先搜索词 {row['top_priority_search_query_count']} 个，"
            f"其中第二页 {row['second_page_search_query_count']} 个，"
            f"近14天已查看过滤 {row['recent_view_filter']}"
        )
    output_fn("启动示例：.venv/bin/python scripts/run_single_job_live.py start --config <名称>")
    return 0


def status_command(
    args: argparse.Namespace,
    *,
    output_fn: Callable[[str], None] = print,
) -> int:
    status = build_workflow_status(workflow_paths(ROOT), run_id=args.run)
    if args.json:
        output_fn(json.dumps(status, ensure_ascii=False))
        return 0
    if status["status"] == "no_active_run":
        output_fn("当前没有活动运行。")
        output_fn(f"下一步：{status['next_command']}")
        return 0
    output_fn(f"运行：{status['run_id']}")
    output_fn(f"状态：{status['status']} · 下一动作：{status['next_action']}")
    output_fn(
        f"配置：{status['config_name']} · 岗位：{status['job_id']} · "
        f"rubric：{status['rubric_version']}"
    )
    funnel = status["funnel"]
    output_fn(
        "漏斗："
        f"卡片 {funnel['candidate_card_count']}，"
        f"待详情 {funnel['pending_detail_count']}，"
        f"待评分 {funnel['pending_score_count']}，"
        f"有效评分 {funnel['valid_score_count']}"
    )
    if status["blocker"]:
        output_fn(f"阻塞：{status['blocker']}")
    if status["artifacts"]:
        output_fn("产物：")
        for name, reference in sorted(status["artifacts"].items()):
            output_fn(f"  - {name}: {reference['path']}")
    if "source_collection" in status["artifacts"]:
        for hint in _report_hints(scoring_complete=status["status"] == "scoring_complete"):
            output_fn(hint)
    if status["next_command"]:
        output_fn(f"下一步：{status['next_command']}")
    else:
        output_fn("当前自动化流程已完成；发布/收藏仍使用原独立入口。")
    return 0


def pool_command(
    args: argparse.Namespace,
    *,
    output_fn: Callable[[str], None] = print,
) -> int:
    """Show every scored candidate for the active job and rubric from local state."""

    paths = workflow_paths(ROOT)
    active = load_active_workflow_run(paths)
    if active is None:
        raise ValueError("当前没有活动岗位；请先完成一个岗位运行后再查看总候选池")
    report = build_single_job_report(
        paths=paths,
        run_id=str(active.state["run_id"]),
        candidate_scope="pool",
        include_candidates=True,
    )
    candidates = list(report.get("candidates") or [])
    if args.favorited_only:
        candidates = [
            candidate
            for candidate in candidates
            if candidate.get("local_favorite_status") == "favorited_local"
        ]
    if not args.all:
        candidates = candidates[: args.top]
    report["candidates"] = candidates
    report["candidate_selection"] = {
        "top": None if args.all else args.top,
        "all": args.all,
        "favorited_only": args.favorited_only,
        "matched_count": len(candidates),
    }
    scope = "同 JD/rubric 总池"
    if args.favorited_only:
        scope += "中本地已收藏候选"
    report["candidate_heading"] = f"{scope}（全部）：" if args.all else f"{scope} Top{args.top}："
    if args.json:
        output_fn(json.dumps(report, ensure_ascii=False, sort_keys=True))
        return 0
    output_fn(format_single_job_report(report, section="scores"))
    output_fn(
        "范围：当前活动岗位 × 评分卡的总池；收藏状态仅来自本地收藏注册表，"
        "未同步或查询 BOSS。"
    )
    if args.favorited_only or args.all:
        selection = "仅本地已收藏；" if args.favorited_only else "全部候选；"
        output_fn(f"筛选：{selection}实际显示 {len(candidates)} 人。")
    return 0


def report_command(
    args: argparse.Namespace,
    *,
    output_fn: Callable[[str], None] = print,
) -> int:
    """Show the funnel and score report for the active workflow run."""

    paths = workflow_paths(ROOT)
    active = load_active_workflow_run(paths)
    if active is None:
        raise ValueError("当前没有活动岗位；请先完成一个岗位运行后再查看本轮报告")
    section = "scores" if args.scores else "all"
    report = build_single_job_report(
        paths=paths,
        run_id=str(active.state["run_id"]),
        candidates=args.top,
        candidate_scope="new",
        include_candidates=args.top is not None,
    )
    if args.top is not None:
        report["candidate_heading"] = f"本轮新评分 Top{args.top}："
    if args.json:
        payload = report if section == "all" else {
            key: value
            for key, value in report.items()
            if key not in {"supply", "scores"} or key == section
        }
        output_fn(json.dumps(payload, ensure_ascii=False, sort_keys=True))
        return 0
    output_fn(format_single_job_report(report, section=section))
    return 0


def _local_now(now: Clock | None = None) -> datetime:
    value = now() if now is not None else datetime.now(tz=SHANGHAI)
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI)
    return value.astimezone(SHANGHAI)


def _positive_int(value: str) -> int:
    try:
        parsed = int(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("必须是正整数") from exc
    if parsed <= 0:
        raise argparse.ArgumentTypeError("必须是正整数")
    return parsed


def _load_published_batch(path: Path) -> dict[str, Any]:
    try:
        batch = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取发布批次：{path}") from exc
    if not isinstance(batch, dict):
        raise ValueError("发布批次必须是 JSON 对象")
    return batch


def _load_favorite_sync_receipt(path: Path) -> dict[str, Any]:
    try:
        receipt = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"无法读取收藏同步回执：{path}") from exc
    if not isinstance(receipt, dict):
        raise ValueError("收藏同步回执必须是 JSON 对象")
    return receipt


def _parse_selected_ranks(value: str) -> list[int]:
    normalized = str(value or "").strip().replace("，", ",")
    if not normalized:
        raise ValueError("至少选择一名候选人")
    result: list[int] = []
    for index, token in enumerate(normalized.split(",")):
        item = token.strip()
        if not item or not item.isdigit():
            raise ValueError(f"第 {index + 1} 个收藏编号无效")
        result.append(int(item))
    return result


def _favorite_statuses(batch: dict[str, Any], ledger: FavoriteDeliveryLedger) -> dict[str, str]:
    candidates = batch.get("candidates")
    if not isinstance(candidates, list):
        return {}
    candidate_ids = [
        str(row.get("candidate_id") or "").strip()
        for row in candidates
        if isinstance(row, dict) and str(row.get("candidate_id") or "").strip()
    ]
    return ledger.statuses(candidate_ids)


def _stable_candidate_ids(batch: dict[str, Any]) -> dict[str, str]:
    candidates = batch.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("发布批次 candidates 必须是列表")
    result: dict[str, str] = {}
    for index, row in enumerate(candidates):
        if not isinstance(row, dict):
            raise ValueError(f"发布批次 candidates[{index}] 必须是对象")
        candidate_id = str(row.get("candidate_id") or "").strip()
        identifiers = row.get("boss_identifiers")
        encrypt_geek_id = (
            str(identifiers.get("encryptGeekId") or "").strip()
            if isinstance(identifiers, dict)
            else ""
        )
        if not candidate_id or not encrypt_geek_id:
            raise ValueError("发布批次候选缺少稳定 encryptGeekId")
        result[candidate_id] = encrypt_geek_id
    return result


def _migrate_legacy_favorite_ledger(
    *,
    work_dir: Path,
    ledger_path: Path,
    registry: FavoriteRegistry,
    stable_candidate_ids: dict[str, str],
    output_fn: Callable[[str], None],
) -> None:
    source_path = Path(work_dir) / "favorite_delivery_state.json"
    if not source_path.is_file():
        return
    source_identity = str(source_path.expanduser().resolve(strict=False))
    receipt = migrate_favorite_delivery_ledger(
        source_path=source_path,
        target_path=ledger_path,
        registry=registry,
        stable_candidate_ids=stable_candidate_ids,
        migration_id="legacy-work-dir-"
        + hashlib.sha256(source_identity.encode("utf-8")).hexdigest()[:16],
    )
    output_fn(
        "已幂等迁移旧 work-dir 收藏账本："
        f"导入 {receipt['imported_count']} 条，源文件保留。"
    )


def _render_favorite_batch(batch: dict[str, Any], output_fn: Callable[[str], None]) -> None:
    candidates = batch.get("candidates")
    if not isinstance(candidates, list):
        raise ValueError("发布批次 candidates 必须是列表")
    for row in candidates:
        if not isinstance(row, dict):
            continue
        display = row.get("display") if isinstance(row.get("display"), dict) else {}
        output_fn(
            f"{row.get('rank')}. {str(display.get('name') or '').strip() or '未命名候选人'}  "
            f"{row.get('score')} 分  {str(row.get('summary') or '').strip()}"
        )
    output_fn("")
    output_fn("请选择要收藏的人，例如：1,2,4")


def _write_immutable_json(path: Path, value: dict[str, Any]) -> None:
    if path.exists():
        try:
            existing = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"已有冻结计划损坏：{path}") from exc
        if existing != value:
            raise ValueError(f"已有冻结计划不可覆盖：{path}")
        return
    atomic_write_json(path, value, sort_keys=True)


def _bound_favorite_plan(
    plan: dict[str, Any],
    *,
    board_date: str,
    work_dir: Path,
) -> dict[str, Any]:
    result = dict(plan)
    result["board_date"] = board_date
    result["account_key"] = account_key_for(FIXED_AUTH_DIR)
    result["operation_manifest_kind"] = "favorite_delivery"
    result["operation_manifest"] = build_favorite_delivery_operation_manifest(
        batch_id=str(result["batch_id"]),
        candidates=result["candidates"],
    )
    work_root = str(Path(work_dir).expanduser().resolve(strict=False))
    result["execution_policy"] = {
        "fixed_auth_namespace": True,
        "fixed_guard_namespace": True,
        "authorization_mode": "one_time_inline",
        "strictly_serial": True,
        "work_root_digest": hashlib.sha256(work_root.encode("utf-8")).hexdigest()[:16],
    }
    return result


def _execute_favorite_live_plan(
    *,
    plan: dict[str, Any],
    ledger: FavoriteDeliveryLedger,
    registry: FavoriteRegistry,
    work_dir: Path,
    access: Any,
    authorization: Any,
) -> dict[str, Any]:
    execution_id = f"{plan['plan_id']}-{authorization.authorization_id[:8]}"
    run_dir = Path(work_dir) / "runs" / execution_id

    from boss_agent_cli.auth.manager import AuthManager
    from boss_hire.boss_guard import BossRequestGuard
    from boss_hire.safe_recruiter_client import SafeBossRecruiterClient

    guard = BossRequestGuard(
        root=FIXED_GUARD_DIR,
        account_key=access.account_key,
        run_id=execution_id,
        operation_manifest=plan["operation_manifest"],
    )
    with guard:
        with SafeBossRecruiterClient(AuthManager(FIXED_AUTH_DIR), guard=guard) as client:
            return run_favorite_delivery(
                plan=plan,
                ledger=ledger,
                registry=registry,
                execute_candidate=lambda candidate, write, verify: execute_favorite_candidate(
                    client=client,
                    candidate=candidate,
                    write_operation=write,
                    verify_operation=verify,
                ),
                work_dir=run_dir,
            )


def _render_favorite_result(result: dict[str, Any], output_fn: Callable[[str], None]) -> None:
    receipt = result["receipt"]
    output_fn(f"已确认收藏：{receipt['confirmed_count']}")
    output_fn(f"已收藏而跳过：{receipt['already_confirmed_count']}")
    output_fn(f"失败：{receipt['failed_count']}")
    output_fn(f"结果不明：{receipt['unknown_count']}")
    output_fn(f"未选择：{receipt['not_selected_count']}")
    output_fn(f"回执：{Path(result['receipt_path']).resolve()}")


def favorite_command(
    args: argparse.Namespace,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    now: Clock | None = None,
) -> int:
    current = _local_now(now)
    board_date = current.date().isoformat()
    account_key = account_key_for(FIXED_AUTH_DIR)
    account_state_dir = favorite_account_state_dir(account_key)
    registry = FavoriteRegistry(account_state_dir, account_key=account_key)
    ledger_path = account_state_dir / "delivery_ledger.json"
    batch = _load_published_batch(args.batch)
    _migrate_legacy_favorite_ledger(
        work_dir=args.work_dir,
        ledger_path=ledger_path,
        registry=registry,
        stable_candidate_ids=_stable_candidate_ids(batch),
        output_fn=output_fn,
    )
    _render_favorite_batch(batch, output_fn)
    selection_text = args.select or input_fn("请选择要收藏的人：")
    selected_ranks = _parse_selected_ranks(selection_text)
    ledger = FavoriteDeliveryLedger(ledger_path)
    sync_receipt = _load_favorite_sync_receipt(args.sync_receipt)
    plan = build_synced_favorite_delivery_plan(
        batch,
        selected_ranks=selected_ranks,
        sync_receipt=sync_receipt,
        account_key=account_key,
        board_date=board_date,
        favorite_candidate_ids=registry.known_candidate_ids(),
        favorite_statuses=_favorite_statuses(batch, ledger),
        retry_definite_failures=args.retry_definite_failures,
    )
    selected_names = "、".join(row["name"] or f"第 {row['rank']} 名" for row in plan["candidates"])
    output_fn(f"即将收藏 {plan['selected_count']} 人：{selected_names}")
    preview_path = args.work_dir / "previews" / plan["plan_id"] / "favorite_delivery_plan.json"
    atomic_write_json(preview_path, plan, sort_keys=True)
    if not args.live:
        output_fn(f"本地预览已生成：{preview_path.resolve()}")
        output_fn("未传入 --live，未同步登录态、未创建 BOSS 客户端。")
        return 0

    expected = f"确认{plan['selected_count']}人"
    confirmation = input_fn(
        "请先在 BOSS 官方页面确认账号正常且没有验证码或安全验证。"
        f"输入“{expected}”继续："
    )
    if confirmation.strip() != expected:
        raise BossLiveAccessDenied("收藏确认文本不匹配，未同步登录态、未访问 BOSS")

    fresh_batch = _load_published_batch(args.batch)
    fresh_ledger = FavoriteDeliveryLedger(ledger_path)
    fresh_registry = FavoriteRegistry(account_state_dir, account_key=account_key)
    try:
        fresh_plan = build_synced_favorite_delivery_plan(
            fresh_batch,
            selected_ranks=selected_ranks,
            sync_receipt=_load_favorite_sync_receipt(args.sync_receipt),
            account_key=account_key,
            board_date=board_date,
            favorite_candidate_ids=fresh_registry.known_candidate_ids(),
            favorite_statuses=_favorite_statuses(fresh_batch, fresh_ledger),
            retry_definite_failures=args.retry_definite_failures,
        )
    except ValueError as exc:
        raise BossLiveAccessDenied(
            "确认后发布批次、同步回执或候选收藏状态发生变化，未同步登录态"
        ) from exc
    if content_hash(fresh_plan) != content_hash(plan):
        raise BossLiveAccessDenied("确认后发布批次或候选收藏状态发生变化，未同步登录态")

    bound_plan = _bound_favorite_plan(
        fresh_plan,
        board_date=board_date,
        work_dir=args.work_dir,
    )
    plan_digest = content_hash(bound_plan)[:12]
    frozen_plan_path = (
        args.work_dir
        / "plans"
        / f"{bound_plan['plan_id']}-{plan_digest}"
        / "favorite_delivery_plan.json"
    )
    _write_immutable_json(frozen_plan_path, bound_plan)
    operation_count = len(bound_plan["operation_manifest"])
    if operation_count == 0:
        result = run_favorite_delivery(
            plan=bound_plan,
            ledger=fresh_ledger,
            registry=fresh_registry,
            execute_candidate=lambda _candidate, _write, _verify: (_ for _ in ()).throw(
                AssertionError("already confirmed candidates must not execute")
            ),
            work_dir=args.work_dir / "runs" / f"{bound_plan['plan_id']}-idempotent-{plan_digest}",
        )
        _render_favorite_result(result, output_fn)
        return 0

    access = preflight_boss_access(
        live=True,
        confirm_live=board_date,
        auth_dir=FIXED_AUTH_DIR,
        operation_count=operation_count,
        now=current,
    )
    auth_sync = sync_auth_from_chrome(FIXED_AUTH_DIR)
    session_fingerprint = str(auth_sync.get("session_fingerprint") or "").strip()
    if not session_fingerprint:
        raise BossLiveAccessDenied("登录态同步未返回 session fingerprint，未创建 BOSS 客户端")
    store = LiveAuthorizationStore(FIXED_GUARD_DIR)
    issued = store.issue(
        plan=bound_plan,
        session_fingerprint=session_fingerprint,
        confirm_live=board_date,
        note=f"终端确认收藏 {bound_plan['selected_count']} 人",
        now=lambda: current,
    )
    execution_plan = _load_published_batch(frozen_plan_path)
    if execution_plan != bound_plan:
        raise BossLiveAccessDenied("冻结收藏计划在授权后发生变化，未创建 BOSS 客户端")
    authorization = store.consume(
        authorization_id=issued.authorization_id,
        plan=execution_plan,
        session_fingerprint=session_fingerprint,
        now=lambda: current,
    )
    result = _execute_favorite_live_plan(
        plan=execution_plan,
        ledger=fresh_ledger,
        registry=fresh_registry,
        work_dir=args.work_dir,
        access=access,
        authorization=authorization,
    )
    _render_favorite_result(result, output_fn)
    receipt = result["receipt"]
    return 0 if receipt["failed_count"] == 0 and receipt["unknown_count"] == 0 else 2


def _bound_plan(*, config_path: Path, work_dir: Path, board_date: str) -> dict[str, Any]:
    config = read_single_job_run_config(config_path)
    search_job_id = None
    persisted_search_plan = None
    if config.top_priority_search_query_count > 0:
        inventory = CandidateInventory.load(Path(work_dir) / "candidate_inventory.json").to_dict()
        job_artifacts = inventory.get("job_artifacts") or {}
        if not isinstance(job_artifacts, dict) or len(job_artifacts) != 1:
            raise ValueError("搜索来源需要唯一的当前持久化搜索计划")
        search_job_id, artifacts = next(iter(job_artifacts.items()))
        persisted_search_plan = artifacts.get("search_plan") if isinstance(artifacts, dict) else None
        if not isinstance(persisted_search_plan, dict):
            raise ValueError("搜索来源需要唯一的当前持久化搜索计划")
    plan = build_single_job_run_plan(
        board_date=board_date,
        config=config,
        auth_dir=FIXED_AUTH_DIR,
        search_job_id=search_job_id,
        persisted_search_plan=persisted_search_plan,
    )
    work_root = str(Path(work_dir).expanduser().resolve(strict=False))
    plan["execution_policy"] = {
        **dict(plan.get("execution_policy") or {}),
        "fixed_auth_namespace": True,
        "fixed_guard_namespace": True,
        "authorization_mode": "one_time",
        "work_root_digest": hashlib.sha256(work_root.encode("utf-8")).hexdigest()[:16],
    }
    return plan


def _bound_read_plan(*, plan_path: Path, work_dir: Path, board_date: str) -> dict[str, Any]:
    plan = load_single_job_run_plan(plan_path)
    if plan.get("plan_kind") not in {"candidate_details", "favorite_registry_sync"}:
        raise ValueError("--plan 必须是候选详情或收藏同步计划")
    if plan.get("board_date") != board_date:
        raise BossLiveAccessDenied("只读计划日期不是 Asia/Shanghai 当天")
    if plan.get("account_key") != account_key_for(FIXED_AUTH_DIR):
        raise BossLiveAccessDenied("只读计划不属于固定 BOSS 账号命名空间")
    work_root = str(Path(work_dir).expanduser().resolve(strict=False))
    plan["execution_policy"] = {
        **dict(plan.get("execution_policy") or {}),
        "fixed_auth_namespace": True,
        "fixed_guard_namespace": True,
        "authorization_mode": "one_time",
        "work_root_digest": hashlib.sha256(work_root.encode("utf-8")).hexdigest()[:16],
    }
    return plan


def _requested_plan(args: argparse.Namespace, *, board_date: str) -> dict[str, Any]:
    if getattr(args, "plan", None) is not None:
        return _bound_read_plan(
            plan_path=args.plan,
            work_dir=args.work_dir,
            board_date=board_date,
        )
    return _bound_plan(config_path=args.config, work_dir=args.work_dir, board_date=board_date)


def authorize_command(args: argparse.Namespace, *, now: Clock | None = None) -> int:
    current = _local_now(now)
    board_date = current.date().isoformat()
    plan = _requested_plan(args, board_date=board_date)
    operation_count = len(plan["operation_manifest"])
    access = preflight_boss_access(
        live=True,
        confirm_live=args.confirm_live,
        auth_dir=FIXED_AUTH_DIR,
        operation_count=operation_count,
        now=current,
    )
    auth_sync = sync_auth_from_chrome(FIXED_AUTH_DIR)
    receipt = LiveAuthorizationStore(FIXED_GUARD_DIR).issue(
        plan=plan,
        session_fingerprint=str(auth_sync["session_fingerprint"]),
        confirm_live=args.confirm_live,
        note=args.note,
        now=lambda: current,
    )
    if plan.get("plan_kind") == "candidate_details":
        plan_fields = {
            "job_id": plan["job_id"],
            "selection": plan["selection"],
            "selected_count": plan["selected_count"],
            "remaining_pending_count": plan["remaining_pending_count"],
        }
    elif plan.get("plan_kind") == "favorite_registry_sync":
        plan_fields = {
            "mode": plan["mode"],
            "purpose": plan["purpose"],
            "max_pages": plan["max_pages"],
            "batch_id": plan.get("batch_id"),
        }
    else:
        plan_fields = {
            "recommendation_source_enabled": plan["recommendation_source_enabled"],
            "requested_search_queries": plan["top_priority_search_query_count"],
            "selected_search_queries": plan["selected_search_query_count"],
            "search_query_shortfall": plan["search_query_shortfall"],
            "requested_second_page_search_queries": plan.get("second_page_search_query_count", 0),
            "selected_second_page_search_queries": plan.get("selected_second_page_search_query_count", 0),
            "recent_view_filter": plan.get("recent_view_filter", "include_all"),
            "second_page_search_query_shortfall": plan.get("second_page_search_query_shortfall", 0),
            "search_plan_version": plan.get("search_plan_version"),
        }
    print(
        json.dumps(
            {
                "authorization_id": receipt.authorization_id,
                "status": receipt.status,
                "plan_id": receipt.plan_id,
                "local_date": receipt.local_date,
                "operation_count": receipt.operation_count,
                "plan_kind": plan.get("plan_kind", "source_collection"),
                **plan_fields,
                "boss_access": access.summary(),
                "auth_sync": {
                    "source": auth_sync["source"],
                    "changed": auth_sync["changed"],
                    "cookie_count": auth_sync["cookie_count"],
                },
            },
            ensure_ascii=False,
        )
    )
    return 0


def run_command(args: argparse.Namespace, *, now: Clock | None = None) -> int:
    if not args.live:
        raise BossLiveAccessDenied("真实运行需要显式传入 run --live")
    current = _local_now(now)
    board_date = current.date().isoformat()
    plan = _requested_plan(args, board_date=board_date)
    operation_count = len(plan["operation_manifest"])
    access = preflight_boss_access(
        live=True,
        confirm_live=board_date,
        auth_dir=FIXED_AUTH_DIR,
        operation_count=operation_count,
        now=current,
    )

    session_fingerprint = load_saved_session_fingerprint(FIXED_AUTH_DIR)
    authorization = LiveAuthorizationStore(FIXED_GUARD_DIR).consume(
        authorization_id=args.authorization_id,
        plan=plan,
        session_fingerprint=session_fingerprint,
        now=lambda: current,
    )
    execution_id = f"{plan['plan_id']}-{authorization.authorization_id[:8]}"
    run_dir = args.work_dir / execution_id
    write_single_job_run_plan(plan, run_dir / "run_plan.json")

    result = _execute_read_live_plan(
        plan=plan,
        execution_id=execution_id,
        work_dir=run_dir,
        access=access,
    )
    if plan.get("plan_kind") == "candidate_details":
        result_fields = {
            "job_id": plan["job_id"],
            "selection": plan["selection"],
            "selected_count": plan["selected_count"],
            "detail_request_count": result["artifact"]["detail_request_count"],
            "cached_resume_count": result["artifact"]["cached_resume_count"],
            "remaining_pending_count": result["artifact"]["remaining_pending_count"],
            "detail_collection_path": str(result["artifact_path"]),
        }
    elif plan.get("plan_kind") == "favorite_registry_sync":
        result_fields = {
            "mode": plan["mode"],
            "purpose": plan["purpose"],
            "sync_status": result["receipt"]["status"],
            "sync_complete": result["receipt"]["complete"],
            "pages_read": result["receipt"]["pages_read"],
            "observed_count": result["receipt"]["observed_count"],
            "favorite_sync_receipt_path": str(result["receipt_path"]),
        }
    else:
        result_fields = {
            "candidate_cards": result["candidate_cards"],
            "recommendation_source_enabled": plan["recommendation_source_enabled"],
            "requested_search_queries": plan["top_priority_search_query_count"],
            "selected_search_queries": plan["selected_search_query_count"],
            "search_query_shortfall": plan["search_query_shortfall"],
            "requested_second_page_search_queries": plan.get("second_page_search_query_count", 0),
            "selected_second_page_search_queries": plan.get("selected_second_page_search_query_count", 0),
            "recent_view_filter": plan.get("recent_view_filter", "include_all"),
            "second_page_search_query_shortfall": plan.get("second_page_search_query_shortfall", 0),
            "search_plan_version": plan.get("search_plan_version"),
            "source_collection_path": str(result["artifact_path"]),
        }
    print(
        json.dumps(
            {
                "plan_id": plan["plan_id"],
                "execution_id": execution_id,
                "authorization": authorization.summary(),
                "plan_kind": plan.get("plan_kind", "source_collection"),
                **result_fields,
                "boss_access": access.summary(),
            },
            ensure_ascii=False,
        )
    )
    if plan.get("plan_kind") == "favorite_registry_sync" and not result["receipt"]["complete"]:
        return 2
    return 0


def _execute_read_live_plan(
    *,
    plan: dict[str, Any],
    execution_id: str,
    work_dir: Path,
    access: Any,
    inventory_path: Path | None = None,
    generated_at: str | None = None,
) -> dict[str, Any]:
    # External dependencies are imported only after a one-time authorization is consumed.
    from boss_agent_cli.auth.manager import AuthManager
    from boss_hire.boss_guard import BossRequestGuard
    from boss_hire.safe_recruiter_client import SafeBossRecruiterClient

    guard = BossRequestGuard(
        root=FIXED_GUARD_DIR,
        account_key=access.account_key,
        run_id=execution_id,
        operation_manifest=plan["operation_manifest"],
    )
    with guard:
        with SafeBossRecruiterClient(AuthManager(FIXED_AUTH_DIR), guard=guard) as client:
            if plan.get("plan_kind") in {"job_catalog", "job_snapshot"}:
                from boss_hire.job_setup import run_job_read
                result = run_job_read(plan=plan, client=client, work_dir=work_dir, generated_at=generated_at or "")
            elif plan.get("plan_kind") == "candidate_details":
                from boss_agent_cli.commands.recruiter.resume_parser import parse_resume

                result = run_candidate_detail_collection(
                    plan=plan,
                    client=client,
                    parse_resume=parse_resume,
                    work_dir=work_dir,
                    inventory_path=inventory_path,
                    generated_at=generated_at,
                )
            elif plan.get("plan_kind") == "favorite_registry_sync":
                registry = FavoriteRegistry(
                    favorite_account_state_dir(access.account_key),
                    account_key=access.account_key,
                )
                result = run_favorite_registry_sync(
                    plan=plan,
                    client=client,
                    registry=registry,
                    work_dir=work_dir,
                    generated_at=generated_at,
                )
            else:
                result = run_single_job_source_collection(
                    plan=plan,
                    client=client,
                    work_dir=work_dir,
                    inventory_path=inventory_path,
                    generated_at=generated_at,
                )
    return result


def clear_circuit_command(args: argparse.Namespace) -> int:
    from boss_hire.boss_guard import BossRequestGuard

    account_key = account_key_for(FIXED_AUTH_DIR)
    BossRequestGuard.clear_circuit(
        root=FIXED_GUARD_DIR,
        account_key=account_key,
        note=args.note,
    )
    print(json.dumps({"cleared": True, "account_key": account_key}, ensure_ascii=False))
    return 0


def _require_setup_idle(paths: WorkflowPaths) -> None:
    # Caller holds the existing workflow lock, so start cannot overlap setup commits.
    from boss_hire.workflow_run import _load_active_unlocked
    active = _load_active_unlocked(paths)
    if active and active.state['status'] not in TERMINAL_RUN_STATUSES:
        raise ValueError('已有未结束运行；先 status/continue，或本人 close 后再选择或准备岗位')


def _confirm_setup(expected: str, *, input_fn: Callable[[str], str], output_fn: Callable[[str], None]) -> None:
    output_fn('请本人核对这是固定使用的本人 BOSS 账号，账号正常且没有安全验证。')
    try:
        answer = input_fn(f'输入“{expected}”继续：')
    except EOFError as exc:
        raise BossLiveAccessDenied('未确认本阶段，未创建客户端') from exc
    if answer.strip() != expected:
        raise BossLiveAccessDenied('确认文本不匹配，未同步登录态、未创建客户端')


def _execute_job_read_after_confirmation(plan: dict[str, Any], paths: WorkflowPaths, current: datetime) -> dict:
    from uuid import uuid4
    stage_root = paths.work_root / 'job_setup_runs' / uuid4().hex
    plan = dict(plan)
    plan['execution_policy'] = {'strictly_serial': True, 'authorization_mode': 'one_time_inline',
                                'fixed_auth_namespace': True, 'fixed_guard_namespace': True,
                                'work_root_digest': content_hash(str(stage_root.resolve()))}
    frozen_path = stage_root / 'plan.json'
    _write_immutable_json(frozen_path, plan)
    access = preflight_boss_access(live=True, confirm_live=current.date().isoformat(),
                                  auth_dir=FIXED_AUTH_DIR, operation_count=1, now=current)
    auth = sync_auth_from_chrome(FIXED_AUTH_DIR)
    fingerprint = str(auth.get('session_fingerprint') or '')
    if not fingerprint:
        raise BossLiveAccessDenied('登录态缺少会话指纹；未创建客户端')
    store = LiveAuthorizationStore(FIXED_GUARD_DIR)
    issued = store.issue(plan=plan, session_fingerprint=fingerprint,
                         confirm_live=current.date().isoformat(), note='本人确认岗位只读一步', now=lambda: current)
    frozen = json.loads(frozen_path.read_text())
    if frozen != plan:
        raise BossLiveAccessDenied('岗位计划漂移；未创建客户端')
    store.consume(authorization_id=issued.authorization_id, plan=frozen,
                  session_fingerprint=fingerprint, now=lambda: current)
    result = _execute_read_live_plan(plan=frozen, execution_id=stage_root.name, work_dir=stage_root,
                                     access=access, generated_at=current.isoformat())
    return result['artifact']


def jobs_command(args: argparse.Namespace, *, input_fn=input, output_fn=print, now: Clock | None = None) -> int:
    from boss_hire.job_setup import build_job_read_plan, load_catalog
    from boss_hire.workflow_run import _workflow_lock
    paths = workflow_paths(ROOT)
    current = _local_now(now)
    account = account_key_for(FIXED_AUTH_DIR)
    with _workflow_lock(paths):
        if args.refresh:
            _require_setup_idle(paths)
            output_fn('本次只读取一次岗位列表，不读取 JD 或候选人。')
            _confirm_setup('确认读取岗位列表1次', input_fn=input_fn, output_fn=output_fn)
            plan = build_job_read_plan(account_key=account, board_date=current.date().isoformat())
            catalog = _execute_job_read_after_confirmation(plan, paths, current)
            atomic_write_json(paths.work_root / 'job_catalog.json', catalog, sort_keys=True)
        else:
            catalog = load_catalog(paths.work_root, account_key=account, board_date=current.date().isoformat())
        for number, row in enumerate(catalog['jobs'], 1):
            output_fn(f"{number}. {row['name']}（岗位标识 {row['encrypt_job_id']}）")
        if not catalog['jobs']:
            output_fn('本次返回没有开放岗位；不会翻页或补请求。请本人核对官方页面。')
        if args.select is not None:
            _require_setup_idle(paths)
            if args.select < 1 or args.select > len(catalog['jobs']):
                raise ValueError('岗位编号不在当前列表；未访问 BOSS')
            summary = catalog['jobs'][args.select - 1]
            output_fn(f"本次只读取所选岗位 JD：{summary['name']}（{summary['encrypt_job_id']}）。")
            _confirm_setup('确认读取所选JD1次', input_fn=input_fn, output_fn=output_fn)
            fresh = load_catalog(paths.work_root, account_key=account, board_date=current.date().isoformat())
            if content_hash(fresh) != content_hash(catalog):
                raise BossLiveAccessDenied('确认后岗位列表变化；未同步登录态')
            plan = build_job_read_plan(account_key=account, board_date=current.date().isoformat(), summary=summary)
            selected = _execute_job_read_after_confirmation(plan, paths, current)
            atomic_write_json(paths.work_root / 'selected_job.json', selected, sort_keys=True)
            output_fn('所选 JD 已保存。下一步：.venv/bin/python scripts/run_single_job_live.py prepare-job')
        elif catalog['jobs']:
            output_fn('下一步：本人执行 .venv/bin/python scripts/run_single_job_live.py jobs --select <编号>')
    return 0


def prepare_job_command(args: argparse.Namespace, *, input_fn=input, output_fn=print, now: Clock | None = None) -> int:
    from uuid import uuid4
    from boss_hire.job_setup import read_selected_job
    from boss_hire.recruiter_jobs import RecruiterJob
    from boss_hire.search_plan import SearchPlanConfig
    from boss_hire.single_job_llm import SEARCH_GENERATOR_VERSION, prepare_single_job_llm_artifacts
    from boss_hire.state_store import jd_hash
    from boss_hire.workflow_run import _workflow_lock
    paths = workflow_paths(ROOT)
    account = account_key_for(FIXED_AUTH_DIR)
    with _workflow_lock(paths):
        _require_setup_idle(paths)
        selected = read_selected_job(paths.work_root, account_key=account)
        if selected is None or not isinstance(selected.get('job'), dict):
            raise ValueError('尚无选定 JD；请本人先 jobs --refresh，再 jobs --select <编号>')
        job = RecruiterJob(**selected['job'])
        source_hash = jd_hash(job.to_jd_text())
        if job.encrypt_job_id != selected['job_id'] or selected.get('source_jd_hash') != source_hash or not job.is_open:
            raise ValueError('所选 JD 身份或摘要不一致；请重新选择岗位')
        inventory = CandidateInventory.load(paths.inventory_path)
        cached = inventory.get_job_artifacts(job.encrypt_job_id, source_hash)
        cached_plan = cached.get('search_plan') if isinstance(cached, dict) else None
        cache_is_current = (
            isinstance(cached_plan, dict)
            and cached_plan.get('search_generator_version') == SEARCH_GENERATOR_VERSION
            and bool(cached_plan.get('routes'))
        )
        if cache_is_current:
            output_fn('所选 JD 材料已就绪，本次复用；BOSS 0 次、LLM 0 次。')
        else:
            if cached:
                output_fn('所选 JD 的旧搜索生成契约已失效；保留旧产物并生成新材料。')
            settings = _scoring_settings()
            output_fn(f'所选岗位：{job.name}；模型：{settings["model"]}。本次向配置的模型服务发送 JD，生成评分标准与搜索路线，不访问 BOSS。')
            _confirm_setup('确认生成岗位材料', input_fn=input_fn, output_fn=output_fn)
            fresh = read_selected_job(paths.work_root, account_key=account)
            if fresh != selected or _scoring_settings() != settings:
                raise BossLiveAccessDenied('确认后所选 JD 或模型配置变化；未创建模型客户端')
            llm = OpenAICompatibleJsonLlm(base_url=settings['base_url'], api_key=settings['api_key'], model=settings['model'])
            output_dir = paths.work_root / 'job_materials' / uuid4().hex
            artifacts = prepare_single_job_llm_artifacts(job=job, llm=llm, output_dir=output_dir,
                                                         search_config=SearchPlanConfig(total_query_budget=12))
            # Commit only after the whole immutable artifact set is complete. Failed generations remain unready.
            if read_selected_job(paths.work_root, account_key=account) != selected:
                raise ValueError('生成期间所选 JD 变化；材料未写入就绪库存')
            inventory.record_job_artifacts(job.encrypt_job_id, source_hash,
                                           rubric=artifacts['rubric'], search_plan=artifacts['search_plan'])
            inventory.save(paths.inventory_path)
            output_fn(f"岗位材料已保存；搜索路线 {len(artifacts['search_plan'].get('routes', []))} 条。")
            output_fn(f"搜索路线不足：{artifacts['search_plan'].get('generation_shortfall', 0)}；不以宽泛路线补足。")
            output_fn(f'可在 Codex 查看评分标准与搜索计划：{output_dir}')
        output_fn('核对材料后，下一步：.venv/bin/python scripts/run_single_job_live.py start --config default')
    return 0



def favorite_close_command(args: argparse.Namespace, *, now: Clock | None = None) -> int:
    from boss_hire.favorite_workflow import favorite_workflow_paths, load_active_favorite_workflow_session
    from boss_hire.favorite_workflow_command import close_favorite_workflow
    session = load_active_favorite_workflow_session(favorite_workflow_paths(favorite_account_state_dir()))
    if session is None:
        raise ValueError("没有活动收藏会话")
    close_favorite_workflow(session, note=args.note, updated_at=_local_now(now).isoformat())
    print("本地收藏会话已关闭；未访问 BOSS/LLM，未更改收藏账本或熔断。")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="唯一 BOSS launcher；执行使用 configs/start/status/continue，查看使用 report/pool。",
        epilog=(
            "主流程：configs → start --config <名称> → status/continue。"
            "查看：report（本轮）/ pool（总池）。authorize/run/favorite/clear-circuit 为兼容或独立高级入口。"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    jobs = subparsers.add_parser("jobs", help="查看本地岗位列表；--refresh/--select 由本人确认后各读取一个阶段。")
    job_action = jobs.add_mutually_exclusive_group()
    job_action.add_argument("--refresh", action="store_true", help="本人确认后只读取岗位列表一次")
    job_action.add_argument("--select", type=_positive_int, help="从本地列表选编号，本人确认后只读取此岗位 JD")
    jobs.set_defaults(handler=jobs_command)
    prepare_job = subparsers.add_parser("prepare-job", help="本人确认后为所选 JD 生成评分标准和搜索路线；不访问 BOSS。")
    prepare_job.set_defaults(handler=prepare_job_command)


    favorite_close = subparsers.add_parser("favorite-close", help="本人关闭尚未尝试交付的本地收藏会话，保留账本。")
    favorite_close.add_argument("--note", required=True, help="关闭原因")
    favorite_close.set_defaults(handler=favorite_close_command)

    configs = subparsers.add_parser("configs", help="列出固定目录中的两字段来源配置；不访问 BOSS/LLM。")
    configs.add_argument("--json", action="store_true", help="输出稳定 JSON contract")
    configs.set_defaults(handler=configs_command)

    status = subparsers.add_parser("status", help="显示当前本地运行状态；不访问 BOSS/LLM。")
    status.add_argument("--run", help="显式查看指定运行；省略时只读取活动运行指针")
    status.add_argument("--json", action="store_true", help="输出稳定 JSON contract")
    status.set_defaults(handler=status_command)

    report = subparsers.add_parser(
        "report",
        help="显示当前本轮供给漏斗与评分；不访问 BOSS/LLM。",
    )
    report.add_argument("--scores", action="store_true", help="仅显示本轮评分部分")
    report.add_argument("--top", type=_positive_int, help="显示本轮新评分前 N 名")
    report.add_argument("--json", action="store_true", help="输出稳定 JSON contract")
    report.set_defaults(handler=report_command)

    pool = subparsers.add_parser(
        "pool",
        help="显示当前活动岗位 × 评分卡的总池评分和本地收藏状态；不访问 BOSS/LLM。",
    )
    pool_display = pool.add_mutually_exclusive_group()
    pool_display.add_argument("--top", type=_positive_int, help="按总分仅显示前 N 人（默认 20）")
    pool_display.add_argument("--all", action="store_true", help="显示总池全部候选")
    pool.add_argument("--favorited-only", action="store_true", help="仅显示本地收藏注册表中已收藏的候选")
    pool.add_argument("--json", action="store_true", help="输出稳定 JSON contract")
    pool.set_defaults(handler=pool_command, top=20)

    start = subparsers.add_parser("start", help="选择一次来源配置，确认后启动新的来源阶段。")
    start.add_argument("--config", required=True, help="run_configs 下的配置名称，可省略 .json")
    start.add_argument(
        "--filter",
        action="append",
        default=[],
        help="搜索筛选，格式：中文字段=中文选项；可重复",
    )
    start.set_defaults(handler=start_command)

    continue_parser = subparsers.add_parser("continue", help="推进当前运行，至多执行一个外部阶段。")
    continue_parser.add_argument("--select", type=int, help="详情阶段仅选择前 N 名 pending 候选")
    continue_parser.set_defaults(handler=continue_command)

    close = subparsers.add_parser("close", help="显式关闭本地运行；不删除产物或清除 circuit。")
    close.add_argument("--run", help="无活动指针或需消歧时显式指定 run ID")
    close.add_argument("--note", required=True, help="本地关闭原因")
    close.set_defaults(handler=close_command)

    authorize = subparsers.add_parser(
        "authorize",
        help="旧高级入口：人工同步当前 Chrome 登录态并签发一次性授权。",
    )
    authorize_input = authorize.add_mutually_exclusive_group(required=True)
    authorize_input.add_argument("--config", type=Path, help="来源采集的两字段 JSON 配置")
    authorize_input.add_argument("--plan", type=Path, help="离线生成的候选详情或收藏同步计划")
    authorize.add_argument("--work-dir", type=Path, required=True)
    authorize.add_argument("--confirm-live", required=True, help="Asia/Shanghai 当天日期")
    authorize.add_argument("--note", required=True, help="人工账号检查与本轮授权说明")
    authorize.set_defaults(handler=authorize_command)

    run = subparsers.add_parser("run", help="旧高级入口：消费一次性授权并执行受控只读计划。")
    run.add_argument("--live", action="store_true", help="明确执行真实只读运行")
    run.add_argument("--authorization-id", required=True)
    run_input = run.add_mutually_exclusive_group(required=True)
    run_input.add_argument("--config", type=Path, help="必须与授权时相同")
    run_input.add_argument("--plan", type=Path, help="必须与授权时相同的候选详情或收藏同步计划")
    run.add_argument("--work-dir", type=Path, required=True, help="必须与授权时相同")
    run.set_defaults(handler=run_command)

    favorite = subparsers.add_parser(
        "favorite",
        help="独立高级入口：从已发布批次选择候选并生成 BOSS 收藏交付。",
    )
    favorite.add_argument("--batch", required=True, type=Path, help="candidate_shortlist.json")
    favorite.add_argument(
        "--sync-receipt",
        required=True,
        type=Path,
        help="本次收藏前独立完成的同批次收藏同步回执",
    )
    favorite.add_argument("--work-dir", required=True, type=Path)
    favorite.add_argument("--select", help="逗号分隔的发布排名；省略时交互输入")
    favorite.add_argument(
        "--retry-definite-failures",
        action="store_true",
        help="恢复模式：仅允许重新执行历史明确 favorite_failed；unknown/reserved 仍阻断",
    )
    favorite.add_argument("--live", action="store_true", help="明确执行真实收藏；仍需终端确认")
    favorite.set_defaults(handler=favorite_command)

    clear = subparsers.add_parser(
        "clear-circuit",
        help="旧人工恢复入口：复核后清除固定账号的本地 circuit。",
    )
    clear.add_argument("--note", required=True, help="人工复核说明")
    clear.set_defaults(handler=clear_circuit_command)
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    return build_parser().parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    return int(args.handler(args))


_parse_args_with_advanced_favorite = parse_args
_favorite_command_with_advanced_arguments = favorite_command


def _normal_favorite_command(
    args: argparse.Namespace,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    now: Callable[[], datetime] | None = None,
) -> int:
    """Prepare or resume the local, business-facing favorite workflow."""

    from boss_hire.boss_access import SHANGHAI, account_key_for
    from boss_hire.favorite_workflow_command import (
        build_favorite_workflow_sync_plan,
        confirm_favorite_sync,
        prepare_favorite_workflow,
        record_favorite_workflow_sync,
    )
    from boss_hire.single_job_run_plan import write_single_job_run_plan
    from boss_hire.supply_inventory import CandidateInventory
    from boss_hire.workflow_run import load_active_workflow_run

    current = now() if now is not None else datetime.now(tz=SHANGHAI)
    if current.tzinfo is None:
        current = current.replace(tzinfo=SHANGHAI)
    else:
        current = current.astimezone(SHANGHAI)
    paths = workflow_paths()
    run = load_active_workflow_run(paths)
    if run is None:
        raise ValueError("当前没有可收藏的岗位；请先完成一个岗位的本地评分")
    if run.state.get("status") != "scoring_complete":
        raise ValueError("当前岗位尚未完成本地评分，不能准备收藏名单")
    account_key = account_key_for(FIXED_AUTH_DIR)
    session = prepare_favorite_workflow(
        inventory=CandidateInventory.load(paths.inventory_path),
        account_state_dir=favorite_account_state_dir(account_key),
        account_key=account_key,
        job_id=str(run.state["job_id"]),
        job_title=str(run.state.get("job_title") or run.state["job_id"]),
        rubric_version=str(run.state["rubric_version"]),
        board_date=current.date().isoformat(),
        created_at=current.isoformat(),
    )
    snapshot = session.state["candidate_snapshot"]
    output_fn(f"当前岗位：{session.state['job_title']}")
    output_fn(f"本地有效评分池：{snapshot['actual_count']} 人")
    if snapshot["shortage_count"]:
        output_fn(f"本地候选不足 5 人：还差 {snapshot['shortage_count']} 人；系统不会自动补人")
    if not snapshot["actual_count"]:
        output_fn("当前没有可核对的候选；本次未访问 BOSS 或执行收藏。")
        return 0
    if session.state["status"] not in {"awaiting_sync_confirmation", "blocked_incomplete_sync"}:
        output_fn("本次收藏状态已保存；后续步骤将在下一阶段提供。")
        return 0
    plan = build_favorite_workflow_sync_plan(session, auth_dir=FIXED_AUTH_DIR)
    output_fn("本次将核对 BOSS 收藏状态，并从本地候选池选出未收藏且得分最高的 5 人。")
    output_fn("不会执行收藏，也不会增加搜索、详情或评分请求。")
    output_fn(f"最多读取 {plan['max_pages']} 页，严格串行，不重试。")
    confirm_favorite_sync(session, input_fn=input_fn)
    sync_root = session.state_path.parent / "sync"
    plan_path = sync_root / "plan.json"
    runs_root = sync_root / "runs"
    write_single_job_run_plan(plan, plan_path)
    execution_plan = _bound_read_plan(
        plan_path=plan_path,
        work_dir=runs_root,
        board_date=current.date().isoformat(),
    )
    auth_state = sync_auth_from_chrome(FIXED_AUTH_DIR)
    if not hasattr(auth_state, "get"):
        raise BossLiveAccessDenied("收藏状态核对认证状态无效")
    session_fingerprint = str(auth_state.get("session_fingerprint") or "").strip()
    authorization = LiveAuthorizationStore(FIXED_GUARD_DIR).issue(
        plan=execution_plan,
        session_fingerprint=session_fingerprint,
        confirm_live=current.date().isoformat(),
        note="收藏会话已确认核对当前收藏状态",
        now=lambda: current,
    )
    legacy_args = _parse_args_with_advanced_favorite(
        [
            "run",
            "--live",
            "--authorization-id",
            authorization.authorization_id,
            "--plan",
            str(plan_path),
            "--work-dir",
            str(runs_root),
        ]
    )
    result = run_command(legacy_args, now=lambda: current)
    receipt_paths = sorted(sync_root.rglob("favorite_sync_receipt.json"))
    if len(receipt_paths) != 1:
        raise RuntimeError("收藏状态核对未生成唯一回执")
    receipt = json.loads(receipt_paths[0].read_text(encoding="utf-8"))
    updated = record_favorite_workflow_sync(
        session,
        plan=execution_plan,
        receipt=receipt,
        updated_at=current.isoformat(),
    )
    if updated.state["status"] in {"awaiting_delivery_confirmation", "completed"}:
        selection = updated.state["final_selection"]
        output_fn("收藏状态核对完成：完整")
        output_fn(f"冻结排名池：{snapshot['actual_count']} 人")
        output_fn(f"同步后已收藏并排除：{len(selection['registry_excluded_candidate_ids'])} 人")
        output_fn(f"最终待收藏：{selection['actual_count']} 人")
        for display_rank, candidate in enumerate(selection["candidates"], start=1):
            display = candidate.get("display") or {}
            name = str(display.get("name") or candidate["candidate_id"])
            output_fn(f"{display_rank}. {name}  {candidate['score']} 分")
        if selection["actual_count"]:
            if selection["shortage_count"]:
                output_fn(f"未收藏候选不足 5 人：还差 {selection['shortage_count']} 人；不会扩大来源。")
            output_fn("本次未执行收藏。下一步：再次执行同一个 favorite 命令，选择并确认收藏。")
        else:
            output_fn("冻结排名池中的候选均已收藏，无需写入；本次流程已完成。")
    else:
        output_fn("收藏状态核对未完成；本次不会执行收藏。")
    return result


def parse_args(argv: Sequence[str] | None = None) -> argparse.Namespace:
    raw = list(sys.argv[1:] if argv is None else argv)
    if raw == ["favorite"]:
        return argparse.Namespace(
            command="favorite",
            favorite_workflow=True,
            handler=favorite_command,
        )
    return _parse_args_with_advanced_favorite(raw)


def favorite_command(
    args: argparse.Namespace,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    now: Callable[[], datetime] | None = None,
) -> int:
    if getattr(args, "favorite_workflow", False):
        return _normal_favorite_command(args, input_fn=input_fn, output_fn=output_fn, now=now)
    return _favorite_command_with_advanced_arguments(
        args,
        input_fn=input_fn,
        output_fn=output_fn,
        now=now,
    )


def _normal_favorite_delivery_command(session, *, input_fn, output_fn, now):
    from boss_hire.favorite_workflow_command import (
        materialize_favorite_workflow_delivery_inputs,
        record_favorite_workflow_delivery,
    )

    timestamp = _local_now(now)
    receipt_paths = sorted(
        (session.state_path.parent / "delivery").rglob("favorite_delivery_receipt.json")
    )
    if receipt_paths:
        if len(receipt_paths) != 1:
            raise ValueError("收藏交付回执缺失或不唯一")
        try:
            receipt = json.loads(receipt_paths[0].read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError("收藏交付回执无法读取") from exc
        if not isinstance(receipt, dict):
            raise ValueError("收藏交付回执无效")
        updated = record_favorite_workflow_delivery(
            session,
            receipt=receipt,
            updated_at=timestamp.isoformat(),
        )
        _render_normal_favorite_result(
            receipt,
            final_count=session.state["final_selection"]["actual_count"],
            output_fn=output_fn,
        )
        if updated.state["status"] != "completed":
            output_fn("收藏已停止，请勿重试本次名单。")
        return 0

    final_selection = session.state.get("final_selection")
    if not isinstance(final_selection, dict) or not final_selection.get("candidates"):
        raise ValueError("收藏会话缺少同步后冻结的最终待收藏名单；未访问 BOSS")
    candidates = final_selection["candidates"]
    output_fn("以下名单已完成收藏状态核对：")
    display_to_pool_rank: dict[int, int] = {}
    for display_rank, candidate in enumerate(candidates, start=1):
        display = candidate.get("display") or {}
        name = str(display.get("name") or "候选人").strip()
        output_fn(f"{display_rank}. {name}｜匹配分 {candidate['score']}")
        display_to_pool_rank[display_rank] = candidate["rank"]
    try:
        selection = input_fn(f"请输入要收藏的编号；直接回车默认全部 {len(candidates)} 人：")
    except EOFError as exc:
        raise BossLiveAccessDenied("未选择要收藏的人") from exc
    selection_text = str(selection or "").strip() or ",".join(map(str, display_to_pool_rank))
    display_ranks = _parse_selected_ranks(selection_text)
    if len(set(display_ranks)) != len(display_ranks):
        raise ValueError("收藏编号不能重复")
    try:
        pool_ranks = [display_to_pool_rank[rank] for rank in display_ranks]
    except KeyError as exc:
        raise ValueError(f"收藏编号超出最终待收藏名单：{exc.args[0]}") from exc
    selected_names = "、".join(
        str((candidates[rank - 1].get("display") or {}).get("name") or "候选人").strip()
        for rank in display_ranks
    )
    expected = f"确认收藏{len(pool_ranks)}人"
    try:
        confirmation = input_fn(f"即将新增收藏 {len(pool_ranks)} 人：{selected_names}\n输入“{expected}”继续：")
    except EOFError as exc:
        raise BossLiveAccessDenied("收藏确认未完成") from exc
    if str(confirmation or "").strip() != expected:
        raise BossLiveAccessDenied("收藏确认文本不匹配，未同步登录态、未访问 BOSS")

    inputs = materialize_favorite_workflow_delivery_inputs(session)
    legacy_args = argparse.Namespace(
        command="favorite",
        batch=inputs.batch_path,
        sync_receipt=inputs.sync_receipt_path,
        work_dir=inputs.work_dir,
        select=",".join(map(str, pool_ranks)),
        retry_definite_failures=False,
        live=True,
    )
    result = _favorite_command_with_advanced_arguments(
        legacy_args,
        input_fn=lambda _prompt: f"确认{len(pool_ranks)}人",
        output_fn=lambda _message: None,
        now=now,
    )
    if result != 0:
        return result
    receipts = sorted(inputs.work_dir.rglob("favorite_delivery_receipt.json"))
    if len(receipts) != 1:
        raise ValueError("收藏交付回执缺失或不唯一")
    try:
        receipt = json.loads(receipts[0].read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError("收藏交付回执无法读取") from exc
    if not isinstance(receipt, dict):
        raise ValueError("收藏交付回执无效")
    updated = record_favorite_workflow_delivery(
        session,
        receipt=receipt,
        updated_at=timestamp.isoformat(),
    )
    _render_normal_favorite_result(
        receipt,
        final_count=final_selection["actual_count"],
        output_fn=output_fn,
    )
    if updated.state["status"] != "completed":
        output_fn("收藏已停止，请勿重试本次名单。")
    return result


def _render_normal_favorite_result(receipt, *, final_count, output_fn):
    selected_count = int(receipt.get("selected_count") or 0)
    output_fn(f"新增收藏：{int(receipt.get('confirmed_count') or 0)}")
    output_fn(f"已收藏而跳过：{int(receipt.get('already_confirmed_count') or 0)}")
    output_fn(f"失败：{int(receipt.get('failed_count') or 0)}")
    output_fn(f"结果不明：{int(receipt.get('unknown_count') or 0)}")
    output_fn(f"未选择：{max(0, int(final_count) - selected_count)}")
    output_fn("本次收藏已完成" if not receipt.get("failed_count") and not receipt.get("unknown_count") else "本次收藏未完整完成")


_favorite_command_with_normal_workflow = favorite_command


def favorite_command(
    args: argparse.Namespace,
    *,
    input_fn: Callable[[str], str] = input,
    output_fn: Callable[[str], None] = print,
    now: Clock | None = None,
) -> int:
    """Route a resumed normal session directly to its next user-visible stage."""

    if getattr(args, "favorite_workflow", False):
        from boss_hire.favorite_workflow import (
            favorite_workflow_paths,
            load_active_favorite_workflow_session,
        )

        session = load_active_favorite_workflow_session(
            favorite_workflow_paths(favorite_account_state_dir())
        )
        timestamp = _local_now(now)
        if (
            session is not None
            and session.state.get("board_date") != timestamp.date().isoformat()
        ):
            from boss_hire.favorite_workflow_command import expire_favorite_workflow_session

            session = expire_favorite_workflow_session(
                session,
                updated_at=timestamp.isoformat(),
            )
        if session is not None and session.state.get("status") == "awaiting_delivery_confirmation":
            from boss_hire.workflow_run import load_active_workflow_run as load_current_run
            run = load_current_run(workflow_paths())
            if (run is None or run.state.get("status") != "scoring_complete"
                or session.state.get("job_id") != run.state.get("job_id")
                or session.state.get("rubric_version") != run.state.get("rubric_version")):
                raise ValueError("收藏会话与当前岗位/评分版本不一致；未访问 BOSS。请本人 favorite-close --note <原因>，保留账本")
            return _normal_favorite_delivery_command(
                session,
                input_fn=input_fn,
                output_fn=output_fn,
                now=now,
            )
    return _favorite_command_with_normal_workflow(
        args,
        input_fn=input_fn,
        output_fn=output_fn,
        now=now,
    )


if __name__ == "__main__":
    raise SystemExit(main())
