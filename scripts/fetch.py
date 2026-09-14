"""Fetch a public pricing page. Nothing provider-specific lives here.

Shared by every provider's own scrape.py, so the "no credentials, ever" rule and
the "too short to be a real page" guard are enforced once, not reimplemented
per provider with a chance to drift.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any


class FetchError(Exception):
    """The page could not be retrieved. Callers turn this into their own error type."""


USER_AGENT = "ai-pricing-bot/1 (+https://github.com/Simon-LM/ai-pricing)"
FETCH_TIMEOUT_SECONDS = 30

# Below this, the response is not a pricing page -- it is an error page, a consent
# wall, or a redirect stub. Fail rather than parse it and find nothing.
MIN_PAGE_BYTES = 10_000

# A JSON endpoint's whole body is data, so it is legitimately far smaller than a
# marketing page. The 10 kB floor above would reject a perfectly good response.
MIN_JSON_BYTES = 100

# The two shapes a source comes in, and what each implies for the request. A source
# is declared once, in its provider's mapping.json, rather than each scraper guessing
# from the URL: `html` is the fail-safe default, because reading a JSON endpoint as a
# page fails loudly on the size floor instead of quietly accepting a wrong answer.
#
# Both JSON endpoints this repository reads happen to ignore `Accept` today and serve
# JSON either way, so this is hardening rather than a fix for a live fault. What it
# removes is the pair of assumptions underneath that luck: that a server will keep
# ignoring a header asking it for HTML, and that a data endpoint's answer will always
# be bigger than a marketing page's.
SOURCE_FORMATS: dict[str, dict[str, Any]] = {
    "html": {"accept": "text/html", "min_bytes": MIN_PAGE_BYTES},
    "json": {"accept": "application/json", "min_bytes": MIN_JSON_BYTES},
}


def fetch_page(url: str, *, min_bytes: int = MIN_PAGE_BYTES, accept: str = "text/html") -> str:
    """GET a public page. No credentials of any kind are sent, ever."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": accept}
    )
    try:
        with urllib.request.urlopen(request, timeout=FETCH_TIMEOUT_SECONDS) as response:
            if response.status != 200:
                raise FetchError(f"{url} returned HTTP {response.status}")
            charset = response.headers.get_content_charset() or "utf-8"
            body = response.read().decode(charset, errors="replace")
    except urllib.error.HTTPError as exc:
        raise FetchError(f"{url} returned HTTP {exc.code}") from exc
    except (urllib.error.URLError, TimeoutError, OSError) as exc:
        raise FetchError(f"{url} could not be fetched: {exc}") from exc

    if len(body) < min_bytes:
        raise FetchError(
            f"{url} returned only {len(body)} characters, too short to be the pricing "
            f"page. Probably an error or consent page."
        )
    return body


def fetch_json(url: str, *, min_bytes: int = MIN_JSON_BYTES) -> Any:
    """GET a public JSON endpoint. No credentials of any kind are sent, ever.

    Returns whatever the endpoint parses to, asserting nothing about its shape --
    a caller that needs a particular structure checks for it itself. Raises
    FetchError, not a JSON error, so a caller has one failure type to handle
    whether the endpoint was unreachable or answered with something that is not
    JSON at all.
    """
    body = fetch_page(url, min_bytes=min_bytes, accept=SOURCE_FORMATS["json"]["accept"])
    try:
        return json.loads(body)
    except json.JSONDecodeError as exc:
        raise FetchError(f"{url} did not return JSON: {exc}") from exc
