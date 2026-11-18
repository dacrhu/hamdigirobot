"""BPSK63: Varicode, modulator and decoder loopback through synthetic audio."""

import numpy as np

from hamdigirobot.decoder.bpsk_decoder import BpskDecoderCore
from hamdigirobot.modes import varicode
from hamdigirobot.tx.bpsk_modulator import BpskParams, synthesize

SAMPLE_RATE = 48_000
CHUNK = 240
TEXT = "CQ CQ DE HG7WHD K\nRST 599 73 de hg7whd?"


def _audio(text, params, snr_db=None, seed=1, lead_s=1.0, tail_s=1.0):
    wave = synthesize(text, params).samples
    audio = np.concatenate([np.zeros(int(SAMPLE_RATE * lead_s), np.float32), wave, np.zeros(int(SAMPLE_RATE * tail_s), np.float32)])
    if snr_db is not None:
        rng = np.random.default_rng(seed)
        sigma = 10 ** (-snr_db / 20) * np.sqrt(0.5)
        audio = audio + rng.normal(0, sigma, len(audio)).astype(np.float32)
    return audio


def _decode(audio, carrier=1000.0, **kwargs):
    core = BpskDecoderCore(SAMPLE_RATE, carrier_hz=carrier, **kwargs)
    out = ""
    for i in range(0, len(audio), CHUNK):
        out += core.process_block(audio[i : i + CHUNK]).decoded_text
    return out


def test_varicode_table_is_well_formed():
    codes = varicode._CODES
    assert len(codes) == 123
    assert len(set(codes)) == len(codes)
    for c in codes:
        assert c[0] == "1" and c[-1] == "1" and "00" not in c and len(c) <= varicode.MAX_CODE_BITS


def test_varicode_known_codes():
    enc = dict(zip("e ta", ["11", "1", "101", "1011"]))
    for ch, bits in enc.items():
        assert varicode.encode_text(ch)[0][0] == bits + "00"


def test_varicode_roundtrip_all_printable():
    text = "".join(chr(c) for c in range(32, 123)) + "\nok"
    bits = "".join(b for b, _ in varicode.encode_text(text))
    dec = varicode.VaricodeDecoder()
    out = "".join(dec.feed(int(b)) for b in "0000" + bits)
    assert out == text


def test_decoder_ignores_idle_and_garbage():
    dec = varicode.VaricodeDecoder()
    assert "".join(dec.feed(0) for _ in range(50)) == ""
    assert "".join(dec.feed(1) for _ in range(30)) == ""


def test_modulator_is_bounded_and_char_starts_follow_text_index():
    wf = synthesize("AB", BpskParams())
    assert np.max(np.abs(wf.samples)) <= 1.0
    assert [i for _, i in wf.char_starts] == [0, 1]
    assert synthesize("é", BpskParams()) is None


def test_modulator_duration():
    wf = synthesize("e", BpskParams())  # 32 lead + (11 + 00) + 8 tail symbols
    assert len(wf.samples) == round((32 + 4 + 8) * SAMPLE_RATE / 62.5)


def test_loopback_clean():
    assert _decode(_audio(TEXT, BpskParams(freq_hz=1000.0))).strip() == TEXT


def test_loopback_other_carrier():
    assert _decode(_audio(TEXT, BpskParams(freq_hz=1800.0)), carrier=1800.0).strip() == TEXT


def test_loopback_with_noise():
    out = _decode(_audio(TEXT, BpskParams(), snr_db=6.0))  # SNR over the full 24 kHz band
    assert out.strip() == TEXT


def test_loopback_with_small_frequency_offset():
    out = _decode(_audio(TEXT, BpskParams(freq_hz=1004.0)), carrier=1000.0)
    assert out.strip() == TEXT


def test_decoder_follows_carrier_change():
    core = BpskDecoderCore(SAMPLE_RATE, carrier_hz=600.0)
    core.target_pitch_hz = 1200.0
    audio = _audio("CQ", BpskParams(freq_hz=1200.0))
    out = "".join(core.process_block(audio[i : i + CHUNK]).decoded_text for i in range(0, len(audio), CHUNK))
    assert out.strip() == "CQ"
    assert core.target_pitch_hz == 1200.0


def test_noise_only_stays_squelched():
    rng = np.random.default_rng(3)
    noise = rng.normal(0, 0.05, SAMPLE_RATE * 6).astype(np.float32)
    assert len(_decode(noise)) <= 3


def test_squelch_closes_after_signal():
    core = BpskDecoderCore(SAMPLE_RATE, carrier_hz=1000.0)
    audio = _audio("CQ", BpskParams(), snr_db=30.0, tail_s=2.0)
    states = [core.process_block(audio[i : i + CHUNK]).squelch_open for i in range(0, len(audio), CHUNK)]
    assert any(states) and states[-1] is False


def test_backend_renders_bpsk_waveform():
    from hamdigirobot.tx.bpsk_backend import BpskAudioTxBackend

    backend = BpskAudioTxBackend(params=BpskParams(freq_hz=1500.0))
    wf = backend._render("CQ")
    assert wf is not None and [i for _, i in wf.char_starts] == [0, 1]
    assert backend._render("é") is None


def test_adif_record_carries_psk_submode():
    from hamdigirobot.models.adif import format_qso_record
    from hamdigirobot.models.qso import QsoRecord
    from hamdigirobot.modes import BPSK63, mode_by_key

    assert mode_by_key("bpsk63") is BPSK63
    out = format_qso_record(QsoRecord(callsign="HG1ABC", mode=BPSK63.adif_mode, submode=BPSK63.adif_submode))
    assert "<MODE:3>PSK" in out and "<SUBMODE:6>BPSK63" in out


def test_rig_goes_to_data_usb_in_bpsk63():
    from hamdigirobot.hamlib.sideband import rig_modes_for

    assert rig_modes_for("bpsk63") == rig_modes_for("rtty")


def test_output_level_scales_audio_backends():
    import numpy as np

    from hamdigirobot.audio.playback import SidetonePlayback
    from hamdigirobot.tx.audio_backend import AudioTxBackend
    from hamdigirobot.tx.bpsk_backend import BpskAudioTxBackend

    def peak(pb):
        pb.key(True)
        out = np.zeros((4800, 1), np.float32)
        for _ in range(3):
            pb._callback(out, 4800, None, None)
        return float(np.max(np.abs(out)))

    full, half = SidetonePlayback(600.0), SidetonePlayback(600.0)
    half.volume = 0.5
    assert abs(peak(half) / peak(full) - 0.5) < 0.01
    backend = AudioTxBackend(frequency_hz=600.0)
    backend.volume = 0.25
    assert backend._playback.volume == 0.25 and backend.volume == 0.25

    b = BpskAudioTxBackend(params=BpskParams())
    b._buffer = np.ones(1000, np.float32)
    b._done, b.volume = False, 0.3
    out = np.zeros((480, 1), np.float32)
    b._callback(out, 480, None, None)
    assert np.allclose(out, 0.3)


def test_capture_gain_applied():
    import numpy as np

    from hamdigirobot.audio.capture import AudioCapture

    cap = AudioCapture()
    cap.gain = 2.0
    cap._callback(np.full((100, 1), 0.1, np.float32), 100, None, None)
    assert np.allclose(cap.ring_buffer.read_available(), 0.2)
