"""secsgem_sim - a from-scratch SECS-II / HSMS / GEM equipment simulator.

Layers (bottom to top):
    secs2.py        SECS-II data items (SEMI E5): binary encode/decode + SML text
    message.py      SecsMessage: stream/function/W-bit + one SECS-II body item
    hsms.py         HSMS transport (SEMI E37) over TCP with asyncio
    equipment.py    GEM equipment behaviour (subset of SEMI E30) for an etch chamber
    host.py         Host (MES-side) client with a small async API
"""

__version__ = "0.1.0"
