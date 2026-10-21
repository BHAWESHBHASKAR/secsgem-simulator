"""Scripted host-side scenario that exercises the whole stack.

Run the equipment in one terminal and this in another:
    python -m secsgem_sim equipment --port 5000
    python -m secsgem_sim host --port 5000 --demo
"""

from __future__ import annotations

import asyncio
import logging

from .equipment import (
    ALID_CHAMBER_OVER_TEMP, CEID_PROCESS_ABORTED, CEID_PROCESS_COMPLETED, CEID_PROCESS_PAUSED,
    CEID_PROCESS_RESUMED, CEID_PROCESS_STARTED, CEID_WAFER_COMPLETED, DVID_CURRENT_WAFER,
    DVID_PPID, ECID_TEMP_SETPOINT, ECID_WAFER_PROCESS_TIME, SVID_CHAMBER_PRESSURE,
    SVID_CHAMBER_TEMP, SVID_PROCESS_STATE, SVID_WAFER_COUNT,
)
from .host import AlarmReport, EventReport, GemHost, S9Error
from .message import SecsMessage

log = logging.getLogger("secsgem_sim.demo")

# What the "MES" knows about this tool type (normally from a tool model / config DB).
EVENT_NAMES = {
    CEID_PROCESS_STARTED: "ProcessStarted", CEID_PROCESS_COMPLETED: "ProcessCompleted",
    CEID_WAFER_COMPLETED: "WaferCompleted", CEID_PROCESS_ABORTED: "ProcessAborted",
    CEID_PROCESS_PAUSED: "ProcessPaused", CEID_PROCESS_RESUMED: "ProcessResumed",
}
RPT_LOT = 100     # report: who/what is running
RPT_WAFER = 101   # report: per-wafer process data
REPORTS = {
    RPT_LOT: [SVID_PROCESS_STATE, DVID_PPID],
    RPT_WAFER: [DVID_CURRENT_WAFER, SVID_WAFER_COUNT, SVID_CHAMBER_TEMP, SVID_CHAMBER_PRESSURE],
}
VID_NAMES = {SVID_PROCESS_STATE: "ProcessState", DVID_PPID: "PPID",
             DVID_CURRENT_WAFER: "CurrentWafer", SVID_WAFER_COUNT: "WaferCount",
             SVID_CHAMBER_TEMP: "ChamberTemp", SVID_CHAMBER_PRESSURE: "ChamberPressure"}


def step(text: str) -> None:
    log.info("=" * 12 + " " + text + " " + "=" * 12)


def describe_event(report: EventReport) -> None:
    parts = []
    for rptid, values in report.reports.items():
        named = ", ".join(f"{VID_NAMES.get(v, v)}={_fmt(x)}" for v, x in zip(REPORTS.get(rptid, []), values))
        parts.append(f"RPT {rptid}: {named}")
    log.info("EVENT  %s (CEID %d)  %s", EVENT_NAMES.get(report.ceid, "?"), report.ceid, " | ".join(parts))


def describe_alarm(alarm: AlarmReport) -> None:
    log.warning("ALARM  ALID %d %s: %s", alarm.alid, "SET" if alarm.is_set else "CLEARED", alarm.text)


def _fmt(value) -> str:
    return f"{value:.1f}" if isinstance(value, float) else str(value)


async def run_demo(host: GemHost) -> None:
    host.on_event(describe_event)
    host.on_alarm(describe_alarm)

    step("1. Connect: TCP + HSMS Select")
    await host.connect()

    step("2. Establish communications (S1F13)")
    log.info("COMMACK = %d", await host.establish_communications())

    step("3. Request ONLINE (S1F17), then Are You There (S1F1)")
    # Order matters: while OFFLINE the tool answers S1F1 with S1F0.
    log.info("ONLACK = %d", await host.request_online())
    mdln, softrev = await host.are_you_there()
    log.info("tool model %s, software %s", mdln, softrev)

    step("4. Read status variables (S1F11 names, S1F3 values)")
    names = await host.get_sv_namelist()
    values = await host.get_status_variables([svid for svid, _, _ in names])
    for (svid, name, units), value in zip(names, values):
        log.info("  SV %d %-16s = %s %s", svid, name, _fmt(value), units)

    step("5. Equipment constants (S2F13 read, S2F15 write)")
    log.info("EC values: %s", await host.get_equipment_constants())
    log.info("EAC = %d", await host.set_equipment_constants({ECID_WAFER_PROCESS_TIME: 0.6}))

    step("6. Configure data collection (S2F33 define, S2F35 link, S2F37 enable)")
    log.info("DRACK = %d", await host.define_reports(REPORTS))
    log.info("LRACK = %d", await host.link_event_reports({
        CEID_PROCESS_STARTED: [RPT_LOT], CEID_WAFER_COMPLETED: [RPT_WAFER],
        CEID_PROCESS_COMPLETED: [RPT_LOT, RPT_WAFER], CEID_PROCESS_ABORTED: [RPT_LOT, RPT_WAFER],
        CEID_PROCESS_PAUSED: [RPT_LOT], CEID_PROCESS_RESUMED: [RPT_LOT],
    }))
    log.info("ERACK = %d", await host.enable_events())

    step("7. Enable and list alarms (S5F3, S5F5)")
    log.info("ACKC5 = %d", await host.enable_alarms())
    for alarm in await host.list_alarms():
        log.info("  ALID %d set=%s: %s", alarm.alid, alarm.is_set, alarm.text)

    step("8. Rejection: RESUME while IDLE (expect HCACK 2)")
    result = await host.remote_command("RESUME")
    log.info("HCACK = %d (%s)", result.hcack, result.hcack.name)

    step("9. Run recipe ETCH_OXIDE_01 with a PAUSE/RESUME in the middle")
    first_wafer = host.expect_event(CEID_WAFER_COMPLETED)
    completed = host.expect_event(CEID_PROCESS_COMPLETED)
    result = await host.remote_command("START", {"PPID": "ETCH_OXIDE_01"})
    log.info("HCACK = %d (%s)", result.hcack, result.hcack.name)
    await asyncio.wait_for(first_wafer, 10)
    log.info("PAUSE -> HCACK %d", (await host.remote_command("PAUSE")).hcack)
    log.info("ProcessState while paused: %s", (await host.get_status_variables([SVID_PROCESS_STATE]))[0])
    await asyncio.sleep(0.5)
    log.info("RESUME -> HCACK %d", (await host.remote_command("RESUME")).hcack)
    await asyncio.wait_for(completed, 15)

    step("10. Over-temperature: bad setpoint, alarm, host ABORTs")
    await host.set_equipment_constants({ECID_TEMP_SETPOINT: 95.0})   # above OverTempLimit 80
    alarm_set = host.expect_alarm(ALID_CHAMBER_OVER_TEMP, is_set=True)
    alarm_cleared = host.expect_alarm(ALID_CHAMBER_OVER_TEMP, is_set=False)
    aborted = host.expect_event(CEID_PROCESS_ABORTED)
    await host.remote_command("START", {"PPID": "ETCH_POLY_02"})
    await asyncio.wait_for(alarm_set, 15)
    log.info("host policy: over-temp alarm -> ABORT")
    result = await host.remote_command("ABORT")
    log.info("HCACK = %d (%s)", result.hcack, result.hcack.name)
    await asyncio.wait_for(aborted, 5)
    await asyncio.wait_for(alarm_cleared, 15)
    await host.set_equipment_constants({ECID_TEMP_SETPOINT: 60.0})

    step("11. Unknown message S1F99 -> equipment answers S9F5")
    try:
        await host.send(SecsMessage(1, 99, True))
    except S9Error as exc:
        log.info("got %s", exc)

    step("12. Linktest, go OFFLINE (S1F15), Separate")
    log.info("linktest round trip %.1f ms", await host.linktest() * 1000)
    log.info("OFLACK = %d", await host.request_offline())
    await host.close()
    stats = host.connection.stats
    log.info("done: %d HSMS messages sent, %d received, %d event reports, %d alarm reports",
             stats["sent"], stats["received"], len(host.events_received), len(host.alarms_received))
