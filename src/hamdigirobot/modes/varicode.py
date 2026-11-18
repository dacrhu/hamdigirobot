"""PSK31 Varicode (G3PLX): the variable-length character code used by the
PSK modes (BPSK31/63/125, ...).

Each character is a bit string that starts and ends with 1 and never
contains two consecutive 0s; characters are separated by "00". Frequent
characters are short ("e" = 11, " " = 1), rare ones long. On the air a 0 is
a phase reversal and a 1 is "no change" (see tx.bpsk_modulator).

Only the 7-bit ASCII range 0-122 is implemented ("{", "|", "}", "~" and DEL
are left out; they are rare in QSOs). Qt-free, pure Python.
"""

from __future__ import annotations

# Index = ASCII code.
_CODES: tuple[str, ...] = (
    "1010101011", "1011011011", "1011101101", "1101110111", "1011101011",  # 0-4
    "1101011111", "1011101111", "1011111101", "1011111111", "11101111",  # 5-9
    "11101", "1101101111", "1011011101", "11111", "1101110101",  # 10-14 (LF=10, CR=13)
    "1110101011", "1011110111", "1011110101", "1110101101", "1110101111",  # 15-19
    "1101011011", "1101101011", "1101101101", "1101010111", "1101111011",  # 20-24
    "1101111101", "1110110111", "1101010101", "1101011101", "1110111011",  # 25-29
    "1011111011", "1101111111",  # 30-31
    "1", "111111111", "101011111", "111110101", "111011011",  # 32-36   ! " # $
    "1011010101", "1010111011", "101111111", "11111011", "11110111",  # 37-41  % & ' ( )
    "101101111", "111011111", "1110101", "110101", "1010111",  # 42-46      * + , - .
    "110101111",  # 47 /
    "10110111", "10111101", "11101101", "11111111", "101110111",  # 48-52   0-4
    "101011011", "101101011", "110101101", "110101011", "110110111",  # 53-57 5-9
    "11110101", "110111101", "111101101", "1010101", "111010111",  # 58-62   : ; < = >
    "1010101111", "1010111101",  # 63-64  ? @
    "1111101", "11101011", "10101101", "10110101", "1110111",  # 65-69   A-E
    "11011011", "11111101", "101010101", "1111111", "111111101",  # 70-74 F-J
    "101111101", "11010111", "10111011", "11011101", "10101011",  # 75-79 K-O
    "11010101", "111011101", "10101111", "1101111", "1101101",  # 80-84   P-T
    "101010111", "110110101", "101011101", "101110101", "101111011",  # 85-89 U-Y
    "1010101101",  # 90 Z
    "111110111", "111101111", "111111011", "1010111111", "101101101",  # 91-95 [ \ ] ^ _
    "1011011111",  # 96 `
    "1011", "1011111", "101111", "101101", "11",  # 97-101  a-e
    "111101", "1011011", "101011", "1101", "111101011",  # 102-106 f-j
    "10111111", "11011", "111011", "1111", "111",  # 107-111 k-o
    "111111", "110111111", "10101", "10111", "101",  # 112-116 p-t
    "110111", "1111011", "1101011", "11011111", "1011101",  # 117-121 u-y
    "111010101",  # 122 z
)

_ENCODE: dict[str, str] = {chr(i): code for i, code in enumerate(_CODES)}
_DECODE: dict[str, str] = {code: chr(i) for i, code in enumerate(_CODES)}

SEPARATOR = "00"
MAX_CODE_BITS = 10


def encode_text(text: str) -> list[tuple[str, int]]:
    """-> [(bits, text_index)]: each character's code *including* the
    trailing "00" separator. "\\n" is sent as CR LF; characters without a
    code are skipped. `text_index` is the index into `text` (for the TX
    progress highlight)."""
    out: list[tuple[str, int]] = []
    for index, ch in enumerate(text):
        if ch == "\n":
            out.append((_ENCODE["\r"] + SEPARATOR + _ENCODE["\n"] + SEPARATOR, index))
            continue
        code = _ENCODE.get(ch)
        if code is not None:
            out.append((code + SEPARATOR, index))
    return out


class VaricodeDecoder:
    """Bit-at-a-time Varicode -> text. CR becomes "\\n" and a LF directly
    after a CR is swallowed, so both "CR", "LF" and "CR LF" line ends give
    exactly one newline; unknown bit patterns are dropped."""

    def __init__(self) -> None:
        self._bits = ""
        self._last_was_cr = False

    def reset(self) -> None:
        self._bits = ""
        self._last_was_cr = False

    def feed(self, bit: int) -> str:
        if bit:
            self._bits += "1"
        elif self._bits:
            if self._bits.endswith("0"):  # "00": end of character
                code, self._bits = self._bits[:-1], ""
                return self._emit(code)
            self._bits += "0"
        # a 0 with nothing buffered is idle: ignored
        if len(self._bits) > MAX_CODE_BITS + 1:  # garbage: resynchronise
            self._bits = ""
        return ""

    def _emit(self, code: str) -> str:
        ch = _DECODE.get(code)
        if ch is None:
            return ""
        if ch == "\r":
            self._last_was_cr = True
            return "\n"
        was_cr, self._last_was_cr = self._last_was_cr, False
        if ch == "\n":
            return "" if was_cr else "\n"
        return ch
