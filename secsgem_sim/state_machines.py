"""Small table-driven state machines for GEM control state and process state.

A transition table maps (current_state, trigger) -> next_state. Anything not in
the table is an invalid transition. Keeping the rules in one dict makes them
easy to read, test and explain, and the equipment code just calls fire().
"""

from __future__ import annotations

import logging
from enum import Enum
from typing import Callable, Dict, List, Tuple

log = logging.getLogger("secsgem_sim.state")


class InvalidTransition(Exception):
    pass


class StateMachine:
    def __init__(self, name: str, initial: Enum, transitions: Dict[Tuple[Enum, str], Enum]) -> None:
        self.name = name
        self._state = initial
        self._transitions = transitions
        self.listeners: List[Callable[[Enum, Enum, str], None]] = []

    @property
    def state(self) -> Enum:
        return self._state

    def can(self, trigger: str) -> bool:
        return (self._state, trigger) in self._transitions

    def fire(self, trigger: str) -> Enum:
        key = (self._state, trigger)
        if key not in self._transitions:
            raise InvalidTransition(f"{self.name}: '{trigger}' not allowed in {self._state.name}")
        old, self._state = self._state, self._transitions[key]
        log.info("%s: %s --%s--> %s", self.name, old.name, trigger, self._state.name)
        for listener in list(self.listeners):
            listener(old, self._state, trigger)
        return self._state


# --- GEM control state (simplified SEMI E30) ---------------------------------
#
# E30 has OFFLINE substates (EQUIPMENT OFFLINE, ATTEMPT ONLINE, HOST OFFLINE)
# and ONLINE substates (LOCAL, REMOTE). We collapse OFFLINE into one state.
# LOCAL vs REMOTE is chosen by the operator at the tool, never by the host.

class ControlState(Enum):
    OFFLINE = "OFFLINE"
    ONLINE_LOCAL = "ONLINE/LOCAL"
    ONLINE_REMOTE = "ONLINE/REMOTE"


# Values of the E30 CONTROLSTATE status variable for each of our states.
CONTROL_STATE_SV = {
    ControlState.OFFLINE: 3,        # 3 = HOST OFF-LINE
    ControlState.ONLINE_LOCAL: 4,   # 4 = ON-LINE LOCAL
    ControlState.ONLINE_REMOTE: 5,  # 5 = ON-LINE REMOTE
}

CONTROL_TRANSITIONS = {
    (ControlState.OFFLINE, "online_local"): ControlState.ONLINE_LOCAL,     # S1F17, switch=LOCAL
    (ControlState.OFFLINE, "online_remote"): ControlState.ONLINE_REMOTE,   # S1F17, switch=REMOTE
    (ControlState.ONLINE_LOCAL, "offline"): ControlState.OFFLINE,          # S1F15 or operator
    (ControlState.ONLINE_REMOTE, "offline"): ControlState.OFFLINE,
    (ControlState.ONLINE_LOCAL, "operator_remote"): ControlState.ONLINE_REMOTE,
    (ControlState.ONLINE_REMOTE, "operator_local"): ControlState.ONLINE_LOCAL,
}


def make_control_state_machine(initial: ControlState = ControlState.OFFLINE) -> StateMachine:
    return StateMachine("ControlState", initial, CONTROL_TRANSITIONS)


# --- Process state of the simulated etch chamber -----------------------------

class ProcessState(Enum):
    IDLE = "IDLE"            # no recipe selected
    SETUP = "SETUP"          # recipe being loaded / chamber being prepared
    READY = "READY"          # recipe loaded, waiting for START
    EXECUTING = "EXECUTING"  # processing wafers
    PAUSED = "PAUSED"        # processing suspended, can RESUME


PROCESS_TRANSITIONS = {
    (ProcessState.IDLE, "select"): ProcessState.SETUP,
    (ProcessState.SETUP, "setup_done"): ProcessState.READY,
    (ProcessState.READY, "start"): ProcessState.EXECUTING,
    (ProcessState.EXECUTING, "pause"): ProcessState.PAUSED,
    (ProcessState.PAUSED, "resume"): ProcessState.EXECUTING,
    (ProcessState.EXECUTING, "complete"): ProcessState.IDLE,
    (ProcessState.SETUP, "abort"): ProcessState.IDLE,
    (ProcessState.READY, "abort"): ProcessState.IDLE,
    (ProcessState.EXECUTING, "abort"): ProcessState.IDLE,
    (ProcessState.PAUSED, "abort"): ProcessState.IDLE,
}


def make_process_state_machine() -> StateMachine:
    return StateMachine("ProcessState", ProcessState.IDLE, PROCESS_TRANSITIONS)
