from __future__ import annotations

import fcntl
import json
import os
import re
from contextlib import contextmanager
from copy import deepcopy
from dataclasses import dataclass
from datetime import date
from pathlib import Path, PurePosixPath
from typing import Any, Iterator, Mapping

from boss_hire.local_security import atomic_write_json, ensure_private_directory, ensure_private_file
from boss_hire.single_job_run_plan import read_single_job_run_config
from boss_hire.state_store import content_hash


WORKFLOW_RUN_SCHEMA_VERSION = 1
WORKFLOW_RUN_CONTRACT = "boss_hire_workflow_run"
ACTIVE_RUN_SCHEMA_VERSION = 1
ACTIVE_RUN_CONTRACT = "boss_hire_active_workflow_run"
RUN_STATUSES = {
    "awaiting_source_confirmation",
    "source_complete",
    "detail_complete",
    "scoring_complete",
    "blocked",
    "closed",
}
TERMINAL_RUN_STATUSES = {"scoring_complete", "closed"}
ALLOWED_TRANSITIONS = {
    "awaiting_source_confirmation": {"source_complete", "blocked", "closed"},
    "source_complete": {"detail_complete", "blocked", "closed"},
    "detail_complete": {"scoring_complete", "blocked", "closed"},
    "scoring_complete": {"closed"},
    "blocked": {"awaiting_source_confirmation", "source_complete", "detail_complete", "closed"},
    "closed": set(),
}
NEXT_ACTION_FOR_STATUS = {
    "awaiting_source_confirmation": "source_collection",
    "source_complete": "candidate_details",
    "detail_complete": "llm_scoring",
    "scoring_complete": "publish_manual",
    "blocked": "resolve_blocker",
    "closed": "none",
}
ARTIFACT_FIELDS = {
    "source_plan",
    "source_collection",
    "source_receipt",
    "detail_plan",
    "detail_collection",
    "detail_receipt",
    "llm_confirmation_receipt",
    "score_summary",
    "close_receipt",
}
_SAFE_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*")
_HEX_64 = re.compile(r"[0-9a-f]{64}")
_ACCOUNT_KEY = re.compile(r"[0-9a-f]{16}")
PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_ROOT = PROJECT_ROOT / "data/local/run_configs"
DEFAULT_WORK_ROOT = PROJECT_ROOT / "data/local/single_job_runs"


@dataclass(frozen=True)
class WorkflowPaths:
    project_root: Path
    config_root: Path
    work_root: Path
    inventory_path: Path
    active_run_path: Path
    runs_root: Path


@dataclass(frozen=True)
class ResolvedRunConfig:
    name: str
    path: Path
    value: dict[str, int]
    digest: str


@dataclass(frozen=True)
class LoadedWorkflowRun:
    state: dict[str, Any]
    state_path: Path
    state_digest: str


def workflow_paths(project_root: Path = PROJECT_ROOT) -> WorkflowPaths:
    stable_project_root = Path(project_root).resolve(strict=False)
    work_root = stable_project_root / "data/local/single_job_runs"
    return WorkflowPaths(
        project_root=stable_project_root,
        config_root=stable_project_root / "data/local/run_configs",
        work_root=work_root,
        inventory_path=work_root / "candidate_inventory.json",
        active_run_path=work_root / "active_run.json",
        runs_root=work_root / "workflow_runs",
    )


def ensure_workflow_directories(paths: WorkflowPaths) -> None:
    ensure_private_directory(paths.work_root)
    ensure_private_directory(paths.runs_root)


@contextmanager
def _workflow_lock(paths: WorkflowPaths) -> Iterator[None]:
    ensure_workflow_directories(paths)
    lock_path = paths.work_root / ".workflow.lock"
    lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
    os.chmod(lock_path, 0o600)
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        os.close(lock_fd)


def _read_json_object(path: Path, label: str) -> dict[str, Any]:
    if path.is_symlink():
        raise ValueError(f"{label} 不允许使用符号链接")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"{label} 不存在：{path}") from exc
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"{label} 无法读取：{path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"{label} 必须是 JSON 对象")
    ensure_private_file(path)
    return value


def _run_state_path(paths: WorkflowPaths, run_id: str) -> Path:
    stable_run_id = _required_text(run_id, "run_id")
    if _SAFE_ID.fullmatch(stable_run_id) is None:
        raise ValueError("run_id 包含不安全字符")
    return paths.runs_root / stable_run_id / "run_state.json"


def _active_pointer(*, run_id: str, state_digest: str) -> dict[str, Any]:
    return {
        "schema_version": ACTIVE_RUN_SCHEMA_VERSION,
        "contract": ACTIVE_RUN_CONTRACT,
        "run_id": run_id,
        "state_path": f"workflow_runs/{run_id}/run_state.json",
        "state_digest": state_digest,
    }


def _validate_active_pointer(value: Mapping[str, Any]) -> dict[str, Any]:
    pointer = dict(value)
    if set(pointer) != {"schema_version", "contract", "run_id", "state_path", "state_digest"}:
        raise ValueError("活动运行指针字段无效")
    if pointer.get("schema_version") != ACTIVE_RUN_SCHEMA_VERSION:
        raise ValueError("活动运行指针 schema_version 无效")
    if pointer.get("contract") != ACTIVE_RUN_CONTRACT:
        raise ValueError("活动运行指针 contract 无效")
    run_id = _required_text(pointer.get("run_id"), "active_run.run_id")
    if _SAFE_ID.fullmatch(run_id) is None:
        raise ValueError("活动运行指针 run_id 不安全")
    expected_path = f"workflow_runs/{run_id}/run_state.json"
    if pointer.get("state_path") != expected_path:
        raise ValueError("活动运行指针 state_path 与 run_id 不一致")
    _digest(pointer.get("state_digest"), "active_run.state_digest")
    return pointer


def _verify_artifacts(run_dir: Path, state: Mapping[str, Any]) -> None:
    for name, reference in state["artifacts"].items():
        artifact_path = run_dir / reference["path"]
        if artifact_path.is_symlink():
            raise ValueError(f"运行产物 {name} 不允许使用符号链接")
        try:
            resolved = artifact_path.resolve(strict=True)
        except OSError as exc:
            raise ValueError(f"运行产物 {name} 不存在") from exc
        if resolved.parent != run_dir and run_dir not in resolved.parents:
            raise ValueError(f"运行产物 {name} 越出运行目录")
        artifact = _read_json_object(resolved, f"运行产物 {name}")
        if content_hash(artifact) != reference["digest"]:
            raise ValueError(f"运行产物 {name} 摘要不一致")


def load_workflow_run(paths: WorkflowPaths, run_id: str) -> LoadedWorkflowRun:
    state_path = _run_state_path(paths, run_id)
    state = validate_workflow_run_state(_read_json_object(state_path, "运行状态"))
    if state["run_id"] != run_id:
        raise ValueError("运行状态 run_id 与目录不一致")
    _verify_artifacts(state_path.parent.resolve(strict=True), state)
    return LoadedWorkflowRun(
        state=state,
        state_path=state_path,
        state_digest=content_hash(state),
    )


def _load_active_unlocked(paths: WorkflowPaths) -> LoadedWorkflowRun | None:
    if not paths.active_run_path.exists():
        return None
    pointer = _validate_active_pointer(_read_json_object(paths.active_run_path, "活动运行指针"))
    loaded = load_workflow_run(paths, pointer["run_id"])
    if loaded.state_digest != pointer["state_digest"]:
        raise ValueError("活动运行指针与运行状态摘要不一致")
    return loaded


def load_active_workflow_run(paths: WorkflowPaths) -> LoadedWorkflowRun | None:
    with _workflow_lock(paths):
        return _load_active_unlocked(paths)


def create_workflow_run(paths: WorkflowPaths, state: Mapping[str, Any]) -> LoadedWorkflowRun:
    return create_workflow_run_with_artifacts(paths, state, artifact_values={})


def create_workflow_run_with_artifacts(
    paths: WorkflowPaths,
    state: Mapping[str, Any],
    *,
    artifact_values: Mapping[str, Mapping[str, Any]],
) -> LoadedWorkflowRun:
    normalized = validate_workflow_run_state(state)
    if set(artifact_values) != set(normalized["artifacts"]):
        raise ValueError("初始运行产物与状态引用不一致")
    with _workflow_lock(paths):
        active = _load_active_unlocked(paths)
        if active is not None and active.state["status"] not in TERMINAL_RUN_STATUSES:
            raise ValueError(f"已有活动运行：{active.state['run_id']}")
        source = artifact_values.get("source_plan", {})
        if source.get("selected_job_id"):
            from boss_hire.job_setup import resolve_prepared_job
            from boss_hire.supply_inventory import CandidateInventory
            job_id, materials = resolve_prepared_job(
                paths.work_root, CandidateInventory.load(paths.inventory_path).to_dict()["job_artifacts"],
                account_key=normalized["account_key"],
            )
            if (job_id != normalized["job_id"] or materials.get("source_jd_hash") != normalized["source_jd_hash"]
                or content_hash(materials.get("rubric")) != normalized["rubric_digest"]
                or content_hash(materials.get("search_plan")) != normalized["search_plan_digest"]):
                raise ValueError("创建运行前岗位材料漂移；未冻结运行")
        state_path = _run_state_path(paths, normalized["run_id"])
        if state_path.exists() or state_path.parent.exists():
            raise ValueError(f"运行实例已存在：{normalized['run_id']}")
        ensure_private_directory(state_path.parent)
        for name, value in artifact_values.items():
            reference = normalized["artifacts"][name]
            if content_hash(value) != reference["digest"]:
                raise ValueError(f"初始运行产物 {name} 摘要不一致")
            artifact_path = state_path.parent / reference["path"]
            ensure_private_directory(artifact_path.parent)
            atomic_write_json(artifact_path, dict(value), sort_keys=True)
        _verify_artifacts(state_path.parent.resolve(strict=True), normalized)
        atomic_write_json(state_path, normalized, sort_keys=True)
        state_digest = content_hash(normalized)
        atomic_write_json(
            paths.active_run_path,
            _active_pointer(run_id=normalized["run_id"], state_digest=state_digest),
            sort_keys=True,
        )
        return LoadedWorkflowRun(normalized, state_path, state_digest)


def save_active_workflow_run(
    paths: WorkflowPaths,
    state: Mapping[str, Any],
    *,
    expected_state_digest: str,
) -> LoadedWorkflowRun:
    normalized = validate_workflow_run_state(state)
    stable_expected = _digest(expected_state_digest, "expected_state_digest")
    with _workflow_lock(paths):
        active = _load_active_unlocked(paths)
        if active is None:
            raise ValueError("没有活动运行")
        if active.state["run_id"] != normalized["run_id"]:
            raise ValueError("活动运行与待保存状态不一致")
        if active.state_digest != stable_expected:
            raise ValueError("运行状态已变化，拒绝覆盖")
        _verify_artifacts(active.state_path.parent.resolve(strict=True), normalized)
        atomic_write_json(active.state_path, normalized, sort_keys=True)
        state_digest = content_hash(normalized)
        atomic_write_json(
            paths.active_run_path,
            _active_pointer(run_id=normalized["run_id"], state_digest=state_digest),
            sort_keys=True,
        )
        return LoadedWorkflowRun(normalized, active.state_path, state_digest)


def save_workflow_run(
    paths: WorkflowPaths,
    state: Mapping[str, Any],
    *,
    expected_state_digest: str,
) -> LoadedWorkflowRun:
    normalized = validate_workflow_run_state(state)
    stable_expected = _digest(expected_state_digest, "expected_state_digest")
    with _workflow_lock(paths):
        current = load_workflow_run(paths, normalized["run_id"])
        if current.state_digest != stable_expected:
            raise ValueError("运行状态已变化，拒绝覆盖")
        _verify_artifacts(current.state_path.parent.resolve(strict=True), normalized)
        atomic_write_json(current.state_path, normalized, sort_keys=True)
        state_digest = content_hash(normalized)
        if paths.active_run_path.exists():
            active = _load_active_unlocked(paths)
            if active is not None and active.state["run_id"] == normalized["run_id"]:
                atomic_write_json(
                    paths.active_run_path,
                    _active_pointer(run_id=normalized["run_id"], state_digest=state_digest),
                    sort_keys=True,
                )
        return LoadedWorkflowRun(normalized, current.state_path, state_digest)


def list_workflow_run_ids(paths: WorkflowPaths) -> list[str]:
    if not paths.runs_root.exists():
        return []
    result: list[str] = []
    for path in sorted(paths.runs_root.iterdir(), key=lambda item: item.name):
        if path.is_symlink() or not path.is_dir() or _SAFE_ID.fullmatch(path.name) is None:
            continue
        if (path / "run_state.json").is_file():
            result.append(path.name)
    return result


def resolve_run_config(name: str, *, config_root: Path = DEFAULT_CONFIG_ROOT) -> ResolvedRunConfig:
    stable_name = _required_text(name, "config name")
    if stable_name.endswith(".json"):
        stable_name = stable_name[:-5]
    if _SAFE_ID.fullmatch(stable_name) is None or stable_name in {".", ".."}:
        raise ValueError("config name 必须是安全文件名，不允许路径或目录穿越")
    root = Path(config_root).resolve(strict=False)
    candidate = root / f"{stable_name}.json"
    if candidate.is_symlink():
        raise ValueError("run config 不允许使用符号链接")
    try:
        resolved = candidate.resolve(strict=True)
    except OSError as exc:
        raise ValueError(f"run config 不存在：{stable_name}") from exc
    if resolved.parent != root:
        raise ValueError("run config 必须位于固定配置目录")
    config = read_single_job_run_config(resolved)
    value = {
        "recommendation_source_enabled": config.recommendation_source_enabled,
        "top_priority_search_query_count": config.top_priority_search_query_count,
        "second_page_search_query_count": config.second_page_search_query_count,
        "recent_view_filter": config.recent_view_filter,
    }
    return ResolvedRunConfig(
        name=stable_name,
        path=resolved,
        value=value,
        digest=content_hash(value),
    )


def _required_text(value: Any, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} 不能为空")
    return result


def _digest(value: Any, field: str) -> str:
    result = _required_text(value, field)
    if _HEX_64.fullmatch(result) is None:
        raise ValueError(f"{field} 必须是 64 位小写十六进制摘要")
    return result


def _relative_artifact_path(value: Any, field: str) -> str:
    result = _required_text(value, field)
    path = PurePosixPath(result)
    if path.is_absolute() or ".." in path.parts or result != path.as_posix():
        raise ValueError(f"{field} 必须是规范的运行目录相对路径")
    return result


def validate_workflow_run_state(value: Mapping[str, Any]) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("运行实例必须是 JSON 对象")
    state = deepcopy(dict(value))
    if state.get("schema_version") != WORKFLOW_RUN_SCHEMA_VERSION:
        raise ValueError("运行实例 schema_version 无效")
    if state.get("contract") != WORKFLOW_RUN_CONTRACT:
        raise ValueError("运行实例 contract 无效")
    run_id = _required_text(state.get("run_id"), "run_id")
    config_name = _required_text(state.get("config_name"), "config_name")
    if _SAFE_ID.fullmatch(run_id) is None or _SAFE_ID.fullmatch(config_name) is None:
        raise ValueError("run_id/config_name 包含不安全字符")
    board_date = _required_text(state.get("board_date"), "board_date")
    try:
        date.fromisoformat(board_date)
    except ValueError as exc:
        raise ValueError("board_date 必须是 YYYY-MM-DD") from exc
    account_key = _required_text(state.get("account_key"), "account_key")
    if _ACCOUNT_KEY.fullmatch(account_key) is None:
        raise ValueError("account_key 必须是 16 位小写十六进制摘要")
    for field in (
        "config_digest",
        "source_jd_hash",
        "search_plan_digest",
        "rubric_digest",
    ):
        _digest(state.get(field), field)
    _required_text(state.get("job_id"), "job_id")
    _required_text(state.get("search_plan_version"), "search_plan_version")
    _required_text(state.get("rubric_version"), "rubric_version")
    _required_text(state.get("created_at"), "created_at")
    _required_text(state.get("updated_at"), "updated_at")
    status = _required_text(state.get("status"), "status")
    if status not in RUN_STATUSES:
        raise ValueError("运行实例 status 无效")
    expected_next_action = NEXT_ACTION_FOR_STATUS[status]
    if state.get("next_action") != expected_next_action:
        raise ValueError("运行实例 next_action 与 status 不一致")
    config = state.get("config")
    historical_config_fields = {
        "recommendation_source_enabled",
        "top_priority_search_query_count",
    }
    page2_config_fields = historical_config_fields | {"second_page_search_query_count"}
    current_config_fields = page2_config_fields | {"recent_view_filter"}
    if not isinstance(config, Mapping) or set(config) not in (
        historical_config_fields,
        page2_config_fields,
        current_config_fields,
    ):
        raise ValueError("运行实例 config 必须冻结受支持的来源配置")
    recommendation_enabled = config.get("recommendation_source_enabled")
    query_count = config.get("top_priority_search_query_count")
    if recommendation_enabled not in {0, 1} or isinstance(recommendation_enabled, bool):
        raise ValueError("运行实例 recommendation_source_enabled 无效")
    if not isinstance(query_count, int) or isinstance(query_count, bool) or query_count < 0:
        raise ValueError("运行实例 top_priority_search_query_count 无效")
    second_page_count = config.get("second_page_search_query_count", 0)
    if (
        not isinstance(second_page_count, int)
        or isinstance(second_page_count, bool)
        or second_page_count < 0
        or second_page_count > query_count
    ):
        raise ValueError("运行实例 second_page_search_query_count 无效")
    if recommendation_enabled == 0 and query_count == 0:
        raise ValueError("运行实例至少启用一个来源")
    recent_view_filter = str(config.get("recent_view_filter", "include_all") or "").strip()
    if recent_view_filter not in {"include_all", "exclude_14d"}:
        raise ValueError("运行实例 recent_view_filter 无效")
    if query_count == 0 and recent_view_filter != "include_all":
        raise ValueError("未启用搜索时运行实例 recent_view_filter 必须是 include_all")
    artifacts = state.get("artifacts")
    if not isinstance(artifacts, Mapping) or set(artifacts) - ARTIFACT_FIELDS:
        raise ValueError("运行实例 artifacts 无效")
    normalized_artifacts: dict[str, dict[str, str]] = {}
    for name, artifact in artifacts.items():
        if not isinstance(artifact, Mapping) or set(artifact) != {"path", "digest"}:
            raise ValueError(f"运行实例 artifact {name} 无效")
        normalized_artifacts[str(name)] = {
            "path": _relative_artifact_path(artifact.get("path"), f"artifacts.{name}.path"),
            "digest": _digest(artifact.get("digest"), f"artifacts.{name}.digest"),
        }
    last_error = state.get("last_error")
    if last_error is not None and not isinstance(last_error, str):
        raise ValueError("运行实例 last_error 必须是字符串或 null")
    state.update(
        {
            "run_id": run_id,
            "config_name": config_name,
            "board_date": board_date,
            "account_key": account_key,
            "status": status,
            "next_action": expected_next_action,
            "config": dict(config),
            "artifacts": normalized_artifacts,
        }
    )
    return state


def build_workflow_run_state(
    *,
    run_id: str,
    config_name: str,
    config: Mapping[str, Any],
    config_digest: str,
    board_date: str,
    account_key: str,
    job_id: str,
    source_jd_hash: str,
    search_plan_version: str,
    search_plan_digest: str,
    rubric_version: str,
    rubric_digest: str,
    created_at: str,
) -> dict[str, Any]:
    return validate_workflow_run_state(
        {
            "schema_version": WORKFLOW_RUN_SCHEMA_VERSION,
            "contract": WORKFLOW_RUN_CONTRACT,
            "run_id": run_id,
            "status": "awaiting_source_confirmation",
            "config_name": config_name,
            "config": dict(config),
            "config_digest": config_digest,
            "board_date": board_date,
            "account_key": account_key,
            "job_id": job_id,
            "source_jd_hash": source_jd_hash,
            "search_plan_version": search_plan_version,
            "search_plan_digest": search_plan_digest,
            "rubric_version": rubric_version,
            "rubric_digest": rubric_digest,
            "artifacts": {},
            "next_action": "source_collection",
            "last_error": None,
            "created_at": created_at,
            "updated_at": created_at,
        }
    )


def transition_workflow_run_state(
    value: Mapping[str, Any],
    *,
    status: str,
    updated_at: str,
    artifacts: Mapping[str, Mapping[str, str]] | None = None,
    last_error: str | None = None,
) -> dict[str, Any]:
    current = validate_workflow_run_state(value)
    target_status = _required_text(status, "status")
    if target_status not in ALLOWED_TRANSITIONS[current["status"]]:
        raise ValueError(f"非法运行状态转换：{current['status']} -> {target_status}")
    merged_artifacts = dict(current["artifacts"])
    if artifacts:
        merged_artifacts.update({str(key): dict(item) for key, item in artifacts.items()})
    current.update(
        {
            "status": target_status,
            "next_action": NEXT_ACTION_FOR_STATUS[target_status],
            "updated_at": _required_text(updated_at, "updated_at"),
            "artifacts": merged_artifacts,
            "last_error": last_error,
        }
    )
    return validate_workflow_run_state(current)


def bind_workflow_run_artifacts(
    value: Mapping[str, Any],
    *,
    updated_at: str,
    artifacts: Mapping[str, Mapping[str, str]],
) -> dict[str, Any]:
    current = validate_workflow_run_state(value)
    merged_artifacts = dict(current["artifacts"])
    merged_artifacts.update({str(key): dict(item) for key, item in artifacts.items()})
    current.update(
        {
            "updated_at": _required_text(updated_at, "updated_at"),
            "artifacts": merged_artifacts,
        }
    )
    return validate_workflow_run_state(current)
