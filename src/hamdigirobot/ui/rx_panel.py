"""RX panel: tuning aid, pitch/WPM readouts, decoded text.

Input-device selection lives entirely on the Settings dialog's "Audio
Devices" tab (ui.settings_dialog) -- this panel has no device UI of its
own. Audio capture / decoder wiring lives in main_window.py.
"""

from __future__ import annotations

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QGuiApplication, QTextCursor
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QVBoxLayout,
    QWidget,
)

from hamdigirobot.ui.form_helpers import FIELD_BLOCK_SPACING, SETTINGS_COLUMN_WIDTH, labeled_field, layout_widgets
from hamdigirobot.ui.qso_fields import QSO_FIELDS
from hamdigirobot.ui.selection_popup import SelectionMacroPopup
from hamdigirobot.ui.style import make_separator
from hamdigirobot.ui.waterfall_widget import WaterfallWidget

# How long a text selection must sit still before the field-routing popup
# appears (see _on_decoded_text_selection_changed) -- long enough that an
# in-progress mouse drag never gets interrupted by it.
SELECTION_POPUP_DELAY_MS = 200

# Bandwidth slider steps in raw Goertzel bin_span units (see
# dsp.tone_detector.GoertzelToneDetector.bin_span); main_window translates
# the resulting value to/from Hz for display via effective_bandwidth_hz.
# The slider is in quarter-bin steps (value / BANDWIDTH_SLIDER_STEPS_PER_BIN
# = bin_span), which makes the bandwidth effectively continuous.
BANDWIDTH_SLIDER_STEPS_PER_BIN = 4
BANDWIDTH_SLIDER_MIN = 0
BANDWIDTH_SLIDER_MAX = 6 * BANDWIDTH_SLIDER_STEPS_PER_BIN
BANDWIDTH_SLIDER_DEFAULT = 1 * BANDWIDTH_SLIDER_STEPS_PER_BIN

RX_SPEED_MIN_WPM = 5
RX_SPEED_MAX_WPM = 60
RX_SPEED_DEFAULT_WPM = 20

SQUELCH_SLIDER_MIN = 0  # 0 = squelch fully open (effectively disabled), see decoder.squelch_ratio setter
SQUELCH_SLIDER_MAX = 30
SQUELCH_SLIDER_DEFAULT = 14


# Pitch spinbox ranges per mode: CW tones are low audio tones, RTTY's
# conventional mark tone (2125 Hz) sits well above that.
CW_PITCH_RANGE = (200, 2000)
RTTY_PITCH_RANGE = (300, 3000)


class RxPanel(QGroupBox):
    """The 'RX' (reception) group box."""

    # Emitted when the operator picks a QSO field in the selection popup
    # for the currently-selected decoded_text text: (field_key, text). Only
    # QsoPanel needs to be able to use these -- RxPanel itself has no idea
    # what a "QSO field" even is beyond a key/label pair (see qso_fields).
    selection_routed_to_field = Signal(str, str)
    # Any of the RTTY-only controls (shift / reverse / UOS) was changed by
    # the operator; read the new values via rtty_shift() etc.
    rtty_settings_changed = Signal()
    # The operator picked LSB/USB for the CW waterfall marker: "lsb"/"usb".
    sideband_chosen = Signal(str)

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__("RX", parent)
        self.setObjectName("rxPanel")
        # QGroupBox's own default size policy is Preferred/Preferred, which
        # -- regardless of the stretch factor MainWindow gives this panel in
        # its own layout -- left it unable to actually claim extra window
        # height; Expanding is what makes RxPanel (and so decoded_text
        # below, its own sole stretch item) able to grow into it.
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)

        layout = QVBoxLayout(self)
        layout.setSpacing(8)

        self.waterfall = WaterfallWidget(parent=self)
        # Full width (no stretch factor -- a QVBoxLayout already gives a
        # widget the whole row's width by default), capped height (see
        # WaterfallWidget) -- it's a tuning aid, not the panel's primary
        # content, so it shouldn't keep growing at decoded_text's expense.
        layout.addWidget(self.waterfall)
        # A real gap (not just layout.setSpacing's 8px) between the
        # waterfall and the controls below -- at some window sizes 8px read
        # as the controls sitting right on top of the waterfall's own
        # bottom-edge frequency-axis labels, which is exactly the
        # "overlapping" complaint this is fixing.
        layout.addSpacing(6)
        layout.addWidget(make_separator(self))

        # Below the waterfall: decoded text on the left (the panel's actual
        # output -- gets all the leftover room, hence taller than before),
        # every setting/control stacked in a narrow column on the right.
        content_row = QHBoxLayout()
        content_row.setSpacing(14)

        self.decoded_text = QPlainTextEdit(self)
        self.decoded_text.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Expanding)
        self.decoded_text.setReadOnly(True)
        self.decoded_text.setPlaceholderText("Received and decoded text appears here…")
        font = self.decoded_text.font()
        font.setFamily("monospace")
        self.decoded_text.setFont(font)
        self.decoded_text.setContextMenuPolicy(Qt.ContextMenuPolicy.CustomContextMenu)
        self.decoded_text.customContextMenuRequested.connect(self._show_decoded_text_context_menu)
        content_row.addWidget(self.decoded_text, stretch=1)

        # A fixed-width, top-aligned QVBoxLayout of "caption label above its
        # control" blocks (see ui.form_helpers) -- not a QFormLayout: that
        # shares one label-column width across every row, which silently
        # clips long labels ("Squelch sensitivity:") the moment total
        # content needs more than the column's cap allows. Stacking
        # sidesteps the shared-column concept entirely.
        controls_column = QWidget(self)
        controls_column.setFixedWidth(SETTINGS_COLUMN_WIDTH)
        controls_layout = QVBoxLayout(controls_column)
        controls_layout.setContentsMargins(0, 0, 0, 0)
        controls_layout.setSpacing(FIELD_BLOCK_SPACING)

        self.pitch_spinbox = QSpinBox(self)
        self.pitch_spinbox.setRange(200, 2000)
        self.pitch_spinbox.setSingleStep(10)
        self.pitch_spinbox.setValue(600)
        # LSB/USB selector next to the pitch (CW only): which side of the
        # pitch the waterfall marks. Manual while there's no CAT link; once
        # the rig reports its sideband it follows that and goes inactive
        # (see set_rig_sideband).
        self.sideband_combo = QComboBox(self)
        self.sideband_combo.addItem("USB", userData="usb")
        self.sideband_combo.addItem("LSB", userData="lsb")
        self._manual_sideband = "usb"
        self.sideband_combo.currentIndexChanged.connect(self._on_sideband_combo_changed)
        self._update_sideband_tooltip(rig_controlled=False)
        pitch_row = QHBoxLayout()
        pitch_row.addWidget(self.pitch_spinbox, stretch=1)
        pitch_row.addWidget(self.sideband_combo)
        pitch_block = labeled_field("Pitch (Hz):", pitch_row, self)
        self.pitch_caption: QLabel = pitch_block.itemAt(0).widget()
        controls_layout.addLayout(pitch_block)
        # The waterfall marker sits on the pitch in CW; in RTTY it sits
        # midway between the mark and space tones (see set_rtty_mode).
        self._marker_offset_hz = 0.0
        self._rtty_shift_hz = 170.0
        self._rtty_reverse = False
        self.pitch_spinbox.valueChanged.connect(self._update_waterfall_marker)

        self.wpm_label = QLabel("RX speed: -- WPM")
        controls_layout.addWidget(self.wpm_label)

        # RTTY-only modem controls (hidden in CW, see set_rtty_mode). Shift
        # and reverse are shared by RX and TX, hence they live here.
        self.rtty_shift_spinbox = QSpinBox(self)
        self.rtty_shift_spinbox.setRange(50, 1000)
        self.rtty_shift_spinbox.setSingleStep(5)
        self.rtty_shift_spinbox.setSuffix(" Hz")
        self.rtty_shift_spinbox.setValue(170)
        self.rtty_shift_spinbox.setToolTip("Space tone = mark tone + shift. Amateur RTTY: 170 Hz.")
        shift_block = labeled_field("Shift:", self.rtty_shift_spinbox, self)
        controls_layout.addLayout(shift_block)

        self.rtty_reverse_checkbox = QCheckBox("Reverse (swap mark/space)", self)
        self.rtty_reverse_checkbox.setToolTip(
            "Use when the rig is on the other sideband than the sender "
            "assumed: RX and TX tones are swapped."
        )
        controls_layout.addWidget(self.rtty_reverse_checkbox)

        self.rtty_uos_checkbox = QCheckBox("Unshift on space (UOS)", self)
        self.rtty_uos_checkbox.setToolTip(
            "Receive: a space returns the decoder to letters. Transmit: no "
            "letters shift is sent after a space. Only enable it if the "
            "other station uses UOS too."
        )
        controls_layout.addWidget(self.rtty_uos_checkbox)
        self._rtty_only_widgets: list[QWidget] = [
            *layout_widgets(shift_block),
            self.rtty_reverse_checkbox,
            self.rtty_uos_checkbox,
        ]
        for widget in self._rtty_only_widgets:
            widget.setVisible(False)
        self.rtty_shift_spinbox.valueChanged.connect(self._on_rtty_controls_changed)
        self.rtty_reverse_checkbox.toggled.connect(self._on_rtty_controls_changed)
        self.rtty_uos_checkbox.toggled.connect(self._on_rtty_controls_changed)

        self.rx_speed_spinbox = QSpinBox(self)
        self.rx_speed_spinbox.setRange(RX_SPEED_MIN_WPM, RX_SPEED_MAX_WPM)
        self.rx_speed_spinbox.setValue(RX_SPEED_DEFAULT_WPM)
        self.rx_speed_spinbox.setSuffix(" WPM")
        controls_layout.addWidget(self.rx_speed_spinbox)

        self.auto_rx_speed_checkbox = QCheckBox("Automatic", self)
        self.auto_rx_speed_checkbox.setChecked(True)
        self.auto_rx_speed_checkbox.setToolTip(
            "When checked, the receive speed is detected automatically. "
            "When unchecked, the decoder uses the fixed speed set above."
        )
        controls_layout.addWidget(self.auto_rx_speed_checkbox)
        self.auto_rx_speed_checkbox.toggled.connect(self._on_auto_rx_speed_toggled)
        self._on_auto_rx_speed_toggled(True)

        self._speed_separator = make_separator(self)
        controls_layout.addWidget(self._speed_separator)

        # Detection-tuning cluster: bandwidth then squelch, each its own
        # row -- previously one very long QHBoxLayout where the two
        # unrelated controls ran straight into each other.
        bandwidth_row = QHBoxLayout()
        self.bandwidth_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.bandwidth_slider.setRange(BANDWIDTH_SLIDER_MIN, BANDWIDTH_SLIDER_MAX)
        self.bandwidth_slider.setValue(BANDWIDTH_SLIDER_DEFAULT)
        bandwidth_row.addWidget(self.bandwidth_slider, stretch=1)
        self.bandwidth_label = QLabel("")
        self.bandwidth_label.setMinimumWidth(50)
        bandwidth_row.addWidget(self.bandwidth_label)
        bandwidth_block = labeled_field("Bandwidth:", bandwidth_row, self)
        controls_layout.addLayout(bandwidth_block)

        self.auto_bandwidth_checkbox = QCheckBox("Automatic", self)
        self.auto_bandwidth_checkbox.setChecked(True)
        self.auto_bandwidth_checkbox.setToolTip(
            "When checked, the bandwidth (and its Goertzel window) automatically "
            "tracks the currently received speed: narrower for slow sending "
            "(better noise rejection), wider for fast sending (needed time "
            "resolution). When unchecked, the slider controls it manually."
        )
        controls_layout.addWidget(self.auto_bandwidth_checkbox)

        # Local wiring (matches the squelch label's self-contained pattern
        # above): the checkbox directly enables/disables the slider so the
        # UI stays consistent even before main_window reacts to the toggle.
        self.auto_bandwidth_checkbox.toggled.connect(self._on_auto_bandwidth_toggled)
        self._on_auto_bandwidth_toggled(self.auto_bandwidth_checkbox.isChecked())

        self._bandwidth_separator = make_separator(self)
        controls_layout.addWidget(self._bandwidth_separator)

        squelch_row = QHBoxLayout()
        self.squelch_slider = QSlider(Qt.Orientation.Horizontal, self)
        self.squelch_slider.setRange(SQUELCH_SLIDER_MIN, SQUELCH_SLIDER_MAX)
        self.squelch_slider.setValue(SQUELCH_SLIDER_DEFAULT)
        squelch_row.addWidget(self.squelch_slider, stretch=1)
        self.squelch_ratio_label = QLabel("")
        self.squelch_ratio_label.setMinimumWidth(30)
        squelch_row.addWidget(self.squelch_ratio_label)
        # Open/closed state as a small colored dot at the row's right edge
        # instead of a whole separate "Squelch: open/closed" line below --
        # same information at a glance, without costing the column an extra
        # row of height (which was crowding the sliders/labels above it).
        self.squelch_state_dot = QLabel(self)
        self.squelch_state_dot.setObjectName("squelchStateDot")
        self.squelch_state_dot.setFixedSize(12, 12)
        squelch_row.addWidget(self.squelch_state_dot)
        controls_layout.addLayout(labeled_field("Squelch sensitivity:", squelch_row, self))

        # The bandwidth label shows a Hz value that depends on the decoder's
        # actual Goertzel bin width, so main_window owns updating it (via
        # set_bandwidth_label) once it applies the new bin_span and reads
        # back effective_bandwidth_hz. The squelch ratio label is a pure
        # display formatting of the slider's own value, so it's handled here.
        self.squelch_slider.valueChanged.connect(self._on_squelch_slider_changed)
        self._on_squelch_slider_changed(self.squelch_slider.value())
        self.set_squelch_open(True)

        # Absorbs spare height when CW-only rows are hidden (RTTY mode), so
        # the remaining controls stay packed at the top.
        controls_layout.addStretch(1)

        # Controls that only make sense for CW (automatic keying-speed
        # detection, the adaptive Goertzel bandwidth); hidden in RTTY mode.
        self._cw_only_widgets: list[QWidget] = [
            self.sideband_combo,
            self.rx_speed_spinbox,
            self.auto_rx_speed_checkbox,
            self.auto_bandwidth_checkbox,
            self._bandwidth_separator,
            *layout_widgets(bandwidth_block),
        ]
        self._rtty_mode = False

        # Top-aligned: without this, Qt stretches controls_column to match
        # decoded_text's (much taller) height, leaving the column's own rows
        # oddly spread out instead of sitting compactly at the top.
        content_row.addWidget(controls_column, alignment=Qt.AlignmentFlag.AlignTop)

        layout.addLayout(content_row, stretch=1)

        # Selection -> QSO-field routing popup (see ui.selection_popup):
        # select a callsign/RST/etc. in the decoded text, click the field
        # name in the popup that appears, done. Debounced rather than shown
        # directly from selectionChanged, which also fires continuously
        # *during* a mouse-drag selection -- popping up (and thus mouse-
        # grabbing, see SelectionMacroPopup) mid-drag would abort the drag.
        self._selection_popup = SelectionMacroPopup(QSO_FIELDS, self)
        self._selection_popup.field_chosen.connect(self._on_selection_field_chosen)
        self._selection_popup_timer = QTimer(self)
        self._selection_popup_timer.setSingleShot(True)
        self._selection_popup_timer.timeout.connect(self._maybe_show_selection_popup)
        self.decoded_text.selectionChanged.connect(self._on_decoded_text_selection_changed)

    def _update_waterfall_marker(self, *_args: object) -> None:
        self.waterfall.set_target_pitch(self.pitch_spinbox.value() + self._marker_offset_hz)
        if not self._rtty_mode:
            self.waterfall.set_rtty_tones(None)
            return
        # The spinbox is the lower tone; with "reverse" that one is the
        # space tone (see tx.rtty_modulator.RttyParams.tones).
        low = float(self.pitch_spinbox.value())
        names = ("SPACE", "MARK") if self._rtty_reverse else ("MARK", "SPACE")
        self.waterfall.set_rtty_tones(
            [(names[0], low), (names[1], low + self._rtty_shift_hz)],
            tolerance_hz=min(25.0, self._rtty_shift_hz / 4.0),
        )

    def sideband(self) -> str:
        """"LSB" / "USB" -- what the waterfall should mark right now."""
        return str(self.sideband_combo.currentData()).upper()

    def set_manual_sideband(self, sideband: str) -> None:
        """Load the saved manual choice (no change signal)."""
        self._manual_sideband = "lsb" if sideband == "lsb" else "usb"
        if self.sideband_combo.isEnabled():
            self._select_sideband(self._manual_sideband)

    def set_rig_sideband(self, sideband: str | None) -> None:
        """The sideband the rig reports over CAT ("USB"/"LSB"), or None when
        there is no (readable) CAT link. With a value the combo shows it and
        is disabled; without, it is editable again and returns to the
        operator's own choice."""
        if sideband:
            self._select_sideband(sideband.lower())
            self.sideband_combo.setEnabled(False)
        else:
            self.sideband_combo.setEnabled(True)
            self._select_sideband(self._manual_sideband)
        self._update_sideband_tooltip(rig_controlled=bool(sideband))

    def _select_sideband(self, key: str) -> None:
        self.sideband_combo.blockSignals(True)
        self.sideband_combo.setCurrentIndex(max(0, self.sideband_combo.findData(key)))
        self.sideband_combo.blockSignals(False)

    def _on_sideband_combo_changed(self, _index: int) -> None:
        self._manual_sideband = str(self.sideband_combo.currentData())
        self.sideband_chosen.emit(self._manual_sideband)

    def _update_sideband_tooltip(self, rig_controlled: bool) -> None:
        self.sideband_combo.setToolTip(
            "Sideband reported by the rig over CAT (not editable)."
            if rig_controlled
            else "Sideband the radio is on: the waterfall marks only that side of "
            "the pitch. Set it by hand; with a CAT connection it follows the rig."
        )

    def rtty_shift(self) -> int:
        return self.rtty_shift_spinbox.value()

    def rtty_reverse(self) -> bool:
        return self.rtty_reverse_checkbox.isChecked()

    def rtty_unshift_on_space(self) -> bool:
        return self.rtty_uos_checkbox.isChecked()

    def _on_rtty_controls_changed(self, *_args: object) -> None:
        self._rtty_shift_hz = float(self.rtty_shift_spinbox.value())
        self._rtty_reverse = self.rtty_reverse_checkbox.isChecked()
        self._marker_offset_hz = self._rtty_shift_hz / 2.0
        self._update_waterfall_marker()
        self.rtty_settings_changed.emit()

    def set_rtty_mode(
        self,
        enabled: bool,
        shift_hz: float = 170.0,
        baud: float = 45.45,
        reverse: bool = False,
        unshift_on_space: bool = False,
    ) -> None:
        """Switches the panel between CW and RTTY. In RTTY the pitch
        spinbox is the *mark* tone, the CW-only speed/bandwidth controls
        disappear, and the speed readout shows the (fixed) baud rate."""
        self._rtty_mode = enabled
        self.setTitle("RX — RTTY" if enabled else "RX")
        self.pitch_caption.setText("Mark tone (Hz):" if enabled else "Pitch (Hz):")
        low, high = RTTY_PITCH_RANGE if enabled else CW_PITCH_RANGE
        self.pitch_spinbox.setRange(low, high)
        for widget in self._cw_only_widgets:
            widget.setVisible(not enabled)
        for widget in self._rtty_only_widgets:
            widget.setVisible(enabled)
        if enabled:
            # Programmatic load of the saved values: no change signals.
            for control, value in (
                (self.rtty_shift_spinbox, int(shift_hz)),
                (self.rtty_reverse_checkbox, reverse),
                (self.rtty_uos_checkbox, unshift_on_space),
            ):
                control.blockSignals(True)
                (control.setValue if control is self.rtty_shift_spinbox else control.setChecked)(value)
                control.blockSignals(False)
            self._rtty_shift_hz = float(shift_hz)
            self._rtty_reverse = reverse
            self._marker_offset_hz = shift_hz / 2.0
            self.set_rtty_baud(baud)
        else:
            self._marker_offset_hz = 0.0
            self.set_wpm(None)
            self._on_auto_rx_speed_toggled(self.auto_rx_speed_checkbox.isChecked())
            self._on_auto_bandwidth_toggled(self.auto_bandwidth_checkbox.isChecked())
        self._update_waterfall_marker()

    def set_bpsk_mode(self, enabled: bool, baud: float = 62.5) -> None:
        """BPSK63 look of the panel; call *after* set_rtty_mode(False), which
        resets the CW layout this builds on. One carrier (the pitch
        spinbox), so none of the CW or RTTY-specific controls apply."""
        if not enabled:
            return
        self._rtty_mode = False
        self.setTitle("RX — BPSK63")
        self.pitch_caption.setText("Carrier (Hz):")
        self.pitch_spinbox.setRange(*RTTY_PITCH_RANGE)
        for widget in (*self._cw_only_widgets, *self._rtty_only_widgets):
            widget.setVisible(False)
        self._marker_offset_hz = 0.0
        self.wpm_label.setText(f"RX speed: {baud:g} baud")
        self._update_waterfall_marker()

    def set_rtty_baud(self, baud: float) -> None:
        self.wpm_label.setText(f"RX speed: {baud:g} baud")

    def set_wpm(self, wpm: float | None) -> None:
        if wpm is None:
            self.wpm_label.setText("RX speed: -- WPM")
        else:
            self.wpm_label.setText(f"RX speed: {wpm:.0f} WPM")

    def set_bandwidth_label(self, hz: float) -> None:
        # Rounded to the nearest 5 Hz rather than the nearest 1 Hz: the raw
        # half-width at the narrowest (single-bin) setting is 31.25 Hz,
        # which reads as an oddly specific "±31 Hz" -- a cleaner, more
        # radio-like round figure ("±30 Hz") is more useful to the operator
        # than the extra digit of (illusory) precision. Purely a display
        # rounding; the actual detection bandwidth is unaffected.
        half_width = round(hz / 2 / 5) * 5
        self.bandwidth_label.setText(f"±{half_width:.0f} Hz")

    def _on_squelch_slider_changed(self, value: int) -> None:
        self.squelch_ratio_label.setText("off" if value == 0 else f"{value}×")

    def _on_auto_rx_speed_toggled(self, enabled: bool) -> None:
        self.rx_speed_spinbox.setDisabled(enabled)

    def _on_auto_bandwidth_toggled(self, enabled: bool) -> None:
        self.bandwidth_slider.setDisabled(enabled)

    def set_squelch_open(self, is_open: bool) -> None:
        if is_open:
            color = "#2ecc71"  # Emerald -- matches the TX progress highlight's "live/active" green
            tooltip = "Squelch: open"
        else:
            color = "#888888"
            tooltip = "Squelch: closed"
        # border-radius (see style.STYLESHEET's QLabel#squelchStateDot rule)
        # renders the fixed 12x12 label as a circle; only the fill color
        # changes here, per state.
        self.squelch_state_dot.setStyleSheet(f"background-color: {color};")
        self.squelch_state_dot.setToolTip(tooltip)

    def append_decoded_text(self, text: str) -> None:
        scrollbar = self.decoded_text.verticalScrollBar()
        was_at_bottom = scrollbar.value() >= scrollbar.maximum()
        dragging = bool(QGuiApplication.mouseButtons() & Qt.MouseButton.LeftButton)

        # The widget's own (visible) cursor/selection is deliberately left
        # untouched: the text is inserted with a separate QTextCursor bound
        # directly to the document. Qt automatically repositions every live
        # cursor tied to a document (including the one backing the
        # operator's active selection) around edits made elsewhere, so this
        # insertion never clobbers an in-progress selection -- unlike
        # calling setTextCursor() with an end-of-document cursor, which
        # used to overwrite (and thus clear) it on every decoded character.
        cursor = QTextCursor(self.decoded_text.document())
        cursor.movePosition(QTextCursor.MoveOperation.End)
        cursor.insertText(text)

        # Only "stick" to the bottom if the operator was already there and
        # isn't mid-drag (click+hold to select) right now -- this avoids
        # yanking the view away from text the operator has scrolled up to
        # read/select, and avoids the view shifting under the mouse during
        # an active drag-selection.
        if was_at_bottom and not dragging:
            scrollbar.setValue(scrollbar.maximum())

    def _show_decoded_text_context_menu(self, pos) -> None:
        # Standard menu (copy, select all, ...) plus a "Clear" action to
        # clear the decoded-text pane -- e.g. to start fresh for a new QSO
        # without restarting the app.
        menu = self.decoded_text.createStandardContextMenu()
        menu.addSeparator()
        clear_action = menu.addAction("Clear")
        clear_action.triggered.connect(self.decoded_text.clear)
        menu.exec(self.decoded_text.mapToGlobal(pos))

    def _on_decoded_text_selection_changed(self) -> None:
        if not self.decoded_text.textCursor().hasSelection():
            self._selection_popup.hide()
            self._selection_popup_timer.stop()
            return
        self._selection_popup_timer.start(SELECTION_POPUP_DELAY_MS)

    def _maybe_show_selection_popup(self) -> None:
        cursor = self.decoded_text.textCursor()
        if not cursor.hasSelection():
            return
        if QGuiApplication.mouseButtons() & Qt.MouseButton.LeftButton:
            # Still mid-drag: try again shortly rather than popping up (and
            # mouse-grabbing, see SelectionMacroPopup) over an in-progress
            # selection gesture.
            self._selection_popup_timer.start(SELECTION_POPUP_DELAY_MS)
            return
        rect = self.decoded_text.cursorRect(cursor)
        global_pos = self.decoded_text.mapToGlobal(rect.bottomRight())
        self._selection_popup.show_near(global_pos)

    def _on_selection_field_chosen(self, field_key: str) -> None:
        # Re-read the selection at click time rather than caching it when
        # the popup was shown -- always the freshest text, and avoids any
        # risk of staleness if the selection somehow changed in between.
        text = self.decoded_text.textCursor().selectedText()
        if text:
            self.selection_routed_to_field.emit(field_key, text)
