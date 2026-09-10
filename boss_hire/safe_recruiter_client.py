from __future__ import annotations

import hashlib
import json
import re
import secrets
import time
from collections.abc import Iterator, Mapping
from contextlib import contextmanager
from importlib.metadata import version
from threading import Lock
from typing import Any, cast

from boss_agent_cli.api import recruiter_endpoints as ep
from boss_agent_cli.api.httpx_helpers import add_stoken_to_get_params
from boss_agent_cli.api.recruiter_client import BossRecruiterClient

from boss_hire.boss_guard import BossRequestGuard
from boss_hire.favorite_sync_contract import (
    FAVORITE_LIST_MAX_PAGES,
    FAVORITE_LIST_TAG,
    FAVORITE_LIST_URL,
)
from boss_hire.search_filters import city_name_for_code, validate_search_filter_params


REQUIRED_BOSS_AGENT_VERSION = "1.19.1"
RECOMMENDATION_URL = "https://www.zhipin.com/wapi/zpjob/rec/geek/list"
FAVORITE_ADD_URL = "https://www.zhipin.com/wapi/zprelation/userMark/add"
FAVORITE_STATUS_URL = "https://www.zhipin.com/wapi/zpitem/web/boss/search/geek/info"
FAVORITE_MARK_TYPE = 6
FAVORITE_TRACE_CHARS = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
FAVORITE_HEADERS = {
    "Origin": "https://www.zhipin.com",
    "Referer": "https://www.zhipin.com/web/frame/search/",
    "Content-Type": "application/x-www-form-urlencoded",
    "X-Requested-With": "XMLHttpRequest",
}


class BossRequestFailed(RuntimeError):
    def __init__(
        self,
        message: str,
        *,
        outcome: str | None = None,
        transport_attempted: bool = False,
        definite_rejection: bool = False,
        http_status: int | None = None,
        response_code: int | None = None,
    ) -> None:
        super().__init__(message)
        self.outcome = outcome
        self.transport_attempted = transport_attempted
        self.definite_rejection = definite_rejection
        self.http_status = http_status
        self.response_code = response_code


class BossRiskStop(BossRequestFailed):
    pass


READ_ENDPOINTS: dict[str, tuple[str, str]] = {
    ep.BOSS_JOB_LIST_URL: ("list_jobs", "metadata"),
    ep.BOSS_JOB_EDIT_URL: ("job_detail", "metadata"),
    ep.BOSS_SEARCH_GEEK_URL: ("search_geeks", "list"),
    ep.BOSS_VIEW_GEEK_URL: ("view_geek", "detail"),
    RECOMMENDATION_URL: ("recommend_geeks", "list"),
    FAVORITE_LIST_URL: ("favorite_list", "list"),
    FAVORITE_STATUS_URL: ("favorite_status", "detail"),
}
WRITE_ENDPOINTS: dict[str, tuple[str, str]] = {
    FAVORITE_ADD_URL: ("favorite_candidate", "write"),
}
RISK_CODES = {9, 32, 36, 37}
RECENT_VIEW_FILTER_VALUES = {"include_all": 0, "exclude_14d": 1}
SEARCH_REQUIRED_PARAM_FIELDS = {
    "page",
    "keywords",
    "tag",
    "city",
    "gender",
    "experience",
    "salary",
    "age",
    "applyStatus",
    "degree",
    "switchFreq",
    "manageExperience",
    "geekJobRequirements",
    "exchangeResume",
    "viewResume",
    "firstDegree",
    "queryAnd",
    "source",
    "activeness",
    "defaultCondition",
    "hasRcd",
    "filterParams",
}
SEARCH_OPTIONAL_PARAM_FIELDS = {"schoolLevel", "select", "jobId"}


def assert_supported_boss_agent_version() -> None:
    installed = version("boss-agent-cli")
    if installed != REQUIRED_BOSS_AGENT_VERSION:
        raise BossRequestFailed(
            f"boss-agent-cli {installed} is not approved; expected {REQUIRED_BOSS_AGENT_VERSION}"
        )


def _message_digest(value: object) -> str:
    text = "" if value is None else str(value)
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _required_text(value: object, field: str) -> str:
    result = str(value or "").strip()
    if not result:
        raise ValueError(f"{field} 不能为空")
    return result


def _int32(value: int) -> int:
    value &= 0xFFFFFFFF
    return value if value < 0x80000000 else value - 0x100000000


def _js_mul_u32(left: int, right: int) -> int:
    return int(float(left) * float(right)) & 0xFFFFFFFF


def _trace_char(value: int, first: int, second: int, shift: int) -> str:
    value = abs(value)
    mixed = (_js_mul_u32(first, value) ^ (value >> 16)) & 0xFFFFFFFF
    mixed = (_js_mul_u32(second, mixed) ^ (mixed >> shift)) & 0xFFFFFFFF
    return FAVORITE_TRACE_CHARS[mixed % len(FAVORITE_TRACE_CHARS)]


def make_favorite_traceid() -> str:
    """Reproduce the frozen BOSS web common-header trace-id format."""

    seed = f"{int(time.time() * 1000):013x}"[-13:] + "".join(
        secrets.choice(FAVORITE_TRACE_CHARS) for _ in range(6)
    )
    forward = reverse = weighted = 0
    middle = len(seed) // 2
    for index, char in enumerate(seed):
        forward = _int32((forward << 5) - forward + ord(char))
        weighted = _int32(
            (weighted << 3) - weighted + ord(char) * (abs(index - middle) + 1)
        )
    for index in range(len(seed) - 1, -1, -1):
        reverse = _int32((reverse << 7) - reverse + ord(seed[index]) * (index + 1))
    suffix = "".join(
        (
            _trace_char(forward ^ reverse, 2654435761, 2246822507, 13),
            _trace_char(reverse ^ weighted, 3266489909, 2654435761, 13),
            _trace_char(weighted ^ forward, 668265261, 2246822507, 13),
        )
    )
    traceid = f"F-{seed}{suffix}"
    if re.fullmatch(r"F-[0-9A-Za-z]{22}", traceid) is None:
        raise BossRequestFailed("favorite traceid generation failed")
    return traceid


def favorite_common_headers(token: Mapping[str, Any]) -> dict[str, str]:
    cookies = token.get("cookies")
    bst = (
        str(cookies.get("bst") or "").strip()
        if isinstance(cookies, Mapping)
        else ""
    )
    if not bst:
        raise BossRequestFailed(
            "favorite request requires bst for zp_token",
            outcome="missing_favorite_auth_header",
            transport_attempted=False,
        )
    return {
        **FAVORITE_HEADERS,
        "traceid": make_favorite_traceid(),
        "zp_token": bst,
    }


class SafeBossRecruiterClient(BossRecruiterClient):
    """Recruiter client with one transport attempt and fail-closed endpoint routing."""

    def __init__(self, auth_manager: Any, *, guard: BossRequestGuard) -> None:
        assert_supported_boss_agent_version()
        self._guard = guard
        self._request_lock = Lock()
        self._active_operation_key: str | None = None
        self._active_operation_used = False
        super().__init__(auth_manager)

    @contextmanager
    def operation(self, operation_key: str) -> Iterator[None]:
        key = operation_key.strip()
        if not key:
            raise ValueError("operation key cannot be empty")
        if self._active_operation_key is not None:
            raise BossRequestFailed("nested BOSS operation contexts are disabled")
        self._active_operation_key = key
        self._active_operation_used = False
        try:
            yield
        finally:
            self._active_operation_key = None
            self._active_operation_used = False

    def _request(self, method: str, url: str, **kwargs: Any) -> dict[str, Any]:
        stable_method = method.upper()
        endpoint = (
            READ_ENDPOINTS.get(url)
            if stable_method == "GET"
            else WRITE_ENDPOINTS.get(url) if stable_method == "POST" else None
        )
        if endpoint is None:
            raise BossRequestFailed(f"BOSS endpoint is not approved for automated access: {method} {url}")
        if endpoint == ("search_geeks", "list"):
            params = kwargs.get("params")
            if set(kwargs) != {"params"} or not isinstance(params, dict):
                raise BossRequestFailed("search request shape is fixed")
            fields = set(params)
            if not SEARCH_REQUIRED_PARAM_FIELDS <= fields:
                raise BossRequestFailed("search request is missing fixed params")
            if fields - SEARCH_REQUIRED_PARAM_FIELDS - SEARCH_OPTIONAL_PARAM_FIELDS:
                raise BossRequestFailed("search request contains unapproved params")
            if params.get("viewResume") not in RECENT_VIEW_FILTER_VALUES.values():
                raise BossRequestFailed("search viewResume must be 0 or 1")
        if endpoint == ("favorite_candidate", "write"):
            data = kwargs.get("data")
            if set(kwargs) != {"data"} or not isinstance(data, dict):
                raise BossRequestFailed("favorite candidate request shape is fixed")
            if set(data) != {"markType", "encryptMarkId", "securityId"}:
                raise BossRequestFailed("favorite candidate payload fields are fixed")
            if data.get("markType") != FAVORITE_MARK_TYPE:
                raise BossRequestFailed("favorite candidate markType is fixed")
            _required_text(data.get("encryptMarkId"), "encrypt_geek_id")
            _required_text(data.get("securityId"), "security_id")
        if endpoint == ("favorite_list", "list"):
            params = kwargs.get("params")
            if set(kwargs) != {"params"} or not isinstance(params, dict):
                raise BossRequestFailed("favorite list request shape is fixed")
            if set(params) != {"tag", "page"} or params.get("tag") != FAVORITE_LIST_TAG:
                raise BossRequestFailed("favorite list params and tag are fixed")
            page = params.get("page")
            if (
                not isinstance(page, int)
                or isinstance(page, bool)
                or page < 1
                or page > FAVORITE_LIST_MAX_PAGES
            ):
                raise BossRequestFailed("favorite list page must be between 1 and 40")
        if endpoint == ("favorite_status", "detail"):
            params = kwargs.get("params")
            if (
                set(kwargs) != {"params"}
                or not isinstance(params, dict)
                or set(params) != {"securityId"}
            ):
                raise BossRequestFailed("favorite status request shape is fixed")
            _required_text(params.get("securityId"), "security_id")
        return self._dispatch_approved_request(
            stable_method,
            url,
            endpoint=endpoint,
            **kwargs,
        )

    def search_geeks(
        self,
        query: str,
        *,
        city: str | None = None,
        page: int = 1,
        job_id: str | None = None,
        experience: str | None = None,
        degree: str | None = None,
        age: str | None = None,
        school_level: str | None = None,
        activeness: str | None = None,
        gender: str | None = None,
        apply_status: str | None = None,
        switch_frequency: str | None = None,
        geek_job_requirements: str | None = None,
        source: str | None = None,
        select: bool = False,
        salary: str | None = None,
        recent_view_filter: str = "include_all",
    ) -> dict[str, Any]:
        stable_filter = str(recent_view_filter or "").strip()
        if stable_filter not in RECENT_VIEW_FILTER_VALUES:
            raise ValueError("recent_view_filter must be include_all or exclude_14d")
        validate_search_filter_params(
            {
                key: value
                for key, value in {
                    "experience": experience,
                    "degree": degree,
                    "age": age,
                    "school_level": school_level,
                    "city": city,
                    "salary": salary,
                    "activeness": activeness,
                    "gender": gender,
                    "apply_status": apply_status,
                    "switch_frequency": switch_frequency,
                    "geek_job_requirements": geek_job_requirements,
                }.items()
                if value is not None
            }
        )
        city_code = city or "-2"
        params: dict[str, Any] = {
            "page": page,
            "keywords": query or "",
            "tag": "",
            "city": city_code,
            "gender": gender or "-1",
            "experience": experience or "-1,-1",
            "salary": salary or "-1,-1",
            "age": age or "-1,-1",
            "applyStatus": apply_status or "-1",
            "degree": degree or "-1,-1",
            "switchFreq": switch_frequency or 0,
            "manageExperience": 0,
            "geekJobRequirements": geek_job_requirements or 0,
            "exchangeResume": 0,
            "viewResume": RECENT_VIEW_FILTER_VALUES[stable_filter],
            "firstDegree": 0,
            "queryAnd": 0,
            "source": source or 4,
            "activeness": activeness or 0,
            "defaultCondition": 2,
            "hasRcd": 0,
            "filterParams": json.dumps(
                {
                    "sortType": 1,
                    "region": {
                        "cityCode": city_code,
                        "cityName": city_name_for_code(city_code),
                        "areas": [],
                    },
                    "overSeaWorkExperience": 0,
                    "overSeaWorkLanguage": 0,
                    "overSeaWorkWill": 0,
                    "manageExperience": 0,
                },
                separators=(",", ":"),
            ),
        }
        if school_level:
            params["schoolLevel"] = school_level
        if select:
            params["select"] = "true"
        if job_id:
            params["jobId"] = job_id
        return self._request("GET", ep.BOSS_SEARCH_GEEK_URL, params=params)

    def _dispatch_approved_request(
        self,
        method: str,
        url: str,
        *,
        endpoint: tuple[str, str],
        **kwargs: Any,
    ) -> dict[str, Any]:
        if self._active_operation_key is not None:
            if self._active_operation_used:
                raise BossRequestFailed("one operation context may issue only one BOSS request")
            self._active_operation_used = True
        if not self._request_lock.acquire(blocking=False):
            raise BossRequestFailed("concurrent BOSS requests are disabled")
        try:
            return self._request_once(method, url, endpoint=endpoint, **kwargs)
        finally:
            self._request_lock.release()

    def _request_once(
        self,
        method: str,
        url: str,
        *,
        endpoint: tuple[str, str],
        **kwargs: Any,
    ) -> dict[str, Any]:
        operation, request_class = endpoint
        if self._active_operation_key == "metadata:selected-job-detail":
            binding = getattr(self._guard, "operation_bindings", {}).get(self._active_operation_key, {})
            params = kwargs.get("params")
            if (operation != "job_detail" or not binding.get("job_id")
                or params != {"encJobId": binding["job_id"], "lid": "", "encAtsJobId": ""}):
                raise BossRequestFailed("selected job request does not match frozen manifest")
        extra_headers: dict[str, str] = kwargs.pop("extra_headers", {})
        if endpoint in {
            ("favorite_candidate", "write"),
            ("favorite_status", "detail"),
        }:
            try:
                extra_headers = {
                    **favorite_common_headers(self._auth.get_token()),
                    **extra_headers,
                }
            except BossRequestFailed:
                raise
            except Exception as exc:
                raise BossRequestFailed(
                    f"favorite auth state unavailable: {type(exc).__name__}",
                    outcome="missing_favorite_auth_header",
                    transport_attempted=False,
                ) from exc
        attempt_id = self._guard.reserve(
            operation,
            operation_key=self._active_operation_key,
            request_class=request_class,
            method=method,
            endpoint_name=operation,
        )

        try:
            client = self._get_client()
            token = self._auth.get_token()
            add_stoken_to_get_params(method, kwargs, token.get("stoken", ""))
            headers = {**self._headers_for(url), **extra_headers}
            response = client.request(method, url, headers=headers, **kwargs)
            self._merge_cookies(response)
        except Exception as exc:
            self._guard.finish_attempt(
                attempt_id,
                outcome="network_error",
                message_digest=_message_digest(type(exc).__name__),
            )
            raise BossRequestFailed(
                f"single BOSS request failed: {type(exc).__name__}",
                outcome="network_error",
                transport_attempted=True,
            ) from exc

        status_code = int(response.status_code)
        response_text = str(response.text or "")
        if status_code in {403, 429} or "安全验证" in response_text:
            reason = f"http_{status_code}" if status_code in {403, 429} else "security_verification"
            self._guard.finish_attempt(
                attempt_id,
                outcome="risk_stop",
                http_status=status_code,
                message_digest=_message_digest(reason),
            )
            self._guard.open_circuit(reason)
            raise BossRiskStop(
                f"BOSS risk stop: {reason}",
                outcome="risk_stop",
                transport_attempted=True,
                http_status=status_code,
            )
        if status_code >= 400:
            self._guard.finish_attempt(
                attempt_id,
                outcome="http_error",
                http_status=status_code,
                message_digest=_message_digest(response_text[:200]),
            )
            raise BossRequestFailed(
                f"BOSS HTTP request failed with status {status_code}",
                outcome="http_error",
                transport_attempted=True,
                definite_rejection=True,
                http_status=status_code,
            )

        try:
            data = response.json()
        except Exception as exc:
            self._guard.finish_attempt(
                attempt_id,
                outcome="invalid_json",
                http_status=status_code,
                message_digest=_message_digest(type(exc).__name__),
            )
            raise BossRequestFailed(
                "BOSS response was not valid JSON",
                outcome="invalid_json",
                transport_attempted=True,
                http_status=status_code,
            ) from exc
        if not isinstance(data, dict):
            self._guard.finish_attempt(
                attempt_id,
                outcome="invalid_payload",
                http_status=status_code,
                message_digest=_message_digest(type(data).__name__),
            )
            raise BossRequestFailed(
                "BOSS response JSON must be an object",
                outcome="invalid_payload",
                transport_attempted=True,
                http_status=status_code,
            )

        raw_code = data.get("code")
        try:
            code = int(raw_code)
        except (TypeError, ValueError):
            code = -1
        message = data.get("message") or data.get("msg") or ""
        if code in RISK_CODES:
            reason = f"code_{code}"
            self._guard.finish_attempt(
                attempt_id,
                outcome="risk_stop",
                http_status=status_code,
                response_code=code,
                message_digest=_message_digest(message),
            )
            self._guard.open_circuit(reason)
            raise BossRiskStop(
                f"BOSS risk stop: {reason}",
                outcome="risk_stop",
                transport_attempted=True,
                http_status=status_code,
                response_code=code,
            )
        if code != 0:
            self._guard.finish_attempt(
                attempt_id,
                outcome="response_error",
                http_status=status_code,
                response_code=code,
                message_digest=_message_digest(message),
            )
            raise BossRequestFailed(
                f"BOSS response code {code}; run stopped without retry",
                outcome="response_error",
                transport_attempted=True,
                definite_rejection=True,
                http_status=status_code,
                response_code=code,
            )

        self._guard.finish_attempt(
            attempt_id,
            outcome="success",
            http_status=status_code,
            response_code=code,
        )
        if self._ADD_ENDPOINT_HINT:
            data.setdefault("__cli_endpoint_hint__", url)
        return cast(dict[str, Any], data)

    def favorite_candidate(
        self,
        *,
        encrypt_geek_id: str,
        security_id: str,
    ) -> dict[str, Any]:
        stable_geek_id = _required_text(encrypt_geek_id, "encrypt_geek_id")
        stable_security_id = _required_text(security_id, "security_id")
        return self._request(
            "POST",
            FAVORITE_ADD_URL,
            data={
                "markType": FAVORITE_MARK_TYPE,
                "encryptMarkId": stable_geek_id,
                "securityId": stable_security_id,
            },
        )

    def favorite_list(self, *, page: int) -> dict[str, Any]:
        return self._request(
            "GET",
            FAVORITE_LIST_URL,
            params={"tag": FAVORITE_LIST_TAG, "page": page},
        )

    def favorite_status(
        self,
        *,
        encrypt_geek_id: str,
        encrypt_job_id: str,
        security_id: str,
    ) -> dict[str, Any]:
        stable_geek_id = _required_text(encrypt_geek_id, "encrypt_geek_id")
        stable_job_id = _required_text(encrypt_job_id, "encrypt_job_id")
        stable_security_id = _required_text(security_id, "security_id")
        _ = stable_geek_id, stable_job_id
        return self._request(
            "GET",
            FAVORITE_STATUS_URL,
            params={"securityId": stable_security_id},
        )

    def _browser_request(self, method: str, url: str, **_kwargs: Any) -> dict[str, Any]:
        raise BossRequestFailed(f"BOSS browser/write endpoint is disabled: {method} {url}")
