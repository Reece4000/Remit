"""Waking whatever waits for the next commit, from one listener per server process.

Each open stream of changes waits here rather than holding a database connection of
its own. One thread listens to the store and, when a unit commits, wakes every waiter
at once; each then reads the feed on from where it was.

A waiter takes `next()` before it reads, then waits on what it took. A commit that
lands while it is reading has already set that, so the wait returns at once, and no
commit is slept through.
"""

import asyncio
import logging
import threading

from remit.kernel import Store

log = logging.getLogger(__name__)

_POLL = 0.1
"""Seconds the listening thread waits at a time, so that stopping it is prompt."""


class Commits:
    def __init__(self, store: Store, *, reconnect: float = 1.0) -> None:
        self._store = store
        self._reconnect = reconnect
        self._stopping = threading.Event()
        self._thread: threading.Thread | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._next = asyncio.Event()

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self._loop = loop
        self._thread = threading.Thread(target=self._listen, name="remit-commits", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stopping.set()
        if self._thread is not None:
            self._thread.join()

    def next(self) -> asyncio.Event:
        """What the next commit sets. Take it before reading, then wait on it."""
        return self._next

    def _listen(self) -> None:
        while not self._stopping.is_set():
            try:
                with self._store.listen() as listener:
                    # Whatever committed while there was no listener has to be read too.
                    self._announce()
                    while not self._stopping.is_set():
                        if listener.wait(_POLL):
                            self._announce()
            except Exception:
                log.exception("lost the store's notifications of commits; listening again")
                self._stopping.wait(self._reconnect)

    def _announce(self) -> None:
        if self._loop is not None and not self._loop.is_closed():
            self._loop.call_soon_threadsafe(self._woken)

    def _woken(self) -> None:
        woken, self._next = self._next, asyncio.Event()
        woken.set()
