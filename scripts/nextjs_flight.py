"""Read data out of a Next.js React Server Components ("Flight") payload.

Standard library only. Shared by the two providers whose sources are Next.js apps:
OVH's AI Endpoints catalog and Mistral's documentation site.

Neither of those pages can be scraped by looking at rendered markup. Both stream
their real data to the browser as JSON inside `self.__next_f.push([1, "..."])`
calls, and the prices a browser displays are derived from it at render time. Reading
the JSON is more robust than re-parsing formatted, locale-dependent price text, and
it gives an explicit unit per figure instead of a suffix that has to be
string-matched.

What makes this fiddly enough to be worth writing once: the payload is a JavaScript
string literal containing JSON, so every quote inside it is escaped, and the JSON
itself is arbitrarily nested. Neither a regex nor a naive split survives that, and
getting it subtly wrong yields a truncated object that still parses.
"""

from __future__ import annotations

import json
import re
from typing import Any

_PUSH_START = re.compile(r'self\.__next_f\.push\(\[\d+,"')


def push_chunks(html_text: str) -> list[str]:
    """Every raw, still-escaped string argument of a `self.__next_f.push` call.

    The regex only finds where each string starts; the string itself is walked
    character by character so that an escaped quote (`\\"`) inside it is never
    mistaken for the real closing quote.
    """
    out: list[str] = []
    for match in _PUSH_START.finditer(html_text):
        i = match.end()
        start = i
        while i < len(html_text):
            ch = html_text[i]
            if ch == "\\":
                i += 2
                continue
            if ch == '"':
                break
            i += 1
        out.append(html_text[start:i])
    return out


def decoded_chunks(html_text: str) -> list[str]:
    """The same chunks, unescaped into the text the browser actually parses.

    A chunk that does not decode is skipped rather than fatal: a page carries many
    of them, this module is only ever after one, and one malformed chunk elsewhere
    on the page is not a reason to fail.
    """
    out: list[str] = []
    for raw in push_chunks(html_text):
        try:
            out.append(json.loads('"' + raw + '"'))
        except json.JSONDecodeError:
            continue
    return out


def decode_after(text: str, key: str) -> Any:
    """Parse the JSON value that follows `"<key>":` in `text`, whatever its type.

    Uses the standard decoder's `raw_decode`, which reads exactly one value and stops
    -- so it bounds a nested object or array correctly, and works just as well for the
    string and boolean values these payloads also carry. A regex cannot do either.

    Raises ValueError if the key is absent, or json.JSONDecodeError if what follows it
    is not a complete JSON value.
    """
    marker = f'"{key}":'
    index = text.index(marker)  # ValueError when absent, which callers rely on
    value, _ = json.JSONDecoder().raw_decode(text, index + len(marker))
    return value


def find_value(html_text: str, key: str) -> Any:
    """The value of the first `"<key>":` found in any chunk, or None if there is none.

    A chunk whose value will not parse is skipped rather than fatal: a page carries
    many chunks, this is only ever after one, and a malformed value elsewhere on the
    page is not a reason to fail. A caller that needs to tell "absent" apart from
    "malformed" should locate its chunk itself and call `decode_after`.
    """
    marker = f'"{key}":'
    for chunk in decoded_chunks(html_text):
        if marker not in chunk:
            continue
        try:
            return decode_after(chunk, key)
        except (ValueError, json.JSONDecodeError):
            continue
    return None
