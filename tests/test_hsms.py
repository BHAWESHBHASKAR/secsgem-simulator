"""HSMS transport tests: header layout, framing, control messages and timers."""

import asyncio
import struct

import pytest

from secsgem_sim.hsms import (
    ConnectionState, ControlTimeoutError, HsmsHeader, HsmsPassiveServer, HsmsProtocolError,
    HsmsTimeouts, NotSelectedError, RejectReason, ReplyTimeoutError, SType,
    control_header, data_header, frame, open_active_connection,
)
from secsgem_sim.message import SecsMessage
from secsgem_sim.secs2 import A, L, U4

FAST = HsmsTimeouts(t3=0.5, t5=0.1, t6=0.5, t7=0.5, t8=0.5, linktest=None)


# --- pure header / framing tests -----------------------------------------

def test_header_round_trip():
    header = HsmsHeader(0x0001, 0x81, 13, 0, 0, 0xDEADBEEF)
    packed = header.pack()
    assert len(packed) == 10
    assert packed == bytes([0x00, 0x01, 0x81, 0x0D, 0x00, 0x00, 0xDE, 0xAD, 0xBE, 0xEF])
    assert HsmsHeader.unpack(packed) == header


def test_data_header_sets_wbit_stream_function():
    msg = SecsMessage(6, 11, True, system_bytes=42, session_id=7)
    header = data_header(msg)
    assert header.wbit and header.stream == 6 and header.function == 11
    assert header.stype == SType.DATA and header.session_id == 7 and header.system_bytes == 42


def test_control_header_uses_session_ffff():
    header = control_header(SType.SELECT_REQ, 5)
    assert header.pack() == bytes([0xFF, 0xFF, 0, 0, 0, 1, 0, 0, 0, 5])


def test_frame_length_prefix_counts_header_and_body():
    data = frame(control_header(SType.LINKTEST_REQ, 1), b"\x01\x00")
    assert struct.unpack(">I", data[:4])[0] == 12
    assert len(data) == 4 + 12


def test_header_wrong_size_rejected():
    with pytest.raises(HsmsProtocolError):
        HsmsHeader.unpack(b"\x00" * 9)


# --- helpers for socket tests ----------------------------------------------

async def echo_handler(msg):
    """Reply to SxFy with SxF(y+1) carrying the same body."""
    return SecsMessage(msg.stream, msg.function + 1, body=msg.body)


async def start_server(handler=echo_handler, timeouts=FAST):
    server = HsmsPassiveServer("127.0.0.1", 0, message_handler=handler, timeouts=timeouts)
    await server.start()
    return server


async def read_frame(reader):
    (length,) = struct.unpack(">I", await reader.readexactly(4))
    data = await reader.readexactly(length)
    return HsmsHeader.unpack(data[:10]), data[10:]


# --- socket tests ----------------------------------------------------------

async def test_select_then_request_reply_matches_system_bytes():
    server = await start_server()
    host = await open_active_connection("127.0.0.1", server.port, timeouts=FAST)
    try:
        assert host.state is ConnectionState.SELECTED
        await asyncio.sleep(0.05)
        assert server.connection.state is ConnectionState.SELECTED
        # Two concurrent requests: each reply must go to the right caller.
        r1, r2 = await asyncio.gather(
            host.send_request(SecsMessage(1, 3, True, L(U4(1)))),
            host.send_request(SecsMessage(1, 3, True, L(U4(2)))),
        )
        assert r1.function == 4 and r1.body == L(U4(1))
        assert r2.body == L(U4(2))
        assert r1.system_bytes != r2.system_bytes
    finally:
        await host.separate()
        await server.stop()


async def test_linktest_round_trip():
    server = await start_server()
    host = await open_active_connection("127.0.0.1", server.port, timeouts=FAST)
    try:
        rtt = await host.linktest()
        assert 0 <= rtt < FAST.t6
    finally:
        await host.separate()
        await server.stop()


async def test_periodic_linktest_keeps_connection_alive():
    timeouts = HsmsTimeouts(t3=1, t6=0.5, t7=1, linktest=0.05)
    server = await start_server(timeouts=timeouts)
    host = await open_active_connection("127.0.0.1", server.port, timeouts=timeouts)
    try:
        await asyncio.sleep(0.3)  # several linktest cycles in both directions
        assert host.is_selected and server.connection.is_selected
    finally:
        await host.separate()
        await server.stop()


async def test_data_before_select_is_rejected():
    server = await start_server()
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        writer.write(frame(data_header(SecsMessage(1, 1, True, system_bytes=99))))
        header, _ = await asyncio.wait_for(read_frame(reader), 1)
        assert header.stype == SType.REJECT_REQ
        assert header.byte3 == RejectReason.ENTITY_NOT_SELECTED
        assert header.system_bytes == 99
    finally:
        writer.close()
        await server.stop()


async def test_cannot_send_data_when_not_selected():
    server = await start_server()
    host = await open_active_connection("127.0.0.1", server.port, timeouts=FAST)
    try:
        await host.deselect()
        assert host.state is ConnectionState.NOT_SELECTED
        with pytest.raises(NotSelectedError):
            await host.send_request(SecsMessage(1, 1, True))
    finally:
        await host.separate()
        await server.stop()


async def test_t3_reply_timeout():
    async def never_reply(msg):
        await asyncio.sleep(10)

    server = await start_server(handler=never_reply)
    host = await open_active_connection("127.0.0.1", server.port, timeouts=FAST)
    try:
        with pytest.raises(ReplyTimeoutError):
            await host.send_request(SecsMessage(1, 1, True))
        assert host.is_selected  # T3 is a transaction failure, not a link failure
    finally:
        await host.separate()
        await server.stop()


async def test_t6_control_timeout_closes_connection():
    async def silent_peer(reader, writer):
        await reader.read()  # accept TCP but never answer Select.req
        writer.close()

    silent = await asyncio.start_server(silent_peer, "127.0.0.1", 0)
    port = silent.sockets[0].getsockname()[1]
    try:
        with pytest.raises(ControlTimeoutError):
            await open_active_connection("127.0.0.1", port, timeouts=FAST)
    finally:
        silent.close()
        await silent.wait_closed()


async def test_t7_not_selected_timeout_drops_connection():
    server = await start_server(timeouts=HsmsTimeouts(t7=0.2, linktest=None))
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        data = await asyncio.wait_for(reader.read(), 2)  # never send Select.req
        assert data == b""  # EOF: the passive side hung up
    finally:
        writer.close()
        await server.stop()


async def test_separate_closes_both_sides():
    server = await start_server()
    host = await open_active_connection("127.0.0.1", server.port, timeouts=FAST)
    await host.separate()
    await asyncio.wait_for(server.connection.closed.wait(), 1)
    assert server.connection.state is ConnectionState.NOT_CONNECTED
    await server.stop()


async def test_second_connection_refused():
    server = await start_server()
    host = await open_active_connection("127.0.0.1", server.port, timeouts=FAST)
    reader, writer = await asyncio.open_connection("127.0.0.1", server.port)
    try:
        assert await asyncio.wait_for(reader.read(), 1) == b""
        assert host.is_selected
    finally:
        writer.close()
        await host.separate()
        await server.stop()


async def test_undecodable_body_is_flagged_not_fatal():
    received = []

    async def handler(msg):
        received.append(msg)
        return None

    server = await start_server(handler=handler)
    host = await open_active_connection("127.0.0.1", server.port, timeouts=FAST)
    try:
        header = data_header(SecsMessage(1, 3, False, system_bytes=5))
        host._writer.write(frame(header, b"\xFD\x00"))  # unknown format code
        await asyncio.sleep(0.1)
        assert received and received[0].decode_error
        assert server.connection.is_selected
    finally:
        await host.separate()
        await server.stop()
