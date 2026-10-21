"""Command line entry point.

    python -m secsgem_sim equipment --port 5000
    python -m secsgem_sim host --port 5000 --demo
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from typing import List, Optional

from .equipment import GemEquipment
from .hsms import HsmsTimeouts
from .logging_setup import setup_logging

log = logging.getLogger("secsgem_sim.cli")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="secsgem_sim", description="SECS/GEM equipment simulator")
    sub = parser.add_subparsers(dest="role", required=True)

    def common(p: argparse.ArgumentParser) -> None:
        p.add_argument("--address", default="127.0.0.1", help="IP to listen on / connect to")
        p.add_argument("--port", type=int, default=5000)
        p.add_argument("--device-id", type=int, default=0, help="SECS device id (HSMS session id)")
        p.add_argument("--t3", type=float, default=45.0, help="reply timeout (s)")
        p.add_argument("--t6", type=float, default=5.0, help="control transaction timeout (s)")
        p.add_argument("--t7", type=float, default=10.0, help="not-selected timeout (s)")
        p.add_argument("--linktest", type=float, default=30.0, help="linktest interval (s), 0 = off")
        p.add_argument("--log-level", default="INFO")
        p.add_argument("--log-format", choices=["text", "json"], default="text")
        p.add_argument("--no-trace", action="store_true", help="hide per-message SML trace")

    eq = sub.add_parser("equipment", help="run the simulated etch tool (HSMS passive)")
    common(eq)
    eq.add_argument("--local", action="store_true", help="operator switch in LOCAL (rejects remote commands)")
    eq.add_argument("--seed", type=int, default=None, help="random seed for sensor noise")

    host = sub.add_parser("host", help="run the host / MES side (HSMS active)")
    common(host)
    host.add_argument("--demo", action="store_true", help="run the scripted end-to-end scenario")
    return parser


def _timeouts(args: argparse.Namespace) -> HsmsTimeouts:
    return HsmsTimeouts(t3=args.t3, t6=args.t6, t7=args.t7, linktest=args.linktest or None)


async def run_equipment(args: argparse.Namespace) -> None:
    equipment = GemEquipment(
        args.address, args.port, device_id=args.device_id, timeouts=_timeouts(args),
        seed=args.seed, operator_remote=not args.local,
    )
    await equipment.start()
    try:
        await asyncio.Event().wait()   # run until Ctrl-C / kill
    finally:
        await equipment.stop()


async def run_host(args: argparse.Namespace) -> None:
    from .demo import describe_alarm, describe_event, run_demo
    from .host import GemHost

    host = GemHost(args.address, args.port, device_id=args.device_id, timeouts=_timeouts(args))
    if args.demo:
        await run_demo(host)
        return
    # Monitor mode: connect, go online, enable everything, print what arrives.
    host.on_event(describe_event)
    host.on_alarm(describe_alarm)
    await host.connect()
    await host.establish_communications()
    await host.request_online()
    await host.enable_events()
    await host.enable_alarms()
    log.info("monitoring events and alarms; Ctrl-C to quit")
    try:
        await host.connection.closed.wait()
    finally:
        await host.close()


def main(argv: Optional[List[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(args.log_level, args.log_format, trace=not args.no_trace)
    runner = run_equipment if args.role == "equipment" else run_host
    try:
        asyncio.run(runner(args))
    except KeyboardInterrupt:
        log.info("interrupted")
    return 0
