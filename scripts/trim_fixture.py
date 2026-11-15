#!/usr/bin/env python3
"""Cut a provider's test fixture out of a page, in the shape the fixtures are kept in.

A fixture is the copy of a public page that a provider's tests parse, so that a change
to the page is a change to a file in this repository -- visible in a diff, reviewable,
and caught by the suite before the daily run reaches production. Cutting one by hand is
what produced the shape this tool reproduces:

  - the page's text, its tables, its own comments and the badges' aria-label are kept;
  - scripts, styles, icons and the attributes no fixture carries are dropped;
  - the page's chrome before the first heading the tests read is dropped;
  - a head states the page and the day it was served, so a reader can fetch it again.

`--page` reads a saved copy; without it the page is fetched exactly as the scraper
fetches it (public, no credentials, refused below the size floor a real page clears).

Usage:
    scripts/trim_fixture.py --url https://example.com/pricing --served 2026-11-15 \\
        --from-heading "Model pricing" --out tests/fixtures/example/pricing_ok.html
"""

from __future__ import annotations

import argparse
import re
import sys
from html.parser import HTMLParser
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from fetch import FetchError, fetch_page  # noqa: E402

CLI_SUMMARY = "Cut a provider's test fixture out of a page."

# The attributes the committed fixtures carry. One a page states but no fixture keeps is
# dropped: a class name or a data- attribute is the site's business, not the scraper's,
# and keeping them would make every fixture churn on every redesign.
DEFAULT_ATTRIBUTES = (
    "href", "target", "rel", "scope", "rowspan", "colspan", "span",
    "role", "aria-label", "translate", "lang", "charset", "dir",
)

# Elements dropped whole, with everything inside them: nothing a scraper reads lives in
# an icon, a style or a script.
DEFAULT_DROPPED_TAGS = ("script", "style", "svg", "canvas", "link", "path")

# Elements that carry no end tag. The parser reports them as start tags, so the trimmer
# needs to know which tags do not open a subtree.
VOID_TAGS = frozenset(
    ["br", "img", "hr", "input", "meta", "link", "col", "source", "area", "base", "embed", "track", "wbr"]
)

DEFAULT_COMMENT = (
    "scripts, styles, icons and most attributes stripped; the text, the tables and the "
    "badges' aria-label kept."
)

_COMMENT = re.compile(r"<!-- (?P<url>\S+) as served on (?P<served>[0-9]{4}-[0-9]{2}-[0-9]{2}): (?P<comment>.*?) --")
_ATTRIBUTE = re.compile(
    r"""\s+([^\s=/>]+)                                # the name
        (?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s>]*)))?  # = "value" | 'value' | bare
    """,
    re.X | re.S,
)
_HEADING = r"<h[1-6][^>]*>(?:<[^>]+>)*\s*"


def trimmed_tag(raw: str, kept: frozenset[str]) -> str:
    """`raw` is a whole start tag, `<a href="x" class="y">`: the same tag, attributes the
    fixtures keep only, and the name's case kept as the page writes it (`colSpan`)."""
    name = re.match(r"<\s*([^\s/>]+)", raw).group(1)  # type: ignore[union-attr]
    tail = raw[1 + len(name):]
    closing = "/>" if tail.rstrip().endswith("/>") else ">"
    body = tail.rstrip()[:-2] if closing == "/>" else tail.rstrip()[:-1]
    attributes = []
    for match in _ATTRIBUTE.finditer(body):
        attribute, double_quoted, single_quoted, bare = match.groups()
        if attribute.lower() not in kept:
            continue
        value = double_quoted if double_quoted is not None else single_quoted if single_quoted is not None else bare
        if double_quoted is None and single_quoted is None:
            attributes.append(attribute if not value else f'{attribute}="{value}"')
        else:
            attributes.append(f'{attribute}="{value}"')
    return "<" + name + "".join(" " + attribute for attribute in attributes) + closing


class _Trimmer(HTMLParser):
    """The page, re-emitted as text: every element kept, every end tag closed, and the
    attributes, the scripts, the styles and the icons left out."""

    def __init__(self, kept: frozenset[str], dropped: frozenset[str]) -> None:
        super().__init__(convert_charrefs=False)
        self.out: list[str] = []
        self.kept = kept
        self.dropped = dropped
        self.skipping = 0

    def handle_decl(self, decl: str) -> None:
        if not self.skipping:
            self.out.append("<!" + decl + ">")

    def handle_comment(self, data: str) -> None:
        if not self.skipping:
            self.out.append("<!--" + data + "-->")

    def kept_tag(self) -> str:
        """The start tag the parser is inside, as the page writes it. `get_starttag_text`
        is typed optional; it is always there for a start tag being handled."""
        raw = self.get_starttag_text()
        assert raw is not None
        return trimmed_tag(raw, self.kept)

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skipping:
            if tag not in VOID_TAGS:
                self.skipping += 1
            return
        if tag in self.dropped:
            if tag not in VOID_TAGS:
                self.skipping = 1
            return
        self.out.append(self.kept_tag())

    def handle_startendtag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if self.skipping or tag in self.dropped:
            return
        self.out.append(self.kept_tag())

    def handle_endtag(self, tag: str) -> None:
        if self.skipping:
            self.skipping -= 1
            return
        if tag in VOID_TAGS:
            return
        self.out.append("</" + tag + ">")

    def handle_data(self, data: str) -> None:
        if not self.skipping:
            self.out.append(data)

    def handle_entityref(self, name: str) -> None:
        if not self.skipping:
            self.out.append("&" + name + ";")

    def handle_charref(self, name: str) -> None:
        if not self.skipping:
            self.out.append("&#" + name + ";")


def trim(page: str, url: str, served: str, from_heading: str | None, comment: str = DEFAULT_COMMENT,
         attributes: tuple[str, ...] = DEFAULT_ATTRIBUTES, dropped_tags: tuple[str, ...] = DEFAULT_DROPPED_TAGS) -> str:
    """The fixture: a head that states where the page came from, then the page from
    `from_heading` on, or from the top of its body when `from_heading` is None."""
    trimmer = _Trimmer(frozenset(a.lower() for a in attributes), frozenset(t.lower() for t in dropped_tags))
    trimmer.feed(page)
    trimmer.close()
    trimmed = "".join(trimmer.out)

    lang = re.search(r"<html[^>]*\slang=\"([^\"]*)\"", trimmed)
    title = re.search(r"<title>(.*?)</title>", trimmed, re.S)
    if lang is None or title is None:
        raise ValueError("the page states no lang or no title to write the head of the fixture with")

    body = trimmed
    if from_heading is not None:
        start = re.search(_HEADING + re.escape(from_heading), trimmed)
        if start is None:
            raise ValueError(f"the page carries no heading {from_heading!r}: the fixture would start somewhere else")
        body = trimmed[start.start():]

    return (
        "<!DOCTYPE html>\n"
        f"<html lang=\"{lang.group(1)}\"><head><meta charset=\"utf-8\">"
        f"<title>{title.group(1)}</title></head><body>\n"
        f"<!-- {url} as served on {served}: {comment} -->\n" + body
    )


def recorded_comment(fixture: str) -> tuple[str, str, str]:
    """(the url, the day, the sentence) a fixture's own comment records. Read back so that
    a fixture says how to cut it again, and so that a test can check it still holds."""
    match = _COMMENT.search(fixture)
    if match is None:
        raise ValueError("the fixture carries no comment stating the page it came from")
    return match["url"], match["served"], match["comment"]


def read_page(page: str | None, url: str) -> str:
    if page is not None:
        return Path(page).read_text(encoding="utf-8", errors="replace")
    try:
        return fetch_page(url)
    except FetchError as exc:
        raise SystemExit(f"could not read {url}: {exc}") from exc


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=CLI_SUMMARY)
    parser.add_argument("--url", required=True, help="the page the fixture is cut from, as the scraper reads it")
    parser.add_argument("--served", required=True, metavar="YYYY-MM-DD", help="the day the page was served")
    parser.add_argument("--out", required=True, help="the fixture to write")
    parser.add_argument("--from-heading", help="start the body at this heading, dropping the page's chrome")
    parser.add_argument("--page", help="read this saved copy of the page instead of fetching --url")
    parser.add_argument("--comment", default=DEFAULT_COMMENT, help="what the fixture's comment says was stripped")
    parser.add_argument("--keep-attr", action="append", default=[], metavar="NAME",
                        help="keep this attribute too (repeatable)")
    parser.add_argument("--drop-tag", action="append", default=[], metavar="TAG",
                        help="drop this element whole too, with everything inside it (repeatable)")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not re.fullmatch(r"[0-9]{4}-[0-9]{2}-[0-9]{2}", args.served):
        raise SystemExit(f"--served takes a date, not {args.served!r}")
    fixture = trim(
        read_page(args.page, args.url), args.url, args.served, args.from_heading, args.comment,
        DEFAULT_ATTRIBUTES + tuple(args.keep_attr), DEFAULT_DROPPED_TAGS + tuple(args.drop_tag),
    )
    Path(args.out).write_text(fixture, encoding="utf-8")
    print(f"{args.out}: {len(fixture)} characters, cut from {args.url} as served on {args.served}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
