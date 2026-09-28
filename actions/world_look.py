"""
world_look — voice access to live world data.

Thomas asks what is happening somewhere and gets an answer built from public
feeds, not from the Globe window. The globe and this are the same data seen
two ways: the map for looking, this for asking. It answers whether or not the
map is open, which is the point — the question is usually asked without anyone
having thought to open a panel first.

The range is deliberately wide. Every one of these is a real source that
answered when it was written:

  "¿qué vuela sobre mí?"          aircraft overhead, by place name
  "¿qué pasa en Madrid?"          a full briefing on any city
  "dime los terremotos"           recent seismic activity
  "¿qué satélites hay?"           satellites transmitting to the ground
  "¿qué tiempo hace?"             current conditions
  "¿está el mundo bien?"          everything at once

The rules this file exists to enforce:

  1. The model never invents a fact. It receives numbers that came out of an
     HTTP response and phrases them; it does not produce them. A source that is
     down produces a sentence saying so, never an empty-sounding answer.

  2. A zero is an answer, not a failure. "Nothing is flying within 150 km right
     now" is true and useful, and is reported as exactly that.

  3. Every answer carries its source and the time it was fetched, so a
     follow-up question can be checked rather than trusted.

Public data only, and only what these services publish by design: aircraft
transponder broadcasts, satellite ephemerides, seismic bulletins, weather
observations. No tracking of individuals, no private feeds, no CCTV, no
military sites.
"""

from __future__ import annotations

import sys
import time
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
if str(BASE_DIR) not in sys.path:
    sys.path.insert(0, str(BASE_DIR))

from core import world_data as wd  # noqa: E402

# Set once at startup. The globe window is opened on request so a spoken answer
# can point the user at what they just heard about, but the answer does not
# depend on it: if opening fails, the data is still returned.
_open_globe = None


def bind_session(open_globe=None) -> None:
    """Wire the globe window in. Called once by JarvisLive.__init__."""
    global _open_globe
    _open_globe = open_globe


def _show_on_globe(place: dict) -> str:
    """Best-effort: bring up the map centred on what we just described.

    The catch here is on purpose -- the answer the user asked for has already
    been produced, and losing it because a window would not open would be
    worse. But it prints. A missing map is worth a line in the log, because the
    alternative is a feature that is silently half-working and impossible to
    diagnose from the outside.
    """
    if _open_globe is None:
        print("[JARVIS] world look: no globe hook bound")
        return ""
    try:
        _open_globe(place["lat"], place["lon"], place.get("name") or place["query"])
    except Exception as exc:                                 # noqa: BLE001
        print("[JARVIS] world look: no se pudo abrir el globo (%s: %s)"
              % (type(exc).__name__, exc))
    return ""


def _when() -> str:
    return time.strftime("%H:%M")


def _geocode_or_explain(place: str) -> tuple[dict | None, str]:
    """Locate a place, or return the sentence explaining that we could not.

    A place that cannot be found is a normal outcome, not an exception: the
    user misheard a name, or said something vague. Letting WorldDataError
    escape took the whole turn down and left the assistant silent, which is the
    worst possible answer. It becomes a spoken sentence instead.
    """
    try:
        return wd.geocode(place), ""
    except wd.WorldDataError as exc:
        return None, ("No encuentro ningún sitio llamado «%s». Dime otra ciudad "
                      "o un país, o prueba con el nombre más corto." % place)


def _clean_name(raw, fallback: str = None) -> str:
    """A callsign has to be sayable.

    Aircraft transponders broadcast placeholder callsigns -- '@@@@@@@@' and
    strings of spaces are common and mean "the airline did not fill this in".
    Narrating them aloud produces noise the user cannot use, so they are dropped
    in favour of the registration, then the hex code.
    """
    name = (raw or "").strip()
    if not name or set(name) <= set("@ ?-_"):
        return fallback
    return name


def _fmt_place(place: dict) -> str:
    bits = [place.get("name") or place.get("query")]
    if place.get("admin1") and place["admin1"] != place.get("name"):
        bits.append(place["admin1"])
    if place.get("country"):
        bits.append(place["country"])
    return ", ".join(b for b in bits if b)


# ── the individual answers ───────────────────────────────────────────────────

def _aircraft(place: dict, radius_km: float, want_globe: bool) -> str:
    data = wd.aircraft_near(place["lat"], place["lon"], radius_km)
    if data["count"] == 0:
        return ("Ahora mismo no veo ningún avión en un radio de %d km alrededor "
                "de %s. Fuente %s, consultado a las %s."
                % (radius_km, _fmt_place(place), data["source"], _when()))

    lines = ["Cerca de %s, en %d km, hay %d aviones. Fuente %s, a las %s."
             % (_fmt_place(place), radius_km, data["count"],
                data["source"], _when())]
    for ac in data["aircraft"][:4]:
        who = (_clean_name(ac["flight"])
               or _clean_name(ac["registration"])
               or ("avión " + ac["hex"] if ac["hex"] else "un avión"))
        bits = ["%s, a %.0f km al %s" % (who, ac["distance_km"], ac["bearing"])]
        if ac["on_ground"]:
            bits.append("en tierra")
        elif ac["altitude_ft"]:
            bits.append("a %d pies de altura" % ac["altitude_ft"])
        if ac["ground_speed_kmh"]:
            bits.append("a %d km/h" % round(ac["ground_speed_kmh"]))
        lines.append("  " + ", ".join(bits) + ".")
    if data["count"] > 4:
        lines.append("  Y %d más." % (data["count"] - 4))
    if want_globe:
        _show_on_globe(place)
    return "\n".join(lines)


def _quakes(place: dict, radius_km: float) -> str:
    data = wd.earthquakes_near(place["lat"], place["lon"], radius_km)
    if data["count"] == 0:
        return ("No hay terremotos de magnitud 2.5 o superior en %d km alrededor "
                "de %s en las últimas 24 horas. Fuente %s, a las %s."
                % (data["radius_km"], _fmt_place(place), data["source"],
                   _when()))

    biggest = data["quakes"][0]
    lines = ["En %d km alrededor de %s, %d terremotos en las últimas 24 horas. "
             "Fuente %s, a las %s."
             % (radius_km, _fmt_place(place), data["count"],
                data["source"], _when())]
    for q in data["quakes"][:4]:
        bits = ["magnitud %.1f" % (q["magnitude"] or 0),
                "a %.0f km" % q["distance_km"]]
        if q["place"]:
            bits.append(q["place"])
        if q["depth_km"] is not None:
            bits.append("a %.0f km de profundidad" % q["depth_km"])
        if q["felt"]:
            bits.append("sentido")
        lines.append("  " + ", ".join(bits) + ".")
    if (biggest["magnitude"] or 0) >= 6.0:
        lines.append("  El mayor fue de magnitud %.1f, notable a escala mundial."
                     % biggest["magnitude"])
    return "\n".join(lines)


def _satellites(limit: int = 8) -> str:
    data = wd.satellites(limit=limit)
    lines = ["Hay %d satélites con señal de seguimiento activa. Fuente %s, a las %s."
             % (data["count"], data["source"], _when())]
    for sat in data["satellites"][:6]:
        bits = [sat["name"]]
        if sat["period_min"]:
            bits.append("órbita de %.0f minutos" % sat["period_min"])
        if sat["inclination_deg"] is not None:
            try:
                bits.append("inclinación de %.0f grados" % float(sat["inclination_deg"]))
            except (TypeError, ValueError):
                pass
        lines.append("  " + ", ".join(bits) + ".")
    if data["count"] > 6:
        lines.append("  Y %d más." % (data["count"] - 6))
    return "\n".join(lines)


def _weather(place: dict) -> str:
    w = wd.weather(place["lat"], place["lon"])
    lines = ["En %s ahora mismo: %s, %.0f grados, sensación de %.0f. "
             "Viento de %.0f km/h del %s, humedad %d por ciento. "
             "Fuente %s, a las %s."
             % (_fmt_place(place), w["description"], w["temperature_c"] or 0,
                w["apparent_c"] or 0, w["wind_kmh"] or 0, w["wind_bearing"],
                w["humidity_pct"] or 0, w["source"], _when())]
    if (w["temperature_c"] or 0) >= 35:
        lines.append("Hace bastante calor, cuidado con el sol.")
    elif (w["temperature_c"] or 0) <= 2:
        lines.append("Hace frío, abrígate bien.")
    return "\n".join(lines)


def _briefing(place: dict, radius_km: float, want_globe: bool) -> str:
    """Everything about a place at once.

    Reports what failed as well as what worked. A partial answer that says
    which part is missing is worth having; the same answer presented as complete
    would not be.
    """
    data = wd.snapshot(place["query"], radius_km)
    head = ["Resumen de %s:" % _fmt_place(data["place"])]

    air = data.get("aircraft")
    if air:
        if air["count"]:
            closest = air["aircraft"][0]
            who = closest["flight"] or closest["registration"] or closest["hex"]
            head.append("  Aviones: %d en %d km. El más cercano es %s, a %.0f km "
                        "al %s." % (air["count"], air["radius_km"], who,
                                    closest["distance_km"], closest["bearing"]))
        else:
            head.append("  Aviones: ninguno en %d km." % air["radius_km"])

    wx = data.get("weather")
    if wx:
        head.append("  Tiempo: %s, %.0f grados, viento de %.0f km/h."
                    % (wx["description"], wx["temperature_c"] or 0,
                       wx["wind_kmh"] or 0))

    quakes = data.get("earthquakes")
    if quakes:
        if quakes["count"]:
            head.append("  Terremotos: %d en las últimas 24 horas, el mayor de "
                        "magnitud %.1f." % (quakes["count"],
                                            quakes["quakes"][0]["magnitude"] or 0))
        else:
            head.append("  Terremotos: ninguno registrado cerca.")

    sats = data.get("satellites")
    if sats:
        head.append("  Satélites: %d con señal activa." % sats["count"])

    for key, why in (data.get("errors") or {}).items():
        head.append("  No he podido consultar %s: %s." % (key, why))

    head.append("  Son datos públicos en directo, consultados a las %s." % _when())
    if want_globe:
        _show_on_globe(data["place"])
    return "\n".join(head)


def _sources() -> str:
    """Answer 'what can you actually see?' honestly."""
    out = ["Esto es lo que puedo consultar ahora mismo:"]
    for label, info in wd.sources().items():
        out.append("  %s %s — %s" % ("✓" if info["ok"] else "✗", label,
                                     info["detail"]))
    out.append("Todo son datos públicos. Los incendios por satélite necesitan una "
               "clave que no tengo, así que no te los puedo dar.")
    return "\n".join(out)


# ── entry point ─────────────────────────────────────────────────────────────

def world_look(parameters: dict, speak=None) -> str:
    """Entry point for the `world_look` tool."""
    action = str(parameters.get("action", "")).strip().lower()
    place = str(parameters.get("place", "")).strip()
    want_globe = bool(parameters.get("show_on_map"))

    if action == "sources":
        try:
            return _sources()
        except Exception as exc:                        # noqa: BLE001
            return "No he podido comprobar las fuentes ahora mismo: %s" % exc

    if action == "satellites":
        try:
            return _satellites()
        except wd.WorldDataError as exc:
            return ("No he podido conectar con la fuente de satélites: %s. "
                    "Inténtalo dentro de un momento." % exc)

    if not place:
        # Only the satellite count and the source list make sense without a
        # location. Everything else needs a centre, and guessing which city the
        # user means would be inventing context.
        return ("¿Sobre qué sitio quieres que mire? Dime una ciudad, un lugar o "
                "las coordenadas.")

    located, problem = _geocode_or_explain(place)
    if located is None:
        return problem

    # A sensible default radius per question type. 150 km answers "overhead";
    # a seismic question is asking about a whole region, and clamping it to the
    # aircraft default would have answered a different question than the one
    # asked. An explicit radius from the caller always wins.
    default_radius = {
        "aircraft": 150.0, "flights": 150.0, "aviones": 150.0, "vuela": 150.0,
        "overhead": 150.0,
        "earthquakes": 1200.0, "terremotos": 1200.0, "seismic": 1200.0,
        "sismos": 1200.0,
        "overview": 150.0, "briefing": 150.0, "brief": 150.0, "todo": 150.0,
    }.get(action, 150.0)
    try:
        radius_km = float(parameters.get("radius_km") or default_radius)
    except (TypeError, ValueError):
        radius_km = default_radius

    try:
        if action in ("overview", "briefing", "brief", "todo"):
            return _briefing(located, radius_km, want_globe)
        if action in ("aircraft", "flights", "aviones", "vuela", "overhead"):
            return _aircraft(located, radius_km, want_globe)
        if action in ("earthquakes", "terremotos", "seismic", "sismos"):
            return _quakes(located, radius_km)
        if action in ("weather", "tiempo", "meteo", "clima"):
            return _weather(located)
    except wd.WorldDataError as exc:
        # The source failed. Say that, with the reason. Never fall back to
        # recalling something plausible from memory: a stale or invented number
        # is worse than an admitted failure, because the user cannot tell which
        # one they are hearing.
        return ("Ahora mismo no he podido consultar la fuente: %s. Puede que esté "
                "caída o saturada; inténtalo dentro de un momento." % exc)

    return ("No sé hacer eso todavía. Puedo mirar aviones, terremotos, el tiempo, "
            "los satélites con señal, o darte un resumen completo de un sitio. "
            "También puedo enseñarte qué fuentes están disponibles ahora.")


TOOL = {
    "name": "world_look",
    "description": (
        "Answers questions about what is happening in the world right now, from "
        "live public data: aircraft currently flying near a place (with callsign, "
        "distance, bearing, altitude and speed), earthquakes in the last 24 hours, "
        "satellites broadcasting a tracking signal, current weather, or a combined "
        "briefing on any city. Works with a place name, so 'what is flying over "
        "Jerez' or 'anything happening in Madrid' both work. "
        "If the user names a place, pass it as `place`. If they say 'here', 'near "
        "me' or nothing at all, ask which place rather than assuming one. "
        "Reports exactly what the sources returned, including when the answer is "
        "that nothing is there. Public broadcast data only: no private tracking of "
        "individuals."
    ),
    "parameters": {
        "type": "OBJECT",
        "properties": {
            "action": {
                "type": "STRING",
                "description": (
                    "aircraft | earthquakes | weather | satellites | overview | "
                    "sources"
                ),
            },
            "place": {
                "type": "STRING",
                "description": (
                    "City, town or landmark to look at, e.g. 'Jerez de la "
                    "Frontera', 'Madrid', 'Tokyo'. Required for everything "
                    "except 'satellites' and 'sources'."
                ),
            },
            "radius_km": {
                "type": "INTEGER",
                "description": (
                    "Search radius in kilometres around the place. Default 150 "
                    "(suitable for 'overhead'). Use up to 400 for aircraft, "
                    "500-2000 for earthquakes."
                ),
            },
            "show_on_map": {
                "type": "BOOLEAN",
                "description": (
                    "Set true when the user wants to SEE it, not just hear it — "
                    "'show me', 'open the map', 'dímelo y enséñamelo'. Opens the "
                    "globe window centred on the place."
                ),
            },
        },
        "required": ["action"],
    },
    "handler": world_look,
}
