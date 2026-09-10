from __future__ import annotations

import hashlib
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Literal
from zoneinfo import ZoneInfo


SHANGHAI = ZoneInfo("Asia/Shanghai")


class BossLiveAccessDenied(RuntimeError):
    """Raised before client construction when a live run is not authorized."""


@dataclass(frozen=True)
class BossAccessPolicy:
    list_interval_seconds: float
    detail_interval_seconds: float
    list_jitter_seconds: tuple[float, float] = (0.0, 0.0)
    detail_jitter_seconds: tuple[float, float] = (0.0, 0.0)

    @classmethod
    def balanced(cls) -> "BossAccessPolicy":
        return cls(
            list_interval_seconds=6.0,
            detail_interval_seconds=15.0,
            list_jitter_seconds=(1.0, 4.0),
            detail_jitter_seconds=(2.0, 10.0),
        )

    def __post_init__(self) -> None:
        if self.list_interval_seconds <= 0 or self.detail_interval_seconds <= 0:
            raise ValueError("BOSS request intervals must be positive")
        if self.detail_interval_seconds < self.list_interval_seconds:
            raise ValueError("detail interval cannot be shorter than list interval")
        for jitter in (self.list_jitter_seconds, self.detail_jitter_seconds):
            if len(jitter) != 2 or jitter[0] < 0 or jitter[1] < jitter[0]:
                raise ValueError("BOSS request jitter must be a nonnegative ordered range")


@dataclass(frozen=True)
class BossAccessReceipt:
    mode: Literal["frozen", "live"]
    network_allowed: bool
    account_key: str
    confirmed_local_date: str | None
    operation_count: int

    def summary(self) -> str:
        return (
            f"mode={self.mode} account={self.account_key} "
            f"operations={self.operation_count} manifest_bound=true"
        )


def _shanghai_now(now: datetime | None) -> datetime:
    value = now or datetime.now(tz=SHANGHAI)
    if value.tzinfo is None:
        return value.replace(tzinfo=SHANGHAI)
    return value.astimezone(SHANGHAI)


def account_key_for(auth_dir: Path) -> str:
    normalized = str(auth_dir.expanduser().resolve(strict=False))
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def preflight_boss_access(
    *,
    live: bool,
    confirm_live: str | None,
    auth_dir: Path,
    operation_count: int,
    now: datetime | None = None,
    policy: BossAccessPolicy | None = None,
) -> BossAccessReceipt:
    """Fail closed before any BOSS dependency or network client is constructed."""

    selected_policy = policy or BossAccessPolicy.balanced()
    account_key = account_key_for(auth_dir)
    if not live:
        return BossAccessReceipt(
            mode="frozen",
            network_allowed=False,
            account_key=account_key,
            confirmed_local_date=None,
            operation_count=0,
        )

    local_date = _shanghai_now(now).date().isoformat()
    if confirm_live != local_date:
        raise BossLiveAccessDenied(
            "BOSS live access is disabled. "
            f"Re-run with --live --confirm-live {local_date} after reviewing the operation manifest."
        )
    if operation_count <= 0:
        raise BossLiveAccessDenied("BOSS live access requires a non-empty operation manifest")

    return BossAccessReceipt(
        mode="live",
        network_allowed=True,
        account_key=account_key,
        confirmed_local_date=local_date,
        operation_count=operation_count,
    )
