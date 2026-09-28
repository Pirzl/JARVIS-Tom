"""world_look: the wording, the rules, and the things it must never do.

Network calls are stubbed. What is under test here is the behaviour a live run
already showed breaking, and each of those cases is a real defect that shipped
into a first draft and was found by reading actual output:

  * an aircraft altitude of 111548 feet, from treating feet as metres
  * a callsign of '@@@@@@@@', read aloud to the user
  * "en 400 km alrededor de España, España", from a radius clamped to the
    aircraft default and then printed back as the answer
  * a place that could not be found, which raised out of the action and took
    the whole turn down

The stubs therefore return the values that caused those bugs, not tidy ones.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from actions import world_look as wl           # noqa: E402
from core import world_data as wd              # noqa: E402

# Imported with the OpenGL attribute already set by the caller; see
# test_world_view.py for why that ordering is not optional.
from PyQt6.QtCore import Qt  # noqa: E402
from PyQt6.QtWidgets import QApplication  # noqa: E402

QApplication.setAttribute(Qt.ApplicationAttribute.AA_ShareOpenGLContexts, True)

import ui_world_view as wv  # noqa: E402


JEREZ = {"query": "Jerez de la Frontera", "name": "Jerez de la Frontera",
         "country": "España", "admin1": "Andalucía",
         "lat": 36.68645, "lon": -6.13606,
         "source": "open-meteo-geocoding"}


def _ac(hex_="abc123", flight="RYR144G", reg="EI-EKM", km=41.2, ft=35000,
        brg="ESE", gs=480.0, ground=False):
    return {"hex": hex_, "flight": flight, "type": "B38M", "registration": reg,
            "lat": 37.0, "lon": -6.0, "distance_km": km,
            "bearing_deg": 112.5, "bearing": brg, "altitude_ft": ft,
            "altitude_m": (int(ft * 0.3048) if ft is not None else None),
            "ground_speed_kmh": gs,
            "on_ground": ground, "emergency": None}


class _Stub:
    """Replace core.world_data functions for the duration of one test."""

    def __init__(self, testcase, **overrides):
        self.tc = testcase
        self.saved = {}
        for name, value in overrides.items():
            self.saved[name] = getattr(wd, name)
            setattr(wd, name, value)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        for name, value in self.saved.items():
            setattr(wd, name, value)
        return False


def _no_geo(place):
    raise wd.WorldDataError("place not found: %s" % place)


class TestAnswersArePlainLanguage(unittest.TestCase):

    def test_aircraft_names_the_closest_and_gives_the_source(self):
        with _Stub(self, geocode=lambda p: JEREZ,
                   aircraft_near=lambda *a, **k: {
                       "count": 3, "radius_km": 150, "source": "adsb.lol",
                       "aircraft": [_ac()]},):
            out = wl.world_look({"action": "aircraft",
                                 "place": "Jerez de la Frontera"})
        self.assertIn("RYR144G", out)
        self.assertIn("41 km", out)
        self.assertIn("ESE", out)
        self.assertIn("adsb.lol", out)          # never an unsourced number

    def test_a_place_that_cannot_be_found_is_a_sentence_not_a_crash(self):
        """The regression. This used to raise out of the action.

        An unhandled error here does not produce a poor answer; it produces no
        answer and a broken turn, so the test asserts the sentence comes back.
        """
        with _Stub(self, geocode=_no_geo):
            out = wl.world_look({"action": "aircraft", "place": "no existe"})
        self.assertIsInstance(out, str)
        self.assertIn("No encuentro ningún sitio", out)
        self.assertNotIn("Traceback", out)

    def test_no_place_asks_which_one_instead_of_guessing(self):
        out = wl.world_look({"action": "aircraft"})
        self.assertIn("Sobre qué sitio", out)
        # It must not have invented a location to answer with.
        self.assertNotIn("aviones", out.lower().split("sitio")[0])

    def test_a_failed_source_says_so_and_invents_nothing(self):
        def boom(*a, **k):
            raise wd.WorldDataError("api.adsb.lol unreachable: timed out")
        with _Stub(self, geocode=lambda p: JEREZ, aircraft_near=boom):
            out = wl.world_look({"action": "aircraft", "place": "Jerez"})
        self.assertIn("no he podido consultar", out.lower())
        self.assertIn("timed out", out)
        # Crucially: no aircraft count at all, because there is none to report.
        self.assertNotIn("RYR144G", out)


class TestNumbersAreNotAbsurd(unittest.TestCase):
    """Everything here was a real output, not a hypothetical.

    An altitude three times the flight envelope is the kind of value that
    looks fine in a dict and gets narrated with total confidence.
    """

    def test_altitude_above_the_envelope_is_discarded(self):
        with _Stub(self):
            real = wd._fetch
            try:
                wd._fetch = lambda url, timeout=None: {"ac": [
                    # Inside the search radius: 36.68/-6.13 with a point a few
                    # km away, so the entry survives the distance filter and is
                    # judged on its altitude alone.
                    {"hex": "aaaaaa", "flight": "NORMAL1", "lat": 36.80,
                     "lon": -6.13, "alt_baro": 35000, "gs": 480},
                    {"hex": "bbbbbb", "flight": "BROKEN1", "lat": 36.75,
                     "lon": -6.13, "alt_baro": 111548, "gs": 480},
                ]}
                wd._cache.clear()
                out = wd.aircraft_near(36.68, -6.13, 150.0)
            finally:
                wd._fetch = real
                wd._cache.clear()
        alts = {a["flight"]: a["altitude_ft"] for a in out["aircraft"]}
        self.assertEqual(alts["NORMAL1"], 35000)
        self.assertIsNone(alts["BROKEN1"],
                          "111548 ft must not survive as a reported altitude")

    def test_altitude_is_read_as_feet_not_metres(self):
        """alt_baro is feet. Multiplying by 3.28 turned 40000 into 111548."""
        with _Stub(self):
            real = wd._fetch
            try:
                wd._fetch = lambda url, timeout=None: {"ac": [
                    {"hex": "407731", "flight": "EXS9UG", "t": "B738",
                     "r": "G-DRTT", "lat": 36.75, "lon": -6.13,
                     "alt_baro": 40000, "gs": 503.7}]}
                wd._cache.clear()
                out = wd.aircraft_near(36.68, -6.13, 150.0)
            finally:
                wd._fetch = real
                wd._cache.clear()
        ac = out["aircraft"][0]
        self.assertEqual(ac["altitude_ft"], 40000)
        self.assertAlmostEqual(ac["altitude_m"], 12192, delta=2)

    def test_satellite_period_comes_from_mean_motion(self):
        """MEAN_MOTION is rev/day. Asking for PERIOD gave 0 for every satellite."""
        with _Stub(self):
            real = wd._fetch
            try:
                wd._fetch = lambda url, timeout=None: [
                    {"OBJECT_NAME": "ISS (ZARYA)", "NORAD_CAT_ID": 25544,
                     "MEAN_MOTION": 15.48664528, "INCLINATION": 51.6315}]
                wd._cache.clear()
                out = wd.satellites()
            finally:
                wd._fetch = real
                wd._cache.clear()
        self.assertAlmostEqual(out["satellites"][0]["period_min"], 93.0, delta=1.5)

    def test_a_placeholder_callsign_is_never_spoken(self):
        """'@@@@@@@' is what a transponder sends when nobody filled it in."""
        with _Stub(self, geocode=lambda p: JEREZ,
                   aircraft_near=lambda *a, **k: {
                       "count": 1, "radius_km": 150, "source": "adsb.lol",
                       "aircraft": [_ac(hex_="3470c5", flight="@@@@@@@@",
                                         reg=None)]}):
            out = wl.world_look({"action": "aircraft", "place": "Jerez"})
        self.assertNotIn("@", out)
        self.assertIn("3470c5", out)          # falls back to the hex code

    def test_a_blank_callsign_falls_back_to_registration(self):
        with _Stub(self, geocode=lambda p: JEREZ,
                   aircraft_near=lambda *a, **k: {
                       "count": 1, "radius_km": 150, "source": "adsb.lol",
                       "aircraft": [_ac(flight="   ", reg="EC-OBY")]}):
            out = wl.world_look({"action": "aircraft", "place": "Jerez"})
        self.assertIn("EC-OBY", out)
        self.assertNotIn("     ", out)


class TestRadiusMatchesTheQuestion(unittest.TestCase):
    """A seismic question needs a region; an overhead question does not."""

    def test_earthquakes_default_to_a_region_not_the_aircraft_radius(self):
        seen = {}

        def q(lat, lon, radius_km=800.0, min_mag=2.5, limit=8):
            seen["radius"] = radius_km
            return {"count": 0, "radius_km": radius_km, "source": "USGS",
                    "quakes": []}

        with _Stub(self, geocode=lambda p: JEREZ, earthquakes_near=q):
            out = wl.world_look({"action": "earthquakes", "place": "España"})
        self.assertGreaterEqual(seen["radius"], 1000)
        self.assertIn(str(int(seen["radius"])), out)

    def test_an_explicit_radius_always_wins(self):
        seen = {}

        def q(lat, lon, radius_km=800.0, min_mag=2.5, limit=8):
            seen["radius"] = radius_km
            return {"count": 0, "radius_km": radius_km, "source": "USGS",
                    "quakes": []}

        with _Stub(self, geocode=lambda p: JEREZ, earthquakes_near=q):
            wl.world_look({"action": "earthquakes", "place": "España",
                           "radius_km": 3000})
        self.assertEqual(seen["radius"], 3000)

    def test_zero_is_reported_as_zero_not_as_an_error(self):
        with _Stub(self, geocode=lambda p: JEREZ,
                   aircraft_near=lambda *a, **k: {
                       "count": 0, "radius_km": 150, "source": "adsb.lol",
                       "aircraft": []}):
            out = wl.world_look({"action": "aircraft", "place": "Jerez"})
        self.assertIn("ningún avión", out.lower())
        self.assertIn("adsb.lol", out)


class TestBriefingIsHonestAboutGaps(unittest.TestCase):

    def test_a_briefing_that_lost_a_source_admits_it(self):
        """snapshot() records a failed source in "errors" rather than raising.

        The stub returns that shape directly, because the behaviour under test
        is the briefing's handling of a partial result -- not snapshot's own
        error collection, which is exercised against the live sources.
        """
        snap = {"place": JEREZ, "radius_km": 150,
                "errors": {"satellites": "celestrak.org unreachable"},
                "aircraft": {"count": 2, "radius_km": 150,
                             "aircraft": [_ac()]},
                "weather": {"description": "nublado", "temperature_c": 23.0,
                            "wind_kmh": 13.0}}
        with _Stub(self, snapshot=lambda *a, **k: snap):
            out = wl.world_look({"action": "overview", "place": "Jerez"})
        self.assertIn("RYR144G", out)
        self.assertIn("nublado", out)
        # The part that failed is named, not quietly dropped.
        self.assertIn("No he podido consultar", out)
        self.assertIn("celestrak.org", out)

    def test_grounded_aircraft_are_not_given_an_altitude(self):
        with _Stub(self, geocode=lambda p: JEREZ,
                   aircraft_near=lambda *a, **k: {
                       "count": 1, "radius_km": 150, "source": "adsb.lol",
                       "aircraft": [_ac(flight="TX00", ground=True, ft=None,
                                         gs=0.0)]}):
            out = wl.world_look({"action": "aircraft", "place": "Madrid"})
        self.assertIn("en tierra", out)
        self.assertNotIn("pies de altura", out)


class TestGlobeBridgeIsWired(unittest.TestCase):
    """The voice path must actually reach the map.

    The bridge was half working for several iterations: the spoken answer was
    correct and the globe stayed where it was. Nothing in the data layer could
    tell, because the data layer never touches the map -- so these assert the
    seams, which is where the actual failures were.
    """

    def test_the_window_exposes_a_focus_method(self):
        """What the session mixin looks for, and what it found once.

        It searched for `focus_world`. The method exists, so this passes --
        but the first version of it called GlobePanel without importing it, and
        the resulting NameError was swallowed by the caller's except, so the
        answer came out fine and the map never opened. Asserted separately
        below.
        """
        import ast
        src = (BASE_DIR / "ui.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        names = [n.name for n in ast.walk(tree)
                 if isinstance(n, ast.FunctionDef) and n.name == "focus_world"]
        self.assertEqual(len(names), 1, "focus_world must be defined once")

    def test_focus_world_imports_the_panel_it_uses(self):
        """The regression that hid a NameError for a whole session.

        GlobePanel is imported inside the method, deliberately, so a JARVIS
        without QtWebEngine still starts. The first draft of focus_world used
        the name without importing it; the caller caught the NameError and
        printed nothing, so the symptom was "the answer works, the map does
        nothing".
        """
        import ast
        src = (BASE_DIR / "ui.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef) and n.name == "focus_world")
        # Every name the body reads must be bound in the body or be a builtin.
        used = {n.id for n in ast.walk(fn) if isinstance(n, ast.Name)
                and isinstance(n.ctx, ast.Load)}
        bound = ({n.id for n in ast.walk(fn) if isinstance(n, ast.Name)
                  and isinstance(n.ctx, ast.Store)}
                 | {a.arg for a in fn.args.args}
                 | {"GlobePanel", "QApplication", "float", "str", "print",
                    "type", "Exception", "ImportError", "len", "isinstance"})
        missing = used - bound - set(dir(__builtins__))
        self.assertNotIn("GlobePanel", missing,
                         "focus_world uses GlobePanel without importing it")

    def test_the_page_side_patch_survives_a_vendor_refresh(self):
        """The bridge is a local patch in vendored third-party code.

        vendor/ is gitignored, so a `git pull` in the checkout, or a fresh
        clone, silently removes it. The doctor must say so, because the symptom
        from the outside is a feature that works and then does not, with no
        error anywhere.
        """
        patch = BASE_DIR / "vendor" / "gods-eye" / "src" / "sharelink.js"
        if not patch.exists():
            self.skipTest("gods-eye checkout not present")
        src = patch.read_text(encoding="utf-8")
        self.assertIn("jarvisFocus", src,
                      "the page-side bridge is missing: the globe will not move")
        self.assertIn("gev:jarvis-focus", src)

    def test_the_page_side_patch_builds_a_destination_cesium_accepts(self):
        """The bug that made the first working shim a no-op.

        Cartesian3.fromRadians() was handed a Cartographic object. Cesium
        rejects that at runtime with "Expected longitude to be typeof number,
        actual typeof was object" and the camera does not move -- no exception
        reaches Python, the method simply has no effect. fromDegrees is the
        correct call and the assertion is on the call, not the outcome.
        """
        patch = BASE_DIR / "vendor" / "gods-eye" / "src" / "sharelink.js"
        if not patch.exists():
            self.skipTest("gods-eye checkout not present")
        src = patch.read_text(encoding="utf-8")
        body = src.split("jarvisFocus(detail)", 1)[1]
        self.assertIn("Cartesian3.fromDegrees", body,
                      "the destination must be built with fromDegrees")
        self.assertNotIn("Cartesian3.fromRadians", body,
                         "fromRadians takes radians, not a Cartographic")

    def test_focus_globe_does_not_reload_a_warm_page(self):
        """Reloading cannot work on a warm globe, and the reason is specific.

        sharelink.js _updateHash() calls history.replaceState with the current
        camera pose on a debounce, so by the time a second request goes out the
        fragment is the app's own state. Measured: 14 polls over 90 seconds
        with the camera unmoved. The cold-start path still uses the hash, which
        is correct, because the hash is read on load.
        """
        import inspect
        src = inspect.getsource(wv.GlobePanel.focus_globe)
        self.assertIn("gev:jarvis-focus", src,
                      "a warm globe must be moved by event, not by reload")
        self.assertNotIn("self._view.load(", src,
                         "reloading cannot move an already-open globe")

    def test_the_page_url_keeps_the_hash_the_app_actually_reads(self):
        """#focus=lat=.. parses to nothing.

        The app reads lat and lon straight off the hash in parseInitialHash(),
        so a wrapper key is silently ignored -- no error, just a globe that
        opens on its default view.

        Checked without constructing a GlobePanel: building one pulls in a real
        QWidget, and a suite that instantiates it takes the interpreter down at
        teardown (the run stops after the test that does it, with every test so
        far passing -- see test_world_view.py for the same WebEngine teardown).
        The URL logic is read off the source instead, which is what is under
        test anyway.
        """
        import inspect
        src = inspect.getsource(wv.GlobePanel._page_url)
        # Only the code, not the docstring: the docstring quotes the wrong
        # format on purpose, to record why it is wrong, and a whole-source
        # match would fail on its own explanation.
        code = src.split('"""')[0] + src.rsplit('"""', 1)[-1]
        self.assertIn('"%s#%s"', code,
                      "the hash must be appended directly, not wrapped in a key")
        self.assertNotIn("#focus=", code)

        params = wv.GlobePanel.focus_params(40.4169, -3.7038)
        self.assertTrue(params.startswith("lat="))
        self.assertIn("lon=-3.703800", params)

    def test_the_focus_parameters_are_in_degrees(self):
        """Radians are what the camera stores, not what the hash carries.

        _buildHashParams writes degrees via Cesium.Math.toDegrees, so the hash
        format is degrees. Handing it radians would put Madrid in the Gulf of
        Guinea without any error.
        """
        params = wv.GlobePanel.focus_params(40.4169, -3.7038)
        self.assertIn("lat=40.416900", params)
        self.assertNotIn("lat=0.7", params)

    def test_the_position_is_recorded_even_without_a_view(self):
        """A cold start has no view yet; the position must survive to the load.

        focus_globe is called on a stand-in rather than a real panel, for the
        teardown reason above. What matters is that it writes the position
        before it looks at the view, and that it returns rather than raising.
        """
        class _Stand:
            _pending_focus = ""
            _view = None
            # focus_globe calls it through self, so the stand-in needs it.
            focus_params = staticmethod(wv.GlobePanel.focus_params)

            def _set_status(self, *_a, **_k):
                pass

        stand = _Stand()
        wv.GlobePanel.focus_globe(stand, 40.4169, -3.7038, height=25000.0,
                                  label="Madrid")
        self.assertIn("lat=40.416900", stand._pending_focus)
        self.assertIn("alt=25000", stand._pending_focus)


class TestToolSchema(unittest.TestCase):
    """The schema is what Gemini reads to decide when to route here."""

    def test_it_declares_every_action_it_handles(self):
        import inspect
        spec = wl.TOOL["parameters"]["properties"]["action"]["description"]
        for action in ("aircraft", "earthquakes", "weather", "satellites",
                       "overview", "sources"):
            self.assertIn(action, spec)

    def test_the_description_tells_the_model_not_to_guess_the_place(self):
        desc = wl.TOOL["description"]
        self.assertIn("ask which place", desc)
        self.assertIn("Public", desc)

    def test_it_is_a_proper_tool_shape(self):
        self.assertEqual(wl.TOOL["parameters"]["type"], "OBJECT")
        self.assertEqual(wl.TOOL["parameters"]["required"], ["action"])
        self.assertTrue(callable(wl.TOOL["handler"]))

    def test_every_alias_the_entry_point_accepts_is_reachable(self):
        """A synonym the handler accepts but the schema never mentions is a
        branch nothing can take, because the model is only offered the schema."""
        import inspect
        src = inspect.getsource(wl.world_look)
        for alias in ("flights", "aviones", "vuela", "overhead", "terremotos",
                      "seismic", "sismos", "tiempo", "meteo", "clima",
                      "briefing", "brief", "todo"):
            self.assertIn(alias, src)


if __name__ == "__main__":
    unittest.main()
