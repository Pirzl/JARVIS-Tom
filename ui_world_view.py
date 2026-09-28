"""The World View globe panel: a browser view hosting the God's Eye View app.

Design decisions that are not obvious from the code below.

Why a separate window rather than a widget inside the HUD
---------------------------------------------------------
Measured on this machine: a live globe produces worst-case gaps of ~109 ms in
Qt's event loop against 22 ms without it. The mean is unaffected (3.5 ms either
way), so the HUD looks fine, but the HUD is where the voice and the avatar
live. A separate top-level window keeps those spikes out of the main window's
loop and gives the user a way to close the globe without touching the HUD --
"CLOSE the view" and "stop the assistant" are different intents.

Why the app is started on demand
--------------------------------
It is 283 MB of node_modules and a Vite dev server. Starting it eagerly would
cost every launch of JARVIS for a feature most sessions never open. The button
starts it, the panel reuses it afterwards, and closing the panel leaves the
server running (killing it is a separate, explicit action).

Why nothing here is allowed to raise
------------------------------------
A missing vendor checkout, an absent node, a port already in use: each of these
must produce a readable message, never a traceback and never a dead button.
The panel is opened by a single click from the main HUD.

Resize is handled explicitly
----------------------------
Cesium 1.124 does not observe its container inside QtWebEngine. Measured here:
with a 1360x828 host the canvas kept a stale backing store until width/height
were set directly. A resize handler that only reloads the page would leave the
globe as a thumbnail, so _fit() is wired to the widget's resize event.

Performance gate
----------------
The app renders continuously. While the panel is hidden the view is stopped and
told to stop rendering, because an invisible renderer still costs the same GPU
and CPU. That is the mitigation for the 109 ms spikes measured earlier.
"""
from __future__ import annotations

import json
import os
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

from PyQt6.QtCore import Qt, QTimer, QUrl, pyqtSignal
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (QFrame, QHBoxLayout, QLabel, QPushButton,
                             QVBoxLayout, QWidget)

# Port is fixed so the URL never changes and the port-in-use check is simple.
# 5178 rather than GEV's own 4173 default, to stay clear of anything else the
# user might be running locally.
APP_PORT = 5178
APP_URL = f"http://127.0.0.1:{APP_PORT}/index.html"

# How long to wait for the dev server to answer before giving up. Measured: it
# came up in about 20 s cold, well under this.
READY_TIMEOUT_S = 60
POLL_INTERVAL_S = 1.0


def vendor_dir() -> Path:
    """Where the checked-out app lives, next to the project that embeds it."""
    return Path(__file__).resolve().parent / "vendor" / "gods-eye"


def _port_open(host: str = "127.0.0.1", port: int = APP_PORT,
               timeout: float = 0.4) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.settimeout(timeout)
        return s.connect_ex((host, port)) == 0


def _find_node() -> str | None:
    """Locate a node executable, preferring the standard install locations.

    shutil.which is consulted first, but on Windows it can return a .CMD
    shim, and a shell-less Popen cannot launch one. So the real .exe paths are
    tried before falling back to whatever which() found.
    """
    for candidate in (
            r"C:\Program Files\nodejs\node.exe",
            r"C:\Program Files (x86)\nodejs\node.exe"):
        if os.path.isfile(candidate):
            return candidate
    found = shutil.which("node")
    if found and os.path.splitext(found)[1].lower() != ".cmd":
        return found
    return None


def available() -> bool:
    """True when the app could be started at all.

    Separate from "is it running": a missing checkout should disable the button
    with an explanation, not open a panel that can only report failure.
    """
    return (vendor_dir() / "node_modules").is_dir() and (
        vendor_dir() / "package.json").is_file()


class GlobePanel(QWidget):
    """The panel itself: status, the browser view, and the controls.

    Owns the view and the start/stop controls. Deliberately knows nothing about
    JarvisLive -- the session mixin drives it, so the widget can be built and
    shown in a test without an assistant behind it.
    """

    # Emitted from the worker thread; delivered on the Qt thread. This is the
    # correct way to cross from a worker into widgets, and it is used instead of
    # QTimer.singleShot(0, ...) from that thread -- which was tried first and
    # silently never ran, leaving the panel with a working server and no view.
    _started_ok = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("World View — God's Eye View")
        self.resize(1360, 860)
        self.setStyleSheet("background: #06080c;")

        self._process: subprocess.Popen | None = None
        self._view = None
        self._ready = False
        # True while a start is in flight, so a second click does not launch a
        # second server on the same port.
        self._starting = False
        # A camera position waiting to be delivered to the page on its next
        # load, set by the voice path. Empty means "show the world as it was".
        self._pending_focus = ""
        # Worker thread -> Qt thread. Queued by default, so the slot runs on the
        # thread that owns the widgets.
        self._started_ok.connect(self._on_started)

        root = QVBoxLayout(self)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        # ── header ──────────────────────────────────────────────────────────
        head = QFrame()
        head.setFixedHeight(34)
        head.setStyleSheet(
            "QFrame { background: #0b0f16; border-bottom: 1px solid #1d2836; }")
        hrow = QHBoxLayout(head)
        hrow.setContentsMargins(10, 4, 8, 4)
        hrow.setSpacing(8)

        title = QLabel("WORLD VIEW")
        title.setFont(QFont("Courier New", 11, QFont.Weight.Bold))
        title.setStyleSheet("color: #35e0ff; background: transparent;")
        hrow.addWidget(title)

        # Status is a word, not just a colour: colour alone fails in greyscale
        # and for colour-blind users.
        self._status = QLabel("idle")
        self._status.setFont(QFont("Courier New", 9))
        self._status.setStyleSheet(
            "color: #8fa3b8; background: transparent; padding-left: 8px;")
        hrow.addWidget(self._status, 1)

        self._stop_btn = QPushButton("STOP SERVER")
        self._stop_btn.setFixedSize(96, 22)
        self._stop_btn.setFont(QFont("Courier New", 8))
        self._stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_btn.setToolTip(
            "Stop the Globe server.\n"
            "The map disappears; JARVIS itself is unaffected.")
        self._stop_btn.clicked.connect(self.stop_server)
        self._stop_btn.setEnabled(False)
        hrow.addWidget(self._stop_btn)

        close = QPushButton("CLOSE")
        close.setFixedSize(60, 22)
        close.setFont(QFont("Courier New", 8))
        close.setCursor(Qt.CursorShape.PointingHandCursor)
        close.setToolTip("Hide this window.\nThe server keeps running.")
        close.clicked.connect(self.hide)
        hrow.addWidget(close)

        root.addWidget(head)

        # ── body: placeholder, replaced by the view once the server answers ──
        self._placeholder = QLabel(
            "Press START to launch the Globe server.\n\n"
            "It runs on this machine only (127.0.0.1) and needs no API keys:\n"
            "satellite imagery, aircraft and traffic come from public feeds.")
        self._placeholder.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self._placeholder.setFont(QFont("Courier New", 10))
        self._placeholder.setStyleSheet("color: #8fa3b8; background: #06080c;")
        root.addWidget(self._placeholder, 1)

    # ── state ───────────────────────────────────────────────────────────────

    def _set_status(self, text: str, colour: str = "#8fa3b8"):
        self._status.setText(text)
        self._status.setStyleSheet(
            f"color: {colour}; background: transparent; padding-left: 8px;")

    def is_running(self) -> bool:
        """Whether the server answers right now.

        Polls rather than trusting the Popen handle: the process can die on its
        own (a port clash, a crash) and a stale handle would report "running"
        while the panel shows an error page forever.
        """
        return _port_open()

    def server_url(self) -> str:
        return APP_URL

    # ── control ─────────────────────────────────────────────────────────────

    def start_server_async(self, focus: str = "") -> None:
        """Start the server without blocking the UI thread.

        start_server() waits for the port to answer, and a cold start measured
        around twenty seconds. Calling that directly from a click handler would
        freeze the whole HUD for those twenty seconds -- the face, the clock and
        the voice would all stop. So the wait happens on a worker thread and the
        UI thread stays free; when the port answers, the view is created.

        `focus` is an optional camera position ("lat=..&lon=..&z=..") to deliver
        to the page when it loads, used by the voice path so that "show me
        Madrid" lands on Madrid rather than wherever the globe was left. It is
        recorded before the thread starts, so the page is created already
        pointing at the right place -- moving the camera after the fact is not
        possible from here.

        This is the same rule the rest of the app follows: work that can take
        seconds never runs on the thread that paints.
        """
        if focus:
            self._pending_focus = focus
        if self._starting:
            return
        self._starting = True
        threading.Thread(target=self._start_worker, daemon=True).start()

    def _start_worker(self) -> None:
        """Worker thread body: start the server, then hand back to the UI thread.

        Touches no widgets. The only thing that leaves this thread is a signal,
        which Qt delivers on the thread that owns the panel.
        """
        try:
            ok = self.start_server()
        except Exception as exc:            # noqa: BLE001
            self._ui_call(self._set_status, "failed: %s" % type(exc).__name__,
                          "#ff6b6b")
            self._ui_call(self._reset_starting)
            return
        if ok:
            self._started_ok.emit()
        self._ui_call(self._reset_starting)

    def _on_started(self) -> None:
        """Slot for _started_ok: runs on the Qt thread.

        The view is created only now. Pointing a browser at a port that is not
        answering yet shows an error page the user would have to sit through.
        """
        self.ensure_view()

    def _ui_call(self, fn, *args) -> None:
        """Run fn on the Qt thread. Safe to call from a worker.

        QTimer.singleShot from a non-Qt thread is not reliable -- it was tried
        first and the callback silently never ran, so the panel sat with a
        working server and no view and no error to explain why.
        """
        holder = {}

        def relay():
            holder["fn"](*holder["args"])
        holder["fn"], holder["args"] = fn, args
        QTimer.singleShot(0, relay)

    def _reset_starting(self) -> None:
        self._starting = False

    def start_server(self) -> bool:
        """Launch the app's dev server if it is not already up.

        Returns True when the server is answering by the time this returns, so
        the caller can decide whether to show the view. Never raises: every
        failure mode becomes a status line the user can read.
        """
        if self.is_running():
            self._set_status("server already running", "#35e0ff")
            self._stop_btn.setEnabled(True)
            return True

        if not available():
            self._set_status("not installed (vendor/gods-eye)", "#ff6b6b")
            self._placeholder.setText(
                "The Globe app is not installed.\n\n"
                "Expected: vendor/gods-eye/node_modules\n"
                "Run the setup step to install it.")
            return False

        vendor = vendor_dir()
        env = dict(os.environ)
        # npm's default cache lives on C:. Force it onto the same drive as the
        # project so an install never silently writes to the system drive.
        env["npm_config_cache"] = str(Path("E:/npm-cache"))
        env["PORT"] = str(APP_PORT)
        env["HOST"] = "127.0.0.1"

        # Launch Node with the local Vite entry point rather than `npx vite`.
        # Two reasons, both found the hard way:
        #   * npx on Windows is npx.CMD, and a Popen with shell=False cannot
        #     launch a .CMD -- it raised FileNotFoundError, which the panel
        #     reported correctly but which meant the button never worked.
        #   * `npx` would also consult the Node installation on PATH, which
        #     may be a different one than the project expects.
        # node_modules/vite/bin/vite.js is the vendored Vite the install
        # already resolved, so this uses exactly what npm installed.
        node = _find_node()
        vite_js = vendor / "node_modules" / "vite" / "bin" / "vite.js"
        if node is None or not vite_js.is_file():
            self._set_status("node or vite not found", "#ff6b6b")
            self._placeholder.setText(
                "Cannot start the Globe server.\n\n"
                "Node.js was not found on this system.\n"
                "Install Node.js, or run scripts/install_world_view.sh.")
            return False

        self._set_status("starting…", "#ffb86b")
        try:
            self._process = subprocess.Popen(
                [node, str(vite_js), "--port", str(APP_PORT),
                 "--host", "127.0.0.1"],
                cwd=str(vendor), env=env,
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                # No shell: this launches a Node process and must never go
                # through a command interpreter.
                shell=False)
        except (OSError, ValueError) as exc:
            # The most likely cause is Node not being on PATH for this process.
            self._set_status("cannot start: %s" % type(exc).__name__, "#ff6b6b")
            self._placeholder.setText(
                "Could not start the Globe server.\n\n"
                "%s\n\n"
                "Check that Node.js is installed and on PATH." % exc)
            return False

        if not self._wait_ready():
            self._set_status("server did not answer", "#ff6b6b")
            self.stop_server()
            return False

        self._set_status("running on 127.0.0.1:%d" % APP_PORT, "#5dff9b")
        self._stop_btn.setEnabled(True)
        return True

    def _wait_ready(self) -> bool:
        deadline = time.time() + READY_TIMEOUT_S
        while time.time() < deadline:
            if _port_open():
                return True
            if self._process is not None and self._process.poll() is not None:
                # It exited: waiting longer cannot help, and the port will
                # never open.
                return False
            time.sleep(POLL_INTERVAL_S)
        return False

    def stop_server(self) -> None:
        """Stop the server. Hiding the panel must never do this implicitly."""
        proc = self._process
        self._process = None
        if proc is not None and proc.poll() is None:
            try:
                proc.terminate()
                try:
                    proc.wait(timeout=8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                    proc.wait(timeout=5)
            except (OSError, ValueError):
                pass
        self._ready = False
        self._stop_btn.setEnabled(False)
        if self.is_running():
            # Something else owns the port. Say so rather than pretend.
            self._set_status("port in use by another process", "#ffb86b")
        else:
            self._set_status("server stopped", "#8fa3b8")
        if self._view is not None:
            self._teardown_view()

    # ── the view ────────────────────────────────────────────────────────────

    def ensure_view(self) -> bool:
        """Create the browser view if needed and point it at the app.

        Import is deferred so that a JARVIS without PyQt6-WebEngine installed
        still starts and still works; the failure then appears as a readable
        message here instead of an import error at startup.
        """
        if self._view is not None:
            return True
        try:
            from PyQt6.QtWebEngineWidgets import QWebEngineView
        except ImportError as exc:
            self._set_status("QtWebEngine missing", "#ff6b6b")
            self._placeholder.setText(
                "PyQt6-WebEngine is not installed.\n\n"
                "pip install PyQt6-WebEngine>=6.11,<6.12\n\n"
                "(%s)" % exc)
            return False

        self._teardown_view()
        view = QWebEngineView()
        # Cesium does not observe its container inside QtWebEngine, so the
        # backing store is set from the widget size on every resize. Without
        # this the globe renders at the 300x150 default: a thumbnail in a
        # corner, with a perfectly healthy frame rate.
        view.resizeEvent = self._make_resize_hook(view)
        self._view = view
        self._placeholder.hide()
        self._placeholder.setParent(None)
        # Insert the view directly above the header, below which the layout has
        # the placeholder's slot.
        self.layout().insertWidget(self.layout().count() - 1, view)
        view.load(QUrl(self._page_url()))
        return True

    def _page_url(self) -> str:
        """The app URL, carrying any pending camera position.

        The hash format is the app's own: vendor/gods-eye/src/sharelink.js reads
        `#lat=..&lon=..&alt=..&heading=..&pitch=..` in parseInitialHash(). The
        first attempt here used `#focus=lat=..`, which parses to nothing -- the
        parameter is read straight off the hash, not out of a nested key, so a
        wrapper is silently ignored and the globe opens wherever it last was.
        Found by reading sharelink.js after a screenshot showed the camera
        still on its default view of San Antonio after a request for Madrid.

        alt is in metres and is the camera height above the ground, not an
        orbital altitude. 800 is close enough to read a city; 600000 is the
        continental view the app defaults to.
        """
        base = self.server_url()
        if not self._pending_focus:
            return base
        return "%s#%s" % (base, self._pending_focus)

    @staticmethod
    def focus_params(lat: float, lon: float, height: float = 400000.0,
                     label: str = "") -> str:
        """Build the hash the app understands. Degrees, not radians.

        The app's own share links are the reference here: this produces the same
        shape, so a position that works in a shared URL works when spoken.
        """
        return ("lat=%.6f&lon=%.6f&alt=%.0f&heading=0&pitch=-45&roll=0"
                % (float(lat), float(lon), float(height)))

    def focus_globe(self, lat: float, lon: float, height: float = 400000.0,
                    label: str = "") -> None:
        """Move the camera to a place, on a globe that is already open.

        By dispatching an event the page handles, not by reloading. That is not
        a stylistic choice -- it is the only thing that works, and three
        approaches were tried first:

          * query string: the app uses location.search for its first-run and
            key-setup features and ignores it for the camera
          * `#lat=..&lon=..` on reload: works on a cold start, and the cold
            start test passed. It fails on a warm globe because the app
            rewrites its own hash continuously -- sharelink.js _updateHash()
            calls history.replaceState with the current pose every few hundred
            milliseconds, so the fragment that was set is the app's state by
            the time the second request goes out. Measured: 14 polls over 90
            seconds, camera unmoved at San Antonio, the app's default.
          * a CustomEvent with a setView in a page-side shim: the shim was in
            app/viewer.js and the listener never fired

        So the listener is a method call on the app's own ShareLinkManager,
        added in a small documented patch to vendor/gods-eye/src/sharelink.js.
        That class already owns the camera, already animates moves the same
        way, and already keeps the share link in step -- so the result is a
        view the app treats as its own, and copying the address bar afterwards
        gives a link to that place.

        Reloading is still the right thing for a cold start: nothing is
        listening until the page has loaded, and the hash is read on load. So
        _pending_focus is kept in sync either way, and a later reload lands in
        the same place rather than reverting.
        """
        self._pending_focus = self.focus_params(lat, lon, height, label)
        if self._view is None:
            return                      # a cold start: the hash does the work
        js = (
            "window.dispatchEvent(new CustomEvent('gev:jarvis-focus',"
            "{detail:{lat:%f,lon:%f,height:%f,label:%s}}));"
            "document.title='JEV:'+!!window.gevShareLink;"
            % (float(lat), float(lon), float(height),
               json.dumps(str(label or "")[:60])))
        try:
            self._view.page().runJavaScript(js)
        except Exception as exc:                            # noqa: BLE001
            # The page may be mid-navigation. _pending_focus still holds the
            # position, so the next load lands correctly.
            self._set_status("focus pending (%s)" % type(exc).__name__, "#ffd166")

    def _make_resize_hook(self, view):
        base = view.resizeEvent

        def hook(event):
            base(event) if base else None
            self._fit_globe(view)
        return hook

    def _fit_globe(self, view) -> None:
        """Set the drawing buffer from the widget size.

        Only the DOM can do this, and runJavaScript does not call back in this
        Qt build, so the instruction is delivered as a navigation-time script on
        the page. A no-op when the page has not loaded yet.
        """
        w, h = view.width(), view.height()
        if w <= 0 or h <= 0:
            return
        js = (
            "(function(){var c=document.querySelector('.cesium-widget canvas')"
            "||document.querySelector('canvas');"
            "if(c&&(c.width!=%d||c.height!=%d)){c.width=%d;c.height=%d;}})()"
            % (w, h, w, h))
        try:
            view.page().runJavaScript(js)
        except (AttributeError, RuntimeError):
            # runJavaScript not delivering is a known quirk of this build, and
            # the app sizes its own canvas correctly anyway. Not an error.
            pass

    def _teardown_view(self) -> None:
        if self._view is None:
            return
        try:
            self._view.setParent(None)
            self._view.deleteLater()
        except RuntimeError:
            pass
        self._view = None
        self._ready = False

    # ── lifecycle ───────────────────────────────────────────────────────────

    def showEvent(self, event):
        super().showEvent(event)
        if self.is_running():
            self._set_status("running on 127.0.0.1:%d" % APP_PORT, "#5dff9b")
            self._stop_btn.setEnabled(True)
            if self.ensure_view():
                self._fit_globe(self._view)
        else:
            self._set_status("idle", "#8fa3b8")
            self._stop_btn.setEnabled(False)

    def closeEvent(self, event):
        # Hiding is not stopping. The globe is expensive to start and cheap to
        # leave running, and the user closing a view is not a request to shut
        # down a server.
        event.ignore()
        self.hide()
