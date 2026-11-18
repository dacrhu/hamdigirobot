"""BPSK63 receive pipeline: audio -> carrier phase per symbol -> differential
bits -> Varicode -> text.

Qt-free and device-free (like `RttyDecoderCore`, whose tuning attributes it
mirrors so decoder.decoder.CwDecoder and ui.main_window can drive it);
unit-testable with synthetic audio from tx.bpsk_modulator.

Detection
---------
The 8 kHz decimated stream is mixed down by the carrier frequency and
integrated over a sliding window of exactly one symbol (128 samples at
62.5 baud). Every hop (1/16 symbol) that gives one complex value y.

Symbol timing
-------------
Of the 16 hop phases only one lines the window up with the symbols; there
|y|^2 is largest on average (a misaligned window straddles two symbols and
partly cancels at a phase reversal). A running average of |y|^2 is kept per
phase and the best phase is followed (with hysteresis, so noise does not
make it hop around).

Bits
----
Differential: z = y_k * conj(y_{k-1}); Re(z) < 0 is a phase reversal = bit 0,
otherwise bit 1 -- no carrier phase recovery needed. A slow frequency
tracker (angle(z^2)/2 is the per-symbol phase error left by a carrier
offset) keeps the detector centred for a few Hz of mistuning.

Squelch
-------
Level based, like RTTY's: the carrier energy at the best timing phase vs. the
energy at two reference frequencies 250 Hz either side (the noise floor).
The gate opens at signal > ratio x noise (the squelch slider; 0 = always
open), stays open down to 60% of that and holds for ~0.25 s. While it is
closed no bits are decoded.
"""

from __future__ import annotations

import numpy as np

from hamdigirobot.decoder.decoder import SPECTRUM_HOP_SECONDS, ProcessedBlock
from hamdigirobot.dsp.spectrum import SpectrumAnalyzer, SpectrumSlice
from hamdigirobot.dsp.tone_detector import PROCESSING_SAMPLE_RATE, StreamingDecimator
from hamdigirobot.modes.varicode import VaricodeDecoder
from hamdigirobot.tx.bpsk_modulator import BAUD

HOPS_PER_SYMBOL = 16
TIMING_SMOOTHING = 0.9  # per-symbol averaging of |y|^2 at each timing phase
TIMING_HYSTERESIS = 1.2  # another phase must be this much stronger to take over
NOISE_SMOOTHING_S = 0.12
NOISE_REF_OFFSET_HZ = 250.0
NOISE_REF_MIN_HZ = 200.0
NOISE_REF_MAX_HZ = 3500.0  # inside the decimator's passband
SQUELCH_CLOSE_FRACTION = 0.6
SQUELCH_HANG_SYMBOLS = 16
SYMBOL_SNR_FRACTION = 0.5  # each decoded symbol needs signal/noise >= this x the squelch ratio
WARMUP_SYMBOLS = 8  # after the gate opens, let the timing lock before decoding
AFC_GAIN = 0.05
AFC_LIMIT_HZ = 8.0


class BpskDecoderCore:
    def __init__(
        self,
        sample_rate: int,
        carrier_hz: float = 1000.0,
        waterfall_half_span_hz: int = 1000,
        squelch_ratio: float = 14.0,
    ) -> None:
        self.sample_rate = sample_rate
        self.waterfall_half_span_hz = waterfall_half_span_hz
        self.squelch_ratio = squelch_ratio
        # CwDecoderCore-compatible no-op knobs.
        self.detection_bin_span = 1.0
        self.auto_bandwidth = False
        self.manual_wpm: float | None = None

        self._decimator = StreamingDecimator(input_rate=sample_rate)
        self._varicode = VaricodeDecoder()
        self._window = int(round(PROCESSING_SAMPLE_RATE / BAUD))
        self._hop = self._window // HOPS_PER_SYMBOL
        hop_s = self._hop / PROCESSING_SAMPLE_RATE
        self._noise_alpha = float(np.exp(-hop_s / NOISE_SMOOTHING_S))
        self._afc_limit = 2.0 * np.pi * AFC_LIMIT_HZ / BAUD

        self._spectrum = SpectrumAnalyzer(sample_rate=PROCESSING_SAMPLE_RATE)
        self._spectrum_window = np.zeros(self._spectrum.fft_size, dtype=np.float32)
        self._spectrum_hop_samples = int(PROCESSING_SAMPLE_RATE * SPECTRUM_HOP_SECONDS)
        self._spectrum_accumulated = 0

        self._pending_carrier: float | None = None
        self._carrier_hz = float(carrier_hz)
        self._configure()

    # -- configuration ----------------------------------------------------

    @property
    def target_pitch_hz(self) -> float:
        """The carrier frequency (what the pitch spinbox means in BPSK63 mode)."""
        return self._pending_carrier if self._pending_carrier is not None else self._carrier_hz

    @target_pitch_hz.setter
    def target_pitch_hz(self, hz: float) -> None:
        """Safe to call from another thread than process_block's: applied at
        the start of the next block."""
        self._pending_carrier = float(hz)

    @property
    def effective_bandwidth_hz(self) -> float:
        return 2.0 * BAUD

    @property
    def current_wpm(self) -> float:
        return 0.0

    @property
    def squelch_open(self) -> bool:
        return self.squelch_ratio <= 0.0 or self._gate_open

    def reset_decoder(self) -> None:
        self._varicode.reset()
        self._reset_stream_state()

    def _configure(self) -> None:
        f = self._carrier_hz
        lo, hi = f - NOISE_REF_OFFSET_HZ, f + NOISE_REF_OFFSET_HZ
        refs = [r for r in (lo, hi) if NOISE_REF_MIN_HZ <= r <= NOISE_REF_MAX_HZ]
        if not refs:
            refs = [min(max(hi, NOISE_REF_MIN_HZ), NOISE_REF_MAX_HZ)]
        self._w = 2.0 * np.pi * np.array([f, *refs]) / PROCESSING_SAMPLE_RATE
        self._reset_stream_state()

    def _reset_stream_state(self) -> None:
        self._total = 0  # decimated samples consumed so far
        self._tail = np.zeros((len(self._w), 0), dtype=np.complex128)
        self._ring = np.zeros(2 * HOPS_PER_SYMBOL, dtype=np.complex128)  # y per hop, indexed by hop number
        self._energy = np.zeros(HOPS_PER_SYMBOL)  # running |y|^2 per timing phase
        self._energy_init = np.zeros(HOPS_PER_SYMBOL, dtype=bool)
        self._best = 0
        self._noise: float | None = None
        self._gate_open = False
        self._hang_left = 0
        self._open_symbols = 0  # symbols since the gate opened
        self._afc = 0.0  # rad/symbol of carrier offset
        self._varicode.reset()

    # -- processing -------------------------------------------------------

    def process_block(self, samples: np.ndarray) -> ProcessedBlock:
        if self._pending_carrier is not None:
            pending, self._pending_carrier = self._pending_carrier, None
            if pending != self._carrier_hz:
                self._carrier_hz = pending
                self._configure()
        decimated = self._decimator.process(samples)
        text = self._process_decoding(decimated)
        return ProcessedBlock(
            decoded_text=text,
            spectrum_slice=self._process_spectrum(decimated),
            wpm=None,
            squelch_open=self.squelch_open,
        )

    def _process_decoding(self, decimated: np.ndarray) -> str:
        n = len(decimated)
        if n == 0:
            return ""
        window, hop = self._window, self._hop
        idx = np.arange(self._total, self._total + n, dtype=np.float64)
        phase = np.mod(self._w[:, None] * idx[None, :], 2.0 * np.pi)
        mixed = np.concatenate([self._tail, decimated.astype(np.float64)[None, :] * np.exp(-1j * phase)], axis=1)
        start = self._total - self._tail.shape[1]  # global index of mixed[:, 0]
        cum = np.concatenate([np.zeros((len(self._w), 1), dtype=np.complex128), np.cumsum(mixed, axis=1)], axis=1)

        new_total = self._total + n
        first_end = (self._total // hop + 1) * hop
        ends = np.arange(first_end, new_total + 1, hop)
        ends = ends[ends - window >= start]
        text = ""
        if len(ends):
            hi = ends - start
            y_all = cum[:, hi] - cum[:, hi - window]  # (freqs, hops)
            carrier = y_all[0]
            noise = (np.abs(y_all[1:]) ** 2).mean(axis=0)
            for end, y, noise_k in zip(ends, carrier, noise):
                text += self._on_hop(int(end) // hop, complex(y), float(noise_k))
        self._tail = mixed[:, -window:]
        self._total = new_total
        return text

    def _on_hop(self, hop_index: int, y: complex, noise: float) -> str:
        a = self._noise_alpha
        self._noise = noise if self._noise is None else a * self._noise + (1.0 - a) * noise
        self._ring[hop_index % len(self._ring)] = y

        p = hop_index % HOPS_PER_SYMBOL
        e = y.real * y.real + y.imag * y.imag
        if self._energy_init[p]:
            self._energy[p] = TIMING_SMOOTHING * self._energy[p] + (1.0 - TIMING_SMOOTHING) * e
        else:
            self._energy[p], self._energy_init[p] = e, True
        if p != self._best:
            if self._energy[p] > TIMING_HYSTERESIS * self._energy[self._best]:
                self._best = p
            else:
                return ""
        if not self._energy_init.all():
            return ""  # first symbol: no previous sample yet

        prev = self._ring[(hop_index - HOPS_PER_SYMBOL) % len(self._ring)]
        z = y * np.conj(prev) * np.exp(-1j * self._afc)
        self._update_gate(float(self._energy[self._best]))
        if not self._gate_open and self.squelch_ratio > 0.0:
            self._open_symbols = 0
        else:
            self._open_symbols += 1
        if self.squelch_ratio > 0.0 and (
            self._open_symbols <= WARMUP_SYMBOLS
            # The gate holds open for a while after a transmission ends; noise
            # symbols in that tail must not decode by chance.
            or e < SYMBOL_SNR_FRACTION * self.squelch_ratio * (self._noise or 0.0)
        ):
            self._varicode.reset()
            return ""
        out = self._varicode.feed(1 if z.real > 0.0 else 0)
        # Per-symbol phase error left by a carrier offset (BPSK: mod pi).
        err = 0.5 * float(np.angle(z * z))
        self._afc = float(np.clip(self._afc + AFC_GAIN * err, -self._afc_limit, self._afc_limit))
        return out

    def _update_gate(self, signal: float) -> None:
        ratio = self.squelch_ratio
        if ratio <= 0.0:
            self._gate_open = True
            return
        threshold = ratio * (self._noise or 0.0) + 1e-12
        if signal > threshold:
            self._gate_open = True
            self._hang_left = SQUELCH_HANG_SYMBOLS
        elif self._gate_open:
            if signal > SQUELCH_CLOSE_FRACTION * threshold:
                self._hang_left = SQUELCH_HANG_SYMBOLS
            elif self._hang_left > 0:
                self._hang_left -= 1
            else:
                self._gate_open = False

    # -- spectrum (waterfall) ---------------------------------------------

    def _process_spectrum(self, decimated: np.ndarray) -> SpectrumSlice | None:
        n = len(decimated)
        if n == 0:
            return None
        window_len = len(self._spectrum_window)
        if n >= window_len:
            self._spectrum_window[:] = decimated[-window_len:]
        else:
            self._spectrum_window[: window_len - n] = self._spectrum_window[n:]
            self._spectrum_window[window_len - n :] = decimated
        self._spectrum_accumulated += n
        if self._spectrum_accumulated < self._spectrum_hop_samples:
            return None
        self._spectrum_accumulated = 0
        return self._spectrum.analyze(
            self._spectrum_window,
            center_hz=self._carrier_hz,
            half_span_hz=self.waterfall_half_span_hz,
        )
