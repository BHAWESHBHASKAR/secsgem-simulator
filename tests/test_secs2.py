"""SECS-II codec tests: wire bytes, round trips, length boundaries, errors, SML."""

import pytest

from secsgem_sim.message import SecsMessage
from secsgem_sim.secs2 import (
    A, B, BOOLEAN, F4, F8, I1, I2, I4, I8, L, U1, U2, U4, U8,
    Format, Secs2DecodeError, Secs2EncodeError, SecsItem, decode, encode, to_sml,
)


# --- exact wire bytes for known examples ----------------------------------

def test_ascii_wire_format():
    # 0x41 = format code 0o20 (A) << 2 | 1 length byte
    assert encode(A("MDLN")) == bytes([0x41, 0x04]) + b"MDLN"


def test_list_wire_format_counts_items_not_bytes():
    data = encode(L(A("MDLN"), A("1.0")))
    assert data[:2] == bytes([0x01, 0x02])  # L, 1 length byte, 2 items
    assert data == bytes([0x01, 0x02, 0x41, 0x04]) + b"MDLN" + bytes([0x41, 0x03]) + b"1.0"


@pytest.mark.parametrize("item, expected", [
    (U4(1001), bytes([0xB1, 0x04, 0x00, 0x00, 0x03, 0xE9])),
    (U1(5), bytes([0xA5, 0x01, 0x05])),
    (I2(-2), bytes([0x69, 0x02, 0xFF, 0xFE])),
    (BOOLEAN(True, False), bytes([0x25, 0x02, 0x01, 0x00])),
    (B(0x80), bytes([0x21, 0x01, 0x80])),
    (F4(1.5), bytes([0x91, 0x04, 0x3F, 0xC0, 0x00, 0x00])),
    (L(), bytes([0x01, 0x00])),
    (A(""), bytes([0x41, 0x00])),
])
def test_known_encodings(item, expected):
    assert encode(item) == expected


# --- round trips for every type ------------------------------------------

ROUND_TRIP_ITEMS = [
    A("Hello SECS"),
    B(0x00, 0x7F, 0xFF),
    BOOLEAN(True, False, True),
    U1(0, 255), U2(0, 65535), U4(0, 4294967295), U8(0, 2**64 - 1),
    I1(-128, 127), I2(-32768, 32767), I4(-2**31, 2**31 - 1), I8(-2**63, 2**63 - 1),
    F4(0.5, -2.25, 1024.0), F8(3.141592653589793, -1e300),
    U4(),          # zero-length numeric array is legal
    L(),           # empty list is legal (and means "all" in many GEM messages)
]


@pytest.mark.parametrize("item", ROUND_TRIP_ITEMS, ids=lambda i: i.format.name)
def test_round_trip_every_type(item):
    assert decode(encode(item)) == item


def test_round_trip_nested_lists():
    item = L(
        U4(1),
        L(A("RPT"), L(U4(1001), F4(25.5)), L()),
        L(L(L(BOOLEAN(True)))),
    )
    assert decode(encode(item)) == item


# --- length boundaries: 1, 2 and 3 length bytes ---------------------------

@pytest.mark.parametrize("size, n_length_bytes", [
    (255, 1), (256, 2), (65535, 2), (65536, 3),
])
def test_binary_length_boundaries(size, n_length_bytes):
    item = B(bytes(size))
    data = encode(item)
    assert data[0] & 0b11 == n_length_bytes
    assert len(data) == 1 + n_length_bytes + size
    assert decode(data) == item


@pytest.mark.parametrize("size", [255, 256, 65535, 65536])
def test_ascii_length_boundaries(size):
    item = A("x" * size)
    assert decode(encode(item)) == item


@pytest.mark.parametrize("count, n_length_bytes", [(255, 1), (256, 2)])
def test_list_length_boundaries_use_item_count(count, n_length_bytes):
    item = L(*[U1(i % 256) for i in range(count)])
    data = encode(item)
    assert data[0] == (0 << 2) | n_length_bytes
    assert int.from_bytes(data[1:1 + n_length_bytes], "big") == count
    assert decode(data) == item


def test_numeric_array_length_is_in_bytes():
    data = encode(U4(*range(100)))  # 400 bytes -> needs 2 length bytes
    assert data[0] == (Format.U4.value << 2) | 2
    assert int.from_bytes(data[1:3], "big") == 400


def test_item_too_long_rejected():
    with pytest.raises(Secs2EncodeError):
        encode(B(bytes(0x1000000)))


# --- encode errors -------------------------------------------------------

@pytest.mark.parametrize("item", [U1(256), U1(-1), I1(128), U4(1.5), I2(True)])
def test_integer_out_of_range_or_wrong_type(item):
    with pytest.raises(Secs2EncodeError):
        encode(item)


def test_non_ascii_text_rejected():
    with pytest.raises(Secs2EncodeError):
        encode(A("température"))


# --- decode errors -------------------------------------------------------

@pytest.mark.parametrize("data, reason", [
    (b"", "empty"),
    (bytes([0x40, 0x01]), "zero length bytes"),
    (bytes([0xFD, 0x00]), "unknown format code"),
    (bytes([0x41, 0x05]) + b"abc", "truncated data"),
    (bytes([0x42, 0x00]), "truncated length bytes"),
    (bytes([0x01, 0x02, 0x41, 0x00]), "list missing a child"),
    (bytes([0xB1, 0x03, 0x00, 0x00, 0x01]), "U4 not a multiple of 4"),
    (encode(U1(1)) + b"\x00", "trailing bytes"),
])
def test_decode_errors(data, reason):
    with pytest.raises(Secs2DecodeError):
        decode(data)


# --- SML and helpers -----------------------------------------------------

def test_sml_single_line():
    assert to_sml(L(A("MDLN"), A("1.0"))) == '<L [2] <A "MDLN"> <A "1.0">>'


def test_sml_scalars():
    assert to_sml(U4(1, 2)) == "<U4 1 2>"
    assert to_sml(BOOLEAN(True)) == "<BOOLEAN TRUE>"
    assert to_sml(B(0, 0x80)) == "<B 0x00 0x80>"
    assert to_sml(F4(25.3)) == "<F4 25.3>"   # float32 noise is hidden
    assert to_sml(L()) == "<L [0]>"
    assert to_sml(A('say "hi"')) == '<A "say \\"hi\\"">'


def test_sml_pretty_breaks_long_lists():
    item = L(*[A("a fairly long ascii value %d" % i) for i in range(4)])
    text = to_sml(item, pretty=True)
    assert text.splitlines()[0] == "<L [4]"
    assert text.splitlines()[-1] == ">"


def test_message_sml_and_header():
    msg = SecsMessage(1, 13, True, L(), system_bytes=7, session_id=0)
    assert msg.to_sml() == "S1F13 W\n<L [0]>"
    assert msg.header_bytes() == bytes([0, 0, 0x81, 13, 0, 0, 0, 0, 0, 7])


def test_to_python():
    assert L(U4(5), A("x"), F8(1.0, 2.0), L()).to_python() == [5, "x", [1.0, 2.0], []]
