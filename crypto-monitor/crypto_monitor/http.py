"""Small, bounded HTTPS GET client. Redirects and arbitrary destinations are denied."""
from __future__ import annotations

import ipaddress
import json
import socket
import time
import urllib.error
import urllib.parse
import urllib.request


class FetchError(RuntimeError):
    """Safe diagnostic: never includes URLs, credentials, bodies or underlying errors."""


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


def public_host(host: str) -> None:
    try:
        addresses = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        if not addresses or any(not ipaddress.ip_address(a[4][0]).is_global for a in addresses):
            raise FetchError("destination is not public")
    except (OSError, ValueError) as exc:
        raise FetchError("public DNS resolution failed") from None


class Client:
    def __init__(self, allowed_hosts, *, max_requests=60, timeout=10, max_bytes=4_000_000,
                 max_seconds=240, opener=None, resolve=True, sleep=time.sleep):
        self.allowed_hosts = frozenset(allowed_hosts)
        self.max_requests = min(max_requests, 100)
        self.timeout = min(timeout, 20)
        self.max_bytes = min(max_bytes, 8_000_000)
        self.deadline = time.monotonic() + min(max_seconds, 300)
        self.count = 0
        self.opener = opener or urllib.request.build_opener(NoRedirect())
        self.resolve = resolve
        self.sleep = sleep

    def get_bytes(self, url, headers=None):
        try:
            parsed = urllib.parse.urlsplit(url)
            host = parsed.hostname
            if (parsed.scheme != "https" or host not in self.allowed_hosts or parsed.username
                    or parsed.password or parsed.port not in (None, 443) or parsed.fragment):
                raise FetchError("GET destination denied")
        except ValueError:
            raise FetchError("invalid GET destination") from None
        if self.resolve:
            public_host(host)
        safe_headers = {"User-Agent": "crypto-monitor/1.0 (public read-only monitor)",
                        "Accept": "application/json, application/rss+xml, application/atom+xml, text/xml"}
        for name, value in (headers or {}).items():
            low = name.lower()
            permitted = {"x-cg-demo-api-key": "api.coingecko.com",
                         "cg-api-key": "open-api-v4.coinglass.com"}
            if low in permitted and host != permitted[low]:
                raise FetchError("credential destination denied")
            if low not in (*permitted, "user-agent", "accept"):
                raise FetchError("request header denied")
            if "\n" in value or "\r" in value:
                raise FetchError("invalid header")
            safe_headers[name] = value
        for attempt in range(2):
            if self.count >= self.max_requests or time.monotonic() >= self.deadline:
                raise FetchError("run request/time budget exhausted")
            self.count += 1
            req = urllib.request.Request(url, headers=safe_headers, method="GET")
            delay = 1
            try:
                with self.opener.open(req, timeout=self.timeout) as response:
                    body = response.read(self.max_bytes + 1)
                    if len(body) > self.max_bytes:
                        raise FetchError("response size bound exceeded")
                    return body
            except urllib.error.HTTPError as exc:
                exc.close()
                code = exc.code
                if code == 429:
                    try:
                        delay = float(exc.headers.get("Retry-After", "2"))
                    except (TypeError, ValueError):
                        delay = 2
                    if not 0 <= delay <= 15:
                        raise FetchError("provider rate limit; retry deferred") from None
                elif code not in (500, 502, 503, 504):
                    raise FetchError(f"provider HTTP {code}") from None
                if attempt:
                    raise FetchError(f"provider HTTP {code} after bounded retry") from None
            except (OSError, TimeoutError, urllib.error.URLError):
                if attempt:
                    raise FetchError("provider network failure after bounded retry") from None
            if time.monotonic() + delay >= self.deadline:
                raise FetchError("run time budget exhausted")
            self.sleep(delay)
        raise FetchError("provider unavailable")

    def get_json(self, url, headers=None):
        try:
            return json.loads(self.get_bytes(url, headers=headers))
        except (ValueError, UnicodeError):
            raise FetchError("provider returned invalid JSON") from None
