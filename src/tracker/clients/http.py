"""Shared HTTP helper: identifiable User-Agent, timeout, bounded retry with exponential backoff."""

import logging
import time
from typing import Callable

import httpx

from ..config import get_settings

log = logging.getLogger(__name__)


class UpstreamError(Exception):
    """Endpoint unreachable, returned an unexpected status, or returned unparseable data."""


class NotFound(UpstreamError):
    """Endpoint returned 404 (for hiscores: player not on this table)."""


class ProfilePrivate(UpstreamError):
    """RuneMetrics refused the profile (PROFILE_PRIVATE, NO_PROFILE, ...)."""


def make_client() -> httpx.Client:
    s = get_settings()
    return httpx.Client(
        headers={"User-Agent": s.user_agent, "Accept": "*/*"},
        timeout=s.http_timeout_s,
        follow_redirects=True,
    )


def get_with_retry(
    client: httpx.Client, url: str, *, retries: int | None = None, sleep: Callable[[float], None] = time.sleep
) -> httpx.Response:
    """GET with retries on transport errors, 429 and 5xx. 404 raises NotFound immediately."""
    attempts = retries if retries is not None else get_settings().http_retries
    last_error: Exception | None = None
    for attempt in range(1, attempts + 1):
        try:
            resp = client.get(url)
        except httpx.TransportError as exc:
            last_error = exc
        else:
            if resp.status_code == 404:
                raise NotFound(f"404 from {url}")
            if resp.status_code == 200:
                return resp
            last_error = UpstreamError(f"HTTP {resp.status_code} from {url}")
            if resp.status_code < 500 and resp.status_code != 429:
                raise last_error
        if attempt < attempts:
            delay = 2 ** attempt
            log.warning("request failed (%s), retrying in %ss", last_error, delay)
            sleep(delay)
    raise UpstreamError(f"giving up after {attempts} attempts: {last_error}") from last_error
