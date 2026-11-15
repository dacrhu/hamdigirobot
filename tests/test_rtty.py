"""RTTY modulator + decoder: loopback through synthetic audio."""

import numpy as np
import pytest

from hamdigirobot.decoder.rtty_decoder import RttyDecoderCore
from hamdigirobot.modes.baudot import encode_text
from hamdigirobot.tx.rtty_modulator import RttyParams, synthesize

SAMPLE_RATE = 48_000
CHUNK = 240
TEXT = "CQ CQ DE HG7WHD K\nRST 599 5NN 73 $?"


def _audio(text, params, snr_db=None, seed=1):
    wave = synthesize(encode_text(text), params).samples
    audio = np.concatenate([np.zeros(SAMPLE_RATE, np.float32), wave, np.zeros(SAMPLE_RATE, np.float32)])
    if snr_db is not None:
        rng = np.random.default_rng(seed)
        sigma = 10 ** (-snr_db / 20) * np.sqrt(0.5)
        audio = audio + rng.normal(0, sigma, len(audio)).astype(np.float32)
    return audio


def _decode(audio, params, **kwargs):
    core = RttyDecoderCore(SAMPLE_RATE, params, **kwargs)
    out = ""
    for i in range(0, len(audio), CHUNK):
        out += core.process_block(audio[i : i + CHUNK]).decoded_text
    return out


def test_modulator_phase_is_continuous_and_bounded():
    wave = synthesize(encode_text("RYRYRY"), RttyParams()).samples
    assert np.max(np.abs(wave)) <= 1.0 + 1e-6
    # No sample-to-sample jump anywhere (a phase discontinuity at a tone
    # change would show up as a big step at 2.1 kHz / 48 kHz).
    assert np.max(np.abs(np.diff(wave))) < 2 * np.sin(np.pi * 2300 / SAMPLE_RATE) + 0.05


def test_modulator_char_starts_follow_text_index():
    wf = synthesize(encode_text("AB"), RttyParams())
    indices = [i for _, i in wf.char_starts]
    assert indices == sorted(indices)
    assert set(indices) == {0, 1}
    assert all(0 <= s < len(wf.samples) for s, _ in wf.char_starts)


def test_modulator_duration_matches_baud_and_stop_bits():
    p = RttyParams(baud=50.0, stop_bits=2.0)
    wf = synthesize([(1, 0)] * 10, p, lead_in_ms=0, tail_ms=0)
    expected = 10 * (1 + 5 + 2.0) / 50.0
    assert len(wf.samples) / SAMPLE_RATE == pytest.approx(expected, abs=0.002)


@pytest.mark.parametrize(
    "params",
    [
        RttyParams(),
        RttyParams(baud=50.0, stop_bits=2.0),
        RttyParams(baud=75.0, shift_hz=425.0),
        RttyParams(baud=100.0, shift_hz=850.0, mark_hz=1500.0),
        RttyParams(reverse=True),
        RttyParams(stop_bits=1.0),
    ],
)
def test_loopback_clean(params):
    assert _decode(_audio(TEXT, params), params) == TEXT


def test_loopback_noisy():
    params = RttyParams()
    assert _decode(_audio(TEXT, params, snr_db=-3), params) == TEXT


def test_decoder_tolerates_small_mistuning_and_baud_error():
    rx = RttyParams()
    assert _decode(_audio(TEXT, RttyParams(mark_hz=2125 + 25)), rx) == TEXT
    assert _decode(_audio(TEXT, RttyParams(baud=45.45 * 1.015)), rx) == TEXT


def test_reverse_mismatch_does_not_decode_correctly():
    tx = RttyParams()
    rx = RttyParams(reverse=True)
    assert _decode(_audio(TEXT, tx), rx) != TEXT


def test_unshift_on_space_end_to_end():
    params = RttyParams()
    text = "RST 599 599 K"
    wave = synthesize(encode_text(text, unshift_on_space=True), params).samples
    audio = np.concatenate([np.zeros(SAMPLE_RATE, np.float32), wave, np.zeros(SAMPLE_RATE, np.float32)])
    assert _decode(audio, params, unshift_on_space=True) == text


def test_set_params_retunes_decoder():
    core = RttyDecoderCore(SAMPLE_RATE, RttyParams())
    core.target_pitch_hz = 1500
    assert core.params.mark_hz == 1500
    audio = _audio("TEST", RttyParams(mark_hz=1500))
    out = "".join(core.process_block(audio[i : i + CHUNK]).decoded_text for i in range(0, len(audio), CHUNK))
    assert out == "TEST"


def test_chunk_size_independence():
    params = RttyParams()
    audio = _audio(TEXT, params)
    for chunk in (97, 1000, 4800):
        core = RttyDecoderCore(SAMPLE_RATE, params)
        out = "".join(core.process_block(audio[i : i + chunk]).decoded_text for i in range(0, len(audio), chunk))
        assert out == TEXT, chunk
