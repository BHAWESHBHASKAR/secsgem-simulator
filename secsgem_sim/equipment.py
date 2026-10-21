"""GEM equipment simulator: a single-chamber etch tool (subset of SEMI E30).

What the tool does
------------------
- Listens for the host over HSMS (passive mode).
- Answers status / constant / report-setup / alarm-setup / remote-command
  messages from the host.
- Runs a background simulation: chamber temperature and pressure drift toward
  setpoints while a recipe is executing, and cool / pump down when idle.
- Sends S6F11 event reports (ProcessStarted, WaferCompleted, ...) and S5F1
  alarm reports (e.g. chamber over-temperature) to the host.

GEM concepts in this file
-------------------------
SVID  Status Variable ID     - live values the host can poll (S1F3) or put in reports
DVID  Data Variable ID       - values only meaningful at the moment of an event (e.g. PPID)
VID   Variable ID            - either of the above; reports are lists of VIDs
ECID  Equipment Constant ID  - settings the host may read/change (S2F13/S2F15)
CEID  Collection Event ID    - things that happen (ProcessStarted ...)
RPTID Report ID              - host-defined list of VIDs (S2F33), linked to CEIDs (S2F35)
ALID  Alarm ID               - abnormal conditions reported with S5F1
PPID  Process Program ID     - the recipe name
"""

from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .codes import (
    ACKC5, ALCD_SET_BIT, ALED_ENABLE, COMMACK, CPACK, DRACK, EAC, ERACK, HCACK, LRACK,
    OFLACK, ONLACK, S9_DESCRIPTIONS, S9_ILLEGAL_DATA, S9_TRANSACTION_TIMEOUT,
    S9_UNRECOGNIZED_DEVICE_ID, S9_UNRECOGNIZED_FUNCTION, S9_UNRECOGNIZED_STREAM, AlarmCategory,
)
from .hsms import (
    ConnectionState, HsmsConnection, HsmsError, HsmsPassiveServer, HsmsTimeouts,
    ReplyTimeoutError,
)
from .message import SecsMessage
from .secs2 import (
    A, B, F4, INTEGER_FORMATS, L, NUMERIC_FORMATS, U1, U4, Format, SecsItem,
)
from .simulation import AMBIENT_TEMP_C, BASE_PRESSURE_MTORR, ChamberModel
from .state_machines import (
    CONTROL_STATE_SV, ControlState, ProcessState,
    make_control_state_machine, make_process_state_machine,
)

log = logging.getLogger("secsgem_sim.equipment")

MDLN = "ETCH-SIM-200"   # equipment model name (S1F2 / S1F14)
SOFTREV = "0.1.0"       # software revision

# --- identifiers -------------------------------------------------------------
SVID_CHAMBER_TEMP = 1001
SVID_CHAMBER_PRESSURE = 1002
SVID_WAFER_COUNT = 1003
SVID_PROCESS_STATE = 1004
SVID_CONTROL_STATE = 1005
SVID_CLOCK = 1006

ECID_TEMP_SETPOINT = 2001
ECID_PRESSURE_SETPOINT = 2002
ECID_OVER_TEMP_LIMIT = 2003
ECID_WAFER_PROCESS_TIME = 2004

CEID_PROCESS_STARTED = 3001
CEID_PROCESS_COMPLETED = 3002
CEID_WAFER_COMPLETED = 3003
CEID_PROCESS_ABORTED = 3004
CEID_PROCESS_PAUSED = 3005
CEID_PROCESS_RESUMED = 3006
CEID_PROCESS_STOPPED = 3007

DVID_PPID = 4001
DVID_CURRENT_WAFER = 4002

ALID_CHAMBER_OVER_TEMP = 1
ALID_CHAMBER_PRESSURE_HIGH = 2
PRESSURE_ALARM_LIMIT = 250.0   # mTorr
ALARM_HYSTERESIS = 2.0         # value must fall this far below the limit to clear

# Recipes the tool knows: PPID -> number of wafers in the (simulated) lot.
RECIPES = {"ETCH_OXIDE_01": 3, "ETCH_POLY_02": 5}

# Allowed parameters (CPNAMEs) per remote command (RCMD).
REMOTE_COMMAND_PARAMS = {
    "PP-SELECT": {"PPID"},
    "START": {"PPID"},
    "STOP": set(),
    "PAUSE": set(),
    "RESUME": set(),
    "ABORT": set(),
}


class DataError(Exception):
    """The message body does not have the structure we expect -> reply S9F7."""


# --- small data model --------------------------------------------------------

@dataclass
class Variable:
    """A status variable (SV) or data variable (DV)."""
    vid: int
    name: str
    units: str
    make_item: Callable[..., SecsItem]   # SECS-II type, e.g. F4 or A
    read: Callable[[], Any]              # returns the current value

    def item(self) -> SecsItem:
        return self.make_item(self.read())


@dataclass
class EquipmentConstant:
    ecid: int
    name: str
    units: str
    minimum: float
    maximum: float
    value: float

    def item(self) -> SecsItem:
        return F4(self.value)


@dataclass
class Alarm:
    alid: int
    name: str
    text: str
    category: AlarmCategory
    enabled: bool = False  # whether S5F1 is sent; the alarm STATE is tracked regardless
    active: bool = False

    @property
    def alcd(self) -> int:
        return (ALCD_SET_BIT if self.active else 0) | int(self.category)

    def report_item(self) -> SecsItem:
        return L(B(self.alcd), U4(self.alid), A(self.text))


# --- body parsing helpers (raise DataError on bad structure) -----------------

def _list(item: Optional[SecsItem], length: Optional[int] = None) -> List[SecsItem]:
    if item is None or item.format is not Format.L:
        raise DataError("expected a list")
    if length is not None and len(item.value) != length:
        raise DataError(f"expected L[{length}], got L[{len(item.value)}]")
    return item.value


def _uint(item: SecsItem) -> int:
    if item.format not in INTEGER_FORMATS or len(item.value) != 1:
        raise DataError(f"expected a single integer, got {item.to_sml()}")
    return item.value[0]


def _ascii(item: SecsItem) -> str:
    if item.format is not Format.A:
        raise DataError(f"expected ASCII, got {item.to_sml()}")
    return item.value


def _flag(item: SecsItem) -> int:
    """One byte from a B, BOOLEAN or U1 item (used for ALED / CEED)."""
    if item.format is Format.B and len(item.value) == 1:
        return item.value[0]
    if item.format in (Format.BOOLEAN, Format.U1) and len(item.value) == 1:
        return int(item.value[0])
    raise DataError(f"expected a one-byte flag, got {item.to_sml()}")


def _clock() -> str:
    """E5 CLOCK format, 16 chars: YYYYMMDDhhmmsscc (cc = centiseconds)."""
    now = time.time()
    return time.strftime("%Y%m%d%H%M%S", time.localtime(now)) + f"{int(now % 1 * 100):02d}"


# --- the equipment -----------------------------------------------------------

class GemEquipment:
    def __init__(
        self,
        address: str = "127.0.0.1",
        port: int = 5000,
        *,
        device_id: int = 0,
        timeouts: Optional[HsmsTimeouts] = None,
        tick: float = 0.2,
        setup_time: float = 0.3,
        seed: Optional[int] = None,
        operator_remote: bool = True,
    ) -> None:
        self.device_id = device_id
        self.tick = tick                  # simulation step in seconds
        self.setup_time = setup_time      # simulated recipe load time
        self.operator_remote = operator_remote  # LOCAL/REMOTE key switch on the tool

        self.chamber = ChamberModel(seed=seed)
        self.control = make_control_state_machine(ControlState.OFFLINE)
        self.process = make_process_state_machine()
        self.communicating = False        # E30 communication state (set by S1F13)

        self.wafer_count = 0
        self.current_ppid = ""
        self.current_wafer = 0

        self.status_variables: Dict[int, Variable] = {v.vid: v for v in [
            Variable(SVID_CHAMBER_TEMP, "ChamberTemp", "degC", F4, lambda: self.chamber.temperature),
            Variable(SVID_CHAMBER_PRESSURE, "ChamberPressure", "mTorr", F4, lambda: self.chamber.pressure),
            Variable(SVID_WAFER_COUNT, "WaferCount", "wafers", U4, lambda: self.wafer_count),
            Variable(SVID_PROCESS_STATE, "ProcessState", "", A, lambda: self.process.state.value),
            Variable(SVID_CONTROL_STATE, "ControlState", "", U1,
                     lambda: CONTROL_STATE_SV[self.control.state]),
            Variable(SVID_CLOCK, "Clock", "", A, _clock),
        ]}
        self.data_variables: Dict[int, Variable] = {v.vid: v for v in [
            Variable(DVID_PPID, "PPID", "", A, lambda: self.current_ppid),
            Variable(DVID_CURRENT_WAFER, "CurrentWafer", "", U4, lambda: self.current_wafer),
        ]}
        self.constants: Dict[int, EquipmentConstant] = {c.ecid: c for c in [
            EquipmentConstant(ECID_TEMP_SETPOINT, "TempSetpoint", "degC", 20.0, 120.0, 60.0),
            EquipmentConstant(ECID_PRESSURE_SETPOINT, "PressureSetpoint", "mTorr", 1.0, 300.0, 50.0),
            EquipmentConstant(ECID_OVER_TEMP_LIMIT, "OverTempLimit", "degC", 30.0, 150.0, 80.0),
            EquipmentConstant(ECID_WAFER_PROCESS_TIME, "WaferProcessTime", "s", 0.1, 60.0, 1.0),
        ]}
        self.events: Dict[int, str] = {
            CEID_PROCESS_STARTED: "ProcessStarted",
            CEID_PROCESS_COMPLETED: "ProcessCompleted",
            CEID_WAFER_COMPLETED: "WaferCompleted",
            CEID_PROCESS_ABORTED: "ProcessAborted",
            CEID_PROCESS_PAUSED: "ProcessPaused",
            CEID_PROCESS_RESUMED: "ProcessResumed",
            CEID_PROCESS_STOPPED: "ProcessStopped",
        }
        self.alarms: Dict[int, Alarm] = {a.alid: a for a in [
            Alarm(ALID_CHAMBER_OVER_TEMP, "ChamberOverTemp",
                  "Chamber temperature above OverTempLimit", AlarmCategory.EQUIPMENT_SAFETY),
            Alarm(ALID_CHAMBER_PRESSURE_HIGH, "ChamberPressureHigh",
                  "Chamber pressure above 250 mTorr", AlarmCategory.PARAMETER_CONTROL_WARNING),
        ]}

        # Host-configured data collection (S2F33 / S2F35 / S2F37):
        self.reports: Dict[int, List[int]] = {}       # RPTID -> [VID, ...]
        self.event_links: Dict[int, List[int]] = {}   # CEID -> [RPTID, ...]
        self.enabled_events: Set[int] = set()
        self._dataid = 0

        self._handlers: Dict[Tuple[int, int], Callable[[SecsMessage], SecsMessage]] = {
            (1, 1): self._s1f1_are_you_there,
            (1, 3): self._s1f3_selected_status,
            (1, 11): self._s1f11_sv_namelist,
            (1, 13): self._s1f13_establish_communications,
            (1, 15): self._s1f15_offline,
            (1, 17): self._s1f17_online,
            (2, 13): self._s2f13_get_constants,
            (2, 15): self._s2f15_set_constants,
            (2, 33): self._s2f33_define_report,
            (2, 35): self._s2f35_link_event_report,
            (2, 37): self._s2f37_enable_events,
            (2, 41): self._s2f41_remote_command,
            (5, 3): self._s5f3_enable_alarm,
            (5, 5): self._s5f5_list_alarms,
        }
        self._remote_commands: Dict[str, Callable[[Dict[str, SecsItem]], Tuple[HCACK, list]]] = {
            "PP-SELECT": self._rcmd_pp_select,
            "START": self._rcmd_start,
            "STOP": self._rcmd_stop,
            "PAUSE": self._rcmd_pause,
            "RESUME": self._rcmd_resume,
            "ABORT": self._rcmd_abort,
        }

        self._server = HsmsPassiveServer(
            address, port, message_handler=self.handle_message, session_id=device_id,
            timeouts=timeouts, on_connection=self._on_connection, name="equipment",
        )
        # Created in start() so they belong to the running event loop.
        self._outbox: Optional[asyncio.Queue] = None
        self._resume: Optional[asyncio.Event] = None
        self._process_task: Optional[asyncio.Task] = None
        self._background: List[asyncio.Task] = []
        self._stop_requested = False

    # ----- lifecycle --------------------------------------------------------

    async def start(self) -> None:
        self._outbox = asyncio.Queue()
        self._resume = asyncio.Event()
        self._resume.set()
        await self._server.start()
        self._background = [
            asyncio.ensure_future(self._simulation_loop()),
            asyncio.ensure_future(self._sender_loop()),
        ]
        log.info("equipment %s ready: control=%s process=%s",
                 MDLN, self.control.state.value, self.process.state.value)

    async def stop(self) -> None:
        for task in [self._process_task, *self._background]:
            if task is not None and not task.done():
                task.cancel()
        await self._server.stop()

    @property
    def port(self) -> int:
        return self._server.port

    @property
    def connection(self) -> Optional[HsmsConnection]:
        return self._server.connection

    def _on_connection(self, conn: HsmsConnection) -> None:
        conn.state_listeners.append(self._on_hsms_state)

    def _on_hsms_state(self, old: ConnectionState, new: ConnectionState) -> None:
        # Losing the HSMS session ends GEM communication; the host must S1F13 again.
        if new is not ConnectionState.SELECTED and self.communicating:
            log.info("communication state: COMMUNICATING -> NOT COMMUNICATING")
            self.communicating = False

    # ----- operator panel (things only a person at the tool can do) ----------

    def set_operator_switch(self, remote: bool) -> None:
        """Turn the LOCAL/REMOTE key switch. The host can never do this."""
        self.operator_remote = remote
        trigger = "operator_remote" if remote else "operator_local"
        if self.control.can(trigger):
            self.control.fire(trigger)

    # ----- incoming messages -------------------------------------------------

    async def handle_message(self, msg: SecsMessage) -> Optional[SecsMessage]:
        """Entry point for every primary message from the host."""
        if msg.session_id != self.device_id:
            await self._send_s9(S9_UNRECOGNIZED_DEVICE_ID, msg)
            return None
        if msg.decode_error:
            await self._send_s9(S9_ILLEGAL_DATA, msg)
            return None

        handler = self._handlers.get((msg.stream, msg.function))
        if handler is None:
            known_streams = {stream for stream, _ in self._handlers}
            error = S9_UNRECOGNIZED_FUNCTION if msg.stream in known_streams else S9_UNRECOGNIZED_STREAM
            await self._send_s9(error, msg)
            return None

        if not self._allowed_in_current_state(msg):
            # E30: refuse with SxF0 ("abort transaction") instead of a normal reply.
            return SecsMessage(msg.stream, 0) if msg.wbit else None

        try:
            return handler(msg)
        except DataError as exc:
            log.warning("illegal data in %s: %s", msg.name, exc)
            await self._send_s9(S9_ILLEGAL_DATA, msg)
            return None

    def _allowed_in_current_state(self, msg: SecsMessage) -> bool:
        sf = (msg.stream, msg.function)
        if not self.communicating:
            if sf != (1, 13):
                log.warning("refusing %s: communication not established (send S1F13 first)", msg.name)
                return False
            return True
        if self.control.state is ControlState.OFFLINE and sf not in ((1, 13), (1, 17)):
            log.warning("refusing %s: equipment is OFFLINE", msg.name)
            return False
        return True

    async def _send_s9(self, function: int, offending: SecsMessage) -> None:
        """S9Fx error: body is the 10-byte header of the message we are complaining about."""
        log.warning("sending S9F%d (%s) about %s", function, S9_DESCRIPTIONS[function], offending.name)
        await self._send_unsolicited(SecsMessage(9, function, False, B(offending.header_bytes())))

    async def _send_unsolicited(self, msg: SecsMessage) -> None:
        conn = self.connection
        if conn is None or not conn.is_selected:
            return
        try:
            await conn.send_request(msg)
        except HsmsError as exc:
            log.warning("could not send %s: %s", msg.name, exc)

    # ----- Stream 1: equipment status ---------------------------------------

    def _s1f1_are_you_there(self, msg: SecsMessage) -> SecsMessage:
        return SecsMessage(1, 2, body=L(A(MDLN), A(SOFTREV)))

    def _s1f13_establish_communications(self, msg: SecsMessage) -> SecsMessage:
        _list(msg.body)  # host sends L[0]
        if not self.communicating:
            log.info("communication state: NOT COMMUNICATING -> COMMUNICATING")
        self.communicating = True
        return SecsMessage(1, 14, body=L(B(COMMACK.ACCEPTED), L(A(MDLN), A(SOFTREV))))

    def _s1f3_selected_status(self, msg: SecsMessage) -> SecsMessage:
        svids = [_uint(i) for i in _list(msg.body)] or list(self.status_variables)  # L[0] = all
        values = []
        for svid in svids:
            variable = self.status_variables.get(svid)
            values.append(variable.item() if variable else L())  # unknown SVID -> empty item
        return SecsMessage(1, 4, body=L(*values))

    def _s1f11_sv_namelist(self, msg: SecsMessage) -> SecsMessage:
        svids = [_uint(i) for i in _list(msg.body)] or list(self.status_variables)
        rows = []
        for svid in svids:
            variable = self.status_variables.get(svid)
            name, units = (variable.name, variable.units) if variable else ("", "")
            rows.append(L(U4(svid), A(name), A(units)))
        return SecsMessage(1, 12, body=L(*rows))

    def _s1f15_offline(self, msg: SecsMessage) -> SecsMessage:
        self.control.fire("offline")
        return SecsMessage(1, 16, body=B(OFLACK.ACK))

    def _s1f17_online(self, msg: SecsMessage) -> SecsMessage:
        if self.control.state is not ControlState.OFFLINE:
            return SecsMessage(1, 18, body=B(ONLACK.ALREADY_ONLINE))
        # Which ONLINE substate we land in is decided by the operator's switch.
        self.control.fire("online_remote" if self.operator_remote else "online_local")
        return SecsMessage(1, 18, body=B(ONLACK.ACCEPTED))

    # ----- Stream 2: equipment constants, reports, remote commands -----------

    def _s2f13_get_constants(self, msg: SecsMessage) -> SecsMessage:
        ecids = [_uint(i) for i in _list(msg.body)] or list(self.constants)
        values = [self.constants[e].item() if e in self.constants else L() for e in ecids]
        return SecsMessage(2, 14, body=L(*values))

    def _s2f15_set_constants(self, msg: SecsMessage) -> SecsMessage:
        changes = []
        for pair in _list(msg.body):
            ecid_item, value_item = _list(pair, 2)
            changes.append((_uint(ecid_item), value_item))

        # Validate everything first: the change is all-or-nothing.
        for ecid, value_item in changes:
            if ecid not in self.constants:
                return SecsMessage(2, 16, body=B(EAC.UNKNOWN_ECID))
            constant = self.constants[ecid]
            if value_item.format not in NUMERIC_FORMATS or len(value_item.value) != 1:
                return SecsMessage(2, 16, body=B(EAC.OUT_OF_RANGE))
            if not constant.minimum <= value_item.value[0] <= constant.maximum:
                return SecsMessage(2, 16, body=B(EAC.OUT_OF_RANGE))

        for ecid, value_item in changes:
            constant = self.constants[ecid]
            value = float(value_item.value[0])
            if value_item.format is Format.F4:
                value = float(f"{value:.7g}")  # drop float32 noise: 0.6000000238 -> 0.6
            log.info("EC %s (%d): %s -> %s", constant.name, ecid, constant.value, value)
            constant.value = value
        return SecsMessage(2, 16, body=B(EAC.OK))

    def _s2f33_define_report(self, msg: SecsMessage) -> SecsMessage:
        _dataid, report_list = _list(msg.body, 2)
        definitions = []
        for entry in _list(report_list):
            rptid_item, vid_list = _list(entry, 2)
            definitions.append((_uint(rptid_item), [_uint(v) for v in _list(vid_list)]))

        if not definitions:  # L[0] means "delete all reports" (and their links)
            self.reports.clear()
            self.event_links.clear()
            return SecsMessage(2, 34, body=B(DRACK.OK))

        known_vids = set(self.status_variables) | set(self.data_variables)
        seen: Set[int] = set()
        for rptid, vids in definitions:
            if vids and (rptid in self.reports or rptid in seen):
                return SecsMessage(2, 34, body=B(DRACK.RPTID_ALREADY_DEFINED))
            if any(v not in known_vids for v in vids):
                return SecsMessage(2, 34, body=B(DRACK.UNKNOWN_VID))
            seen.add(rptid)

        for rptid, vids in definitions:
            if vids:
                self.reports[rptid] = vids
            else:  # empty VID list deletes that report and unlinks it everywhere
                self.reports.pop(rptid, None)
                for ceid in list(self.event_links):
                    self.event_links[ceid] = [r for r in self.event_links[ceid] if r != rptid]
        return SecsMessage(2, 34, body=B(DRACK.OK))

    def _s2f35_link_event_report(self, msg: SecsMessage) -> SecsMessage:
        _dataid, link_list = _list(msg.body, 2)
        links = []
        for entry in _list(link_list):
            ceid_item, rptid_list = _list(entry, 2)
            links.append((_uint(ceid_item), [_uint(r) for r in _list(rptid_list)]))

        for ceid, rptids in links:
            if ceid not in self.events:
                return SecsMessage(2, 36, body=B(LRACK.UNKNOWN_CEID))
            if any(r not in self.reports for r in rptids):
                return SecsMessage(2, 36, body=B(LRACK.UNKNOWN_RPTID))
            if rptids and self.event_links.get(ceid):
                return SecsMessage(2, 36, body=B(LRACK.CEID_ALREADY_LINKED))

        for ceid, rptids in links:
            if rptids:
                self.event_links[ceid] = rptids
            else:  # empty RPTID list unlinks the event
                self.event_links.pop(ceid, None)
        return SecsMessage(2, 36, body=B(LRACK.OK))

    def _s2f37_enable_events(self, msg: SecsMessage) -> SecsMessage:
        ceed_item, ceid_list = _list(msg.body, 2)
        enable = bool(_flag(ceed_item))
        ceids = [_uint(c) for c in _list(ceid_list)] or list(self.events)  # L[0] = all
        if any(c not in self.events for c in ceids):
            return SecsMessage(2, 38, body=B(ERACK.UNKNOWN_CEID))
        for ceid in ceids:
            if enable:
                self.enabled_events.add(ceid)
            else:
                self.enabled_events.discard(ceid)
        return SecsMessage(2, 38, body=B(ERACK.OK))

    def _s2f41_remote_command(self, msg: SecsMessage) -> SecsMessage:
        rcmd_item, param_list = _list(msg.body, 2)
        rcmd = _ascii(rcmd_item).upper()
        params: Dict[str, SecsItem] = {}
        for entry in _list(param_list):
            name_item, value_item = _list(entry, 2)
            params[_ascii(name_item)] = value_item

        def reply(hcack: HCACK, cpacks: Optional[list] = None) -> SecsMessage:
            cp = [L(A(name), B(code)) for name, code in (cpacks or [])]
            log.info("S2F41 %s -> HCACK %d (%s)", rcmd, hcack, hcack.name)
            return SecsMessage(2, 42, body=L(B(hcack), L(*cp)))

        handler = self._remote_commands.get(rcmd)
        if handler is None:
            return reply(HCACK.INVALID_COMMAND)
        if self.control.state is not ControlState.ONLINE_REMOTE:
            return reply(HCACK.CANNOT_PERFORM_NOW)  # host may only command in REMOTE
        unknown = [(n, CPACK.NAME_DOES_NOT_EXIST) for n in params if n not in REMOTE_COMMAND_PARAMS[rcmd]]
        if unknown:
            return reply(HCACK.INVALID_PARAMETER, unknown)
        hcack, cpacks = handler(params)
        return reply(hcack, cpacks)

    # ----- remote command implementations -----------------------------------
    # Each returns (HCACK, [(CPNAME, CPACK), ...]).

    @staticmethod
    def _parse_ppid(params: Dict[str, SecsItem]) -> Tuple[Optional[str], list]:
        item = params.get("PPID")
        if item is None:
            return None, []
        if item.format is not Format.A:
            return None, [("PPID", CPACK.ILLEGAL_FORMAT)]
        if item.value not in RECIPES:
            return None, [("PPID", CPACK.ILLEGAL_VALUE)]
        return item.value, []

    def _rcmd_pp_select(self, params: Dict[str, SecsItem]) -> Tuple[HCACK, list]:
        if self.process.state is not ProcessState.IDLE:
            return HCACK.CANNOT_PERFORM_NOW, []
        ppid, cpacks = self._parse_ppid(params)
        if ppid is None:
            return HCACK.INVALID_PARAMETER, cpacks
        self.current_ppid = ppid
        self.process.fire("select")
        self._begin(self._finish_setup())
        return HCACK.ACK_FINISH_LATER, []

    def _rcmd_start(self, params: Dict[str, SecsItem]) -> Tuple[HCACK, list]:
        state = self.process.state
        if state in (ProcessState.EXECUTING, ProcessState.PAUSED):
            return HCACK.ALREADY_IN_CONDITION, []
        if state is ProcessState.SETUP:
            return HCACK.CANNOT_PERFORM_NOW, []
        ppid, cpacks = self._parse_ppid(params)
        if cpacks:
            return HCACK.INVALID_PARAMETER, cpacks

        if state is ProcessState.IDLE:
            # START from IDLE = select the recipe and run it in one go.
            if ppid is None:
                return HCACK.INVALID_PARAMETER, []
            self.current_ppid = ppid
            self.process.fire("select")
            self._begin(self._setup_then_execute())
        else:  # READY: run the recipe chosen by PP-SELECT
            if ppid is not None and ppid != self.current_ppid:
                return HCACK.INVALID_PARAMETER, [("PPID", CPACK.ILLEGAL_VALUE)]
            self.process.fire("start")
            self._begin(self._execute())
        # Transitions happen synchronously above so a second command sent right
        # away sees the new state. Completion is reported later by S6F11.
        return HCACK.ACK_FINISH_LATER, []

    def _rcmd_stop(self, params: Dict[str, SecsItem]) -> Tuple[HCACK, list]:
        if self.process.state is not ProcessState.EXECUTING:
            return HCACK.CANNOT_PERFORM_NOW, []
        self._stop_requested = True   # finish the current wafer, then stop
        return HCACK.ACK_FINISH_LATER, []

    def _rcmd_pause(self, params: Dict[str, SecsItem]) -> Tuple[HCACK, list]:
        if self.process.state is ProcessState.PAUSED:
            return HCACK.ALREADY_IN_CONDITION, []
        if self.process.state is not ProcessState.EXECUTING:
            return HCACK.CANNOT_PERFORM_NOW, []
        self.process.fire("pause")
        self._resume.clear()
        self.emit_event(CEID_PROCESS_PAUSED)
        return HCACK.OK, []

    def _rcmd_resume(self, params: Dict[str, SecsItem]) -> Tuple[HCACK, list]:
        if self.process.state is ProcessState.EXECUTING:
            return HCACK.ALREADY_IN_CONDITION, []
        if self.process.state is not ProcessState.PAUSED:
            return HCACK.CANNOT_PERFORM_NOW, []
        self.process.fire("resume")
        self._resume.set()
        self.emit_event(CEID_PROCESS_RESUMED)
        return HCACK.OK, []

    def _rcmd_abort(self, params: Dict[str, SecsItem]) -> Tuple[HCACK, list]:
        if not self.process.can("abort"):
            return HCACK.CANNOT_PERFORM_NOW, []
        if self._process_task is not None and not self._process_task.done():
            self._process_task.cancel()
        self.process.fire("abort")
        self._resume.set()
        self._stop_requested = False
        self.emit_event(CEID_PROCESS_ABORTED)
        return HCACK.OK, []

    # ----- process execution (background task) -------------------------------

    def _begin(self, coro) -> None:
        self._process_task = asyncio.ensure_future(coro)

    async def _finish_setup(self) -> None:
        await asyncio.sleep(self.setup_time)
        self.process.fire("setup_done")

    async def _setup_then_execute(self) -> None:
        await self._finish_setup()
        self.process.fire("start")
        await self._execute()

    async def _execute(self) -> None:
        """Process every wafer of the recipe. Cancelled by ABORT."""
        self._stop_requested = False
        self.emit_event(CEID_PROCESS_STARTED)
        total = RECIPES[self.current_ppid]
        for wafer in range(1, total + 1):
            self.current_wafer = wafer
            await self._process_one_wafer()
            self.wafer_count += 1
            self.emit_event(CEID_WAFER_COMPLETED)
            if self._stop_requested and wafer < total:
                self.process.fire("complete")
                self.emit_event(CEID_PROCESS_STOPPED)
                return
        self.process.fire("complete")
        self.emit_event(CEID_PROCESS_COMPLETED)

    async def _process_one_wafer(self) -> None:
        remaining = self.constants[ECID_WAFER_PROCESS_TIME].value
        while remaining > 0:
            await self._resume.wait()   # blocks here while PAUSED
            step = min(self.tick, remaining)
            await asyncio.sleep(step)
            remaining -= step

    # ----- sensor simulation + alarms ----------------------------------------

    async def _simulation_loop(self) -> None:
        while True:
            await asyncio.sleep(self.tick)
            self.simulate_step(self.tick)

    def simulate_step(self, dt: float) -> None:
        processing = self.process.state in (ProcessState.EXECUTING, ProcessState.PAUSED)
        if processing:  # heater + process gas on
            temp_target = self.constants[ECID_TEMP_SETPOINT].value
            pressure_target = self.constants[ECID_PRESSURE_SETPOINT].value
        else:           # cool to ambient, pump to base pressure
            temp_target, pressure_target = AMBIENT_TEMP_C, BASE_PRESSURE_MTORR
        self.chamber.step(dt, temp_target, pressure_target)

        limit = self.constants[ECID_OVER_TEMP_LIMIT].value
        temp = self.chamber.temperature
        self._update_alarm(ALID_CHAMBER_OVER_TEMP, temp > limit, temp < limit - ALARM_HYSTERESIS)
        pressure = self.chamber.pressure
        self._update_alarm(ALID_CHAMBER_PRESSURE_HIGH, pressure > PRESSURE_ALARM_LIMIT,
                           pressure < PRESSURE_ALARM_LIMIT - 10 * ALARM_HYSTERESIS)

    def _update_alarm(self, alid: int, should_set: bool, should_clear: bool) -> None:
        alarm = self.alarms[alid]
        if not alarm.active and should_set:
            self._change_alarm(alarm, True)
        elif alarm.active and should_clear:
            self._change_alarm(alarm, False)

    def _change_alarm(self, alarm: Alarm, active: bool) -> None:
        alarm.active = active
        log.warning("alarm %s (%d) %s", alarm.name, alarm.alid, "SET" if active else "CLEARED")
        if alarm.enabled and self._can_report():
            self._queue(SecsMessage(5, 1, True, alarm.report_item()))

    # ----- Stream 5: alarm management ---------------------------------------

    def _s5f3_enable_alarm(self, msg: SecsMessage) -> SecsMessage:
        aled_item, alid_item = _list(msg.body, 2)
        enable = bool(_flag(aled_item) & ALED_ENABLE)
        if alid_item.format not in INTEGER_FORMATS:
            raise DataError("ALID must be an integer")
        alids = list(alid_item.value) or list(self.alarms)  # zero-length ALID = all alarms
        if any(a not in self.alarms for a in alids):
            return SecsMessage(5, 4, body=B(ACKC5.ERROR))
        for alid in alids:
            self.alarms[alid].enabled = enable
        return SecsMessage(5, 4, body=B(ACKC5.OK))

    def _s5f5_list_alarms(self, msg: SecsMessage) -> SecsMessage:
        if msg.body is None or msg.body.format not in INTEGER_FORMATS:
            raise DataError("S5F5 body must be an integer ALID vector")
        alids = list(msg.body.value) or list(self.alarms)
        rows = [self.alarms[a].report_item() if a in self.alarms else L(B(), U4(a), A())
                for a in alids]
        return SecsMessage(5, 6, body=L(*rows))

    # ----- Stream 6: event reports -------------------------------------------

    def _can_report(self) -> bool:
        conn = self.connection
        return (conn is not None and conn.is_selected and self.communicating
                and self.control.state is not ControlState.OFFLINE)

    def emit_event(self, ceid: int) -> None:
        """Collection event happened: build S6F11 NOW (values at event time) and queue it."""
        if ceid not in self.enabled_events or not self._can_report():
            log.info("event %s (%d) occurred (not reported)", self.events[ceid], ceid)
            return
        all_variables = {**self.status_variables, **self.data_variables}
        reports = []
        for rptid in self.event_links.get(ceid, []):
            values = [all_variables[vid].item() for vid in self.reports[rptid]]
            reports.append(L(U4(rptid), L(*values)))
        self._dataid += 1
        log.info("event %s (%d) -> S6F11 with %d report(s)", self.events[ceid], ceid, len(reports))
        self._queue(SecsMessage(6, 11, True, L(U4(self._dataid), U4(ceid), L(*reports))))

    def _queue(self, msg: SecsMessage) -> None:
        self._outbox.put_nowait(msg)

    async def _sender_loop(self) -> None:
        """Send queued S6F11/S5F1 one at a time, in order, waiting for each reply."""
        while True:
            msg = await self._outbox.get()
            conn = self.connection
            if conn is None or not conn.is_selected:
                log.warning("dropping %s: not connected (spooling not implemented)", msg.name)
                continue
            try:
                await conn.send_request(msg)
            except ReplyTimeoutError as exc:
                # E5: on T3 expiry the equipment sends S9F9 quoting the header (SHEAD).
                await self._send_unsolicited(
                    SecsMessage(9, S9_TRANSACTION_TIMEOUT, False, B(exc.message.header_bytes())))
            except HsmsError as exc:  # connection closed, not selected, rejected...
                log.warning("could not send %s: %s", msg.name, exc)
