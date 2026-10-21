import pytest

from secsgem_sim.simulation import ChamberModel
from secsgem_sim.state_machines import (
    ControlState, InvalidTransition, ProcessState,
    make_control_state_machine, make_process_state_machine,
)


def test_process_happy_path():
    sm = make_process_state_machine()
    for trigger, expected in [("select", ProcessState.SETUP), ("setup_done", ProcessState.READY),
                              ("start", ProcessState.EXECUTING), ("pause", ProcessState.PAUSED),
                              ("resume", ProcessState.EXECUTING), ("complete", ProcessState.IDLE)]:
        assert sm.fire(trigger) is expected


@pytest.mark.parametrize("trigger", ["start", "pause", "resume", "complete", "abort"])
def test_process_invalid_from_idle(trigger):
    sm = make_process_state_machine()
    assert not sm.can(trigger)
    with pytest.raises(InvalidTransition):
        sm.fire(trigger)
    assert sm.state is ProcessState.IDLE


def test_process_abort_from_paused():
    sm = make_process_state_machine()
    for t in ("select", "setup_done", "start", "pause"):
        sm.fire(t)
    assert sm.fire("abort") is ProcessState.IDLE


def test_control_state_host_cannot_jump_local_to_remote():
    sm = make_control_state_machine()
    sm.fire("online_local")
    assert not sm.can("online_remote")  # only the operator switch does this
    assert sm.fire("operator_remote") is ControlState.ONLINE_REMOTE
    assert sm.fire("offline") is ControlState.OFFLINE


def test_listener_called_on_transition():
    seen = []
    sm = make_control_state_machine()
    sm.listeners.append(lambda old, new, trig: seen.append((old, new, trig)))
    sm.fire("online_remote")
    assert seen == [(ControlState.OFFLINE, ControlState.ONLINE_REMOTE, "online_remote")]


def test_chamber_converges_to_target():
    chamber = ChamberModel(seed=1)
    for _ in range(100):  # 10 simulated seconds at dt=0.1
        chamber.step(0.1, temp_target=60.0, pressure_target=50.0)
    assert abs(chamber.temperature - 60.0) < 1.5
    assert abs(chamber.pressure - 50.0) < 2.0
