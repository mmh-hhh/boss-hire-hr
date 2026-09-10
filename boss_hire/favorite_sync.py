from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from boss_hire.boss_access import SHANGHAI
from boss_hire.favorite_registry import FavoriteRegistry
from boss_hire.favorite_sync_contract import (
    FAVORITE_LIST_MAX_PAGES,
    FAVORITE_LIST_PAGE_SIZE,
    FAVORITE_SYNC_RECEIPT_CONTRACT,
    SYNC_MODES,
    SYNC_PURPOSES,
)
from boss_hire.safe_recruiter_client import BossRiskStop


class FavoriteSyncContractError(ValueError):
    pass


@dataclass(frozen=True)
class FavoriteListPage:
    candidate_ids: tuple[str, ...]
    has_more: bool


@dataclass(frozen=True)
class FavoritePageCollection:
    mode: str
    status: str
    complete: bool
    pages_read: int
    first_page_ids: tuple[str, ...]
    observed_candidate_ids: tuple[str, ...]
    error: str | None = None


@dataclass(frozen=True)
class FavoriteSyncPersistence:
    receipt_id: str
    observed_count: int
    new_candidate_count: int
    checkpoint_advanced: bool


def _receipt_local_date(value: object, *, field: str) -> str:
    text = str(value or "").strip()
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValueError(f"{field} must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        raise ValueError(f"{field} must include a timezone")
    return parsed.astimezone(SHANGHAI).date().isoformat()


def validate_favorite_sync_receipt(
    receipt: Mapping[str, Any],
    *,
    account_key: str,
    board_date: str,
    purpose: str,
    require_complete: bool = False,
    batch_id: str | None = None,
    batch_digest: str | None = None,
) -> dict[str, Any]:
    if not isinstance(receipt, Mapping):
        raise ValueError("favorite sync receipt must be an object")
    stable_account_key = str(account_key or "").strip()
    stable_board_date = str(board_date or "").strip()
    stable_purpose = str(purpose or "").strip()
    if stable_purpose not in SYNC_PURPOSES:
        raise ValueError("favorite sync receipt purpose is invalid")
    if (
        receipt.get("schema_version") != 1
        or receipt.get("contract") != FAVORITE_SYNC_RECEIPT_CONTRACT
        or receipt.get("account_key") != stable_account_key
        or receipt.get("board_date") != stable_board_date
        or receipt.get("purpose") != stable_purpose
    ):
        raise ValueError("favorite sync receipt identity or account binding is invalid")
    if _receipt_local_date(receipt.get("completed_at"), field="completed_at") != stable_board_date:
        raise ValueError("favorite sync receipt is not from the publish date")
    plan_id = str(receipt.get("plan_id") or "").strip()
    status = str(receipt.get("status") or "").strip()
    complete = receipt.get("complete")
    checkpoint_advanced = receipt.get("checkpoint_advanced")
    pages_read = receipt.get("pages_read")
    max_pages = receipt.get("max_pages")
    if not plan_id or not isinstance(complete, bool) or not isinstance(checkpoint_advanced, bool):
        raise ValueError("favorite sync receipt status fields are invalid")
    if (
        not isinstance(pages_read, int)
        or isinstance(pages_read, bool)
        or pages_read < 0
        or not isinstance(max_pages, int)
        or isinstance(max_pages, bool)
        or max_pages < 1
        or max_pages > FAVORITE_LIST_MAX_PAGES
        or pages_read > max_pages
    ):
        raise ValueError("favorite sync receipt page counts are invalid")
    complete_statuses = {"anchor_reached", "end_reached"}
    incomplete_statuses = {"sync_incomplete", "risk_stopped", "failed"}
    if complete:
        if status not in complete_statuses or checkpoint_advanced is not True:
            raise ValueError("favorite sync receipt complete status is inconsistent")
    elif status not in incomplete_statuses or checkpoint_advanced is not False:
        raise ValueError("favorite sync receipt incomplete status is inconsistent")
    if require_complete and not complete:
        raise ValueError("favorite sync receipt must be complete")
    first_page_ids = receipt.get("first_page_ids")
    if not isinstance(first_page_ids, list):
        raise ValueError("favorite sync receipt first_page_ids must be a list")
    _stable_candidate_ids(first_page_ids, field="first_page_ids")
    if len(first_page_ids) > FAVORITE_LIST_PAGE_SIZE:
        raise ValueError("favorite sync receipt first_page_ids cannot exceed 10")
    if stable_purpose == "publish":
        if receipt.get("batch_id") is not None or receipt.get("batch_digest") is not None:
            raise ValueError("publish favorite sync receipt cannot bind a batch")
    else:
        if (
            not batch_id
            or not batch_digest
            or receipt.get("batch_id") != batch_id
            or receipt.get("batch_digest") != batch_digest
        ):
            raise ValueError("favorite delivery sync receipt batch binding is invalid")
    return dict(receipt)


def _stable_candidate_ids(values: Sequence[object], *, field: str) -> tuple[str, ...]:
    if isinstance(values, (str, bytes)):
        raise ValueError(f"{field} must be a sequence")
    result = tuple(str(value or "").strip() for value in values)
    if any(not value for value in result):
        raise ValueError(f"{field} cannot contain empty candidate ids")
    if len(set(result)) != len(result):
        raise ValueError(f"{field} cannot contain duplicate candidate ids")
    return result


def parse_favorite_list_page(payload: Mapping[str, Any]) -> FavoriteListPage:
    if not isinstance(payload, Mapping):
        raise FavoriteSyncContractError("favorite list response must be an object")
    try:
        code = int(payload.get("code"))
    except (TypeError, ValueError) as exc:
        raise FavoriteSyncContractError("favorite list response code is invalid") from exc
    if code != 0:
        raise FavoriteSyncContractError(f"favorite list business code {code}")
    data = payload.get("zpData")
    if not isinstance(data, Mapping):
        raise FavoriteSyncContractError("favorite list zpData must be an object")
    if "cardList" not in data:
        raise FavoriteSyncContractError("favorite list zpData.cardList is required")
    cards = data.get("cardList")
    if not isinstance(cards, list):
        raise FavoriteSyncContractError("favorite list zpData.cardList must be a list")
    if len(cards) > FAVORITE_LIST_PAGE_SIZE:
        raise FavoriteSyncContractError(
            f"favorite list page cannot contain more than {FAVORITE_LIST_PAGE_SIZE} cards"
        )
    has_more = data.get("hasMore")
    if not isinstance(has_more, bool):
        raise FavoriteSyncContractError("favorite list zpData.hasMore must be a boolean")
    candidate_ids: list[str] = []
    for index, card in enumerate(cards):
        if not isinstance(card, Mapping):
            raise FavoriteSyncContractError(f"favorite list cardList[{index}] must be an object")
        candidate_id = str(card.get("encryptGeekId") or "").strip()
        if not candidate_id:
            raise FavoriteSyncContractError(
                f"favorite list cardList[{index}].encryptGeekId is required"
            )
        candidate_ids.append(candidate_id)
    if len(set(candidate_ids)) != len(candidate_ids):
        raise FavoriteSyncContractError("favorite list page contains duplicate candidate ids")
    return FavoriteListPage(candidate_ids=tuple(candidate_ids), has_more=has_more)


def _contains_ordered_anchor(
    observed_candidate_ids: Sequence[str],
    anchor_group: Sequence[str],
) -> bool:
    anchor_length = len(anchor_group)
    if anchor_length == 0 or len(observed_candidate_ids) < anchor_length:
        return False
    anchor = tuple(anchor_group)
    return any(
        tuple(observed_candidate_ids[index : index + anchor_length]) == anchor
        for index in range(len(observed_candidate_ids) - anchor_length + 1)
    )


def collect_favorite_pages(
    *,
    fetch_page: Callable[[int], Mapping[str, Any]],
    mode: str,
    anchor_group: Sequence[str],
    max_pages: int = FAVORITE_LIST_MAX_PAGES,
) -> FavoritePageCollection:
    stable_mode = str(mode or "").strip()
    if stable_mode not in SYNC_MODES:
        raise ValueError("favorite sync mode must be initialize or incremental")
    if (
        not isinstance(max_pages, int)
        or isinstance(max_pages, bool)
        or max_pages < 1
        or max_pages > FAVORITE_LIST_MAX_PAGES
    ):
        raise ValueError("favorite sync max_pages must be between 1 and 40")
    stable_anchor = _stable_candidate_ids(anchor_group, field="anchor_group")
    if stable_mode == "initialize" and stable_anchor:
        raise ValueError("initialize favorite sync cannot use an anchor group")
    if len(stable_anchor) > FAVORITE_LIST_PAGE_SIZE:
        raise ValueError("favorite sync anchor_group cannot contain more than 10 candidate ids")

    observed: list[str] = []
    first_page_ids: tuple[str, ...] = ()
    page_signatures: set[tuple[str, ...]] = set()
    pages_read = 0
    for page_number in range(1, max_pages + 1):
        try:
            page = parse_favorite_list_page(fetch_page(page_number))
        except BossRiskStop as exc:
            return FavoritePageCollection(
                mode=stable_mode,
                status="risk_stopped",
                complete=False,
                pages_read=pages_read,
                first_page_ids=first_page_ids,
                observed_candidate_ids=tuple(observed),
                error=str(exc),
            )
        except Exception as exc:
            return FavoritePageCollection(
                mode=stable_mode,
                status="failed",
                complete=False,
                pages_read=pages_read,
                first_page_ids=first_page_ids,
                observed_candidate_ids=tuple(observed),
                error=str(exc),
            )

        signature = page.candidate_ids
        if page.has_more and (not signature or signature in page_signatures):
            reason = "non-progressing empty page" if not signature else "duplicate page"
            return FavoritePageCollection(
                mode=stable_mode,
                status="failed",
                complete=False,
                pages_read=pages_read,
                first_page_ids=first_page_ids,
                observed_candidate_ids=tuple(observed),
                error=reason,
            )
        page_signatures.add(signature)
        pages_read += 1
        if page_number == 1:
            first_page_ids = signature
        observed.extend(signature)

        if stable_mode == "incremental" and _contains_ordered_anchor(observed, stable_anchor):
            return FavoritePageCollection(
                mode=stable_mode,
                status="anchor_reached",
                complete=True,
                pages_read=pages_read,
                first_page_ids=first_page_ids,
                observed_candidate_ids=tuple(observed),
            )
        if not page.has_more:
            return FavoritePageCollection(
                mode=stable_mode,
                status="end_reached",
                complete=True,
                pages_read=pages_read,
                first_page_ids=first_page_ids,
                observed_candidate_ids=tuple(observed),
            )

    return FavoritePageCollection(
        mode=stable_mode,
        status="sync_incomplete",
        complete=False,
        pages_read=pages_read,
        first_page_ids=first_page_ids,
        observed_candidate_ids=tuple(observed),
        error="favorite list still has more pages at the configured cap",
    )


def persist_favorite_sync_result(
    *,
    registry: FavoriteRegistry,
    result: FavoritePageCollection,
    receipt_id: str,
    observed_at: str | None = None,
) -> FavoriteSyncPersistence:
    stable_receipt_id = str(receipt_id or "").strip()
    if not stable_receipt_id:
        raise ValueError("receipt_id cannot be empty")
    new_candidate_count = registry.record_candidates(
        result.observed_candidate_ids,
        source="list_sync",
        receipt_id=stable_receipt_id,
        observed_at=observed_at,
    )
    checkpoint_advanced = False
    if result.complete:
        registry.save_complete_checkpoint(
            anchor_group=result.first_page_ids,
            receipt_id=stable_receipt_id,
            sync_status=result.status,
            completed_at=observed_at,
        )
        checkpoint_advanced = True
    return FavoriteSyncPersistence(
        receipt_id=stable_receipt_id,
        observed_count=len(result.observed_candidate_ids),
        new_candidate_count=new_candidate_count,
        checkpoint_advanced=checkpoint_advanced,
    )
