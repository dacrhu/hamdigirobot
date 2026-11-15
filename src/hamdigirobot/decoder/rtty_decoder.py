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
least `margin` clear of zero; otherwise scanning resumes just after the
false edge (so noise can't swallow a real character that follows).
The "squelch" slider sets that margin (0 = accept anything).
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
SQUELCH_MARGIN_PER_STEP = 0.03  # squelch slider step -> required |d| (14 -> 0.42)
MAX_MARGIN = 0.9
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

        self._last_abs_d = 0.0
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
        return min(MAX_MARGIN, max(0.0, self.squelch_ratio) * SQUELCH_MARGIN_PER_STEP)

    @property
    def squelch_open(self) -> bool:
        return self.margin <= 0.0 or self._last_abs_d >= self.margin

    def reset_decoder(self) -> None:
        self._baudot.reset()
        self._reset_stream_state()

    def _configure(self, params: RttyParams) -> None:
        self._params = params
        self._window = max(8, int(round(PROCESSING_SAMPLE_RATE / params.baud)))
        self._hop = max(1, self._window // HOPS_PER_BIT)
        mark_f, space_f = params.tones()
        self._w_mark = 2.0 * np.pi * mark_f / PROCESSING_SAMPLE_RATE
        self._w_space = 2.0 * np.pi * space_f / PROCESSING_SAMPLE_RATE
        self._reset_stream_state()

    def _reset_stream_state(self) -> None:
        self._total = 0  # decimated samples consumed so far
        self._tail_mark = np.zeros(0, dtype=np.complex128)
        self._tail_space = np.zeros(0, dtype=np.complex128)
        self._hist_t: list[float] = []  # sample-time of each hop's window end
        self._hist_d: list[float] = []
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
        mixed_m = np.concatenate([self._tail_mark, x * np.exp(-1j * self._w_mark * idx)])
        mixed_s = np.concatenate([self._tail_space, x * np.exp(-1j * self._w_space * idx)])
        start = self._total - len(self._tail_mark)  # global index of mixed_*[0]
        cum_m = np.concatenate([[0.0], np.cumsum(mixed_m)])
        cum_s = np.concatenate([[0.0], np.cumsum(mixed_s)])

        new_total = self._total + n
        first_end = (self._total // hop + 1) * hop
        ends = np.arange(first_end, new_total + 1, hop)
        ends = ends[ends - window >= start]
        if len(ends):
            hi = ends - start
            lo = hi - window
            e_m = np.abs(cum_m[hi] - cum_m[lo]) ** 2
            e_s = np.abs(cum_s[hi] - cum_s[lo]) ** 2
            d = (e_m - e_s) / (e_m + e_s + 1e-12)
            self._hist_t.extend(float(e) for e in ends)
            self._hist_d.extend(float(v) for v in d)
            self._last_abs_d = float(np.max(np.abs(d)))

        self._tail_mark = mixed_m[-window:]
        self._tail_space = mixed_s[-window:]
        self._total = new_total
        text = self._scan()
        self._trim_history()
        return text

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
            if code is None:
                self._scan_pos = i0 + 1  # false edge: rescan right after it
            else:
                out += self._baudot.feed(code)
                resume_t = t0 + FRAME_BITS * window
                self._scan_pos = max(i0 + 1, int(np.searchsorted(t, resume_t)))
        return out

    def _read_frame(self, t0: float) -> int | None:
        window = self._window
        times = t0 + (np.arange(7) + 1) * window
        values = np.interp(times, self._hist_t, self._hist_d)
        margin = self.margin
        if values[0] > -margin or values[0] >= 0.0:  # start bit must be space
            return None
        if values[6] < margin or values[6] <= 0.0:  # stop bit must be mark
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
