"""Pytest configuration.

We only depend on pytest, not pytest-asyncio, so this small hook runs any
`async def test_...` function with asyncio.run(). Each test gets a fresh event
loop and a hard 20 s timeout so a protocol bug can never hang the suite.
"""

import asyncio
import inspect

import pytest


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    if inspect.iscoroutinefunction(pyfuncitem.obj):
        argnames = pyfuncitem._fixtureinfo.argnames
        kwargs = {name: pyfuncitem.funcargs[name] for name in argnames}
        asyncio.run(asyncio.wait_for(pyfuncitem.obj(**kwargs), timeout=20))
        return True
    return None
