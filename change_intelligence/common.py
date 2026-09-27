"""Small, dependency-free HTTP and serialization helpers."""

import json
import os
import time
from datetime import datetime, timezone
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener


class Failure(Exception):
    """An actionable error safe to print in CI (never a remote response body)."""


class HTTPFailure(Failure):
    def __init__(self, status):
        self.status = status
        super().__init__(f"HTTP {status}; check service availability and credentials")


class NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward API keys or webhook secrets to a redirect destination.
        return None


def request_json(method, url, headers, body=None, attempts=3):
    parsed = urlsplit(url)
    if parsed.scheme != "https" or not parsed.hostname or parsed.username or parsed.password:
        raise Failure("API URLs must use HTTPS and must not contain credentials")
    data = None if body is None else json.dumps(body).encode()
    request = Request(url, data=data, method=method, headers={
        "Content-Type": "application/json", "User-Agent": "application-change-intelligence/1",
        **headers,
    })
    for attempt in range(attempts):
        try:
            with build_opener(NoRedirect()).open(request, timeout=120) as response:
                raw = response.read(8_000_001)
                if len(raw) > 8_000_000:
                    raise Failure("API response exceeded the supported size")
                try:
                    return json.loads(raw) if raw else {}
                except (ValueError, UnicodeError) as exc:
                    raise Failure("API returned an invalid JSON response") from exc
        except HTTPError as exc:
            status = exc.code
            exc.close()
            if status not in (408, 429, 500, 502, 503, 504) or attempt == attempts - 1:
                raise HTTPFailure(status) from None
        except (URLError, TimeoutError, OSError):
            if attempt == attempts - 1:
                raise Failure("API connection failed or timed out") from None
        time.sleep(2 ** attempt)
    raise Failure("API request failed")


def required_env(name):
    value = os.environ.get(name, "").strip()
    if not value:
        raise Failure(f"Configure {name} before running this job")
    return value


def now():
    return datetime.now(timezone.utc).isoformat()


def json_text(value):
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=True) + "\n"
