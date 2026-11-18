"""Application configuration: a plain, human-readable JSON file.

We deliberately avoid QSettings in favor
of a dataclasses-based config with an explicit schema version, so the config
file stays easy to read/edit by hand (useful when debugging Hamlib port/baud
issues) and can be migrated cleanly as the schema grows (e.g. toward the
future external-logger integration).
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Literal

from hamdigirobot.hamlib.ctypes_bindings import RIG_MODEL_NETRIGCTL

CONFIG_VERSION = 15

TxBackendName = Literal["audio", "hamlib"]
SidebandChoice = Literal["lsb", "usb"]
HamlibConnectionType = Literal["serial", "network"]
LoggingMethod = Literal["file", "udp"]
LoggingUdpFormat = Literal["adif", "wsjtx"]
ModeName = Literal["cw", "rtty", "bpsk63"]


def config_dir() -> Path:
    """Return the XDG-compliant config directory for hamdigirobot, creating it if needed."""
    xdg_config_home = os.environ.get("XDG_CONFIG_HOME")
    base = Path(xdg_config_home) if xdg_config_home else Path.home() / ".config"
    directory = base / "hamdigirobot"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def config_path() -> Path:
    return config_dir() / "config.json"


@dataclass
class AppConfig:
    config_version: int = CONFIG_VERSION

    # Active operating mode (see hamdigirobot.modes). The CW settings below
    # (target_pitch_hz, tx_speed_wpm, ...) stay as they were; each further
    # mode adds its own prefixed fields (rtty_*).
    mode: ModeName = "cw"

    # Shared RX/TX pitch reference (the radio's sidetone
    # pitch is fixed and pre-configured here; the operator tunes the radio's
    # VFO to match it, aided by the waterfall marker).
    target_pitch_hz: int = 600

    # The sideband the radio is on, for the CW waterfall's one-sided marker
    # (the LSB/USB selector next to the pitch). Only used while there is no
    # CAT link; with one, the rig's own mode wins (see ui.rx_panel).
    cw_sideband: SidebandChoice = "usb"

    # TX
    tx_speed_wpm: int = 20
    tx_backend: TxBackendName = "audio"
    tx_output_device: str | None = None
    # "Kezi adas emulalasa" -- see tx.encoder.text_to_elements's jitter
    # param and ui.tx_panel's manual_keying_checkbox. Intensity is the
    # unitless 1-8 slider level (see tx.encoder.manual_keying_jitter_from_level
    # for the actual jitter-fraction mapping) -- default 1 matches
    # MANUAL_KEYING_LEVEL_MIN there.
    manual_keying_emulation: bool = False
    manual_keying_intensity: int = 1

    # RX
    rx_input_device: str | None = None
    # Software levels (percent): RX input gain and TX audio output level.
    # Mainly for the sound-card modes; the output level does nothing for CW over CAT.
    rx_input_gain_percent: int = 100
    tx_output_level_percent: int = 100
    waterfall_half_span_hz: int = 1000
    detection_bin_span: float = 1.0  # Goertzel bins +/- target; see dsp.tone_detector
    squelch_ratio: float = 14.0
    auto_bandwidth: bool = True  # window/bandwidth auto-tracks detected WPM; see dsp.tone_detector

    auto_rx_speed: bool = True  # RX keying speed auto-detected; else fixed at rx_speed_wpm
    rx_speed_wpm: int = 20

    # RTTY (AFSK through the sound card). target_pitch_hz is the CW tone;
    # in RTTY mode the pitch spinbox edits rtty_mark_hz instead (the space
    # tone is mark + shift; "reverse" swaps which of the two means mark).
    bpsk_carrier_hz: int = 1000  # BPSK63 audio carrier (the pitch spinbox in that mode)
    rtty_baud: float = 45.45
    rtty_shift_hz: int = 170
    rtty_mark_hz: int = 2125
    rtty_stop_bits: float = 1.5
    rtty_reverse: bool = False
    rtty_unshift_on_space: bool = False  # only if the other side does UOS too
    # PTT follows the TX tab: tx_backend "hamlib" keys the rig over CAT around
    # each RTTY transmission; "audio" leaves keying to VOX / the audio itself.

    # Hamlib CAT. Two ways to reach the rig (see hamlib_effective_target):
    # a local serial port (rig_model_id + port_path + baud_rate), or a TCP
    # connection to a running rigctld daemon (network_host:network_port),
    # which is Hamlib's own NETRIGCTL backend -- rigctld already owns the
    # real rig, so no rig model needs picking in that mode.
    hamlib_connection_type: HamlibConnectionType = "serial"
    hamlib_rig_model_id: int | None = None
    hamlib_port_path: str = ""
    hamlib_baud_rate: int = 9600
    hamlib_network_host: str = "localhost"
    hamlib_network_port: int = 4532  # rigctld's default port

    # Own (operator) data -- see ui.settings_dialog. operator_callsign also
    # doubles as QsoRecord.station_callsign (the de-facto counterpart to
    # each logged QSO's remote callsign) when logging -- see models.qso.
    operator_callsign: str = ""
    operator_name: str = ""
    operator_locator: str = ""
    operator_qth: str = ""

    # QSO logging (see ui.qso_panel's "Log QSO" button, models.qso) -- either
    # an ADIF file (one per operator callsign, in logging_folder) or ADIF
    # over UDP to logging_udp_host:logging_udp_port, itself in either of two
    # wire formats (logging_udp_format -- "wsjtx" by default: the WSJT-X
    # Network Message protocol most current companion loggers speak, e.g.
    # QLog/JTAlert/GridTracker; "adif" is a bare ADIF record instead, for
    # Log4OM-style listeners). See ui.settings_dialog's "Logging" tab.
    logging_method: LoggingMethod = "file"
    logging_folder: str = ""
    logging_udp_host: str = "127.0.0.1"
    logging_udp_port: int = 2237
    logging_udp_format: LoggingUdpFormat = "wsjtx"

    # TX quick-button macro texts (see hamdigirobot.macros / ui.macro_bar),
    # keyed by MacroDefinition.key. Only overrides live here -- a macro
    # missing from this dict just falls back to its DEFAULT_MACROS text,
    # so shipping a new default macro in a later version never needs a
    # migration step.
    macro_texts: dict[str, str] = field(default_factory=dict)
    # Operator renames of the quick buttons, same keying/override scheme as
    # macro_texts (a missing or blank entry means "use the default label").
    macro_labels: dict[str, str] = field(default_factory=dict)

    # Check GitHub Releases for a newer version at startup (see hamdigirobot.updates).
    check_for_updates: bool = True

    @classmethod
    def load(cls) -> "AppConfig":
        path = config_path()
        if not path.exists():
            return cls()
        try:
            with path.open("r", encoding="utf-8") as f:
                data = json.load(f)
        except (json.JSONDecodeError, OSError):
            # Corrupt or unreadable config: fall back to defaults rather than crash.
            return cls()
        data = _migrate(data)
        known_fields = {f.name for f in cls.__dataclass_fields__.values()}
        filtered = {k: v for k, v in data.items() if k in known_fields}
        return cls(**filtered)

    def bpsk_params(self):
        """The BPSK63 modem settings as a tx.bpsk_modulator.BpskParams."""
        from hamdigirobot.tx.bpsk_modulator import BpskParams

        return BpskParams(freq_hz=float(self.bpsk_carrier_hz))

    def rtty_params(self):
        """The RTTY modem settings as a tx.rtty_modulator.RttyParams."""
        from hamdigirobot.tx.rtty_modulator import RttyParams

        return RttyParams(
            baud=float(self.rtty_baud),
            shift_hz=float(self.rtty_shift_hz),
            mark_hz=float(self.rtty_mark_hz),
            stop_bits=float(self.rtty_stop_bits),
            reverse=bool(self.rtty_reverse),
        )

    def hamlib_effective_target(self) -> tuple[int | None, str, int | None]:
        """The (model_id, port_path, baud_rate) triple to hand to
        HamlibRig / HamlibTxBackend / FrequencyMonitor, resolving the
        serial-vs-network connection choice into what those already
        understand. In "network" mode that's Hamlib's NETRIGCTL backend
        with a "host:port" pathname and no serial speed; in "serial" mode
        it's just the configured serial fields verbatim (model_id may be
        None there -- meaning "no rig picked yet").
        """
        if self.hamlib_connection_type == "network":
            return (
                RIG_MODEL_NETRIGCTL,
                f"{self.hamlib_network_host}:{self.hamlib_network_port}",
                None,
            )
        return (self.hamlib_rig_model_id, self.hamlib_port_path, self.hamlib_baud_rate)

    def save(self) -> None:
        path = config_path()
        tmp_path = path.with_suffix(".json.tmp")
        with tmp_path.open("w", encoding="utf-8") as f:
            json.dump(asdict(self), f, indent=2, ensure_ascii=False)
            f.write("\n")
        tmp_path.replace(path)


def _migrate(data: dict) -> dict:
    """Placeholder for forward config migrations, keyed off config_version."""
    version = data.get("config_version", 1)
    if version < 2:
        data.setdefault("auto_bandwidth", True)
    if version < 3:
        data.setdefault("operator_callsign", "")
        data.setdefault("operator_name", "")
        data.setdefault("operator_locator", "")
        data.setdefault("operator_qth", "")
    if version < 4:
        data.setdefault("macro_texts", {})
    if version < 5:
        data.setdefault("manual_keying_emulation", False)
    if version < 6:
        data.setdefault("manual_keying_intensity", 1)
    if version < 7:
        data.setdefault("logging_method", "file")
        data.setdefault("logging_folder", "")
        data.setdefault("logging_udp_host", "127.0.0.1")
        data.setdefault("logging_udp_port", 2237)
    if version < 8:
        data.setdefault("logging_udp_format", "wsjtx")
    if version < 9:
        data.setdefault("hamlib_connection_type", "serial")
        data.setdefault("hamlib_network_host", "localhost")
        data.setdefault("hamlib_network_port", 4532)
    if version < 10:
        data.setdefault("auto_rx_speed", True)
        data.setdefault("rx_speed_wpm", 20)
    if version < 11:
        data.setdefault("macro_labels", {})
    if version < 12:
        data.setdefault("check_for_updates", True)
    if version < 13:
        data.setdefault("mode", "cw")
        data.setdefault("rtty_baud", 45.45)
        data.setdefault("rtty_shift_hz", 170)
        data.setdefault("rtty_mark_hz", 2125)
        data.setdefault("rtty_stop_bits", 1.5)
        data.setdefault("rtty_reverse", False)
        data.setdefault("rtty_unshift_on_space", False)
    if version < 14:
        data.setdefault("cw_sideband", "usb")
    if version < 15:
        data.setdefault("bpsk_carrier_hz", 1000)
    if data.get("cw_sideband") not in ("lsb", "usb"):
        data["cw_sideband"] = "usb"  # e.g. an "auto" from a development build
    if version < CONFIG_VERSION:
        data["config_version"] = CONFIG_VERSION
    return data
