"""NASA's image library, as something JARVIS can actually be asked about.

`api.nasa.gov` is the public image and video archive: hundreds of thousands of
photographs, most of them from missions that are long finished, indexed and
searchable without a key. `images-api.nasa.gov` is the search front end for it,
and returns JSON with a direct link to each asset.

Two things about this integration are worth stating plainly, because both cost
real time to discover.

**The key is a header, not a parameter.** `?api_key=...` is rejected with
`Unacceptable search parameter: api_key` -- a 400 that reads like a wrong key
and is not one. The key goes in the `X-API-Key` header, or in no header at
all: the search endpoint answers 200 without one, rate-limited by IP.

**This key is not a FIRMS key.** They are both "NASA API keys" and they are
for different services. A key registered at api.nasa.gov returns
`Invalid MAP_KEY` from FIRMS, which is confusing only because NASA's own
signup makes them look interchangeable. So the fire detection keeps its own
key and its own error, and this one never touches it -- see `fires_near` in
`world_data.py` for the other half of that story.

The key is read from the Globe app's ignored `.env` like the FIRMS one, and
never cached, so correcting it takes effect on the next question.
"""
from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

GEV_ENV = (Path(__file__).resolve().parent.parent
           / "vendor" / "gods-eye" / ".env")

BASE = "https://images-api.nasa.gov"
ASSETS = "https://images-assets.nasa.gov/image"

DEFAULT_TIMEOUT = 25


def nasa_api_key() -> str:
    """The api.nasa.gov key, read fresh from the ignored .env.

    Both names are accepted. `NASA_API_KEY` is the correct one; `FIRMS_MAP_KEY`
    is there because the same file has carried it since before this module
    existed, and the value registered at api.nasa.gov was pasted there. Reading
    it from under either name is deliberate, but never write FIRMS code that
    trusts it: the two services reject each other's keys.
    """
    try:
        text = GEV_ENV.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        if name.strip().upper() in ("NASA_API_KEY", "FIRMS_MAP_KEY"):
            key = value.strip().strip("'\"")
            if key:
                return key
    return ""


def _stamp() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _get(url: str, timeout: int = DEFAULT_TIMEOUT) -> tuple[int, bytes]:
    """Fetch, with the key in the header rather than the query string.

    Passing it as a parameter is not a style preference: the endpoint rejects
    the unknown parameter outright, so a key appended to the URL turns a
    working request into a 400 that looks like a bad credential.
    """
    req = urllib.request.Request(url, headers={
        "User-Agent": "JARVIS/1.0",
        "Accept": "application/json",
    })
    key = nasa_api_key()
    if key:
        req.add_header("X-API-Key", key)
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.status, resp.read()


# Search terms that need no external source, and the media types the archive
# actually carries. NASA is a photography archive first: `image` and `video`
# cover almost everything worth showing, and asking for an unsupported type
# returns an empty collection rather than an error.
MEDIA_TYPES = ("image", "video")


def search_images(query: str, media_type: str = "image", limit: int = 8,
                  page_size: int | None = None) -> dict:
    """Search NASA's public media archive.

    Returns a dict, never raises, and always carries `ok`. A spoken answer has
    to survive the network being down, and "I could not reach NASA" is a
    different sentence from "NASA has nothing on that".
    """
    query = (query or "").strip()
    if not query:
        return {"ok": False, "reason": "empty query", "items": [],
                "queried_at": _stamp()}
    if media_type not in MEDIA_TYPES:
        media_type = "image"
    size = page_size or max(1, min(int(limit or 8), 100))
    url = (f"{BASE}/search?q={urllib.parse.quote(query)}"
           f"&media_type={media_type}&page_size={size}")
    try:
        status, body = _get(url)
    except urllib.error.HTTPError as exc:
        detail = exc.read()[:200].decode("utf-8", "replace").strip()
        return {"ok": False, "status": exc.code, "reason": detail or
                f"HTTP {exc.code}", "items": [], "query": query,
                "queried_at": _stamp()}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}",
                "items": [], "query": query, "queried_at": _stamp()}

    try:
        data = json.loads(body)
    except ValueError:
        return {"ok": False, "reason": "NASA returned something that is not "
                                       "JSON", "items": [], "query": query,
                "queried_at": _stamp()}

    collection = (data.get("collection") or {})
    items = []
    for entry in (collection.get("items") or [])[: int(limit or 8)]:
        meta = (entry.get("data") or [{}])[0]
        links = entry.get("links") or [{}]
        thumb = links[0].get("href", "") if links else ""
        nasa_id = meta.get("nasa_id", "")
        # Prefer a medium still for video, where `~thumb.jpg` is all the
        # manifest offers and would look like a broken image in a panel.
        if media_type == "video" and thumb.endswith("~thumb.jpg"):
            thumb = f"{ASSETS}/{nasa_id}/{nasa_id}~medium.jpg"
        items.append({
            "nasa_id": nasa_id,
            "title": (meta.get("title") or "").strip(),
            "description": re.sub(r"\s+", " ", meta.get("description") or "")
                             .strip()[:400],
            "date": (meta.get("date_created") or "")[:10],
            "center": meta.get("center", ""),
            "media_type": media_type,
            "thumb": thumb,
            "full": f"{ASSETS}/{nasa_id}/{nasa_id}~orig.jpg"
                    if media_type == "image" else f"{ASSETS}/{nasa_id}/",
            "keywords": [k.strip() for k in (meta.get("keywords") or [])
                         if k.strip()][:8],
        })

    total_hits = (data.get("collection") or {}).get("metadata", {})
    return {
        "ok": True,
        "query": query,
        "media_type": media_type,
        "count": len(items),
        "total_hits": total_hits.get("total_hits"),
        "items": items,
        "source": "NASA Image Library (images-api.nasa.gov)",
        "queried_at": _stamp(),
    }


def asset_manifest(nasa_id: str) -> dict:
    """Every file that makes up one asset, from `/asset/{nasa_id}`.

    Worth having for a large original: the search result links a thumbnail and
    the manifest lists the real file, sometimes a 6000-pixel TIFF. Answering
    "give me the biggest one" is what this is for.
    """
    nasa_id = (nasa_id or "").strip()
    if not nasa_id:
        return {"ok": False, "reason": "no asset id", "files": []}
    try:
        status, body = _get(f"{BASE}/asset/{urllib.parse.quote(nasa_id)}")
        data = json.loads(body)
    except urllib.error.HTTPError as exc:
        return {"ok": False, "status": exc.code,
                "reason": exc.read()[:150].decode("utf-8", "replace"),
                "files": []}
    except Exception as exc:  # noqa: BLE001
        return {"ok": False, "reason": f"{type(exc).__name__}: {exc}",
                "files": []}
    hrefs = ((data.get("collection") or {}).get("items") or [{}])[0].get("href", "")
    files = []
    if hrefs:
        try:
            for f in _get(hrefs)[1].decode("utf-8", "replace").splitlines():
                f = f.strip()
                if f:
                    files.append(f"https://images-assets.nasa.gov/{nasa_id}/{f}")
        except Exception:  # noqa: BLE001
            files = []
    return {"ok": bool(files), "nasa_id": nasa_id, "files": files,
            "queried_at": _stamp()}


def source_status() -> dict:
    """For doctor.py and the voice prompt.

    Deliberately does not make a request. A health check that calls the
    internet turns a working, correctly-configured machine into a red one every
    time the network blips, and the fix people reach for is to disable the
    check. It reports what is configured, and `search_images` reports what is
    reachable.
    """
    key = nasa_api_key()
    return {
        "name": "NASA Image Library",
        "configured": bool(key),
        # The key improves the rate limit; it is not required for search.
        "key_required": False,
        "note": ("api.nasa.gov key present; search works without it"
                 if key else
                 "no api.nasa.gov key; search still works, rate-limited by IP"),
        "endpoint": BASE,
    }
