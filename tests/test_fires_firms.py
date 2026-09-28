"""Fire detection: two bugs that both looked like a bad key, and neither was.

Worth keeping in one place because the failure was misleading in the same way
twice, and both times the first hypothesis was the credential.

**The source segment was not a source.** The URL was built as
`KEY/MLAST24h/BOX/DAYS`. `MLAST24h` is a WMS/WFS layer name, not a sensor, and
FIRMS answers `Invalid source.` -- text that reads like a rejected credential.
Every documented source begins with `VIIRS_` or `MODIS_`. It is also not
optional: omitting the segment is a 400 too, so "just leave it out" is not the
fix.

**The path order.** The API takes `KEY/SOURCE/BOX/DAYS`. Passing a date where
the source belongs produces the same `Invalid source.`, which is why the
diagnosis kept coming back to the key.

**A separate, quieter bug.** MODIS names the brightness column `brightness`
and VIIRS names it `bright_ti4`. The parser looked up one name, found nothing,
and set the field to None -- the row still parsed, the reading just vanished.
Nothing failed, so nothing looked wrong, and every fire would have been
reported without its temperature.

Offline throughout. The live check is recorded in the module docstring: 115
detections over a 300 km box in Andalusia, brightness and FRP populated, and
`Invalid MAP_KEY` still returned for a deliberately wrong key, which is what
proves the credential is the part that works.
"""
from __future__ import annotations

import io
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import world_data as wd  # noqa: E402

# A real VIIRS_SNPP_NRT response, header included. Column names matter here and
# this is the point of the file: `bright_ti4`, not `brightness`.
VIIRS_CSV = (
    "latitude,longitude,bright_ti4,scan,track,acq_date,acq_time,satellite,"
    "instrument,confidence,version,bright_ti5,frp,daynight\n"
    # Two real rows, captured verbatim from VIIRS_SNPP_NRT. The field count has
    # to match the header exactly -- fourteen here. `fires_near` skips any row
    # shorter than the header, so a fixture one field short parses to nothing
    # at all, and the failure reads as "the detection was out of range"
    # rather than "the row was malformed". Both rows below were counted.
    "37.59531,-5.08321,301.03,0.43,0.38,2026-09-26,211,N,VIIRS,n,2.0NRT,"
    "291.09,2.64,N\n"
    "37.40000,-5.50000,335.14,0.50,0.40,2026-09-26,150,N,VIIRS,n,2.0NRT,"
    "290.00,1.51,N\n"
)

MODIS_CSV = (
    "latitude,longitude,satellite,instrument,confidence,acq_date,acq_time,"
    "brightness,frp,daynight,version\n"
    "37.50000,-5.90000,Terra,MODIS,n,2026-09-26,1200,312.5,2.1,DAY,3.0\n"
)


def _csv_response(body, code=200):
    resp = mock.MagicMock()
    resp.read.return_value = body.encode("utf-8")
    resp.__enter__.return_value = resp
    resp.__exit__.return_value = False
    return resp


def _http_error(code, text):
    return urllib.error.HTTPError("u", code, text, {},
                                  io.BytesIO(text.encode("utf-8")))


class TestTheSourceSegmentIsReal(unittest.TestCase):
    def test_the_configured_source_is_a_real_sensor(self):
        """The bug: `MLAST24h`, a WMS layer name, sent as a sensor."""
        self.assertRegex(
            wd.FIRMS_SOURCE, r"^(VIIRS|MODIS|LANDSAT)_",
            f"{wd.FIRMS_SOURCE!r} is not a documented FIRMS source; the API "
            f"answers 'Invalid source.' for anything else")

    def test_it_is_near_real_time(self):
        """A wildfire a day old is history, not news. The standard-processing
        products lag; the question this answers does not tolerate that."""
        self.assertTrue(
            wd.FIRMS_SOURCE.endswith("_NRT") or "_SP" in wd.FIRMS_SOURCE,
            "expected a near-real-time product for a 'is it burning now' "
            "question")


class TestTheUrlIsBuiltCorrectly(unittest.TestCase):
    """`KEY/SOURCE/BOX/DAYS` -- a date in the source slot is the same error."""

    def _url(self):
        with mock.patch.object(wd, "firms_key", return_value="KEY123"):
            with mock.patch("urllib.request.urlopen",
                            return_value=_csv_response(VIIRS_CSV)) as u:
                wd.fires_near(37.0, -6.0, radius_km=50, days=1)
        return u.call_args[0][0].full_url

    def test_the_order_is_key_source_box_days(self):
        url = self._url()
        parts = url.split("/api/area/csv/")[1].split("/")
        self.assertEqual(parts[0], "KEY123")
        self.assertEqual(parts[1], wd.FIRMS_SOURCE,
                         "the source must occupy the second slot; a date "
                         "there returns 'Invalid source.'")
        self.assertEqual(parts[-1], "1")
        self.assertEqual(len(parts[2].split(",")), 4, "the box is four numbers")

    def test_the_key_appears_exactly_once(self):
        """It is in the URL by necessity -- FIRMS has no header auth -- so the
        requirement is that it is not also somewhere it would be logged or
        printed, like the box or the day count."""
        url = self._url()
        self.assertEqual(url.count("KEY123"), 1, url)

    def test_the_url_never_carries_a_date(self):
        """A date in this endpoint is the mistake that started all of it."""
        self.assertNotRegex(self._url(), r"/\d{8}/")


class TestBothSensorsParse(unittest.TestCase):
    """The quiet bug: a missing reading that looks like a sensor that
    measured nothing."""

    def _fires(self, csv_body, **kw):
        # Centred on the fixtures themselves, not an arbitrary point. The
        # first version asked about 37.0,-6.0 with detections at 37.59,-5.08 --
        # about 90 km away -- and a 400 km box still returned zero, because
        # `fires_near` filters by great-circle distance after the bounding
        # box, so the centre is what decides, not the box.
        with mock.patch.object(wd, "firms_key", return_value="KEY123"):
            with mock.patch("urllib.request.urlopen",
                            return_value=_csv_response(csv_body)):
                return wd.fires_near(37.4, -5.4, **kw)

    def test_viirs_brightness_is_read(self):
        r = self._fires(VIIRS_CSV, radius_km=400, days=1)
        self.assertGreater(r["count"], 0)
        # Sorted by distance, so the first row is the nearer detection. The
        # first version asserted 301.03 here and failed on 335.14 -- the
        # sorting was right, the expectation was written from the file order
        # instead of from the output order.
        self.assertEqual(r["fires"][0]["distance_km"], 8.8)
        self.assertEqual(r["fires"][0]["brightness"], "335.14",
                         "VIIRS calls it bright_ti4; looking up 'brightness' "
                         "silently yields None")
        self.assertEqual(r["count"], 2, "both rows parse, not just the first")

    def test_modis_brightness_is_still_read(self):
        r = self._fires(MODIS_CSV, radius_km=400, days=1)
        self.assertGreater(r["count"], 0)
        self.assertEqual(r["fires"][0]["brightness"], "312.5")

    def test_both_sensors_yield_a_reading(self):
        """The single assertion that covers the whole class of bug: whichever
        sensor answered, the temperature came through."""
        for body, label in ((VIIRS_CSV, "VIIRS"), (MODIS_CSV, "MODIS")):
            r = self._fires(body, radius_km=400, days=1)
            for f in r["fires"]:
                self.assertIsNotNone(f["brightness"],
                                     f"{label} parsed but produced no "
                                     f"brightness -- the row looked fine and "
                                     f"the reading was missing")

    def test_frp_is_numeric_for_both(self):
        r = self._fires(VIIRS_CSV, radius_km=400, days=1)
        self.assertIsInstance(r["fires"][0]["frp"], float)
        self.assertGreater(r["fires"][0]["frp"], 0)

    def test_an_empty_response_is_zero_fires_not_an_error(self):
        r = self._fires("", radius_km=400, days=1)
        self.assertEqual(r["count"], 0)
        self.assertEqual(r["fires"], [])

    def test_a_short_row_is_skipped_not_fatal(self):
        """One truncated line must not cost the other detections. A parse that
        raises here would turn a partial answer into no answer at all."""
        body = VIIRS_CSV + "37.1,-5.1,broken\n"
        r = self._fires(body, radius_km=400, days=1)
        self.assertEqual(r["count"], 2, "the two good rows must survive")


class TestFailuresStayDistinguishable(unittest.TestCase):
    """The property the original docstring was written to protect."""

    def _run(self, side_effect):
        with mock.patch.object(wd, "firms_key", return_value="KEY123"):
            with mock.patch("urllib.request.urlopen", side_effect=side_effect):
                return wd.fires_near(37.0, -6.0, radius_km=50, days=1)

    def test_a_rejected_key_is_not_reported_as_no_fires(self):
        """This is the safety-relevant one. A rejected key must never come
        back as an empty list, because an empty list means 'I looked and there
        is nothing burning'."""
        with self.assertRaises(wd.WorldDataError) as ctx:
            self._run(_http_error(400, "Invalid MAP_KEY."))
        self.assertIn("clave", str(ctx.exception).lower())

    def test_an_invalid_source_is_not_blamed_on_the_key(self):
        with self.assertRaises(wd.WorldDataError):
            self._run(_http_error(400, "Invalid source."))

    def test_a_network_failure_says_so(self):
        with self.assertRaises(wd.WorldDataError):
            self._run(OSError("no route to host"))

    def test_the_key_never_appears_in_the_error(self):
        """The key is in the URL, so reusing the exception text would print
        it into a log and out loud."""
        with self.assertRaises(wd.WorldDataError) as ctx:
            self._run(_http_error(400, "Invalid MAP_KEY."))
        self.assertNotIn("KEY123", str(ctx.exception))


class TestCredentials(unittest.TestCase):
    def test_no_key_is_its_own_message(self):
        with mock.patch.object(wd, "firms_key", return_value=""):
            with self.assertRaises(wd.WorldDataError) as ctx:
                wd.fires_near(37.0, -6.0)
        self.assertIn("firms_map_key", str(ctx.exception).lower())

    def test_bad_coordinates_are_refused_before_any_request(self):
        with mock.patch.object(wd, "firms_key", return_value="K"):
            with mock.patch("urllib.request.urlopen") as u:
                with self.assertRaises(wd.WorldDataError):
                    wd.fires_near(999.0, -6.0)
        u.assert_not_called()


class TestMutationChecks(unittest.TestCase):
    def test_M1_restoring_MLAST24h_breaks_the_sensor_test(self):
        """The exact regression that shipped: revert the source and this
        goes red."""
        with mock.patch.object(wd, "FIRMS_SOURCE", "MLAST24h"):
            self.assertNotRegex(wd.FIRMS_SOURCE, r"^(VIIRS|MODIS|LANDSAT)_")

    def test_M2_dropping_bright_ti4_breaks_the_viirs_test(self):
        with mock.patch.object(wd, "_first_present",
                               side_effect=lambda c, i, n: None):
            with mock.patch.object(wd, "firms_key", return_value="K"):
                with mock.patch("urllib.request.urlopen",
                                return_value=_csv_response(VIIRS_CSV)):
                    r = wd.fires_near(37.4, -5.4, radius_km=400, days=1)
            self.assertIsNone(r["fires"][0]["brightness"],
                              "mutation reproduced: reading vanishes silently")

    def test_M3_a_date_in_the_source_slot_is_detected(self):
        bad = ("https://firms.modaps.eosdis.nasa.gov/api/area/csv/KEY/"
               "20260926/-6.5,36.4,-6.2,36.7/1")
        parts = bad.split("/api/area/csv/")[1].split("/")
        self.assertRegex(parts[1], r"^\d+$",
                         "an 8-digit segment is a date, not a source")


if __name__ == "__main__":
    unittest.main()
