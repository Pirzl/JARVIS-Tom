"""
ui_deep_eye.py — the Deep Eye panel shown over the assistant's face.

A scan takes minutes and streams lines of output the whole time. Where that
output goes decides whether the feature is usable:

  * nowhere      — JARVIS freezes and the user cannot tell a running scan from
                   a hung one, so they press the button again;
  * the log      — correct, but buries the findings among progress chatter;
  * here         — a panel over the HUD, streaming live, that reports the
                   outcome in plain language when it finishes.

The last one is why this is a panel rather than a status line. Legibility wins
over minimalism here: a wall of raw scanner output is not a feature.

Colour is carried by text and borders, never by hue alone — the finding counts
are labelled CRITICAL / HIGH / MEDIUM / LOW so the information survives
greyscale and colour-blindness.
"""

from __future__ import annotations

import re

from PyQt6.QtCore import Qt, QTimer
from PyQt6.QtGui import QFont
from PyQt6.QtWidgets import (
    QFrame, QHBoxLayout, QLabel, QLineEdit, QProgressBar, QPushButton,
    QTextEdit, QVBoxLayout, QWidget,
)

# ANSI box-drawing and emoji arrive in deep-eye's output; Qt renders the second
# as tofu boxes at 8pt, and the first is noise. Strip both for display only —
# the raw text is kept for the report.
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]")
_BOX = re.compile(r"[\u2500-\u257f\u2580-\u259f]")
_EMOJI = re.compile(
    "[\U0001F300-\U0001FAFF\u2600-\u27BF\u2B00-\u2BFF\uFE0F\u2190-\u21FF\u2B50]")


def clean_line(raw: str) -> str:
    """Make one scanner line readable in a small monospace panel."""
    line = _ANSI.sub("", raw or "")
    line = _EMOJI.sub("", line)
    line = _BOX.sub("", line)
    # deep-eye pads its tables with box characters; collapse the runs of space
    # that leaves so the text does not wrap for no reason.
    line = re.sub(r"[ \t]{2,}", "  ", line).rstrip()
    return line.strip()


class DeepEyePanel(QFrame):
    """Live scan output plus a verdict. Parentless; reparented on show."""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("DeepEyePanel")
        self.setStyleSheet("""
            QFrame#DeepEyePanel {
                background: rgba(6, 12, 20, 242);
                border: 1px solid #1f4a63;
                border-radius: 6px;
            }
            QLabel#deTitle   { color: #4fc3f7; font-family: 'Courier New'; font-size: 10pt; font-weight: bold; }
            QLabel#deTarget  { color: #b0bec5; font-family: 'Courier New'; font-size: 8pt; }
            QLabel#deStatus  { color: #8bc34a; font-family: 'Courier New'; font-size: 8pt; }
            QLabel#deVerdict { color: #e0e0e0; font-family: 'Courier New'; font-size: 9pt; }
            QLabel#deIdle   { color: #607d8b; font-family: 'Courier New'; font-size: 8pt; }
            QLineEdit#deTargetEdit {
                background: #04080e; color: #b0bec5; border: 1px solid #1f4a63;
                border-radius: 3px; font-family: 'Courier New'; font-size: 8pt;
                padding: 3px 5px;
            }
            QLineEdit#deTargetEdit:focus { border-color: #4fc3f7; }
            QTextEdit        { background: #04080e; color: #7fb3c8;
                               border: 1px solid #143244; border-radius: 4px;
                               font-family: 'Courier New'; font-size: 8pt; }
            QProgressBar     { background: #0d1a24; border: 1px solid #143244;
                               border-radius: 3px; height: 8px; }
            QProgressBar::chunk { background: #4fc3f7; }
            QPushButton      { background: transparent; color: #78909c;
                               border: 1px solid #143244; border-radius: 3px;
                               font-family: 'Courier New'; font-size: 8pt; }
            QPushButton:hover      { color: #4fc3f7; border-color: #4fc3f7; }
            QPushButton#deStop:hover { color: #ef5350; border-color: #ef5350; }
            QPushButton#deScan   { color: #4fc3f7; border-color: #1f4a63;
                                   padding: 0 10px; }
            QPushButton#deScan:hover   { background: #0d2836; }
            QPushButton#deScan:disabled { color: #37474f; border-color: #14232c; }
        """)

        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 10, 12, 10)
        lay.setSpacing(6)

        self._title = QLabel("DEEP EYE  ·  SECURITY SCAN")
        self._title.setObjectName("deTitle")
        lay.addWidget(self._title)

        self._target = QLabel("")
        self._target.setObjectName("deTarget")
        self._target.setWordWrap(True)
        lay.addWidget(self._target)

        self._status = QLabel("Idle")
        self._status.setObjectName("deStatus")
        lay.addWidget(self._status)

        # The scanner reports no percentage, so this is an activity indicator,
        # not a real progress bar. Labelled as such — a progress bar that lies
        # about how far along it is worse than a spinner.
        self._pulse = QProgressBar()
        self._pulse.setRange(0, 0)          # indeterminate
        self._pulse.setTextVisible(False)
        self._pulse.setVisible(False)
        lay.addWidget(self._pulse)

        self._log = QTextEdit()
        self._log.setReadOnly(True)
        self._log.setMinimumHeight(150)
        # No wrap: scanner lines are column-aligned tables, and wrapping them
        # destroys the alignment that makes them readable. The enum lives on
        # QTextEdit itself in PyQt6 (QTextEdit.LineWrap.NoWrap), not on a
        # separate class as in some other bindings.
        self._log.setLineWrapMode(QTextEdit.LineWrapMode.NoWrap)
        lay.addWidget(self._log, 1)

        # ── the idle state ────────────────────────────────────────────────
        # An empty panel is indistinguishable from a broken one. The first
        # version opened to a blank log and a bare "Idle", which looked
        # exactly like a feature that failed to start. The panel now says
        # what it is, what it will do, and offers the one thing you can do
        # from here.
        self._idle = QLabel(
            "No scan running.\n\n"
            "Type a site you own or have permission to audit, "
            "then press SCAN.\n\n"
            "JARVIS will always ask you to confirm before it "
            "touches any server."
        )
        self._idle.setObjectName("deIdle")
        self._idle.setWordWrap(True)
        self._idle.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        lay.addWidget(self._idle)

        self._row_target = QLabel("Site:")
        self._row_target.setObjectName("deTarget")
        self._target_edit = QLineEdit()
        self._target_edit.setObjectName("deTargetEdit")
        self._target_edit.setPlaceholderText("ejemplo.com")
        self._target_edit.returnPressed.connect(lambda: self._start_clicked())
        self._scan_btn = QPushButton("SCAN")
        self._scan_btn.setObjectName("deScan")
        self._scan_btn.setFixedHeight(26)
        self._scan_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._scan_btn.clicked.connect(self._start_clicked)
        ent_row = QHBoxLayout()
        ent_row.setSpacing(6)
        ent_row.addWidget(self._row_target)
        ent_row.addWidget(self._target_edit, 1)
        ent_row.addWidget(self._scan_btn)
        lay.addLayout(ent_row)

        self._verdict = QLabel("")
        self._verdict.setObjectName("deVerdict")
        self._verdict.setWordWrap(True)
        self._verdict.setVisible(False)
        lay.addWidget(self._verdict)

        row = QHBoxLayout()
        row.setSpacing(6)
        row.addStretch()
        self._stop_btn = QPushButton("STOP SCAN")
        self._stop_btn.setObjectName("deStop")
        self._stop_btn.setFixedHeight(24)
        self._stop_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._stop_btn.clicked.connect(self._on_stop)
        self._stop_btn.setVisible(False)
        row.addWidget(self._stop_btn)

        self._close_btn = QPushButton("CLOSE")
        self._close_btn.setFixedHeight(24)
        self._close_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._close_btn.clicked.connect(self.close_panel)
        row.addWidget(self._close_btn)
        lay.addLayout(row)

        self.on_stop = None          # set by the owner
        self.on_request = None       # set by the owner: (target) -> None
        self._tick = QTimer(self)
        self._tick.timeout.connect(self._nudge_pulse)
        self._phase = 0
        # True between begin() and end(). The header button consults it so a
        # reopen during a scan does not wipe the output the user is watching.
        self._running = False
        self._show_idle()

    def _nudge_pulse(self) -> None:
        """The indeterminate bar is enough on its own; this only repaints so
        the activity reads as alive on themes that animate chunk colour."""
        self._phase = (self._phase + 1) % 4
        self._pulse.setFormat("")

    # ── lifecycle ──────────────────────────────────────────────────────────

    def _show_idle(self) -> None:
        """The resting state: instructions up, log and verdict down.

        A log box sitting empty above a blank verdict is what made the first
        version look broken, so the log is hidden until there is something in
        it. The two states are mutually exclusive by construction — a run
        cannot leave an empty log on screen pretending to be idle.
        """
        self._idle.setVisible(True)
        self._log.setVisible(False)
        self._verdict.setVisible(False)
        self._pulse.setVisible(False)
        self._tick.stop()
        self._stop_btn.setVisible(False)
        self._status.setText("Idle")
        self._target.setText("")

    def _start_clicked(self) -> None:
        """SCAN pressed. This does NOT start a scan.

        It hands the target to the owner, which routes it through the same
        confirmation gate as the voice path. The button is a way of asking, not
        a way of doing — a scan of somebody's server must never begin from a
        click without the HUD asking Thomas first.
        """
        target = self._target_edit.text().strip()
        if not target:
            self._status.setText("Type a site first")
            self._target_edit.setFocus()
            return
        self._scan_btn.setEnabled(False)
        self._status.setText("Asking for confirmation...")
        if callable(self.on_request):
            try:
                self.on_request(target)
            except Exception as e:
                self._scan_btn.setEnabled(True)
                self._status.setText(f"Could not start: {e}")
            return
        # No owner wired: say so rather than sitting silent forever.
        self._scan_btn.setEnabled(True)
        self._status.setText("Not available")

    def begin(self, target: str) -> None:
        self._running = True
        self._idle.setVisible(False)
        self._log.setVisible(True)
        self._scan_btn.setEnabled(False)
        self._target_edit.setText(target)
        self._log.clear()
        self._verdict.setVisible(False)
        self._verdict.clear()
        self._target.setText(f"Target:  {target}")
        self._status.setText("Scanning…")
        self._pulse.setVisible(True)
        self._stop_btn.setVisible(True)
        self._tick.start(120)
        self.raise_()

    def end(self, ok: bool, message: str) -> None:
        """The run is over. Keep the output and the verdict on screen — the
        user needs to read them — and re-enable the target row so a second
        audit can be started without closing anything."""
        self._running = False
        self._pulse.setVisible(False)
        self._tick.stop()
        self._stop_btn.setVisible(False)
        self._scan_btn.setEnabled(True)
        self._status.setText("Done" if ok else "Stopped")
        self._verdict.setText(message)
        self._verdict.setVisible(True)
        self.raise_()

    def reset(self) -> None:
        """Back to the resting state, with the log emptied.

        Without the clear(), reopening the panel after a finished scan showed
        the previous run's output next to a fresh status line, which reads as
        the new scan having already found the same things.
        """
        self._log.clear()
        self._target_edit.clear()
        self._show_idle()

    def close_panel(self) -> None:
        """Closing the panel hides it; it must not stop a running scan, which
        the user may still be watching through the log or waiting on. To stop
        a scan, press STOP — two different intents, two different controls."""
        self.hide()
        btn = getattr(self.parent(), "_deep_eye_btn", None)
        if btn is not None:
            btn.setChecked(False)

    def _on_stop(self) -> None:
        if callable(self.on_stop):
            try:
                self.on_stop()
            except Exception:
                pass

    # ── content ────────────────────────────────────────────────────────────

    def append_line(self, raw: str) -> None:
        """Add one scanner line.

        No thread hop here on purpose. The reader thread never calls this
        directly — it goes through JarvisUI.deep_eye_line, which emits a
        queued signal, so this always runs on the Qt thread already. An extra
        bounce would add a frame of latency to every one of several hundred
        lines for nothing.
        """
        text = clean_line(raw)
        if not text:
            return
        # Keep the document from growing without bound on a long scan.
        self._log.document().setMaximumBlockCount(500)
        self._log.append(text)
        bar = self._log.verticalScrollBar()
        bar.setValue(bar.maximum())

    def show_findings(self, result) -> None:
        """Render a finished ScanResult as labelled counts."""
        self._running = False
        self._pulse.setVisible(False)
        self._tick.stop()
        self._stop_btn.setVisible(False)
        self._scan_btn.setEnabled(True)
        self._status.setText("Done" if result.ok else "Finished with errors")

        by_sev: dict = {}
        for f in getattr(result, "findings", []) or []:
            sev = str(f.get("severity", "unknown")).upper()
            by_sev[sev] = by_sev.get(sev, 0) + 1

        if not by_sev:
            self._verdict.setText(
                "No findings. Either the site is clean for the checks that ran, "
                "or they did not reach it.")
            self._verdict.setVisible(True)
            return

        # Labelled, not colour-coded: the word is the information.
        order = [s for s in ("CRITICAL", "HIGH", "MEDIUM", "LOW", "INFO")
                 if s in by_sev]
        counts = "   ".join(f"{s}: {by_sev[s]}" for s in order)
        self._verdict.setText(f"{len(getattr(result, 'findings', []))} findings\n{counts}")
        self._verdict.setVisible(True)
