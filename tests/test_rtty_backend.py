"""RttyAudioTxBackend with the sound device stubbed out: the audio callback
is driven by hand, as in tests/test_playback.py."""

import threading

import numpy as np

from hamdigirobot.tx import rtty_backend
from hamdigirobot.tx.rtty_backend import RttyAudioTxBackend
from hamdigirobot.tx.rtty_modulator import RttyParams


class _FakeRig:
    def __init__(self):
        self.events = []

    def open(self):
        self.events.append("open")

    def close(self):
        self.events.append("close")

    def set_ptt(self, on):
        self.events.append(f"ptt_{'on' if on else 'off'}")


def _make(rig=None):
    backend = RttyAudioTxBackend(RttyParams(), rig=rig)
    backend._rig_open = rig is not None  # skip real start()/OutputStream
    return backend


def _pump(backend, rendered, stop_event):
    out = np.zeros((rtty_backend.BLOCKSIZE, 1), dtype=np.float32)
    while not stop_event.is_set():
        backend._callback(out, rtty_backend.BLOCKSIZE, None, None)
        rendered.append(out[:, 0].copy())
        stop_event.wait(0.001)


def test_plays_whole_message_and_reports_progress(monkeypatch):
    monkeypatch.setattr(rtty_backend, "DRAIN_S", 0.0)
    rig = _FakeRig()
    backend = _make(rig)
    rendered, done, progress = [], threading.Event(), []
    t = threading.Thread(target=_pump, args=(backend, rendered, done))
    t.start()
    backend.send_text("CQ", 0, lambda: False, progress_callback=progress.append)
    done.set()
    t.join()
    audio = np.concatenate(rendered)
    assert np.max(np.abs(audio)) > 0.5
    assert progress and progress[0] == 0 and progress[-1] == 1
    assert rig.events == ["ptt_on", "ptt_off"]


def test_stop_flag_interrupts_and_drops_ptt(monkeypatch):
    monkeypatch.setattr(rtty_backend, "DRAIN_S", 0.0)
    rig = _FakeRig()
    backend = _make(rig)
    rendered, done = [], threading.Event()
    t = threading.Thread(target=_pump, args=(backend, rendered, done))
    t.start()
    calls = {"n": 0}

    def stop():
        calls["n"] += 1
        return calls["n"] > 5

    backend.send_text("CQ CQ CQ CQ CQ CQ CQ CQ", 0, stop)
    done.set()
    t.join()
    played = sum(len(b) for b in rendered)
    assert played < 48_000 * 2  # far shorter than the full message
    assert rig.events[-1] == "ptt_off"


def test_empty_text_sends_nothing():
    backend = _make()
    backend.send_text("@@@", 0, lambda: False)  # nothing encodable
