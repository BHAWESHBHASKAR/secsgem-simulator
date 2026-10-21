"""SecsMessage: one SECS-II message (stream, function, W-bit, body).

Naming: "S1F13" means Stream 1, Function 13.
    - Streams group related messages (S1 = equipment status, S2 = control,
      S5 = alarms, S6 = data collection, S9 = system errors).
    - Odd functions are PRIMARY messages (requests / reports).
    - Even functions are SECONDARY messages (the reply to function - 1).
    - Function 0 is the special "abort transaction" reply (e.g. S1F0).
    - The W-bit ("wait bit") on a primary means "I expect a reply".
"""

from __future__ import annotations

import struct
from dataclasses import dataclass
from typing import Optional

from .secs2 import SecsItem, to_sml


@dataclass
class SecsMessage:
    stream: int
    function: int
    wbit: bool = False
    body: Optional[SecsItem] = None
    # Filled in by the HSMS layer when a message is sent or received:
    system_bytes: int = 0   # transaction id that pairs a reply with its request
    session_id: int = 0     # HSMS session id == SECS device id for data messages
    # Set by the HSMS layer if the body bytes were not valid SECS-II:
    decode_error: Optional[str] = None

    @property
    def name(self) -> str:
        return f"S{self.stream}F{self.function}"

    @property
    def is_primary(self) -> bool:
        return self.function % 2 == 1

    def to_sml(self) -> str:
        """Full SML text, e.g. 'S1F1 W' or 'S1F2\\n<L [2] <A "X"> <A "1">>'."""
        head = self.name + (" W" if self.wbit else "")
        if self.body is None:
            return head
        return head + "\n" + to_sml(self.body, pretty=True)

    def header_bytes(self) -> bytes:
        """The 10-byte HSMS message header for this message.

        Used as MHEAD/SHEAD in S9 error messages so the receiver can tell which
        message the error refers to (the last 4 bytes are the system bytes).
        """
        byte2 = (0x80 if self.wbit else 0x00) | (self.stream & 0x7F)
        return struct.pack(">HBBBBI", self.session_id, byte2, self.function, 0, 0, self.system_bytes)
