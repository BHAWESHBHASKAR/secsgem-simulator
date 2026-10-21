"""Host (MES-side) client: an active HSMS connection with a small GEM API.

    host = GemHost("127.0.0.1", 5000)
    await host.connect()                      # TCP connect + HSMS Select
    await host.establish_communications()     # S1F13
    await host.request_online()               # S1F17
    result = await host.remote_command("START", {"PPID": "ETCH_OXIDE_01"})
    host.on_event(lambda report: print(report))

Incoming S6F11 / S5F1 are acknowledged automatically and passed to callbacks.
"""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, Iterable, List, Optional, Tuple, Union

from .codes import ACKC5, ACKC6, ALCD_SET_BIT, ALED_ENABLE, HCACK, S9_DESCRIPTIONS
from .hsms import HsmsConnection, HsmsError, HsmsTimeouts, open_active_connection
from .message import SecsMessage
from .secs2 import A, B, BOOLEAN, F4, I4, L, U4, Format, SecsItem

log = logging.getLogger("secsgem_sim.host")


class TransactionAbortedError(HsmsError):
    """The equipment answered with SxF0 (it refused the transaction)."""


class S9Error(HsmsError):
    """The equipment sent an S9Fx error about one of our messages."""

    def __init__(self, function: int) -> None:
        super().__init__(f"S9F{function}: {S9_DESCRIPTIONS.get(function, 'error')}")
        self.function = function


@dataclass
class EventReport:
    dataid: int
    ceid: int
    reports: Dict[int, List[Any]] = field(default_factory=dict)  # RPTID -> values


@dataclass
class AlarmReport:
    alcd: int
    alid: int
    text: str

    @property
    def is_set(self) -> bool:
        return bool(self.alcd & ALCD_SET_BIT)

    @property
    def category(self) -> int:
        return self.alcd & 0x7F


@dataclass
class RemoteCommandResult:
    hcack: HCACK
    cpacks: List[Tuple[str, int]]

    @property
    def accepted(self) -> bool:
        return self.hcack in (HCACK.OK, HCACK.ACK_FINISH_LATER)


def _to_item(value: Union[SecsItem, str, int, float, bool]) -> SecsItem:
    """Pick a sensible SECS-II type for a plain Python value."""
    if isinstance(value, SecsItem):
        return value
    if isinstance(value, bool):
        return BOOLEAN(value)
    if isinstance(value, str):
        return A(value)
    if isinstance(value, int):
        return U4(value) if value >= 0 else I4(value)
    if isinstance(value, float):
        return F4(value)
    raise TypeError(f"cannot convert {value!r} to a SECS-II item")


def _single_byte(item: SecsItem) -> int:
    return item.value[0]


class GemHost:
    def __init__(
        self,
        address: str = "127.0.0.1",
        port: int = 5000,
        *,
        device_id: int = 0,
        timeouts: Optional[HsmsTimeouts] = None,
        connect_attempts: int = 3,
    ) -> None:
        self.address = address
        self.port = port
        self.device_id = device_id
        self.timeouts = timeouts or HsmsTimeouts()
        self.connect_attempts = connect_attempts
        self.connection: Optional[HsmsConnection] = None
        self._event_callbacks: List[Callable[[EventReport], None]] = []
        self._alarm_callbacks: List[Callable[[AlarmReport], None]] = []
        self._event_waiters: List[Tuple[int, asyncio.Future]] = []
        self._alarm_waiters: List[Tuple[int, Optional[bool], asyncio.Future]] = []
        self.events_received: List[EventReport] = []
        self.alarms_received: List[AlarmReport] = []
        self.s9_received: List[int] = []

    # ----- connection --------------------------------------------------------

    async def connect(self) -> None:
        self.connection = await open_active_connection(
            self.address, self.port, message_handler=self._handle_message,
            session_id=self.device_id, timeouts=self.timeouts, name="host",
            connect_attempts=self.connect_attempts,
        )

    async def close(self) -> None:
        if self.connection is not None:
            await self.connection.separate()

    async def linktest(self) -> float:
        return await self._conn().linktest()

    def _conn(self) -> HsmsConnection:
        if self.connection is None:
            raise HsmsError("not connected; call connect() first")
        return self.connection

    async def send(self, msg: SecsMessage) -> Optional[SecsMessage]:
        """Send a primary; return the reply. Raises on SxF0 or S9Fx."""
        reply = await self._conn().send_request(msg)
        if reply is not None and reply.function == 0:
            raise TransactionAbortedError(f"equipment answered {msg.name} with {reply.name}")
        return reply

    # ----- callbacks / waiting for unsolicited messages ----------------------

    def on_event(self, callback: Callable[[EventReport], None]) -> None:
        self._event_callbacks.append(callback)

    def on_alarm(self, callback: Callable[[AlarmReport], None]) -> None:
        self._alarm_callbacks.append(callback)

    def expect_event(self, ceid: int) -> "asyncio.Future[EventReport]":
        """Register interest BEFORE triggering the event, then await the future.

        Registering first avoids a race where the event arrives before we
        start waiting for it.
        """
        future = asyncio.get_event_loop().create_future()
        self._event_waiters.append((ceid, future))
        return future

    def expect_alarm(self, alid: int, is_set: Optional[bool] = True) -> "asyncio.Future[AlarmReport]":
        future = asyncio.get_event_loop().create_future()
        self._alarm_waiters.append((alid, is_set, future))
        return future

    async def _handle_message(self, msg: SecsMessage) -> Optional[SecsMessage]:
        if (msg.stream, msg.function) == (6, 11):
            self._dispatch_event(self._parse_event_report(msg.body))
            return SecsMessage(6, 12, body=B(ACKC6.OK))
        if (msg.stream, msg.function) == (5, 1):
            alcd, alid, text = msg.body.value
            self._dispatch_alarm(AlarmReport(_single_byte(alcd), alid.value[0], text.value))
            return SecsMessage(5, 2, body=B(ACKC5.OK))
        if msg.stream == 9:
            self._handle_s9(msg)
            return None
        log.warning("host does not handle %s", msg.name)
        return SecsMessage(msg.stream, 0) if msg.wbit else None

    @staticmethod
    def _parse_event_report(body: SecsItem) -> EventReport:
        dataid, ceid, reports = body.value
        parsed = {}
        for report in reports.value:
            rptid, values = report.value
            parsed[rptid.value[0]] = [v.to_python() for v in values.value]
        return EventReport(dataid.value[0], ceid.value[0], parsed)

    def _dispatch_event(self, report: EventReport) -> None:
        self.events_received.append(report)
        for ceid, future in list(self._event_waiters):
            if ceid == report.ceid and not future.done():
                future.set_result(report)
                self._event_waiters.remove((ceid, future))
        for callback in self._event_callbacks:
            callback(report)

    def _dispatch_alarm(self, alarm: AlarmReport) -> None:
        self.alarms_received.append(alarm)
        for entry in list(self._alarm_waiters):
            alid, is_set, future = entry
            if alid == alarm.alid and is_set in (None, alarm.is_set) and not future.done():
                future.set_result(alarm)
                self._alarm_waiters.remove(entry)
        for callback in self._alarm_callbacks:
            callback(alarm)

    def _handle_s9(self, msg: SecsMessage) -> None:
        """S9Fx bodies carry the header of OUR message; fail that transaction now
        instead of waiting for T3 to expire."""
        self.s9_received.append(msg.function)
        log.warning("equipment reported S9F%d (%s)", msg.function,
                    S9_DESCRIPTIONS.get(msg.function, "?"))
        if msg.body is not None and msg.body.format is Format.B and len(msg.body.value) == 10:
            system_bytes = int.from_bytes(msg.body.value[6:10], "big")
            self._conn().fail_transaction(system_bytes, S9Error(msg.function))

    # ----- Stream 1 ----------------------------------------------------------

    async def are_you_there(self) -> Tuple[str, str]:
        reply = await self.send(SecsMessage(1, 1, True))
        mdln, softrev = reply.body.value
        return mdln.value, softrev.value

    async def establish_communications(self) -> int:
        reply = await self.send(SecsMessage(1, 13, True, L()))
        return _single_byte(reply.body.value[0])

    async def request_offline(self) -> int:
        reply = await self.send(SecsMessage(1, 15, True))
        return _single_byte(reply.body)

    async def request_online(self) -> int:
        reply = await self.send(SecsMessage(1, 17, True))
        return _single_byte(reply.body)

    async def get_status_variables(self, svids: Iterable[int] = ()) -> List[Any]:
        """S1F3. Unknown SVIDs come back as [] (an empty item)."""
        reply = await self.send(SecsMessage(1, 3, True, L(*[U4(s) for s in svids])))
        return [item.to_python() for item in reply.body.value]

    async def get_sv_namelist(self, svids: Iterable[int] = ()) -> List[Tuple[int, str, str]]:
        reply = await self.send(SecsMessage(1, 11, True, L(*[U4(s) for s in svids])))
        return [(row.value[0].value[0], row.value[1].value, row.value[2].value)
                for row in reply.body.value]

    # ----- Stream 2 ----------------------------------------------------------

    async def get_equipment_constants(self, ecids: Iterable[int] = ()) -> List[Any]:
        reply = await self.send(SecsMessage(2, 13, True, L(*[U4(e) for e in ecids])))
        return [item.to_python() for item in reply.body.value]

    async def set_equipment_constants(self, values: Dict[int, Any]) -> int:
        body = L(*[L(U4(ecid), _to_item(value)) for ecid, value in values.items()])
        reply = await self.send(SecsMessage(2, 15, True, body))
        return _single_byte(reply.body)

    async def define_reports(self, reports: Dict[int, List[int]], dataid: int = 1) -> int:
        body = L(U4(dataid), L(*[L(U4(rptid), L(*[U4(v) for v in vids]))
                                 for rptid, vids in reports.items()]))
        reply = await self.send(SecsMessage(2, 33, True, body))
        return _single_byte(reply.body)

    async def link_event_reports(self, links: Dict[int, List[int]], dataid: int = 1) -> int:
        body = L(U4(dataid), L(*[L(U4(ceid), L(*[U4(r) for r in rptids]))
                                 for ceid, rptids in links.items()]))
        reply = await self.send(SecsMessage(2, 35, True, body))
        return _single_byte(reply.body)

    async def enable_events(self, ceids: Iterable[int] = (), enable: bool = True) -> int:
        body = L(BOOLEAN(enable), L(*[U4(c) for c in ceids]))
        reply = await self.send(SecsMessage(2, 37, True, body))
        return _single_byte(reply.body)

    async def remote_command(self, rcmd: str, params: Optional[Dict[str, Any]] = None) -> RemoteCommandResult:
        param_items = [L(A(name), _to_item(value)) for name, value in (params or {}).items()]
        reply = await self.send(SecsMessage(2, 41, True, L(A(rcmd), L(*param_items))))
        hcack_item, cpack_list = reply.body.value
        cpacks = [(entry.value[0].value, _single_byte(entry.value[1])) for entry in cpack_list.value]
        return RemoteCommandResult(HCACK(_single_byte(hcack_item)), cpacks)

    # ----- Stream 5 ----------------------------------------------------------

    async def enable_alarms(self, alids: Iterable[int] = (), enable: bool = True) -> int:
        alids = list(alids)
        if len(alids) > 1:  # S5F3 takes one ALID (or zero-length for all)
            results = [await self.enable_alarms([a], enable) for a in alids]
            return max(results)
        body = L(B(ALED_ENABLE if enable else 0), U4(*alids))
        reply = await self.send(SecsMessage(5, 3, True, body))
        return _single_byte(reply.body)

    async def list_alarms(self, alids: Iterable[int] = ()) -> List[AlarmReport]:
        reply = await self.send(SecsMessage(5, 5, True, U4(*alids)))
        result = []
        for row in reply.body.value:
            alcd, alid, text = row.value
            result.append(AlarmReport(alcd.value[0] if alcd.value else 0, alid.value[0], text.value))
        return result
