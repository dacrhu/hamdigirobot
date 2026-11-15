"""Operating modes (adásmódok) HAM Digi Robot supports.

The UI layout is shared between modes; only the mode-specific controls
(speed vs. baud, pitch vs. mark tone, ...) and the TX/RX signal chains
differ. `ModeSpec` is the small, Qt-free registry entry the rest of the app
keys off (menu, window title, ADIF MODE field); the actual encoders and
decoders live next to the CW ones (tx/, decoder/) and are picked in
ui.main_window.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ModeSpec:
    key: str  # value stored in config.AppConfig.mode
    label: str  # shown in the Mode menu / window title
    adif_mode: str  # ADIF MODE field value for QSO logging


CW = ModeSpec("cw", "CW", "CW")
RTTY = ModeSpec("rtty", "RTTY", "RTTY")

ALL_MODES: list[ModeSpec] = [CW, RTTY]
_BY_KEY = {m.key: m for m in ALL_MODES}


def mode_by_key(key: str) -> ModeSpec:
    """Unknown/corrupt keys fall back to CW (the original mode)."""
    return _BY_KEY.get(key, CW)
