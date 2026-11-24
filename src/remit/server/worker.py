"""The worker: delivers recorded events to subscriptions, for as long as it runs.

It dispatches until nothing is waiting, then sleeps until a unit commits. A delivery
that fails keeps its place and is tried again, no sooner than `retry` seconds later, so
a subscription that keeps failing does not spin. Any number of workers can run at
once: each event is still delivered to each subscription exactly once.
"""

import logging
import threading
import time

from remit.kernel import Kernel, Listener

log = logging.getLogger(__name__)

_POLL = 0.1
"""Seconds the worker sleeps at a time, so that it stops promptly when asked."""


def work(kernel: Kernel, *, stop: threading.Event, idle: float = 30.0, retry: float = 5.0) -> None:
    """Dispatch until `stop` is set. `idle` bounds a sleep, should a notification be lost."""
    with kernel.store.listen() as listener:
        while not stop.is_set():
            dispatched = kernel.dispatch()
            for failure in dispatched.failures:
                log.error(
                    "%s failed on event %d: %s",
                    failure.subscription,
                    failure.position,
                    failure.error,
                )
            if dispatched.delivered:
                continue
            if dispatched.failures:
                stop.wait(retry)
            else:
                _sleep(listener, stop, idle)


def _sleep(listener: Listener, stop: threading.Event, seconds: float) -> None:
    """Until a unit commits, `stop` is set, or the time is up, whichever comes first."""
    until = time.monotonic() + seconds
    while not stop.is_set() and time.monotonic() < until:
        if listener.wait(_POLL):
            return
