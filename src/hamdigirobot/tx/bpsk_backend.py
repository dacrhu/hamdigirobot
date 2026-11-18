"""BPSK63 TX backend: same machinery as the RTTY one (the whole message is
rendered to audio up front and played through a sound-card OutputStream,
optionally keying PTT over Hamlib CAT) -- only the waveform differs."""

from __future__ import annotations

from hamdigirobot.tx.bpsk_modulator import BpskParams, synthesize
from hamdigirobot.tx.rtty_backend import RttyAudioTxBackend


class BpskAudioTxBackend(RttyAudioTxBackend):
    def __init__(self, params: BpskParams, **kwargs) -> None:
        super().__init__(params=params, **kwargs)  # type: ignore[arg-type]

    def _render(self, text: str):
        return synthesize(text, self.params, sample_rate=self.samplerate)  # type: ignore[arg-type]
