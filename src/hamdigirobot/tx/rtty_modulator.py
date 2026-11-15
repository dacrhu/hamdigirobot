"""RTTY AFSK modulator: Baudot codes -> phase-continuous two-tone audio.

Pure numpy, no Qt/audio device, so it is directly unit-testable and also
the signal source for the decoder's synthetic tests.

Framing per character: 1 start bit (space), 5 data bits LSB first
(1 = mark), `stop_bits` stop bits (mark; fractional values such as 1.5 are
honoured). The tone phase never jumps at a bit boundary (only the
frequency changes), which keeps the signal's spectrum narrow.
"""

from __future__ import annotations

from dataclasses import dataclass

import numpy as np

SAMPLE_RATE = 48_000
LEAD_IN_MS = 250.0  # idle mark before the first character (AGC/PTT settle)
TAIL_MS = 60.0  # idle mark after the last stop bit
FADE_MS = 5.0  # raised-cosine ramp on both ends against key clicks


@dataclass(frozen=True)
class RttyParams:
    baud: float = 45.45
    shift_hz: float = 170.0
    mark_hz: float = 2125.0
    stop_bits: float = 1.5
    reverse: bool = False  # swap which tone is mark (inverted/LSB setups)

    @property
    def space_hz(self) -> float:
        return self.mark_hz + self.shift_hz

    def tones(self) -> tuple[float, float]:
        """(frequency used for mark, frequency used for space) -- already
        accounting for `reverse`."""
        if self.reverse:
            return self.space_hz, self.mark_hz
        return self.mark_hz, self.space_hz


@dataclass
class RttyWaveform:
    samples: np.ndarray  # float32, mono
    sample_rate: int
    # (first_sample, text_index) -- when each source character starts.
    char_starts: list[tuple[int, int]]


def _bit_segments(codes: list[tuple[int, int]], stop_bits: float) -> list[tuple[bool, float, int | None]]:
    """-> [(is_mark, duration_in_bits, text_index_if_char_starts_here)]"""
    segments: list[tuple[bool, float, int | None]] = []
    for code, text_index in codes:
        segments.append((False, 1.0, text_index))  # start bit
        for bit in range(5):
            segments.append((bool((code >> bit) & 1), 1.0, None))
        segments.append((True, stop_bits, None))
    return segments


def synthesize(
    codes: list[tuple[int, int]],
    params: RttyParams,
    sample_rate: int = SAMPLE_RATE,
    lead_in_ms: float = LEAD_IN_MS,
    tail_ms: float = TAIL_MS,
) -> RttyWaveform:
    mark_f, space_f = params.tones()
    samples_per_bit = sample_rate / params.baud
    lead = int(round(lead_in_ms * sample_rate / 1000.0))
    tail = int(round(tail_ms * sample_rate / 1000.0))

    # Sample-accurate (non-drifting) boundaries: derived from the cumulative
    # bit count, not from summing rounded per-bit lengths.
    segments = _bit_segments(codes, params.stop_bits)
    boundaries = [0]
    cumulative_bits = 0.0
    for _, duration, _ in segments:
        cumulative_bits += duration
        boundaries.append(int(round(cumulative_bits * samples_per_bit)))
    body_len = boundaries[-1]
    total = lead + body_len + tail

    freq = np.full(total, mark_f, dtype=np.float64)
    char_starts: list[tuple[int, int]] = []
    for i, (is_mark, _, text_index) in enumerate(segments):
        start, end = lead + boundaries[i], lead + boundaries[i + 1]
        freq[start:end] = mark_f if is_mark else space_f
        if text_index is not None:
            char_starts.append((start, text_index))

    # Phase-continuous: integrate the instantaneous frequency.
    phase = 2.0 * np.pi * np.cumsum(freq) / sample_rate
    wave = np.sin(phase)

    fade = min(int(FADE_MS * sample_rate / 1000.0), total // 2)
    if fade > 0:
        ramp = 0.5 - 0.5 * np.cos(np.pi * np.arange(fade) / fade)
        wave[:fade] *= ramp
        wave[-fade:] *= ramp[::-1]

    return RttyWaveform(samples=wave.astype(np.float32), sample_rate=sample_rate, char_starts=char_starts)
