import pytest

from hamdigirobot.modes.baudot import FIGS_CODE, LTRS_CODE, BaudotDecoder, encode_text


def _decode(codes, uos=False):
    d = BaudotDecoder(unshift_on_space=uos)
    return "".join(d.feed(c) for c, _ in codes)


@pytest.mark.parametrize("uos", [False, True])
def test_round_trip(uos):
    text = "CQ DE HG7WHD 599 TNX 73 $ ? ' , . : ; ( ) ! & # / -\nOK"
    assert _decode(encode_text(text, unshift_on_space=uos), uos) == text


def test_lowercase_is_uppercased_and_crlf_normalised():
    assert _decode(encode_text("cq\r\nde")) == "CQ\nDE"


def test_unsupported_characters_are_dropped():
    assert _decode(encode_text("A@B~C")) == "ABC"


def test_shift_codes_only_when_needed_and_carry_text_index():
    codes = encode_text("A12B")
    assert [c for c, _ in codes].count(FIGS_CODE) == 1
    assert [c for c, _ in codes].count(LTRS_CODE) == 2  # initial + back to letters
    # The FIGS code belongs to the "1" (index 1), the LTRS back to "B" (3)
    assert (FIGS_CODE, 1) in codes
    assert (LTRS_CODE, 3) in codes


def test_unshift_on_space_saves_the_ltrs_after_space():
    with_uos = [c for c, _ in encode_text("5 A", unshift_on_space=True)]
    without = [c for c, _ in encode_text("5 A", unshift_on_space=False)]
    assert len(with_uos) == len(without) - 1


def test_decoder_starts_in_letters_and_ignores_null_and_bell():
    d = BaudotDecoder()
    assert d.feed(0) == ""
    assert d.feed(1) == "E"
    d.feed(FIGS_CODE)
    assert d.feed(5) == ""  # BELL
