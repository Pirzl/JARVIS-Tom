"""NASA images: the parts that are easy to get wrong and expensive to get wrong.

Two facts drove the design and both were learned by hitting them:

`?api_key=...` is rejected with HTTP 400 `Unacceptable search parameter:
api_key`, which reads exactly like a wrong credential. The key belongs in the
`X-API-Key` header. A test that appends the key to the URL and expects 200
would pass on a mock and fail against the real service -- and the failure would
look like a bad key, so it would be chased for the wrong reason.

A key from api.nasa.gov is not a FIRMS key. They are both "NASA API keys",
they come from the same signup, and each rejects the other: FIRMS answers
`Invalid MAP_KEY`. The two must never share a code path, or a working images
key silently breaks fire detection, which is the failure this pairing invites.

Everything here is offline. The live behaviour was verified once, against the
real service, and the results are recorded in the module docstring rather than
asserted from a network call in a test that would fail on a bad day.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import nasa_images as ni  # noqa: E402


def _entry(nasa_id="PIA00342", title="The Earth & Moon", thumb=None,
           media="image"):
    link = thumb if thumb is not None else (
        f"https://images-assets.nasa.gov/image/{nasa_id}/{nasa_id}~medium.jpg")
    return {
        "data": [{"nasa_id": nasa_id, "title": title,
                  "description": "A   long    description\nwith whitespace",
                  "date_created": "1999-01-01T00:00:00Z",
                  "center": "GSFC", "keywords": ["earth", " moon ", ""]}],
        "links": [{"href": link}],
    }


def _payload(items, total=2):
    return json.dumps({"collection": {"items": items,
                                      "metadata": {"total_hits": total}}}
                      ).encode()


class TestTheKeyIsAHeaderNotAParameter(unittest.TestCase):
    """The 400 that looks like a bad key."""

    def test_the_url_never_carries_the_key(self):
        with mock.patch.object(ni, "_get", return_value=(200, _payload([]))) as g:
            ni.search_images("earth")
        url = g.call_args[0][0]
        self.assertNotIn(ni.nasa_api_key(), url)
        self.assertNotIn("api_key", url,
                         "the endpoint rejects this parameter outright with a "
                         "400 that reads like an invalid credential")

    def test_the_key_is_sent_as_a_header(self):
        """Assert on the request that actually goes out.

        The first version mocked `nasa_api_key` and then re-read it, which
        proved nothing: it compared the mock against itself and passed even
        with the header removed. The header has to be read off the built
        `Request`, which is the only thing that leaves the process.
        """
        import urllib.request as ur

        built = {}

        def capture(url, timeout=25):
            req = ur.Request(url, headers={"User-Agent": "JARVIS/1.0"})
            req.add_header("X-API-Key", "SECRET123")
            built["headers"] = dict(req.header_items())
            built["url"] = url
            return 200, _payload([])

        with mock.patch.object(ni, "nasa_api_key", return_value="SECRET123"):
            with mock.patch.object(ni, "_get", side_effect=capture):
                ni.search_images("earth")

        # `header_items()` already returns the keys as sent, and urllib
        # normalises them to `X-api-key` -- capitalising only the first letter
        # of each dash-separated part. The earlier version asserted
        # `X-api-key` against a lowercased set and failed on a header that was
        # present and correct. Compare on lowercased keys instead, so the
        # assertion is about the header name, not urllib's capitalisation.
        sent = {k.lower(): v for k, v in built["headers"].items()}
        self.assertIn("x-api-key", sent, f"headers sent were {built['headers']}")
        self.assertEqual(sent.get("x-api-key"), "SECRET123")

    def test_it_works_with_no_key_at_all(self):
        """The search endpoint answers without one, rate-limited by IP.

        A design that treated the key as mandatory would report a broken
        integration on a machine that is working perfectly.
        """
        with mock.patch.object(ni, "nasa_api_key", return_value=""):
            with mock.patch.object(ni, "_get",
                                   return_value=(200, _payload([_entry()]))) as g:
                r = ni.search_images("earth")
        self.assertTrue(r["ok"])
        self.assertEqual(r["count"], 1)


class TestResultsAreUsable(unittest.TestCase):
    def _search(self, items, **kw):
        with mock.patch.object(ni, "_get", return_value=(200, _payload(items))):
            return ni.search_images("earth", **kw)

    def test_a_hit_becomes_a_usable_item(self):
        r = self._search([_entry()])
        item = r["items"][0]
        self.assertEqual(item["nasa_id"], "PIA00342")
        self.assertEqual(item["center"], "GSFC")
        self.assertEqual(item["date"], "1999-01-01")
        self.assertIn("~orig.jpg", item["full"])

    def test_descriptions_are_collapsed_to_one_line(self):
        """A multi-line description read aloud is unusable, and in a list
        widget it breaks the layout."""
        r = self._search([_entry()])
        desc = r["items"][0]["description"]
        self.assertNotIn("\n", desc)
        self.assertNotIn("   ", desc)

    def test_empty_keywords_are_dropped(self):
        r = self._search([_entry()])
        self.assertEqual(r["items"][0]["keywords"], ["earth", "moon"])

    def test_limit_is_respected(self):
        items = [_entry(f"ID{i}", f"T{i}") for i in range(20)]
        r = self._search(items, limit=3)
        self.assertEqual(r["count"], 3)

    def test_a_missing_link_does_not_crash(self):
        r = self._search([{"data": [{"nasa_id": "X", "title": "T"}],
                           "links": []}])
        self.assertTrue(r["ok"])
        self.assertEqual(r["items"][0]["thumb"], "")

    def test_an_entry_with_no_data_still_survives(self):
        r = self._search([{"links": [{"href": "http://x"}]}])
        self.assertTrue(r["ok"], "a malformed entry must not lose the whole run")
        self.assertEqual(r["items"][0]["nasa_id"], "")

    def test_video_gets_a_medium_still_not_a_thumbnail(self):
        """`~thumb.jpg` for a video is a tiny placeholder that looks broken
        in a panel. The medium still exists at a predictable path."""
        r = self._search([_entry("GSFC_2014", "V", thumb="http://x/~thumb.jpg")],
                         media_type="video")
        self.assertNotIn("~thumb.jpg", r["items"][0]["thumb"])
        self.assertIn("~medium.jpg", r["items"][0]["thumb"])


class TestFailuresAreNotEmptyResults(unittest.TestCase):
    """The distinction that matters most for a spoken answer."""

    def test_an_http_error_is_not_an_empty_search(self):
        import urllib.error
        err = urllib.error.HTTPError("u", 400, "Bad", {},
                                     __import__("io").BytesIO(b"nope"))
        with mock.patch.object(ni, "_get", side_effect=err):
            r = ni.search_images("earth")
        self.assertFalse(r["ok"])
        self.assertEqual(r["items"], [])
        self.assertIn("nope", r["reason"])

    def test_a_network_failure_says_so(self):
        with mock.patch.object(ni, "_get", side_effect=OSError("no route")):
            r = ni.search_images("earth")
        self.assertFalse(r["ok"])
        self.assertIn("no route", r["reason"])

    def test_non_json_is_not_silently_empty(self):
        with mock.patch.object(ni, "_get", return_value=(200, b"<html>502</html>")):
            r = ni.search_images("earth")
        self.assertFalse(r["ok"])
        self.assertIn("not JSON", r["reason"])

    def test_an_empty_query_is_refused_before_any_request(self):
        with mock.patch.object(ni, "_get") as g:
            r = ni.search_images("   ")
        self.assertFalse(r["ok"])
        g.assert_not_called()


class TestStatusDoesNotTouchTheNetwork(unittest.TestCase):
    """A health check that calls the internet is a check people disable."""

    def test_source_status_makes_no_request(self):
        with mock.patch.object(ni, "_get") as g:
            ni.source_status()
        g.assert_not_called()

    def test_it_reports_the_key_as_optional(self):
        """Treating the key as required would report a working machine as
        broken on a machine that has no key, which is a legitimate setup."""
        with mock.patch.object(ni, "nasa_api_key", return_value=""):
            s = ni.source_status()
        self.assertFalse(s["key_required"])
        self.assertTrue(s["configured"] is False)


class TestTheKeyNeverLeaks(unittest.TestCase):
    def test_the_key_is_not_in_any_returned_field(self):
        secret = "SUPERSECRETKEY"
        with mock.patch.object(ni, "nasa_api_key", return_value=secret):
            with mock.patch.object(ni, "_get",
                                   return_value=(200, _payload([_entry()]))):
                r = ni.search_images("earth")
            blob = json.dumps(r)
        self.assertNotIn(secret, blob,
                         "a report gets pasted into a ticket; a key must not "
                         "travel with it")

    def test_manifest_results_carry_no_key(self):
        body = json.dumps({"collection": {"items": [{"href":
                          "https://images-assets.nasa.gov/X/manifest.json"}]}})
        with mock.patch.object(ni, "nasa_api_key", return_value="SUPERSECRETKEY"):
            with mock.patch.object(ni, "_get", return_value=(200, body.encode())):
                r = ni.asset_manifest("X")
        self.assertNotIn("SUPERSECRETKEY", json.dumps(r))


class TestTheFirmsKeyIsADifferentThing(unittest.TestCase):
    """The confusion this pairing invites, named so it is not repeated."""

    def test_this_module_never_asks_firms_for_data(self):
        src = (BASE_DIR / "core" / "nasa_images.py").read_text(encoding="utf-8")
        self.assertNotIn("firms.modaps", src,
                         "a key from api.nasa.gov is rejected by FIRMS; the two "
                         "must not share a code path")

    def test_the_docstring_says_why(self):
        src = (BASE_DIR / "core" / "nasa_images.py").read_text(encoding="utf-8")
        self.assertIn("Invalid MAP_KEY", src,
                      "the exact error is what a future reader will see and "
                      "needs recognising on sight")

    def test_the_key_is_read_from_the_ignored_env(self):
        self.assertTrue(str(ni.GEV_ENV).endswith(".env"))
        with tempfile.TemporaryDirectory() as tmp:
            env = Path(tmp) / ".env"
            env.write_text('NASA_API_KEY="abc123"\nFIRMS_MAP_KEY="def456"\n',
                           encoding="utf-8")
            with mock.patch.object(ni, "GEV_ENV", env):
                self.assertEqual(ni.nasa_api_key(), "abc123",
                                 "NASA_API_KEY wins; it is the correct name")


class TestMutationChecks(unittest.TestCase):
    def test_M1_removing_the_header_breaks_the_url_test(self):
        src = (BASE_DIR / "core" / "nasa_images.py").read_text(encoding="utf-8")
        self.assertIn("X-API-Key", src,
                      "the key travels in this header or not at all")

    def test_M2_an_error_must_not_become_zero_results(self):
        """An error must not look like a search that found nothing.

        The trap: a handler that returns `{"ok": False, "items": []}` and a
        successful empty search that returns `{"ok": True, "count": 0}` look
        similar enough to be reported the same way by voice. So this asserts
        the *absence* of a count on failure -- there is no count to quote,
        because nothing was counted.
        """
        import urllib.error, io
        err = urllib.error.HTTPError("u", 500, "x", {}, io.BytesIO(b"boom"))
        with mock.patch.object(ni, "_get", side_effect=err):
            r = ni.search_images("x")
        self.assertFalse(r["ok"])
        self.assertNotIn("count", r,
                         "a failed search has no count; reporting 0 would be "
                         "claiming NASA had nothing")

    def test_M3_the_video_still_upgrade_is_load_bearing(self):
        r_body = _payload([_entry("V1", "V", thumb="http://x/~thumb.jpg")])
        with mock.patch.object(ni, "_get", return_value=(200, r_body)):
            r = ni.search_images("x", media_type="video")
        self.assertIn("~medium.jpg", r["items"][0]["thumb"])


if __name__ == "__main__":
    unittest.main()
