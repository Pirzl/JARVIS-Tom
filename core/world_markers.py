"""Turning a spoken answer into things worth looking at.

Phases 1A to 1C made the globe and the voice work, but they were two systems
that never spoke to each other. Ask JARVIS what is flying over Jerez and it
tells you -- in numbers, over voice -- and the map stays blank. That gap is
what this closes: the same lookup that produced the sentence also produces the
markers, so the answer and the picture are the same fact shown two ways.

The rule that shapes the code: **what is drawn must be what was said.** If the
voice says "26 aircraft", the globe must show the 26 aircraft the source
returned, not a sample, not a decorative cluster. A marker the user cannot
trace back to a number in the sentence is a lie in a different colour.

Everything here is public broadcast data. Nothing identifies a person, follows
a vehicle or watches a private thing -- the same constraint the voice answers
already operate under, and the payload is built to make violating it awkward
rather than merely discouraged.
"""
import math

# Cap on markers sent to the globe in one answer. Not about the network: it is
# the point at which a globe full of dots stops being readable. 200 is roughly
# where "look at all of it" becomes "I cannot see anything", and a spoken
# answer tops out at four or five items long before this, so the cap is a
# safety edge rather than something normally reached.
MAX_MARKERS = 200

# How far away from the requested centre a marker may be and still be drawn.
# Without this a bad coordinate from a provider drops a dot in the middle of the
# ocean and the user is left looking at the wrong part of the planet wondering
# what they did wrong.
MAX_OFFSET_KM = 2200.0

# Colour per kind, as ARGB. Chosen to be distinguishable on the dark satellite
# basemap and from each other, and to stay readable under the globe's own bloom
# and scanline styling, which is why the fills are saturated rather than pale.
KIND_COLOURS = {
    "aircraft":     0xFF00E5FF,   # cyan
    "earthquake":   0xFFFF4D4D,   # red
    "fire":         0xFFFF9500,   # orange
    "satellite":    0xFFB388FF,   # violet
    "place":        0xFF76FF03,   # green
}

DEFAULT_COLOUR = 0xFFFFFFFF


def _c(lat: float, lon: float):
    """A valid coordinate, or None.

    Returns a pair of floats clamped into range, or None when the value is not
    a number at all. NaN is the one that matters: it survives comparisons, so a
    plain range check passes it and Cesium then throws deep inside a render.
    """
    try:
        flat, flon = float(lat), float(lon)
    except (TypeError, ValueError):
        return None
    if not (math.isfinite(flat) and math.isfinite(flon)):
        return None
    return (max(-90.0, min(90.0, flat)), max(-180.0, min(180.0, flon)))


def _distance_km(a_lat, a_lon, b_lat, b_lon) -> float:
    """Great-circle distance, same formula the spoken answers use.

    Reusing the number matters more than the code: if the voice says "41 km" and
    the marker sits somewhere else because two different formulas disagree, the
    user has no way to tell which one is lying.
    """
    r = 6371.0
    p1, p2 = math.radians(a_lat), math.radians(b_lat)
    dp = math.radians(b_lat - a_lat)
    dl = math.radians(b_lon - a_lon)
    h = math.sin(dp / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(min(1.0, math.sqrt(h)))


def _marker(lat, lon, kind, label, detail="", rank=0, **extra) -> dict:
    """One marker, in the shape the globe's JS side expects.

    `rank` orders the list from most to least notable, so the globe can label
    the first few and not the rest. `detail` is a short secondary line: altitude
    and speed for a plane, magnitude for a quake. It is a label, not a data
    dump -- anything longer belongs in the spoken answer, which is where someone
    actually reads it.
    """
    point = _c(lat, lon)
    if point is None:
        return None
    marker = {
        "lat": point[0],
        "lon": point[1],
        "kind": kind,
        "label": str(label or "")[:80],
        "detail": str(detail or "")[:120],
        "rank": int(rank),
        "colour": KIND_COLOURS.get(kind, DEFAULT_COLOUR),
    }
    # Only the fields a given kind actually has, so the JS never has to guard
    # against an altitude of undefined on a fire marker.
    for key, value in extra.items():
        if value is not None:
            marker[key] = value
    return marker


def from_aircraft(data: dict) -> list:
    """Every aircraft returned for the query, not a selection of them.

    The voice names the closest few. The globe can afford to show all of them,
    and the cap is applied after sorting so what gets dropped is the farthest,
    not an arbitrary slice.
    """
    out = []
    for craft in (data or {}).get("aircraft", []):
        bits = []
        if craft.get("altitude_ft"):
            bits.append("%.0f ft" % craft["altitude_ft"])
        if craft.get("ground_speed_kmh"):
            try:
                bits.append("%.0f km/h" % float(craft["ground_speed_kmh"]))
            except (TypeError, ValueError):
                pass
        if craft.get("distance_km") is not None:
            bits.append("%.0f km" % craft["distance_km"])
        name = craft.get("flight") or craft.get("registration") or "Sin nombre"
        if isinstance(name, str) and name and set(name) == {"@"}:
            # A callsign of "@@@@@@" is a missing value wearing a disguise.
            name = craft.get("registration") or craft.get("hex") or "Sin nombre"
        out.append(_marker(
            craft.get("lat"), craft.get("lon"), "aircraft", name,
            ", ".join(bits), 0,
            altitudeFt=craft.get("altitude_ft"),
            speedKmh=craft.get("ground_speed_kmh"),
            callsign=craft.get("flight") or "",
            emergency=bool(craft.get("emergency")),
        ))
    out = [m for m in out if m]
    # Already ordered by distance: world_data sorts by distance_km before
    # truncating, and that is the order the spoken answer reads out. Keeping it
    # means the globe and the voice agree on which plane is the closest one.
    return out[:MAX_MARKERS]


def from_earthquakes(data: dict) -> list:
    """Quake epicentres, strongest first.

    A shallow M6 is more newsworthy than a deep M4, so the ordering is by
    magnitude rather than by distance from the city asked about: when someone
    asks "any earthquakes near here" they want to know about the big ones.
    """
    out = []
    for quake in (data or {}).get("quakes", []):
        bits = []
        if quake.get("magnitude") is not None:
            bits.append("M%.1f" % quake["magnitude"])
        if quake.get("depth_km") is not None:
            bits.append("%.0f km de profundidad" % quake["depth_km"])
        if quake.get("distance_km") is not None:
            bits.append("a %.0f km" % quake["distance_km"])
        name = quake.get("place") or "Terremoto"
        # USGS place strings are long and machine-made ("120 km SSW of Foo").
        # Trim to the nearest named thing so the label is readable on a globe.
        name = name.split(" of ")[-1] if " of " in name else name
        out.append(_marker(
            quake.get("lat"), quake.get("lon"), "earthquake", name,
            ", ".join(bits), 0,
            magnitude=quake.get("magnitude"),
            tsunami=bool(quake.get("tsunami")),
        ))
    out = [m for m in out if m]
    out.sort(key=lambda m: m.get("magnitude") or 0, reverse=True)
    return out[:MAX_MARKERS]


def from_fires(data: dict) -> list:
    """Fire detections, nearest first -- the order the spoken answer uses."""
    out = []
    for fire in (data or {}).get("fires", []):
        bits = []
        if fire.get("distance_km") is not None:
            bits.append("a %.0f km" % fire["distance_km"])
        if fire.get("confidence") in ("low", "nominal", "high"):
            bits.append({"low": "confianza baja", "nominal": "confianza normal",
                         "high": "confianza alta"}[fire["confidence"]])
        if fire.get("frp"):
            bits.append("%.0f MW" % fire["frp"])
        out.append(_marker(
            fire.get("lat"), fire.get("lon"), "fire", "Foco de incendio",
            ", ".join(bits), 0, frpMW=fire.get("frp"),
        ))
    return [m for m in out if m][:MAX_MARKERS]


def from_satellites(data: dict) -> list:
    """Satellites broadcasting a tracking signal.

    CelesTrak's active-satellites group carries an orbit, not a position: there
    is no latitude and longitude to draw. TLE propagation would give one, and
    was left out on purpose -- it is a different amount of code and a different
    set of failure modes, for a list that is better read aloud than looked at.
    The globe therefore shows the place asked about and says nothing false
    about where the satellites are.
    """
    return []


def build(kind: str, data: dict, centre: dict = None) -> list:
    """The markers for one answer, centre first when there is one.

    `centre` is the place the user asked about. It is drawn as a distinct
    marker because it is the only thing in the payload the user chose rather
    than a source reported -- and when a marker looks wrong, being able to see
    what the query was anchored to is the fastest way to find out why.
    """
    builders = {
        "aircraft": from_aircraft,
        "earthquakes": from_earthquakes,
        "fires": from_fires,
        "satellites": from_satellites,
        # A briefing registers its aircraft layer under "overview", so asking
        # for a briefing draws the same planes a plain aircraft question would.
        "overview": from_aircraft,
    }
    builder = builders.get(kind)
    if builder is None or not data:
        # No builder, or nothing came back from the source. Either way the
        # answer is "no markers", not a guessed set: the centre alone would be
        # a dot on a map implying something was found there.
        return []
    markers = builder(data)

    if centre:
        point = _c(centre.get("lat"), centre.get("lon"))
        if point is not None:
            markers.insert(0, _marker(
                point[0], point[1], "place",
                centre.get("name") or centre.get("query") or "Centro",
                "Consultado aquí", -1,
            ))
    return markers[:MAX_MARKERS + 1]


def payload(kind: str, data: dict, centre: dict = None) -> dict:
    """The complete message for the globe.

    Carries the source and the moment alongside the markers, because a dot on
    a globe with no idea when it was true is how a stale reading becomes a
    current fact. The globe shows both; it cannot decide on its own.
    """
    markers = build(kind, data, centre)
    return {
        "kind": kind,
        "markers": markers,
        "count": len(markers),
        "source": (data or {}).get("source", ""),
        "queriedAt": (data or {}).get("queried_at", ""),
    }
