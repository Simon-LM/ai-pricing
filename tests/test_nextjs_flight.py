"""Tests for the Next.js Flight payload reader, in isolation.

Standard library only: `python3 -m unittest discover tests`.

Two providers depend on this module reading a value out of a JavaScript string
containing JSON. The failure that matters is not a crash -- it is returning a value
that parses but is wrong, which is exactly what a regex or a naive split does to
nested or escaped data. So the cases here are mostly about not being fooled.

Deliberately synthetic: payloads are built by hand, so the tests keep working when a
provider re-captures its fixtures.
"""

from __future__ import annotations

import json
import sys
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "scripts"))

import nextjs_flight as flight  # noqa: E402


def page(*payloads: object) -> str:
    """Wrap values in the markup a Next.js app actually emits."""
    scripts = []
    for payload in payloads:
        inner = json.dumps(payload) if not isinstance(payload, str) else payload
        scripts.append(f'<script>self.__next_f.push([1,{json.dumps(inner)}])</script>')
    return "<html><body>" + "".join(scripts) + "</body></html>"


class TestChunks(unittest.TestCase):
    def test_a_page_with_no_payload_yields_nothing(self) -> None:
        self.assertEqual(flight.push_chunks("<html><body>hello</body></html>"), [])

    def test_an_escaped_quote_does_not_end_a_chunk(self) -> None:
        """The whole reason the scan is character by character. A chunk is a JS string
        literal, and the JSON inside it is nothing but quotes."""
        html = page('{"name":"OCR 4.1"}')
        self.assertEqual(flight.decoded_chunks(html), ['{"name":"OCR 4.1"}'])

    def test_chunks_come_back_in_document_order(self) -> None:
        html = page('{"a":1}', '{"b":2}')
        self.assertEqual(flight.decoded_chunks(html), ['{"a":1}', '{"b":2}'])

    def test_an_undecodable_chunk_is_skipped_not_fatal(self) -> None:
        """A page carries many chunks and this module is only ever after one."""
        html = '<html><script>self.__next_f.push([1,"\\q"])</script>' + page('{"a":1}')[12:]
        self.assertIn('{"a":1}', flight.decoded_chunks(html))


class TestDecodeAfter(unittest.TestCase):
    def test_a_nested_object_is_bounded_correctly(self) -> None:
        """Where a regex gives up: the value contains braces of its own."""
        text = '{"pricing":{"input":[{"price":4}],"output":[]},"after":1}'
        self.assertEqual(
            flight.decode_after(text, "pricing"), {"input": [{"price": 4}], "output": []}
        )

    def test_a_brace_inside_a_string_does_not_close_the_value(self) -> None:
        text = '{"pricing":{"note":"} not the end","price":4},"after":1}'
        self.assertEqual(flight.decode_after(text, "pricing"), {"note": "} not the end", "price": 4})

    def test_it_reads_scalars_too(self) -> None:
        text = '{"currentModelName":"OCR 4.1","isRetired":false,"count":3}'
        self.assertEqual(flight.decode_after(text, "currentModelName"), "OCR 4.1")
        self.assertIs(flight.decode_after(text, "isRetired"), False)
        self.assertEqual(flight.decode_after(text, "count"), 3)

    def test_an_absent_key_raises(self) -> None:
        with self.assertRaises(ValueError):
            flight.decode_after('{"a":1}', "b")

    def test_an_unterminated_value_raises(self) -> None:
        with self.assertRaises(json.JSONDecodeError):
            flight.decode_after('{"models":[{"id":"a"}', "models")


class TestFindValue(unittest.TestCase):
    def test_it_finds_a_value_in_whichever_chunk_carries_it(self) -> None:
        html = page('{"unrelated":1}', '{"pricing":{"free":true}}')
        self.assertEqual(flight.find_value(html, "pricing"), {"free": True})

    def test_an_absent_key_is_none_rather_than_an_error(self) -> None:
        self.assertIsNone(flight.find_value(page('{"a":1}'), "pricing"))

    def test_a_malformed_value_is_skipped_and_a_later_good_one_wins(self) -> None:
        """Real pages carry the same key twice: once as data and once as a React
        reference string. Neither may take precedence by accident."""
        html = page('{"pricing":[1,2', '{"pricing":{"free":true}}')
        self.assertEqual(flight.find_value(html, "pricing"), {"free": True})


if __name__ == "__main__":
    unittest.main()
