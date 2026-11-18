"""RTTY (AFSK) TX backend: renders the whole message to audio up front
(tx.rtty_modulator) and plays it through a sound-card OutputStream,
optionally keying the rig's PTT over Hamlib CAT around it.

Unlike CW (AudioTxBackend: one long keyed tone paced by a software clock),
RTTY's timing lives in the audio samples themselves, so the sound card's
own clock paces the bits -- Python only watches the playback position (for
the progress highlight) and the stop flag. `send_text`'s `wpm`/`jitter`
arguments are CW concepts and ignored here.
"""

from __future__ import annotations

import bisect
import logging
import time
from collections.abc import Callable

import numpy as np
import sounddevice as sd

from hamdigirobot.audio.pipewire_route import (
    PipeWireSink,
    route_active_stream_to_sink,
    snapshot_sink_input_ids,
)
from hamdigirobot.hamlib.rig_client import HamlibRig
from hamdigirobot.modes.baudot import encode_text
from hamdigirobot.tx.backend import TxBackend
from hamdigirobot.tx.rtty_modulator import SAMPLE_RATE, RttyParams, synthesize

logger = logging.getLogger(__name__)

BLOCKSIZE = 480  # 10 ms at 48 kHz
PTT_LEAD_S = 0.08  # PTT keyed this long before the first audio sample
DRAIN_S = 0.15  # let the device's output buffer play out before PTT drops
STOP_FADE_S = 0.01  # Stop ramps the tone down instead of cutting it
_POLL_S = 0.01


class RttyAudioTxBackend(TxBackend):
    def __init__(
        self,
        params: RttyParams,
        unshift_on_space: bool = False,
        device: str | int | None = None,
        route_to_pipewire_sink: PipeWireSink | None = None,
        rig: HamlibRig | None = None,
        samplerate: int = SAMPLE_RATE,
    ) -> None:
        self.params = params
        self.unshift_on_space = unshift_on_space
        self.device = device
        self.samplerate = samplerate
        self._route_to_pipewire_sink = route_to_pipewire_sink
        self._rig = rig
        self._rig_open = False
        self._ptt_on = False

        self._stream: sd.OutputStream | None = None
        self._buffer: np.ndarray | None = None
        self._pos = 0
        self._done = True
        self._abort = False

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Open the rig (if PTT over CAT is used) and the audio stream.
        Raises on failure so the caller can show a status message."""
        if self._rig is not None:
            self._rig.open(set_cw_mode=False)  # keep the mode the rig is in (RTTY: USB/DATA)
            self._rig_open = True
        try:
            self._open_stream()
        except Exception:
            self.close()
            raise

    def _open_stream(self) -> None:
        route = self._route_to_pipewire_sink
        before_ids = snapshot_sink_input_ids() if route is not None else None
        self._stream = sd.OutputStream(
            device=None if route is not None else self.device,
            samplerate=self.samplerate,
            blocksize=BLOCKSIZE,
            channels=1,
            dtype="float32",
            callback=self._callback,
        )
        self._stream.start()
        if route is not None and before_ids is not None:
            route_active_stream_to_sink(route, before_ids)

    def close(self) -> None:
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                logger.exception("closing RTTY audio stream failed")
            self._stream = None
        self._set_ptt(False)
        if self._rig is not None and self._rig_open:
            try:
                self._rig.close()
            except Exception:
                logger.exception("closing rig after RTTY TX failed")
            self._rig_open = False

    # -- audio callback ---------------------------------------------------

    def _callback(self, outdata: np.ndarray, frames: int, time_info, status) -> None:
        outdata[:, 0] = 0.0
        buf = self._buffer
        if buf is None or self._done:
            return
        if self._abort:
            fade = min(frames, int(STOP_FADE_S * self.samplerate))
            chunk = buf[self._pos : self._pos + fade]
            ramp = np.linspace(1.0, 0.0, len(chunk), dtype=np.float32)
            outdata[: len(chunk), 0] = chunk * ramp
            self._done = True
            return
        chunk = buf[self._pos : self._pos + frames]
        outdata[: len(chunk), 0] = chunk
        self._pos += len(chunk)
        if self._pos >= len(buf):
            self._done = True

    # -- sending ----------------------------------------------------------

    def _set_key(self, is_on: bool) -> None:  # TxBackend's abstract hook; unused
        pass

    def _set_ptt(self, on: bool) -> None:
        if self._rig is None or not self._rig_open or self._ptt_on == on:
            return
        self._rig.set_ptt(on)
        self._ptt_on = on

    def _render(self, text: str):
        """The whole message as a waveform (samples + char_starts), or None
        when there is nothing sendable. The one mode-specific step: BPSK63's
        backend (tx.bpsk_backend) overrides just this."""
        codes = encode_text(text, unshift_on_space=self.unshift_on_space)
        if not codes:
            return None
        return synthesize(codes, self.params, sample_rate=self.samplerate)

    def send_text(
        self,
        text: str,
        wpm: float,
        stop_flag: Callable[[], bool],
        progress_callback: Callable[[int], None] | None = None,
        jitter: float = 0.0,
    ) -> None:
        waveform = self._render(text)
        if waveform is None:
            return
        starts = [s for s, _ in waveform.char_starts]
        indices = [i for _, i in waveform.char_starts]

        self._abort = False
        self._pos = 0
        self._buffer = waveform.samples
        try:
            self._set_ptt(True)
            if self._rig is not None:
                time.sleep(PTT_LEAD_S)
            self._done = False
            last_index: int | None = None
            while not self._done:
                if stop_flag():
                    self._abort = True
                    break
                if progress_callback is not None:
                    k = bisect.bisect_right(starts, self._pos) - 1
                    if k >= 0 and indices[k] != last_index:
                        last_index = indices[k]
                        progress_callback(last_index)
                time.sleep(_POLL_S)
            # Let the last block (or the stop fade) actually reach the device.
            time.sleep(DRAIN_S)
        finally:
            self._done = True
            self._buffer = None
            self._set_ptt(False)
