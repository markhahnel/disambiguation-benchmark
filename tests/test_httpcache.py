"""CachingClient: what is written to the cache, and what is deliberately not.

Everything here runs against httpx.MockTransport; nothing touches the
network. The status codes and bodies are test scaffolding, not results.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Iterator
from pathlib import Path

import httpx
import pytest
import structlog
from structlog.testing import capture_logs

from disambig import httpcache
from disambig.httpcache import CachingClient, is_cacheable_status

Handler = Callable[[httpx.Request], httpx.Response]


class Recorder:
    """Counts network hits and hands each request to a handler."""

    def __init__(self, handler: Handler) -> None:
        self.handler = handler
        self.requests: list[httpx.Request] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self.handler(request)


def cached_rows(cache_path: Path) -> list[tuple[str, int]]:
    db = sqlite3.connect(cache_path)
    try:
        return list(db.execute("SELECT url, status FROM http_cache ORDER BY url"))
    finally:
        db.close()


@pytest.fixture
def cache_path(tmp_path: Path) -> Path:
    return tmp_path / "http_cache.sqlite"


@pytest.fixture
def make_client(cache_path: Path) -> Iterator[Callable[..., CachingClient]]:
    clients: list[CachingClient] = []

    def factory(recorder: Recorder, max_retries: int = 5) -> CachingClient:
        client = CachingClient(
            cache_path,
            "test-agent",
            min_interval_s=0.0,
            max_retries=max_retries,
            transport=httpx.MockTransport(recorder),
        )
        clients.append(client)
        return client

    yield factory
    for client in clients:
        client.close()


@pytest.fixture
def no_sleep(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    slept: list[float] = []
    monkeypatch.setattr(httpcache.time, "sleep", slept.append)
    return slept


def constant(status: int, body: bytes = b"{}", headers: dict[str, str] | None = None) -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(status, content=body, headers=headers)

    return handler


URL = "https://api.example.test/records?page=1"


def test_cacheable_statuses_are_2xx_and_404_only() -> None:
    assert all(is_cacheable_status(status) for status in (200, 201, 204, 299, 404))
    assert not any(is_cacheable_status(status) for status in (300, 301, 400, 403, 410, 429, 500))


def test_2xx_is_cached_and_replayed(
    make_client: Callable[..., CachingClient], cache_path: Path
) -> None:
    recorder = Recorder(constant(200, b'{"ok": true}'))
    client = make_client(recorder)
    first = client.get(URL)
    second = client.get(URL)
    assert (first.status, first.from_cache, first.json()) == (200, False, {"ok": True})
    assert (second.status, second.from_cache, second.body) == (200, True, b'{"ok": true}')
    assert len(recorder.requests) == 1
    assert cached_rows(cache_path) == [(URL, 200)]


def test_404_is_cached(make_client: Callable[..., CachingClient], cache_path: Path) -> None:
    recorder = Recorder(constant(404, b"not here"))
    client = make_client(recorder)
    client.get(URL)
    replay = client.get(URL)
    assert (replay.status, replay.from_cache) == (404, True)
    assert len(recorder.requests) == 1
    assert cached_rows(cache_path) == [(URL, 404)]


def test_400_is_returned_but_never_cached(
    make_client: Callable[..., CachingClient], cache_path: Path
) -> None:
    # The motivating case: a bad query parameter answered with 400 must be
    # retried on the next run, not replayed from the cache until a human
    # deletes the row.
    recorder = Recorder(constant(400, b"Page size cannot be greater than 25"))
    client = make_client(recorder)
    first = client.get(URL)
    second = client.get(URL)
    assert (first.status, first.from_cache) == (400, False)
    assert (second.status, second.from_cache) == (400, False)
    assert len(recorder.requests) == 2
    assert cached_rows(cache_path) == []


def test_other_4xx_are_not_cached_either(
    make_client: Callable[..., CachingClient], cache_path: Path
) -> None:
    recorder = Recorder(constant(403))
    client = make_client(recorder)
    client.get(URL)
    client.get(URL)
    assert len(recorder.requests) == 2
    assert cached_rows(cache_path) == []


def test_uncached_response_is_logged(
    make_client: Callable[..., CachingClient], monkeypatch: pytest.MonkeyPatch
) -> None:
    client = make_client(Recorder(constant(400)))
    with capture_logs() as events:
        monkeypatch.setattr(httpcache, "log", structlog.get_logger("test_httpcache"))
        client.get(URL)
    assert [(event["event"], event["status"]) for event in events] == [
        ("http_response_not_cached", 400)
    ]


def test_5xx_exhausts_retries_raises_and_caches_nothing(
    make_client: Callable[..., CachingClient], cache_path: Path, no_sleep: list[float]
) -> None:
    recorder = Recorder(constant(500))
    client = make_client(recorder, max_retries=2)
    with pytest.raises(RuntimeError, match=r"failed after 3 attempts \(last status 500\)"):
        client.get(URL)
    assert len(recorder.requests) == 3
    assert no_sleep == [1.0, 2.0]
    assert cached_rows(cache_path) == []


def test_retryable_then_success_is_cached_once(
    make_client: Callable[..., CachingClient], cache_path: Path, no_sleep: list[float]
) -> None:
    statuses = iter([503, 200])

    def flaky(request: httpx.Request) -> httpx.Response:
        return httpx.Response(next(statuses), content=b"[]")

    recorder = Recorder(flaky)
    client = make_client(recorder)
    response = client.get(URL)
    assert (response.status, response.from_cache) == (200, False)
    assert len(recorder.requests) == 2
    assert no_sleep == [1.0]
    assert cached_rows(cache_path) == [(URL, 200)]
    assert client.get(URL).from_cache is True
    assert len(recorder.requests) == 2


def test_429_honours_a_numeric_retry_after(
    make_client: Callable[..., CachingClient], no_sleep: list[float]
) -> None:
    statuses = iter([429, 200])

    def throttled(request: httpx.Request) -> httpx.Response:
        status = next(statuses)
        headers = {"Retry-After": "7"} if status == 429 else None
        return httpx.Response(status, content=b"[]", headers=headers)

    client = make_client(Recorder(throttled))
    assert client.get(URL).status == 200
    assert no_sleep == [7.0]


def test_refresh_bypasses_the_cache_and_replaces_the_row(
    make_client: Callable[..., CachingClient], cache_path: Path
) -> None:
    bodies = iter([b"first", b"second"])
    recorder = Recorder(lambda request: httpx.Response(200, content=next(bodies)))
    client = make_client(recorder)
    assert client.get(URL).body == b"first"
    refreshed = client.get(URL, refresh=True)
    assert (refreshed.body, refreshed.from_cache) == (b"second", False)
    replay = client.get(URL)
    assert (replay.body, replay.from_cache) == (b"second", True)
    assert len(recorder.requests) == 2
    assert cached_rows(cache_path) == [(URL, 200)]


def test_request_headers_are_part_of_the_cache_key(
    make_client: Callable[..., CachingClient],
) -> None:
    recorder = Recorder(
        lambda request: httpx.Response(200, content=request.headers["Accept"].encode())
    )
    client = make_client(recorder)
    as_json = client.get(URL, headers={"Accept": "application/json"})
    as_csv = client.get(URL, headers={"Accept": "text/csv"})
    assert (as_json.body, as_csv.body) == (b"application/json", b"text/csv")
    assert len(recorder.requests) == 2
    assert client.get(URL, headers={"Accept": "text/csv"}).from_cache is True


def test_user_agent_is_sent_on_every_request(
    make_client: Callable[..., CachingClient],
) -> None:
    recorder = Recorder(constant(200))
    client = make_client(recorder)
    client.get(URL)
    assert recorder.requests[0].headers["User-Agent"] == "test-agent"


def test_cache_survives_a_new_client_on_the_same_file(
    make_client: Callable[..., CachingClient],
) -> None:
    recorder = Recorder(constant(200, b"persisted"))
    make_client(recorder).get(URL)
    replay = make_client(recorder).get(URL)
    assert (replay.body, replay.from_cache) == (b"persisted", True)
    assert len(recorder.requests) == 1
