"""CLI parsing and the full scripted demo, run in-process."""

from secsgem_sim.cli import build_parser
from secsgem_sim.demo import run_demo
from secsgem_sim.equipment import GemEquipment
from secsgem_sim.host import GemHost

from tests.helpers import TEST_TIMEOUTS


def test_parser_roles_and_defaults():
    args = build_parser().parse_args(["equipment", "--port", "5555", "--local"])
    assert args.role == "equipment" and args.port == 5555 and args.local
    args = build_parser().parse_args(["host", "--demo"])
    assert args.role == "host" and args.demo and args.port == 5000 and args.t3 == 45.0


async def test_full_demo_scenario_runs_against_equipment():
    equipment = GemEquipment(port=0, tick=0.1, setup_time=0.1, seed=7, timeouts=TEST_TIMEOUTS)
    await equipment.start()
    host = GemHost(port=equipment.port, timeouts=TEST_TIMEOUTS)
    try:
        await run_demo(host)
    finally:
        await equipment.stop()
    ceids = [e.ceid for e in host.events_received]
    assert 3002 in ceids and 3004 in ceids               # completed one lot, aborted another
    assert [a.is_set for a in host.alarms_received] == [True, False]
    assert equipment.wafer_count >= 3
