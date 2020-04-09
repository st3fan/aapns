""" Observed connection outcomes, so far:
    * Closed('ErrorCodes.NO_ERROR')
    * Closed('[Errno 54] Connection reset by peer') 
    * Closed('Server closed the connection')
"""
import logging
from asyncio import CancelledError, create_subprocess_exec, create_task, gather, sleep
from asyncio.subprocess import PIPE
from contextlib import asynccontextmanager, suppress
from os import killpg
from signal import SIGTERM

import pytest

pytestmark = pytest.mark.asyncio


async def collect(stream, name, output=[]):
    with suppress(CancelledError):
        async for blob in stream:
            line = blob.decode("utf-8").strip()
            logging.warning("%s: %s", name, line)
            output.append(line)


@asynccontextmanager
async def server_factory(flavour):
    server = await create_subprocess_exec(
        "go",
        "run",
        f"tests/stress/server-{flavour}.go",
        stdout=PIPE,
        stderr=PIPE,
        start_new_session=True,
    )
    try:
        output = []
        to = create_task(collect(server.stdout, "server:stdout", output))
        te = create_task(collect(server.stderr, "server:stderr", output))
        try:
            for delay in (2 ** i for i in range(-10, 3)):  # max 8s total
                await sleep(delay)
                if "exit status" in " ".join(output):
                    raise OSError(f"test server {flavour!r} crashed")
                if "Serving on" in " ".join(output):
                    break
            else:
                raise TimeoutError(f"test server {flavour!r} did not come up")
            yield server
        finally:
            to.cancel()
            te.cancel()
            with suppress(CancelledError):
                gather(te, to)
    finally:
        with suppress(ProcessLookupError):
            # server.terminate() is not enough,because `go run`'s child somehow survives
            # Thus, I'm starting the server in a new session and kill the entire session
            killpg(server.pid, SIGTERM)
        with suppress(CancelledError):
            await server.wait()


@pytest.fixture
async def ok_server():
    async with server_factory("ok") as s:
        yield s


async def test_nothing(ok_server):
    await sleep(5)
    1 / 0
