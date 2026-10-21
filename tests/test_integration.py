"""End-to-end tests: real equipment + real host talking HSMS over localhost."""

import asyncio

import pytest

from secsgem_sim.codes import DRACK, EAC, ERACK, HCACK, LRACK, ONLACK, CPACK
from secsgem_sim.equipment import (
    ALID_CHAMBER_OVER_TEMP, CEID_PROCESS_ABORTED, CEID_PROCESS_COMPLETED, CEID_PROCESS_PAUSED,
    CEID_PROCESS_STARTED, CEID_PROCESS_STOPPED, CEID_WAFER_COMPLETED, DVID_CURRENT_WAFER,
    DVID_PPID, ECID_OVER_TEMP_LIMIT, ECID_TEMP_SETPOINT, MDLN, SVID_CHAMBER_TEMP,
    SVID_CONTROL_STATE, SVID_PROCESS_STATE, SVID_WAFER_COUNT,
)
from secsgem_sim.host import S9Error, TransactionAbortedError
from secsgem_sim.message import SecsMessage
from secsgem_sim.secs2 import A, L, U4
from secsgem_sim.state_machines import ControlState, ProcessState

from tests.helpers import running_pair


async def setup_reporting(host):
    assert await host.define_reports({
        100: [SVID_PROCESS_STATE, DVID_PPID],
        101: [SVID_WAFER_COUNT, DVID_CURRENT_WAFER, SVID_CHAMBER_TEMP],
    }) == DRACK.OK
    assert await host.link_event_reports({
        CEID_PROCESS_STARTED: [100],
        CEID_WAFER_COMPLETED: [101],
        CEID_PROCESS_COMPLETED: [100, 101],
    }) == LRACK.OK
    assert await host.enable_events() == ERACK.OK


# --- the headline scenario -----------------------------------------------

async def test_select_s1f13_remote_start_event_report_linktest():
    async with running_pair(online_remote=False) as (equipment, host):
        assert host.connection.is_selected
        assert await host.establish_communications() == 0
        assert await host.request_online() == ONLACK.ACCEPTED
        assert equipment.control.state is ControlState.ONLINE_REMOTE
        await setup_reporting(host)

        started = host.expect_event(CEID_PROCESS_STARTED)
        completed = host.expect_event(CEID_PROCESS_COMPLETED)
        result = await host.remote_command("START", {"PPID": "ETCH_OXIDE_01"})
        assert result.hcack is HCACK.ACK_FINISH_LATER

        report = await asyncio.wait_for(started, 2)
        assert report.reports[100] == ["EXECUTING", "ETCH_OXIDE_01"]
        done = await asyncio.wait_for(completed, 5)
        assert done.reports[101][0] == 3          # WaferCount after a 3-wafer lot
        wafer_events = [e for e in host.events_received if e.ceid == CEID_WAFER_COMPLETED]
        assert [e.reports[101][1] for e in wafer_events] == [1, 2, 3]
        assert equipment.process.state is ProcessState.IDLE

        assert await host.linktest() < 1.0


async def test_are_you_there_and_status_variables():
    async with running_pair() as (equipment, host):
        mdln, softrev = await host.are_you_there()
        assert mdln == MDLN
        state, control, unknown = await host.get_status_variables(
            [SVID_PROCESS_STATE, SVID_CONTROL_STATE, 9999])
        assert state == "IDLE" and control == 5 and unknown == []
        everything = await host.get_status_variables()   # empty list = all SVIDs
        assert len(everything) == len(equipment.status_variables)


async def test_sv_namelist():
    async with running_pair() as (_, host):
        rows = await host.get_sv_namelist([SVID_CHAMBER_TEMP, 9999])
        assert rows == [(SVID_CHAMBER_TEMP, "ChamberTemp", "degC"), (9999, "", "")]


# --- communication / control state rejections ----------------------------

async def test_messages_refused_before_s1f13():
    async with running_pair(online_remote=False) as (_, host):
        with pytest.raises(TransactionAbortedError):
            await host.are_you_there()           # S1F1 -> S1F0


async def test_messages_refused_while_offline():
    async with running_pair() as (equipment, host):
        assert await host.request_offline() == 0
        assert equipment.control.state is ControlState.OFFLINE
        with pytest.raises(TransactionAbortedError):
            await host.get_status_variables()   # S1F3 -> S1F0
        assert await host.request_online() == ONLACK.ACCEPTED


async def test_online_when_already_online():
    async with running_pair() as (_, host):
        assert await host.request_online() == ONLACK.ALREADY_ONLINE


async def test_remote_command_rejected_in_local():
    async with running_pair(online_remote=False, operator_remote=False) as (equipment, host):
        await host.establish_communications()
        await host.request_online()
        assert equipment.control.state is ControlState.ONLINE_LOCAL
        result = await host.remote_command("START", {"PPID": "ETCH_OXIDE_01"})
        assert result.hcack is HCACK.CANNOT_PERFORM_NOW
        equipment.set_operator_switch(remote=True)   # operator turns the key
        result = await host.remote_command("START", {"PPID": "ETCH_OXIDE_01"})
        assert result.hcack is HCACK.ACK_FINISH_LATER


# --- process state machine via remote commands ---------------------------

async def test_unknown_remote_command():
    async with running_pair() as (_, host):
        assert (await host.remote_command("FLY")).hcack is HCACK.INVALID_COMMAND


@pytest.mark.parametrize("rcmd", ["PAUSE", "RESUME", "ABORT", "STOP"])
async def test_commands_rejected_when_idle(rcmd):
    async with running_pair() as (_, host):
        assert (await host.remote_command(rcmd)).hcack is HCACK.CANNOT_PERFORM_NOW


async def test_start_with_bad_parameters():
    async with running_pair() as (_, host):
        r = await host.remote_command("START", {"PPID": "NO_SUCH_RECIPE"})
        assert r.hcack is HCACK.INVALID_PARAMETER and r.cpacks == [("PPID", CPACK.ILLEGAL_VALUE)]
        r = await host.remote_command("START", {"SPEED": 3})
        assert r.cpacks == [("SPEED", CPACK.NAME_DOES_NOT_EXIST)]
        r = await host.remote_command("START")
        assert r.hcack is HCACK.INVALID_PARAMETER   # PPID required from IDLE


async def test_start_twice_is_already_in_condition():
    async with running_pair() as (_, host):
        await host.remote_command("START", {"PPID": "ETCH_OXIDE_01"})
        second = await host.remote_command("START", {"PPID": "ETCH_OXIDE_01"})
        assert second.hcack in (HCACK.CANNOT_PERFORM_NOW, HCACK.ALREADY_IN_CONDITION)


async def test_pp_select_then_start():
    async with running_pair() as (equipment, host):
        assert (await host.remote_command("PP-SELECT", {"PPID": "ETCH_POLY_02"})).hcack \
            is HCACK.ACK_FINISH_LATER
        assert equipment.process.state is ProcessState.SETUP
        await asyncio.sleep(0.2)
        assert equipment.process.state is ProcessState.READY
        assert (await host.remote_command("START")).hcack is HCACK.ACK_FINISH_LATER
        assert equipment.process.state is ProcessState.EXECUTING


async def test_pause_resume_and_abort():
    async with running_pair() as (equipment, host):
        await setup_reporting(host)
        await host.link_event_reports({CEID_PROCESS_ABORTED: [100]})
        started = host.expect_event(CEID_PROCESS_STARTED)
        await host.remote_command("START", {"PPID": "ETCH_POLY_02"})
        await asyncio.wait_for(started, 2)

        assert (await host.remote_command("PAUSE")).hcack is HCACK.OK
        assert (await host.remote_command("PAUSE")).hcack is HCACK.ALREADY_IN_CONDITION
        count = equipment.wafer_count
        await asyncio.sleep(0.4)        # longer than one wafer: nothing should finish
        assert equipment.wafer_count == count
        assert (await host.remote_command("RESUME")).hcack is HCACK.OK

        aborted = host.expect_event(CEID_PROCESS_ABORTED)
        assert (await host.remote_command("ABORT")).hcack is HCACK.OK
        report = await asyncio.wait_for(aborted, 2)
        assert report.reports[100][0] == "IDLE"
        assert equipment.process.state is ProcessState.IDLE


async def test_stop_finishes_current_wafer():
    async with running_pair() as (equipment, host):
        await host.enable_events()
        stopped = host.expect_event(CEID_PROCESS_STOPPED)
        await host.remote_command("START", {"PPID": "ETCH_POLY_02"})  # 5 wafers
        await asyncio.sleep(0.15)
        assert (await host.remote_command("STOP")).hcack is HCACK.ACK_FINISH_LATER
        await asyncio.wait_for(stopped, 2)
        assert equipment.wafer_count == 1


async def test_disabled_events_are_not_sent():
    async with running_pair() as (equipment, host):
        await setup_reporting(host)
        await host.enable_events([CEID_PROCESS_STARTED], enable=False)
        completed = host.expect_event(CEID_PROCESS_COMPLETED)
        await host.remote_command("START", {"PPID": "ETCH_OXIDE_01"})
        await asyncio.wait_for(completed, 5)
        assert CEID_PROCESS_STARTED not in [e.ceid for e in host.events_received]


# --- equipment constants ---------------------------------------------------

async def test_equipment_constants_read_write_and_validation():
    async with running_pair() as (equipment, host):
        assert await host.get_equipment_constants([ECID_TEMP_SETPOINT, 9999]) == [60.0, []]
        assert await host.set_equipment_constants({ECID_TEMP_SETPOINT: 70.0}) == EAC.OK
        assert await host.get_equipment_constants([ECID_TEMP_SETPOINT]) == [70.0]
        assert await host.set_equipment_constants({ECID_TEMP_SETPOINT: 500.0}) == EAC.OUT_OF_RANGE
        assert await host.set_equipment_constants({9999: 1.0}) == EAC.UNKNOWN_ECID
        # all-or-nothing: one bad value means nothing changes
        assert await host.set_equipment_constants(
            {ECID_TEMP_SETPOINT: 65.0, ECID_OVER_TEMP_LIMIT: 1.0}) == EAC.OUT_OF_RANGE
        assert equipment.constants[ECID_TEMP_SETPOINT].value == 70.0


# --- report setup rejections ---------------------------------------------

async def test_report_definition_errors():
    async with running_pair() as (equipment, host):
        assert await host.define_reports({1: [9999]}) == DRACK.UNKNOWN_VID
        assert await host.define_reports({1: [SVID_CHAMBER_TEMP]}) == DRACK.OK
        assert await host.define_reports({1: [SVID_CHAMBER_TEMP]}) == DRACK.RPTID_ALREADY_DEFINED
        assert await host.link_event_reports({9999: [1]}) == LRACK.UNKNOWN_CEID
        assert await host.link_event_reports({CEID_PROCESS_STARTED: [77]}) == LRACK.UNKNOWN_RPTID
        assert await host.link_event_reports({CEID_PROCESS_STARTED: [1]}) == LRACK.OK
        assert await host.link_event_reports({CEID_PROCESS_STARTED: [1]}) == LRACK.CEID_ALREADY_LINKED
        assert await host.enable_events([9999]) == ERACK.UNKNOWN_CEID
        assert await host.define_reports({}) == DRACK.OK   # L[0] deletes all
        assert equipment.reports == {} and equipment.event_links == {}


# --- alarms ----------------------------------------------------------------

async def test_over_temperature_alarm_set_and_clear():
    async with running_pair() as (equipment, host):
        assert await host.enable_alarms() == 0
        listed = await host.list_alarms()
        assert {a.alid for a in listed} == set(equipment.alarms)
        assert not any(a.is_set for a in listed)

        await host.set_equipment_constants({ECID_TEMP_SETPOINT: 95.0, ECID_OVER_TEMP_LIMIT: 40.0})
        alarm_set = host.expect_alarm(ALID_CHAMBER_OVER_TEMP, is_set=True)
        await host.remote_command("START", {"PPID": "ETCH_POLY_02"})
        alarm = await asyncio.wait_for(alarm_set, 3)
        assert alarm.is_set and alarm.category == 2

        assert (await host.list_alarms([ALID_CHAMBER_OVER_TEMP]))[0].is_set
        alarm_clear = host.expect_alarm(ALID_CHAMBER_OVER_TEMP, is_set=False)
        await host.remote_command("ABORT")        # chamber cools towards ambient
        assert not (await asyncio.wait_for(alarm_clear, 5)).is_set


async def test_disabled_alarm_is_not_reported():
    async with running_pair() as (equipment, host):
        await host.set_equipment_constants({ECID_TEMP_SETPOINT: 95.0, ECID_OVER_TEMP_LIMIT: 40.0})
        await host.remote_command("START", {"PPID": "ETCH_POLY_02"})
        await asyncio.sleep(1.0)
        assert equipment.alarms[ALID_CHAMBER_OVER_TEMP].active  # state still tracked
        assert host.alarms_received == []


# --- S9 error messages -----------------------------------------------------

async def test_unknown_function_gets_s9f5():
    async with running_pair() as (_, host):
        with pytest.raises(S9Error) as info:
            await host.send(SecsMessage(1, 99, True))
        assert info.value.function == 5


async def test_unknown_stream_gets_s9f3():
    async with running_pair() as (_, host):
        with pytest.raises(S9Error) as info:
            await host.send(SecsMessage(64, 1, True))
        assert info.value.function == 3


async def test_bad_structure_gets_s9f7():
    async with running_pair() as (_, host):
        with pytest.raises(S9Error) as info:
            await host.send(SecsMessage(2, 41, True, A("START")))   # should be L[2]
        assert info.value.function == 7


async def test_wrong_device_id_gets_s9f1():
    async with running_pair() as (_, host):
        host.connection.session_id = 7
        with pytest.raises(S9Error) as info:
            await host.send(SecsMessage(1, 1, True))
        assert info.value.function == 1


async def test_equipment_sends_s9f9_when_host_does_not_ack_event():
    async with running_pair() as (equipment, host):
        await host.enable_events()
        equipment.connection.timeouts.t3 = 0.2
        real_handler = host.connection.message_handler

        async def ignore_event_reports(msg):   # host "forgets" to answer S6F11
            if (msg.stream, msg.function) == (6, 11):
                return None
            return await real_handler(msg)

        host.connection.message_handler = ignore_event_reports
        await host.remote_command("START", {"PPID": "ETCH_OXIDE_01"})
        await asyncio.sleep(0.5)
        assert 9 in host.s9_received   # equipment's T3 expired -> S9F9
