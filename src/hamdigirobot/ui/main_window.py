"""Main application window.

Layout: two visually separate group boxes (RX/vetel, TX/adas), per
Specifikacio.md's "kulon megjelenes" requirement, stacked vertically, plus a
minimal menu bar and a status bar for connection/error state.
"""

from __future__ import annotations

from PySide6.QtCore import QThread, Qt, Signal
from PySide6.QtGui import QAction, QActionGroup, QKeySequence, QShortcut
from PySide6.QtWidgets import QMainWindow, QVBoxLayout, QWidget

from hamdigirobot import macros
from hamdigirobot.audio import devices as audio_devices
from hamdigirobot.audio.capture import AudioCapture
from hamdigirobot.config import AppConfig
from hamdigirobot.decoder.bpsk_decoder import BpskDecoderCore
from hamdigirobot.decoder.decoder import CwDecoder
from hamdigirobot.decoder.rtty_decoder import RttyDecoderCore
from hamdigirobot.hamlib.ctypes_bindings import HamlibUnavailableError
from hamdigirobot.hamlib.frequency_monitor import FrequencyMonitor
from hamdigirobot.hamlib.rig_client import HamlibError, HamlibRig
from hamdigirobot.hamlib.sideband import rig_modes_for
from hamdigirobot.modes import ALL_MODES, BPSK63, CW, RTTY, mode_by_key
from hamdigirobot.models.qso import QsoLoggingError, QsoRecord, build_qso_logger
from hamdigirobot.tx.audio_backend import AudioTxBackend
from hamdigirobot.tx.bpsk_backend import BpskAudioTxBackend
from hamdigirobot.tx.bpsk_modulator import BAUD as BPSK63_BAUD
from hamdigirobot.tx.backend import TxBackend
from hamdigirobot.tx.encoder import manual_keying_jitter_from_level
from hamdigirobot.tx.hamlib_backend import HamlibTxBackend
from hamdigirobot.tx.rtty_backend import RttyAudioTxBackend
from hamdigirobot.tx.worker import TxWorker
from hamdigirobot.ui.qso_panel import QsoPanel
from hamdigirobot.ui.rx_panel import BANDWIDTH_SLIDER_STEPS_PER_BIN, RxPanel
from hamdigirobot.ui.settings_dialog import SettingsDialog
from hamdigirobot.ui.tx_panel import TxPanel


class MainWindow(QMainWindow):
    # Cross-thread requests into self._freq_monitor (see hamlib
    # .frequency_monitor's module docstring for why these go through signals
    # rather than calling the worker's methods directly).
    _freq_monitor_set_target = Signal(object, str, object)  # model_id, port_path, baud_rate
    _freq_monitor_pause = Signal()
    _freq_monitor_resume = Signal()
    _freq_monitor_stop = Signal()
    _freq_monitor_set_rig_mode = Signal(object)  # list of RIG_MODE_* bits to try

    def __init__(self, config: AppConfig) -> None:
        super().__init__()
        self.config = config
        self._mode = mode_by_key(config.mode)
        self._last_rig_target: tuple | None = None
        self.setWindowTitle("HAM Digi Robot")
        self.resize(900, 700)

        central = QWidget(self)
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(10)
        layout.setContentsMargins(10, 10, 10, 10)

        self.rx_panel = RxPanel(central)
        self.qso_panel = QsoPanel(central)
        self.tx_panel = TxPanel(config, central)
        # RX is the only one that should keep growing into extra window
        # height -- it holds the waterfall + the decoded-text log, both of
        # which genuinely benefit from more room. QSO Data is a single
        # fixed row of fields, and TX's own content (controls + a short
        # TX text box) doesn't need to keep growing either -- giving it a
        # share of the stretch (as before) just left a large empty-looking
        # gap under its macro buttons on a tall window.
        layout.addWidget(self.rx_panel, stretch=1)
        layout.addWidget(self.qso_panel)
        layout.addWidget(self.tx_panel)
        self.rx_panel.selection_routed_to_field.connect(self.qso_panel.set_field_text)
        self.tx_panel.macro_bar.macro_activated.connect(self._on_macro_activated)

        self._build_shortcuts()
        self._apply_config_to_widgets()
        self._build_menu()

        # Never let a panel shrink below its own comfortable size, via two
        # things that both turned out to be necessary:
        #
        # 1. Per-panel hard floors: when the *total* window height on offer
        #    falls short of every panel's combined minimum, QVBoxLayout
        #    shrinks the stretch=1 item (RxPanel) first/most rather than
        #    spreading the deficit evenly -- QsoPanel/TxPanel (stretch 0)
        #    kept their full preferred size while RxPanel got squeezed
        #    *below* its own minimumSizeHint, crushing its pitch/bandwidth/
        #    squelch controls into an overlapping, unreadable mess.
        #    setMinimumHeight() makes each panel's own minimum a hard floor
        #    the layout can no longer take from -- so if the window really
        #    is too short, panels visibly overlap each other instead of a
        #    single panel silently mangling its own contents (a far more
        #    obvious, fixable failure mode). Safe to compute right after
        #    construction (no show()/layout pass needed first): each panel's
        #    minimumSizeHint() is governed by its own fixed-width
        #    controls_column (SETTINGS_COLUMN_WIDTH, see form_helpers), not
        #    by whatever width the panel eventually gets from this layout.
        # 2. A correctly chrome-aware whole-window floor: central widget's
        #    own minimumSizeHint() only covers *its* content -- setMinimumSize
        #    on self (the QMainWindow) needs the menu bar's and status bar's
        #    height added on top, or the window's floor comes out shorter
        #    than what the central widget actually needs, silently
        #    reintroducing the exact per-panel deficit (1) guards against.
        for panel in (self.rx_panel, self.qso_panel, self.tx_panel):
            panel.setMinimumHeight(panel.minimumSizeHint().height())
        chrome_height = self.menuBar().sizeHint().height() + self.statusBar().sizeHint().height()
        central_minimum = central.minimumSizeHint()
        self.setMinimumSize(central_minimum.width(), central_minimum.height() + chrome_height)

        self.statusBar().showMessage("Radio: not connected | Audio: not initialized")
        self._start_update_check()

        self._audio_capture: AudioCapture | None = None
        self._decoder: CwDecoder | None = None
        self._decoder_thread: QThread | None = None

        self._tx_backend: TxBackend | None = None
        self._tx_worker: TxWorker | None = None
        self._tx_thread: QThread | None = None
        self._last_rx_wpm: float | None = None

        # Polls the rig's VFO frequency for the QSO panel, on its own
        # QThread -- separate from the Hamlib connection TX itself opens
        # per-send (see _build_hamlib_tx_backend). Never held open at the
        # same time as a TX connection: two open handles to the same serial
        # port at once could interleave their traffic and corrupt either
        # side's CAT commands, so _build_hamlib_tx_backend pauses this first,
        # and _stop_tx_pipeline resumes it once the send's own connection is
        # closed. See hamlib.frequency_monitor's module docstring for why
        # this needs its own thread at all (blocking Hamlib I/O on the GUI
        # thread -- e.g. retrying against a powered-off rig -- was freezing
        # the whole window).
        #
        # _freq_monitor_pause specifically has to be a *blocking* queued
        # connection, not the default fire-and-forget queued one: a plain
        # emit() only ever posts the pause() call onto the worker thread's
        # event queue and returns immediately, with no guarantee it actually
        # ran -- if a poll happened to be in flight (or about to start) right
        # as Send was clicked, _build_hamlib_tx_backend would open its own
        # connection to the same port while the monitor's was still live,
        # producing exactly the two-handles-on-one-port corruption this is
        # meant to prevent (seen in the wild as Hamlib error -14,
        # RIG_BUSBUSY "collision on the bus", failing rig_send_morse).
        # BlockingQueuedConnection makes emit() itself wait for pause()'s
        # _close_rig() to actually finish on the worker thread before
        # _build_hamlib_tx_backend is allowed to proceed to its own open() --
        # a short, bounded stall (at most one poll's worth of CAT I/O) on
        # Send, not the unbounded once-a-second GUI freeze the dedicated
        # thread was originally introduced to fix.
        self._freq_monitor = FrequencyMonitor()
        self._freq_monitor_thread = QThread(self)
        self._freq_monitor.moveToThread(self._freq_monitor_thread)
        self._freq_monitor_thread.started.connect(self._freq_monitor.run)
        self._freq_monitor.frequency_changed.connect(self.qso_panel.set_frequency_hz)
        self._freq_monitor.sideband_changed.connect(self._on_rig_sideband_changed)
        self._freq_monitor_set_target.connect(self._freq_monitor.set_target)
        self._freq_monitor_pause.connect(self._freq_monitor.pause, Qt.ConnectionType.BlockingQueuedConnection)
        self._freq_monitor_resume.connect(self._freq_monitor.resume)
        self._freq_monitor_stop.connect(self._freq_monitor.stop)
        self._freq_monitor_set_rig_mode.connect(self._freq_monitor.set_rig_mode)
        self._freq_monitor.rig_mode_error.connect(self.statusBar().showMessage)
        self._freq_monitor_thread.start()
        self._sync_freq_monitor_target()

        self.rx_panel.pitch_spinbox.valueChanged.connect(self._on_target_pitch_changed)
        self.rx_panel.bandwidth_slider.valueChanged.connect(self._on_bandwidth_changed)
        self.rx_panel.squelch_slider.valueChanged.connect(self._on_squelch_ratio_changed)
        self.rx_panel.auto_bandwidth_checkbox.toggled.connect(self._on_auto_bandwidth_changed)
        self.rx_panel.auto_rx_speed_checkbox.toggled.connect(self._on_rx_speed_mode_changed)
        self.rx_panel.rx_speed_spinbox.valueChanged.connect(self._on_rx_speed_mode_changed)

        self.tx_panel.baud_combo.currentIndexChanged.connect(self._on_rtty_controls_changed)
        self.tx_panel.stop_bits_combo.currentIndexChanged.connect(self._on_rtty_controls_changed)
        self.rx_panel.rtty_settings_changed.connect(self._on_rtty_controls_changed)
        self.rx_panel.sideband_chosen.connect(self._on_sideband_chosen)
        self.tx_panel.send_button.clicked.connect(self._on_tx_send_clicked)
        self.tx_panel.stop_button.clicked.connect(self._on_tx_stop_clicked)
        self.tx_panel.match_rx_speed_button.clicked.connect(self._on_match_rx_speed_clicked)

        self.qso_panel.log_button.clicked.connect(self._on_log_qso_clicked)

        self._start_audio_pipeline(self.config.rx_input_device)

    def _apply_config_to_widgets(self) -> None:
        self.rx_panel.set_manual_sideband(self.config.cw_sideband)
        self._apply_mode_to_panels()
        self._apply_cw_sideband()
        self.rx_panel.waterfall.set_half_span(self.config.waterfall_half_span_hz)
        self.rx_panel.bandwidth_slider.setValue(round(self.config.detection_bin_span * BANDWIDTH_SLIDER_STEPS_PER_BIN))
        self.rx_panel.squelch_slider.setValue(round(self.config.squelch_ratio))
        self.rx_panel.auto_bandwidth_checkbox.setChecked(self.config.auto_bandwidth)
        self.rx_panel.rx_speed_spinbox.setValue(self.config.rx_speed_wpm)
        self.rx_panel.auto_rx_speed_checkbox.setChecked(self.config.auto_rx_speed)
        self.tx_panel.speed_spinbox.setValue(self.config.tx_speed_wpm)
        self.tx_panel.manual_keying_checkbox.setChecked(self.config.manual_keying_emulation)
        self.tx_panel.manual_keying_slider.setValue(self.config.manual_keying_intensity)
        self.tx_panel.set_manual_keying_available(self.config.tx_backend != "hamlib")
        self.qso_panel.set_frequency_editable(self.config.tx_backend != "hamlib")

    def _apply_mode_to_panels(self) -> None:
        """Mode-specific look of the (shared-layout) panels: speed vs. baud,
        pitch vs. mark tone, which controls are shown. Also loads that
        mode's own tone into the pitch spinbox."""
        rtty = self._mode is RTTY
        psk = self._mode is BPSK63
        # Read before touching the spinbox: switching its range can clamp
        # (and signal) the old mode's value into this mode's config field.
        pitch = self._pitch_from_config()
        self.rx_panel.set_rtty_mode(
            rtty,
            self.config.rtty_shift_hz,
            self.config.rtty_baud,
            self.config.rtty_reverse,
            self.config.rtty_unshift_on_space,
        )
        self.tx_panel.set_rtty_mode(rtty, self.config.rtty_baud, self.config.rtty_stop_bits)
        self.rx_panel.set_bpsk_mode(psk, BPSK63_BAUD)
        self.tx_panel.set_bpsk_mode(psk)
        self.rx_panel.pitch_spinbox.setValue(pitch)
        self.setWindowTitle(f"HAM Digi Robot — {self._mode.label}")

    def _pitch_from_config(self) -> int:
        """The pitch spinbox value (CW pitch / RTTY mark / BPSK63 carrier)
        the current mode keeps in the config."""
        if self._mode is RTTY:
            return self.config.rtty_mark_hz
        if self._mode is BPSK63:
            return self.config.bpsk_carrier_hz
        return self.config.target_pitch_hz

    def _store_pitch(self, hz: int) -> None:
        if self._mode is RTTY:
            self.config.rtty_mark_hz = hz
        elif self._mode is BPSK63:
            self.config.bpsk_carrier_hz = hz
        else:
            self.config.target_pitch_hz = hz

    def _set_mode(self, key: str) -> None:
        new_mode = mode_by_key(key)
        if new_mode is self._mode:
            return
        if self._tx_thread is not None:
            self.statusBar().showMessage("Stop the transmission before changing mode.")
            self._mode_actions[self._mode.key].setChecked(True)
            return
        # The old mode's pitch goes back to its own config field before the
        # spinbox is repurposed (it is also kept live by _on_target_pitch_changed).
        self._store_pitch(self.rx_panel.pitch_spinbox.value())
        self._stop_audio_pipeline()
        self._mode = new_mode
        self.config.mode = new_mode.key
        self._apply_mode_to_panels()
        self._start_audio_pipeline(self.config.rx_input_device)
        self.statusBar().showMessage(f"Mode: {new_mode.label}")
        self._apply_mode_to_rig()

    def _apply_mode_to_rig(self) -> None:
        """Send the current operating mode's setting to the rig (CAT only):
        CW -> CW, RTTY -> data-USB (Hamlib PKTUSB), falling back to USB.
        Called at startup, on every mode change and when the rig connection
        settings change. Done by the frequency-monitor thread (it owns the
        CAT link); it leaves a rig that's already in the right mode alone,
        and a failure shows up in the status bar."""
        if self.config.tx_backend != "hamlib":
            return
        model_id, _port, _baud = self.config.hamlib_effective_target()
        if model_id is None:
            return
        self._freq_monitor_set_rig_mode.emit(rig_modes_for(self._mode.key))

    def _on_rig_sideband_changed(self, sideband: str) -> None:
        # With a CAT-reported sideband the selector follows the rig and is
        # inactive; without one it is the operator's to set.
        self.rx_panel.set_rig_sideband(sideband or None)
        self._apply_cw_sideband()

    def _on_sideband_chosen(self, sideband: str) -> None:
        self.config.cw_sideband = sideband
        self._apply_cw_sideband()

    def _apply_cw_sideband(self) -> None:
        self.rx_panel.waterfall.set_sideband(self.rx_panel.sideband())

    def _sync_freq_monitor_target(self) -> None:
        """Tells the frequency-monitor thread what (if anything) to poll,
        from the current config -- called once at startup and again
        whenever the Settings dialog is accepted, since the rig
        connection settings and the TX backend choice are the only things
        that change this."""
        target = self.config.hamlib_effective_target() if self.config.tx_backend == "hamlib" else None
        if target is not None:
            self._freq_monitor_set_target.emit(*target)
        else:
            self._freq_monitor_set_target.emit(None, "", None)
        # A new rig connection (including the first one, at startup) gets
        # the current mode's settings; an unchanged one is left alone so a
        # mode the operator set by hand isn't overridden on every Settings OK.
        if target != self._last_rig_target:
            self._last_rig_target = target
            self._apply_mode_to_rig()

    def _build_menu(self) -> None:
        menu_bar = self.menuBar()

        file_menu = menu_bar.addMenu("&File")
        quit_action = QAction("&Quit", self)
        quit_action.triggered.connect(self.close)
        file_menu.addAction(quit_action)

        mode_menu = menu_bar.addMenu("&Mode")
        mode_group = QActionGroup(self)
        mode_group.setExclusive(True)
        self._mode_actions: dict[str, QAction] = {}
        for mode in ALL_MODES:
            action = QAction(mode.label, self)
            action.setCheckable(True)
            action.setChecked(mode is self._mode)
            action.triggered.connect(lambda _checked=False, key=mode.key: self._set_mode(key))
            mode_group.addAction(action)
            mode_menu.addAction(action)
            self._mode_actions[mode.key] = action

        settings_menu = menu_bar.addMenu("&Settings")
        open_settings_action = QAction("&Open Settings…", self)
        open_settings_action.triggered.connect(self._open_settings_dialog)
        settings_menu.addAction(open_settings_action)

        help_menu = menu_bar.addMenu("&Help")
        about_action = QAction("&About", self)
        about_action.triggered.connect(self._show_about)
        help_menu.addAction(about_action)

    def _open_settings_dialog(self) -> None:
        previous_input_device = self.config.rx_input_device
        dialog = SettingsDialog(self.config, self)
        if dialog.exec():
            self.tx_panel.set_manual_keying_available(self.config.tx_backend != "hamlib")
            self.qso_panel.set_frequency_editable(self.config.tx_backend != "hamlib")
            # Rig model/port/baud may have changed -- reconnect the monitor
            # against the current config rather than keep polling a now-
            # stale connection (or one for a backend that's no longer
            # configured).
            self._sync_freq_monitor_target()
            # The output device is only read fresh at send time (see
            # _build_audio_tx_backend), so nothing else to apply for it, but
            # a changed *input* device needs the already-running capture
            # pipeline actually restarted onto it.
            if self.config.rx_input_device != previous_input_device:
                self._start_audio_pipeline(self.config.rx_input_device)

    def _on_rtty_controls_changed(self, *_args: object) -> None:
        """Baud / shift / stop bits / reverse / UOS on the panels -> config
        (what the next send uses) and the running RX decoder."""
        if self._mode is not RTTY:
            return
        self.config.rtty_baud = self.tx_panel.current_baud()
        self.config.rtty_stop_bits = self.tx_panel.current_stop_bits()
        self.config.rtty_shift_hz = self.rx_panel.rtty_shift()
        self.config.rtty_reverse = self.rx_panel.rtty_reverse()
        self.config.rtty_unshift_on_space = self.rx_panel.rtty_unshift_on_space()
        self.rx_panel.set_rtty_baud(self.config.rtty_baud)
        if self._decoder is not None:
            self._decoder.core.set_params(self.config.rtty_params())
            self._decoder.core.unshift_on_space = self.config.rtty_unshift_on_space
            self._apply_effective_bandwidth()

    def _start_update_check(self) -> None:
        if not self.config.check_for_updates:
            return
        from hamdigirobot import __version__
        from hamdigirobot.updates import UpdateChecker

        self._update_checker = UpdateChecker(__version__, self)
        self._update_checker.update_available.connect(self._on_update_available)
        self._update_checker.start()

    def _on_update_available(self, tag: str, url: str) -> None:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        from PySide6.QtWidgets import QMessageBox

        from hamdigirobot import __version__

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Information)
        box.setWindowTitle("Update available")
        box.setText(f"A newer version of HAM Digi Robot is available: {tag} (you have v{__version__}).")
        download = box.addButton("Download", QMessageBox.ButtonRole.AcceptRole)
        box.addButton("Later", QMessageBox.ButtonRole.RejectRole)
        box.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        box.finished.connect(
            lambda _result: QDesktopServices.openUrl(QUrl(url)) if box.clickedButton() is download else None
        )
        # open() (not exec()) so the popup never blocks the UI -- e.g. a CW
        # send in progress -- while it waits for an answer.
        box.open()

    def _show_about(self) -> None:
        from PySide6.QtWidgets import QMessageBox

        from hamdigirobot import __version__

        QMessageBox.information(
            self,
            "About",
            f"HAM Digi Robot - v{__version__}\n\nDeveloped by HG7WHD\n\nhttps://github.com/dacrhu/hamdigirobot",
        )

    # -- Audio pipeline (capture -> ring buffer -> CwDecoder -> waterfall) --

    def _on_target_pitch_changed(self, hz: int) -> None:
        self._store_pitch(hz)
        if self._decoder is not None:
            self._decoder.target_pitch_hz = hz

    def _on_bandwidth_changed(self, slider_value: int) -> None:
        bin_span = slider_value / BANDWIDTH_SLIDER_STEPS_PER_BIN
        self.config.detection_bin_span = bin_span
        if self._decoder is not None:
            self._decoder.detection_bin_span = bin_span
            self._apply_effective_bandwidth()

    def _on_squelch_ratio_changed(self, ratio: int) -> None:
        self.config.squelch_ratio = float(ratio)
        if self._decoder is not None:
            self._decoder.squelch_ratio = float(ratio)

    def _on_auto_bandwidth_changed(self, enabled: bool) -> None:
        self.config.auto_bandwidth = enabled
        if self._decoder is not None:
            self._decoder.auto_bandwidth = enabled
            self._apply_effective_bandwidth()

    def _on_rx_speed_mode_changed(self, _value: object = None) -> None:
        self.config.auto_rx_speed = self.rx_panel.auto_rx_speed_checkbox.isChecked()
        self.config.rx_speed_wpm = self.rx_panel.rx_speed_spinbox.value()
        if self._decoder is not None:
            self._decoder.manual_wpm = self._manual_rx_wpm()

    def _manual_rx_wpm(self) -> float | None:
        return None if self.config.auto_rx_speed else float(self.config.rx_speed_wpm)

    def _apply_effective_bandwidth(self) -> None:
        if self._decoder is None:
            return
        hz = self._decoder.effective_bandwidth_hz
        self.rx_panel.set_bandwidth_label(hz)
        self.rx_panel.waterfall.set_signal_width(hz)

    def _on_rx_wpm_changed(self, wpm: float) -> None:
        self.rx_panel.set_wpm(wpm)
        self._last_rx_wpm = wpm
        self.tx_panel.match_rx_speed_button.setEnabled(True)
        # wpm_changed fires on the same ~0.25s cadence the decoder uses to
        # retune the adaptive window (see CwDecoderCore.process_block), so
        # this is the natural place to keep the bandwidth label/waterfall
        # marker live as the window changes in auto mode. A no-op-looking
        # refresh in manual mode, where the value doesn't change on its own.
        self._apply_effective_bandwidth()

    def _start_audio_pipeline(self, device_name: str | None) -> None:
        self._stop_audio_pipeline()

        # A saved/selected name can resolve to a real PortAudio device (used
        # directly) or a PipeWire source with no PortAudio equivalent (e.g.
        # another app's sink monitor) -- routed onto the stream after it
        # opens on the system default. See audio/pipewire_route.py.
        pa_device, pipewire_source = audio_devices.resolve_input_device(device_name)
        if device_name is not None and pa_device is None and pipewire_source is None:
            self.statusBar().showMessage(
                f"Audio input not found ({device_name}) — using default device."
            )

        try:
            capture = AudioCapture(device=pa_device, route_to_pipewire_source=pipewire_source)
            capture.start()
        except Exception as exc:
            self.statusBar().showMessage(f"Audio error ({device_name or 'default device'}): {exc}")
            return

        self._audio_capture = capture

        if self._mode is BPSK63:
            decoder = CwDecoder(
                ring_buffer=capture.ring_buffer,
                sample_rate=capture.samplerate,
                core=BpskDecoderCore(
                    sample_rate=capture.samplerate,
                    carrier_hz=self.config.bpsk_carrier_hz,
                    waterfall_half_span_hz=self.config.waterfall_half_span_hz,
                ),
            )
        elif self._mode is RTTY:
            decoder = CwDecoder(
                ring_buffer=capture.ring_buffer,
                sample_rate=capture.samplerate,
                core=RttyDecoderCore(
                    sample_rate=capture.samplerate,
                    params=self.config.rtty_params(),
                    unshift_on_space=self.config.rtty_unshift_on_space,
                    waterfall_half_span_hz=self.config.waterfall_half_span_hz,
                ),
            )
        else:
            decoder = CwDecoder(
                ring_buffer=capture.ring_buffer,
                sample_rate=capture.samplerate,
                target_pitch_hz=self.rx_panel.pitch_spinbox.value(),
                waterfall_half_span_hz=self.config.waterfall_half_span_hz,
            )
        decoder.detection_bin_span = self.config.detection_bin_span
        decoder.squelch_ratio = self.config.squelch_ratio
        decoder.auto_bandwidth = self.config.auto_bandwidth
        decoder.manual_wpm = self._manual_rx_wpm()

        thread = QThread(self)
        decoder.moveToThread(thread)
        thread.started.connect(decoder.run)
        decoder.spectrum_column.connect(self.rx_panel.waterfall.push_spectrum_slice)
        decoder.character_decoded.connect(self.rx_panel.append_decoded_text)
        decoder.wpm_changed.connect(self._on_rx_wpm_changed)
        decoder.squelch_state_changed.connect(self.rx_panel.set_squelch_open)
        decoder.squelch_state_changed.connect(self.rx_panel.waterfall.set_squelch_open)
        thread.start()

        self._decoder = decoder
        self._decoder_thread = thread

        self._apply_effective_bandwidth()

        label = device_name or "default device"
        self.statusBar().showMessage(f"Audio: capture running ({label}, {capture.samplerate} Hz)")

    def _stop_audio_pipeline(self) -> None:
        if self._decoder is not None:
            self._decoder.stop()
        if self._decoder_thread is not None:
            self._decoder_thread.quit()
            self._decoder_thread.wait(2000)
        self._decoder = None
        self._decoder_thread = None

        if self._audio_capture is not None:
            self._audio_capture.stop()
        self._audio_capture = None

    # -- TX pipeline (encoder -> backend, on its own QThread) --

    def _build_shortcuts(self) -> None:
        # F1-F8 (the macros) live in MacroBar. Send/Stop/Log go through the
        # buttons' own click() so their enabled state is honoured for free
        # (no Send mid-send, no Stop when idle).
        for key, button in (
            ("F12", self.tx_panel.send_button),
            ("Esc", self.tx_panel.stop_button),
            ("F9", self.qso_panel.log_button),
        ):
            shortcut = QShortcut(QKeySequence(key), self)
            shortcut.setContext(Qt.ShortcutContext.WindowShortcut)
            shortcut.activated.connect(button.click)
            button.setText(f"{button.text()}  [{key}]")

    def _on_match_rx_speed_clicked(self) -> None:
        if self._last_rx_wpm is not None:
            self.tx_panel.speed_spinbox.setValue(round(self._last_rx_wpm))

    def _on_macro_activated(self, template: str) -> None:
        # QsoPanel's field values are read fresh here (not cached anywhere)
        # so a macro always reflects whatever's currently in the QSO panel,
        # not whatever it was when the button/editor was last touched.
        qso_field_values = {key: edit.text() for key, edit in self.qso_panel.fields.items()}
        variables = macros.build_variables(self.config, qso_field_values)
        resolved = macros.resolve_macro_text(template, variables)

        # A macro replaces whatever's currently in the TX field rather than
        # inserting at the cursor -- these are canned whole messages, not
        # snippets meant to be spliced into other text.
        self.tx_panel.tx_text_edit.clear()
        cursor = self.tx_panel.tx_text_edit.textCursor()
        cursor.insertText(resolved)
        self.tx_panel.tx_text_edit.setTextCursor(cursor)
        self.tx_panel.tx_text_edit.setFocus()

    # -- QSO logging (ADIF file or UDP, see models.qso/models.adif and
    # ui.settings_dialog's "Logging" tab) --

    def _on_log_qso_clicked(self) -> None:
        callsign = self.qso_panel.fields["callsign"].text().strip()
        freq_hz = self.qso_panel.frequency_hz()

        # Both required fields get checked (and highlighted) together, not
        # one-at-a-time -- so an operator missing both sees both flagged red
        # right away, instead of fixing the callsign only to be told about
        # the frequency next.
        self.qso_panel.set_callsign_error(not callsign)
        self.qso_panel.set_frequency_error(freq_hz is None)
        if not callsign:
            self.statusBar().showMessage("Enter the other station's callsign before logging.")
            return
        if freq_hz is None:
            # Without a frequency there's no BAND either (see models.adif's
            # band_for_frequency_hz) -- both are technically optional per
            # the bare ADIF spec, but real-world logging software routinely
            # rejects an import missing them, so block here rather than
            # produce a record that silently fails to import later.
            self.statusBar().showMessage(
                "Enter or confirm the frequency before logging (needed for BAND)."
            )
            return
        if not self.config.operator_callsign:
            self.statusBar().showMessage("Set your own callsign first -- Settings → My Data.")
            return

        record = QsoRecord(
            callsign=callsign,
            freq_hz=freq_hz,
            mode=self._mode.adif_mode,
            submode=self._mode.adif_submode,
            rx_start=self.qso_panel.start_time_edit.dateTime().toPython(),
            rx_end=self.qso_panel.end_time_edit.dateTime().toPython(),
            rst_sent=self.qso_panel.fields["rst_sent"].text().strip(),
            rst_rcvd=self.qso_panel.fields["rst_rcvd"].text().strip(),
            qth=self.qso_panel.fields["qth"].text().strip(),
            locator=self.qso_panel.fields["locator"].text().strip(),
            name=self.qso_panel.fields["name"].text().strip(),
            station_callsign=self.config.operator_callsign,
            operator_name=self.config.operator_name,
        )
        try:
            build_qso_logger(self.config).log_qso(record)
        except QsoLoggingError as exc:
            self.statusBar().showMessage(f"Logging failed: {exc}")
            return
        self.statusBar().showMessage(f"Logged QSO with {record.callsign}.")
        self.qso_panel.clear_all()

    def _build_audio_tx_backend(self) -> TxBackend | None:
        device_name = self.config.tx_output_device
        pa_device, pipewire_sink = audio_devices.resolve_output_device(device_name)
        if device_name is not None and pa_device is None and pipewire_sink is None:
            self.statusBar().showMessage(
                f"Audio output not found ({device_name}) — using default device."
            )
        # The audio TX tone shares the same target pitch as the RX tuning
        # marker (Specifikacio.md: one fixed, pre-configured sidetone pitch
        # for both directions), read live from the pitch spinbox rather than
        # self.config (which is only synced back to it on close).
        frequency_hz = self.rx_panel.pitch_spinbox.value()
        backend = AudioTxBackend(
            frequency_hz=frequency_hz, device=pa_device, route_to_pipewire_sink=pipewire_sink
        )
        try:
            backend.start()
        except Exception as exc:
            self.statusBar().showMessage(f"TX audio error ({device_name or 'default device'}): {exc}")
            return None
        return backend

    def _build_rtty_tx_backend(self) -> TxBackend | None:
        return self._build_modem_tx_backend(
            lambda **kw: RttyAudioTxBackend(
                params=self.config.rtty_params(), unshift_on_space=self.config.rtty_unshift_on_space, **kw
            ),
            "RTTY",
        )

    def _build_bpsk_tx_backend(self) -> TxBackend | None:
        # The carrier is the pitch spinbox (kept live in config.bpsk_carrier_hz).
        return self._build_modem_tx_backend(
            lambda **kw: BpskAudioTxBackend(params=self.config.bpsk_params(), **kw), "BPSK63"
        )

    def _build_modem_tx_backend(self, make_backend, label: str) -> TxBackend | None:
        """Shared setup of the sound-card modem modes (RTTY, BPSK63):
        output device and, with the Hamlib TX setting, CAT PTT.
        `make_backend(device=, route_to_pipewire_sink=, rig=)` builds the
        mode's backend."""
        device_name = self.config.tx_output_device
        pa_device, pipewire_sink = audio_devices.resolve_output_device(device_name)
        if device_name is not None and pa_device is None and pipewire_sink is None:
            self.statusBar().showMessage(f"Audio output not found ({device_name}) — using default device.")

        rig: HamlibRig | None = None
        paused_monitor = False
        if self.config.tx_backend == "hamlib":  # same rig/CAT setting as the TX tab
            model_id, port_path, baud_rate = self.config.hamlib_effective_target()
            if model_id is None:
                self.statusBar().showMessage("No rig model selected for CAT PTT -- set one on the Settings → TX tab.")
                return None
            # Same one-handle-per-port rule as _build_hamlib_tx_backend.
            self._freq_monitor_pause.emit()
            paused_monitor = True
            rig = HamlibRig(model_id=model_id, port_path=port_path, baud_rate=baud_rate)

        backend = make_backend(
            device=pa_device,
            route_to_pipewire_sink=pipewire_sink,
            rig=rig,
        )
        try:
            backend.start()
        except Exception as exc:
            self.statusBar().showMessage(f"{label} TX error: {exc}")
            if paused_monitor:
                self._freq_monitor_resume.emit()
            return None
        return backend

    def _build_hamlib_tx_backend(self) -> TxBackend | None:
        model_id, port_path, baud_rate = self.config.hamlib_effective_target()
        if model_id is None:
            self.statusBar().showMessage(
                "No rig model selected -- set one on the Settings → TX tab."
            )
            return None

        # Free the port first -- see the frequency-monitor setup comment in
        # __init__ for why this and the TX connection below must never both
        # be open at once, and why this specific emit() actually blocks
        # until the monitor's connection is closed rather than just posting
        # the request. _stop_tx_pipeline resumes it once this send's own
        # connection is closed.
        self._freq_monitor_pause.emit()

        backend = HamlibTxBackend(
            model_id=model_id,
            port_path=port_path,
            baud_rate=baud_rate,
        )
        try:
            backend.start()
        except (HamlibError, HamlibUnavailableError) as exc:
            self.statusBar().showMessage(f"TX CAT error: {exc}")
            return None
        return backend

    def _on_tx_send_clicked(self) -> None:
        if self._tx_thread is not None:
            return  # a send is already in progress; the button is disabled but guard anyway

        # Kept unstripped: TxWorker.progress reports character indices into
        # this exact string, which set_tx_progress then uses directly as
        # tx_text_edit cursor positions -- stripping here would desync the
        # highlight from the widget's actual (unstripped) content.
        text = self.tx_panel.tx_text_edit.toPlainText()
        if not text.strip():
            return

        backend_name = self.config.tx_backend
        if self._mode is RTTY:
            backend = self._build_rtty_tx_backend()
        elif self._mode is BPSK63:
            backend = self._build_bpsk_tx_backend()
        elif backend_name == "hamlib":
            backend = self._build_hamlib_tx_backend()
        else:
            backend = self._build_audio_tx_backend()
        if backend is None:
            return  # a status bar message explaining why was already shown

        if self._mode is RTTY:
            wpm = self.tx_panel.current_baud()
        elif self._mode is BPSK63:
            wpm = BPSK63_BAUD
        else:
            wpm = self.tx_panel.speed_spinbox.value()
        # Manual-keying jitter only means anything for the audio backend --
        # over CAT the rig's own keyer paces the real timing regardless of
        # what we pass (see tx.hamlib_backend), and the checkbox/slider are
        # disabled in that mode anyway (ui.tx_panel.set_manual_keying_available).
        if self._mode is CW and backend_name != "hamlib" and self.tx_panel.manual_keying_checkbox.isChecked():
            jitter = manual_keying_jitter_from_level(self.tx_panel.manual_keying_slider.value())
        else:
            jitter = 0.0
        worker = TxWorker(backend=backend, text=text, wpm=wpm, jitter=jitter)
        thread = QThread(self)
        worker.moveToThread(thread)
        thread.started.connect(worker.run)
        worker.finished.connect(self._on_tx_finished)
        worker.error.connect(self._on_tx_error)
        worker.progress.connect(self.tx_panel.set_tx_progress)
        thread.start()

        self._tx_backend = backend
        self._tx_worker = worker
        self._tx_thread = thread

        self.tx_panel.set_sending(True)
        unit = "WPM" if self._mode is CW else "baud"
        self.statusBar().showMessage(f"Sending ({wpm:g} {unit})…")

    def _on_tx_stop_clicked(self) -> None:
        if self._tx_worker is not None:
            self._tx_worker.stop()

    def _on_tx_error(self, message: str) -> None:
        self.statusBar().showMessage(message)

    def _on_tx_finished(self) -> None:
        self._stop_tx_pipeline()
        self.tx_panel.set_sending(False)
        self.statusBar().showMessage("Send complete.")

    def _stop_tx_pipeline(self) -> None:
        if self._tx_worker is not None:
            self._tx_worker.stop()
        if self._tx_thread is not None:
            self._tx_thread.quit()
            self._tx_thread.wait(2000)
        if self._tx_backend is not None:
            self._tx_backend.close()
        self._tx_backend = None
        self._tx_worker = None
        self._tx_thread = None
        # The port is free again (whether or not this was a Hamlib CAT send
        # -- resuming when it was never paused is a harmless no-op) -- see
        # _build_hamlib_tx_backend's own pause().
        self._freq_monitor_resume.emit()

    def closeEvent(self, event) -> None:  # noqa: N802 (Qt override)
        self._stop_audio_pipeline()
        self._stop_tx_pipeline()
        self._freq_monitor_stop.emit()
        self._freq_monitor_thread.quit()
        self._freq_monitor_thread.wait(2000)

        # Persist current UI state back into the config before closing.
        self._store_pitch(self.rx_panel.pitch_spinbox.value())
        self.config.mode = self._mode.key
        self.config.tx_speed_wpm = self.tx_panel.speed_spinbox.value()
        self.config.manual_keying_emulation = self.tx_panel.manual_keying_checkbox.isChecked()
        self.config.manual_keying_intensity = self.tx_panel.manual_keying_slider.value()
        self.config.save()
        super().closeEvent(event)
