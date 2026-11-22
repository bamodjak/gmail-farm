"""HTTP client: curl_cffi session with browser impersonation.

One job: make HTTP requests that look like a real browser (TLS/JA3
fingerprint via curl_cffi impersonation), keep cookies across requests
in a session, retry transient failures with exponential backoff, and
optionally route through a proxy. No signup logic lives here.
"""

from __future__ import annotations

import logging
import time
from typing import Any

from curl_cffi.requests import Session

log = logging.getLogger(__name__)

# Browser-like headers for the Google signup pages.
DEFAULT_HEADERS = {
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-User": "?1",
    "Sec-Fetch-Dest": "document",
}


class HttpClient:
    """A single curl_cffi session with retries and cookie persistence."""

    def __init__(self, impersonate: str = "chrome131_android",
                 timeout: float = 30.0, max_retries: int = 3,
                 backoff_base: float = 2.0, proxy: str = "") -> None:
        self.timeout = timeout
        self.max_retries = max_retries
        self.backoff_base = backoff_base
        self.proxy = proxy or None
        self.session = Session(impersonate=impersonate)
        self.session.headers.update(DEFAULT_HEADERS)
        if self.proxy:
            self.session.proxies = {"http": self.proxy, "https": self.proxy}
            log.info("proxy enabled: %s", self._redact_proxy(self.proxy))

    @staticmethod
    def _redact_proxy(proxy: str) -> str:
        # Never log proxy credentials if present in the URL.
        if "@" in proxy:
            scheme, rest = proxy.split("://", 1)
            return f"{scheme}://***@{rest.split('@', 1)[1]}"
        return proxy

    def _request(self, method: str, url: str, **kwargs: Any):
        """One request with retries on transport errors and 5xx."""
        kwargs.setdefault("timeout", self.timeout)
        last_exc: Exception | None = None
        for attempt in range(self.max_retries + 1):
            try:
                resp = self.session.request(method, url, **kwargs)
                if resp.status_code >= 500 and attempt < self.max_retries:
                    wait = self.backoff_base * (2 ** attempt)
                    log.warning("HTTP %s on %s %s; retry in %.1fs",
                                resp.status_code, method, url, wait)
                    time.sleep(wait)
                    continue
                return resp
            except Exception as exc:  # transport error (timeout, reset, ...)
                last_exc = exc
                if attempt < self.max_retries:
                    wait = self.backoff_base * (2 ** attempt)
                    log.warning("transport error on %s %s (%s); retry in %.1fs",
                                method, url, exc, wait)
                    time.sleep(wait)
                    continue
                raise
        # Unreachable in practice; kept for type-checkers.
        if last_exc is not None:
            raise last_exc
        raise RuntimeError("unreachable: retry loop ended without result")

    def get(self, url: str, **kwargs: Any):
        return self._request("GET", url, **kwargs)

    def post(self, url: str, **kwargs: Any):
        return self._request("POST", url, **kwargs)

    def cookies_dict(self) -> dict[str, str]:
        """Current session cookies as a plain dict (for debugging)."""
        try:
            return {c.name: c.value for c in self.session.cookies}
        except Exception:
            return {}

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass
