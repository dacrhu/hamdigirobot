"""Which sideband (LSB/USB) a Hamlib operating mode puts the signal on --
for the CW waterfall's sideband marker. Pure function, no libhamlib needed.
"""

from __future__ import annotations

from hamdigirobot.hamlib import ctypes_bindings as b

_USB_MODES = b.RIG_MODE_USB | b.RIG_MODE_PKTUSB | b.RIG_MODE_ECSSUSB | b.RIG_MODE_CW | b.RIG_MODE_RTTYR
_LSB_MODES = b.RIG_MODE_LSB | b.RIG_MODE_PKTLSB | b.RIG_MODE_ECSSLSB | b.RIG_MODE_CWR | b.RIG_MODE_RTTY


def sideband_from_mode(mode: int) -> str | None:
    """"USB" / "LSB", or None when the mode has no sideband (AM, FM, ...)
    or is unknown. Hamlib's "CW" is the normal (upper) CW sideband and
    "CWR" the reversed (lower) one."""
    if mode & _USB_MODES and not mode & _LSB_MODES:
        return "USB"
    if mode & _LSB_MODES and not mode & _USB_MODES:
        return "LSB"
    return None


def rig_modes_for(mode_key: str) -> list[int]:
    """Rig modes to try, in order, when the app is in operating mode
    `mode_key` ("cw" / "rtty"). RTTY is AFSK through the sound card, so the
    rig wants its *data* USB mode: Hamlib's PKTUSB is the universal
    selector for it (Yaesu DATA-USB, Icom USB-D, ... -- the backend maps it),
    with plain USB as the fallback for a rig/backend that has no data mode
    (it rejects PKTUSB). CW: the rig's CW mode.
    """
    if mode_key == "rtty":
        return [b.RIG_MODE_PKTUSB, b.RIG_MODE_USB]
    return [b.RIG_MODE_CW]


_UPPER_VOICE_DATA = b.RIG_MODE_USB | b.RIG_MODE_PKTUSB | b.RIG_MODE_ECSSUSB


def mode_took_effect(wanted: int, reported: int) -> bool:
    """Did a rig_set_mode(`wanted`) stick, judging by the mode the rig
    reports afterwards? A data-USB request may legitimately read back as
    USB (some backends report the data flag separately), but never as CW --
    which sideband_from_mode alone would call "USB" too."""
    if wanted & _UPPER_VOICE_DATA:
        return bool(reported & _UPPER_VOICE_DATA)
    return reported == wanted
