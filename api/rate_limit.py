"""Per-client sliding-window rate limits for API routes."""
from __future__ import annotations

import time
from collections import defaultdict, deque
from threading import Lock

from starlette.requests import Request

from .. import config

_lock = Lock()
_hits: dict[str, deque[float]] = defaultdict(deque)
_cooldown_until: dict[str, float] = {}
_violation_strikes: dict[str, int] = {}
_last_violation_at: dict[str, float] = {}
# Last compute attempt (monotonic) per IP — enforces COMPUTE_MIN_INTERVAL_SEC.
_last_compute_attempt: dict[str, float] = {}

_BUCKET_LIMIT_ATTR = {
    "api": "RATE_LIMIT_API_PER_MIN",
    "compute": "RATE_LIMIT_COMPUTE_PER_MIN",
    "db_register": "RATE_LIMIT_DB_REGISTER_PER_MIN",
    "phases": "RATE_LIMIT_PHASES_PER_MIN",
}

_COOLDOWN_ATTR = {
    "compute": "RATE_LIMIT_COMPUTE_COOLDOWN_SEC",
    "db_register": "RATE_LIMIT_DB_REGISTER_COOLDOWN_SEC",
}


def client_ip(request: Request) -> str:
    """Prefer Cloudflare / reverse-proxy headers when present."""
    cf = request.headers.get("cf-connecting-ip", "").strip()
    if cf:
        return cf
    xff = request.headers.get("x-forwarded-for", "").strip()
    if xff:
        return xff.split(",", 1)[0].strip()
    if request.client and request.client.host:
        return request.client.host
    return "unknown"


def limit_for_bucket(bucket: str) -> int:
    attr = _BUCKET_LIMIT_ATTR.get(bucket)
    return int(getattr(config, attr)) if attr else 0


def cooldown_sec_for_bucket(bucket: str) -> int:
    attr = _COOLDOWN_ATTR.get(bucket)
    return int(getattr(config, attr)) if attr else 0


def _buckets_for_request(method: str, path: str) -> list[str]:
    if not config.RATE_LIMIT_ENABLED or not path.startswith("/api/"):
        return []
    if path == "/api/health":
        return []

    buckets: list[str] = []
    if limit_for_bucket("api") > 0:
        buckets.append("api")

    if method == "POST":
        if path == "/api/compute" and limit_for_bucket("compute") > 0:
            buckets.append("compute")
        elif path == "/api/databases/register" and limit_for_bucket("db_register") > 0:
            buckets.append("db_register")
        elif path == "/api/phases" and limit_for_bucket("phases") > 0:
            buckets.append("phases")

    return buckets


def _retry_after(q: deque[float], limit: int, now: float, window: float) -> int | None:
    cutoff = now - window
    while q and q[0] <= cutoff:
        q.popleft()
    if len(q) >= limit:
        return int(max(1, q[0] + window - now))
    return None


def _cooldown_remaining(bucket: str, ip: str, now: float) -> int | None:
    base = cooldown_sec_for_bucket(bucket)
    if base <= 0:
        return None

    key = f"{bucket}:{ip}"
    until = _cooldown_until.get(key)
    if until is None:
        return None
    if now >= until:
        _cooldown_until.pop(key, None)
        return None
    return int(max(1, until - now))


def _start_cooldown(bucket: str, ip: str, now: float) -> int:
    base = cooldown_sec_for_bucket(bucket)
    if base <= 0:
        return 0

    key = f"{bucket}:{ip}"
    last = _last_violation_at.get(key, 0.0)
    if now - last > float(config.RATE_LIMIT_VIOLATION_RESET_SEC):
        _violation_strikes[key] = 0

    strikes = _violation_strikes.get(key, 0) + 1
    _violation_strikes[key] = strikes
    _last_violation_at[key] = now

    duration = float(base)
    if config.RATE_LIMIT_COOLDOWN_ESCALATE:
        max_sec = float(config.RATE_LIMIT_COOLDOWN_MAX_SEC)
        duration = min(max_sec, base * (2 ** (strikes - 1)))

    _cooldown_until[key] = now + duration
    return int(max(1, duration))


def _compute_min_interval_remaining(ip: str, now: float) -> int | None:
    gap = float(config.COMPUTE_MIN_INTERVAL_SEC)
    if gap <= 0:
        return None
    last = _last_compute_attempt.get(ip)
    if last is None:
        return None
    remain = last + gap - now
    if remain <= 0:
        return None
    return int(max(1, remain))


def limit_detail_message(
    bucket: str,
    *,
    reason: str,
    retry_after: int,
    limit: int,
) -> str:
    label = bucket.replace("_", " ")
    if reason == "cooldown":
        return (
            f"{label.title()} temporarily blocked after repeated burst limits. "
            f"Try again in {retry_after} s."
        )
    if reason == "min_interval":
        return (
            f"Please wait at least {int(config.COMPUTE_MIN_INTERVAL_SEC)} s "
            f"between compute requests. Try again in {retry_after} s."
        )
    return (
        f"Rate limit exceeded ({limit} requests per "
        f"{config.RATE_LIMIT_WINDOW_SEC} s for {label}). "
        f"Try again in {retry_after} s."
    )


def check_rate_limit(
    request: Request,
) -> tuple[bool, str | None, int | None, str | None]:
    """Return (allowed, failing_bucket, retry_after_sec, reason)."""
    buckets = _buckets_for_request(request.method, request.url.path)
    if not buckets:
        return True, None, None, None

    ip = client_ip(request)
    now = time.monotonic()
    window = float(config.RATE_LIMIT_WINDOW_SEC)
    keyed = [(bucket, f"{bucket}:{ip}", limit_for_bucket(bucket)) for bucket in buckets]
    is_compute = request.method == "POST" and request.url.path == "/api/compute"

    with _lock:
        for bucket in buckets:
            remaining = _cooldown_remaining(bucket, ip, now)
            if remaining is not None:
                return False, bucket, remaining, "cooldown"

        if is_compute:
            gap_remain = _compute_min_interval_remaining(ip, now)
            if gap_remain is not None:
                return False, "compute", gap_remain, "min_interval"

        for bucket, key, limit in keyed:
            retry_after = _retry_after(_hits[key], limit, now, window)
            if retry_after is not None:
                if cooldown_sec_for_bucket(bucket) > 0:
                    cooldown = _start_cooldown(bucket, ip, now)
                    return False, bucket, cooldown, "cooldown"
                return False, bucket, retry_after, "burst"

        for _bucket, key, limit in keyed:
            q = _hits[key]
            _retry_after(q, limit, now, window)
            q.append(now)

        if is_compute:
            _last_compute_attempt[ip] = now

    return True, None, None, None
