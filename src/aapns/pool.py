from __future__ import annotations

import asyncio
import ssl
from asyncio import (
    CancelledError,
    Event,
    TimeoutError,
    create_task,
    gather,
    sleep,
    wait_for,
)
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field
from itertools import count
from logging import getLogger
from random import shuffle
from time import time
from typing import Optional, Set

from .connection import Connection, Request, Response, create_ssl_context
from .errors import Blocked, Closed, FormatError, ResponseTooLarge, Timeout

logger = getLogger(__package__)


@dataclass(eq=False)
class Pool:
    """Simple fixed-size connection pool with automatic replacement"""

    origin: str
    size: int
    ssl_context: ssl.SSLContext
    active: Set[Connection]
    dying: Set[Connection] = field(default_factory=set)
    closing: bool = False
    closed: bool = False
    errors: int = 0
    retrying: int = 0
    completed: int = 0
    outcome: Optional[str] = None
    maintenance: asyncio.Task = field(init=False)
    maintenance_needed: asyncio.Event = field(default_factory=asyncio.Event)

    def __repr__(self):
        bits = [
            self.state,
            self.origin,
            f"alive:{len(self.active)}",
            f"dying:{len(self.dying)}",
        ]
        if self.state != "closed":
            all = self.active | self.dying
            bits.append(f"buffered:{sum(connection.buffered for connection in all)}")
            bits.append(f"inflight:{sum(connection.inflight for connection in all)}")
        bits.append(f"retrying:{self.retrying}")
        bits.append(f"completed:{self.completed}")
        bits.append(f"errors:{self.errors}")
        return "<Pool %s>" % " ".join(bits)

    @classmethod
    async def create(cls, origin: str, size=2, ssl=None):
        if size < 1:
            raise ValueError("Connection pool size must be strictly positive")
        ssl_context = ssl or create_ssl_context()
        connections = set(
            await gather(
                *(Connection.create(origin, ssl=ssl_context) for i in range(size))
            )
        )
        # FIXME run the hook / ensure no connection is dead
        return cls(origin, size, ssl_context, connections)

    def __post_init__(self):
        self.maintenance = create_task(self.maintain(), name="maintenance")

    @property
    def state(self):
        if not self.closing:
            return "active"
        elif not self.closed:
            return "closing"
        else:
            return "closed"

    def resize(self, size):
        assert size > 0
        self.size = size
        self.maintenance_needed.set()

    def termination_hook(self, connection):
        if not self.outcome and connection.outcome == "BadCertificateEnvironment":
            self.closing = True
            self.outcome = connection.outcome

    async def maintain(self):
        while True:
            if self.closing or self.closed:
                return

            for connection in list(self.active):
                if connection.closing:
                    self.active.remove(connection)
                    self.dying.add(connection)
                    self.termination_hook(connection)

            while len(self.active) > self.size:
                connection = self.active.pop()
                connection.closing = True
                self.dying.add(connection)
                self.termination_hook(connection)

            for connection in list(self.dying):
                if connection.closed:
                    self.dying.remove(connection)
                    self.termination_hook(connection)
                elif not connection.channels:
                    self.dying.remove(connection)
                    try:
                        await connection.close()
                    finally:
                        self.termination_hook(connection)
                if self.closing or self.closed:
                    return

            while len(self.active) < self.size:
                if not await self.add_one_connection():
                    break
                if self.closing or self.closed:
                    return

            # FIXME wait for a trigger:
            # * some connection state has changed
            with suppress(TimeoutError):
                await wait_for(self.maintenance_needed.wait(), timeout=1)
            self.maintenance_needed.clear()

    async def add_one_connection(self):
        try:
            connection = await Connection.create(self.origin, ssl=self.ssl)
            self.active.add(connection)
            self.termination_hook(connection)
            return True
        except Exception:
            logger.exception("Failed creating APN connection")

    async def close(self):
        self.closing = True
        if not self.outcome:
            self.outcome = "Closed"
        try:
            if self.maintenance:
                self.maintenance.cancel()
                with suppress(CancelledError):
                    await self.maintenance

            await gather(
                *(connection.close() for connection in self.active | self.dying)
            )
        finally:
            self.closed = True

    async def post(self, req: "Request") -> "Response":
        with self.count_requests():
            for delay in (10 ** i for i in count(-3, 0.5)):
                if self.closing:
                    raise Closed(self.outcome)

                try:
                    return await self.post_once(req)
                except Blocked:
                    pass

                if self.closing:
                    raise Closed(self.outcome)

                if time() + delay > req.deadline:
                    raise Timeout()

                try:
                    self.retrying += 1
                    await sleep(delay)
                finally:
                    self.retrying -= 1

            assert False, "unreachable"

    @contextmanager
    def count_requests(self):
        try:
            yield
        except:
            self.errors += 1
            raise
        else:
            self.completed += 1

    async def post_once(self, req: "Request") -> "Response":
        # FIXME ideally, follow weighted round-robin discipline:
        # * generally allocate requests evenly across connections
        # * but keep load for few last connections lighter
        #   to prevent all connections expiring at once
        # * ideally track connection backlog

        # FIXME handle connection getting closed
        # FIXME handle connection replacement
        active = list(self.active)
        shuffle(active)
        for connection in active:
            if self.closing:
                raise Closed(self.outcome)
            if connection.closed:
                continue
            try:
                return await connection.post(req)
            except (Blocked, Closed):
                pass
        else:
            raise Blocked()
