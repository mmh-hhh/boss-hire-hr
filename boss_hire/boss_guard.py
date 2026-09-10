from __future__ import annotations

import fcntl
import os
import random
import sqlite3
import time
from collections.abc import Callable
from datetime import datetime
from pathlib import Path
from typing import Any, Mapping, Sequence

from boss_hire.boss_access import BossAccessPolicy, SHANGHAI


class BossGuardError(RuntimeError):
    pass


class BossGuardBusy(BossGuardError):
    pass


class BossCircuitOpen(BossGuardError):
    pass


SCHEMA = """
CREATE TABLE IF NOT EXISTS live_runs (
    run_id TEXT NOT NULL,
    account_key TEXT NOT NULL,
    local_date TEXT NOT NULL,
    started_at TEXT NOT NULL,
    ended_at TEXT,
    status TEXT NOT NULL,
    planned_budget INTEGER NOT NULL,
    operation_count INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (account_key, run_id)
);
CREATE INDEX IF NOT EXISTS live_runs_account_date
    ON live_runs(account_key, local_date);

CREATE TABLE IF NOT EXISTS request_attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id TEXT NOT NULL,
    account_key TEXT NOT NULL,
    local_date TEXT NOT NULL,
    attempted_at REAL NOT NULL,
    operation TEXT NOT NULL,
    request_class TEXT NOT NULL,
    method TEXT NOT NULL,
    endpoint_name TEXT NOT NULL,
    outcome TEXT NOT NULL,
    http_status INTEGER,
    response_code INTEGER,
    message_digest TEXT
);
CREATE INDEX IF NOT EXISTS request_attempts_account_date
    ON request_attempts(account_key, local_date, attempted_at);
CREATE INDEX IF NOT EXISTS request_attempts_run
    ON request_attempts(account_key, run_id);

CREATE TABLE IF NOT EXISTS circuit_breaker (
    account_key TEXT PRIMARY KEY,
    state TEXT NOT NULL,
    reason TEXT,
    opened_at TEXT,
    opened_run_id TEXT,
    cleared_at TEXT,
    clear_note TEXT
);

CREATE TABLE IF NOT EXISTS live_authorizations (
    authorization_id TEXT PRIMARY KEY,
    account_key TEXT NOT NULL,
    session_fingerprint TEXT NOT NULL,
    local_date TEXT NOT NULL,
    plan_id TEXT NOT NULL,
    plan_digest TEXT NOT NULL,
    planned_budget INTEGER NOT NULL,
    status TEXT NOT NULL,
    created_at TEXT NOT NULL,
    consumed_at TEXT,
    note_digest TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS live_authorizations_account_date
    ON live_authorizations(account_key, local_date, status);
CREATE UNIQUE INDEX IF NOT EXISTS live_authorizations_one_plan_per_session_day
    ON live_authorizations(account_key, session_fingerprint, local_date, plan_digest);
"""


def _private_root(root: Path) -> None:
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    root.chmod(0o700)


def _connect(root: Path) -> sqlite3.Connection:
    _private_root(root)
    database = root / "guard.sqlite3"
    connection = sqlite3.connect(database, timeout=5)
    database.chmod(0o600)
    connection.executescript(SCHEMA)
    live_run_columns = {
        str(row[1]) for row in connection.execute("PRAGMA table_info(live_runs)").fetchall()
    }
    if "operation_count" not in live_run_columns:
        connection.execute(
            "ALTER TABLE live_runs ADD COLUMN operation_count INTEGER NOT NULL DEFAULT 0"
        )
    connection.commit()
    return connection


def _normalize_operation_manifest(
    value: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[str, str, str]]:
    if not value:
        raise ValueError("operation manifest cannot be empty")
    result: dict[str, tuple[str, str, str]] = {}
    for index, item in enumerate(value):
        operation_key = str(item.get("operation_key") or "").strip()
        request_class = str(item.get("request_class") or "").strip()
        method = str(item.get("method") or "").strip().upper()
        endpoint_name = str(item.get("endpoint_name") or "").strip()
        if not operation_key or not request_class or not method or not endpoint_name:
            raise ValueError(f"operation manifest item {index} is incomplete")
        if request_class not in {"metadata", "list", "detail", "write"}:
            raise ValueError(f"operation manifest item {index} has invalid request_class")
        if request_class == "write":
            if method != "POST" or endpoint_name != "favorite_candidate":
                raise ValueError(
                    f"operation manifest item {index} is not an allowed favorite write"
                )
        elif method != "GET":
            raise ValueError(f"operation manifest item {index} must use GET")
        if operation_key in result:
            raise ValueError(f"duplicate operation key: {operation_key}")
        result[operation_key] = (request_class, method, endpoint_name)
    has_favorite_list = any(item[2] == "favorite_list" for item in result.values())
    has_write = any(item[0] == "write" for item in result.values())
    if has_favorite_list and has_write:
        raise ValueError("favorite list operations cannot be mixed with write operations")
    return result


class BossRequestGuard:
    def __init__(
        self,
        *,
        root: Path,
        account_key: str,
        run_id: str,
        operation_manifest: Sequence[Mapping[str, Any]],
        policy: BossAccessPolicy | None = None,
        now: Callable[[], datetime] | None = None,
        sleep: Callable[[float], None] | None = None,
        random_uniform: Callable[[float, float], float] | None = None,
    ) -> None:
        self.root = root
        self.account_key = account_key
        self.run_id = run_id
        self.operation_manifest = _normalize_operation_manifest(operation_manifest)
        self.operation_bindings = {str(item["operation_key"]): dict(item.get("binding") or {}) for item in operation_manifest}
        self.policy = policy or BossAccessPolicy.balanced()
        self._now = now or (lambda: datetime.now(tz=SHANGHAI))
        self._sleep = sleep or time.sleep
        self._random_uniform = random_uniform or random.SystemRandom().uniform
        self._lock_fd: int | None = None
        self._connection: sqlite3.Connection | None = None
        self._acquired = False

    def _local_now(self) -> datetime:
        value = self._now()
        if value.tzinfo is None:
            return value.replace(tzinfo=SHANGHAI)
        return value.astimezone(SHANGHAI)

    def acquire(self) -> "BossRequestGuard":
        if self._acquired:
            return self

        _private_root(self.root)
        lock_path = self.root / f"{self.account_key}.lock"
        lock_fd = os.open(lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        os.chmod(lock_path, 0o600)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as exc:
            os.close(lock_fd)
            raise BossGuardBusy(f"another BOSS live process holds account {self.account_key}") from exc

        self._lock_fd = lock_fd
        try:
            self._connection = _connect(self.root)
            now = self._local_now()
            circuit = self._connection.execute(
                "SELECT state, reason FROM circuit_breaker WHERE account_key = ?",
                (self.account_key,),
            ).fetchone()
            if circuit and circuit[0] == "OPEN":
                raise BossCircuitOpen(f"BOSS circuit is open: {circuit[1] or 'manual review required'}")

            local_date = now.date().isoformat()
            self._connection.execute(
                """
                INSERT INTO live_runs(
                    run_id, account_key, local_date, started_at, status,
                    planned_budget, operation_count
                ) VALUES (?, ?, ?, ?, 'running', ?, ?)
                """,
                (
                    self.run_id,
                    self.account_key,
                    local_date,
                    now.isoformat(),
                    len(self.operation_manifest),
                    len(self.operation_manifest),
                ),
            )
            self._connection.commit()
            self._acquired = True
            return self
        except Exception:
            self._release_resources()
            raise

    def reserve(
        self,
        operation: str,
        *,
        operation_key: str | None = None,
        request_class: str,
        method: str,
        endpoint_name: str,
    ) -> int:
        connection = self._require_connection()
        if request_class not in {"metadata", "list", "detail", "write"}:
            raise BossGuardError(f"unknown BOSS request class: {request_class}")
        recorded_operation = operation
        key = str(operation_key or "").strip()
        expected = self.operation_manifest.get(key)
        if expected is None:
            raise BossGuardError(f"operation is not authorized by manifest: {key or '<missing>'}")
        actual = (request_class, method.upper(), endpoint_name)
        if actual != expected or operation != endpoint_name:
            raise BossGuardError(
                f"operation {key} does not match manifest: expected={expected!r} actual={actual!r}"
            )
        duplicate = connection.execute(
            """
            SELECT 1 FROM request_attempts
            WHERE account_key = ? AND run_id = ? AND operation = ?
            LIMIT 1
            """,
            (self.account_key, self.run_id, key),
        ).fetchone()
        if duplicate is not None:
            raise BossGuardError(f"operation was already reserved: {key}")
        recorded_operation = key
        interval_seconds = self._request_interval(request_class)
        while True:
            now = self._local_now()
            local_date = now.date().isoformat()
            connection.execute("BEGIN IMMEDIATE")
            try:
                circuit = connection.execute(
                    "SELECT state, reason FROM circuit_breaker WHERE account_key = ?",
                    (self.account_key,),
                ).fetchone()
                if circuit and circuit[0] == "OPEN":
                    raise BossCircuitOpen(f"BOSS circuit is open: {circuit[1] or 'manual review required'}")

                wait_seconds = self._required_wait(connection, now.timestamp(), interval_seconds)
                if wait_seconds > 0:
                    connection.rollback()
                    self._sleep(wait_seconds)
                    continue

                cursor = connection.execute(
                    """
                    INSERT INTO request_attempts(
                        run_id, account_key, local_date, attempted_at, operation,
                        request_class, method, endpoint_name, outcome
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'reserved')
                    """,
                    (
                        self.run_id,
                        self.account_key,
                        local_date,
                        now.timestamp(),
                        recorded_operation,
                        request_class,
                        method.upper(),
                        endpoint_name,
                    ),
                )
                connection.commit()
                return int(cursor.lastrowid)
            except Exception:
                connection.rollback()
                raise

    def _request_interval(self, request_class: str) -> float:
        if request_class in {"detail", "write"}:
            minimum = self.policy.detail_interval_seconds
            jitter = self.policy.detail_jitter_seconds
        else:
            minimum = self.policy.list_interval_seconds
            jitter = self.policy.list_jitter_seconds
        extra = jitter[0] if jitter[0] == jitter[1] else self._random_uniform(*jitter)
        return minimum + extra

    def _required_wait(
        self,
        connection: sqlite3.Connection,
        now_timestamp: float,
        interval_seconds: float,
    ) -> float:
        last_row = connection.execute(
            """
            SELECT attempted_at FROM request_attempts
            WHERE account_key = ? ORDER BY attempted_at DESC LIMIT 1
            """,
            (self.account_key,),
        ).fetchone()
        interval_wait = 0.0
        if last_row is not None:
            interval_wait = max(0.0, float(last_row[0]) + interval_seconds - now_timestamp)

        return interval_wait

    def finish_attempt(
        self,
        attempt_id: int,
        *,
        outcome: str,
        http_status: int | None = None,
        response_code: int | None = None,
        message_digest: str | None = None,
    ) -> None:
        connection = self._require_connection()
        connection.execute(
            """
            UPDATE request_attempts
            SET outcome = ?, http_status = ?, response_code = ?, message_digest = ?
            WHERE id = ? AND account_key = ? AND run_id = ?
            """,
            (outcome, http_status, response_code, message_digest, attempt_id, self.account_key, self.run_id),
        )
        connection.commit()

    def request_count_for_run(self) -> int:
        connection = self._require_connection()
        return int(
            connection.execute(
                "SELECT COUNT(*) FROM request_attempts WHERE account_key = ? AND run_id = ?",
                (self.account_key, self.run_id),
            ).fetchone()[0]
        )

    def open_circuit(self, reason: str) -> None:
        connection = self._require_connection()
        now = self._local_now().isoformat()
        connection.execute(
            """
            INSERT INTO circuit_breaker(account_key, state, reason, opened_at, opened_run_id)
            VALUES (?, 'OPEN', ?, ?, ?)
            ON CONFLICT(account_key) DO UPDATE SET
                state = 'OPEN', reason = excluded.reason, opened_at = excluded.opened_at,
                opened_run_id = excluded.opened_run_id, cleared_at = NULL, clear_note = NULL
            """,
            (self.account_key, reason, now, self.run_id),
        )
        connection.commit()

    @classmethod
    def clear_circuit(
        cls,
        *,
        root: Path,
        account_key: str,
        note: str,
        now: Callable[[], datetime] | None = None,
    ) -> None:
        if not note.strip():
            raise ValueError("manual circuit clear requires a review note")
        clock = now or (lambda: datetime.now(tz=SHANGHAI))
        value = clock()
        if value.tzinfo is None:
            value = value.replace(tzinfo=SHANGHAI)
        else:
            value = value.astimezone(SHANGHAI)
        connection = _connect(root)
        try:
            connection.execute(
                """
                INSERT INTO circuit_breaker(account_key, state, cleared_at, clear_note)
                VALUES (?, 'CLOSED', ?, ?)
                ON CONFLICT(account_key) DO UPDATE SET
                    state = 'CLOSED', cleared_at = excluded.cleared_at,
                    clear_note = excluded.clear_note
                """,
                (account_key, value.isoformat(), note.strip()),
            )
            connection.commit()
        finally:
            connection.close()

    def _require_connection(self) -> sqlite3.Connection:
        if not self._acquired or self._connection is None:
            raise BossGuardError("BOSS request guard is not acquired")
        return self._connection

    def close(self, *, status: str = "completed") -> None:
        if self._connection is not None and self._acquired:
            self._connection.execute(
                "UPDATE live_runs SET ended_at = ?, status = ? WHERE account_key = ? AND run_id = ?",
                (self._local_now().isoformat(), status, self.account_key, self.run_id),
            )
            self._connection.commit()
        self._release_resources()

    def _release_resources(self) -> None:
        if self._connection is not None:
            self._connection.close()
            self._connection = None
        if self._lock_fd is not None:
            try:
                fcntl.flock(self._lock_fd, fcntl.LOCK_UN)
            finally:
                os.close(self._lock_fd)
                self._lock_fd = None
        self._acquired = False

    def __enter__(self) -> "BossRequestGuard":
        return self.acquire()

    def __exit__(self, exc_type: type[BaseException] | None, exc: BaseException | None, traceback: Any) -> bool:
        self.close(status="failed" if exc is not None else "completed")
        return False
