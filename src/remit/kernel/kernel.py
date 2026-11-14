"""The kernel: the only way anything changes.

`execute` runs a command for an actor: authorise, run the handler in a unit of work,
commit the documents, events and audit entry together. `dispatch` delivers recorded
events to subscriptions, each delivery its own unit of work.

Every unit is a serialisable transaction, so the checks a handler makes still hold when
it commits, whatever else is running. When the store cannot serialise a unit, the kernel
runs it again; handlers only touch the context, which makes that safe.
"""

import random
import time
from collections.abc import Callable, Iterable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, cast
from uuid import UUID

from pydantic import BaseModel

from .actor import Actor, ActorKind
from .codec import Codec
from .context import Context
from .document import Document
from .errors import ConcurrencyError, RegistrationError, Rejected, RemitError, Unauthorised
from .message import Command, Outcome, Recorded
from .module import Module, Subscription
from .registry import Registry
from .store import AuditEntry, Commit, Json, MemoryStore, Store, Unit

_READER = Actor(id="system:kernel", kind=ActorKind.SYSTEM)


@dataclass(frozen=True)
class DeliveryFailure:
    subscription: str
    position: int
    error: str


@dataclass(frozen=True)
class Dispatched:
    delivered: int
    failures: tuple[DeliveryFailure, ...]


class Kernel:
    def __init__(
        self,
        modules: Iterable[Module],
        *,
        settings: Iterable[BaseModel] = (),
        store: Store | None = None,
        clock: Callable[[], datetime] = lambda: datetime.now(UTC),
        attempts: int = 8,
    ) -> None:
        self.registry = Registry(modules)
        self.store: Store = store or MemoryStore()
        self._codec = Codec(self.registry)
        self._clock = clock
        self._attempts = attempts
        self._settings = {type(value): value for value in settings}
        for module in self.registry.modules.values():
            if module.settings and module.settings not in self._settings:
                raise RegistrationError(
                    f"module {module.name!r} needs {module.settings.__name__} settings"
                )
            if (module.migrations or module.sql) and not self.store.runs_sql:
                raise RegistrationError(f"module {module.name!r} needs a PostgreSQL store")

    def execute[R](
        self,
        command: Command[R],
        *,
        actor: Actor,
        idempotency_key: str | None = None,
        dry_run: bool = False,
    ) -> Outcome[R]:
        """Run a command. With `dry_run`, report what it would do and change nothing."""
        spec = self.registry.command(type(command))
        if not actor.may(spec.name):
            raise Unauthorised(f"{actor.id} does not hold the permission {spec.name}")
        key = f"{actor.id}:{idempotency_key}" if idempotency_key else None

        def work(unit: Unit) -> Outcome[R]:
            if key and (seen := unit.idempotency(key)):
                if seen["command"] != command.model_dump(mode="json"):
                    raise Rejected(
                        f"idempotency key {idempotency_key!r} was already used for another command"
                    )
                return cast(Outcome[R], self._codec.load_outcome(spec, seen))

            context = self._context(unit, actor)
            result = spec.handler(command, context)
            outcome = Outcome(result, context.written, context.emitted, committed=not dry_run)
            if dry_run:
                unit.rollback()
            else:
                record = (key, self._codec.dump_outcome(spec, command, outcome)) if key else None
                unit.commit(self._commit(context, spec.name, command, idempotency=record))
            return outcome

        return self._in_unit(work)

    def dispatch(self) -> Dispatched:
        """Deliver recorded events to subscriptions, once each and in order.

        A subscription's writes commit together with its cursor, so its effects inside
        the store happen exactly once, however many workers are dispatching. One that
        fails keeps its place and is retried on the next call. Events emitted during a
        pass are delivered on the next one, so a worker calls this until `delivered` is
        zero.
        """
        delivered = 0
        failures: list[DeliveryFailure] = []
        for subscription in self.registry.subscriptions:
            while True:
                result = self._in_unit(partial(self._deliver, subscription))
                if isinstance(result, DeliveryFailure):
                    failures.append(result)
                if result is not True:
                    break
                delivered += 1
        return Dispatched(delivered, tuple(failures))

    def get[D: Document](self, kind: type[D], id: UUID) -> D:
        return self._in_unit(lambda unit: self._context(unit, _READER).get(kind, id))

    def find[D: Document](self, kind: type[D], **equals: object) -> list[D]:
        return self._in_unit(lambda unit: self._context(unit, _READER).find(kind, **equals))

    def settings[S: BaseModel](self, kind: type[S]) -> S:
        return cast(S, self._settings[kind])

    def sql(self, query: str, parameters: Sequence[object] = ()) -> list[tuple[Any, ...]]:
        return self._in_unit(lambda unit: unit.sql(query, parameters))

    def events(self, *, after: int = 0) -> list[Recorded]:
        """The event log, in the order events were committed."""
        stored = self._in_unit(lambda unit: unit.events(after=after))
        return [self._codec.load_event(event) for event in stored]

    def audit(self) -> list[AuditEntry]:
        """Every command and delivery that committed, oldest first."""
        return self._in_unit(lambda unit: unit.audit())

    def _deliver(self, subscription: Subscription, unit: Unit) -> DeliveryFailure | bool:
        """Deliver the subscription's next event. False when it has none waiting."""
        name = self.registry.event_name(subscription.event)
        waiting = unit.events(after=unit.cursor(subscription.name), name=name, limit=1)
        if not waiting:
            return False
        recorded = self._codec.load_event(waiting[0])
        actor = Actor(id=f"system:{subscription.module}", kind=ActorKind.SYSTEM)
        context = self._context(unit, actor)
        try:
            subscription.handler(recorded.event, context)
        except ConcurrencyError:
            raise
        except RemitError as error:
            unit.rollback()
            return DeliveryFailure(subscription.name, recorded.position, str(error))
        unit.commit(
            self._commit(
                context,
                subscription.name,
                recorded.event,
                cause=recorded.position,
                cursor=(subscription.name, recorded.position),
            )
        )
        return True

    def _in_unit[T](self, work: Callable[[Unit], T]) -> T:
        for attempt in range(1, self._attempts + 1):
            try:
                with self.store.unit() as unit:
                    return work(unit)
            except ConcurrencyError:
                if attempt == self._attempts:
                    raise
                time.sleep(random.uniform(0, 0.002 * 2**attempt))  # noqa: S311  jitter, not secrecy
        raise AssertionError("unreachable")

    def _context(self, unit: Unit, actor: Actor) -> Context:
        return Context(
            actor=actor,
            now=self._clock(),
            unit=unit,
            registry=self.registry,
            settings=self._settings,
        )

    def _commit(
        self,
        context: Context,
        action: str,
        input: BaseModel,
        *,
        cause: int | None = None,
        cursor: tuple[str, int] | None = None,
        idempotency: tuple[str, Json] | None = None,
    ) -> Commit:
        documents = tuple(self._codec.dump(document) for document in context.written)
        events = tuple(self._codec.dump_event(event) for event in context.emitted)
        audit = AuditEntry(
            at=context.now,
            actor=context.actor,
            action=action,
            input=input.model_dump(mode="json"),
            documents=tuple((d.kind, d.id, d.version) for d in documents),
            events=tuple(name for name, _ in events),
            cause=cause,
        )
        return Commit(documents, events, audit, cursor=cursor, idempotency=idempotency)
