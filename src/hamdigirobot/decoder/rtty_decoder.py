"""RTTY receive pipeline: audio -> mark/space tone energies -> UART framing
-> Baudot -> text.

Qt-free and device-free (like `CwDecoderCore`), so it is unit-testable with
synthetic audio from tx.rtty_modulator. `RttyDecoderCore` deliberately
exposes the same tuning attributes as `CwDecoderCore` (target_pitch_hz,
squelch_ratio, ...) so the Qt shell `decoder.decoder.CwDecoder` and
ui.main_window can drive either one; attributes that mean nothing for RTTY
(bandwidth, manual WPM) are accepted and ignored.

Detection
---------
The 8 kHz decimated stream is mixed down by the mark and the space
frequency and integrated over a sliding window of exactly one bit time
(a matched filter for an on/off-keyed tone). Every hop (~1/8 bit) the
normalised difference d = (E_mark - E_space) / (E_mark + E_space) is
produced: +1 = clean mark, -1 = clean space.

Framing
-------
With a one-bit window, d crosses zero half a window *after* the start bit
began, so the start edge is t0 = t_cross - W/2. Bit k (0 = start, 1..5 data
LSB-first, 6 = stop) is then fully covered by the window ending at
t0 + (k+1)*W, where d is read by linear interpolation. A frame is accepted
only if the start bit is space, the stop bit is mark and every bit is at
least `FRAME_MARGIN` clear of zero; otherwise scanning resumes just after
the false edge (so noise can't swallow a real character that follows).

Squelch
-------
Level based, like the CW decoder's: the stronger of the two tone energies
(the signal) is compared with the energy at two reference frequencies well
outside the tone pair (the noise floor), both smoothed over ~120 ms. The
gate opens when signal > ratio x noise (the squelch slider is that ratio,
0 = always open), stays open down to 60% of that (hysteresis) and holds
for ~0.4 s, so fades don't chop characters. While closed, no characters are
emitted and the LED/waterfall show "closed".
"""

from __future__ import annotations

import numpy as np

from hamdigirobot.decoder.decoder import (
    SPECTRUM_HOP_SECONDS,
    ProcessedBlock,
)
from hamdigirobot.dsp.spectrum import SpectrumAnalyzer, SpectrumSlice
from hamdigirobot.dsp.tone_detector import PROCESSING_SAMPLE_RATE, StreamingDecimator
from hamdigirobot.modes.baudot import BaudotDecoder
from hamdigirobot.tx.rtty_modulator import RttyParams

HOPS_PER_BIT = 8
FRAME_MARGIN = 0.35  # every sampled bit must be at least this clear of d = 0
SQUELCH_SMOOTHING_S = 0.12  # time constant of the signal/noise energy averages
SQUELCH_CLOSE_FRACTION = 0.6  # hysteresis: stays open down to 60% of the open threshold
SQUELCH_HANG_S = 0.4
CHAR_SNR_FRACTION = 0.5  # sampled bits need instantaneous signal/noise >= this x the squelch ratio
NOISE_REF_MIN_HZ = 200.0
NOISE_REF_MAX_HZ = 3500.0  # inside the decimator's passband (8 kHz / 2 * 0.9)
FRAME_BITS = 7.0  # start + 5 data + (at least) 1 stop
HISTORY_BITS = 40.0  # how much d-history is kept for re-scanning


class RttyDecoderCore:
    def __init__(
        self,
        sample_rate: int,
        params: RttyParams | None = None,
        unshift_on_space: bool = False,
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
        self._baudot = BaudotDecoder(unshift_on_space=unshift_on_space)

        self._spectrum = SpectrumAnalyzer(sample_rate=PROCESSING_SAMPLE_RATE)
        self._spectrum_window = np.zeros(self._spectrum.fft_size, dtype=np.float32)
        self._spectrum_hop_samples = int(PROCESSING_SAMPLE_RATE * SPECTRUM_HOP_SECONDS)
        self._spectrum_accumulated = 0

        self._pending_params: RttyParams | None = None
        self._configure(params or RttyParams())

    # -- configuration ----------------------------------------------------

    @property
    def params(self) -> RttyParams:
        return self._pending_params or self._params

    def set_params(self, params: RttyParams) -> None:
        """Safe to call from another thread than process_block's: the new
        parameters are only applied at the start of the next block, so the
        decoder's streaming state is never reconfigured mid-block."""
        self._pending_params = params

    @property
    def unshift_on_space(self) -> bool:
        return self._baudot.unshift_on_space

    @unshift_on_space.setter
    def unshift_on_space(self, enabled: bool) -> None:
        self._baudot.unshift_on_space = enabled

    @property
    def target_pitch_hz(self) -> float:
        """The mark frequency (what the pitch spinbox means in RTTY mode)."""
        return self.params.mark_hz

    @target_pitch_hz.setter
    def target_pitch_hz(self, hz: float) -> None:
        p = self.params
        self.set_params(
            RttyParams(baud=p.baud, shift_hz=p.shift_hz, mark_hz=float(hz), stop_bits=p.stop_bits, reverse=p.reverse)
        )

    @property
    def effective_bandwidth_hz(self) -> float:
        return self.params.shift_hz + 2.0 * self.params.baud

    @property
    def current_wpm(self) -> float:
        return 0.0

    @property
    def margin(self) -> float:
        """Required clearance of each sampled bit from d = 0 (squelch off:
        none, i.e. every decision is accepted)."""
        return FRAME_MARGIN if self.squelch_ratio > 0.0 else 0.0

    @property
    def squelch_open(self) -> bool:
        return self.squelch_ratio <= 0.0 or self._gate_open

    def reset_decoder(self) -> None:
        self._baudot.reset()
        self._reset_stream_state()

    def _configure(self, params: RttyParams) -> None:
        self._params = params
        self._window = max(8, int(round(PROCESSING_SAMPLE_RATE / params.baud)))
        self._hop = max(1, self._window // HOPS_PER_BIT)
        mark_f, space_f = params.tones()
        # Noise reference tones: well outside the pair (sinc sidelobes of a
        # one-bit window need >= ~3 bins), clamped into the usable band.
        offset = max(1.5 * params.shift_hz, 3.5 * params.baud)
        f_low, f_high = min(mark_f, space_f), max(mark_f, space_f)
        refs = [f for f in (f_low - offset, f_high + offset) if NOISE_REF_MIN_HZ <= f <= NOISE_REF_MAX_HZ]
        if not refs:  # tones too close to a band edge for either side: use the nearer side anyway
            refs = [min(max(f_high + offset, NOISE_REF_MIN_HZ), NOISE_REF_MAX_HZ)]
        self._n_refs = len(refs)
        freqs = np.array([mark_f, space_f, *refs], dtype=np.float64)
        self._w = 2.0 * np.pi * freqs / PROCESSING_SAMPLE_RATE  # one mixer per measured frequency
        hop_s = self._hop / PROCESSING_SAMPLE_RATE
        self._squelch_alpha = float(np.exp(-hop_s / SQUELCH_SMOOTHING_S))
        self._hang_hops = int(round(SQUELCH_HANG_S / hop_s))
        self._reset_stream_state()

    def _reset_stream_state(self) -> None:
        self._total = 0  # decimated samples consumed so far
        self._tail = np.zeros((len(self._w), 0), dtype=np.complex128)
        self._hist_t: list[float] = []  # sample-time of each hop's window end
        self._hist_d: list[float] = []
        self._hist_open: list[bool] = []  # squelch gate at each hop
        self._hist_snr: list[float] = []  # instantaneous signal / smoothed noise energy
        self._sig: float | None = None  # smoothed signal / noise energies
        self._noise: float | None = None
        self._gate_open = False
        self._hang_left = 0
        self._scan_pos = 1
        self._candidate: tuple[float, int] | None = None  # (t_cross, hop index)

    # -- processing -------------------------------------------------------

    def process_block(self, samples: np.ndarray) -> ProcessedBlock:
        if self._pending_params is not None:
            pending, self._pending_params = self._pending_params, None
            if pending != self._params:
                self._configure(pending)
        decimated = self._decimator.process(samples)
        text = self._process_decoding(decimated)
        spectrum_slice = self._process_spectrum(decimated)
        return ProcessedBlock(
            decoded_text=text,
            spectrum_slice=spectrum_slice,
            wpm=None,
            squelch_open=self.squelch_open,
        )

    def _process_decoding(self, decimated: np.ndarray) -> str:
        n = len(decimated)
        if n == 0:
            return ""
        window, hop = self._window, self._hop
        idx = np.arange(self._total, self._total + n, dtype=np.float64)
        x = decimated.astype(np.float64)
        # One complex mixer output per measured frequency (mark, space,
        # noise references), integrated over a sliding one-bit window.
        mixed = np.concatenate([self._tail, x[None, :] * np.exp(-1j * self._w[:, None] * idx[None, :])], axis=1)
        start = self._total - self._tail.shape[1]  # global index of mixed[:, 0]
        cum = np.concatenate([np.zeros((len(self._w), 1), dtype=np.complex128), np.cumsum(mixed, axis=1)], axis=1)

        new_total = self._total + n
        first_end = (self._total // hop + 1) * hop
        ends = np.arange(first_end, new_total + 1, hop)
        ends = ends[ends - window >= start]
        if len(ends):
            hi = ends - start
            energy = np.abs(cum[:, hi] - cum[:, hi - window]) ** 2  # (freqs, hops)
            e_m, e_s = energy[0], energy[1]
            d = (e_m - e_s) / (e_m + e_s + 1e-12)
            signal = np.maximum(e_m, e_s)
            noise = energy[2:].mean(axis=0)
            for end, d_k, sig_k, noise_k in zip(ends, d, signal, noise):
                self._update_gate(float(sig_k), float(noise_k))
                self._hist_t.append(float(end))
                self._hist_d.append(float(d_k))
                self._hist_open.append(self._gate_open)
                self._hist_snr.append(float(sig_k) / (self._noise + 1e-12))

        self._tail = mixed[:, -window:]
        self._total = new_total
        text = self._scan()
        self._trim_history()
        return text

    def _update_gate(self, sig: float, noise: float) -> None:
        """Level squelch on the smoothed signal/noise energies (see module
        docstring). Ratio 0 leaves the gate open."""
        a = self._squelch_alpha
        self._sig = sig if self._sig is None else a * self._sig + (1.0 - a) * sig
        self._noise = noise if self._noise is None else a * self._noise + (1.0 - a) * noise
        ratio = self.squelch_ratio
        if ratio <= 0.0:
            self._gate_open = True
            return
        threshold = ratio * self._noise + 1e-12
        if self._sig > threshold:
            self._gate_open = True
            self._hang_left = self._hang_hops
        elif self._gate_open:
            if self._sig > SQUELCH_CLOSE_FRACTION * threshold:
                self._hang_left = self._hang_hops
            elif self._hang_left > 0:
                self._hang_left -= 1
            else:
                self._gate_open = False

    def _scan(self) -> str:
        out = ""
        d, t = self._hist_d, self._hist_t
        window = self._window
        while True:
            if self._candidate is None:
                if self._scan_pos >= len(d):
                    break
                i = self._scan_pos
                self._scan_pos += 1
                if d[i - 1] >= 0.0 > d[i]:
                    frac = d[i - 1] / (d[i - 1] - d[i])
                    t_cross = t[i - 1] + frac * (t[i] - t[i - 1])
                    self._candidate = (t_cross, i)
                continue
            t_cross, i0 = self._candidate
            t0 = t_cross - window / 2.0
            if t[-1] < t0 + FRAME_BITS * window:
                break  # frame not complete yet; wait for more audio
            code = self._read_frame(t0)
            self._candidate = None
            if code is not None and not (
                self._gate_open_at(t0 + window) and self._gate_open_at(t0 + FRAME_BITS * window)
            ):
                # Squelched at the frame's start or end: noise next to a
                # transmission's edges that happened to frame cleanly.
                code = None
            if code is None:
                self._scan_pos = i0 + 1  # false edge: rescan right after it
            else:
                out += self._baudot.feed(code)
                resume_t = t0 + FRAME_BITS * window
                self._scan_pos = max(i0 + 1, int(np.searchsorted(t, resume_t)))
        return out

    def _gate_open_at(self, t: float) -> bool:
        if self.squelch_ratio <= 0.0:
            return True
        k = min(int(np.searchsorted(self._hist_t, t)), len(self._hist_open) - 1)
        return self._hist_open[k]

    def _read_frame(self, t0: float) -> int | None:
        window = self._window
        times = t0 + (np.arange(7) + 1) * window
        values = np.interp(times, self._hist_t, self._hist_d)
        margin = self.margin
        if values[0] > -margin or values[0] >= 0.0:  # start bit must be space
            return None
        if values[6] < margin or values[6] <= 0.0:  # stop bit must be mark
            return None
        if self.squelch_ratio > 0.0:
            # Character-level squelch: at every sampled bit the tone must
            # stand out of the noise right now (not just within the smoothed
            # gate, which holds open for a while after a transmission ends --
            # noise decoding cleanly by chance in that window).
            snr = np.interp(times, self._hist_t, self._hist_snr)
            if np.min(snr) < CHAR_SNR_FRACTION * self.squelch_ratio:
                return None
        data = values[1:6]
        if margin > 0.0 and np.any(np.abs(data) < margin):
            return None
        code = 0
        for bit, v in enumerate(data):
            if v > 0.0:
                code |= 1 << bit
        return code

    def _trim_history(self) -> None:
        keep = int(HISTORY_BITS * self._window / self._hop)
        excess = len(self._hist_d) - keep
        floor = self._scan_pos - 1
        if self._candidate is not None:
            floor = min(floor, self._candidate[1] - 1)
        excess = min(excess, floor)
        if excess <= 0:
            return
        del self._hist_t[:excess]
        del self._hist_d[:excess]
        del self._hist_open[:excess]
        del self._hist_snr[:excess]
        self._scan_pos -= excess
        if self._candidate is not None:
            self._candidate = (self._candidate[0], self._candidate[1] - excess)

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
        p = self._params
        return self._spectrum.analyze(
            self._spectrum_window,
            center_hz=p.mark_hz + p.shift_hz / 2.0,
            half_span_hz=self.waterfall_half_span_hz,
        )
