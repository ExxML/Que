import html

from PyQt6.QtCore import Qt, QTimer, pyqtSignal
from PyQt6.QtGui import QFont, QFontMetrics, QResizeEvent
from PyQt6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from core.transcriber import SOURCE_LABELS, Transcriber
from ui.theme import PRIMARY_COLOR, SPEAKING_COLOR, qss

HEADER_HEIGHT = 30
BODY_HEIGHT = 150
TOP_MARGIN = 4  # The input field's own top margin spaces the panel from the input


class TranscriptPanel(QWidget):
    """Show live speaker indicators, a caption, and an expandable transcript above the input.

    The panel is docked in the window layout, so it never covers chat text and needs
    no fill of its own; the window background shows through unchanged.
    """

    size_changed = pyqtSignal()  # Wanted visibility or height changed

    def __init__(self, transcriber: Transcriber, main_window: QWidget) -> None:
        super().__init__(main_window)
        self.transcriber = transcriber
        self.expanded = False
        self.status = ""
        self.speaking: dict[str, bool] = dict.fromkeys(SOURCE_LABELS, False)
        self.indicators = {source: QLabel() for source in SOURCE_LABELS}
        self.caption = QLabel()
        self.toggle_button = QPushButton()
        self.body = QScrollArea()
        self.text = QLabel()
        self.shown = False  # Last wanted visibility reported through size_changed
        self._init_UI()
        transcriber.changed.connect(self._on_transcript_changed)
        transcriber.recording_changed.connect(lambda _recording: self._refresh())
        transcriber.speaking_changed.connect(self._on_speaking_changed)
        transcriber.status_changed.connect(self._on_status_changed)
        transcriber.download_finished.connect(self._on_download_finished)
        self._refresh()

    def wanted(self) -> bool:
        """Report whether there is anything to show.

        Returns:
            bool: True while recording, while transcript lines exist, or while a model is missing.
        """
        return (
            self.transcriber.recording
            or bool(self.transcriber.segments)
            or self.transcriber.missing_model
        )

    def resizeEvent(self, event: QResizeEvent | None) -> None:
        """Re-elide the caption to the new width.

        Args:
            event (QResizeEvent): The resize event.
        """
        super().resizeEvent(event)
        self._refresh_caption()

    def _init_UI(self) -> None:
        """Build the framed header row and the collapsible transcript body."""
        outer = QVBoxLayout(self)
        outer.setContentsMargins(12, TOP_MARGIN, 12, 0)
        frame = QWidget()
        frame.setObjectName("transcriptFrame")
        frame.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        # Transparent like the input container: a translucent fill would stack on the window's tint
        frame.setStyleSheet(f"""
            QWidget#transcriptFrame {{
                background-color: transparent;
                border: 1px solid {qss(PRIMARY_COLOR, 180)};
                border-radius: 10px;
            }}
            QLabel {{ background: transparent; color: {qss(PRIMARY_COLOR, 235)}; }}
            QPushButton {{ color: {qss(PRIMARY_COLOR, 200)}; background: transparent; border: none; padding: 0 4px; }}
            QPushButton:hover {{ color: {qss(PRIMARY_COLOR)}; }}
        """)
        outer.addWidget(frame)
        frame_layout = QVBoxLayout(frame)
        frame_layout.setContentsMargins(10, 0, 6, 6)
        frame_layout.setSpacing(0)

        font = QFont("Microsoft JhengHei", 9)
        header = QWidget()
        header.setFixedHeight(HEADER_HEIGHT - 2)
        header_layout = QHBoxLayout(header)
        header_layout.setContentsMargins(0, 0, 0, 0)
        header_layout.setSpacing(10)
        for source, indicator in self.indicators.items():
            indicator.setFont(font)
            indicator.setTextFormat(Qt.TextFormat.RichText)
            indicator.setAccessibleName(f"{SOURCE_LABELS[source]} audio activity")
            header_layout.addWidget(indicator)
        self.caption.setFont(font)
        self.caption.setTextFormat(Qt.TextFormat.PlainText)
        self.caption.setSizePolicy(
            QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred
        )
        self.caption.setAccessibleName("Latest transcript line")
        header_layout.addWidget(self.caption, 1)
        self.toggle_button.setFont(font)
        self.toggle_button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
        self.toggle_button.clicked.connect(self._toggle_expanded)
        header_layout.addWidget(self.toggle_button)
        frame_layout.addWidget(header)

        self.text.setFont(font)
        self.text.setTextFormat(Qt.TextFormat.RichText)
        self.text.setWordWrap(True)
        self.text.setAlignment(Qt.AlignmentFlag.AlignTop | Qt.AlignmentFlag.AlignLeft)
        self.text.setTextInteractionFlags(Qt.TextInteractionFlag.TextSelectableByMouse)
        self.body.setWidget(self.text)
        self.body.setWidgetResizable(True)
        self.body.setFrameShape(QScrollArea.Shape.NoFrame)
        self.body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.body.setStyleSheet(f"""
            QScrollArea, QScrollArea > QWidget > QWidget {{ background: transparent; }}
            QScrollBar:vertical {{ background: transparent; width: 4px; border: none; }}
            QScrollBar::handle:vertical {{ background: {qss(PRIMARY_COLOR, 102)}; border-radius: 2px; min-height: 20px; }}
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical {{ height: 0px; }}
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical {{ background: transparent; }}
        """)
        self.body.setFixedHeight(BODY_HEIGHT - 6)
        self.body.hide()
        frame_layout.addWidget(self.body)
        self.setVisible(False)

    def _toggle_expanded(self) -> None:
        """Expand or collapse the full transcript."""
        self.expanded = not self.expanded
        self._refresh()
        if self.expanded:
            self._scroll_to_end()

    def _on_transcript_changed(self) -> None:
        """Refresh after new lines arrive, following the end if already there."""
        scrollbar = self.body.verticalScrollBar()
        assert scrollbar is not None
        at_end = scrollbar.value() >= scrollbar.maximum() - 4
        self._refresh()
        if at_end:
            self._scroll_to_end()

    def _on_speaking_changed(self, source: str, speaking: bool) -> None:
        """Light a source's indicator while it has speech.

        Args:
            source (str): The audio source.
            speaking (bool): Whether speech is active.
        """
        self.speaking[source] = speaking
        self._refresh_indicators()

    def _on_status_changed(self, status: str) -> None:
        """Show loading or error text in place of the caption.

        Args:
            status (str): The status, or an empty string to show captions.
        """
        self.status = status
        self._refresh()  # The missing-model notice also changes visibility

    def _on_download_finished(self, _model: str, _error: str) -> None:
        """Drop the downloading note from the caption."""
        self._refresh_caption()

    def _refresh(self) -> None:
        """Synchronize visibility, size, and all text with the transcriber."""
        recording = self.transcriber.recording
        segments = self.transcriber.segments
        unsent = sum(
            1 for segment in segments if segment.seq > self.transcriber.sent_seq
        )
        lines = [
            f"<p style='margin: 0 0 4px 0; color: {qss(PRIMARY_COLOR, 255 if segment.seq > self.transcriber.sent_seq else 190)};'>"
            f"<b>{SOURCE_LABELS[segment.source]}</b>&nbsp; {html.escape(segment.text)}</p>"
            for segment in segments
        ]
        model = self.transcriber.recording_model
        model_note = (
            f"{'Transcribing' if recording else 'Transcribed'} with {model}."
            if model
            else ""
        )
        hint = (
            f"<p style='margin: 2px 0 0 0; color: {qss(PRIMARY_COLOR, 190)};'>"
            f"Bright lines are sent with your next message. Dimmed lines were already sent. {model_note}</p>"
        )
        self.text.setText(
            "".join(lines) + hint
            if lines
            else f"<p style='color: {qss(PRIMARY_COLOR, 190)};'>Speech from your microphone (You) and computer audio (Them) appears here. {model_note}</p>"
        )
        label = f"{unsent} new" if unsent else "Transcript"
        self.toggle_button.setText(f"{label} {'▾' if self.expanded else '▴'}")
        self.toggle_button.setToolTip(
            f"{'Hide transcript' if self.expanded else 'Show full transcript'}"
            f"{f' · {model}' if model else ''}"
        )
        self.toggle_button.setAccessibleName(
            f"{'Hide' if self.expanded else 'Show'} transcript, {unsent} lines not yet sent"
        )
        for indicator in self.indicators.values():
            indicator.setVisible(recording)
        self._refresh_indicators()
        self._refresh_caption()
        self.body.setVisible(self.expanded)
        height = HEADER_HEIGHT + TOP_MARGIN + (BODY_HEIGHT if self.expanded else 0)
        if self.height() != height or self.shown != self.wanted():
            self.setFixedHeight(height)
            self.shown = self.wanted()
            self.size_changed.emit()

    def _refresh_indicators(self) -> None:
        """Draw each source's dot lit while it is speaking."""
        for source, indicator in self.indicators.items():
            speaking = self.speaking[source]
            color = qss(SPEAKING_COLOR) if speaking else qss(PRIMARY_COLOR, 150)
            indicator.setText(
                f"<span style='color: {color};'>●</span> {SOURCE_LABELS[source]}"
            )
            indicator.setToolTip(
                f"{SOURCE_LABELS[source]}: {'speaking' if speaking else 'quiet'}"
            )

    def _refresh_caption(self) -> None:
        """Show status, the latest line, or a hint, elided to one line."""
        segments = self.transcriber.segments
        transcriber = self.transcriber
        health = " ".join(
            value for value in transcriber.source_health.values() if value
        )
        if transcriber.recording and health:
            text = health
        elif self.status:
            text = self.status
        elif (
            transcriber.recording
            and transcriber.downloading
            and (not segments or segments[-1].seq < transcriber.first_seq)
        ):
            # Until this recording's first line, say why the new model is not in use yet
            text = f"Using {transcriber.recording_model} while {transcriber.downloading} downloads…"
        elif segments:
            latest = segments[-1]
            text = f"{SOURCE_LABELS[latest.source]}: {latest.text}"
        elif self.transcriber.recording:
            text = "Listening…"
        else:
            text = "Recording stopped"
        if not self.transcriber.recording and segments and not self.status:
            text = f"Stopped · {text}"
        self.caption.setToolTip(text)
        self.caption.setText(
            QFontMetrics(self.caption.font()).elidedText(
                text, Qt.TextElideMode.ElideLeft, max(self.caption.width(), 40)
            )
        )

    def _scroll_to_end(self) -> None:
        """Scroll the transcript to its newest line once layout settles."""
        scrollbar = self.body.verticalScrollBar()
        assert scrollbar is not None
        QTimer.singleShot(0, lambda: scrollbar.setValue(scrollbar.maximum()))
