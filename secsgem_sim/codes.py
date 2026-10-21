"""Acknowledge codes used in GEM replies (values from SEMI E5).

Each reply carries a one-byte code (sent as a <B> item) that tells the host
whether its request was accepted, and if not, why.
"""

from enum import IntEnum


class HCACK(IntEnum):
    """S2F42 Host Command Acknowledge."""
    OK = 0                      # command performed
    INVALID_COMMAND = 1         # no such RCMD
    CANNOT_PERFORM_NOW = 2      # e.g. not ONLINE/REMOTE, or wrong process state
    INVALID_PARAMETER = 3       # at least one CPNAME/CPVAL is bad (see CPACK list)
    ACK_FINISH_LATER = 4        # accepted; completion is signalled later by an event
    ALREADY_IN_CONDITION = 5    # e.g. PAUSE while already paused
    NO_SUCH_OBJECT = 6


class CPACK(IntEnum):
    """Per-parameter error in S2F42."""
    NAME_DOES_NOT_EXIST = 1
    ILLEGAL_VALUE = 2
    ILLEGAL_FORMAT = 3


class COMMACK(IntEnum):
    """S1F14 Establish Communications Acknowledge."""
    ACCEPTED = 0
    DENIED = 1


class OFLACK(IntEnum):
    """S1F16 Offline Acknowledge."""
    ACK = 0


class ONLACK(IntEnum):
    """S1F18 Online Acknowledge."""
    ACCEPTED = 0
    NOT_ALLOWED = 1
    ALREADY_ONLINE = 2


class EAC(IntEnum):
    """S2F16 Equipment Acknowledge Code (set equipment constants)."""
    OK = 0
    UNKNOWN_ECID = 1
    BUSY = 2
    OUT_OF_RANGE = 3


class DRACK(IntEnum):
    """S2F34 Define Report Acknowledge."""
    OK = 0
    INSUFFICIENT_SPACE = 1
    INVALID_FORMAT = 2
    RPTID_ALREADY_DEFINED = 3
    UNKNOWN_VID = 4


class LRACK(IntEnum):
    """S2F36 Link Report Acknowledge."""
    OK = 0
    INSUFFICIENT_SPACE = 1
    INVALID_FORMAT = 2
    CEID_ALREADY_LINKED = 3
    UNKNOWN_CEID = 4
    UNKNOWN_RPTID = 5


class ERACK(IntEnum):
    """S2F38 Enable/Disable Event Report Acknowledge."""
    OK = 0
    UNKNOWN_CEID = 1


class ACKC5(IntEnum):
    """S5F2 / S5F4 alarm acknowledge."""
    OK = 0
    ERROR = 1


class ACKC6(IntEnum):
    """S6F12 event report acknowledge."""
    OK = 0


# ALCD (alarm code) byte: bit 8 = alarm set (1) / cleared (0); bits 1-7 = category.
ALCD_SET_BIT = 0x80


class AlarmCategory(IntEnum):
    PERSONAL_SAFETY = 1
    EQUIPMENT_SAFETY = 2
    PARAMETER_CONTROL_WARNING = 3
    PARAMETER_CONTROL_ERROR = 4
    IRRECOVERABLE_ERROR = 5
    EQUIPMENT_STATUS_WARNING = 6
    ATTENTION_FLAGS = 7
    DATA_INTEGRITY = 8


# ALED byte in S5F3: bit 8 set = enable, 0 = disable.
ALED_ENABLE = 0x80

# S9Fx system error functions.
S9_UNRECOGNIZED_DEVICE_ID = 1
S9_UNRECOGNIZED_STREAM = 3
S9_UNRECOGNIZED_FUNCTION = 5
S9_ILLEGAL_DATA = 7
S9_TRANSACTION_TIMEOUT = 9

S9_DESCRIPTIONS = {
    1: "Unrecognized Device ID",
    3: "Unrecognized Stream Type",
    5: "Unrecognized Function Type",
    7: "Illegal Data",
    9: "Transaction Timer Timeout",
    11: "Data Too Long",
    13: "Conversation Timeout",
}
