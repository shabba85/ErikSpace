"""HTTP layer: rate limiting, retry with backoff, on-disk cache, and an
immutable raw-payload archive.

Every computed figure in this system must be reproducible from original bytes.
So every response body is written to
``data/raw/{source}/{ticker}/{retrieved_at}.json`` before anything parses it,
and every request -- including failures -- is appended to an HTTP log.

The archive is append-only by construction: filenames are timestamped and the
writer refuses to overwrite an existing file.
"""

from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Mapping

import httpx

from . import config
from .provenance import utcnow


class FetchError(RuntimeError):
    """A fetch that failed after exhausting retries.

    Raised, never swallowed into a default value.  Callers turn it into a null
    with reason 'upstream_error'.
    """

    def __init__(self, url: str, status: int | None, detail: str):
        super().__init__(f"{url} -> {status}: {detail}")
        self.url = url
        self.status = status
        self.detail = detail


@dataclass
class RateLimiter:
    """Token-bucket-ish sliding window, per source."""

    per_minute: int
    _hits: deque[float] = field(default_factory=deque)
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def acquire(self, sleep=time.sleep, clock=time.monotonic) -> None:
        if self.per_minute <= 0:
            return
        while True:
            with self._lock:
                now = clock()
                while self._hits and now - self._hits[0] >= 60.0:
                    self._hits.popleft()
                if len(self._hits) < self.per_minute:
                    self._hits.append(now)
                    return
                wait = 60.0 - (now - self._hits[0])
            sleep(max(wait, 0.01))


_LIMITERS: dict[str, RateLimiter] = {}


def limiter_for(source: str) -> RateLimiter:
    if source not in _LIMITERS:
        rpm = config.settings()["http"]["rate_limit_per_minute"].get(source, 60)
        _LIMITERS[source] = RateLimiter(per_minute=int(rpm))
    return _LIMITERS[source]


# ---------------------------------------------------------------------------
# raw payload archive
# ---------------------------------------------------------------------------


def _safe(part: str) -> str:
    return "".join(c if c.isalnum() or c in "._-" else "_" for c in part)[:80]


@dataclass(frozen=True)
class Fetched:
    """A response plus everything needed to cite it."""

    source: str
    url: str
    retrieved_at: str
    payload_path: str
    body: Any
    from_cache: bool
    status: int

    @property
    def sha256(self) -> str:
        return hashlib.sha256(
            json.dumps(self.body, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()


def archive(source: str, ticker: str, url: str, body: Any, retrieved_at: str) -> str:
    """Write the raw payload immutably and return its repo-relative path."""
    d = config.path("raw") / _safe(source) / _safe(ticker or "_")
    d.mkdir(parents=True, exist_ok=True)
    fname = f"{_safe(retrieved_at)}.json"
    p = d / fname
    n = 1
    while p.exists():  # never overwrite an archived payload
        p = d / f"{_safe(retrieved_at)}.{n}.json"
        n += 1
    tmp = p.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(
            {"_meta": {"source": source, "ticker": ticker, "url": url,
                       "retrieved_at": retrieved_at}, "body": body},
            indent=2, sort_keys=True, default=str,
        )
    )
    os.replace(tmp, p)
    try:
        p.chmod(0o444)  # immutable-by-convention
    except OSError:  # pragma: no cover
        pass
    return str(p.relative_to(config.ROOT))


def read_archived(payload_path: str) -> Any:
    """Reproduce a figure from the original bytes."""
    p = config.ROOT / payload_path
    return json.loads(p.read_text())["body"]


# ---------------------------------------------------------------------------
# cache
# ---------------------------------------------------------------------------


def _cache_key(url: str, params: Mapping[str, Any] | None) -> str:
    return hashlib.sha256(
        (url + json.dumps(dict(params or {}), sort_keys=True)).encode()
    ).hexdigest()


def _cache_path(source: str, key: str) -> Path:
    return config.path("raw").parent / "cache" / _safe(source) / f"{key}.json"


def _cache_read(source: str, key: str, ttl: int) -> dict[str, Any] | None:
    p = _cache_path(source, key)
    if not p.exists():
        return None
    try:
        rec = json.loads(p.read_text())
    except (json.JSONDecodeError, OSError):
        return None
    age = time.time() - float(rec.get("_cached_at_epoch", 0))
    if ttl >= 0 and age > ttl:
        return None
    return rec


def _cache_write(source: str, key: str, rec: dict[str, Any]) -> None:
    p = _cache_path(source, key)
    p.parent.mkdir(parents=True, exist_ok=True)
    rec = dict(rec, _cached_at_epoch=time.time())
    tmp = p.with_suffix(".tmp")
    tmp.write_text(json.dumps(rec, default=str))
    os.replace(tmp, p)


# ---------------------------------------------------------------------------
# logging
# ---------------------------------------------------------------------------

_log_lock = threading.Lock()


def log_call(record: dict[str, Any]) -> None:
    p = config.path("http_log")
    p.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, sort_keys=True, default=str)
    with _log_lock, p.open("a") as fh:
        fh.write(line + "\n")


# ---------------------------------------------------------------------------
# the fetch
# ---------------------------------------------------------------------------


def get_json(
    source: str,
    url: str,
    *,
    ticker: str = "",
    params: Mapping[str, Any] | None = None,
    headers: Mapping[str, str] | None = None,
    kind: str = "statements",
    client: httpx.Client | None = None,
    force: bool = False,
    redact: tuple[str, ...] = ("apikey", "api_key", "token", "key"),
) -> Fetched:
    """Fetch JSON with cache, rate limit, retry/backoff, archive and log.

    ``redact`` names query parameters stripped from the URL we record, so an API
    key never lands in provenance, the archive, or the log.
    """
    cfg = config.settings()["http"]
    ttl = cfg["cache_ttl_seconds"].get(kind, 3600)
    key = _cache_key(url, params)
    cited = _cited_url(url, params, redact)

    if not force:
        rec = _cache_read(source, key, ttl)
        if rec is not None:
            return Fetched(source=source, url=cited, retrieved_at=rec["retrieved_at"],
                           payload_path=rec["payload_path"], body=rec["body"],
                           from_cache=True, status=int(rec.get("status", 200)))

    hdrs = {"User-Agent": config.user_agent(), "Accept": "application/json"}
    hdrs.update(headers or {})

    backoffs = list(cfg["backoff_seconds"])
    attempts = int(cfg["retries"])
    own = client is None
    cl = client or httpx.Client(timeout=float(cfg["timeout_seconds"]), follow_redirects=True)
    try:
        last: Exception | None = None
        for attempt in range(attempts):
            limiter_for(source).acquire()
            started = utcnow()
            t0 = time.monotonic()
            try:
                resp = cl.get(url, params=dict(params or {}), headers=hdrs)
                elapsed = round(time.monotonic() - t0, 3)
                log_call({"ts": started, "source": source, "ticker": ticker,
                          "url": cited, "status": resp.status_code,
                          "elapsed_s": elapsed, "attempt": attempt + 1,
                          "bytes": len(resp.content), "cached": False})
                if resp.status_code in (429, 500, 502, 503, 504):
                    last = FetchError(cited, resp.status_code, "retryable")
                    _sleep(backoffs, attempt)
                    continue
                if resp.status_code >= 400:
                    raise FetchError(cited, resp.status_code, resp.text[:300])
                body = resp.json()
            except httpx.HTTPError as exc:
                log_call({"ts": started, "source": source, "ticker": ticker,
                          "url": cited, "status": None, "error": str(exc)[:300],
                          "attempt": attempt + 1, "cached": False})
                last = FetchError(cited, None, str(exc))
                _sleep(backoffs, attempt)
                continue

            retrieved_at = utcnow()
            payload_path = archive(source, ticker, cited, body, retrieved_at)
            _cache_write(source, key, {"body": body, "retrieved_at": retrieved_at,
                                       "payload_path": payload_path,
                                       "status": resp.status_code})
            return Fetched(source=source, url=cited, retrieved_at=retrieved_at,
                           payload_path=payload_path, body=body, from_cache=False,
                           status=resp.status_code)
        raise last or FetchError(cited, None, "exhausted retries")
    finally:
        if own:
            cl.close()


def _sleep(backoffs: list[float], attempt: int) -> None:
    if attempt < len(backoffs):
        time.sleep(float(backoffs[attempt]))


def _cited_url(url: str, params: Mapping[str, Any] | None, redact: tuple[str, ...]) -> str:
    """The URL we put in provenance: real, clickable, and key-free."""
    safe = {k: ("REDACTED" if k.lower() in redact else v) for k, v in (params or {}).items()}
    if not safe:
        return url
    qs = "&".join(f"{k}={v}" for k, v in sorted(safe.items()))
    sep = "&" if "?" in url else "?"
    return f"{url}{sep}{qs}"
