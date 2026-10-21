"""HSMS transport (SEMI E37) over TCP/IP using asyncio.

HSMS carries SECS-II messages over a TCP connection. Every HSMS message is:

    +----------------+---------------------+---------------------------+
    | length (4 B)   | header (10 B)       | body (SECS-II item bytes) |
    +----------------+---------------------+---------------------------+
      length = 10 + len(body), big-endian

The 10-byte header:

    bytes 0-1  session id   (data messages: SECS device id; control: 0xFFFF)
    byte  2    header byte 2 (data: W-bit << 7 | stream;   control: usually 0)
    byte  3    header byte 3 (data: function;              control: status/reason)
    byte  4    PType        (0 = SECS-II encoding; nothing else is defined)
    byte  5    SType        (0 = data message, 1..9 = control messages below)
    bytes 6-9  system bytes (transaction id; a reply copies the request's value)

Connection states (E37):

    NOT CONNECTED --TCP connect--> CONNECTED/NOT SELECTED --Select--> CONNECTED/SELECTED
         ^                                  |   ^                            |
         +------ TCP close / Separate ------+   +-------- Deselect ----------+

Only in SELECTED may data (SECS-II) messages flow.

Timers used here (defaults are the usual E37/E5 values):
    T3  reply timeout          - how long to wait for the reply to a W-bit data message
    T5  connect separation     - delay between active-mode connection attempts
    T6  control transaction    - how long to wait for Select/Deselect/Linktest .rsp
    T7  not-selected timeout   - passive side drops a TCP connection that never selects
    T8  network intercharacter - max time to receive the rest of a message once started
    linktest interval          - how often to send Linktest.req as a heartbeat
"""

from __future__ import annotations

import asyncio
import logging
import struct
from dataclasses import dataclass, replace
from enum import Enum, IntEnum
from typing import Awaitable, Callable, Dict, List, Optional, Set

from .message import SecsMessage
from .secs2 import Secs2DecodeError, decode, encode

log = logging.getLogger("secsgem_sim.hsms")
trace_log = logging.getLogger("secsgem_sim.trace")

HEADER_STRUCT = struct.Struct(">HBBBBI")   # the 10-byte header
LENGTH_STRUCT = struct.Struct(">I")        # the 4-byte length prefix
HEADER_SIZE = HEADER_STRUCT.size           # == 10
CONTROL_SESSION_ID = 0xFFFF
PTYPE_SECS2 = 0
DEFAULT_MAX_MESSAGE_LENGTH = 16 * 1024 * 1024  # sanity limit against garbage lengths


class SType(IntEnum):
    DATA = 0
    SELECT_REQ = 1
    SELECT_RSP = 2
    DESELECT_REQ = 3
    DESELECT_RSP = 4
    LINKTEST_REQ = 5
    LINKTEST_RSP = 6
    REJECT_REQ = 7
    SEPARATE_REQ = 9


CONTROL_NAMES = {
    SType.SELECT_REQ: "Select.req",
    SType.SELECT_RSP: "Select.rsp",
    SType.DESELECT_REQ: "Deselect.req",
    SType.DESELECT_RSP: "Deselect.rsp",
    SType.LINKTEST_REQ: "Linktest.req",
    SType.LINKTEST_RSP: "Linktest.rsp",
    SType.REJECT_REQ: "Reject.req",
    SType.SEPARATE_REQ: "Separate.req",
}


class SelectStatus(IntEnum):
    ESTABLISHED = 0
    ALREADY_ACTIVE = 1
    NOT_READY = 2
    CONNECT_EXHAUST = 3


class DeselectStatus(IntEnum):
    ENDED = 0
    NOT_ESTABLISHED = 1
    BUSY = 2


class RejectReason(IntEnum):
    STYPE_NOT_SUPPORTED = 1
    PTYPE_NOT_SUPPORTED = 2
    TRANSACTION_NOT_OPEN = 3
    ENTITY_NOT_SELECTED = 4


class ConnectionState(Enum):
    NOT_CONNECTED = "NOT CONNECTED"
    NOT_SELECTED = "CONNECTED/NOT SELECTED"
    SELECTED = "CONNECTED/SELECTED"


# --- errors -----------------------------------------------------------------

class HsmsError(Exception):
    """Base class for HSMS errors."""


class HsmsProtocolError(HsmsError):
    """The peer sent something that is not valid HSMS."""


class ConnectionClosedError(HsmsError):
    """The TCP connection is gone (or was never there)."""


class NotSelectedError(HsmsError):
    """Tried to send a data message while not in the SELECTED state."""


class ReplyTimeoutError(HsmsError):
    """T3: no reply to a W-bit data message in time.

    `message` is the primary that went unanswered (with its system bytes), so
    the caller can e.g. send S9F9 quoting its header.
    """

    def __init__(self, text: str, message: Optional[SecsMessage] = None) -> None:
        super().__init__(text)
        self.message = message


class ControlTimeoutError(HsmsError):
    """T6: no response to a control message (Select/Deselect/Linktest) in time."""


class SelectRejectedError(HsmsError):
    """The passive side answered Select.req with a non-zero status."""


class RejectedError(HsmsError):
    """The peer answered our message with Reject.req."""


@dataclass
class HsmsTimeouts:
    t3: float = 45.0
    t5: float = 10.0
    t6: float = 5.0
    t7: float = 10.0
    t8: float = 5.0
    linktest: Optional[float] = 30.0   # None or 0 disables the periodic linktest


# --- header ----------------------------------------------------------------

@dataclass(frozen=True)
class HsmsHeader:
    session_id: int
    byte2: int
    byte3: int
    ptype: int
    stype: int
    system_bytes: int

    def pack(self) -> bytes:
        return HEADER_STRUCT.pack(
            self.session_id, self.byte2, self.byte3, self.ptype, self.stype, self.system_bytes
        )

    @classmethod
    def unpack(cls, data: bytes) -> "HsmsHeader":
        if len(data) != HEADER_SIZE:
            raise HsmsProtocolError(f"header must be {HEADER_SIZE} bytes, got {len(data)}")
        return cls(*HEADER_STRUCT.unpack(data))

    # Interpretations that only make sense for data messages (SType 0):
    @property
    def wbit(self) -> bool:
        return bool(self.byte2 & 0x80)

    @property
    def stream(self) -> int:
        return self.byte2 & 0x7F

    @property
    def function(self) -> int:
        return self.byte3


def data_header(msg: SecsMessage) -> HsmsHeader:
    byte2 = (0x80 if msg.wbit else 0x00) | (msg.stream & 0x7F)
    return HsmsHeader(msg.session_id, byte2, msg.function, PTYPE_SECS2, SType.DATA, msg.system_bytes)


def control_header(stype: SType, system_bytes: int, byte2: int = 0, byte3: int = 0) -> HsmsHeader:
    return HsmsHeader(CONTROL_SESSION_ID, byte2, byte3, PTYPE_SECS2, stype, system_bytes)


def frame(header: HsmsHeader, body: bytes = b"") -> bytes:
    """Prefix header+body with the 4-byte length field."""
    return LENGTH_STRUCT.pack(HEADER_SIZE + len(body)) + header.pack() + body


# --- connection ------------------------------------------------------------

MessageHandler = Callable[[SecsMessage], Awaitable[Optional[SecsMessage]]]
StateListener = Callable[[ConnectionState, ConnectionState], None]


class HsmsConnection:
    """One HSMS connection over one TCP socket (HSMS-SS: a single session).

    The same class is used for both ends; `passive=True` means we accepted the
    TCP connection (equipment side) and so we own the T7 timer.

    Incoming primary data messages are passed to `message_handler`. If it
    returns a SecsMessage and the primary had the W-bit set, that message is
    sent back as the reply (with the same system bytes).
    """

    def __init__(
        self,
        reader: asyncio.StreamReader,
        writer: asyncio.StreamWriter,
        *,
        passive: bool,
        message_handler: Optional[MessageHandler] = None,
        session_id: int = 0,
        timeouts: Optional[HsmsTimeouts] = None,
        name: str = "hsms",
        max_message_length: int = DEFAULT_MAX_MESSAGE_LENGTH,
    ) -> None:
        self._reader = reader
        self._writer = writer
        self.passive = passive
        self.message_handler = message_handler
        self.session_id = session_id
        self.timeouts = timeouts or HsmsTimeouts()
        self.name = name
        self.max_message_length = max_message_length

        self.state = ConnectionState.NOT_SELECTED  # TCP is already connected
        self.state_listeners: List[StateListener] = []
        self.closed = asyncio.Event()

        # Open transactions, keyed by system bytes. Data replies and control
        # responses are kept apart so a stray reply of one kind can't
        # complete a transaction of the other kind.
        self._pending_replies: Dict[int, asyncio.Future] = {}
        self._pending_control: Dict[int, asyncio.Future] = {}
        self._next_system_bytes = 1

        self._write_lock = asyncio.Lock()
        self._tasks: Set[asyncio.Task] = set()
        self._read_task: Optional[asyncio.Task] = None
        self._t7_task: Optional[asyncio.Task] = None
        self._linktest_task: Optional[asyncio.Task] = None

        peer = writer.get_extra_info("peername")
        self.peer = f"{peer[0]}:{peer[1]}" if peer else "?"

    # ----- lifecycle --------------------------------------------------------

    def start(self) -> None:
        """Start the background read loop (and T7 timer on the passive side)."""
        log.info("[%s] TCP connected to %s -> %s", self.name, self.peer, self.state.value)
        self._read_task = asyncio.ensure_future(self._read_loop())
        if self.passive:
            self._start_t7_timer()

    async def close(self, reason: str = "closed") -> None:
        """Close the socket, fail all open transactions, stop timers."""
        if self.state is ConnectionState.NOT_CONNECTED:
            return
        log.info("[%s] closing connection: %s", self.name, reason)
        self._set_state(ConnectionState.NOT_CONNECTED)

        error = ConnectionClosedError(reason)
        for pending in (self._pending_replies, self._pending_control):
            for future in pending.values():
                if not future.done():
                    future.set_exception(error)
            pending.clear()

        current = asyncio.current_task()
        for task in [self._read_task, self._t7_task, self._linktest_task, *self._tasks]:
            if task is not None and task is not current and not task.done():
                task.cancel()

        self._writer.close()
        try:
            await self._writer.wait_closed()
        except (ConnectionError, OSError):
            pass
        self.closed.set()

    # ----- state ------------------------------------------------------------

    def _set_state(self, new: ConnectionState) -> None:
        old = self.state
        if old is new:
            return
        self.state = new
        log.info("[%s] HSMS state %s -> %s", self.name, old.value, new.value)

        if new is ConnectionState.SELECTED:
            self._cancel(self._t7_task)
            self._start_linktest_timer()
        elif new is ConnectionState.NOT_SELECTED:
            self._cancel(self._linktest_task)
            if self.passive:
                self._start_t7_timer()

        for listener in list(self.state_listeners):
            try:
                listener(old, new)
            except Exception:  # a buggy listener must not break the transport
                log.exception("[%s] state listener failed", self.name)

    @property
    def is_selected(self) -> bool:
        return self.state is ConnectionState.SELECTED

    # ----- timers -----------------------------------------------------------

    @staticmethod
    def _cancel(task: Optional[asyncio.Task]) -> None:
        if task is not None and not task.done() and task is not asyncio.current_task():
            task.cancel()

    def _start_t7_timer(self) -> None:
        self._cancel(self._t7_task)
        self._t7_task = asyncio.ensure_future(self._t7_timer())

    async def _t7_timer(self) -> None:
        await asyncio.sleep(self.timeouts.t7)
        if self.state is ConnectionState.NOT_SELECTED:
            log.warning("[%s] T7 timeout (%.1fs): not selected, dropping TCP connection",
                        self.name, self.timeouts.t7)
            await self.close("T7 not-selected timeout")

    def _start_linktest_timer(self) -> None:
        self._cancel(self._linktest_task)
        if self.timeouts.linktest:
            self._linktest_task = asyncio.ensure_future(self._linktest_loop())

    async def _linktest_loop(self) -> None:
        while self.state is ConnectionState.SELECTED:
            await asyncio.sleep(self.timeouts.linktest)
            try:
                await self.linktest()
            except (ControlTimeoutError, ConnectionClosedError):
                return  # linktest() already closed the connection on T6

    # ----- reading ----------------------------------------------------------

    async def _read_loop(self) -> None:
        reason = "peer closed the connection"
        try:
            while self.state is not ConnectionState.NOT_CONNECTED:
                # Between messages we may wait forever...
                length_bytes = await self._reader.readexactly(LENGTH_STRUCT.size)
                (length,) = LENGTH_STRUCT.unpack(length_bytes)
                if length < HEADER_SIZE or length > self.max_message_length:
                    raise HsmsProtocolError(f"invalid message length {length}")
                # ...but once a message has started, the rest must arrive within T8.
                # (Real T8 is per-gap between bytes; a whole-message limit is a
                # simpler approximation that catches the same stuck-peer case.)
                data = await asyncio.wait_for(self._reader.readexactly(length), self.timeouts.t8)
                header = HsmsHeader.unpack(data[:HEADER_SIZE])
                await self._handle_packet(header, data[HEADER_SIZE:])
        except asyncio.IncompleteReadError:
            pass
        except asyncio.TimeoutError:
            reason = "T8 timeout while receiving a message"
            log.warning("[%s] %s", self.name, reason)
        except HsmsProtocolError as exc:
            reason = f"protocol error: {exc}"
            log.error("[%s] %s", self.name, reason)
        except (ConnectionError, OSError, ConnectionClosedError) as exc:
            reason = f"socket error: {exc}"
        finally:
            await self.close(reason)

    async def _handle_packet(self, header: HsmsHeader, body: bytes) -> None:
        if header.ptype != PTYPE_SECS2:
            await self._send_reject(header, RejectReason.PTYPE_NOT_SUPPORTED, header.ptype)
            return
        if header.stype == SType.DATA:
            await self._handle_data(header, body)
            return

        try:
            stype = SType(header.stype)
        except ValueError:
            await self._send_reject(header, RejectReason.STYPE_NOT_SUPPORTED, header.stype)
            return
        self._trace_control("<--", stype, header)

        if stype is SType.SELECT_REQ:
            if self.state is ConnectionState.NOT_SELECTED:
                status = SelectStatus.ESTABLISHED
            else:
                status = SelectStatus.ALREADY_ACTIVE
            # Change state BEFORE replying, so we are ready for data the moment
            # the peer sees our Select.rsp.
            self._set_state(ConnectionState.SELECTED)
            await self._send_control(SType.SELECT_RSP, header.system_bytes, byte3=status)

        elif stype is SType.DESELECT_REQ:
            if self.state is ConnectionState.SELECTED:
                self._set_state(ConnectionState.NOT_SELECTED)
                status = DeselectStatus.ENDED
            else:
                status = DeselectStatus.NOT_ESTABLISHED
            await self._send_control(SType.DESELECT_RSP, header.system_bytes, byte3=status)

        elif stype is SType.LINKTEST_REQ:
            await self._send_control(SType.LINKTEST_RSP, header.system_bytes)

        elif stype is SType.SEPARATE_REQ:
            await self.close("peer sent Separate.req")

        elif stype is SType.REJECT_REQ:
            error = RejectedError(f"peer rejected our message (reason {header.byte3})")
            for pending in (self._pending_replies, self._pending_control):
                future = pending.get(header.system_bytes)
                if future is not None and not future.done():
                    future.set_exception(error)

        else:  # one of the *.rsp messages
            future = self._pending_control.get(header.system_bytes)
            if future is None:
                await self._send_reject(header, RejectReason.TRANSACTION_NOT_OPEN, header.stype)
                return
            # Apply select/deselect state changes here in the read loop (not in
            # select()) so a data message right behind the .rsp is not rejected.
            if stype is SType.SELECT_RSP and header.byte3 in (0, 1):
                self._set_state(ConnectionState.SELECTED)
            elif stype is SType.DESELECT_RSP and header.byte3 == DeselectStatus.ENDED:
                self._set_state(ConnectionState.NOT_SELECTED)
            if not future.done():
                future.set_result(header)

    async def _handle_data(self, header: HsmsHeader, body: bytes) -> None:
        if self.state is not ConnectionState.SELECTED:
            await self._send_reject(header, RejectReason.ENTITY_NOT_SELECTED, header.stype)
            return

        msg = SecsMessage(
            stream=header.stream, function=header.function, wbit=header.wbit,
            system_bytes=header.system_bytes, session_id=header.session_id,
        )
        if body:
            try:
                msg.body = decode(body)
            except Secs2DecodeError as exc:
                msg.decode_error = str(exc)
        self._trace_data("<--", msg)

        # Is this the reply to something we sent? (Replies have even function
        # numbers; F0 is the "abort transaction" reply.)
        future = self._pending_replies.get(msg.system_bytes)
        if future is not None and msg.function % 2 == 0:
            if not future.done():
                future.set_result(msg)
            return
        if msg.function % 2 == 0:
            log.warning("[%s] discarding unexpected reply %s (no open transaction 0x%08X)",
                        self.name, msg.name, msg.system_bytes)
            return

        # A new primary message. Handle it in its own task so a slow handler
        # (or one that itself sends a request and waits) never blocks reading.
        self._spawn(self._dispatch_primary(msg))

    async def _dispatch_primary(self, msg: SecsMessage) -> None:
        if self.message_handler is None:
            return
        try:
            reply = await self.message_handler(msg)
        except Exception:
            log.exception("[%s] message handler failed for %s", self.name, msg.name)
            return
        if reply is not None and msg.wbit:
            try:
                await self.send_reply(msg, reply)
            except (ConnectionClosedError, NotSelectedError) as exc:
                log.warning("[%s] could not send reply %s: %s", self.name, reply.name, exc)

    def _spawn(self, coro: Awaitable) -> None:
        task = asyncio.ensure_future(coro)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    # ----- writing ----------------------------------------------------------

    def _allocate_system_bytes(self) -> int:
        value = self._next_system_bytes
        self._next_system_bytes = (self._next_system_bytes % 0xFFFFFFFF) + 1  # skip 0
        return value

    async def _write(self, header: HsmsHeader, body: bytes = b"") -> None:
        if self.state is ConnectionState.NOT_CONNECTED:
            raise ConnectionClosedError("not connected")
        # The lock keeps concurrent senders from interleaving drain() calls.
        async with self._write_lock:
            try:
                self._writer.write(frame(header, body))
                await self._writer.drain()
            except (ConnectionError, OSError) as exc:
                raise ConnectionClosedError(str(exc)) from exc

    async def _send_control(self, stype: SType, system_bytes: int, byte2: int = 0, byte3: int = 0) -> None:
        header = control_header(stype, system_bytes, byte2, byte3)
        self._trace_control("-->", stype, header)
        await self._write(header)

    async def _send_reject(self, rejected: HsmsHeader, reason: RejectReason, byte2: int) -> None:
        log.warning("[%s] rejecting message (stype=%d): %s", self.name, rejected.stype, reason.name)
        await self._send_control(SType.REJECT_REQ, rejected.system_bytes, byte2=byte2, byte3=reason)

    async def _send_data(self, msg: SecsMessage) -> None:
        if self.state is not ConnectionState.SELECTED:
            raise NotSelectedError(f"cannot send {msg.name}: state is {self.state.value}")
        body = encode(msg.body) if msg.body is not None else b""
        self._trace_data("-->", msg)
        await self._write(data_header(msg), body)

    async def send_request(self, msg: SecsMessage) -> Optional[SecsMessage]:
        """Send a primary message. If the W-bit is set, wait (up to T3) for the reply."""
        msg = replace(msg, system_bytes=self._allocate_system_bytes(), session_id=self.session_id)
        if not msg.wbit:
            await self._send_data(msg)
            return None

        future = asyncio.get_event_loop().create_future()
        self._pending_replies[msg.system_bytes] = future
        try:
            await self._send_data(msg)
            return await asyncio.wait_for(future, self.timeouts.t3)
        except asyncio.TimeoutError:
            log.warning("[%s] T3 timeout (%.1fs) waiting for reply to %s (sys=0x%08X)",
                        self.name, self.timeouts.t3, msg.name, msg.system_bytes)
            raise ReplyTimeoutError(f"T3 timeout waiting for reply to {msg.name}", msg) from None
        finally:
            self._pending_replies.pop(msg.system_bytes, None)

    async def send_reply(self, primary: SecsMessage, reply: SecsMessage) -> None:
        """Send a secondary message that answers `primary` (same system bytes)."""
        reply = replace(reply, wbit=False, system_bytes=primary.system_bytes,
                        session_id=primary.session_id)
        await self._send_data(reply)

    def fail_transaction(self, system_bytes: int, error: Exception) -> bool:
        """Complete an open transaction with an error (e.g. peer sent S9Fx about it)."""
        future = self._pending_replies.get(system_bytes)
        if future is not None and not future.done():
            future.set_exception(error)
            return True
        return False

    # ----- control transactions --------------------------------------------

    async def _control_transaction(self, stype: SType) -> HsmsHeader:
        """Send a control request and wait up to T6 for its response.

        Per E37, a T6 timeout means the link is unusable, so we close it.
        """
        system_bytes = self._allocate_system_bytes()
        future = asyncio.get_event_loop().create_future()
        self._pending_control[system_bytes] = future
        try:
            await self._send_control(stype, system_bytes)
            return await asyncio.wait_for(future, self.timeouts.t6)
        except asyncio.TimeoutError:
            log.warning("[%s] T6 timeout (%.1fs) waiting for response to %s",
                        self.name, self.timeouts.t6, CONTROL_NAMES[stype])
            await self.close(f"T6 timeout on {CONTROL_NAMES[stype]}")
            raise ControlTimeoutError(f"T6 timeout on {CONTROL_NAMES[stype]}") from None
        finally:
            self._pending_control.pop(system_bytes, None)

    async def select(self) -> None:
        """Active side: Select.req -> Select.rsp. Moves the connection to SELECTED."""
        rsp = await self._control_transaction(SType.SELECT_REQ)
        if rsp.byte3 not in (SelectStatus.ESTABLISHED, SelectStatus.ALREADY_ACTIVE):
            raise SelectRejectedError(f"Select.rsp status {rsp.byte3}")

    async def deselect(self) -> None:
        rsp = await self._control_transaction(SType.DESELECT_REQ)
        if rsp.byte3 != DeselectStatus.ENDED:
            raise HsmsError(f"Deselect.rsp status {rsp.byte3}")

    async def linktest(self) -> float:
        """Send Linktest.req and return the round-trip time in seconds."""
        loop = asyncio.get_event_loop()
        start = loop.time()
        await self._control_transaction(SType.LINKTEST_REQ)
        return loop.time() - start

    async def separate(self) -> None:
        """Tell the peer we are ending the session (no reply), then close."""
        if self.state is not ConnectionState.NOT_CONNECTED:
            try:
                await self._send_control(SType.SEPARATE_REQ, self._allocate_system_bytes())
            except ConnectionClosedError:
                pass
        await self.close("sent Separate.req")

    # ----- tracing ----------------------------------------------------------

    def _trace_data(self, direction: str, msg: SecsMessage) -> None:
        text = msg.to_sml().replace("\n", "\n    ")
        if msg.decode_error:
            text += f"\n    !! body could not be decoded: {msg.decode_error}"
        trace_log.info(
            "[%s] %s %s  (sys=0x%08X)", self.name, direction, text, msg.system_bytes,
            extra={"direction": direction, "sxfy": msg.name, "wbit": msg.wbit,
                   "system_bytes": msg.system_bytes, "peer": self.peer},
        )

    def _trace_control(self, direction: str, stype: SType, header: HsmsHeader) -> None:
        detail = ""
        if stype in (SType.SELECT_RSP, SType.DESELECT_RSP):
            detail = f" status={header.byte3}"
        elif stype is SType.REJECT_REQ:
            detail = f" reason={header.byte3}"
        trace_log.info(
            "[%s] %s %s%s  (sys=0x%08X)", self.name, direction, CONTROL_NAMES[stype], detail,
            header.system_bytes,
            extra={"direction": direction, "control": CONTROL_NAMES[stype],
                   "system_bytes": header.system_bytes, "peer": self.peer},
        )


# --- passive (equipment) and active (host) entry points --------------------

class HsmsPassiveServer:
    """Listens for the host's TCP connection (passive mode, typical for equipment).

    HSMS-SS allows one connection at a time; extra connections are refused.
    """

    def __init__(
        self,
        address: str,
        port: int,
        *,
        message_handler: MessageHandler,
        session_id: int = 0,
        timeouts: Optional[HsmsTimeouts] = None,
        on_connection: Optional[Callable[[HsmsConnection], None]] = None,
        name: str = "equipment",
    ) -> None:
        self.address = address
        self.port = port
        self.message_handler = message_handler
        self.session_id = session_id
        self.timeouts = timeouts or HsmsTimeouts()
        self.on_connection = on_connection
        self.name = name
        self.connection: Optional[HsmsConnection] = None
        self._server: Optional[asyncio.AbstractServer] = None

    async def start(self) -> None:
        self._server = await asyncio.start_server(self._accept, self.address, self.port)
        self.port = self._server.sockets[0].getsockname()[1]  # resolves port=0
        log.info("[%s] HSMS passive: listening on %s:%d", self.name, self.address, self.port)

    async def _accept(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
        if self.connection is not None and self.connection.state is not ConnectionState.NOT_CONNECTED:
            log.warning("[%s] refusing second connection (HSMS-SS allows one)", self.name)
            writer.close()
            return
        conn = HsmsConnection(
            reader, writer, passive=True, message_handler=self.message_handler,
            session_id=self.session_id, timeouts=self.timeouts, name=self.name,
        )
        self.connection = conn
        if self.on_connection is not None:
            self.on_connection(conn)
        conn.start()

    async def stop(self) -> None:
        if self.connection is not None:
            await self.connection.close("server stopping")
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()


async def open_active_connection(
    address: str,
    port: int,
    *,
    message_handler: Optional[MessageHandler] = None,
    session_id: int = 0,
    timeouts: Optional[HsmsTimeouts] = None,
    name: str = "host",
    connect_attempts: int = 1,
) -> HsmsConnection:
    """Active mode (typical for the host): TCP connect, then Select.

    If the TCP connect fails, wait T5 before trying again (up to connect_attempts).
    """
    timeouts = timeouts or HsmsTimeouts()
    attempt = 0
    while True:
        attempt += 1
        try:
            reader, writer = await asyncio.open_connection(address, port)
            break
        except OSError as exc:
            if attempt >= connect_attempts:
                raise ConnectionClosedError(f"cannot connect to {address}:{port}: {exc}") from exc
            log.warning("[%s] connect failed (%s); retrying in T5=%.1fs", name, exc, timeouts.t5)
            await asyncio.sleep(timeouts.t5)

    conn = HsmsConnection(
        reader, writer, passive=False, message_handler=message_handler,
        session_id=session_id, timeouts=timeouts, name=name,
    )
    conn.start()
    await conn.select()
    return conn
