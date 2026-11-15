"""Baudot / ITA2 (US-TTY variant) 5-bit code for RTTY.

Pure Python, no Qt. Encoding yields `(code, text_index)` pairs so the TX
progress highlight can follow the source text; shift codes (LTRS/FIGS)
inserted before a character carry that character's `text_index`.
"""

from __future__ import annotations

LTRS_CODE = 31
FIGS_CODE = 27
SPACE_CODE = 4
CR_CODE = 8
LF_CODE = 2

# Index = 5-bit code. None = no printable character (NUL, shift codes).
_LTRS: list[str | None] = [
    None, "E", "\n", "A", " ", "S", "I", "U",
    "\r", "D", "R", "J", "N", "F", "C", "K",
    "T", "Z", "L", "W", "H", "Y", "P", "Q",
    "O", "B", "G", None, "M", "X", "V", None,
]  # fmt: skip
# US-TTY figures case; "\a" (code 5) is BELL, "#" (20) stands in for the
# old "stop" slot.
_FIGS: list[str | None] = [
    None, "3", "\n", "-", " ", "\a", "8", "7",
    "\r", "$", "4", "'", ",", "!", ":", "(",
    "5", '"', ")", "2", "#", "6", "0", "1",
    "9", "?", "&", None, ".", "/", ";", None,
]  # fmt: skip

_CHAR_TO_LTRS = {c: i for i, c in enumerate(_LTRS) if c is not None and c not in ("\r", "\n")}
_CHAR_TO_FIGS = {c: i for i, c in enumerate(_FIGS) if c is not None and c not in ("\r", "\n", "\a", " ")}


def _normalize_newlines(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def encode_text(text: str, unshift_on_space: bool = False) -> list[tuple[int, int]]:
    """Text -> [(5-bit code, text_index)]. Case-insensitive; characters with
    no Baudot equivalent are skipped. The shift state starts unknown, so the
    first character always gets an explicit LTRS/FIGS first.

    `unshift_on_space`: assume the receiver drops back to letters after every
    space (so no LTRS is sent after "599 " before the next word). Only turn
    on when the other station's decoder does UOS too.
    """
    codes: list[tuple[int, int]] = []
    shift: str | None = None  # "L", "F" or None (unknown)
    text = _normalize_newlines(text.upper())
    for index, char in enumerate(text):
        if char == "\n":
            codes.append((CR_CODE, index))
            codes.append((LF_CODE, index))
            continue
        if char == " ":
            codes.append((SPACE_CODE, index))
            if unshift_on_space:
                shift = "L"
            continue
        if char in _CHAR_TO_LTRS:
            if shift != "L":
                codes.append((LTRS_CODE, index))
                shift = "L"
            codes.append((_CHAR_TO_LTRS[char], index))
        elif char in _CHAR_TO_FIGS:
            if shift != "F":
                codes.append((FIGS_CODE, index))
                shift = "F"
            codes.append((_CHAR_TO_FIGS[char], index))
        # else: unsupported character, dropped
    return codes


class BaudotDecoder:
    """Stateful 5-bit code -> text. Starts in letters shift."""

    def __init__(self, unshift_on_space: bool = False) -> None:
        self.unshift_on_space = unshift_on_space
        self._figures = False

    def reset(self) -> None:
        self._figures = False

    def feed(self, code: int) -> str:
        code &= 0x1F
        if code == LTRS_CODE:
            self._figures = False
            return ""
        if code == FIGS_CODE:
            self._figures = True
            return ""
        char = (_FIGS if self._figures else _LTRS)[code]
        if code == SPACE_CODE and self.unshift_on_space:
            self._figures = False
        if char is None or char == "\a":
            return ""
        if char == "\r":
            return ""  # CR is swallowed; LF alone starts the new line
        return char
