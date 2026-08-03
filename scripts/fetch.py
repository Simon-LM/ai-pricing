"""Fetch a public pricing page. Nothing provider-specific lives here.

Shared by every provider's own scrape.py, so the "no credentials, ever" rule and
the "too short to be a real page" guard are enforced once, not reimplemented
per provider with a chance to drift.
"""

from __future__ import annotations

import urllib.error
import urllib.request


class FetchError(Exception):
    """The page could not be retrieved. Callers turn this into their own error type."""


USER_AGENT = "ai-pricing-bot/1 (+https://github.com/Simon-LM/ai-pricing)"
FETCH_TIMEOUT_SECONDS = 30

# Below this, the response is not a pricing page -- it is an error page, a consent
# wall, or a redirect stub. Fail rather than parse it and find nothing.
MIN_PAGE_BYTES = 10_000


def fetch_page(url: str, *, min_bytes: int = MIN_PAGE_BYTES) -> str:
    """GET a public page. No credentials of any kind are sent, ever."""
    request = urllib.request.Request(
        url, headers={"User-Agent": USER_AGENT, "Accept": "text/html"}
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
