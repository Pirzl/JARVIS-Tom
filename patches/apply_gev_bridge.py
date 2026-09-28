"""Reapply the JARVIS bridge to a fresh God's Eye View checkout.

The bridge is three things added to vendor/gods-eye/src/sharelink.js: a
window handle on the ShareLinkManager, a listener for `gev:jarvis-focus` and
another for `gev:jarvis-markers`, and the two methods `jarvisFocus` and
`jarvisMarkers` that move the camera and draw the points.

That file belongs to a third-party repository, bilawalsidhu/gods-eye-view, and
the whole vendor/ tree is gitignored -- correctly, since it is someone else's
code and 283 MB of node_modules. The consequence is that these 176 added lines
exist nowhere in this repository. Delete the checkout, clone it again, and the
globe opens, moves and draws, and the markers never appear, with no error
anywhere to say why. The Python side is committed; the JavaScript it talks to
was not.

So it is kept here as a patch, and this script applies it. Run it after any
fresh clone or re-clone of the vendor tree:

    python patches/apply_gev_bridge.py

Safe to run twice: it detects an already-patched file and does nothing rather
than adding the bridge twice and breaking the class.

What it deliberately does not do is vendor the upstream file. Keeping the patch
means the two sides can be diffed, and when upstream eventually moves, the
conflict is a normal merge conflict in a normal file rather than a mystery.
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
VENDOR = REPO / "vendor" / "gods-eye"
PATCH = HERE / "gods-eye-jarvis-bridge.patch"
TARGET = "src/sharelink.js"

# A line only the patched file has. Used to detect an already-patched checkout
# without applying twice, which would duplicate the methods and the listeners.
SENTINEL = "jarvisFocus(detail)"
SENTINEL2 = "gev:jarvis-markers"


def already_patched() -> bool:
    target = VENDOR / TARGET
    if not target.exists():
        return False
    text = target.read_text(encoding="utf-8", errors="replace")
    return SENTINEL in text and SENTINEL2 in text


def main() -> int:
    if not PATCH.exists():
        print("falta el patch: %s" % PATCH)
        return 1
    if not VENDOR.exists():
        print("no hay checkout de GEV en %s" % VENDOR)
        print(" clonalo primero (ver README de la fase 1A)")
        return 1
    if already_patched():
        print("el puente ya esta aplicado -- no hago nada")
        return 0

    try:
        result = subprocess.run(
            ["git", "apply", "--verbose", str(PATCH)],
            cwd=str(VENDOR), capture_output=True, text=True)
    except FileNotFoundError:
        print("no encuentro git en el PATH")
        return 1

    if result.returncode != 0:
        print("git apply fallo:\n%s" % (result.stderr or result.stdout))
        print("\nSi upstream cambio sharelink.js, el conflicto es real y hay")
        print("que resolverlo a mano. No lo fuerces con --3way sin mirar.")
        return 1

    if not already_patched():
        # git apply can succeed on a partial hunk in ways the sentinel catches.
        print("el patch se aplico pero el puente no esta completo")
        return 1
    print("puente JARVIS aplicado a %s" % (VENDOR / TARGET))
    print("  camara:   gev:jarvis-focus  -> jarvisFocus()")
    print("  marcadores: gev:jarvis-markers -> jarvisMarkers()")
    return 0


if __name__ == "__main__":
    sys.exit(main())
