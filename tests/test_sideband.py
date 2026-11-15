from unittest.mock import MagicMock

import pytest

from hamdigirobot.hamlib import ctypes_bindings as b
from hamdigirobot.hamlib.frequency_monitor import FrequencyMonitor
from hamdigirobot.hamlib.rig_client import HamlibError
from hamdigirobot.hamlib.sideband import rig_modes_for, sideband_from_mode


@pytest.mark.parametrize(
    "mode,expected",
    [
        (b.RIG_MODE_USB, "USB"),
        (b.RIG_MODE_PKTUSB, "USB"),
        (b.RIG_MODE_CW, "USB"),
        (b.RIG_MODE_LSB, "LSB"),
        (b.RIG_MODE_CWR, "LSB"),
        (b.RIG_MODE_RTTY, "LSB"),
        (1 << 0, None),  # AM
        (1 << 5, None),  # FM
        (0, None),
        (b.RIG_MODE_USB | b.RIG_MODE_LSB, None),  # nonsense mask: don't guess
    ],
)
def test_sideband_from_mode(mode, expected):
    assert sideband_from_mode(mode) == expected


def _monitor(rig):
    m = FrequencyMonitor()
    m._model_id = 1
    m._rig = rig
    emitted = []
    m.sideband_changed.connect(emitted.append)
    return m, emitted


def test_monitor_emits_sideband_only_on_change():
    rig = MagicMock()
    rig.get_freq.return_value = 14_030_000.0
    rig.get_mode.return_value = b.RIG_MODE_USB
    m, emitted = _monitor(rig)
    m._poll()
    m._poll()
    rig.get_mode.return_value = b.RIG_MODE_LSB
    m._poll()
    assert emitted == ["USB", "LSB"]


def test_unreadable_mode_means_unknown_but_keeps_polling_frequency():
    rig = MagicMock()
    rig.get_freq.return_value = 7_030_000.0
    rig.get_mode.side_effect = HamlibError("mode", -11)
    m, emitted = _monitor(rig)
    freqs = []
    m.frequency_changed.connect(freqs.append)
    m._poll()
    assert freqs == [7_030_000.0]
    assert emitted == [""]
    assert m._rig is rig  # connection kept


def test_pause_does_not_reset_sideband():
    rig = MagicMock()
    rig.get_freq.return_value = 1.0
    rig.get_mode.return_value = b.RIG_MODE_USB
    m, emitted = _monitor(rig)
    m._poll()
    m.pause()
    m.resume()
    assert emitted == ["USB"]


def test_config_normalises_legacy_auto_sideband():
    from hamdigirobot.config import _migrate

    assert _migrate({"config_version": 14, "cw_sideband": "auto"})["cw_sideband"] == "usb"
    assert _migrate({"config_version": 14, "cw_sideband": "lsb"})["cw_sideband"] == "lsb"


def test_rig_modes_for_each_operating_mode():
    # Hamlib's PKTUSB is the universal data-USB selector (Yaesu DATA-USB, ...);
    # plain USB only as the fallback for a rig without a data mode.
    assert rig_modes_for("rtty") == [b.RIG_MODE_PKTUSB, b.RIG_MODE_USB]
    assert rig_modes_for("cw") == [b.RIG_MODE_CW]


def _rig(current, set_effects=None, readback=None):
    rig = MagicMock()
    rig.get_freq.return_value = 1.0
    modes = iter([current] + ([readback] if readback is not None else []))
    rig.get_mode.side_effect = lambda: next(modes, readback if readback is not None else current)
    if set_effects is not None:
        rig.set_mode.side_effect = set_effects
    return rig


def test_set_rig_mode_sends_data_usb_when_rig_is_in_cw():
    rig = _rig(b.RIG_MODE_CW, readback=b.RIG_MODE_PKTUSB)
    m, emitted = _monitor(rig)
    errors = []
    m.rig_mode_error.connect(errors.append)
    m.set_rig_mode(rig_modes_for("rtty"))
    rig.set_mode.assert_called_once_with(b.RIG_MODE_PKTUSB)
    assert errors == []
    assert emitted == ["USB"]


def test_set_rig_mode_leaves_rig_alone_if_already_in_the_mode():
    rig = _rig(b.RIG_MODE_PKTUSB)
    m, _ = _monitor(rig)
    m.set_rig_mode(rig_modes_for("rtty"))
    rig.set_mode.assert_not_called()


def test_set_rig_mode_falls_back_to_plain_usb_when_rig_has_no_data_mode():
    rig = _rig(b.RIG_MODE_CW, set_effects=[HamlibError("no data mode", -1), None], readback=b.RIG_MODE_USB)
    m, _ = _monitor(rig)
    errors = []
    m.rig_mode_error.connect(errors.append)
    m.set_rig_mode(rig_modes_for("rtty"))
    assert [c.args[0] for c in rig.set_mode.call_args_list] == [b.RIG_MODE_PKTUSB, b.RIG_MODE_USB]
    assert errors == []


def test_set_rig_mode_verifies_by_reading_back():
    # The rig "accepts" the command but stays in CW -> reported, not assumed.
    rig = _rig(b.RIG_MODE_CW, readback=b.RIG_MODE_CW)
    m, _ = _monitor(rig)
    errors = []
    m.rig_mode_error.connect(errors.append)
    m.set_rig_mode(rig_modes_for("rtty"))
    assert len(errors) == 1


def test_set_rig_mode_to_cw():
    rig = _rig(b.RIG_MODE_USB, readback=b.RIG_MODE_CW)
    m, _ = _monitor(rig)
    m.set_rig_mode(rig_modes_for("cw"))
    rig.set_mode.assert_called_once_with(b.RIG_MODE_CW)


def test_set_rig_mode_reports_failure():
    rig = _rig(b.RIG_MODE_CW, set_effects=HamlibError("no", -1))
    m, _ = _monitor(rig)
    errors = []
    m.rig_mode_error.connect(errors.append)
    m.set_rig_mode(rig_modes_for("rtty"))
    assert len(errors) == 1


def test_request_waits_for_the_cat_link_and_is_applied_on_connect(monkeypatch):
    rig = _rig(b.RIG_MODE_CW, readback=b.RIG_MODE_PKTUSB)
    m = FrequencyMonitor()
    m._model_id = 1
    errors = []
    m.rig_mode_error.connect(errors.append)
    state = {"up": False}

    def ensure():
        if state["up"]:
            m._rig = rig
        return state["up"]

    monkeypatch.setattr(m, "_ensure_rig", ensure)
    m.set_rig_mode(rig_modes_for("rtty"))  # rig off: waits, says so once
    m._poll()
    assert len(errors) == 1 and "as soon as" in errors[0]
    rig.set_mode.assert_not_called()
    state["up"] = True
    m._poll()  # link came up -> mode applied
    rig.set_mode.assert_called_once_with(b.RIG_MODE_PKTUSB)


def test_set_rig_mode_ignored_while_paused():
    rig = MagicMock()
    m, _ = _monitor(rig)
    m.pause()
    m.set_rig_mode([b.RIG_MODE_USB])
    rig.set_mode.assert_not_called()
