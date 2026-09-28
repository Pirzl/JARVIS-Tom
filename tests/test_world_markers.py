"""Phase 1D: the map shows what the voice said.

The rule under test is one sentence long and easy to state, hard to keep:
**what is drawn must be what was said.** Not a similar set, not a sample, not
a decorative cluster -- the same facts, in the same order, with the same count.

It failed once for real while this was being built. `aircraft_near` truncated
its list to twelve while `count` reported every aircraft in the radius, which
is right for a sentence ("there are 19, the closest is X") and wrong for a map
that could only ever draw twelve. A voice saying 19 was followed by a picture
showing 12, with nothing on screen to say the picture was a subset. The
default limit is now large enough to cover the count, and a test holds it
there.

Also here: coordinates that survive bad input, markers that are never invented
when a source failed, and the guarantee that a broken map cannot damage a
spoken answer -- the answer is the product, the dots are an extra.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from actions import world_look as wl          # noqa: E402
from core import world_data as wd             # noqa: E402
from core import world_markers as wm          # noqa: E402

JEREZ = {"query": "Jerez de la Frontera", "name": "Jerez de la Frontera",
         "country": "España", "admin1": "Andalucía",
         "lat": 36.68645, "lon": -6.13606, "source": "x"}
TOKIO = {"query": "Tokyo", "name": "Tokyo", "country": "Japan",
         "admin1": "Tokyo", "lat": 35.6895, "lon": 139.6917, "source": "x"}


def _craft(i, lat=36.70, lon=-6.10, dist=None):
    return {"lat": lat, "lon": lon, "flight": "TST%d" % i,
            "registration": "EC-%d" % i, "hex": "abc%03d" % i,
            "altitude_ft": 30000, "ground_speed_kmh": 450.0,
            "distance_km": float(i if dist is None else dist)}


class TestTheMapMatchesTheVoice(unittest.TestCase):
    """The core promise of 1D."""

    def test_every_aircraft_the_voice_counts_is_drawn(self):
        """The bug this whole module exists to prevent.

        `count` is what the sentence says; the list is what the map can draw.
        A limit lower than the count means the map silently shows a subset, and
        nothing on screen reveals it.
        """
        data = {"count": 19, "aircraft": [_craft(i) for i in range(19)],
                "source": "adsb.lol", "queried_at": "12:10"}
        payload = wm.payload("aircraft", data, JEREZ)
        drawn = [m for m in payload["markers"] if m["kind"] == "aircraft"]
        self.assertEqual(len(drawn), 19)
        self.assertEqual(payload["count"], 20)      # + the centre anchor

    def test_the_aircraft_limit_covers_the_count_it_reports(self):
        """Held at the source, not only in the marker builder.

        Raising MAX_MARKERS alone would not help: the truncation happens in
        world_data, before any marker exists. The default limit has to exceed
        the number of aircraft a 150 km query can return, or the sentence and
        the picture drift apart again the first time the sky is busy.
        """
        import inspect
        sig = inspect.signature(wd.aircraft_near)
        self.assertGreaterEqual(
            sig.parameters["limit"].default, 60,
            "the list must be able to hold a realistic count, not a sample")
        data = {"count": 50, "aircraft": [_craft(i) for i in range(50)]}
        drawn = [m for m in wm.payload("aircraft", data, JEREZ)["markers"]
                 if m["kind"] == "aircraft"]
        self.assertEqual(len(drawn), 50)

    def test_the_centre_is_marked_and_comes_first(self):
        data = {"count": 1, "aircraft": [_craft(0)], "source": "adsb.lol"}
        payload = wm.payload("aircraft", data, JEREZ)
        first = payload["markers"][0]
        self.assertEqual(first["kind"], "place")
        self.assertEqual(first["label"], JEREZ["name"])
        self.assertAlmostEqual(first["lat"], JEREZ["lat"], places=4)

    def test_the_closest_aircraft_is_the_one_the_map_leads_with(self):
        data = {"count": 2, "aircraft": [_craft(0, dist=12.0),
                                          _craft(1, dist=88.0)]}
        drawn = [m for m in wm.payload("aircraft", data, JEREZ)["markers"]
                 if m["kind"] == "aircraft"]
        # world_data returns nearest-first and that order is preserved, so the
        # dot nearest the centre is the one the sentence names first.
        self.assertIn("TST0", drawn[0]["label"])

    def test_every_marker_carries_its_source_and_time(self):
        """A dot with no time on it reads as a current fact indefinitely."""
        data = {"count": 1, "aircraft": [_craft(0)], "source": "adsb.lol",
                "queried_at": "14:32"}
        payload = wm.payload("aircraft", data, JEREZ)
        self.assertEqual(payload["source"], "adsb.lol")
        # camelCase on the way to the page, snake_case inside Python: the JS
        # side is the consumer, and mixing conventions in one payload is how a
        # timestamp quietly arrives empty.
        self.assertEqual(payload["queriedAt"], "14:32")
        self.assertNotIn("queried_at", payload)
        # One stamp for the whole set, not per marker: they were all read at
        # the same moment, and a per-marker stamp would imply they were not.
        self.assertTrue(all("queriedAt" not in m for m in payload["markers"]))

    def test_each_kind_gets_its_own_colour(self):
        colours = {
            kind: wm._marker(36.0, -6.0, kind, "x")["colour"]
            for kind in ("aircraft", "earthquake", "fire", "place")
        }
        self.assertEqual(len(set(colours.values())), len(colours),
                         "two kinds sharing a colour is indistinguishable")


class TestAMarkerIsNeverInvented(unittest.TestCase):
    """A dot the sources did not report is a lie with a position."""

    def test_a_failed_lookup_draws_nothing_at_all(self):
        # Not even the centre. A green dot on a map says "look here" and with
        # the lookup failed there is nothing to look at.
        self.assertEqual(wm.payload("fires", {}, JEREZ)["markers"], [])

    def test_a_genuine_zero_still_marks_where_it_looked(self):
        # Different from the case above: the query succeeded and found none.
        # That is information, and the centre is the honest place to show it.
        data = {"count": 0, "quakes": [], "source": "USGS"}
        payload = wm.payload("earthquakes", data, TOKIO)
        self.assertEqual(len(payload["markers"]), 1)
        self.assertEqual(payload["markers"][0]["kind"], "place")

    def test_an_unknown_kind_draws_nothing(self):
        self.assertEqual(wm.payload("invasiones", {"x": 1}, JEREZ)["markers"],
                         [])

    def test_hostile_coordinates_never_reach_the_map(self):
        """NaN is the dangerous one: it passes every ordinary comparison.

        A plain `if -90 <= lat <= 90` accepts NaN, because every comparison
        with NaN is false, so the range check yields False and the guard
        passes. Cesium then throws deep inside a render, long from the value
        that caused it.
        """
        bad = [float("nan"), float("inf"), float("-inf"), None, "hola", {}, []]
        for value in bad:
            with self.subTest(value=value):
                self.assertIsNone(wm._c(value, 0.0))
                self.assertIsNone(wm._c(0.0, value))

    def test_out_of_range_is_clamped_rather_than_dropped(self):
        """A provider with a bad number gets a corner, not a hole.

        Dropping it silently would hide a data problem; clamping keeps the
        marker and makes the wrongness visible at the edge of the world.
        """
        self.assertEqual(wm._c(999.0, 999.0), (90.0, 180.0))
        self.assertEqual(wm._c(-999.0, -999.0), (-90.0, -180.0))

    def test_the_marker_cap_is_enforced(self):
        data = {"count": 5000, "aircraft": [_craft(i) for i in range(5000)]}
        payload = wm.payload("aircraft", data, JEREZ)
        self.assertLessEqual(len(payload["markers"]), wm.MAX_MARKERS + 1)


class TestTheSpokenAnswerSurvivesEverything(unittest.TestCase):
    """The voice answer is the product. The map is an extra."""

    def test_a_broken_map_cannot_change_the_answer(self):
        def boom(payload):
            raise RuntimeError("el mapa esta roto")

        original = wl._show_markers
        wl.bind_session(open_globe=lambda *a: None, show_markers=boom)
        try:
            data = {"count": 1, "aircraft": [_craft(0)], "source": "adsb.lol"}
            wl._last_results["aircraft"] = data
            answer = wl._with_markers("La respuesta completa.", "aircraft",
                                      JEREZ, False)
        finally:
            wl.bind_session(open_globe=original, show_markers=None)
        self.assertEqual(answer, "La respuesta completa.")

    def test_an_unbuildable_payload_cannot_change_the_answer(self):
        original = wl._show_markers
        seen = []

        def capture(payload):
            seen.append(payload)

        wl.bind_session(open_globe=lambda *a: None, show_markers=capture)
        try:
            # No registered results at all: the builder has nothing to work
            # from, and must not invent anything or raise.
            wl._last_results.pop("earthquakes", None)
            answer = wl._with_markers("Terremotos: ninguno.", "earthquakes",
                                      TOKIO, True)
        finally:
            wl.bind_session(open_globe=original, show_markers=None)
        self.assertIn("ninguno", answer)
        self.assertEqual(seen[0]["markers"], [])

    def test_without_a_globe_the_answer_is_untouched(self):
        original = wl._show_markers
        wl.bind_session(open_globe=lambda *a: None, show_markers=None)
        try:
            data = {"count": 1, "aircraft": [_craft(0)], "source": "adsb.lol"}
            wl._last_results["aircraft"] = data
            answer = wl._with_markers("Sin cambios.", "aircraft", JEREZ, False)
        finally:
            wl.bind_session(open_globe=original, show_markers=None)
        self.assertEqual(answer, "Sin cambios.")


class TestTheBridgeSendsOneThing(unittest.TestCase):
    """What crosses to the page, and what must not."""

    def test_the_event_name_is_the_one_the_page_listens_for(self):
        """The page's listener and this dispatch have to agree exactly.

        They are two files that are not in the same repository -- one is
        ignored by git -- so nothing checks this but a test that reads both.
        """
        share = BASE_DIR / "vendor" / "gods-eye" / "src" / "sharelink.js"
        if not share.exists():
            self.skipTest("GEV checkout not present")
        js = share.read_text(encoding="utf-8")
        self.assertIn("gev:jarvis-markers", js,
                      "the page must listen for this event")
        self.assertIn("jarvisMarkers", js,
                      "the page must implement the method it dispatches to")
        self.assertIn("window.gevShareLink", js,
                      "the manager must be reachable for the event to find it")
        panel = (BASE_DIR / "ui_world_view.py").read_text(encoding="utf-8")
        self.assertIn("gev:jarvis-markers", panel,
                      "Python must dispatch the event the page listens for")

    def test_the_page_does_not_reach_for_a_global_cesium(self):
        """A module import is not on window, and injected script has no scope.

        The first version of the page-side drawing used a global Cesium and the
        bridge reported "Cesium is not defined", which looked like the drawing
        failing. It is the drawing's own module import that is in scope here;
        anything reached through `window` is a different thing.
        """
        share = BASE_DIR / "vendor" / "gods-eye" / "src" / "sharelink.js"
        if not share.exists():
            self.skipTest("GEV checkout not present")
        js = share.read_text(encoding="utf-8")
        body = js[js.find("jarvisMarkers(detail)"):js.find("_updateHash() {")]
        self.assertNotIn("window.Cesium", body,
                         "window.Cesium does not exist in a bundled ES module")

    def test_markers_use_the_viewer_entity_collection(self):
        """viewer.entities always exists; a DataSource needs a class lookup.

        The first implementation built a CustomDataSource reached through
        `viewer.dataSources.constructor`, and in the running app that was
        undefined, so the layer silently never existed and every marker was
        lost while the call still reported success.
        """
        share = BASE_DIR / "vendor" / "gods-eye" / "src" / "sharelink.js"
        if not share.exists():
            self.skipTest("GEV checkout not present")
        js = share.read_text(encoding="utf-8")
        body = js[js.find("jarvisMarkers(detail)"):js.find("_updateHash() {")]
        self.assertIn("viewer.entities.add", body)
        self.assertNotIn("dataSources", body,
                         "the DataSource path was the one that failed silently")


class TestSatellitesAreNotDrawnWhereTheyAreNot(unittest.TestCase):
    """CelesTrak gives an orbit, not a position."""

    def test_no_satellite_markers_are_faked_from_an_orbit(self):
        """An orbit is not a position, and drawing one would be a guess.

        CelesTrak's active group carries a mean motion and an inclination, not
        a latitude and longitude. TLE propagation would give one, and was left
        out deliberately. So the globe shows the place asked about -- the query
        succeeded -- and claims nothing about where the satellites are.
        """
        data = {"count": 22, "satellites": [{"name": "ISS", "period_min": 93.0,
                                             "inclination_deg": 51.6}]}
        payload = wm.payload("satellites", data, TOKIO)
        drawn = [m for m in payload["markers"] if m["kind"] == "satellite"]
        self.assertEqual(drawn, [],
                         "an orbit is not somewhere; drawing one would be a "
                         "guess presented as a position")
        self.assertEqual([m["kind"] for m in payload["markers"]], ["place"])

    def test_the_answer_still_lists_them(self):
        """No map is not no data. The voice answer is unaffected."""
        self.assertIn("satellites", wl.TOOL["description"])


if __name__ == "__main__":
    unittest.main()
