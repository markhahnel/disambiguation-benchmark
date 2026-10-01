"""SQLite-backed HTTP cache with polite headers, rate limiting, and backoff.

A second run of the same requests hits the network zero times unless
refresh=True. Cache key is method + URL + the request headers that affect
the response.

What gets cached: 2xx responses and 404. A 404 is a stable answer about a
resource (the record does not exist) and replaying it is correct. Every other
4xx and every 5xx describes the request or the server on the day, not the
resource, so it is returned to the caller but never written to the cache. The
case that motivated this: a 400 from an over-large page-size parameter was
cached once and would have replayed on every later run until somebody deleted
the row by hand.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import httpx
import structlog

log = structlog.get_logger(__name__)

_SCHEMA = """
CREATE TABLE IF NOT EXISTS http_cache (
    key TEXT PRIMARY KEY,
    url TEXT NOT NULL,
    status INTEGER NOT NULL,
    body BLOB NOT NULL,
    fetched_at TEXT NOT NULL
);
"""

RETRYABLE_STATUSES = frozenset({429, 500, 502, 503, 504})


def is_cacheable_status(status: int) -> bool:
    """2xx and 404 only. See the module docstring for why 404 is in and 400 is out."""
    return 200 <= status < 300 or status == 404


@dataclass
class CachedResponse:
    url: str
    status: int
    body: bytes
    from_cache: bool

    def json(self) -> object:
        return json.loads(self.body)


class CachingClient:
    """Sequential polite HTTP client. One host, min_interval between hits.

    transport exists so tests can hand in an httpx.MockTransport; production
    callers leave it unset and get httpx's default.
    """

    def __init__(
        self,
        cache_path: Path,
        user_agent: str,
        min_interval_s: float = 0.25,
        max_retries: int = 5,
        timeout_s: float = 30.0,
        transport: httpx.BaseTransport | None = None,
    ) -> None:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        self._db = sqlite3.connect(cache_path)
        self._db.execute(_SCHEMA)
        self._db.commit()
        self._client = httpx.Client(
            headers={"User-Agent": user_agent},
            timeout=timeout_s,
            follow_redirects=True,
            transport=transport,
        )
        self._min_interval_s = min_interval_s
        self._max_retries = max_retries
        self._last_request_at = 0.0

    @staticmethod
    def _key(method: str, url: str, headers: dict[str, str] | None) -> str:
        payload = json.dumps([method, url, sorted((headers or {}).items())])
        return hashlib.sha256(payload.encode()).hexdigest()

    def get(
        self, url: str, headers: dict[str, str] | None = None, refresh: bool = False
    ) -> CachedResponse:
        key = self._key("GET", url, headers)
        if not refresh:
            row = self._db.execute(
                "SELECT status, body FROM http_cache WHERE key = ?", (key,)
            ).fetchone()
            if row is not None:
                return CachedResponse(url=url, status=row[0], body=row[1], from_cache=True)

        response = self._fetch_with_backoff(url, headers)
        if is_cacheable_status(response.status_code):
            self._db.execute(
                "INSERT OR REPLACE INTO http_cache (key, url, status, body, fetched_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (
                    key,
                    url,
                    response.status_code,
                    response.content,
                    datetime.now(UTC).isoformat(),
                ),
            )
            self._db.commit()
        else:
            log.warning("http_response_not_cached", url=url, status=response.status_code)
        return CachedResponse(
            url=url, status=response.status_code, body=response.content, from_cache=False
        )

    def _fetch_with_backoff(self, url: str, headers: dict[str, str] | None) -> httpx.Response:
        delay = 1.0
        for attempt in range(self._max_retries + 1):
            wait = self._min_interval_s - (time.monotonic() - self._last_request_at)
            if wait > 0:
                time.sleep(wait)
            self._last_request_at = time.monotonic()
            response = self._client.get(url, headers=headers)
            if response.status_code not in RETRYABLE_STATUSES:
                return response
            if attempt == self._max_retries:
                break
            retry_after = response.headers.get("Retry-After")
            sleep_s = float(retry_after) if retry_after and retry_after.isdigit() else delay
            log.warning(
                "http_retry", url=url, status=response.status_code, attempt=attempt, sleep=sleep_s
            )
            time.sleep(sleep_s)
            delay *= 2
        raise RuntimeError(
            f"GET {url} failed after {self._max_retries + 1} attempts "
            f"(last status {response.status_code})"
        )

    def close(self) -> None:
        self._client.close()
        self._db.close()
