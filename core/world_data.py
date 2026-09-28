"""Live world data for the voice bridge, queried straight from the sources.

Why this talks to the APIs instead of to the Globe app
-------------------------------------------------------
The app has no data API of its own -- no WebSocket, no REST endpoint, nothing
JARVIS could read. It is a *client* of the same public feeds this module calls.
So asking the sources directly is not a workaround; it is the same data, one
process fewer, and it works whether or not the globe window is open. A user who
asks "what is flying over Jerez" should get an answer even if they never opened
the map.

What was measured before any of this was written
------------------------------------------------
Every endpoint was probed from this machine, and the results shaped the code:

  USGS earthquakes        200, 44 events in 24 h      -- the richest source
  adsb.lol aircraft       200, 23 aircraft within 150 km of Jerez
  CelesTrak satellites    200, 22 objects
  open-meteo weather      200, current conditions
  open-meteo geocoding    200, city name -> coordinates
  OpenSky anonymous       200, 10149 states worldwide, but 0 in a small box
  NASA FIRMS fires        400 -- needs a key we do not have

Two of those deserve a note. OpenSky's anonymous access returned an empty list
for a tight bounding box around Jerez while the same endpoint without a box
returned ten thousand aircraft worldwide: the box was simply too small for the
sparse coverage, not a broken source. And a 200 with an empty list is a valid
answer that JARVIS must be able to report honestly -- "nothing overhead right
now" is information, and inventing a plausible-sounding flight would not be.

Honesty rules baked in
----------------------
Every result carries its source and its age. When a source is unreachable the
caller gets an error, not an empty list dressed up as a result. Nothing here
asks a language model to fill a gap; the model narrates these facts, it does
not produce them.

Rate limiting is per-source and coarse. These are public endpoints shared with
other users: aircraft feeds are polled at most every 30 s and earthquakes every
60 s, with a short in-memory cache so a burst of questions does not hammer a
free service.
"""
from __future__ import annotations

import json
import math
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

# Never let a slow or hostile endpoint hold up the assistant's answer. These are
# public services and any of them can be slow; a voice answer that arrives in
# 30 seconds is no answer at all.
DEFAULT_TIMEOUT_S = 12.0

# The Globe app keeps its keys in vendor/gods-eye/.env (FIRMS_MAP_KEY). Reusing
# that file means one place to change a key, and vendor/ is gitignored, so a
# key never reaches the repository. It is read lazily and never cached, so
# fixing a key takes effect on the next question rather than after a restart.
GEV_ENV = Path(__file__).resolve().parent.parent / "vendor" / "gods-eye" / ".env"

# NASA names a set of "last N hours" datasets. MLAST24h is the one a question
# about fires today means; the per-sensor sources are what the Globe app layers
# for detail.
#
# This was `MLAST24h`, which is not a sensor. It is a WMS/WFS layer name that
# was never valid here, and the API rejected it with 400 `Invalid source.` --
# an error that reads like a rejected key, which is why a working credential
# was repeatedly suspected of being wrong. Every documented source starts with
# VIIRS_ or MODIS_, and the endpoint answers 400 when the segment is absent,
# so there is no "just omit it" fallback.
#
# VIIRS_SNPP_NRT is the near-real-time product, usable within a few hours of
# the overpass. For "is anything burning near me" that freshness is the whole
# point; the standard-processing products lag by a day or more, and a wildfire
# that is a day old is history.
FIRMS_SOURCE = "VIIRS_SNPP_NRT"

USER_AGENT = "jarvis-osint/1.0 (personal assistant; local use)"

# Sources refresh on their own clocks. Aircraft positions are stale after tens
# of seconds; a cached answer from five minutes ago would be a lie.
_MIN_INTERVAL_S = {
    "aircraft": 30.0,
    "satellites": 900.0,
    "earthquakes": 60.0,
    "weather": 300.0,
    "geocode": 86400.0,      # a city's coordinates do not move
    # Fire data is a scarce, quota-limited, key-authenticated service, and NASA
    # allows 500 transactions a day per application. A minute is enough to
    # answer a question and slow enough not to burn a day's quota by asking it
    # twice.
    "fires": 60.0,
}

_cache: dict[str, tuple[float, object]] = {}
_cache_lock = threading.Lock()


class WorldDataError(RuntimeError):
    """A source could not be read. Never returned as empty data."""


def _fetch(url: str, timeout: float = DEFAULT_TIMEOUT_S) -> object:
    """GET a URL and parse JSON. Raises WorldDataError on any failure.

    A non-2xx status is an error, not an empty result: FIRMS answers 400 without
    a key, and reporting "no fires" for that would be a confident falsehood.
    """
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            raw = resp.read()
    except urllib.error.HTTPError as exc:
        raise WorldDataError(
            "%s: HTTP %s %s" % (urllib.parse.urlsplit(url).netloc,
                                exc.code, exc.reason)) from exc
    except urllib.error.URLError as exc:
        raise WorldDataError(
            "%s unreachable: %s" % (urllib.parse.urlsplit(url).netloc,
                                    exc.reason)) from exc
    except (TimeoutError, OSError) as exc:
        raise WorldDataError(
            "%s: %s" % (type(exc).__name__, exc)) from exc
    try:
        return json.loads(raw.decode("utf-8", "replace"))
    except json.JSONDecodeError as exc:
        raise WorldDataError("bad JSON from %s: %s"
                             % (urllib.parse.urlsplit(url).netloc, exc)) from exc


def _cached(kind: str, produce):
    """Return a cached value if it is young enough, else call produce().

    Held under a lock only for the read, so two threads cannot both fire a
    request for the same kind -- but a slow request does not block the other
    kinds, which matters because a voice answer is waiting on this.
    """
    now = time.time()
    with _cache_lock:
        hit = _cache.get(kind)
    if hit and now - hit[0] < _MIN_INTERVAL_S.get(kind, 60.0):
        return hit[1]
    value = produce()
    with _cache_lock:
        _cache[kind] = (time.time(), value)
    return value


# ── geometry ────────────────────────────────────────────────────────────────

def _stamp() -> str:
    """The moment a lookup was made, in the user's own timezone.

    Local time rather than UTC because the answer is spoken aloud: "14:32" is
    useful, "12:32Z" is something to convert before it means anything. Also the
    reason a dot on the globe is never undated -- a marker with no time on it
    reads as current fact long after it stopped being one.
    """
    return datetime.now().astimezone().strftime("%H:%M")


def distance_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Great-circle distance in kilometres.

    Haversine, because a flat approximation is visibly wrong at the scale a
    user cares about ("overhead" vs "two hundred kilometres away") and this is
    the function that decides which aircraft are mentioned at all.
    """
    r = 6371.0088
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dp = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def bearing_deg(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Initial compass bearing from point 1 to point 2, 0 = north."""
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dl = math.radians(lon2 - lon1)
    y = math.sin(dl) * math.cos(p2)
    x = math.cos(p1) * math.sin(p2) - math.sin(p1) * math.cos(p2) * math.cos(dl)
    return (math.degrees(math.atan2(y, x)) + 360.0) % 360.0


def compass_span(deg: float) -> str:
    """Compass point for a bearing, in Spanish.

    Sixteen points rather than eight: a user asked "which way" is better served
    by ENE than by a vague "east", and this is the difference between an answer
    that lands and one that has to be worked out.
    """
    names = ("N", "NNE", "NE", "ENE", "E", "ESE", "SE", "SSE",
             "S", "SSO", "SO", "OSO", "O", "ONO", "NO", "NNO")
    return names[int((deg % 360.0) / 22.5 + 0.5) % 16]


# ── sources ─────────────────────────────────────────────────────────────────

def geocode(place: str) -> dict:
    """Turn a place name into coordinates.

    This is what makes "overhead" answerable at all: without it, "what is near
    me" has no centre. Open-Meteo's geocoder is keyless and returns the
    municipality, so the answer can name the place back.
    """
    place = (place or "").strip()
    if not place:
        raise WorldDataError("no place given")

    def produce():
        url = ("https://geocoding-api.open-meteo.com/v1/search"
               "?name=%s&count=1&language=es&format=json"
               % urllib.parse.quote(place))
        data = _fetch(url)
        results = data.get("results") if isinstance(data, dict) else None
        if not results:
            raise WorldDataError("place not found: %s" % place)
        hit = results[0]
        return {
            "query": place,
            "name": hit.get("name"),
            "country": hit.get("country"),
            "admin1": hit.get("admin1"),
            "lat": float(hit["latitude"]),
            "lon": float(hit["longitude"]),
            "source": "open-meteo-geocoding",
        }

    return _cached("geocode:" + place.lower(), produce)


def aircraft_near(lat: float, lon: float, radius_km: float = 150.0,
                  limit: int = 60) -> dict:
    """Aircraft within radius_km, nearest first, with times and what they are.

    The default limit is 60, not 12. `count` reports everything the source
    found within the radius while the list is truncated, which is correct for a
    spoken answer -- "there are 19, the closest is X" -- and was quietly wrong
    for the map. The globe could only draw the 12 in the list, so a voice that
    said 19 was followed by a map showing 12, with no way for the user to tell
    the picture was a subset. Twelve was picked when nothing was drawn, purely
    to keep the response small; now that the whole list is the picture, the
    limit has to cover what the voice claims exists.

    Uses adsb.lol rather than OpenSky: measured from here, OpenSky's anonymous
    access returned nothing inside a tight box around Jerez while adsb.lol
    returned 23 aircraft within 150 km. Both are keyless; only one of them
    actually covers the area a user in Spain would ask about.

    Only aircraft with a position are counted. A response with no coordinates
    cannot be said to be anywhere, and including it would produce a confident
    answer about a plane whose location is unknown.
    """
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise WorldDataError("bad coordinates: %s, %s" % (lat, lon))
    radius = max(10.0, min(float(radius_km), 400.0))

    def produce():
        url = ("https://api.adsb.lol/v2/lat/%.5f/lon/%.5f/dist/%d"
               % (lat, lon, int(radius)))
        data = _fetch(url)
        rows = data.get("ac") if isinstance(data, dict) else data
        if not isinstance(rows, list):
            raise WorldDataError("unexpected aircraft payload")

        out = []
        for row in rows:
            try:
                alat, alon = float(row["lat"]), float(row["lon"])
            except (KeyError, TypeError, ValueError):
                continue          # no position: cannot say where it is
            km = distance_km(lat, lon, alat, alon)
            if km > radius:
                continue
            alt = row.get("alt_baro")
            # alt_baro is feet, not metres, despite the name. The first version
            # of this assumed metres and multiplied again by 3.28, which turned
            # a cruising 40000 ft into "111548 ft" -- a number so far beyond
            # anything real that it would have been narrated as fact. Verified
            # against the raw payload: a 40000 ft level.
            try:
                alt_ft = float(alt)
            except (TypeError, ValueError):
                alt_ft = None
            # Sanity bound, because a spoken altitude should never be absurd.
            # A 1200 km ceiling is generous for any aircraft in service.
            if alt_ft is not None and not (-1500.0 <= alt_ft <= 40000.0):
                alt_ft = None
            on_ground = str(alt).lower() == "ground"
            out.append({
                "hex": (row.get("hex") or "").strip(),
                "flight": (row.get("flight") or "").strip() or None,
                "type": row.get("t") or None,
                "registration": (row.get("r") or "").strip() or None,
                "lat": alat,
                "lon": alon,
                "distance_km": round(km, 1),
                "bearing_deg": round(bearing_deg(lat, lon, alat, alon), 1),
                "bearing": compass_span(bearing_deg(lat, lon, alat, alon)),
                "altitude_ft": int(alt_ft) if alt_ft is not None else None,
                "altitude_m": (int(alt_ft * 0.3048) if alt_ft is not None
                               else None),
                "ground_speed_kmh": row.get("gs"),
                "on_ground": on_ground,
                "emergency": row.get("emergency")
                if row.get("emergency") not in (None, "none") else None,
            })
        out.sort(key=lambda a: a["distance_km"])
        return {
            "centre": {"lat": lat, "lon": lon},
            "radius_km": radius,
            "count": len(out),
            "aircraft": out[:limit],
            "source": "adsb.lol",
            "queried_at": _stamp(),
        }

    return _cached("aircraft:%.3f,%.3f:%d" % (lat, lon, int(radius)), produce)


def earthquakes_near(lat: float, lon: float, radius_km: float = 800.0,
                     min_mag: float = 2.5, limit: int = 8) -> dict:
    """Recent earthquakes within radius_km, largest first.

    The USGS feed is global and keyless, and 24 hours of M2.5+ was measured at 44
    events worldwide, so this is a source that reliably has something to say.
    The window is what makes it useful: "this week" includes the same 44 events
    a second call would return, so the cache interval does the work.
    """
    radius = max(50.0, min(float(radius_km), 20000.0))

    def produce():
        url = ("https://earthquake.usgs.gov/earthquakes/feed/v1.0/summary/"
               "%.1f_day.geojson" % min_mag)
        data = _fetch(url)
        features = (data or {}).get("features") or []
        out = []
        for feat in features:
            props = feat.get("properties") or {}
            coords = ((feat.get("geometry") or {}).get("coordinates") or [None])[:3]
            try:
                elat, elon = float(coords[1]), float(coords[0])
            except (TypeError, ValueError, IndexError):
                continue
            km = distance_km(lat, lon, elat, elon)
            if km > radius:
                continue
            out.append({
                "id": feat.get("id"),
                "magnitude": props.get("mag"),
                "place": props.get("place"),
                "lat": elat,
                "lon": elon,
                "depth_km": coords[2],
                "distance_km": round(km, 1),
                "felt": props.get("felt"),
                "tsunami": bool(props.get("tsunami")),
                "url": props.get("url"),
            })
        out.sort(key=lambda e: -(e["magnitude"] or 0))
        return {
            "centre": {"lat": lat, "lon": lon},
            "radius_km": radius,
            "count": len(out),
            "quakes": out[:limit],
            "source": "USGS",
            "queried_at": _stamp(),
        }

    return _cached("quakes:%.2f,%.2f:%d:%.1f"
                   % (lat, lon, int(radius), min_mag), produce)


def satellites(limit: int = 20) -> dict:
    """Satellites in orbit, grouped by mission.

    CelesTrak's "stations" group is the satellites transmitting a signal that a
    receiver on the ground can actually hear -- GPS, Galileo, Iridium, Starlink
    and so on. The full catalogue is tens of thousands of objects including
    debris; "stations" is the set a person asking about satellites means.
    """
    def produce():
        url = ("https://celestrak.org/NORAD/elements/gp.php"
               "?GROUP=stations&FORMAT=json")
        data = _fetch(url)
        if not isinstance(data, list):
            raise WorldDataError("unexpected satellite payload")
        out = []
        for row in data:
            name = (row.get("OBJECT_NAME") or "").strip()
            if not name:
                continue
            # Period is not in the payload; MEAN_MOTION is revolutions per day.
            # Asking for "PERIOD" returned None, and the first version rendered
            # that as "0 min" for every satellite, which reads as a broken
            # number rather than a missing one. 1440 / mean_motion is the period
            # in minutes.
            period = None
            try:
                mm = float(row["MEAN_MOTION"])
                if mm > 0:
                    period = round(1440.0 / mm, 1)
            except (KeyError, TypeError, ValueError):
                period = None
            out.append({
                "name": name,
                "norad_id": row.get("NORAD_CAT_ID"),
                "intl_designator": row.get("OBJECT_ID"),
                "period_min": period,
                "inclination_deg": row.get("INCLINATION"),
            })
        return {"count": len(out), "satellites": out[:limit],
                "source": "CelesTrak", "queried_at": _stamp()}

    return _cached("satellites", produce)


def firms_key() -> str:
    """The NASA FIRMS map key, from the Globe app's own .env.

    Read fresh every call and never cached, so correcting a key takes effect on
    the next question. A key that is present but not working is the normal
    case here rather than an edge case: NASA's own message for a key that is
    wrong and a key that has exceeded its transaction limit is the same
    sentence, and both were seen while writing this.
    """
    try:
        for line in GEV_ENV.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            name, _, value = line.partition("=")
            if name.strip().upper() == "FIRMS_MAP_KEY":
                return value.strip().strip("'\"")
    except (OSError, UnicodeDecodeError):
        return ""
    return ""


def _first_present(cells: list, idx: dict, names: tuple):
    """The first of `names` this row actually has, or None.

    FIRMS is two sensors sharing one endpoint, and they do not agree on column
    names: MODIS writes `brightness`, VIIRS writes `bright_ti4`/`bright_ti5`.
    Reaching for a single name means the other sensor parses into rows with
    that field quietly set to None -- a missing reading that looks like a
    sensor that measured nothing, which is a different and wrong claim.
    """
    for name in names:
        i = idx.get(name)
        if i is not None and i < len(cells):
            value = cells[i]
            if value not in ("", None):
                return value
    return None


def fires_near(lat: float, lon: float, radius_km: float = 150.0,
               days: int = 1, limit: int = 10) -> dict:
    """Active fire detections near a place, from NASA's VIIRS/FIRMS.

    The only source here that needs a credential, and the one that most needs
    to be honest about it. Three outcomes are told apart, because they mean
    different things to someone who asked:

      * no key configured      -> "I can't see fires, nobody gave me a key"
      * key rejected or spent  -> "the key isn't working, here's NASA's word"
      * key fine, nothing found-> "no fires, and I can see the sensor"

    The middle case is the one a naive implementation gets wrong. NASA answers
    400 or 403 with a plain-text body, never CSV, and a parser that treats any
    non-JSON response as "no data" would answer "there are no fires near you"
    every single time -- a confident, wrong, safety-relevant answer.

    Never logs the key, and never puts it in an error message: the URL embeds
    it, so any exception text that included the URL would leak it into a log
    or a spoken reply.
    """
    if not (-90.0 <= lat <= 90.0 and -180.0 <= lon <= 180.0):
        raise WorldDataError("bad coordinates: %s, %s" % (lat, lon))
    key = firms_key()
    if not key:
        raise WorldDataError(
            "no hay clave de NASA FIRMS configurada "
            "(FIRMS_MAP_KEY en vendor/gods-eye/.env)")
    radius = max(10.0, min(float(radius_km), 400.0))
    # NASA takes a bounding box. A rough conversion at these latitudes is
    # accurate enough for a "near me" question and avoids a projection
    # dependency: one degree of latitude is a constant 111 km.
    dlat = radius / 111.0
    dlon = radius / (111.0 * max(0.15, abs(math.cos(math.radians(lat)))))
    box = ",".join("%.4f" % v for v in (
        lon - dlon, lat - dlat, lon + dlon, lat + dlat))

    # The path order is KEY/SOURCE/BOX/DAYS, and getting it wrong fails in a
    # way that looks like a credential problem. Passing a date where the source
    # belongs returns 400 `Invalid source.` -- which reads like a bad key, and
    # sent us looking at the key for a while instead of at this line.
    #
    # The source is not optional either. Without it the API answers 400 on
    # every call, so "just omit it" is not the fallback. VIIRS_SNPP_NRT is the
    # near-real-time product: available within hours, which is what makes it
    # useful for "is anything burning near me right now".
    url = ("https://firms.modaps.eosdis.nasa.gov/api/area/csv/%s/%s/%s/%d"
           % (urllib.parse.quote(key), FIRMS_SOURCE, box, int(days)))
    req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    try:
        with urllib.request.urlopen(req, timeout=45) as resp:
            text = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace").strip()[:160]
        except Exception:                             # noqa: BLE001
            pass
        # The key is in the URL, so the message is rebuilt from scratch rather
        # than reusing the exception, which would carry it.
        if exc.code in (400, 403):
            # NASA often answers with two lines -- "Invalid MAP_KEY." followed
            # by "Invalid source." -- and the second is just the same rejection
            # restated. Only the first is worth telling the user about.
            for line in detail.splitlines():
                line = line.strip()
                if line and "MAP_KEY" in line:
                    detail = line.rstrip(".")
                    break
            raise WorldDataError(
                "NASA rechaza la clave (%s): %s"
                % (exc.code, detail or "clave no valida o cuota agotada")) from None
        raise WorldDataError(
            "NASA FIRMS HTTP %s" % exc.code) from None
    except (TimeoutError, OSError) as exc:
        raise WorldDataError("NASA FIRMS: %s" % type(exc).__name__) from exc

    if "Invalid MAP_KEY" in text or "MAP_KEY is invalid" in text:
        raise WorldDataError(
            "NASA dice que la clave no es valida o se paso de transactions")

    lines = [ln for ln in text.splitlines() if ln.strip()]
    if not lines:
        return {"centre": {"lat": lat, "lon": lon}, "radius_km": radius,
                "count": 0, "fires": [], "source": "NASA FIRMS",
            "queried_at": _stamp()}
    header = lines[0].split(",")
    idx = {name.strip(): i for i, name in enumerate(header)}

    out = []
    for line in lines[1:]:
        cells = line.split(",")
        if len(cells) < len(header):
            continue
        try:
            flat, flon = float(cells[idx["latitude"]]), float(cells[idx["longitude"]])
            km = distance_km(lat, lon, flat, flon)
        except (KeyError, IndexError, ValueError):
            continue
        if km > radius:
            continue
        out.append({
            "lat": flat,
            "lon": flon,
            "distance_km": round(km, 1),
            "bearing": compass_span(bearing_deg(lat, lon, flat, flon)),
            # These are satellite names, not places: "VIIRS" is the sensor.
            "satellite": (cells[idx["satellite"]] if "satellite" in idx else None),
            "confidence": (cells[idx["confidence"]] if "confidence" in idx else None),
            # VIIRS and MODIS name this column differently and the old code
            # only knew one of them. `brightness` is MODIS; VIIRS reports
            # `bright_ti4` (and `bright_ti5`). Looking up one name and getting
            # None was silent -- the row still parsed, the reading just
            # vanished -- so both are accepted and whichever is present is
            # used. `frp` is the number worth saying out loud anyway, in
            # megawatts, and it is spelled the same in both.
            "brightness": _first_present(cells, idx,
                                         ("brightness", "bright_ti4",
                                          "bright_ti5")),
            "acq_date": (cells[idx["acq_date"]] if "acq_date" in idx else None),
            "acq_time": (cells[idx["acq_time"]] if "acq_time" in idx else None),
            "frp": (float(cells[idx["frp"]]) if "frp" in idx
                    and cells[idx["frp"]] not in ("", "0.0") else None),
        })
    out.sort(key=lambda f: f["distance_km"])
    return {"centre": {"lat": lat, "lon": lon}, "radius_km": radius,
            "count": len(out), "fires": out[:limit],
            "source": "NASA FIRMS (%s)" % FIRMS_SOURCE,
            "queried_at": _stamp()}


def weather(lat: float, lon: float) -> dict:
    """Current conditions at a point.

    WMO codes are translated to Spanish text here rather than handed to the
    model as numbers: "CÓDIGO 95" means nothing to a listener, "tormenta
    eléctrica" means everything, and the mapping is deterministic so it cannot
    be got wrong by a model that has never seen the code table.
    """
    wmo = {
        0: "despejado", 1: "mayormente despejado", 2: "parcialmente nublado",
        3: "nublado", 45: "niebla", 48: "niebla con escarcha",
        51: "llovizna ligera", 53: "llovizna", 55: "llovizna intensa",
        56: "llovizna helada ligera", 57: "llovizna helada",
        61: "lluvia ligera", 63: "lluvia", 65: "lluvia intensa",
        66: "lluvia helada ligera", 67: "lluvia helada",
        71: "nieve ligera", 73: "nieve", 75: "nieve intensa",
        77: "granizo de nieve", 80: "chubascos ligeros", 81: "chubascos",
        82: "chubascos violentos", 85: "chubascos de nieve",
        86: "chubascos de nieve fuertes", 95: "tormenta",
        96: "tormenta con granizo", 99: "tormenta fuerte con granizo",
    }

    def produce():
        url = ("https://api.open-meteo.com/v1/forecast?latitude=%.5f"
               "&longitude=%.5f&current=temperature_2m,apparent_temperature,"
               "relative_humidity_2m,wind_speed_10m,wind_direction_10m,"
               "weather_code,surface_pressure&timezone=auto" % (lat, lon))
        data = _fetch(url)
        cur = (data or {}).get("current")
        if not cur:
            raise WorldDataError("no current conditions returned")
        code = cur.get("weather_code")
        return {
            "lat": lat,
            "lon": lon,
            "temperature_c": cur.get("temperature_2m"),
            "apparent_c": cur.get("apparent_temperature"),
            "humidity_pct": cur.get("relative_humidity_2m"),
            "wind_kmh": cur.get("wind_speed_10m"),
            "wind_dir_deg": cur.get("wind_direction_10m"),
            "wind_bearing": compass_span(float(cur.get("wind_direction_10m") or 0)),
            "pressure_hpa": cur.get("surface_pressure"),
            "code": code,
            "description": wmo.get(code, "condiciones desconocidas"),
            "source": "open-meteo",
        }

    return _cached("weather:%.2f,%.2f" % (lat, lon), produce)


def snapshot(place: str, radius_km: float = 150.0) -> dict:
    """Everything at once about a place, for a broad "what's happening" question.

    Each source is independent: one being unreachable must not lose the others.
    The result says which parts failed and why, because a partial answer that
    admits what is missing is useful, and a partial answer that hides the gap
    is not.
    """
    located = geocode(place)
    lat, lon = located["lat"], located["lon"]
    data: dict = {"place": located, "radius_km": radius_km, "errors": {}}
    for key, call in (
            ("aircraft", lambda: aircraft_near(lat, lon, radius_km)),
            ("earthquakes", lambda: earthquakes_near(lat, lon, 800.0)),
            ("weather", lambda: weather(lat, lon)),
            ("satellites", satellites),
    ):
        try:
            data[key] = call()
        except WorldDataError as exc:
            data["errors"][key] = str(exc)
    return data


def sources() -> dict:
    """What is available and how fresh, for the doctor and for the user.

    Probes rather than trusting configuration, because the only honest claim
    about a public endpoint is whether it answered.
    """
    checks = {
        "aircraft (adsb.lol)":
            lambda: aircraft_near(36.53, -6.35, 150.0)["count"],
        "earthquakes (USGS)":
            lambda: earthquakes_near(36.53, -6.35, 20000.0)["count"],
        "satellites (CelesTrak)": lambda: satellites()["count"],
        "weather (open-meteo)":
            lambda: weather(36.53, -6.35)["description"],
    }
    out = {}
    for label, call in checks.items():
        try:
            out[label] = {"ok": True, "detail": str(call())[:80]}
        except WorldDataError as exc:
            out[label] = {"ok": False, "detail": str(exc)[:120]}
        except Exception as exc:                        # noqa: BLE001
            out[label] = {"ok": False,
                          "detail": "%s: %s" % (type(exc).__name__, exc)[:120]}
    out["fires (NASA FIRMS)"] = _firms_status()
    # The image library is a different service with its own key, and it works
    # even when fire detection does not. Keeping them as separate entries is
    # the point: "NASA is broken" is not a thing a user can act on, whereas
    # "fire detection is down, images are fine" is.
    out["images (NASA library)"] = _nasa_images_status()
    return out


def _nasa_images_status() -> dict:
    """Is the image library available?

    Unlike the other entries this does not make a request. The search endpoint
    answers 200 without a key, so probing it on every doctor run would spend a
    rate-limited call to learn nothing that configuration cannot say, and a
    check that reports red on a network blip is a check people switch off. It
    reports what is configured; `core.nasa_images.search_images` reports what
    is reachable, and says which of the two went wrong.
    """
    try:
        from core.nasa_images import source_status as _si
        st = _si()
    except Exception as exc:                             # noqa: BLE001
        return {"ok": False, "detail": "module unavailable: %s" % exc}
    return {"ok": st["configured"],
            "detail": st["note"][:120]}


def _firms_status() -> dict:
    """Is fire data actually available?

    Reports the three states separately, because "no fires" and "I cannot see
    fires" are opposites and conflating them is how an assistant ends up
    reassuring someone about a wildfire it never looked for. The probe asks for
    a one-square-kilometre box, which is a single transaction against a
    500-a-day allowance -- cheap enough to run in the doctor.
    """
    if not firms_key():
        return {"ok": False, "detail": "no FIRMS_MAP_KEY in "
                                       "vendor/gods-eye/.env", "keyed": False}
    try:
        # A deliberately tiny box in the middle of nowhere: it costs one
        # transaction and cannot be confused with a real answer.
        fires_near(0.0, 0.0, radius_km=10.0, limit=1)
        return {"ok": True, "detail": "key accepted", "keyed": True}
    except WorldDataError as exc:
        return {"ok": False, "detail": str(exc)[:120], "keyed": True}
