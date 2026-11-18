"""BPSK63 modulator: Varicode bits -> a single phase-modulated carrier.

Pure numpy (no Qt/audio device), also the signal source of the decoder's
synthetic tests.

BPSK63 = 62.5 symbols/s. A 0 bit is a 180-degree phase reversal, a 1 bit
keeps the phase. The baseband is sum_k a_k * g(t - centre_k) with a_k = +-1
(the carrier's sign in symbol k) and g(x) = (1 + cos(pi x)) / 2 on |x| < 1
(x in symbol times): a raised-cosine envelope that is exactly flat while
the phase stays and passes through zero in the middle of a reversal, so the
signal stays narrow (~ 2 x the symbol rate) and does not click.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

from hamdigirobot.modes.varicode import encode_text

SAMPLE_RATE = 48_000
BAUD = 62.5  # BPSK63 symbol rate
LEAD_IDLE_SYMBOLS = 32  # reversals before the text (decoder sync), 0.5 s
TAIL_IDLE_SYMBOLS = 8
FADE_MS = 5.0
AMPLITUDE = 0.8


@dataclass(frozen=True)
class BpskParams:
    freq_hz: float = 1000.0  # carrier (audio) frequency
    baud: float = BAUD


@dataclass
class BpskWaveform:
    samples: np.ndarray  # float32, mono
    sample_rate: int
    # (first_sample, text_index) -- when each source character starts.
    char_starts: list[tuple[int, int]]


def _g(x: np.ndarray) -> np.ndarray:
    return np.where(np.abs(x) < 1.0, 0.5 * (1.0 + np.cos(np.pi * x)), 0.0)


def synthesize(text: str, params: BpskParams, sample_rate: int = SAMPLE_RATE) -> BpskWaveform | None:
    """None when `text` has nothing sendable."""
    chars = encode_text(text)
    if not chars:
        return None
    bits = "0" * LEAD_IDLE_SYMBOLS
    starts: list[tuple[int, int]] = []  # (symbol index, text index)
    for code, text_index in chars:
        starts.append((len(bits), text_index))
        bits += code
    bits += "0" * TAIL_IDLE_SYMBOLS

    n_sym = len(bits)
    signs = np.empty(n_sym + 2)  # +-1 per symbol, with a repeated pad symbol at each end
    sign = 1.0
    signs[1] = sign
    for k, b in enumerate(bits):
        if b == "0":
            sign = -sign
        signs[k + 1] = sign
    signs[0], signs[-1] = signs[1], signs[-2]

    samples_per_symbol = sample_rate / params.baud
    total = int(round(n_sym * samples_per_symbol))
    n = np.arange(total)
    pos = n / samples_per_symbol
    k = np.minimum(pos.astype(np.int64), n_sym - 1)
    u = pos - k
    base = (
        signs[k + 1] * _g(u - 0.5)
        + signs[k] * _g(u + 0.5)
        + signs[k + 2] * _g(u - 1.5)
    )
    wave = AMPLITUDE * base * np.sin(2.0 * np.pi * params.freq_hz * n / sample_rate)

    fade = min(int(FADE_MS * sample_rate / 1000.0), total // 2)
    if fade > 0:
        ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(fade) / fade)
        wave[:fade] *= ramp
        wave[-fade:] *= ramp[::-1]

    char_starts = [(int(round(sym * samples_per_symbol)), ti) for sym, ti in starts]
    return BpskWaveform(samples=wave.astype(np.float32), sample_rate=sample_rate, char_starts=char_starts)
