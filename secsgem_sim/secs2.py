"""SECS-II data items (SEMI E5): binary encoding, decoding and SML text.

Every SECS-II message body is ONE data item. An item is either a list (L) of
other items, or an array of values of a single type (ASCII text, bytes,
booleans, signed/unsigned integers, floats).

Wire format of one item:

    +-------------+------------------+----------------------+
    | format byte | 1-3 length bytes | data (length bytes)  |
    +-------------+------------------+----------------------+

    format byte = (format code << 2) | number of length bytes

    - The format code is 6 bits (the standard writes them in octal, e.g. A = 20).
    - The low 2 bits say how many length bytes follow (1, 2 or 3).
    - For a list, "length" is the number of child ITEMS.
      For everything else, "length" is the number of DATA BYTES.
    - All multi-byte numbers are big-endian ("network byte order").

Example: the ASCII string "MDLN" encodes as 41 04 4D 44 4C 4E
    0x41 = 0b010000_01 -> format code 0o20 (A), 1 length byte
    0x04 = 4 data bytes
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Tuple


class Secs2Error(Exception):
    """Base class for SECS-II codec errors."""


class Secs2EncodeError(Secs2Error):
    """Raised when an item cannot be encoded (bad value, too long, ...)."""


class Secs2DecodeError(Secs2Error):
    """Raised when bytes on the wire are not a valid SECS-II item."""


class Format(Enum):
    """SECS-II format codes from SEMI E5 (values are the 6-bit codes, in octal)."""

    L = 0o00        # list of items
    B = 0o10        # binary (raw bytes)
    BOOLEAN = 0o11  # one byte per value, 0 = False, anything else = True
    A = 0o20        # ASCII text
    I8 = 0o30       # 8-byte signed integer
    I1 = 0o31       # 1-byte signed integer
    I2 = 0o32       # 2-byte signed integer
    I4 = 0o34       # 4-byte signed integer
    F8 = 0o40       # 8-byte IEEE-754 float
    F4 = 0o44       # 4-byte IEEE-754 float
    U8 = 0o50       # 8-byte unsigned integer
    U1 = 0o51       # 1-byte unsigned integer
    U2 = 0o52       # 2-byte unsigned integer
    U4 = 0o54       # 4-byte unsigned integer


# Python `struct` codes for the numeric formats. The size comes from struct.
_STRUCT_CODES: Dict[Format, str] = {
    Format.I1: "b", Format.I2: "h", Format.I4: "i", Format.I8: "q",
    Format.U1: "B", Format.U2: "H", Format.U4: "I", Format.U8: "Q",
    Format.F4: "f", Format.F8: "d",
}

INTEGER_FORMATS = frozenset({
    Format.I1, Format.I2, Format.I4, Format.I8,
    Format.U1, Format.U2, Format.U4, Format.U8,
})
FLOAT_FORMATS = frozenset({Format.F4, Format.F8})
NUMERIC_FORMATS = INTEGER_FORMATS | FLOAT_FORMATS

# Largest length that fits in 3 length bytes.
MAX_ITEM_LENGTH = 0xFFFFFF


def _integer_range(fmt: Format) -> Tuple[int, int]:
    """Return the (min, max) value an integer format can hold."""
    bits = struct.calcsize(_STRUCT_CODES[fmt]) * 8
    if fmt.name.startswith("U"):
        return 0, (1 << bits) - 1
    return -(1 << (bits - 1)), (1 << (bits - 1)) - 1


@dataclass
class SecsItem:
    """One SECS-II data item.

    `value` depends on the format:
        L        -> list of SecsItem
        A        -> str
        B        -> bytes
        BOOLEAN  -> list of bool
        numeric  -> list of int / float  (SECS-II numerics are always arrays)
    """

    format: Format
    value: Any

    def __post_init__(self) -> None:
        # Normalise so equality comparisons behave predictably.
        if self.format is Format.A:
            if not isinstance(self.value, str):
                raise Secs2EncodeError(f"A item needs str, got {type(self.value).__name__}")
        elif self.format is Format.B:
            self.value = bytes(self.value)
        else:
            self.value = list(self.value)
            if self.format is Format.L:
                for child in self.value:
                    if not isinstance(child, SecsItem):
                        raise Secs2EncodeError("L item children must be SecsItem")
            elif self.format is Format.BOOLEAN:
                self.value = [bool(v) for v in self.value]

    def __len__(self) -> int:
        return len(self.value)

    def __getitem__(self, index: int) -> Any:
        return self.value[index]

    def to_python(self) -> Any:
        """Convert to plain Python. Single-element numeric arrays become scalars."""
        if self.format is Format.L:
            return [child.to_python() for child in self.value]
        if self.format in (Format.A, Format.B):
            return self.value
        if len(self.value) == 1:
            return self.value[0]
        return list(self.value)

    def to_sml(self, pretty: bool = False) -> str:
        return to_sml(self, pretty=pretty)


# ---------------------------------------------------------------------------
# Convenience constructors:  L(A("MDLN"), A("1.0")),  U4(1001, 1002),  ...
# ---------------------------------------------------------------------------

def L(*items: SecsItem) -> SecsItem:
    return SecsItem(Format.L, list(items))


def A(text: str = "") -> SecsItem:
    return SecsItem(Format.A, text)


def B(*values: Any) -> SecsItem:
    """B(0x01, 0x02) or B(b"\\x01\\x02")."""
    if len(values) == 1 and isinstance(values[0], (bytes, bytearray)):
        return SecsItem(Format.B, bytes(values[0]))
    return SecsItem(Format.B, bytes(values))


def BOOLEAN(*values: bool) -> SecsItem:
    return SecsItem(Format.BOOLEAN, list(values))


def _numeric_constructor(fmt: Format) -> Callable[..., SecsItem]:
    def make(*values: Any) -> SecsItem:
        return SecsItem(fmt, list(values))

    make.__name__ = fmt.name
    make.__doc__ = f"Build a {fmt.name} item, e.g. {fmt.name}(1, 2, 3)."
    return make


I1 = _numeric_constructor(Format.I1)
I2 = _numeric_constructor(Format.I2)
I4 = _numeric_constructor(Format.I4)
I8 = _numeric_constructor(Format.I8)
U1 = _numeric_constructor(Format.U1)
U2 = _numeric_constructor(Format.U2)
U4 = _numeric_constructor(Format.U4)
U8 = _numeric_constructor(Format.U8)
F4 = _numeric_constructor(Format.F4)
F8 = _numeric_constructor(Format.F8)


# ---------------------------------------------------------------------------
# Encoding
# ---------------------------------------------------------------------------

def encode(item: SecsItem) -> bytes:
    """Encode one item (recursively for lists) into SECS-II bytes."""
    if item.format is Format.L:
        data = b"".join(encode(child) for child in item.value)
        length = len(item.value)  # lists count ITEMS, not bytes
    else:
        data = _encode_data(item)
        length = len(data)
    return _encode_item_header(item.format, length) + data


def _encode_item_header(fmt: Format, length: int) -> bytes:
    """Format byte + the smallest number of length bytes that fits `length`."""
    if length <= 0xFF:
        n_length_bytes = 1
    elif length <= 0xFFFF:
        n_length_bytes = 2
    elif length <= MAX_ITEM_LENGTH:
        n_length_bytes = 3
    else:
        raise Secs2EncodeError(f"item length {length} exceeds 3 length bytes")
    format_byte = (fmt.value << 2) | n_length_bytes
    return bytes([format_byte]) + length.to_bytes(n_length_bytes, "big")


def _encode_data(item: SecsItem) -> bytes:
    fmt = item.format
    if fmt is Format.A:
        try:
            return item.value.encode("ascii")
        except UnicodeEncodeError as exc:
            raise Secs2EncodeError(f"non-ASCII character in A item: {item.value!r}") from exc
    if fmt is Format.B:
        return item.value
    if fmt is Format.BOOLEAN:
        return bytes(1 if v else 0 for v in item.value)

    code = _STRUCT_CODES[fmt]
    if fmt in INTEGER_FORMATS:
        low, high = _integer_range(fmt)
        for v in item.value:
            if not isinstance(v, int) or isinstance(v, bool):
                raise Secs2EncodeError(f"{fmt.name} needs int values, got {v!r}")
            if not low <= v <= high:
                raise Secs2EncodeError(f"{v} out of range for {fmt.name} ({low}..{high})")
    try:
        return struct.pack(f">{len(item.value)}{code}", *item.value)
    except struct.error as exc:
        raise Secs2EncodeError(f"cannot pack {item.value!r} as {fmt.name}: {exc}") from exc


# ---------------------------------------------------------------------------
# Decoding
# ---------------------------------------------------------------------------

def decode(data: bytes) -> SecsItem:
    """Decode exactly one item; trailing bytes are an error."""
    item, offset = decode_from(data, 0)
    if offset != len(data):
        raise Secs2DecodeError(f"{len(data) - offset} unexpected trailing byte(s)")
    return item


def decode_from(data: bytes, offset: int) -> Tuple[SecsItem, int]:
    """Decode one item starting at `offset`. Returns (item, offset after item)."""
    if offset >= len(data):
        raise Secs2DecodeError("unexpected end of data: expected a format byte")

    format_byte = data[offset]
    format_code = format_byte >> 2
    n_length_bytes = format_byte & 0b11
    if n_length_bytes == 0:
        raise Secs2DecodeError(f"format byte 0x{format_byte:02X} has 0 length bytes")
    try:
        fmt = Format(format_code)
    except ValueError:
        raise Secs2DecodeError(f"unknown format code 0o{format_code:o}") from None

    length_start = offset + 1
    length_end = length_start + n_length_bytes
    if length_end > len(data):
        raise Secs2DecodeError("unexpected end of data inside length bytes")
    length = int.from_bytes(data[length_start:length_end], "big")
    offset = length_end

    if fmt is Format.L:
        children: List[SecsItem] = []
        for _ in range(length):
            child, offset = decode_from(data, offset)
            children.append(child)
        return SecsItem(Format.L, children), offset

    data_end = offset + length
    if data_end > len(data):
        raise Secs2DecodeError(
            f"{fmt.name} item declares {length} bytes but only {len(data) - offset} remain"
        )
    raw = data[offset:data_end]
    return SecsItem(fmt, _decode_data(fmt, raw)), data_end


def _decode_data(fmt: Format, raw: bytes) -> Any:
    if fmt is Format.A:
        try:
            return raw.decode("ascii")
        except UnicodeDecodeError as exc:
            raise Secs2DecodeError("A item contains non-ASCII bytes") from exc
    if fmt is Format.B:
        return raw
    if fmt is Format.BOOLEAN:
        return [b != 0 for b in raw]

    code = _STRUCT_CODES[fmt]
    size = struct.calcsize(code)
    if len(raw) % size != 0:
        raise Secs2DecodeError(f"{fmt.name} data length {len(raw)} is not a multiple of {size}")
    return list(struct.unpack(f">{len(raw) // size}{code}", raw))


# ---------------------------------------------------------------------------
# SML (SECS Message Language) - the human-readable text form
# ---------------------------------------------------------------------------

_COMPACT_WIDTH = 72  # pretty mode keeps a list on one line if it fits


def to_sml(item: Optional[SecsItem], pretty: bool = False, _level: int = 0) -> str:
    """Render an item as SML, e.g. <L [2] <A "MDLN"> <A "1.0">>.

    pretty=True puts nested list children on their own indented lines
    (unless the whole list is short enough to fit on one line).
    """
    if item is None:
        return ""
    if item.format is not Format.L:
        return f"<{item.format.name}{_sml_values(item)}>"

    header = f"<L [{len(item.value)}]"
    if not item.value:
        return header + ">"
    one_line = header + " " + " ".join(to_sml(c) for c in item.value) + ">"
    if not pretty or len(one_line) + 2 * _level <= _COMPACT_WIDTH:
        return one_line
    pad = "  " * (_level + 1)
    children = "\n".join(pad + to_sml(c, True, _level + 1) for c in item.value)
    return f"{header}\n{children}\n{'  ' * _level}>"


def _sml_values(item: SecsItem) -> str:
    fmt = item.format
    if fmt is Format.A:
        escaped = item.value.replace("\\", "\\\\").replace('"', '\\"')
        return f' "{escaped}"'
    if fmt is Format.B:
        return "".join(f" 0x{b:02X}" for b in item.value)
    if fmt is Format.BOOLEAN:
        return "".join(" TRUE" if v else " FALSE" for v in item.value)
    if fmt is Format.F4:
        # F4 has ~7 significant digits; avoid printing float32 noise like 25.299999
        return "".join(f" {v:.7g}" for v in item.value)
    return "".join(f" {v!r}" for v in item.value)
