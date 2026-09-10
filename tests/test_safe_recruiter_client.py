from __future__ import annotations

import json
import unittest
from pathlib import Path
from threading import Event, Thread
from typing import Any
from unittest.mock import patch

from boss_hire.boss_guard import BossCircuitOpen
from boss_hire.safe_recruiter_client import (
    FAVORITE_ADD_URL,
    FAVORITE_LIST_TAG,
    FAVORITE_LIST_URL,
    FAVORITE_MARK_TYPE,
    FAVORITE_STATUS_URL,
    BossRequestFailed,
    BossRiskStop,
    SafeBossRecruiterClient,
)


class FakeAuth:
    def __init__(self) -> None:
        self.force_refresh_calls = 0

    def get_token(self) -> dict[str, Any]:
        return {
            "stoken": "test-stoken",
            "cookies": {"bst": "test-bst"},
            "user_agent": "test",
        }

    def force_refresh(self, **_kwargs: Any) -> None:
        self.force_refresh_calls += 1


class FakeResponse:
    def __init__(self, *, status_code: int = 200, payload: Any = None, text: str = "") -> None:
        self.status_code = status_code
        self._payload = payload
        self.text = text

    def json(self) -> Any:
        if isinstance(self._payload, Exception):
            raise self._payload
        return self._payload


class FakeHttpClient:
    def __init__(self, responses: list[FakeResponse]) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, dict[str, Any]]] = []

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        return self.responses.pop(0)

    def close(self) -> None:
        pass


class BlockingFirstHttpClient(FakeHttpClient):
    def __init__(self, responses: list[FakeResponse]) -> None:
        super().__init__(responses)
        self.started = Event()
        self.release = Event()

    def request(self, method: str, url: str, **kwargs: Any) -> FakeResponse:
        self.calls.append((method, url, kwargs))
        if len(self.calls) == 1:
            self.started.set()
            if not self.release.wait(timeout=2):
                raise TimeoutError("test did not release first BOSS request")
        return self.responses.pop(0)


class FakeGuard:
    def __init__(self, allowed_operations: set[str] | None = None) -> None:
        self.open_reason: str | None = None
        self.attempts: list[dict[str, Any]] = []
        self.allowed_operations = allowed_operations
        self.reserved_operation_keys: set[str] = set()

    def reserve(self, operation: str, **kwargs: Any) -> int:
        if self.open_reason:
            raise BossCircuitOpen(f"BOSS circuit is open: {self.open_reason}")
        operation_key = kwargs.get("operation_key")
        if self.allowed_operations is not None:
            if operation_key not in self.allowed_operations:
                raise BossRequestFailed("operation is not authorized")
            if operation_key in self.reserved_operation_keys:
                raise BossRequestFailed("operation was already reserved")
            self.reserved_operation_keys.add(operation_key)
        self.attempts.append({"operation": operation, **kwargs, "outcome": "reserved"})
        return len(self.attempts)

    def finish_attempt(self, attempt_id: int, **kwargs: Any) -> None:
        self.attempts[attempt_id - 1].update(kwargs)

    def open_circuit(self, reason: str) -> None:
        self.open_reason = reason


class SafeRecruiterClientTests(unittest.TestCase):
    def make_client(
        self, responses: list[FakeResponse]
    ) -> tuple[SafeBossRecruiterClient, FakeHttpClient, FakeAuth]:
        guard = FakeGuard()
        auth = FakeAuth()
        client = SafeBossRecruiterClient(auth, guard=guard)  # type: ignore[arg-type]
        fake_http = FakeHttpClient(responses)
        client._client = fake_http  # type: ignore[assignment]
        client._merge_cookies = lambda _response: None  # type: ignore[method-assign]
        self.addCleanup(client.close)
        return client, fake_http, auth

    def test_successful_search_is_one_transport_attempt(self) -> None:
        client, http, auth = self.make_client([FakeResponse(payload={"code": 0, "zpData": {"geeks": []}})])

        result = client.search_geeks("汽配", page=1, job_id="job-1")

        self.assertEqual(result["code"], 0)
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(auth.force_refresh_calls, 0)

    def test_recent_view_filter_changes_only_view_resume(self) -> None:
        client, http, _auth = self.make_client(
            [
                FakeResponse(payload={"code": 0, "zpData": {"geeks": []}}),
                FakeResponse(payload={"code": 0, "zpData": {"geeks": []}}),
            ]
        )

        client.search_geeks("汽配", page=1, job_id="job-1")
        client.search_geeks(
            "汽配",
            page=1,
            job_id="job-1",
            recent_view_filter="exclude_14d",
        )

        default_params = http.calls[0][2]["params"]
        filtered_params = http.calls[1][2]["params"]
        self.assertEqual(default_params["viewResume"], 0)
        self.assertEqual(filtered_params["viewResume"], 1)
        self.assertEqual(
            {key: value for key, value in default_params.items() if key != "viewResume"},
            {key: value for key, value in filtered_params.items() if key != "viewResume"},
        )
        self.assertEqual(default_params["hasRcd"], 0)
        self.assertEqual(
            json.loads(default_params["filterParams"]),
            json.loads(filtered_params["filterParams"]),
        )

    def test_supported_search_filters_map_to_existing_request_fields(self) -> None:
        client, http, _auth = self.make_client(
            [FakeResponse(payload={"code": 0, "zpData": {"geeks": []}})]
        )

        client.search_geeks(
            "汽配",
            job_id="job-1",
            degree="203,201",
            school_level="1104",
            city="101020100",
            salary="20,30",
            activeness="4",
            gender="0",
            apply_status="701,703",
            switch_frequency="1",
            geek_job_requirements="1,2",
        )

        params = http.calls[0][2]["params"]
        self.assertEqual(params["degree"], "203,201")
        self.assertEqual(params["schoolLevel"], "1104")
        self.assertEqual(params["city"], "101020100")
        self.assertEqual(params["salary"], "20,30")
        self.assertEqual(params["activeness"], "4")
        self.assertEqual(params["gender"], "0")
        self.assertEqual(params["applyStatus"], "701,703")
        self.assertEqual(params["switchFreq"], "1")
        self.assertEqual(params["geekJobRequirements"], "1,2")
        self.assertEqual(json.loads(params["filterParams"])["region"], {
            "cityCode": "101020100",
            "cityName": "上海",
            "areas": [],
        })

        with self.assertRaisesRegex(ValueError, "参数值无效"):
            client.search_geeks("汽配", degree="999,999")
        self.assertEqual(len(http.calls), 1)

    def test_invalid_recent_view_filter_stops_before_transport(self) -> None:
        client, http, _auth = self.make_client([])

        with self.assertRaisesRegex(ValueError, "recent_view_filter"):
            client.search_geeks("汽配", recent_view_filter="exclude_30d")

        self.assertEqual(http.calls, [])

    def test_exact_operation_key_is_required_and_consumed_before_transport(self) -> None:
        guard = FakeGuard({"source:search:title_match:page:1"})
        auth = FakeAuth()
        client = SafeBossRecruiterClient(auth, guard=guard)  # type: ignore[arg-type]
        http = FakeHttpClient(
            [FakeResponse(payload={"code": 0, "zpData": {"geeks": []}})]
        )
        client._client = http  # type: ignore[assignment]
        client._merge_cookies = lambda _response: None  # type: ignore[method-assign]
        self.addCleanup(client.close)

        with client.operation("source:search:title_match:page:1"):
            client.search_geeks("平台招商负责人", page=1, job_id="job-1")
        self.assertEqual(len(http.calls), 1)
        self.assertEqual(
            guard.attempts[0]["operation_key"],
            "source:search:title_match:page:1",
        )

        with self.assertRaisesRegex(BossRequestFailed, "already reserved"):
            with client.operation("source:search:title_match:page:1"):
                client.search_geeks("平台招商负责人", page=1, job_id="job-1")
        with self.assertRaisesRegex(BossRequestFailed, "not authorized"):
            with client.operation("source:search:unplanned:page:1"):
                client.search_geeks("其他词", page=1, job_id="job-1")
        self.assertEqual(len(http.calls), 1)

    def test_overlapping_request_is_rejected_before_second_transport(self) -> None:
        client, _http, _auth = self.make_client([])
        blocking = BlockingFirstHttpClient(
            [
                FakeResponse(payload={"code": 0, "zpData": {"geeks": []}}),
                FakeResponse(payload={"code": 0, "zpData": {"geeks": []}}),
            ]
        )
        client._client = blocking  # type: ignore[assignment]
        first_result: list[dict[str, Any]] = []
        first_error: list[BaseException] = []

        def run_first() -> None:
            try:
                first_result.append(client.search_geeks("汽配", page=1, job_id="job-1"))
            except BaseException as exc:  # noqa: BLE001 - thread result is asserted below
                first_error.append(exc)

        thread = Thread(target=run_first)
        thread.start()
        self.assertTrue(blocking.started.wait(timeout=1))
        try:
            with self.assertRaisesRegex(BossRequestFailed, "concurrent"):
                client.search_geeks("汽配", page=1, job_id="job-1")
            self.assertEqual(len(blocking.calls), 1)
        finally:
            blocking.release.set()
            thread.join(timeout=2)

        self.assertFalse(thread.is_alive())
        self.assertEqual(first_error, [])
        self.assertEqual(first_result[0]["code"], 0)

        result = client.search_geeks("汽配", page=1, job_id="job-1")
        self.assertEqual(result["code"], 0)
        self.assertEqual(len(blocking.calls), 2)

    def test_each_risk_signal_stops_after_one_transport_attempt(self) -> None:
        cases = {
            "http_403": FakeResponse(status_code=403, payload={"code": 0}, text="Forbidden"),
            "http_429": FakeResponse(status_code=429, payload={"code": 0}, text="Too Many Requests"),
            "security_page": FakeResponse(payload={"code": 0}, text="请完成安全验证"),
            "code_32": FakeResponse(payload={"code": 32, "message": "暂时被禁止使用"}),
            "code_36": FakeResponse(payload={"code": 36, "message": "账户存在异常行为"}),
            "code_9": FakeResponse(payload={"code": 9, "message": "请求过于频繁"}),
            "code_37": FakeResponse(payload={"code": 37, "message": "环境异常"}),
        }
        for name, response in cases.items():
            with self.subTest(name=name):
                client, http, auth = self.make_client([response])
                with self.assertRaises(BossRiskStop):
                    client.search_geeks("汽配", page=1, job_id="job-1")
                self.assertEqual(len(http.calls), 1)
                self.assertEqual(auth.force_refresh_calls, 0)
                with self.assertRaises(BossCircuitOpen):
                    client.search_geeks("汽配", page=2, job_id="job-1")
                self.assertEqual(len(http.calls), 1)

    def test_unknown_nonzero_response_stops_run_without_retry(self) -> None:
        client, http, _auth = self.make_client([FakeResponse(payload={"code": 121, "message": "invalid"})])

        with self.assertRaisesRegex(BossRequestFailed, "121"):
            client.search_geeks("汽配", page=1, job_id="job-1")

        self.assertEqual(len(http.calls), 1)

    def test_unapproved_post_endpoint_is_denied_before_transport(self) -> None:
        client, http, _auth = self.make_client([FakeResponse(payload={"code": 0})])

        with self.assertRaisesRegex(BossRequestFailed, "not approved"):
            client.friend_list(page=1)

        self.assertEqual(http.calls, [])

        with self.assertRaisesRegex(BossRequestFailed, "disabled"):
            client.job_offline("job-1")
        self.assertEqual(http.calls, [])

    def test_favorite_candidate_uses_one_fixed_post_and_payload(self) -> None:
        guard = FakeGuard({"favorite:batch-a:candidate-a:write"})
        auth = FakeAuth()
        client = SafeBossRecruiterClient(auth, guard=guard)  # type: ignore[arg-type]
        http = FakeHttpClient([FakeResponse(payload={"code": 0, "zpData": {"mark": True}})])
        client._client = http  # type: ignore[assignment]
        client._merge_cookies = lambda _response: None  # type: ignore[method-assign]
        self.addCleanup(client.close)

        with client.operation("favorite:batch-a:candidate-a:write"):
            result = client.favorite_candidate(
                encrypt_geek_id="geek-a",
                security_id="security-a",
            )

        self.assertEqual(result["code"], 0)
        self.assertEqual(len(http.calls), 1)
        method, url, kwargs = http.calls[0]
        self.assertEqual((method, url), ("POST", FAVORITE_ADD_URL))
        self.assertEqual(
            kwargs["data"],
            {
                "markType": FAVORITE_MARK_TYPE,
                "encryptMarkId": "geek-a",
                "securityId": "security-a",
            },
        )
        fixture = json.loads(
            (
                Path(__file__).parent
                / "fixtures/boss_favorite_write/request_contract.json"
            ).read_text(encoding="utf-8")
        )
        self.assertEqual(url, fixture["endpoint"])
        self.assertEqual(sorted(kwargs["data"]), fixture["form_keys"])
        self.assertTrue(
            set(fixture["required_header_keys"]).issubset(kwargs["headers"])
        )
        self.assertRegex(kwargs["headers"]["traceid"], r"^F-[0-9A-Za-z]{22}$")
        self.assertEqual(kwargs["headers"]["zp_token"], "test-bst")
        self.assertEqual(guard.attempts[0]["request_class"], "write")
        self.assertEqual(guard.attempts[0]["endpoint_name"], "favorite_candidate")

    def test_favorite_candidate_requires_bst_before_transport(self) -> None:
        client, http, auth = self.make_client([FakeResponse(payload={"code": 0})])
        auth.get_token = lambda: {"cookies": {}, "stoken": "", "user_agent": "test"}  # type: ignore[method-assign]

        with self.assertRaisesRegex(BossRequestFailed, "bst") as captured:
            client.favorite_candidate(
                encrypt_geek_id="geek-a",
                security_id="security-a",
            )

        self.assertFalse(captured.exception.transport_attempted)
        self.assertEqual(http.calls, [])

    def test_favorite_list_uses_fixed_recruiter_endpoint_tag_and_page(self) -> None:
        guard = FakeGuard({"favorite-sync:plan-a:page:2"})
        auth = FakeAuth()
        client = SafeBossRecruiterClient(auth, guard=guard)  # type: ignore[arg-type]
        http = FakeHttpClient(
            [FakeResponse(payload={"code": 0, "zpData": {"cardList": [], "hasMore": False}})]
        )
        client._client = http  # type: ignore[assignment]
        client._merge_cookies = lambda _response: None  # type: ignore[method-assign]
        self.addCleanup(client.close)

        with client.operation("favorite-sync:plan-a:page:2"):
            result = client.favorite_list(page=2)

        self.assertEqual(result["code"], 0)
        self.assertEqual(len(http.calls), 1)
        method, url, kwargs = http.calls[0]
        self.assertEqual((method, url), ("GET", FAVORITE_LIST_URL))
        self.assertEqual(kwargs["params"]["tag"], FAVORITE_LIST_TAG)
        self.assertEqual(kwargs["params"]["page"], 2)
        self.assertEqual(guard.attempts[0]["request_class"], "list")
        self.assertEqual(guard.attempts[0]["endpoint_name"], "favorite_list")

    def test_favorite_list_rejects_page_outside_fixed_forty_page_window(self) -> None:
        client, http, _auth = self.make_client([FakeResponse(payload={"code": 0})])

        for page in (0, -1, 41, True, 1.5, "1"):
            with self.subTest(page=page), self.assertRaisesRegex(BossRequestFailed, "between 1 and 40"):
                client.favorite_list(page=page)  # type: ignore[arg-type]

        self.assertEqual(http.calls, [])

    def test_favorite_list_has_no_url_method_tag_or_limit_controls(self) -> None:
        client, http, _auth = self.make_client([FakeResponse(payload={"code": 0})])

        for extra in (
            {"url": "https://example.invalid"},
            {"method": "POST"},
            {"tag": 5},
            {"limit": 100},
        ):
            with self.subTest(extra=extra), self.assertRaises(TypeError):
                client.favorite_list(page=1, **extra)  # type: ignore[call-arg]

        self.assertEqual(http.calls, [])

    def test_favorite_candidate_rejects_missing_identifiers_before_transport(self) -> None:
        client, http, _auth = self.make_client([FakeResponse(payload={"code": 0})])

        for encrypt_geek_id, security_id in (("", "security-a"), ("geek-a", "")):
            with self.subTest(
                encrypt_geek_id=encrypt_geek_id,
                security_id=security_id,
            ), self.assertRaisesRegex(ValueError, "不能为空"):
                client.favorite_candidate(
                    encrypt_geek_id=encrypt_geek_id,
                    security_id=security_id,
                )

        self.assertEqual(http.calls, [])

    def test_favorite_candidate_has_no_url_or_method_override(self) -> None:
        client, http, _auth = self.make_client([FakeResponse(payload={"code": 0})])

        with self.assertRaises(TypeError):
            client.favorite_candidate(  # type: ignore[call-arg]
                encrypt_geek_id="geek-a",
                security_id="security-a",
                url="https://example.invalid/write",
            )
        with self.assertRaises(TypeError):
            client.favorite_candidate(  # type: ignore[call-arg]
                encrypt_geek_id="geek-a",
                security_id="security-a",
                method="GET",
            )

        self.assertEqual(http.calls, [])

    def test_favorite_candidate_transport_failure_is_not_retried(self) -> None:
        client, http, auth = self.make_client([FakeResponse(payload=TimeoutError("timeout"))])
        http.request = lambda method, url, **kwargs: (  # type: ignore[method-assign]
            http.calls.append((method, url, kwargs)),
            (_ for _ in ()).throw(TimeoutError("timeout")),
        )[1]

        with self.assertRaisesRegex(BossRequestFailed, "TimeoutError"):
            client.favorite_candidate(
                encrypt_geek_id="geek-a",
                security_id="security-a",
            )

        self.assertEqual(len(http.calls), 1)
        self.assertEqual(auth.force_refresh_calls, 0)

    def test_favorite_status_uses_one_exact_candidate_detail_read(self) -> None:
        guard = FakeGuard({"favorite:batch-a:candidate-a:verify"})
        auth = FakeAuth()
        client = SafeBossRecruiterClient(auth, guard=guard)  # type: ignore[arg-type]
        http = FakeHttpClient(
            [FakeResponse(payload={"code": 0, "zpData": {"alreadyInterested": 1}})]
        )
        client._client = http  # type: ignore[assignment]
        client._merge_cookies = lambda _response: None  # type: ignore[method-assign]
        self.addCleanup(client.close)

        with client.operation("favorite:batch-a:candidate-a:verify"):
            result = client.favorite_status(
                encrypt_geek_id="geek-a",
                encrypt_job_id="job-open",
                security_id="security-a",
            )

        self.assertEqual(result["zpData"]["alreadyInterested"], 1)
        self.assertEqual(len(http.calls), 1)
        method, url, kwargs = http.calls[0]
        self.assertEqual(method, "GET")
        self.assertEqual(url, FAVORITE_STATUS_URL)
        self.assertEqual(
            kwargs["params"],
            {"securityId": "security-a", "__zp_stoken__": "test-stoken"},
        )
        self.assertEqual(kwargs["headers"]["zp_token"], "test-bst")
        self.assertRegex(kwargs["headers"]["traceid"], r"^F-[0-9A-Za-z]{22}$")
        self.assertEqual(guard.attempts[0]["endpoint_name"], "favorite_status")

    def test_favorite_status_has_no_url_method_or_page_controls(self) -> None:
        client, http, _auth = self.make_client([FakeResponse(payload={"code": 0})])

        for extra in (
            {"url": "https://example.invalid"},
            {"method": "POST"},
            {"page": 2},
        ):
            with self.subTest(extra=extra), self.assertRaises(TypeError):
                client.favorite_status(  # type: ignore[call-arg]
                    encrypt_geek_id="geek-a",
                    encrypt_job_id="job-open",
                    security_id="security-a",
                    **extra,
                )

        self.assertEqual(http.calls, [])

    def test_unreviewed_dependency_version_fails_closed(self) -> None:
        with patch("boss_hire.safe_recruiter_client.version", return_value="1.20.0"):
            with self.assertRaisesRegex(BossRequestFailed, "expected 1.19.1"):
                SafeBossRecruiterClient(FakeAuth(), guard=FakeGuard())  # type: ignore[arg-type]


if __name__ == "__main__":
    unittest.main()
