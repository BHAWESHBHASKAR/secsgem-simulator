"""Shared test helpers: start equipment + host on an ephemeral localhost port."""

from contextlib import asynccontextmanager

from secsgem_sim.equipment import GemEquipment
from secsgem_sim.hsms import HsmsTimeouts

TEST_TIMEOUTS = HsmsTimeouts(t3=2.0, t5=0.1, t6=1.0, t7=2.0, t8=1.0, linktest=None)


@asynccontextmanager
async def running_pair(online_remote=True, **equipment_kwargs):
    """Yield (equipment, host) connected and SELECTED.

    online_remote=True also does S1F13 + S1F17 so remote commands are allowed.
    Simulation runs fast (50 ms ticks, 0.2 s per wafer) to keep tests quick.
    """
    from secsgem_sim.equipment import ECID_WAFER_PROCESS_TIME
    from secsgem_sim.host import GemHost

    kwargs = dict(port=0, tick=0.05, setup_time=0.05, seed=42, timeouts=TEST_TIMEOUTS)
    kwargs.update(equipment_kwargs)
    equipment = GemEquipment(**kwargs)
    equipment.constants[ECID_WAFER_PROCESS_TIME].value = 0.2
    await equipment.start()
    host = GemHost(port=equipment.port, timeouts=TEST_TIMEOUTS)
    await host.connect()
    try:
        if online_remote:
            assert await host.establish_communications() == 0
            assert await host.request_online() == 0
        yield equipment, host
    finally:
        await host.close()
        await equipment.stop()
